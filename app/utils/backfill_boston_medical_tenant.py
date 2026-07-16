import argparse
import asyncio
import os
import sys

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db import AsyncSessionLocal
from sqlalchemy import select
from app.models.companies import Company
from app.models.users import User
from app.models.services import Service
from app.models.teams import Team

async def run_backfill(mode: str):
    async with AsyncSessionLocal() as db:
        print(f"Executing backfill in mode: {mode}")

        # 1. Verify/Create Boston Medical company
        comp_stmt = select(Company).where(Company.company_key == "boston-medical")
        comp_res = await db.execute(comp_stmt)
        boston_medical = comp_res.scalars().first()

        if not boston_medical:
            if mode == "--apply":
                boston_medical = Company(
                    company_name="Boston Medical",
                    company_key="boston-medical",
                    is_active=True
                )
                db.add(boston_medical)
                await db.flush()
                print(f"[APPLY] Created Boston Medical company (ID: {boston_medical.company_id})")
            else:
                # Mock a fake company_id for dry-run if not exists
                boston_medical = Company(company_id=999, company_name="Boston Medical", company_key="boston-medical")
                print(f"[DRY-RUN] Would create Boston Medical company")
        else:
            print(f"Found existing Boston Medical company (ID: {boston_medical.company_id})")

        boston_id = boston_medical.company_id

        # 2. Users Backfill
        users_stmt = select(User).where(User.company_id == None)
        users_res = await db.execute(users_stmt)
        users_null = users_res.scalars().all()
        print(f"Found {len(users_null)} users without company_id")

        if users_null:
            for u in users_null:
                print(f"  - User: username={u.username}, email={u.email} -> will assign to Boston Medical")
                if mode == "--apply":
                    u.company_id = boston_id

        # 3. Services Backfill
        services_stmt = select(Service).where(Service.company_id == None)
        services_res = await db.execute(services_stmt)
        services_null = services_res.scalars().all()
        print(f"Found {len(services_null)} services without company_id")

        if services_null:
            for s in services_null:
                print(f"  - Service: name={s.service_name}, key={s.service_key} -> will assign to Boston Medical")
                if mode == "--apply":
                    s.company_id = boston_id

        # 4. Teams Backfill
        teams_stmt = select(Team).where(Team.company_id == None)
        teams_res = await db.execute(teams_stmt)
        teams_null = teams_res.scalars().all()
        print(f"Found {len(teams_null)} teams without company_id")

        if teams_null:
            for t in teams_null:
                print(f"  - Team: name={t.team_name} -> will assign to Boston Medical")
                if mode == "--apply":
                    t.company_id = boston_id

        # 5. Commit/Rollback
        if mode == "--apply":
            await db.commit()
            print("Successfully applied and committed backfill changes to database.")
        elif mode == "--dry-run":
            await db.rollback()
            print("Dry-run complete. No changes were committed.")


async def verify_backfill():
    async with AsyncSessionLocal() as db:
        print("Starting verification checks...")
        inconsistencies = []

        # Check NULL company_id
        res_usr = await db.execute(select(User).where(User.company_id == None))
        users_null = res_usr.scalars().all()
        if users_null:
            inconsistencies.append(f"{len(users_null)} users have NULL company_id")

        res_svc = await db.execute(select(Service).where(Service.company_id == None))
        services_null = res_svc.scalars().all()
        if services_null:
            inconsistencies.append(f"{len(services_null)} services have NULL company_id")

        res_team = await db.execute(select(Team).where(Team.company_id == None))
        teams_null = res_team.scalars().all()
        if teams_null:
            inconsistencies.append(f"{len(teams_null)} teams have NULL company_id")

        # Check team company_id matches service company_id
        teams_all_res = await db.execute(select(Team))
        teams_all = teams_all_res.scalars().all()
        for t in teams_all:
            svc_res = await db.execute(select(Service).where(Service.service_id == t.service_id))
            svc = svc_res.scalars().first()
            if svc and t.company_id != svc.company_id:
                inconsistencies.append(
                    f"Team '{t.team_name}' company_id ({t.company_id}) does not match Service '{svc.service_name}' company_id ({svc.company_id})"
                )

        if inconsistencies:
            print("\n=== VERIFICATION FAILED: Inconsistencies detected ===")
            for inc in inconsistencies:
                print(f"  [ERROR] {inc}")
        else:
            print("\n=== VERIFICATION SUCCESS: All tenant hierarchy data is consistent ===")


def main():
    parser = argparse.ArgumentParser(description="Backfill legacy tenant records into Boston Medical.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview backfill changes without saving")
    group.add_argument("--apply", action="store_true", help="Apply backfill changes to database")
    group.add_argument("--verify", action="store_true", help="Verify tenant hierarchy data consistency")

    args = parser.parse_args()

    if args.verify:
        asyncio.run(verify_backfill())
    elif args.dry_run:
        asyncio.run(run_backfill("--dry-run"))
    elif args.apply:
        asyncio.run(run_backfill("--apply"))


if __name__ == "__main__":
    main()
