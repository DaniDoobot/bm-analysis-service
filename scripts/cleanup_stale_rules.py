#!/usr/bin/env python3
"""
Administration script to cleanup stale rules in bm_prompt_criterion_typologies.
By default runs as a safe DRY-RUN. Run with --commit to execute the cleanup.
"""
import asyncio
import os
import sys
import argparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Use settings to load DB URL safely
from app.config import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

async def run_cleanup(commit: bool):
    settings = get_settings()
    db_url = settings.database_url
    if "postgresql" in db_url and "91.98.230.119" in db_url:
        print("Target database is PRODUCTION.")
    else:
        print(f"Target database: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    engine = create_async_engine(db_url.replace("postgresql://", "postgresql+asyncpg://").replace("postgres://", "postgresql+asyncpg://"))
    
    async with AsyncSession(engine) as db:
        # 1. Fetch rules for inactive typologies
        inactive_query = text("""
            SELECT pct.id, pc.criterion_key, pc.prompt_id, t.typology_key
            FROM bm_prompt_criterion_typologies pct
            JOIN bm_prompt_criteria pc ON pc.criterion_id = pct.criterion_id
            JOIN bm_typologies t ON t.typology_id = pct.typology_id
            WHERE t.is_active = false
        """)
        inactive_rows = (await db.execute(inactive_query)).fetchall()
        
        # 2. Fetch rules for desassociated typologies
        desassoc_query = text("""
            SELECT pct.id, pc.criterion_key, pc.prompt_id, t.typology_key, p.base_structure_id
            FROM bm_prompt_criterion_typologies pct
            JOIN bm_prompt_criteria pc ON pc.criterion_id = pct.criterion_id
            JOIN bm_typologies t ON t.typology_id = pct.typology_id
            JOIN bm_prompts p ON p.prompt_id = pc.prompt_id
            LEFT JOIN bm_base_structure_typologies bst ON bst.base_structure_id = p.base_structure_id AND bst.typology_id = t.typology_id
            WHERE t.is_active = true AND p.base_structure_id IS NOT NULL AND bst.id IS NULL
        """)
        desassoc_rows = (await db.execute(desassoc_query)).fetchall()

        print("\n" + "="*80)
        print(f"DRY-RUN ANALYSIS: FOUND {len(inactive_rows)} INACTIVE RULES AND {len(desassoc_rows)} DESASSOCIATED RULES")
        print("="*80)
        
        if inactive_rows:
            print("\n--- INACTIVE RULES (Typology is_active = false) ---")
            for r in inactive_rows:
                print(f"  RuleID={r[0]} | Criterio={r[1]} (Prompt={r[2]}) | Typology={r[3]} (Inactive)")
                
        if desassoc_rows:
            print("\n--- DESASSOCIATED RULES (Typology active but desassociated from Prompt's Base Structure) ---")
            for r in desassoc_rows:
                print(f"  RuleID={r[0]} | Criterio={r[1]} (Prompt={r[2]}) | Typology={r[3]} | BaseStructureID={r[4]}")
                
        if not commit:
            print("\n" + "="*80)
            print("DRY-RUN COMPLETE. No changes were made. Run with --commit to execute the cleanup.")
            print("="*80)
            return

        print("\n" + "="*80)
        print("EXECUTING CLEANUP...")
        print("="*80)

        # Execute inactive rules deletion
        if inactive_rows:
            inactive_ids = [r[0] for r in inactive_rows]
            del_inactive = await db.execute(
                text("DELETE FROM bm_prompt_criterion_typologies WHERE id = ANY(:ids)"),
                {"ids": inactive_ids}
            )
            print(f"  Deleted {del_inactive.rowcount} inactive typology rules.")

        # Execute desassociated rules deletion
        if desassoc_rows:
            desassoc_ids = [r[0] for r in desassoc_rows]
            del_desassoc = await db.execute(
                text("DELETE FROM bm_prompt_criterion_typologies WHERE id = ANY(:ids)"),
                {"ids": desassoc_ids}
            )
            print(f"  Deleted {del_desassoc.rowcount} desassociated typology rules.")

        await db.commit()
        print("\nCLEANUP COMPLETE. Database changes committed.")
        print("="*80)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cleanup stale prompt criterion typologies.")
    parser.add_argument("--commit", action="store_true", help="Execute deletions in the database.")
    args = parser.parse_args()
    
    asyncio.run(run_cleanup(commit=args.commit))
