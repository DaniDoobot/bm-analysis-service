"""
Criteria sync service — business logic to synchronize visible criteria names in historical results.
"""
import logging
from typing import Any
from sqlalchemy import select, func, update, text
from sqlalchemy.orm.attributes import flag_modified

from app.models.criteria import PromptCriterion, CriteriaSyncLog
from app.models.analyses import AnalysisCriterionResult
from app.models.mass_evaluations import MassEvaluationCriterionResult, MassEvaluationResult

logger = logging.getLogger(__name__)


class ConcurrencyConflictError(Exception):
    """Raised when the expected counts do not match the current database counts."""
    pass


async def preview_sync_criteria_names(db: AsyncSession, prompt_id: int | None = None) -> dict[str, Any]:
    """
    Previews the synchronization of criterion names in historical results.
    Does not modify any database records.
    """
    # 1. Fetch criteria
    stmt = select(PromptCriterion)
    if prompt_id is not None:
        stmt = stmt.where(PromptCriterion.prompt_id == prompt_id)
    res = await db.execute(stmt)
    criteria = res.scalars().all()

    # Name validation check
    for criterion in criteria:
        c_name = criterion.criterion_name
        if not c_name or not c_name.strip():
            raise ValueError("El nombre del criterio no puede estar vacío.")
        if len(c_name) > 255:
            raise ValueError("El nombre del criterio es excesivamente largo (máximo 255 caracteres).")

    details = []
    total_criteria_to_sync = 0
    individual_results_to_update = 0
    mass_results_to_update = 0

    # 2. Fetch parent MassEvaluationResult rows early if we want to check items_json
    mass_results_stmt = select(MassEvaluationResult)
    if prompt_id is not None:
        mass_results_stmt = mass_results_stmt.where(MassEvaluationResult.prompt_id == prompt_id)
    mass_results_res = await db.execute(mass_results_stmt)
    all_mass_results = mass_results_res.scalars().all()

    for criterion in criteria:
        c_id = criterion.criterion_id
        c_name = criterion.criterion_name
        c_key = criterion.criterion_key
        c_prompt_id = criterion.prompt_id

        # A. Check individual results mismatch
        ind_count_stmt = select(func.count(AnalysisCriterionResult.id)).where(
            AnalysisCriterionResult.criterion_id == c_id,
            AnalysisCriterionResult.criterion_name != c_name
        )
        ind_count = await db.scalar(ind_count_stmt) or 0

        # B. Check mass criterion results mismatch
        mass_count_stmt = select(func.count(MassEvaluationCriterionResult.id)).where(
            MassEvaluationCriterionResult.criterion_id == c_id,
            MassEvaluationCriterionResult.criterion_name != c_name
        )
        mass_count = await db.scalar(mass_count_stmt) or 0

        # C. Find distinct old names from tables
        old_names = set()
        if ind_count > 0:
            ind_names_stmt = select(AnalysisCriterionResult.criterion_name).where(
                AnalysisCriterionResult.criterion_id == c_id,
                AnalysisCriterionResult.criterion_name != c_name
            ).distinct()
            ind_names_res = await db.execute(ind_names_stmt)
            for name in ind_names_res.scalars().all():
                if name:
                    old_names.add(name)

        if mass_count > 0:
            mass_names_stmt = select(MassEvaluationCriterionResult.criterion_name).where(
                MassEvaluationCriterionResult.criterion_id == c_id,
                MassEvaluationCriterionResult.criterion_name != c_name
            ).distinct()
            mass_names_res = await db.execute(mass_names_stmt)
            for name in mass_names_res.scalars().all():
                if name:
                    old_names.add(name)

        # D. Check mismatches in items_json of mass results
        items_mismatch_count = 0
        for r in all_mass_results:
            if not isinstance(r.items_json, list):
                continue
            for item in r.items_json:
                if not isinstance(item, dict):
                    continue
                # Match by criterion_id or criterion_key
                item_cid = item.get("criterion_id")
                item_ckey = item.get("criterion_key") or item.get("output_key")
                match_by_id = (item_cid is not None and c_id is not None and item_cid == c_id)
                match_by_key = (item_ckey is not None and c_key is not None and item_ckey == c_key and r.prompt_id == c_prompt_id)
                
                if (match_by_id or match_by_key) and item.get("name") != c_name:
                    items_mismatch_count += 1
                    if item.get("name"):
                        old_names.add(item.get("name"))

        # If there are mismatches in individual results, mass criterion results, or items_json
        if ind_count > 0 or mass_count > 0 or items_mismatch_count > 0:
            total_criteria_to_sync += 1
            individual_results_to_update += ind_count
            mass_results_to_update += mass_count

            # Prepare old name representation
            old_name_str = ", ".join(sorted(old_names)) if old_names else ""
            
            details.append({
                "prompt_id": c_prompt_id,
                "criterion_key": c_key,
                "old_name": old_name_str,
                "new_name": c_name,
                "individual_rows_affected": ind_count,
                "mass_rows_affected": mass_count
            })

    return {
        "total_criteria_to_sync": total_criteria_to_sync,
        "individual_results_to_update": individual_results_to_update,
        "mass_results_to_update": mass_results_to_update,
        "details": details
    }


