import asyncio
import os
import sys
from sqlalchemy import text

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine

async def run_diagnostic():
    engine = get_engine()
    async with engine.connect() as conn:
        print("===============================================================================")
        print("   DIAGNOSTIC: MASS EVALUATION RESULTS DEDUPLICATION BY CALL_ID")
        print("===============================================================================")
        
        # 1. Check for call_id IS NULL
        q_nulls = "SELECT COUNT(*) FROM bm_mass_evaluation_results WHERE call_id IS NULL;"
        null_count = (await conn.execute(text(q_nulls))).scalar()
        print(f"Rows with call_id IS NULL: {null_count}")
        
        # 2. Get total rows
        q_total = "SELECT COUNT(*) FROM bm_mass_evaluation_results;"
        total_rows = (await conn.execute(text(q_total))).scalar()
        print(f"Total rows in bm_mass_evaluation_results: {total_rows}")
        print("-" * 80)
        
        # 3. Find duplicate groups by call_id
        q_duplicates = """
        SELECT call_id, COUNT(*) as occurrence_count
        FROM bm_mass_evaluation_results
        GROUP BY call_id
        HAVING COUNT(*) > 1
        ORDER BY occurrence_count DESC;
        """
        duplicates = list(await conn.execute(text(q_duplicates)))
        
        if not duplicates:
            print("No duplicate records found by call_id. Database is clean.")
            return
            
        print(f"Found {len(duplicates)} unique call_id groups with duplicates.")
        print("-" * 80)
        
        total_rows_to_delete = 0
        total_criteria_to_delete = 0
        affected_jobs = set()
        affected_runs = set()
        delete_ids = []
        
        for idx, dup in enumerate(duplicates):
            call_id, count = dup
            print(f"\nGroup #{idx+1}: call_id='{call_id}' (Occurs {count} times)")
            
            # Fetch all rows for this call_id, ordered by status=completed first, then mass_analysis_id DESC
            q_rows = """
            SELECT mass_analysis_id, job_id, run_id, prompt_id, evaluacion_global, created_at, status
            FROM bm_mass_evaluation_results
            WHERE call_id = :call_id
            ORDER BY 
                CASE WHEN status = 'completed' THEN 0 ELSE 1 END,
                mass_analysis_id DESC;
            """
            rows = list(await conn.execute(text(q_rows), {"call_id": call_id}))
            
            # The first row is the one to keep
            keep_row = rows[0]
            keep_id, keep_job, keep_run, keep_prompt, keep_eval, keep_created, keep_status = keep_row
            print(f"  [KEEP]   ID: {keep_id:<5} | Job: {keep_job:<4} | Run: {keep_run:<4} | Prompt: {keep_prompt:<4} | Score: {str(keep_eval):<5} | Status: {keep_status:<10} | Created: {keep_created}")
            
            # The rest are marked for deletion
            for del_row in rows[1:]:
                del_id, del_job, del_run, del_prompt, del_eval, del_created, del_status = del_row
                
                # Count child criteria results
                q_child_count = """
                SELECT COUNT(*) FROM bm_mass_evaluation_criterion_results
                WHERE mass_analysis_id = :mass_analysis_id;
                """
                child_count = (await conn.execute(text(q_child_count), {"mass_analysis_id": del_id})).scalar()
                
                print(f"  [DELETE] ID: {del_id:<5} | Job: {del_job:<4} | Run: {del_run:<4} | Prompt: {del_prompt:<4} | Score: {str(del_eval):<5} | Status: {del_status:<10} | Created: {del_created} | Child Criteria: {child_count}")
                
                delete_ids.append(del_id)
                total_rows_to_delete += 1
                total_criteria_to_delete += child_count
                affected_jobs.add(del_job)
                affected_runs.add(del_run)
                
        print("\n" + "=" * 80)
        print("   SUMMARY OF PROPOSED CLEANUP (BY CALL_ID)")
        print("=" * 80)
        print(f"Total parent rows to delete (bm_mass_evaluation_results):           {total_rows_to_delete}")
        print(f"Total child criteria rows to delete (bm_mass_evaluation_criterion_results): {total_criteria_to_delete}")
        print(f"Number of affected Jobs:                                             {len(affected_jobs)} (IDs: {sorted(list(affected_jobs))})")
        print(f"Number of affected Runs:                                             {len(affected_runs)} (IDs: {sorted(list(affected_runs))})")
        print("-" * 80)
        
        # Output exact SQL to be executed
        print("\nEXACT SQL FOR MANUAL MIGRATION AND CLEANUP:")
        print("```sql")
        print("-- 1. Delete duplicate results (keeping the best/most recent for each call_id)")
        if delete_ids:
            ids_str = ", ".join(map(str, delete_ids))
            print(f"DELETE FROM bm_mass_evaluation_results WHERE mass_analysis_id IN ({ids_str});")
        else:
            print("-- No duplicate parent rows to delete.")
        print("")
        print("-- 2. Drop the old call + prompt unique constraint if it exists")
        print("ALTER TABLE public.bm_mass_evaluation_results")
        print("    DROP CONSTRAINT IF EXISTS uq_mass_eval_call_prompt;")
        print("")
        print("-- 3. Apply unique constraint on call_id")
        print("ALTER TABLE public.bm_mass_evaluation_results")
        print("    ADD CONSTRAINT uq_mass_eval_call_id UNIQUE (call_id);")
        print("```")

if __name__ == "__main__":
    asyncio.run(run_diagnostic())
