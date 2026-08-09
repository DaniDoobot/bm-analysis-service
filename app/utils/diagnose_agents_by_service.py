"""
Read-only diagnostic script to inspect services, teams, agents, and evaluations by service in DB.
Supports both PostgreSQL and SQLite fallback.
Run with: python app/utils/diagnose_agents_by_service.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.services.dashboard_service import get_agents_list
from app.utils.hubspot_owners import OWNER_TO_NAME


async def run_diagnostics():
    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///temp_diag.db")
    try:
        from app.db import get_engine
        engine = get_engine()
    except Exception:
        engine = create_async_engine(db_url)

    try:
        async with AsyncSession(engine) as db:
            print("===============================================================")
            print("A. SERVICES IN DATABASE (bm_services)")
            print("===============================================================")
            try:
                res_svc = await db.execute(select(Service))
                services = list(res_svc.scalars().all())
                for s in services:
                    print(f"  Service ID={s.service_id} | Key='{s.service_key}' | Name='{s.service_name}' | CompanyID={s.company_id}")
            except Exception as e:
                print(f"  Error fetching services: {e}")
                services = []

            print("\n===============================================================")
            print("B. TEAMS BY SERVICE (bm_teams)")
            print("===============================================================")
            try:
                res_teams = await db.execute(select(Team))
                teams = list(res_teams.scalars().all())
                for t in teams:
                    print(f"  Team ID={t.team_id} | Name='{t.team_name}' | ServiceID={t.service_id} | CompanyID={t.company_id}")
            except Exception as e:
                print(f"  Error fetching teams: {e}")
                teams = []

            print("\n===============================================================")
            print("C. ACTIVE USERS WITH HUBSPOT_OWNER_ID (bm_users)")
            print("===============================================================")
            try:
                res_users = await db.execute(
                    select(User).where(User.is_active == True, User.hubspot_owner_id.is_not(None))
                )
                users = list(res_users.scalars().all())

                # Build user service map
                user_services_map: dict[int, set[int]] = {}
                for u in users:
                    s_set = set()
                    if u.primary_service_id:
                        s_set.add(u.primary_service_id)
                    if u.primary_team_id:
                        t_obj = next((t for t in teams if t.team_id == u.primary_team_id), None)
                        if t_obj:
                            s_set.add(t_obj.service_id)
                    user_services_map[u.user_id] = s_set

                res_us = await db.execute(select(UserServiceAssociation))
                for row in res_us.scalars().all():
                    if row.user_id in user_services_map:
                        user_services_map[row.user_id].add(row.service_id)

                team_service_map = {t.team_id: t.service_id for t in teams}
                res_ut = await db.execute(select(UserTeamAssociation))
                for row in res_ut.scalars().all():
                    if row.user_id in user_services_map and row.team_id in team_service_map:
                        user_services_map[row.user_id].add(team_service_map[row.team_id])

                res_at = await db.execute(select(AgentTeamAssociation))
                for row in res_at.scalars().all():
                    if row.user_id in user_services_map and row.team_id in team_service_map:
                        user_services_map[row.user_id].add(team_service_map[row.team_id])

                for u in users:
                    svcs = user_services_map.get(u.user_id, set())
                    print(f"  User ID={u.user_id} | Name='{u.name or u.username}' | Role='{u.role}' | OwnerID='{u.hubspot_owner_id}' | PrimarySvc={u.primary_service_id} | PrimaryTeam={u.primary_team_id} | AllAssignedServices={sorted(list(svcs))}")
            except Exception as e:
                print(f"  Error fetching users: {e}")

            print("\n===============================================================")
            print("D. EVALUATIONS BY AGENT AND SERVICE IN LAST 30 DAYS (bm_mass_evaluation_results)")
            print("===============================================================")
            try:
                q_evals = """
                    SELECT service_id, service_key, hubspot_owner_id, agent_name, COUNT(*) as cnt
                    FROM bm_mass_evaluation_results
                    WHERE status = 'completed' AND hubspot_owner_id IS NOT NULL
                    GROUP BY service_id, service_key, hubspot_owner_id, agent_name
                    ORDER BY service_id, cnt DESC;
                """
                res_ev = await db.execute(text(q_evals))
                for r in res_ev.fetchall():
                    print(f"  Service ID={r[0]} ({r[1]}) | OwnerID='{r[2]}' | AgentName='{r[3]}' | Count={r[4]}")
            except Exception as e:
                print(f"  Error fetching mass evaluation stats: {e}")

            print("\n===============================================================")
            print("E. CURRENT /bm/agents OUTPUT PER SERVICE")
            print("===============================================================")
            for s in services + [None]:
                svc_id = s.service_id if s else None
                svc_label = f"Service '{s.service_name}' (ID={svc_id})" if s else "ALL SERVICES (service_id=None)"
                try:
                    res_agents = await get_agents_list(db, service_id=svc_id)
                    print(f"\n--- {svc_label} ---")
                    for ag in res_agents:
                        print(f"  [{ag['initials'] or '??'}] {ag['name']} (owner_id={ag['hubspot_owner_id']}) -> total_analyses={ag['total_analyses']}")
                except Exception as e_ag:
                    print(f"  Error fetching /bm/agents for {svc_label}: {e_ag}")
    except Exception as e_main:
        print(f"Diagnostic error: {e_main}")


if __name__ == "__main__":
    asyncio.run(run_diagnostics())
