"""
scripts/fix_demo_agent_names.py
================================
Data correction script/migration for demo company (company_id=6):
- Strictly targets company_id=6.
- Strictly targets demo agents (hubspot_owner_id in demo_owner_01..demo_owner_60).
- Uses hubspot_owner_id as the stable key.
- Updates:
    bm_users:
      name -> 'Agente Demo XX'
      username -> 'agente.demo.XX'
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
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import Dict, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.config import get_settings
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.agent_resolvers import calculate_demo_agent_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DEMO_COMPANY_ID = 6


async def fix_demo_agent_names(db: AsyncSession, dry_run: bool = False) -> Dict[str, Any]:
    """
    Perform idempotent fix of demo agent names and initials/codes for company_id=6.
    """
    logger.info("Starting demo agent names fix for company_id=%d (dry_run=%s)", DEMO_COMPANY_ID, dry_run)

    updated_users = 0
    updated_settings = 0
    updated_evals = 0
    updated_reports = 0

    for i in range(1, 61):
        idx_str = f"{i:02d}"
        oid = f"demo_owner_{idx_str}"
        target_name = f"Agente Demo {idx_str}"
        target_username = f"agente.demo.{idx_str}"
        target_code = calculate_demo_agent_code(i)

        oids = [f"demo_owner_{idx_str}", f"demo_owner_{i}"]

        # 1. Update User
        stmt_u = (
            update(User)
            .where(User.company_id == DEMO_COMPANY_ID, User.hubspot_owner_id.in_(oids))
            .values(
                name=target_name,
                username=target_username,
                agent_initials=target_code,
                hubspot_owner_id=oid,
            )
        )
        res_u = await db.execute(stmt_u)
        updated_users += res_u.rowcount

        # 2. Update TrainingAgentSetting
        stmt_s = (
            update(TrainingAgentSetting)
            .where(TrainingAgentSetting.company_id == DEMO_COMPANY_ID, TrainingAgentSetting.hubspot_owner_id.in_(oids))
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
            .where(MassEvaluationResult.company_id == DEMO_COMPANY_ID, MassEvaluationResult.hubspot_owner_id.in_(oids))
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
            .where(TrainingAgentReport.company_id == DEMO_COMPANY_ID, TrainingAgentReport.hubspot_owner_id.in_(oids))
            .values(
                agent_name=target_name,
                agent_initials=target_code,
                hubspot_owner_id=oid,
            )
        )
        res_r = await db.execute(stmt_r)
        updated_reports += res_r.rowcount

    logger.info(
        "Demo agent names fix summary: %d users, %d training settings, %d mass evaluations, %d training reports updated.",
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
        logger.info("[DRY RUN] Rolled back all changes.")

    return {
        "updated_users": updated_users,
        "updated_settings": updated_settings,
        "updated_evals": updated_evals,
        "updated_reports": updated_reports,
        "dry_run": dry_run,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Fix demo agent names for company_id=6")
    parser.add_argument("--db-url", type=str, default=None, help="Custom database URL")
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
        await fix_demo_agent_names(session, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(async_main())
