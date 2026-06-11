import sys
import os
import asyncio
from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import SessionLocal

async def main():
    async with SessionLocal() as db:
        print("=== DATABASE CLEANUP START ===")
        
        # 1. Delete training call evaluations, sessions, completion status, simulation prompts, agent reports, runs
        tables_to_clear = [
            "bm_training_call_evaluations",
            "bm_training_call_sessions",
            "bm_training_completion_status",
            "bm_training_simulation_prompts",
            "bm_training_agent_reports",
            "bm_training_runs",
            "bm_training_scheduler_settings",
            "bm_training_evaluation_prompts"
        ]
        
        for table in tables_to_clear:
            try:
                res = await db.execute(text(f"DELETE FROM {table};"))
                print(f"Cleared table '{table}': {res.rowcount} rows deleted.")
            except Exception as e:
                print(f"Error clearing table '{table}': {e}")
                await db.rollback()
                
        # 2. Reset agent settings codes to NULL
        try:
            res = await db.execute(text("""
                UPDATE bm_training_agent_settings 
                SET training_code = NULL, 
                    training_numeric_code = NULL;
            """))
            print(f"Reset training codes in 'bm_training_agent_settings': {res.rowcount} rows updated.")
        except Exception as e:
            print(f"Error resetting training codes: {e}")
            await db.rollback()
            
        await db.commit()
        print("=== DATABASE CLEANUP COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(main())
