#!/usr/bin/env python
"""
scripts/seed_demo_company.py
=============================
Transactional seeding tool for the isolated Demo Company structure.

Creates:
1. Company:
   - Name: "Empresa Demo"
   - Key: "empresa-demo"
   - is_demo: True
2. Services (2):
   - "Atención al Cliente" (key: "atencion-al-cliente")
   - "Ventas" (key: "ventas")
3. Teams (4):
   - Atención al Cliente:
     - "Front Atención" (10 agents)
     - "Backoffice Atención" (20 agents)
   - Ventas:
     - "Equipo Comercial" (10 agents)
     - "Equipo Retención" (20 agents)
4. Users (67 total):
   - 1 Company Admin (administradordeempresa.demo@doobot.ai)
   - 2 Service Managers (administrador.atencion.demo@doobot.ai, administrador.ventas.demo@doobot.ai)
   - 4 Team Coordinators (administrador.<equipo>.demo@doobot.ai)
   - 60 Agents (agente.demo.01@doobot.ai .. agente.demo.60@doobot.ai)
5. Trainer Credentials:
   - Provisions canonical training credentials via AgentCredentialsService for all 60 agents.
6. Associations:
   - Syncs bm_user_services, bm_user_teams, and bm_agent_teams.

Safety Rules:
- Enforces strict tenant isolation (all records bound to the new company_id).
- Aborts if company_key='empresa-demo' already exists in the database.
- Runs in a single atomic transaction; any error triggers full rollback.
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.core.roles import InternalRole
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.services.agent_credentials_service import AgentCredentialsService
from app.services.users_service import (
    save_user_service_associations,
    save_user_team_associations,
)
from app.utils.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("seed_demo_company")

DEMO_COMPANY_NAME = "Empresa Demo"
DEMO_COMPANY_KEY = "empresa-demo"
DEMO_DEFAULT_PASSWORD = "DemoPassword123!"


class DemoCompanySeedError(Exception):
    """Base exception for demo company seeding errors."""
    pass


class DemoCompanyAlreadyExistsError(DemoCompanySeedError):
    """Raised when the demo company key already exists in the database."""
    pass


async def seed_demo_structure(db: AsyncSession) -> Dict[str, Any]:
    """
    Execute transactional seeding of the Demo Company base structure.
    
    Must be called inside an active transaction (e.g. async with db.begin():).
    """
    logger.info("Starting Demo Company structure seed...")

    # 1. Check if demo company already exists
    stmt_check = select(Company).where(Company.company_key == DEMO_COMPANY_KEY)
    res_check = await db.execute(stmt_check)
    existing_comp = res_check.scalars().first()
    if existing_comp:
        raise DemoCompanyAlreadyExistsError(
            f"Company with key '{DEMO_COMPANY_KEY}' already exists (id={existing_comp.company_id}, name='{existing_comp.company_name}'). "
            "Aborting seed to prevent duplication. Run purge_company.py first if you wish to recreate it."
        )

    # 2. Create Company
    company = Company(
        company_name=DEMO_COMPANY_NAME,
        company_key=DEMO_COMPANY_KEY,
        is_demo=True,
        is_active=True,
    )
    db.add(company)
    await db.flush()
    logger.info("Created Company: id=%s, name='%s'", company.company_id, company.company_name)

    company_id = company.company_id
    pass_hash = hash_password(DEMO_DEFAULT_PASSWORD)

    # 3. Create Services
    svc_atencion = Service(
        company_id=company_id,
        service_key="atencion-al-cliente",
        service_name="Atención al Cliente",
        description="Servicio de atención general al cliente, soporte y consultas.",
        is_active=True,
    )
    svc_ventas = Service(
        company_id=company_id,
        service_key="ventas",
        service_name="Ventas",
        description="Servicio comercial de captación, fidelización y retención de clientes.",
        is_active=True,
    )
    db.add_all([svc_atencion, svc_ventas])
    await db.flush()
    logger.info("Created Services: Atención (id=%s), Ventas (id=%s)", svc_atencion.service_id, svc_ventas.service_id)

    # 4. Create Teams
    # Atención al Cliente
    team_front = Team(
        company_id=company_id,
        service_id=svc_atencion.service_id,
        team_name="Front Atención",
        is_active=True,
    )
    team_backoffice = Team(
        company_id=company_id,
        service_id=svc_atencion.service_id,
        team_name="Backoffice Atención",
        is_active=True,
    )
    # Ventas
    team_comercial = Team(
        company_id=company_id,
        service_id=svc_ventas.service_id,
        team_name="Equipo Comercial",
        is_active=True,
    )
    team_retencion = Team(
        company_id=company_id,
        service_id=svc_ventas.service_id,
        team_name="Equipo Retención",
        is_active=True,
    )
    db.add_all([team_front, team_backoffice, team_comercial, team_retencion])
    await db.flush()
    logger.info(
        "Created Teams: Front (id=%s), Backoffice (id=%s), Comercial (id=%s), Retención (id=%s)",
        team_front.team_id,
        team_backoffice.team_id,
        team_comercial.team_id,
        team_retencion.team_id,
    )

    created_users: List[User] = []

    # 5. Create 1 Company Admin
    comp_admin = User(
        username="administradordeempresa.demo",
        email="administradordeempresa.demo@doobot.ai",
        name="Administrador Empresa Demo",
        role="company_admin",
        company_id=company_id,
        primary_service_id=None,
        primary_team_id=None,
        password_hash=pass_hash,
        is_active=True,
        must_reset_password=False,
    )
    db.add(comp_admin)
    await db.flush()
    await save_user_service_associations(
        db, comp_admin.user_id, [svc_atencion.service_id, svc_ventas.service_id]
    )
    created_users.append(comp_admin)

    # 6. Create 2 Service Managers
    mgr_atencion = User(
        username="administrador.atencion.demo",
        email="administrador.atencion.demo@doobot.ai",
        name="Administrador Atención Demo",
        role="service_manager",
        company_id=company_id,
        primary_service_id=svc_atencion.service_id,
        primary_team_id=None,
        password_hash=pass_hash,
        is_active=True,
        must_reset_password=False,
    )
    mgr_ventas = User(
        username="administrador.ventas.demo",
        email="administrador.ventas.demo@doobot.ai",
        name="Administrador Ventas Demo",
        role="service_manager",
        company_id=company_id,
        primary_service_id=svc_ventas.service_id,
        primary_team_id=None,
        password_hash=pass_hash,
        is_active=True,
        must_reset_password=False,
    )
    db.add_all([mgr_atencion, mgr_ventas])
    await db.flush()
    await save_user_service_associations(db, mgr_atencion.user_id, [svc_atencion.service_id])
    await save_user_service_associations(db, mgr_ventas.user_id, [svc_ventas.service_id])
    created_users.extend([mgr_atencion, mgr_ventas])

    # 7. Create 4 Team Coordinators
    coord_definitions = [
        ("administrador.frontatencion.demo", "administrador.frontatencion.demo@doobot.ai", "Coordinador Front Atención Demo", svc_atencion.service_id, team_front.team_id),
        ("administrador.backofficeatencion.demo", "administrador.backofficeatencion.demo@doobot.ai", "Coordinador Backoffice Atención Demo", svc_atencion.service_id, team_backoffice.team_id),
        ("administrador.equipocomercial.demo", "administrador.equipocomercial.demo@doobot.ai", "Coordinador Equipo Comercial Demo", svc_ventas.service_id, team_comercial.team_id),
        ("administrador.equiporetencion.demo", "administrador.equiporetencion.demo@doobot.ai", "Coordinador Equipo Retención Demo", svc_ventas.service_id, team_retencion.team_id),
    ]

    for uname, email, full_name, s_id, t_id in coord_definitions:
        coord_user = User(
            username=uname,
            email=email,
            name=full_name,
            role="team_coordinator",
            company_id=company_id,
            primary_service_id=s_id,
            primary_team_id=t_id,
            password_hash=pass_hash,
            is_active=True,
            must_reset_password=False,
        )
        db.add(coord_user)
        await db.flush()
        await save_user_service_associations(db, coord_user.user_id, [s_id])
        await save_user_team_associations(db, coord_user.user_id, [t_id], role="team_coordinator")
        created_users.append(coord_user)

    # 8. Create 60 Agents
    # Distribution:
    # 01..10 -> Front Atención (10)
    # 11..30 -> Backoffice Atención (20)
    # 31..40 -> Equipo Comercial (10)
    # 41..60 -> Equipo Retención (20)
    agents_created: List[User] = []
    for i in range(1, 61):
        idx_str = f"{i:02d}"
        if 1 <= i <= 10:
            target_svc = svc_atencion
            target_team = team_front
        elif 11 <= i <= 30:
            target_svc = svc_atencion
            target_team = team_backoffice
        elif 31 <= i <= 40:
            target_svc = svc_ventas
            target_team = team_comercial
        else:
            target_svc = svc_ventas
            target_team = team_retencion

        agent_user = User(
            username=f"agente.demo.{idx_str}",
            email=f"agente.demo.{idx_str}@doobot.ai",
            name=f"Agente Demo {idx_str}",
            role="agent",
            company_id=company_id,
            primary_service_id=target_svc.service_id,
            primary_team_id=target_team.team_id,
            hubspot_owner_id=f"demo_owner_{idx_str}",
            agent_initials=f"D{idx_str}",
            password_hash=pass_hash,
            is_active=True,
            must_reset_password=False,
        )
        db.add(agent_user)
        await db.flush()

        # Associations
        await save_user_service_associations(db, agent_user.user_id, [target_svc.service_id])
        await save_user_team_associations(db, agent_user.user_id, [target_team.team_id], role="agent")

        # Trainer credentials
        await AgentCredentialsService.ensure_agent_training_credentials(db, user=agent_user, commit=False)

        created_users.append(agent_user)
        agents_created.append(agent_user)

    # Verify Trainer credentials count
    tr_settings_res = await db.execute(
        select(TrainingAgentSetting).where(TrainingAgentSetting.company_id == company_id)
    )
    tr_settings = list(tr_settings_res.scalars().all())

    logger.info(
        "Seeded Demo Company structure: 1 company, 2 services, 4 teams, %d total users (%d agents), %d trainer settings.",
        len(created_users),
        len(agents_created),
        len(tr_settings),
    )

    return {
        "status": "success",
        "company": {
            "company_id": company.company_id,
            "company_name": company.company_name,
            "company_key": company.company_key,
            "is_demo": company.is_demo,
        },
        "services": [
            {"service_id": svc_atencion.service_id, "service_name": svc_atencion.service_name, "service_key": svc_atencion.service_key},
            {"service_id": svc_ventas.service_id, "service_name": svc_ventas.service_name, "service_key": svc_ventas.service_key},
        ],
        "teams": [
            {"team_id": team_front.team_id, "team_name": team_front.team_name, "service_id": svc_atencion.service_id, "agents_count": 10},
            {"team_id": team_backoffice.team_id, "team_name": team_backoffice.team_name, "service_id": svc_atencion.service_id, "agents_count": 20},
            {"team_id": team_comercial.team_id, "team_name": team_comercial.team_name, "service_id": svc_ventas.service_id, "agents_count": 10},
            {"team_id": team_retencion.team_id, "team_name": team_retencion.team_name, "service_id": svc_ventas.service_id, "agents_count": 20},
        ],
        "users": {
            "total": len(created_users),
            "company_admins": 1,
            "service_managers": 2,
            "team_coordinators": 4,
            "agents": len(agents_created),
        },
        "trainer_settings_count": len(tr_settings),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Seed base structure for Demo Company (Empresa Demo) in an isolated tenant."
    )
    parser.add_argument("--db-url", type=str, default=None, help="Optional custom database URL")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Roll back transaction without applying changes")
    return parser.parse_args()


async def async_main():
    args = parse_args()
    settings = get_settings()
    db_url = args.db_url or os.environ.get("DATABASE_URL") or settings.database_url
    if not db_url:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    engine = create_async_engine(db_url, echo=False)

    print("=" * 80)
    print(f"SEED DEMO COMPANY - Mode: {'DRY RUN' if args.dry_run else 'APPLY'}")
    print("=" * 80)

    try:
        async with AsyncSession(engine) as session:
            async with session.begin():
                summary = await seed_demo_structure(session)
                if args.dry_run:
                    await session.rollback()
                    print("\n[DRY-RUN] Seeding logic executed successfully. Rolled back changes.")
                else:
                    print("\n[APPLY] Base structure seeded and committed successfully!")

        print("-" * 80)
        print(f"Company: {summary['company']['company_name']} [id={summary['company']['company_id']}, key='{summary['company']['company_key']}']")
        print(f"Services: {len(summary['services'])}")
        for s in summary['services']:
            print(f"  - {s['service_name']} (id={s['service_id']})")
        print(f"Teams: {len(summary['teams'])}")
        for t in summary['teams']:
            print(f"  - {t['team_name']} (id={t['team_id']}, agents={t['agents_count']})")
        print(f"Users: {summary['users']['total']} (Agents: {summary['users']['agents']}, Admins: {summary['users']['company_admins']}, Mgrs: {summary['users']['service_managers']}, Coords: {summary['users']['team_coordinators']})")
        print(f"Trainer Credentials: {summary['trainer_settings_count']}")
        print("=" * 80)

    except DemoCompanyAlreadyExistsError as e:
        print(f"\nABORTED: {e}")
        sys.exit(2)
    except Exception as e:
        logger.exception("Seeding failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(async_main())
