"""
scripts/migrate_demo_simulation_codes.py
========================================
Safe, idempotent migration script to modernize simulation codes for Empresa Demo.

- Targets EXCLUSIVELY Empresa Demo (company_key='empresa-demo' or (is_demo=True and company_name='Empresa Demo')).
- Safety aborts if company_id == 1 or if 'boston' appears in the company name.
- Converts legacy simulation codes (e.g. SIM-DEMO-AT01) to 6-8 char alphanumeric codes:
    SIM-DEMO-AT01 -> ATEN01
    SIM-DEMO-AT02 -> ATEN02
    SIM-DEMO-VN01 -> VENT01
    SIM-DEMO-VN02 -> VENT02
- Dynamically handles any additional demo simulations to adhere to ^[A-Z0-9]{6,8}$.
- Preserves simulation_id and all relationships (versions, sessions, evaluations, configs).
- Default mode is DRY-RUN. Use --apply to commit changes.
"""
import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, update, or_, and_, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.config import get_settings
from app.models.companies import Company
from app.models.services import Service
from app.models.trainer import (
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("migrate_demo_simulation_codes")

# Known explicit mapping for Empresa Demo standard simulations
KNOWN_CODE_MAPPINGS: Dict[str, str] = {
    "SIM-DEMO-AT01": "ATEN01",
    "SIM-DEMO-AT02": "ATEN02",
    "SIM-DEMO-VN01": "VENT01",
    "SIM-DEMO-VN02": "VENT02",
}


async def resolve_demo_company(db: AsyncSession, override_company_id: Optional[int] = None) -> Company:
    """Safely locate Empresa Demo, preventing any execution on real companies."""
    if override_company_id is not None:
        stmt = select(Company).where(Company.company_id == override_company_id)
        res = await db.execute(stmt)
        comp = res.scalars().first()
        if not comp:
            raise RuntimeError(f"Company ID {override_company_id} not found.")
        if comp.company_id == 1 or "boston" in comp.company_name.lower():
            raise RuntimeError(f"SAFETY ABORT: Company ID {comp.company_id} is Boston Medical. Aborting!")
        return comp

    stmt = select(Company).where(
        or_(
            Company.company_key == "empresa-demo",
            and_(Company.is_demo.is_(True), Company.company_name == "Empresa Demo"),
        )
    )
    res = await db.execute(stmt)
    companies = list(res.scalars().all())

    if not companies:
        raise RuntimeError("SAFETY ABORT: Empresa Demo not found.")

    unique_companies = {c.company_id: c for c in companies}
    if len(unique_companies) > 1:
        details = [f"id={c.company_id}, name='{c.company_name}'" for c in unique_companies.values()]
        raise RuntimeError(f"SAFETY ABORT: Multiple demo companies found: {'; '.join(details)}")

    demo_company = list(unique_companies.values())[0]
    if demo_company.company_id == 1 or "boston" in demo_company.company_name.lower():
        raise RuntimeError(f"SAFETY ABORT: Resolved company is Boston Medical (id={demo_company.company_id}). Aborting!")

    return demo_company


def is_valid_demo_code(code: str) -> bool:
    """Check if code matches 6-8 uppercase alphanumeric characters."""
    return bool(re.match(r"^[A-Z0-9]{6,8}$", code))


def derive_target_code(current_code: str, service_name: str, used_codes: set) -> str:
    """Derive a clean 6-8 char alphanumeric code for a demo simulation."""
    current_upper = current_code.strip().upper()

    # 1. Known explicit mapping
    if current_upper in KNOWN_CODE_MAPPINGS:
        return KNOWN_CODE_MAPPINGS[current_upper]

    # 2. Already compliant
    if is_valid_demo_code(current_upper):
        return current_upper

    # 3. Dynamic service-based mnemonic
    svc_upper = (service_name or "").upper()
    if "VENT" in svc_upper:
        prefix = "VENT"
    elif "ATEN" in svc_upper or "CLIENT" in svc_upper:
        prefix = "ATEN"
    elif "RECL" in svc_upper:
        prefix = "RECL"
    elif "SOPO" in svc_upper:
        prefix = "SOPO"
    else:
        clean = re.sub(r"[^A-Z0-9]", "", svc_upper)
        prefix = clean[:4] if len(clean) >= 4 else clean.ljust(4, "X")

    for i in range(1, 100):
        cand = f"{prefix}{i:02d}"
        if cand not in used_codes:
            return cand

    for i in range(100, 1000):
        cand = f"{prefix[:5]}{i:03d}"
        if cand not in used_codes:
            return cand

    raise RuntimeError(f"Could not generate unique code for code '{current_code}'")


async def migrate_demo_simulation_codes(
    db: AsyncSession,
    apply: bool = False,
    override_company_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute dry-run or apply migration of demo simulation codes."""
    demo_company = await resolve_demo_company(db, override_company_id=override_company_id)
    cid = demo_company.company_id
    cname = demo_company.company_name
    logger.info("Target company resolved: id=%d, name='%s', key='%s'", cid, cname, demo_company.company_key)

    # Fetch all simulations for this company joined with Service
    stmt = (
        select(TrainerSimulation, Service.service_name)
        .outerjoin(Service, TrainerSimulation.service_id == Service.service_id)
        .where(TrainerSimulation.company_id == cid)
        .order_by(TrainerSimulation.simulation_id)
    )
    res = await db.execute(stmt)
    sim_rows = res.all()

    # Also fetch all globally used simulation codes across the DB to prevent collisions
    stmt_global = select(TrainerSimulation.code, TrainerSimulation.simulation_id)
    res_global = await db.execute(stmt_global)
    global_codes_map = {row[0].upper(): row[1] for row in res_global.all()}

    logger.info("Found %d simulations for Empresa Demo.", len(sim_rows))

    plan: List[Dict[str, Any]] = []
    used_in_batch = set(global_codes_map.keys())

    for sim, svc_name in sim_rows:
        target_code = derive_target_code(sim.code, svc_name or "", used_in_batch)
        action = "UPDATE" if sim.code != target_code else "ALREADY_COMPLIANT"

        # Check collision with other simulations
        existing_owner_id = global_codes_map.get(target_code)
        if existing_owner_id and existing_owner_id != sim.simulation_id:
            raise RuntimeError(
                f"Collision detected! Target code '{target_code}' is already used by simulation {existing_owner_id}."
            )

        used_in_batch.add(target_code)

        # Count related records for audit
        v_count = await db.scalar(
            select(func.count()).where(TrainerSimulationVersion.simulation_id == sim.simulation_id)
        )
        s_count = await db.scalar(
            select(func.count()).where(TrainerSession.simulation_id == sim.simulation_id)
        )

        plan.append({
            "simulation_id": sim.simulation_id,
            "name": sim.name,
            "service_name": svc_name,
            "current_code": sim.code,
            "target_code": target_code,
            "status": sim.status,
            "action": action,
            "versions_count": v_count,
            "sessions_count": s_count,
        })

    # Print summary table
    print("\n" + "=" * 110)
    print(f"{'ID':<6} | {'STATUS':<10} | {'CURRENT CODE':<15} | {'NEW CODE':<10} | {'ACTION':<18} | {'VERSIONS':<8} | {'NAME'}")
    print("-" * 110)
    for p in plan:
        print(f"{p['simulation_id']:<6} | {p['status']:<10} | {p['current_code']:<15} | {p['target_code']:<10} | {p['action']:<18} | {p['versions_count']:<8} | {p['name'][:35]}")
    print("=" * 110 + "\n")

    updated_count = 0
    if apply:
        for p in plan:
            if p["action"] == "UPDATE":
                stmt_up = (
                    update(TrainerSimulation)
                    .where(TrainerSimulation.simulation_id == p["simulation_id"])
                    .values(
                        code=p["target_code"],
                        updated_at=datetime.now(timezone.utc)
                    )
                )
                await db.execute(stmt_up)
                updated_count += 1
        await db.commit()
        logger.info("[APPLY] Successfully updated %d simulation codes in Empresa Demo.", updated_count)
    else:
        await db.rollback()
        logger.info("[DRY RUN] Preview complete. %d simulations would be updated. No changes committed.", sum(1 for p in plan if p["action"] == "UPDATE"))

    return {
        "company_id": cid,
        "company_name": cname,
        "total_simulations": len(plan),
        "updated": updated_count if apply else sum(1 for p in plan if p["action"] == "UPDATE"),
        "apply": apply,
        "plan": [
            {
                "simulation_id": p["simulation_id"],
                "current_code": p["current_code"],
                "target_code": p["target_code"],
                "action": p["action"],
                "name": p["name"],
                "service": p["service_name"],
            }
            for p in plan
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate Empresa Demo simulation codes to 6-8 char alphanumeric format.")
    parser.add_argument("--db-url", type=str, default=None, help="Custom database URL")
    parser.add_argument("--company-id", type=int, default=None, help="Explicit company ID override (must not be Boston Medical)")
    parser.add_argument("--apply", action="store_true", default=False, help="Apply changes (default is dry-run)")
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
        await migrate_demo_simulation_codes(
            session,
            apply=args.apply,
            override_company_id=args.company_id,
        )


if __name__ == "__main__":
    asyncio.run(async_main())
