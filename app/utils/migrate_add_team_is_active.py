"""
Migration: Add is_active column to bm_teams table.

Run once in production:
    python app/utils/migrate_add_team_is_active.py --apply

Dry-run (no changes):
    python app/utils/migrate_add_team_is_active.py --dry-run
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import text
from app.db import get_engine

SQL_CHECK = """
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name = 'bm_teams' AND column_name = 'is_active';
"""

SQL_ADD_COLUMN = """
    ALTER TABLE bm_teams 
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
"""


async def run(apply: bool):
    engine = get_engine()
    db_url = str(engine.url)
    print(f"[INFO] Target database: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    async with engine.begin() as conn:
        # Check if column already exists
        result = await conn.execute(text(SQL_CHECK))
        existing = result.fetchone()

        if existing:
            print("[OK] Column 'is_active' already exists in bm_teams. Nothing to do.")
            return

        if apply:
            print("[APPLY] Adding column 'is_active' BOOLEAN NOT NULL DEFAULT TRUE to bm_teams...")
            await conn.execute(text(SQL_ADD_COLUMN))
            print("[OK] Column added successfully.")
        else:
            print("[DRY-RUN] Would execute:")
            print(f"  {SQL_ADD_COLUMN.strip()}")
            print("[DRY-RUN] No changes committed.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry-run"
    apply = mode == "--apply"

    if mode not in ("--apply", "--dry-run"):
        print("Usage: python migrate_add_team_is_active.py [--apply | --dry-run]")
        sys.exit(1)

    asyncio.run(run(apply=apply))
