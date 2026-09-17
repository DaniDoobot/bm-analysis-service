"""
scripts/fix_demo_agent_names.py
================================
Data correction script/migration for demo company:
- Dynamically resolves Empresa Demo (by company_key='empresa-demo' or (is_demo=True and company_name='Empresa Demo')).
- Aborts safely if not exactly 1 demo company is found.
- Strictly targets demo agents (hubspot_owner_id in demo_owner_01..demo_owner_60).
- Uses hubspot_owner_id as the stable key.
- Updates:
    bm_users:
      name -> 'Agente Demo XX'
      username -> 'agente.demo.XX'
      email -> 'agente.demo.XX@doobot.ai'
      agent_initials -> calculated agent_code ('AC-F01'..'VT-R20')
    bm_training_agent_settings:
      agent_name -> 'Agente Demo XX'
      agent_initials -> calculated agent_code
    bm_mass_evaluation_results:
      agent_name -> 'Agente Demo XX'
    bm_training_agent_reports:
      agent_name -> 'Agente Demo XX'
      agent_initials -> calculated agent_code
- NEVER touches Boston Medical (company_id=1) or any real company.
- Supports --dry-run for detailed preview without committing.
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, update, or_, and_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.config import get_settings
from app.models.companies import Company
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.agent_resolvers import calculate_demo_agent_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("fix_demo_agent_names")


async def resolve_demo_company(db: AsyncSession) -> Company:
    """
    Dynamically and safely locate the unique Demo Company.
    Matches company_key == 'empresa-demo' OR (is_demo == True AND company_name == 'Empresa Demo').
    Raises RuntimeError if not found or if ambiguous.
    """
    stmt = select(Company).where(
        or_(
            Company.company_key == "empresa-demo",
            and_(Company.is_demo.is_(True), Company.company_name == "Empresa Demo"),
        )
    )
    res = await db.execute(stmt)
    companies = list(res.scalars().all())

    if not companies:
        err_msg = "SAFETY ABORT: Demo company not found! Neither company_key='empresa-demo' nor is_demo=True was found."
        logger.critical(err_msg)
        raise RuntimeError(err_msg)

    # De-duplicate by company_id in case a company matched both conditions
    unique_companies = {c.company_id: c for c in companies}
    if len(unique_companies) > 1:
        c_details = [f"id={c.company_id}, name='{c.company_name}', key='{c.company_key}'" for c in unique_companies.values()]
        err_msg = f"SAFETY ABORT: Ambiguous demo company match! Found {len(unique_companies)} companies: {'; '.join(c_details)}"
        logger.critical(err_msg)
        raise RuntimeError(err_msg)

    demo_company = list(unique_companies.values())[0]

    # Extreme safety check: company must NOT be a known real company
    if demo_company.company_id == 1 or "boston" in demo_company.company_name.lower():
        err_msg = f"SAFETY ABORT: Resolved company appears to be Boston Medical (id={demo_company.company_id})! Refusing to proceed."
        logger.critical(err_msg)
        raise RuntimeError(err_msg)

    return demo_company


async def fix_demo_agent_names(
    db: AsyncSession,
    dry_run: bool = False,
    override_company_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Perform idempotent fix of demo agent names, usernames, emails and initials/codes.
    Dynamically finds Empresa Demo (or uses override_company_id if explicitly supplied).
    Includes strict safety assertions and detailed dry-run preview.
    """
    if override_company_id is not None:
        stmt_comp = select(Company).where(Company.company_id == override_company_id)
        res_comp = await db.execute(stmt_comp)
        demo_company = res_comp.scalar_one_or_none()
        if not demo_company:
            err_msg = f"SAFETY ABORT: Company with id={override_company_id} not found."
            logger.critical(err_msg)
            raise RuntimeError(err_msg)
    else:
        demo_company = await resolve_demo_company(db)

    demo_company_id = demo_company.company_id
    logger.info(
        "Targeting demo company: id=%d, name='%s', key='%s', is_demo=%s (dry_run=%s)",
        demo_company_id,
        demo_company.company_name,
        demo_company.company_key,
        demo_company.is_demo,
        dry_run,
    )

    # 1. Fetch current demo users for inspection and safety verification
    stmt_users = (
        select(User)
        .where(User.company_id == demo_company_id, User.hubspot_owner_id.like("demo_owner_%"))
        .order_by(User.hubspot_owner_id)
    )
    res_users = await db.execute(stmt_users)
    existing_users: List[User] = list(res_users.scalars().all())

    # Safety check: Verify that every fetched user strictly belongs to demo_company_id and has hubspot_owner_id LIKE 'demo_owner_%'
    for u in existing_users:
        if u.company_id != demo_company_id or not (u.hubspot_owner_id and u.hubspot_owner_id.startswith("demo_owner_")):
            err_msg = (
                f"SAFETY ABORT: Found unexpected record in demo set! "
                f"user_id={u.user_id}, company_id={u.company_id}, hubspot_owner_id={u.hubspot_owner_id}. "
                f"Aborting without modifying any data."
            )
            logger.critical(err_msg)
            raise RuntimeError(err_msg)

    print("\n" + "=" * 95)
    print(f"DEMO AGENTS CORRECTION (company_id={demo_company_id}, name='{demo_company.company_name}') - DRY RUN: {dry_run}")
    print(f"Total demo agent records found in database: {len(existing_users)}")
    print("=" * 95)
    print(f"{'HS Owner ID':<15} | {'Nombre Actual':<25} -> {'Nombre Nuevo':<20} | {'Cód. Actual':<12} -> {'Cód. Nuevo':<10}")
    print("-" * 95)

    user_by_oid: Dict[str, User] = {u.hubspot_owner_id: u for u in existing_users if u.hubspot_owner_id}

    for i in range(1, 61):
        idx_str = f"{i:02d}"
        oid = f"demo_owner_{idx_str}"
        target_name = f"Agente Demo {idx_str}"
        target_code = calculate_demo_agent_code(i)

        current_u = user_by_oid.get(oid) or user_by_oid.get(f"demo_owner_{i}")
        curr_name = (current_u.name if current_u and current_u.name else "(no existe)")
        curr_code = (current_u.agent_initials if current_u and current_u.agent_initials else "-")

        print(f"{oid:<15} | {curr_name:<25} -> {target_name:<20} | {curr_code:<12} -> {target_code:<10}")

    print("=" * 95 + "\n")

    updated_users = 0
    updated_settings = 0
    updated_evals = 0
    updated_reports = 0

    for i in range(1, 61):
        idx_str = f"{i:02d}"
        oid = f"demo_owner_{idx_str}"
        target_name = f"Agente Demo {idx_str}"
        target_username = f"agente.demo.{idx_str}"
        target_email = f"agente.demo.{idx_str}@doobot.ai"
        target_code = calculate_demo_agent_code(i)

        oids = [f"demo_owner_{idx_str}", f"demo_owner_{i}"]

        # 1. Update User
        stmt_u = (
            update(User)
            .where(User.company_id == demo_company_id, User.hubspot_owner_id.in_(oids))
            .values(
                name=target_name,
                username=target_username,
                email=target_email,
                agent_initials=target_code,
                hubspot_owner_id=oid,
            )
        )
        res_u = await db.execute(stmt_u)
        updated_users += res_u.rowcount

        # 2. Update TrainingAgentSetting
        stmt_s = (
            update(TrainingAgentSetting)
            .where(TrainingAgentSetting.company_id == demo_company_id, TrainingAgentSetting.hubspot_owner_id.in_(oids))
            .values(
                agent_name=target_name,
                agent_initials=target_code,
                hubspot_owner_id=oid,
            )
        )
        res_s = await db.execute(stmt_s)
        updated_settings += res_s.rowcount

        # 3. Update MassEvaluationResult
        stmt_e = (
            update(MassEvaluationResult)
            .where(MassEvaluationResult.company_id == demo_company_id, MassEvaluationResult.hubspot_owner_id.in_(oids))
            .values(
                agent_name=target_name,
                hubspot_owner_id=oid,
            )
        )
        res_e = await db.execute(stmt_e)
        updated_evals += res_e.rowcount

        # 4. Update TrainingAgentReport
        stmt_r = (
            update(TrainingAgentReport)
            .where(TrainingAgentReport.company_id == demo_company_id, TrainingAgentReport.hubspot_owner_id.in_(oids))
            .values(
                agent_name=target_name,
                agent_initials=target_code,
                hubspot_owner_id=oid,
            )
        )
        res_r = await db.execute(stmt_r)
        updated_reports += res_r.rowcount

    logger.info(
        "Demo agent names fix summary for company_id=%d: %d users, %d training settings, %d mass evaluations, %d training reports matched/updated.",
        demo_company_id,
        updated_users,
        updated_settings,
        updated_evals,
        updated_reports,
    )

    if not dry_run:
        await db.commit()
        logger.info("Changes successfully committed to database.")
    else:
        await db.rollback()
        logger.info("[DRY RUN] Rolled back all changes without committing.")

    return {
        "company_id": demo_company_id,
        "company_name": demo_company.company_name,
        "found_demo_users": len(existing_users),
        "updated_users": updated_users,
        "updated_settings": updated_settings,
        "updated_evals": updated_evals,
        "updated_reports": updated_reports,
        "dry_run": dry_run,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Fix demo agent names for Empresa Demo")
    parser.add_argument("--db-url", type=str, default=None, help="Custom database URL")
    parser.add_argument("--company-id", type=int, default=None, help="Explicit company ID override")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Dry run without committing")
    return parser.parse_args()


async def async_main():
    args = parse_args()
    settings = get_settings()
    db_url = args.db_url or os.environ.get("DATABASE_URL") or settings.database_url
    if not db_url:
        logger.error("DATABASE_URL is not set.")
        sys.exit(1)

    from app.db import _make_async_url
    async_db_url = _make_async_url(db_url)
    engine = create_async_engine(async_db_url, echo=False)
    async with AsyncSession(engine) as session:
        await fix_demo_agent_names(
            session,
            dry_run=args.dry_run,
            override_company_id=args.company_id,
        )


if __name__ == "__main__":
    asyncio.run(async_main())
