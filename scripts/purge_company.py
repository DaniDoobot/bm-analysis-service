#!/usr/bin/env python
"""
scripts/purge_company.py
========================
Safe internal tool for transactional purging of demo/test companies.

Key Safety Rules:
1. Dry-run by default: without --apply, no changes are committed to the database.
2. Protection: company_id=1 (Boston Medical) is strictly forbidden.
3. Protection: Non-demo companies (is_demo=False) are rejected unless --force-non-demo is explicitly passed.
4. Target restriction: By default, allows targeting company_id in {2, 3}.
5. Transactional: All deletions occur in a single atomic transaction.
6. FK-Safe: Reverse-dependency order prevents RESTRICT violations and orphaned SET NULL records.
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set

# Ensure application root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User, UserAudit, PasswordResetToken
from app.models.typologies import Typology
from app.models.analyses import Analysis, CallAnalysisCurrent, AnalysisResult, AnalysisCriterionResult
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
)
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingEvaluationPrompt,
    TrainingAgentSetting,
)
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)
from app.models.prompts import (
    Prompt,
    PromptVersion,
    PromptBaseStructure,
    StructurePermission,
    StructurePermissionAudit,
    BaseStructureTypology,
)
from app.models.criteria import PromptCriterion, PromptCriterionTypology, CriteriaSyncLog
from app.models.drafts import PromptDraft

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("purge_company")


class CompanyPurgeError(Exception):
    """Base exception for company purge errors."""
    pass


class CompanyNotFoundError(CompanyPurgeError):
    """Raised when the requested company is not found in database."""
    pass


class CompanyProtectedError(CompanyPurgeError):
    """Raised when a safety guard blocks deletion."""
    pass


ALLOWED_DEFAULT_IDS: Set[int] = {2, 3}


async def _get_existing_tables(session: AsyncSession) -> Set[str]:
    """Inspect and return existing table names in the current database schema without risking transaction errors."""
    bind = session.get_bind()
    dialect_name = getattr(bind.dialect, "name", "")
    if dialect_name == "postgresql":
        res = await session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        return {row[0] for row in res.all()}
    elif dialect_name == "sqlite":
        res = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        return {row[0] for row in res.all()}
    else:
        res = await session.execute(
            text("SELECT table_name FROM information_schema.tables")
        )
        return {row[0] for row in res.all()}


async def _safe_count(
    session: AsyncSession,
    table_name: str,
    where_clause: str,
    params: dict,
    existing_tables: Optional[Set[str]] = None,
) -> int:
    """Query row count if table exists, returning 0 if table does not exist."""
    if existing_tables is not None and table_name not in existing_tables:
        return 0
    sql = f"SELECT COUNT(*) FROM {table_name} WHERE {where_clause}"
    res = await session.execute(text(sql), params)
    return int(res.scalar() or 0)


async def _safe_delete(
    session: AsyncSession,
    table_name: str,
    where_clause: str,
    params: dict,
    existing_tables: Optional[Set[str]] = None,
) -> int:
    """Delete rows if table exists, returning count of affected rows. Never query if table does not exist."""
    if existing_tables is not None and table_name not in existing_tables:
        logger.debug("[SKIP] Table '%s' does not exist in database schema.", table_name)
        return 0
    count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE {where_clause}"
    cnt_res = await session.execute(text(count_sql), params)
    cnt = int(cnt_res.scalar() or 0)
    if cnt > 0:
        del_sql = f"DELETE FROM {table_name} WHERE {where_clause}"
        await session.execute(text(del_sql), params)
        logger.info("  [DELETED] %d row(s) from '%s'", cnt, table_name)
    else:
        logger.debug("  [NOOP] 0 rows in '%s'", table_name)
    return cnt


async def audit_company_resources(session: AsyncSession, company_id: int) -> Dict[str, Any]:
    """Inspect and return inventory of all resources tied to company_id."""
    c_res = await session.execute(select(Company).where(Company.company_id == company_id))
    company = c_res.scalars().first()
    if not company:
        raise CompanyNotFoundError(f"Company with id={company_id} was not found in the database.")

    # 1. Services
    s_res = await session.execute(select(Service).where(Service.company_id == company_id))
    services = list(s_res.scalars().all())
    service_ids = [s.service_id for s in services]

    # 2. Users
    u_res = await session.execute(select(User).where(User.company_id == company_id))
    users = list(u_res.scalars().all())
    user_ids = [u.user_id for u in users]
    hubspot_owner_ids = [u.hubspot_owner_id for u in users if u.hubspot_owner_id]

    # 3. Teams
    t_res = await session.execute(select(Team).where(Team.company_id == company_id))
    teams = list(t_res.scalars().all())
    team_ids = [t.team_id for t in teams]

    # 4. Typologies
    typ_res = await session.execute(select(Typology).where(Typology.company_id == company_id))
    typologies = list(typ_res.scalars().all())
    typology_ids = [t.typology_id for t in typologies]

    # 5. Analyses
    an_res = await session.execute(select(func.count(Analysis.analysis_id)).where(Analysis.company_id == company_id))
    analyses_count = an_res.scalar() or 0

    # 6. Mass Evaluation Jobs & Results
    job_res = await session.execute(select(MassEvaluationJob.job_id).where(MassEvaluationJob.company_id == company_id))
    job_ids = list(job_res.scalars().all())

    mass_res = await session.execute(
        select(func.count(MassEvaluationResult.mass_analysis_id)).where(MassEvaluationResult.company_id == company_id)
    )
    mass_results_count = mass_res.scalar() or 0

    # 7. Training Runs
    tr_res = await session.execute(select(func.count(TrainingRun.training_run_id)).where(TrainingRun.company_id == company_id))
    training_runs_count = tr_res.scalar() or 0

    # 8. Trainer
    tc_res = await session.execute(
        select(func.count(TrainerEvaluationConfig.config_id)).where(TrainerEvaluationConfig.company_id == company_id)
    )
    trainer_configs_count = tc_res.scalar() or 0

    tsim_res = await session.execute(
        select(func.count(TrainerSimulation.simulation_id)).where(TrainerSimulation.company_id == company_id)
    )
    trainer_sims_count = tsim_res.scalar() or 0

    tsess_res = await session.execute(
        select(func.count(TrainerSession.session_id)).where(TrainerSession.company_id == company_id)
    )
    trainer_sessions_count = tsess_res.scalar() or 0

    # 9. Prompts
    pr_res = await session.execute(select(func.count(Prompt.prompt_id)).where(Prompt.company_id == company_id))
    prompts_count = pr_res.scalar() or 0

    # 10. Training agent settings (Trainer credentials)
    tas_res = await session.execute(
        select(func.count(TrainingAgentSetting.setting_id)).where(TrainingAgentSetting.company_id == company_id)
    )
    training_agent_settings_count = tas_res.scalar() or 0

    return {
        "company": {
            "company_id": company.company_id,
            "company_name": company.company_name,
            "company_key": company.company_key,
            "is_demo": bool(getattr(company, "is_demo", False)),
            "is_active": bool(company.is_active),
            "created_at": str(company.created_at) if company.created_at else None,
        },
        "counts": {
            "services": len(services),
            "users": len(users),
            "teams": len(teams),
            "typologies": len(typologies),
            "analyses": analyses_count,
            "mass_jobs": len(job_ids),
            "mass_results": mass_results_count,
            "training_runs": training_runs_count,
            "training_agent_settings": training_agent_settings_count,
            "trainer_configs": trainer_configs_count,
            "trainer_simulations": trainer_sims_count,
            "trainer_sessions": trainer_sessions_count,
            "prompts": prompts_count,
        },
        "ids": {
            "service_ids": service_ids,
            "user_ids": user_ids,
            "team_ids": team_ids,
            "typology_ids": typology_ids,
            "job_ids": job_ids,
            "hubspot_owner_ids": hubspot_owner_ids,
        }
    }


async def purge_company(
    session: AsyncSession,
    company_id: int,
    apply: bool = False,
    force_non_demo: bool = False,
    allowed_ids: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """
    Safely purge a company and all its cascaded resources in a single transaction.

    Enforces safety guards:
    - company_id == 1 is strictly blocked.
    - is_demo == False is strictly blocked unless force_non_demo=True.
    - company_id not in allowed_ids is blocked unless force_non_demo=True.
    """
    if allowed_ids is None:
        allowed_ids = ALLOWED_DEFAULT_IDS

    # Guard 1: Prohibit company_id=1
    if company_id == 1:
        raise CompanyProtectedError(
            "CRITICAL GUARD VIOLATION: Deletion of company_id=1 (Boston Medical) is strictly prohibited."
        )

    # Fetch audit
    audit = await audit_company_resources(session, company_id)
    comp_meta = audit["company"]
    is_demo = comp_meta["is_demo"]

    # Guard 2: Target company restriction
    if company_id not in allowed_ids and not force_non_demo:
        raise CompanyProtectedError(
            f"Company ID {company_id} ('{comp_meta['company_name']}') is not in the allowed target list "
            f"{sorted(list(allowed_ids))}. If you intended to delete this company, pass --force-non-demo."
        )

    # Guard 3: is_demo check
    if not is_demo and not force_non_demo:
        raise CompanyProtectedError(
            f"Company '{comp_meta['company_name']}' (id={company_id}) has is_demo=False. "
            "Deleting non-demo companies is prohibited by default. Pass --force-non-demo to override."
        )

    if not apply:
        logger.info("[DRY-RUN] Safety checks passed for company_id=%s. No changes will be applied.", company_id)
        return {
            "status": "dry_run",
            "applied": False,
            "company": comp_meta,
            "counts": audit["counts"],
            "deleted_records": {},
        }

    logger.info("================================================================================")
    logger.info("[APPLY] Starting atomic purge for company_id=%s ('%s')...", company_id, comp_meta["company_name"])
    logger.info("================================================================================")

    # Inspect schema once to avoid any invalid SQL query aborting transaction
    existing_tables = await _get_existing_tables(session)
    logger.info("[SCHEMA] Discovered %d tables in current database schema.", len(existing_tables))

    ids = audit["ids"]
    service_ids = ids["service_ids"]
    user_ids = ids["user_ids"]
    team_ids = ids["team_ids"]
    job_ids = ids["job_ids"]
    typology_ids = ids["typology_ids"]
    owner_ids = ids["hubspot_owner_ids"]

    deleted_counts: Dict[str, int] = {}

    def _fmt_ids(id_list: List[Any]) -> str:
        if not id_list:
            return "(-1)"
        return f"({','.join(str(int(x) if isinstance(x, (int, float)) else repr(str(x))) for x in id_list)})"

    async def _del(table_name: str, where_clause: str, params: Optional[dict] = None) -> int:
        return await _safe_delete(session, table_name, where_clause, params or {}, existing_tables=existing_tables)

    # -------------------------------------------------------------------------
    # Group 1: Personalized Training
    # -------------------------------------------------------------------------
    logger.info("--- [PHASE 1/6] Purging Personalized Training resources ---")
    tr_reports = await session.execute(
        select(TrainingAgentReport.training_report_id).where(
            (TrainingAgentReport.company_id == company_id) |
            (TrainingAgentReport.service_id.in_(service_ids) if service_ids else text("1=0"))
        )
    )
    report_ids = list(tr_reports.scalars().all())

    if report_ids:
        deleted_counts["bm_training_call_evaluations"] = await _del(
            "bm_training_call_evaluations", f"cycle_id IN {_fmt_ids(report_ids)}"
        )
        deleted_counts["bm_training_call_sessions"] = await _del(
            "bm_training_call_sessions", f"cycle_id IN {_fmt_ids(report_ids)}"
        )
        deleted_counts["bm_training_completion_status"] = await _del(
            "bm_training_completion_status", f"training_report_id IN {_fmt_ids(report_ids)}"
        )
        deleted_counts["bm_training_simulation_prompts"] = await _del(
            "bm_training_simulation_prompts", f"training_report_id IN {_fmt_ids(report_ids)}"
        )

    deleted_counts["bm_training_agent_reports"] = await _del(
        "bm_training_agent_reports",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )
    deleted_counts["bm_training_runs"] = await _del(
        "bm_training_runs",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )
    deleted_counts["bm_training_evaluation_prompts"] = await _del(
        "bm_training_evaluation_prompts",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )
    deleted_counts["bm_training_agent_settings"] = await _del(
        "bm_training_agent_settings",
        f"company_id = {company_id}" + (f" OR hubspot_owner_id IN {_fmt_ids(owner_ids)}" if owner_ids else "")
    )

    # -------------------------------------------------------------------------
    # Group 2: Trainer
    # -------------------------------------------------------------------------
    logger.info("--- [PHASE 2/6] Purging Trainer Module resources ---")
    # 1. Evaluations (FK to sessions)
    tr_session_ids_res = await session.execute(
        select(TrainerSession.session_id).where(
            (TrainerSession.company_id == company_id) |
            (TrainerSession.service_id.in_(service_ids) if service_ids else text("1=0"))
        )
    )
    tr_session_ids = list(tr_session_ids_res.scalars().all())

    if tr_session_ids:
        deleted_counts["bm_trainer_evaluations"] = await _del(
            "bm_trainer_evaluations", f"session_id IN {_fmt_ids(tr_session_ids)}"
        )
    else:
        deleted_counts["bm_trainer_evaluations"] = 0

    # 2. Sessions (FK to simulations)
    deleted_counts["bm_trainer_sessions"] = await _del(
        "bm_trainer_sessions",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    # 3. Simulation versions (FK to simulations)
    tr_sim_ids_res = await session.execute(
        select(TrainerSimulation.simulation_id).where(
            (TrainerSimulation.company_id == company_id) |
            (TrainerSimulation.service_id.in_(service_ids) if service_ids else text("1=0"))
        )
    )
    tr_sim_ids = list(tr_sim_ids_res.scalars().all())

    if tr_sim_ids:
        deleted_counts["bm_trainer_simulation_versions"] = await _del(
            "bm_trainer_simulation_versions", f"simulation_id IN {_fmt_ids(tr_sim_ids)}"
        )
    else:
        deleted_counts["bm_trainer_simulation_versions"] = 0

    # 4. Simulations (FK to evaluation configs & services)
    deleted_counts["bm_trainer_simulations"] = await _del(
        "bm_trainer_simulations",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    # 5. Evaluation configs (FK to prompts & services)
    deleted_counts["bm_trainer_evaluation_configs"] = await _del(
        "bm_trainer_evaluation_configs",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    # -------------------------------------------------------------------------
    # Group 3: Mass Evaluations & Automations
    # -------------------------------------------------------------------------
    logger.info("--- [PHASE 3/6] Purging Mass Evaluations & Automations ---")
    mass_ids_res = await session.execute(
        select(MassEvaluationResult.mass_analysis_id).where(
            (MassEvaluationResult.company_id == company_id) |
            (MassEvaluationResult.service_id.in_(service_ids) if service_ids else text("1=0")) |
            (MassEvaluationResult.job_id.in_(job_ids) if job_ids else text("1=0"))
        )
    )
    mass_analysis_ids = list(mass_ids_res.scalars().all())

    if mass_analysis_ids:
        deleted_counts["bm_mass_evaluation_criterion_results"] = await _del(
            "bm_mass_evaluation_criterion_results",
            f"mass_analysis_id IN {_fmt_ids(mass_analysis_ids)}"
        )
    else:
        deleted_counts["bm_mass_evaluation_criterion_results"] = 0

    deleted_counts["bm_mass_evaluation_results"] = await _del(
        "bm_mass_evaluation_results",
        f"company_id = {company_id}" +
        (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "") +
        (f" OR job_id IN {_fmt_ids(job_ids)}" if job_ids else "")
    )

    if service_ids:
        auto_ids_res = await session.execute(
            select(MassAnalysisAutomation.automation_id).where(MassAnalysisAutomation.service_id.in_(service_ids))
        )
        auto_ids = list(auto_ids_res.scalars().all())
        if auto_ids:
            deleted_counts["bm_mass_analysis_automation_runs"] = await _del(
                "bm_mass_analysis_automation_runs", f"automation_id IN {_fmt_ids(auto_ids)}"
            )
            deleted_counts["bm_mass_analysis_automations"] = await _del(
                "bm_mass_analysis_automations", f"automation_id IN {_fmt_ids(auto_ids)}"
            )

    deleted_counts["bm_mass_evaluation_runs"] = await _del(
        "bm_mass_evaluation_runs",
        f"company_id = {company_id}" + (f" OR job_id IN {_fmt_ids(job_ids)}" if job_ids else "")
    )
    deleted_counts["bm_mass_evaluation_jobs"] = await _del(
        "bm_mass_evaluation_jobs",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    # -------------------------------------------------------------------------
    # Group 4: Individual Analyses
    # -------------------------------------------------------------------------
    logger.info("--- [PHASE 4/6] Purging Individual Analyses ---")
    an_ids_res = await session.execute(
        select(Analysis.analysis_id).where(
            (Analysis.company_id == company_id) |
            (Analysis.service_id.in_(service_ids) if service_ids else text("1=0"))
        )
    )
    an_ids = list(an_ids_res.scalars().all())

    if an_ids:
        deleted_counts["bm_analysis_criterion_results"] = await _del(
            "bm_analysis_criterion_results", f"analysis_id IN {_fmt_ids(an_ids)}"
        )
        deleted_counts["bm_analysis_results"] = await _del(
            "bm_analysis_results", f"analysis_id IN {_fmt_ids(an_ids)}"
        )

    deleted_counts["bm_call_analysis_current"] = await _del(
        "bm_call_analysis_current",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )
    deleted_counts["bm_analyses"] = await _del(
        "bm_analyses",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    # -------------------------------------------------------------------------
    # Group 5: Prompts, Structures & Criteria
    # -------------------------------------------------------------------------
    logger.info("--- [PHASE 5/6] Purging Prompts, Base Structures & Criteria ---")
    prompt_ids_res = await session.execute(
        select(Prompt.prompt_id).where(
            (Prompt.company_id == company_id) |
            (Prompt.service_id.in_(service_ids) if service_ids else text("1=0"))
        )
    )
    prompt_ids = list(prompt_ids_res.scalars().all())

    if prompt_ids:
        crit_ids_res = await session.execute(
            select(PromptCriterion.criterion_id).where(PromptCriterion.prompt_id.in_(prompt_ids))
        )
        crit_ids = list(crit_ids_res.scalars().all())
        if crit_ids:
            deleted_counts["bm_criteria_sync_logs"] = await _del(
                "bm_criteria_sync_logs",
                f"criterion_id IN {_fmt_ids(crit_ids)} OR prompt_id IN {_fmt_ids(prompt_ids)}"
            )
            deleted_counts["bm_prompt_criterion_typologies"] = await _del(
                "bm_prompt_criterion_typologies", f"criterion_id IN {_fmt_ids(crit_ids)}"
            )
            deleted_counts["bm_prompt_criteria"] = await _del(
                "bm_prompt_criteria", f"criterion_id IN {_fmt_ids(crit_ids)}"
            )
        deleted_counts["bm_prompt_drafts"] = await _del(
            "bm_prompt_drafts", f"prompt_id IN {_fmt_ids(prompt_ids)}"
        )
        deleted_counts["bm_prompt_versions"] = await _del(
            "bm_prompt_versions", f"prompt_id IN {_fmt_ids(prompt_ids)}"
        )
        deleted_counts["bm_prompts"] = await _del(
            "bm_prompts", f"prompt_id IN {_fmt_ids(prompt_ids)}"
        )

    # Base structures and subordinate relationships
    base_struct_res = await session.execute(
        select(PromptBaseStructure.id).where(
            (PromptBaseStructure.company_id == company_id) |
            (PromptBaseStructure.service_id.in_(service_ids) if service_ids else text("1=0"))
        )
    )
    base_struct_ids = list(base_struct_res.scalars().all())
    if base_struct_ids:
        deleted_counts["bm_base_structure_typologies"] = await _del(
            "bm_base_structure_typologies", f"base_structure_id IN {_fmt_ids(base_struct_ids)}"
        )

    # Structure permissions
    if user_ids or base_struct_ids or prompt_ids:
        perm_parts = []
        audit_parts = []
        if user_ids:
            perm_parts.append(f"user_id IN {_fmt_ids(user_ids)}")
            audit_parts.append(f"(actor_user_id IN {_fmt_ids(user_ids)} OR affected_user_id IN {_fmt_ids(user_ids)})")
        if base_struct_ids:
            struct_filter = f"(structure_type = 'base' AND structure_id IN {_fmt_ids(base_struct_ids)})"
            perm_parts.append(struct_filter)
            audit_parts.append(struct_filter)
        if prompt_ids:
            prompt_filter = f"(structure_type = 'specific' AND structure_id IN {_fmt_ids(prompt_ids)})"
            perm_parts.append(prompt_filter)
            audit_parts.append(prompt_filter)
        deleted_counts["bm_structure_permissions_audit"] = await _del("bm_structure_permissions_audit", " OR ".join(audit_parts))
        deleted_counts["bm_structure_permissions"] = await _del("bm_structure_permissions", " OR ".join(perm_parts))

    deleted_counts["bm_prompt_base_structures"] = await _del(
        "bm_prompt_base_structures",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    # -------------------------------------------------------------------------
    # Group 6: Teams, Users, Services & Company
    # -------------------------------------------------------------------------
    logger.info("--- [PHASE 6/6] Purging Teams, Users, Services & Company ---")
    deleted_counts["bm_typologies"] = await _del(
        "bm_typologies",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    if team_ids or user_ids:
        team_parts = []
        if team_ids:
            team_parts.append(f"team_id IN {_fmt_ids(team_ids)}")
        if user_ids:
            team_parts.append(f"user_id IN {_fmt_ids(user_ids)}")
        team_clause = " OR ".join(team_parts)
        deleted_counts["bm_agent_teams"] = await _del("bm_agent_teams", team_clause)
        deleted_counts["bm_user_teams"] = await _del("bm_user_teams", team_clause)

    deleted_counts["bm_teams"] = await _del(
        "bm_teams",
        f"company_id = {company_id}" + (f" OR service_id IN {_fmt_ids(service_ids)}" if service_ids else "")
    )

    if service_ids or user_ids:
        svc_parts = []
        if service_ids:
            svc_parts.append(f"service_id IN {_fmt_ids(service_ids)}")
        if user_ids:
            svc_parts.append(f"user_id IN {_fmt_ids(user_ids)}")
        deleted_counts["bm_user_services"] = await _del("bm_user_services", " OR ".join(svc_parts))

    if user_ids:
        deleted_counts["bm_user_audits"] = await _del(
            "bm_user_audits",
            f"admin_user_id IN {_fmt_ids(user_ids)} OR target_user_id IN {_fmt_ids(user_ids)}"
        )
        deleted_counts["bm_password_reset_tokens"] = await _del(
            "bm_password_reset_tokens",
            f"user_id IN {_fmt_ids(user_ids)} OR created_by_admin_id IN {_fmt_ids(user_ids)}"
        )

    # Break foreign keys from bm_users before deleting users
    if "bm_users" in existing_tables:
        await session.execute(
            text(f"UPDATE bm_users SET primary_service_id = NULL, primary_team_id = NULL WHERE company_id = {company_id}")
        )

    deleted_counts["bm_users"] = await _del("bm_users", f"company_id = {company_id}")
    deleted_counts["bm_services"] = await _del("bm_services", f"company_id = {company_id}")

    # Pre-delete final validation inside the transaction
    rem_svc = await _safe_count(session, "bm_services", "company_id = :id", {"id": company_id}, existing_tables=existing_tables)
    rem_usr = await _safe_count(session, "bm_users", "company_id = :id", {"id": company_id}, existing_tables=existing_tables)
    rem_tm = await _safe_count(session, "bm_teams", "company_id = :id", {"id": company_id}, existing_tables=existing_tables)
    if rem_svc > 0 or rem_usr > 0 or rem_tm > 0:
        raise CompanyPurgeError(
            f"Pre-delete validation failed: Residual rows detected (services={rem_svc}, users={rem_usr}, teams={rem_tm})"
        )

    # Delete the company
    del_comp_res = await session.execute(delete(Company).where(Company.company_id == company_id))
    deleted_counts["bm_companies"] = del_comp_res.rowcount or 1

    logger.info("[APPLY] Company id=%s successfully deleted.", company_id)

    return {
        "status": "applied",
        "applied": True,
        "company": comp_meta,
        "counts": audit["counts"],
        "deleted_records": deleted_counts,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Safely and transactionally purge test/demo companies and all subordinate resources."
    )
    parser.add_argument("--company-id", type=int, required=True, help="Target company ID to purge")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry-run mode (default, rollbacks all changes)")
    parser.add_argument("--apply", action="store_true", default=False, help="Execute and commit deletion permanently")
    parser.add_argument("--force-non-demo", action="store_true", default=False, help="Allow deleting non-demo company or outside default ids")
    parser.add_argument("--db-url", type=str, default=None, help="Optional custom database URL")
    return parser.parse_args()


async def async_main():
    args = parse_args()
    apply_mode = bool(args.apply)
    force_non_demo = bool(args.force_non_demo)

    settings = get_settings()
    db_url = args.db_url or os.environ.get("DATABASE_URL") or settings.database_url
    if not db_url:
        print("ERROR: DATABASE_URL is not set in environment or config.")
        sys.exit(1)

    engine = create_async_engine(db_url, echo=False)

    print("=" * 80)
    print(f"PURGE COMPANY TOOL - Mode: {'APPLY (PERMANENT DELETION)' if apply_mode else 'DRY RUN (NO CHANGES)'}")
    print(f"Target Company ID: {args.company_id}")
    print("=" * 80)

    try:
        async with AsyncSession(engine) as session:
            async with session.begin():
                result = await purge_company(
                    session=session,
                    company_id=args.company_id,
                    apply=apply_mode,
                    force_non_demo=force_non_demo,
                )
                if not apply_mode:
                    await session.rollback()

        comp = result["company"]
        print(f"Company: ({comp['company_id']}) {comp['company_name']} [key: '{comp['company_key']}']")
        print(f"Demo Flag: is_demo={comp['is_demo']} | Active: {comp['is_active']}")
        print("-" * 80)
        print("RESOURCE INVENTORY:")
        for k, v in result["counts"].items():
            print(f"  - {k:25}: {v}")

        if apply_mode:
            print("-" * 80)
            print("DELETED RECORDS:")
            for k, v in result["deleted_records"].items():
                if v > 0:
                    print(f"  - {k:35}: {v}")
            print("=" * 80)
            print("SUCCESS: Company and all associated resources purged successfully.")
        else:
            print("=" * 80)
            print("DRY RUN COMPLETE: No data was modified. To execute, run with --apply.")

    except CompanyProtectedError as e:
        print(f"\nSAFETY GUARD BLOCKED EXECUTION:\n{e}")
        sys.exit(2)
    except CompanyNotFoundError as e:
        print(f"\nERROR: {e}")
        sys.exit(3)
    except Exception as e:
        logger.exception("Purge failed with unexpected error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(async_main())
