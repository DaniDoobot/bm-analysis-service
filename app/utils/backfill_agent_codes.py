"""
backfill_agent_codes.py
=======================
Idempotent administrative utility to provision missing training credentials
(alphanumeric training_code and 4-digit numeric PIN training_numeric_code)
for existing active agents.

Safety:
- Safe to run on live environments with --dry-run (default).
- Never overwrites existing non-null credentials.
- Only targets active users with normalized role 'agente' / 'agent',
  valid hubspot_owner_id, and valid agent_initials.
- Never prints plaintext credentials to stdout or logs.

Usage:
  python app/utils/backfill_agent_codes.py [--dry-run | --apply]
"""
import argparse
import asyncio
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import InternalRole, normalize_role
from app.models.personalized_training import TrainingAgentSetting
from app.models.services import Service
from app.models.teams import UserServiceAssociation
from app.models.users import User
from app.services.agent_credentials_service import AgentCredentialsService


async def run_backfill(db: AsyncSession, dry_run: bool = True) -> Dict[str, any]:
    """
    Scan for active agents with missing credentials and report or provision them.
    Returns summary dictionary.
    """
    # 1. Fetch active agents with hubspot_owner_id
    stmt_users = select(User).where(
        and_(
            User.is_active == True,
            User.hubspot_owner_id.isnot(None),
            User.hubspot_owner_id != "",
        )
    ).order_by(User.name.asc())
    res_users = await db.execute(stmt_users)
    all_candidate_users = [
        u for u in res_users.scalars().all()
        if normalize_role(u.role) == InternalRole.AGENT
    ]

    # 2. Fetch existing settings for these agents
    owner_ids = [u.hubspot_owner_id for u in all_candidate_users]
    settings_map: Dict[str, TrainingAgentSetting] = {}
    if owner_ids:
        stmt_settings = select(TrainingAgentSetting).where(
            TrainingAgentSetting.hubspot_owner_id.in_(owner_ids)
        )
        res_settings = await db.execute(stmt_settings)
        settings_map = {s.hubspot_owner_id: s for s in res_settings.scalars().all()}

    # 3. Fetch service mappings for reporting
    stmt_services = select(Service)
    res_services = await db.execute(stmt_services)
    services_map = {s.service_id: s.service_name for s in res_services.scalars().all()}

    stmt_user_services = select(UserServiceAssociation)
    res_user_services = await db.execute(stmt_user_services)
    user_to_service: Dict[int, str] = {}
    for assoc in res_user_services.scalars().all():
        s_name = services_map.get(assoc.service_id, f"Service {assoc.service_id}")
        user_to_service[assoc.user_id] = s_name

    # 4. Classify each agent
    pending_agents: List[Dict[str, any]] = []
    complete_agents: List[Dict[str, any]] = []

    for u in all_candidate_users:
        hs_id = u.hubspot_owner_id
        setting = settings_map.get(hs_id)

        has_alpha = bool(setting and setting.training_code and setting.training_code.strip())
        has_numeric = bool(setting and setting.training_numeric_code and setting.training_numeric_code.strip())

        svc_name = user_to_service.get(u.user_id) or (services_map.get(u.primary_service_id) if u.primary_service_id else "Sin Servicio")
        resolved_initials = (u.agent_initials or (setting.agent_initials if setting else None) or "").strip()

        agent_info = {
            "user_id": u.user_id,
            "name": u.name or u.username,
            "hubspot_owner_id": hs_id,
            "initials": resolved_initials,
            "service_name": svc_name,
            "setting_exists": setting is not None,
            "needs_training_code": not has_alpha,
            "needs_numeric_code": not has_numeric,
        }

        if not has_alpha or not has_numeric:
            pending_agents.append(agent_info)
        else:
            complete_agents.append(agent_info)

    print("\n" + "=" * 75)
    print("BACKFILL AGENT CODES — AUDIT & PROVISIONING")
    print("=" * 75)
    print(f"Mode: {'DRY RUN (No changes written)' if dry_run else 'APPLY (Provisioning missing credentials)'}")
    print(f"Total active agents scanned: {len(all_candidate_users)}")
    print(f"Agents with complete credentials: {len(complete_agents)}")
    print(f"Agents pending credentials: {len(pending_agents)}")
    print("=" * 75)

    # Group pending agents by service
    by_service: Dict[str, List[Dict[str, any]]] = {}
    for a in pending_agents:
        s = a["service_name"]
        by_service.setdefault(s, []).append(a)

    print("\n--- PENDING AGENTS BY SERVICE (ANONYMIZED) ---")
    for svc, agents in sorted(by_service.items()):
        print(f"\n[Service: {svc}] ({len(agents)} agents):")
        for a in agents:
            tc_flag = "NEEDS_CODE" if a["needs_training_code"] else "OK"
            num_flag = "NEEDS_PIN" if a["needs_numeric_code"] else "OK"
            setting_flag = "EXISTS" if a["setting_exists"] else "MISSING"
            print(f"  - {a['name']:<25} | Initials: {a['initials']:2} | Setting: {setting_flag:7} | Alpha: {tc_flag:10} | Numeric: {num_flag:9}")

    # Provision if apply mode
    provisioned_count = 0
    if not dry_run and pending_agents:
        print("\n--- APPLYING PROVISIONING ---")
        for a in pending_agents:
            u_obj = next((u for u in all_candidate_users if u.user_id == a["user_id"]), None)
            if u_obj:
                res = await AgentCredentialsService.ensure_agent_training_credentials(
                    db,
                    user=u_obj,
                    agent_initials=a["initials"],
                    commit=True,
                )
                if not u_obj.agent_initials and a["initials"]:
                    u_obj.agent_initials = a["initials"]
                    await db.commit()
                if res:
                    provisioned_count += 1
                    print(f"  [PROVISIONED] {a['name']} ({a['hubspot_owner_id']})")

        print(f"\nSuccessfully provisioned credentials for {provisioned_count} agents.")

    return {
        "total_scanned": len(all_candidate_users),
        "complete_count": len(complete_agents),
        "pending_count": len(pending_agents),
        "provisioned_count": provisioned_count,
        "by_service": {svc: len(ag) for svc, ag in by_service.items()},
        "pending_agents": pending_agents,
    }


async def main():
    parser = argparse.ArgumentParser(description="Backfill agent training credentials")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="Scan and report without modifying data (default)")
    group.add_argument("--apply", action="store_true", help="Provision missing credentials")

    args = parser.parse_args()
    dry_run = not args.apply

    from app.db import get_engine
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await run_backfill(session, dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
