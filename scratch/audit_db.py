import sys
import os
import asyncio
from sqlalchemy import text
from urllib.parse import urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import get_engine, SessionLocal
from app.config import get_settings

async def main():
    settings = get_settings()
    db_url = settings.database_url
    if not db_url:
        print("DATABASE_URL is not configured.")
        return
        
    parsed = urlparse(db_url)
    print(f"Database Host: {parsed.hostname}")
    print(f"Database Name: {parsed.path.lstrip('/')}")
    print(f"Database Username: {parsed.username}")
    
    engine = get_engine()
    
    tables_to_check = [
        "bm_training_agent_settings",
        "bm_training_runs",
        "bm_training_agent_reports",
        "bm_training_simulation_prompts",
        "bm_training_completion_status",
        "bm_training_call_sessions",
        "bm_training_call_evaluations",
        "bm_training_scheduler_settings",
        "bm_training_evaluation_prompts"
    ]
    
    async with SessionLocal() as db:
        print("\n=== Checking Tables Existence & Row Counts ===")
        for table in tables_to_check:
            try:
                res = await db.execute(text(f"SELECT COUNT(*) FROM {table};"))
                count = res.scalar()
                print(f"Table '{table}' exists: YES, Row count: {count}")
            except Exception as e:
                print(f"Table '{table}' exists: NO or Error: {e}")
                await db.rollback()

        print("\n=== Checking added columns in bm_training_agent_settings ===")
        try:
            res = await db.execute(text("""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = 'bm_training_agent_settings';
            """))
            cols = res.fetchall()
            for col in cols:
                if col[0] in ["training_code", "training_numeric_code", "training_code_enabled", "training_code_updated_at"]:
                    print(f"  Column '{col[0]}' exists: YES ({col[1]})")
        except Exception as e:
            print(f"Error checking columns: {e}")
            await db.rollback()

        print("\n=== Checking added columns in bm_users ===")
        try:
            res = await db.execute(text("""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = 'bm_users';
            """))
            cols = res.fetchall()
            for col in cols:
                if col[0] in ["hubspot_owner_id", "agent_initials", "must_reset_password", "password_set_at", "reset_token"]:
                    print(f"  Column '{col[0]}' exists: YES ({col[1]})")
        except Exception as e:
            print(f"Error checking columns in bm_users: {e}")
            await db.rollback()

        print("\n=== Checking dummy data / mock agents ===")
        # HubSpot owner IDs used in tests: e.g. "dummy_owner_1", "dummy_owner_2" or agent code test runs
        try:
            res = await db.execute(text("SELECT hubspot_owner_id, agent_name, training_code, training_numeric_code FROM bm_training_agent_settings;"))
            agents = res.fetchall()
            print("Agents in settings:")
            for agent in agents:
                print(f"  - {agent[0]} | {agent[1]} | Code: {agent[2]} | Numeric: {agent[3]}")
        except Exception as e:
            print(f"Error checking agents: {e}")
            await db.rollback()

        # Let's count sessions, evaluations, or reports with dummy owners
        for table in ["bm_training_agent_reports", "bm_training_call_sessions", "bm_training_call_evaluations", "bm_training_completion_status"]:
            try:
                # Find column name for agent id or owner id
                col_to_use = "hubspot_owner_id"
                if table == "bm_training_call_sessions" or table == "bm_training_call_evaluations":
                    col_to_use = "agent_id"
                
                res = await db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {col_to_use} LIKE 'dummy%';"))
                count = res.scalar()
                print(f"Dummy records in '{table}': {count}")
            except Exception as e:
                print(f"Error checking dummy data in '{table}': {e}")
                await db.rollback()

if __name__ == "__main__":
    asyncio.run(main())
