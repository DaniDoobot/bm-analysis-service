import sys
import os
import argparse
import asyncio
from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.models.prompts import Prompt, PromptBaseStructure
from app.models.users import User

# Allocations agreed upon
ANGEL_USER_ID = 7
DANI_USER_ID = 2

ANGEL_PROMPTS = [3, 4, 5, 6, 9, 10, 11, 12, 13, 20, 22, 25]
DANI_PROMPTS = [7, 8, 17, 21, 23]
# System or orphaned prompts to dani
SYSTEM_ORPHANED_PROMPTS = [1, 2, 19, 24]

from app.config import get_settings

async def run_backfill(execute: bool, confirm: bool):
    settings = get_settings()
    db_url = settings.database_url or ""
    
    # Parse DB url to print host and db name
    try:
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        db_host = parsed.hostname
        db_name = parsed.path.lstrip('/')
    except Exception:
        db_host = "Unknown"
        db_name = "Unknown"
        
    print("===============================================================================")
    print(f"DATABASE HOST: {db_host}")
    print(f"DATABASE NAME: {db_name}")
    print(f"RUN MODE:      {'EXECUTE' if execute else 'DRY RUN'}")
    print("===============================================================================")
    
    is_prod = (
        "91.98.230.119" in db_url
        or db_url.endswith("/n8n")
        or "/n8n?" in db_url
        or "speechbm.doobot.ai" in db_url.lower()
        or ("prod" in db_url.lower() and "_test" not in db_url.lower() and "_dev" not in db_url.lower())
    )
    
    if is_prod:
        print("CRITICAL SAFETY VIOLATION: Execution on production database is strictly forbidden.")
        print("Execution has been blocked to prevent data loss. Aborting.")
        sys.exit(3)
    else:
        if execute and not confirm:
            ans = input("\nAre you sure you want to execute this ownership backfill? (type 'yes' to confirm): ")
            if ans.lower() != 'yes':
                print("Aborted.")
                sys.exit(0)

    engine = get_engine()
    
    # 1. Protection Check: is it already done?
    async with AsyncSession(engine) as db:
        # Check if any NULL owner exists
        stmt_null_base = select(PromptBaseStructure.id).where(PromptBaseStructure.owner_user_id == None)
        null_bases = (await db.execute(stmt_null_base)).scalars().all()
        
        stmt_null_prompt = select(Prompt.prompt_id).where(Prompt.owner_user_id == None)
        null_prompts = (await db.execute(stmt_null_prompt)).scalars().all()
        
        if not null_bases and not null_prompts:
            print("PROTECTION WARNING: All base structures and specific prompts already have an owner assigned.")
            print("There is no need to run backfill. Aborting to prevent accidental double run.")
            return

        # 2. Validate target users (dani & angel) are active and not agents
        for uid, name in [(DANI_USER_ID, "dani"), (ANGEL_USER_ID, "angel")]:
            user = await db.get(User, uid)
            if not user:
                raise ValueError(f"Error: Target user '{name}' (ID {uid}) does not exist in the database.")
            if not user.is_active:
                raise ValueError(f"Error: Target user '{name}' (ID {uid}) is INACTIVE.")
            if user.role == "agent":
                raise ValueError(f"Error: Target user '{name}' (ID {uid}) has the role 'agent'.")
            print(f"Validated target user '{name}' (ID {uid}): Active and not agent.")

    print("\n--- OWNERSHIP ALLOCATION TABLE ---")
    print("Base structures: all to dani (ID 2)")
    print(f"Angel's specific structures: {ANGEL_PROMPTS} to angel (ID 7)")
    print(f"Dani's specific structures: {DANI_PROMPTS} to dani (ID 2)")
    print(f"System/Orphaned structures: {SYSTEM_ORPHANED_PROMPTS} to dani (ID 2)")
    
    if not execute:
        print("\n[DRY RUN] No changes will be written to the database. Run with --execute to commit.")
        return

    if not confirm and not is_prod:
        ans = input("\nAre you sure you want to execute this ownership backfill? (type 'yes' to confirm): ")
        if ans.lower() != 'yes':
            print("Aborted.")
            return

    # Execute
    print("\nExecuting backfill...")
    async with AsyncSession(engine) as db:
        try:
            # 1. Base structures
            res_base = await db.execute(
                update(PromptBaseStructure)
                .where(PromptBaseStructure.owner_user_id == None)
                .values(owner_user_id=DANI_USER_ID)
            )
            print(f"Base structures updated: {res_base.rowcount}")
            
            # 2. Specific structures (angel)
            res_angel = await db.execute(
                update(Prompt)
                .where(and_(Prompt.prompt_id.in_(ANGEL_PROMPTS), Prompt.owner_user_id == None))
                .values(owner_user_id=ANGEL_USER_ID)
            )
            print(f"Angel's specific structures updated: {res_angel.rowcount}")
            
            # 3. Specific structures (dani)
            res_dani = await db.execute(
                update(Prompt)
                .where(and_(Prompt.prompt_id.in_(DANI_PROMPTS), Prompt.owner_user_id == None))
                .values(owner_user_id=DANI_USER_ID)
            )
            print(f"Dani's specific structures updated: {res_dani.rowcount}")
            
            # 4. Remaining specific structures (system/orphaned)
            res_rem = await db.execute(
                update(Prompt)
                .where(Prompt.owner_user_id == None)
                .values(owner_user_id=DANI_USER_ID)
            )
            print(f"Remaining specific structures (system/orphaned) updated: {res_rem.rowcount}")
            
            await db.commit()
            print("Ownership backfill completed successfully!")
        except Exception as e:
            await db.rollback()
            print(f"Error executing backfill: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run structure ownership backfill script.")
    parser.add_argument("--execute", action="store_true", help="Execute changes in database.")
    parser.add_argument("--confirm", action="store_true", help="Bypass confirmation prompt.")
    args = parser.parse_args()
    
    asyncio.run(run_backfill(args.execute, args.confirm))