async def execute_sync_criteria_names(
    db: AsyncSession,
    prompt_id: int | None = None,
    performed_by_email: str | None = None,
    expected_individual_results_to_update: int | None = None,
    expected_mass_results_to_update: int | None = None
) -> dict[str, Any]:
    """
    Executes the synchronization of criterion names in historical results.
    Modifies database records inside the current transaction.
    """
    # PostgreSQL transactional advisory lock to prevent concurrent executions
    if db.bind and db.bind.dialect.name == "postgresql":
        logger.info("Acquiring transactional advisory lock for criteria sync...")
        await db.execute(text("SELECT pg_advisory_xact_lock(987654321)"))

    # 1. Recalculate current counts for concurrency checks
    current_preview = await preview_sync_criteria_names(db, prompt_id=prompt_id)
    
    current_ind = current_preview["individual_results_to_update"]
    current_mass = current_preview["mass_results_to_update"]

    # Concurrency verification
    if expected_individual_results_to_update is not None and expected_individual_results_to_update != current_ind:
        raise ConcurrencyConflictError(
            f"Concurrency conflict: expected individual results to update ({expected_individual_results_to_update}) "
            f"does not match current count ({current_ind})."
        )
    if expected_mass_results_to_update is not None and expected_mass_results_to_update != current_mass:
        raise ConcurrencyConflictError(
            f"Concurrency conflict: expected mass results to update ({expected_mass_results_to_update}) "
            f"does not match current count ({current_mass})."
        )

    # 2. Fetch criteria
    stmt = select(PromptCriterion)
    if prompt_id is not None:
        stmt = stmt.where(PromptCriterion.prompt_id == prompt_id)
    res = await db.execute(stmt)
    criteria = res.scalars().all()

    # Pre-load parent MassEvaluationResult rows for items_json update
    result_stmt = select(MassEvaluationResult)
    if prompt_id is not None:
        result_stmt = result_stmt.where(MassEvaluationResult.prompt_id == prompt_id)
    result_res = await db.execute(result_stmt)
    mass_results = result_res.scalars().all()

    individual_criteria_rows_updated = 0
    mass_criteria_rows_updated = 0
    mass_results_rows_updated = 0

    # Maps for items_json matches
    id_to_name = {c.criterion_id: c.criterion_name for c in criteria}
    key_to_name = {(c.prompt_id, c.criterion_key): c.criterion_name for c in criteria}

    # 3. Synchronize each criterion
    for criterion in criteria:
        c_id = criterion.criterion_id
        c_name = criterion.criterion_name
        c_key = criterion.criterion_key
        c_prompt_id = criterion.prompt_id

        # A. Collect old names for audit log
        old_names = set()
        
        # Check individual results old names
        ind_names_stmt = select(AnalysisCriterionResult.criterion_name).where(
            AnalysisCriterionResult.criterion_id == c_id,
            AnalysisCriterionResult.criterion_name != c_name
        ).distinct()
        ind_names_res = await db.execute(ind_names_stmt)
        for name in ind_names_res.scalars().all():
            if name:
                old_names.add(name)

        # Check mass criterion results old names
        mass_names_stmt = select(MassEvaluationCriterionResult.criterion_name).where(
            MassEvaluationCriterionResult.criterion_id == c_id,
            MassEvaluationCriterionResult.criterion_name != c_name
        ).distinct()
        mass_names_res = await db.execute(mass_names_stmt)
        for name in mass_names_res.scalars().all():
            if name:
                old_names.add(name)

        # B. Perform physical updates on AnalysisCriterionResult
        ind_stmt = (
            update(AnalysisCriterionResult)
            .where(
                AnalysisCriterionResult.criterion_id == c_id,
                AnalysisCriterionResult.criterion_name != c_name
            )
            .values(criterion_name=c_name)
        )
        ind_res = await db.execute(ind_stmt)
        ind_affected = ind_res.rowcount
        individual_criteria_rows_updated += ind_affected

        # C. Perform physical updates on MassEvaluationCriterionResult
        mass_stmt = (
            update(MassEvaluationCriterionResult)
            .where(
                MassEvaluationCriterionResult.criterion_id == c_id,
                MassEvaluationCriterionResult.criterion_name != c_name
            )
            .values(criterion_name=c_name)
        )
        mass_res = await db.execute(mass_stmt)
        mass_affected = mass_res.rowcount
        mass_criteria_rows_updated += mass_affected

        # D. Update items_json for this specific criterion and track affected parent rows
        criterion_mass_parent_rows_affected = 0
        for r in mass_results:
            if not isinstance(r.items_json, list):
                continue
            modified = False
            new_items = []
            for item in r.items_json:
                if not isinstance(item, dict):
                    new_items.append(item)
                    continue
                item_cid = item.get("criterion_id")
                item_ckey = item.get("criterion_key") or item.get("output_key")
                match_by_id = (item_cid is not None and c_id is not None and item_cid == c_id)
                match_by_key = (item_ckey is not None and c_key is not None and item_ckey == c_key and r.prompt_id == c_prompt_id)
                
                if (match_by_id or match_by_key) and item.get("name") != c_name:
                    if item.get("name"):
                        old_names.add(item.get("name"))
                    item_copy = dict(item)
                    item_copy["name"] = c_name
                    new_items.append(item_copy)
                    modified = True
                else:
                    new_items.append(item)
            
            if modified:
                r.items_json = new_items
                flag_modified(r, "items_json")
                db.add(r)
                criterion_mass_parent_rows_affected += 1
                mass_results_rows_updated += 1  # Note: this is global counter, but we deduplicate globally later

        # E. Persist CriteriaSyncLog if any row was affected
        if ind_affected > 0 or mass_affected > 0 or criterion_mass_parent_rows_affected > 0:
            old_name_str = ", ".join(sorted(old_names)) if old_names else ""
            log_entry = CriteriaSyncLog(
                prompt_id=c_prompt_id,
                criterion_id=c_id,
                criterion_key=c_key,
                old_name=old_name_str,
                new_name=c_name,
                individual_rows_affected=ind_affected,
                mass_rows_affected=mass_affected,
                mass_results_rows_affected=criterion_mass_parent_rows_affected,
                performed_by_email=performed_by_email
            )
            db.add(log_entry)

    # De-duplicate mass_results_rows_updated because multiple criteria updates in a single MassEvaluationResult
    # could increment the count multiple times, but we want the actual number of unique parent rows updated.
    # Let's count how many mass_results are currently dirty.
    unique_mer_updated = 0
    for r in db.dirty:
        if isinstance(r, MassEvaluationResult):
            unique_mer_updated += 1

    return {
        "ok": True,
        "individual_criteria_rows_updated": individual_criteria_rows_updated,
        "mass_criteria_rows_updated": mass_criteria_rows_updated,
        "mass_results_rows_updated": unique_mer_updated
    }
