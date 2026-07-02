"""Verification test suite for mass evaluation result upsert/replace logic."""
import os
import sys

# Override DATABASE_URL to use a test SQLite database before imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_eval_test.db"

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from sqlalchemy import BigInteger

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine, Base
from app.services.mass_evaluation_service import MassEvaluationService
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

async def test_upsert_logic():
    print("=== INICIANDO PRUEBAS DE UPSERT DE RESULTADOS MASIVOS ===")
    
    # Delete database file to clean the schema cache
    if os.path.exists("mass_eval_test.db"):
        print("Cleaning old test database file...")
        try:
            os.remove("mass_eval_test.db")
        except Exception as e:
            print("Failed to remove old test DB file:", e)
            
    # 1. Initialize DB to ensure columns and unique constraints are created
    # Remove uq_mass_eval_call_prompt constraint from metadata for SQLite testing of duplicates
    constraints_to_remove = [
        c for c in MassEvaluationResult.__table__.constraints
        if getattr(c, "name", None) == "uq_mass_eval_call_prompt"
    ]
    for c in constraints_to_remove:
        MassEvaluationResult.__table__.constraints.remove(c)

    engine = get_engine()
    print("Initializing database schema on test SQLite database (without unique constraint to allow duplicate testing)...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[OK] Schema initialization completed.")

    async with AsyncSession(engine, expire_on_commit=False) as db:
        # Create dummy job & run 1
        job1 = MassEvaluationJob(
            job_name="Test Job 1",
            execution_source="on_demand",
            prompt_id=999
        )
        db.add(job1)
        await db.flush()

        run1 = MassEvaluationRun(
            job_id=job1.job_id,
            trigger_type="manual",
            status="completed",
            execution_source="on_demand"
        )
        db.add(run1)
        await db.flush()

        # Clean up any existing test data for call_id='call_test_upsert'
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_test_upsert"))
        await db.commit()

        print("\nTest 1: First evaluation (insert)...")
        res1 = await MassEvaluationService._upsert_mass_evaluation_result(
            db=db,
            run_id=run1.run_id,
            job_id=job1.job_id,
            execution_source="on_demand",
            call_id="call_test_upsert",
            prompt_id=999,
            defaults={
                "hs_object_id": "123",
                "recording_url": "http://test.url/1",
                "hubspot_owner_id": "owner_1",
                "agent_name": "Agent 1",
                "call_timestamp": datetime.now(timezone.utc),
                "call_duration_seconds": 120,
                "direction": "inbound",
                "prompt_snapshot": "Test Prompt Snapshot 1",
                "status": "completed",
                "result_json": {"score": 8.0},
                "items_json": [{"criterion_key": "empatia", "name": "Empatía", "type": "number", "numeric_value": 8.0}],
                "evaluacion_global": Decimal("8.00"),
                "service_id": 1,
                "service_key": "front"
            }
        )
        
        # Add a criterion result
        crit1 = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id,
            run_id=run1.run_id,
            job_id=job1.job_id,
            execution_source="on_demand",
            call_id="call_test_upsert",
            prompt_id=999,
            criterion_key="empatia",
            criterion_name="Empatía",
            criterion_type="number",
            numeric_value=Decimal("8.0")
        )
        db.add(crit1)
        await db.commit()

        # Verify insertion
        stmt_check = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_test_upsert")
        res_check = await db.execute(stmt_check)
        results = res_check.scalars().all()
        assert len(results) == 1, f"Expected 1 result, got {len(results)}"
        res_row = results[0]
        assert res_row.status == "completed"
        assert res_row.evaluacion_global == Decimal("8.00")
        assert res_row.source_job_id == job1.job_id
        assert res_row.source_run_id == run1.run_id
        assert res_row.job_id == job1.job_id
        assert res_row.run_id == run1.run_id
        assert res_row.last_evaluated_at is not None
        
        original_mass_analysis_id = res_row.mass_analysis_id
        print(f"[OK] First evaluation inserted successfully. mass_analysis_id={original_mass_analysis_id}")

        # Create dummy job & run 2
        job2 = MassEvaluationJob(
            job_name="Test Job 2",
            execution_source="on_demand",
            prompt_id=999
        )
        db.add(job2)
        await db.flush()

        run2 = MassEvaluationRun(
            job_id=job2.job_id,
            trigger_type="manual",
            status="completed",
            execution_source="on_demand"
        )
        db.add(run2)
        await db.flush()

        print("\nTest 2: Second evaluation (upsert / overwrite)...")
        res2 = await MassEvaluationService._upsert_mass_evaluation_result(
            db=db,
            run_id=run2.run_id,
            job_id=job2.job_id,
            execution_source="on_demand",
            call_id="call_test_upsert",
            prompt_id=999,
            defaults={
                "hs_object_id": "123",
                "recording_url": "http://test.url/2",
                "hubspot_owner_id": "owner_1",
                "agent_name": "Agent 1",
                "call_timestamp": datetime.now(timezone.utc),
                "call_duration_seconds": 150,
                "direction": "inbound",
                "prompt_snapshot": "Test Prompt Snapshot 2",
                "status": "completed",
                "result_json": {"score": 9.5},
                "items_json": [
                    {"criterion_key": "empatia", "name": "Empatía", "type": "number", "numeric_value": 9.0},
                    {"criterion_key": "claridad", "name": "Claridad", "type": "number", "numeric_value": 10.0}
                ],
                "evaluacion_global": Decimal("9.50"),
                "service_id": 1,
                "service_key": "front"
            }
        )

        # Add new criterion results
        crit2_1 = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id,
            run_id=run2.run_id,
            job_id=job2.job_id,
            execution_source="on_demand",
            call_id="call_test_upsert",
            prompt_id=999,
            criterion_key="empatia",
            criterion_name="Empatía",
            criterion_type="number",
            numeric_value=Decimal("9.0")
        )
        crit2_2 = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id,
            run_id=run2.run_id,
            job_id=job2.job_id,
            execution_source="on_demand",
            call_id="call_test_upsert",
            prompt_id=999,
            criterion_key="claridad",
            criterion_name="Claridad",
            criterion_type="number",
            numeric_value=Decimal("10.0")
        )
        db.add(crit2_1)
        db.add(crit2_2)
        await db.commit()

        # Verify upsert / overwrite
        stmt_check_2 = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_test_upsert")
        res_check_2 = await db.execute(stmt_check_2)
        results_2 = res_check_2.scalars().all()
        
        # 1. Row count remains 1
        assert len(results_2) == 1, f"Expected exactly 1 result row after upsert, got {len(results_2)}"
        
        res_row_2 = results_2[0]
        # 2. mass_analysis_id remains the same
        assert res_row_2.mass_analysis_id == original_mass_analysis_id, f"mass_analysis_id changed! {res_row_2.mass_analysis_id} vs {original_mass_analysis_id}"
        
        # 3. Fields updated
        assert res_row_2.evaluacion_global == Decimal("9.50"), f"Expected global score 9.50, got {res_row_2.evaluacion_global}"
        assert res_row_2.recording_url == "http://test.url/2"
        
        # 4. Audit columns populated correctly
        assert res_row_2.source_job_id == job1.job_id, f"source_job_id should remain Job 1 ({job1.job_id}), got {res_row_2.source_job_id}"
        assert res_row_2.source_run_id == run1.run_id, f"source_run_id should remain Run 1 ({run1.run_id}), got {res_row_2.source_run_id}"
        assert res_row_2.job_id == job2.job_id, f"job_id should update to Job 2 ({job2.job_id}), got {res_row_2.job_id}"
        assert res_row_2.run_id == run2.run_id, f"run_id should update to Run 2 ({run2.run_id}), got {res_row_2.run_id}"
        assert res_row_2.updated_at is not None
        assert res_row_2.last_evaluated_at is not None
        assert res_row_2.updated_at > res_row_2.created_at

        # 5. Criteria records replaced correctly
        stmt_crit = select(MassEvaluationCriterionResult).where(
            MassEvaluationCriterionResult.mass_analysis_id == original_mass_analysis_id
        ).order_by(MassEvaluationCriterionResult.criterion_key.asc())
        res_crit = await db.execute(stmt_crit)
        criteria = res_crit.scalars().all()
        
        assert len(criteria) == 2, f"Expected 2 criteria rows, got {len(criteria)}"
        assert criteria[0].criterion_key == "claridad"
        assert criteria[0].numeric_value == Decimal("10.0")
        assert criteria[1].criterion_key == "empatia"
        assert criteria[1].numeric_value == Decimal("9.0")
        print("[OK] Upsert and replacement logic verified successfully.")

        # -------------------------------------------------------------
        # TEST 3: Overwrite protection (completed vs failed/skipped)
        # -------------------------------------------------------------
        print("\nTest 3: Protection of completed status against failed/skipped re-evaluations...")
        
        # Create dummy job & run 3
        job3 = MassEvaluationJob(
            job_name="Test Job 3 (Failed)",
            execution_source="on_demand",
            prompt_id=999
        )
        db.add(job3)
        await db.flush()

        run3 = MassEvaluationRun(
            job_id=job3.job_id,
            trigger_type="manual",
            status="failed",
            execution_source="on_demand"
        )
        db.add(run3)
        await db.flush()

        # Perform failed upsert on the same call_id and prompt_id
        res3 = await MassEvaluationService._upsert_mass_evaluation_result(
            db=db,
            run_id=run3.run_id,
            job_id=job3.job_id,
            execution_source="on_demand",
            call_id="call_test_upsert",
            prompt_id=999,
            defaults={
                "hs_object_id": "123",
                "recording_url": "http://test.url/2",
                "hubspot_owner_id": "owner_1",
                "agent_name": "Agent 1",
                "call_timestamp": datetime.now(timezone.utc),
                "call_duration_seconds": 150,
                "direction": "inbound",
                "prompt_snapshot": "Test Prompt Snapshot 2",
                "status": "failed",
                "result_json": None,
                "items_json": None,
                "evaluacion_global": None,
                "service_id": 1,
                "service_key": "front",
                "error_message": "Some API Error"
            }
        )
        await db.commit()

        # Verify that the parent status remains 'completed' and global score is still 9.50
        stmt_check_3 = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_test_upsert")
        res_check_3 = await db.execute(stmt_check_3)
        results_3 = res_check_3.scalars().all()
        assert len(results_3) == 1
        res_row_3 = results_3[0]
        
        assert res_row_3.status == "completed", f"Status was changed to {res_row_3.status}! Expected: completed"
        assert res_row_3.evaluacion_global == Decimal("9.50"), f"Score was cleared to {res_row_3.evaluacion_global}! Expected: 9.50"
        
        # Verify that criteria results were NOT deleted
        stmt_crit_3 = select(MassEvaluationCriterionResult).where(
            MassEvaluationCriterionResult.mass_analysis_id == original_mass_analysis_id
        )
        res_crit_3 = await db.execute(stmt_crit_3)
        criteria_3 = res_crit_3.scalars().all()
        assert len(criteria_3) == 2, f"Criteria rows were deleted! Got {len(criteria_3)}, Expected: 2"
        
        print("[OK] Protection logic verified successfully: completed status and criteria results were preserved.")

        # -------------------------------------------------------------
        # TEST 4: Upsert behavior with pre-existing duplicates
        # -------------------------------------------------------------
        print("\nTest 4: Upsert behavior with pre-existing duplicates...")

        # 1. Create two pre-existing results for same call_id and prompt_id
        # Result 1: Older row (mass_analysis_id will be lower)
        dup_job_old = MassEvaluationJob(
            job_name="Dup Job Old",
            execution_source="on_demand",
            prompt_id=888
        )
        db.add(dup_job_old)
        await db.flush()

        dup_run_old = MassEvaluationRun(
            job_id=dup_job_old.job_id,
            trigger_type="manual",
            status="completed",
            execution_source="on_demand"
        )
        db.add(dup_run_old)
        await db.flush()

        dup_res_old = MassEvaluationResult(
            run_id=dup_run_old.run_id,
            job_id=dup_job_old.job_id,
            execution_source="on_demand",
            call_id="call_dup_preexisting",
            prompt_id=888,
            status="completed",
            evaluacion_global=Decimal("7.00"),
            recording_url="http://test.url/old",
            hs_object_id="dup_1",
            hubspot_owner_id="owner_1",
            call_timestamp=datetime.now(timezone.utc),
            call_duration_seconds=100,
            direction="inbound",
            service_id=1,
            service_key="front",
            prompt_snapshot="Test Snapshot Old"
        )
        db.add(dup_res_old)
        await db.flush()

        # Result 2: Newer row (mass_analysis_id will be higher)
        dup_job_new = MassEvaluationJob(
            job_name="Dup Job New",
            execution_source="on_demand",
            prompt_id=888
        )
        db.add(dup_job_new)
        await db.flush()

        dup_run_new = MassEvaluationRun(
            job_id=dup_job_new.job_id,
            trigger_type="manual",
            status="completed",
            execution_source="on_demand"
        )
        db.add(dup_run_new)
        await db.flush()

        dup_res_new = MassEvaluationResult(
            run_id=dup_run_new.run_id,
            job_id=dup_job_new.job_id,
            execution_source="on_demand",
            call_id="call_dup_preexisting",
            prompt_id=888,
            status="completed",
            evaluacion_global=Decimal("8.00"),
            recording_url="http://test.url/new",
            hs_object_id="dup_1",
            hubspot_owner_id="owner_1",
            call_timestamp=datetime.now(timezone.utc),
            call_duration_seconds=110,
            direction="inbound",
            service_id=1,
            service_key="front",
            prompt_snapshot="Test Snapshot New"
        )
        db.add(dup_res_new)
        await db.flush()

        # Add criteria for the newer row
        crit_dup_new = MassEvaluationCriterionResult(
            mass_analysis_id=dup_res_new.mass_analysis_id,
            run_id=dup_run_new.run_id,
            job_id=dup_job_new.job_id,
            execution_source="on_demand",
            call_id="call_dup_preexisting",
            prompt_id=888,
            criterion_key="empatia",
            criterion_name="Empatía",
            criterion_type="number",
            numeric_value=Decimal("8.0")
        )
        db.add(crit_dup_new)
        await db.commit()

        # Confirm we have exactly 2 rows for this call_id
        stmt_dup_check = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_dup_preexisting").order_by(MassEvaluationResult.mass_analysis_id.asc())
        res_dup_check = await db.execute(stmt_dup_check)
        dup_results = res_dup_check.scalars().all()
        assert len(dup_results) == 2, f"Expected 2 duplicate rows, got {len(dup_results)}"
        old_id = dup_results[0].mass_analysis_id
        new_id = dup_results[1].mass_analysis_id
        assert old_id < new_id

        # 2. Perform a new completed upsert on the same call_id and prompt_id (888)
        dup_job_upsert = MassEvaluationJob(
            job_name="Dup Job Upsert",
            execution_source="on_demand",
            prompt_id=888
        )
        db.add(dup_job_upsert)
        await db.flush()

        dup_run_upsert = MassEvaluationRun(
            job_id=dup_job_upsert.job_id,
            trigger_type="manual",
            status="completed",
            execution_source="on_demand"
        )
        db.add(dup_run_upsert)
        await db.flush()

        res_upsert = await MassEvaluationService._upsert_mass_evaluation_result(
            db=db,
            run_id=dup_run_upsert.run_id,
            job_id=dup_job_upsert.job_id,
            execution_source="on_demand",
            call_id="call_dup_preexisting",
            prompt_id=888,
            defaults={
                "hs_object_id": "dup_1",
                "recording_url": "http://test.url/upserted",
                "hubspot_owner_id": "owner_1",
                "agent_name": "Agent 1",
                "call_timestamp": datetime.now(timezone.utc),
                "call_duration_seconds": 120,
                "direction": "inbound",
                "prompt_snapshot": "Test Prompt Snapshot Upsert",
                "status": "completed",
                "result_json": {"score": 9.0},
                "items_json": [{"criterion_key": "empatia", "name": "Empatía", "type": "number", "numeric_value": 9.0}],
                "evaluacion_global": Decimal("9.00"),
                "service_id": 1,
                "service_key": "front"
            }
        )
        
        # Add new criteria
        crit_upsert = MassEvaluationCriterionResult(
            mass_analysis_id=res_upsert.mass_analysis_id,
            run_id=dup_run_upsert.run_id,
            job_id=dup_job_upsert.job_id,
            execution_source="on_demand",
            call_id="call_dup_preexisting",
            prompt_id=888,
            criterion_key="empatia",
            criterion_name="Empatía",
            criterion_type="number",
            numeric_value=Decimal("9.0")
        )
        db.add(crit_upsert)
        await db.commit()

        # 3. Verify
        # - Total row count remains exactly 2 (did not create a 3rd row)
        stmt_final_check = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_dup_preexisting").order_by(MassEvaluationResult.mass_analysis_id.asc())
        res_final_check = await db.execute(stmt_final_check)
        final_results = res_final_check.scalars().all()
        assert len(final_results) == 2, f"Expected exactly 2 duplicate rows, got {len(final_results)}"
        
        # - The newer row was updated to 9.00
        assert final_results[1].mass_analysis_id == new_id, "The updated row ID changed!"
        assert final_results[1].evaluacion_global == Decimal("9.00"), f"Expected 9.00, got {final_results[1].evaluacion_global}"
        assert final_results[1].recording_url == "http://test.url/upserted"

        # - The older row (old_id) is completely untouched (score remains 7.00)
        assert final_results[0].mass_analysis_id == old_id
        assert final_results[0].evaluacion_global == Decimal("7.00"), f"Older row was modified! Score: {final_results[0].evaluacion_global}"
        assert final_results[0].recording_url == "http://test.url/old"

        print("[OK] Duplicate safety logic verified: updated the most recent duplicate while leaving older ones untouched.")

        # Cleanup
        print("\nCleaning up test data...")
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.in_(["call_test_upsert", "call_dup_preexisting"])))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id.in_([job1.job_id, job2.job_id, job3.job_id, dup_job_old.job_id, dup_job_new.job_id, dup_job_upsert.job_id])))
        await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.run_id.in_([run1.run_id, run2.run_id, run3.run_id, dup_run_old.run_id, dup_run_new.run_id, dup_run_upsert.run_id])))
        await db.commit()
        print("=== TODAS LAS PRUEBAS DE UPSERT HAN PASADO CON EXITO ===")

if __name__ == "__main__":
    asyncio.run(test_upsert_logic())
