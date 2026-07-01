import asyncio
import os
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine

async def dry_run_cleanup():
    engine = get_engine()
    async with engine.connect() as conn:
        print("===============================================================================")
        print("   DRY-RUN: MASS EVALUATION RESULTS DEDUPLICATION & INTEGRITY AUDIT")
        print("===============================================================================")
        
        # 1. Query all groups of duplicates
        q_duplicates = """
        SELECT call_id, prompt_id, COUNT(*) as occurrence_count
        FROM bm_mass_evaluation_results
        GROUP BY call_id, prompt_id
        HAVING COUNT(*) > 1
        ORDER BY occurrence_count DESC;
        """
        duplicates = list(await conn.execute(text(q_duplicates)))
        
        if not duplicates:
            print("No duplicate records found by (call_id + prompt_id). Database is clean.")
            return
            
        print(f"Found {len(duplicates)} unique (call_id + prompt_id) groups with duplicates.")
        print("-" * 80)
        
        total_rows_to_delete = 0
        total_criteria_to_delete = 0
        affected_jobs = set()
        affected_runs = set()
        
        delete_ids = []
        
        for idx, dup in enumerate(duplicates):
            call_id, prompt_id, count = dup
            print(f"\nGroup #{idx+1}: call_id='{call_id}' | prompt_id={prompt_id} (Occurs {count} times)")
            
            # Fetch all rows in this group, ordered by mass_analysis_id DESC (most recent first)
            q_rows = """
            SELECT mass_analysis_id, job_id, run_id, evaluacion_global, created_at, status
            FROM bm_mass_evaluation_results
            WHERE call_id = :call_id AND prompt_id = :prompt_id
            ORDER BY mass_analysis_id DESC;
            """
            rows = list(await conn.execute(text(q_rows), {"call_id": call_id, "prompt_id": prompt_id}))
            
            # The first one is kept
            keep_row = rows[0]
            keep_id, keep_job, keep_run, keep_eval, keep_created, keep_status = keep_row
            print(f"  [KEEP]  ID: {keep_id:<5} | Job: {keep_job:<4} | Run: {keep_run:<4} | Score: {str(keep_eval):<5} | Status: {keep_status:<10} | Created: {keep_created}")
            
            # The rest are deleted
            for del_row in rows[1:]:
                del_id, del_job, del_run, del_eval, del_created, del_status = del_row
                
                # Count child criteria results
                q_child_count = """
                SELECT COUNT(*) FROM bm_mass_evaluation_criterion_results
                WHERE mass_analysis_id = :mass_analysis_id;
                """
                child_count = (await conn.execute(text(q_child_count), {"mass_analysis_id": del_id})).scalar()
                
                print(f"  [DELETE] ID: {del_id:<5} | Job: {del_job:<4} | Run: {del_run:<4} | Score: {str(del_eval):<5} | Status: {del_status:<10} | Created: {del_created} | Child Criteria: {child_count}")
                
                delete_ids.append(del_id)
                total_rows_to_delete += 1
                total_criteria_to_delete += child_count
                affected_jobs.add(del_job)
                affected_runs.add(del_run)
        
        print("\n" + "=" * 80)
        print("   SUMMARY OF PROPOSED CLEANUP")
        print("=" * 80)
        print(f"Total parent rows to delete (bm_mass_evaluation_results):           {total_rows_to_delete}")
        print(f"Total child criteria rows to delete (bm_mass_evaluation_criterion_results): {total_criteria_to_delete}")
        print(f"Number of affected Jobs:                                             {len(affected_jobs)} (IDs: {sorted(list(affected_jobs))})")
        print(f"Number of affected Runs:                                             {len(affected_runs)} (IDs: {sorted(list(affected_runs))})")
        print("-" * 80)
        
        # Output exact SQL to be executed
        print("\nEXACT SQL FOR MANUAL MIGRATION AND CLEANUP:")
        print("```sql")
        print("-- 1. Add audit columns if they do not exist")
        print("ALTER TABLE bm_mass_evaluation_results")
        print("    ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ NULL,")
        print("    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NULL,")
        print("    ADD COLUMN IF NOT EXISTS source_job_id INTEGER NULL,")
        print("    ADD COLUMN IF NOT EXISTS source_run_id INTEGER NULL;")
        print("")
        print("-- 2. Delete duplicate results (keeping the most recent for each call_id + prompt_id)")
        if delete_ids:
            # Format list of IDs
            ids_str = ", ".join(map(str, delete_ids))
            print(f"DELETE FROM bm_mass_evaluation_results WHERE mass_analysis_id IN ({ids_str});")
        else:
            print("-- No duplicate parent rows to delete.")
        print("")
        print("-- 3. Apply unique constraint (to be run manually after verifying deduplication)")
        print("ALTER TABLE bm_mass_evaluation_results")
        print("    ADD CONSTRAINT uq_mass_eval_call_prompt UNIQUE (call_id, prompt_id);")
        print("```")

if __name__ == "__main__":
    asyncio.run(dry_run_cleanup())
