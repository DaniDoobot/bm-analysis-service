import asyncio
import logging
from sqlalchemy import select, func
from app.db import get_engine, SessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.analyses import Analysis, AnalysisCriterionResult
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.services.analysis_persistence import save_analysis
from app.services.mass_evaluation_service import MassEvaluationService

logging.basicConfig(level=logging.INFO)

async def test_criterion_result_storage():
    engine = get_engine()
    
    async with SessionLocal() as db:
        # --- Setup Dummy Prompt & Criteria ---
        prompt = Prompt(prompt_name="Test Prompt Storage", prompt_type="text")
        db.add(prompt)
        await db.flush()

        v = PromptVersion(prompt_id=prompt.prompt_id, prompt="Test", is_current=True)
        db.add(v)
        
        c1 = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="score_val", criterion_type="score_1_10", output_key="score_val", feed_key="score_feed")
        c2 = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="bool_val", criterion_type="boolean", output_key="bool_val")
        c3 = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="text_val", criterion_type="text", output_key="text_val")
        db.add_all([c1, c2, c3])
        await db.flush()

        prompt_id = prompt.prompt_id
        v_id = v.id

        # --- 1. Manual Analysis Test ---
        print("\n--- Testing Manual Analysis ---")
        call_id_manual = "manual_call_1"
        
        result_json_1 = {
            "score_val": 8,
            "score_feed": "Good score",
            "bool_val": "Sí",
            "text_val": "Some text"
        }
        
        a1 = await save_analysis(
            db=db,
            analysis_type="text",
            call_metadata={"call_id": call_id_manual},
            prompt_metadata={"prompt_id": prompt_id, "prompt_version_id": v_id},
            model_metadata={"model_provider": "test"},
            result_json=result_json_1,
            payload={}
        )
        await db.commit()
        
        # Verify rows in AnalysisCriterionResult
        res1 = await db.execute(select(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == a1.analysis_id))
        rows1 = res1.scalars().all()
        assert len(rows1) == 3, f"Expected 3 criteria results, got {len(rows1)}"
        
        # Verify field mapping
        score_row = next(r for r in rows1 if r.criterion_key == "score_val")
        assert score_row.numeric_value == 8, "score not in numeric_value"
        assert score_row.feedback == "Good score", "feedback not stored"
        
        bool_row = next(r for r in rows1 if r.criterion_key == "bool_val")
        assert bool_row.boolean_value is True, "boolean not in boolean_value"
        
        text_row = next(r for r in rows1 if r.criterion_key == "text_val")
        assert text_row.text_value == "Some text", "text not in text_value"
        
        # Repeat same call
        a2 = await save_analysis(
            db=db,
            analysis_type="text",
            call_metadata={"call_id": call_id_manual},
            prompt_metadata={"prompt_id": prompt_id, "prompt_version_id": v_id},
            model_metadata={"model_provider": "test"},
            result_json=result_json_1,
            payload={}
        )
        await db.commit()
        
        assert a2.analysis_id != a1.analysis_id, "New analysis_id should be created"
        res2 = await db.execute(select(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == a2.analysis_id))
        assert len(res2.scalars().all()) == 3, "Should create 3 NEW criteria results"
        print("Manual Analysis OK")

        # --- 2. Mass Evaluation Test ---
        print("\n--- Testing Mass Evaluation ---")
        job = MassEvaluationJob(job_name="Test Job Storage", prompt_id=prompt_id)
        db.add(job)
        await db.flush()
        
        run = MassEvaluationRun(job_id=job.job_id, trigger_type="manual")
        db.add(run)
        await db.flush()
        
        call_id_mass = "mass_call_1"
        
        # Simulate mass evaluation save (run 1)
        res_mass_1 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id=call_id_mass,
            prompt_id=prompt_id,
            prompt_snapshot="{}"
        )
        # Delete old just like service
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.job_id == job.job_id, MassEvaluationResult.call_id == call_id_mass))
        db.add(res_mass_1)
        await db.flush()
        
        c_m1 = MassEvaluationCriterionResult(mass_analysis_id=res_mass_1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id, call_id=call_id_mass, criterion_key="score_val", numeric_value=5)
        db.add(c_m1)
        await db.commit()
        
        # Verify
        m1_count = await db.execute(select(func.count()).select_from(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.job_id == job.job_id, MassEvaluationCriterionResult.call_id == call_id_mass))
        assert m1_count.scalar() == 1
        
        # Simulate mass evaluation save (run 2) - SAME CALL, NEW VALUES
        run2 = MassEvaluationRun(job_id=job.job_id, trigger_type="manual")
        db.add(run2)
        await db.flush()
        
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.job_id == job.job_id, MassEvaluationResult.call_id == call_id_mass))
        
        res_mass_2 = MassEvaluationResult(
            run_id=run2.run_id,
            job_id=job.job_id,
            call_id=call_id_mass,
            prompt_id=prompt_id,
            prompt_snapshot="{}"
        )
        db.add(res_mass_2)
        await db.flush()
        
        c_m2 = MassEvaluationCriterionResult(mass_analysis_id=res_mass_2.mass_analysis_id, run_id=run2.run_id, job_id=job.job_id, call_id=call_id_mass, criterion_key="score_val", numeric_value=10)
        db.add(c_m2)
        await db.commit()
        
        # Verify overwrote previous run's result for same job+call
        m2_res = await db.execute(select(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.job_id == job.job_id, MassEvaluationCriterionResult.call_id == call_id_mass))
        m2_rows = m2_res.scalars().all()
        assert len(m2_rows) == 1, f"Expected 1 overwritten criterion result, got {len(m2_rows)}"
        assert m2_rows[0].numeric_value == 10, "Value should be updated to 10"
        
        print("Mass Evaluation OK")

    print("\nAll storage tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_criterion_result_storage())
