import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, func

from app.db import get_engine, SessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.services import Service
from app.models.typologies import Typology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.services.mass_evaluation_service import MassEvaluationService

logging.basicConfig(level=logging.INFO)

async def test_inactive_prompt_mass_evaluation():
    engine = get_engine()
    
    async with SessionLocal() as db:
        # --- 1. Setup Data ---
        print("\n--- Setting up test data ---")
        
        # Service & Typology
        s = Service(service_key="test_srv", service_name="Test Service")
        db.add(s)
        await db.flush()
        
        t = Typology(service_id=s.service_id, typology_key="test_typ", typology_name="Test Typology", is_active=True)
        db.add(t)
        await db.flush()

        # Inactive Prompt
        prompt = Prompt(prompt_name="Test Inactive Prompt", prompt_type="audio", is_active=False, service_id=s.service_id)
        db.add(prompt)
        await db.flush()

        # Version (is_current=False just to test fallback, or True, doesn't matter)
        v = PromptVersion(prompt_id=prompt.prompt_id, prompt="Test Snapshot Text", is_current=False)
        db.add(v)
        await db.flush()
        
        # Criteria
        c1 = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="score_val", criterion_type="score_1_10", output_key="score_val", feed_key="score_feed")
        db.add(c1)
        await db.flush()
        
        assoc = PromptCriterionTypology(criterion_id=c1.criterion_id, typology_id=t.typology_id)
        db.add(assoc)
        await db.flush()
        
        # Job
        job = MassEvaluationJob(job_name="Inactive Prompt Job", prompt_id=prompt.prompt_id, max_calls=1)
        db.add(job)
        await db.flush()
        
        # Run
        run = MassEvaluationRun(job_id=job.job_id, trigger_type="manual", status="running", started_at=datetime.now(timezone.utc))
        db.add(run)
        await db.commit()
        
        job_id = job.job_id
        run_id = run.run_id

        print("Data prepared. Job ID:", job_id, "Run ID:", run_id)

    # --- 2. Mocking External Dependencies ---
    import app.services.mass_evaluation_service as mes
    
    # Mock HubSpot
    class MockHubSpot:
        async def search_calls_for_mass_evaluation(self, filters):
            return [{
                "call_id": "test_call_999",
                "recording_url": "http://example.com/audio.mp3",
                "hubspot_owner_id": "1234",
                "hs_object_id": "999",
                "call_timestamp": "2026-05-22T10:00:00Z",
                "call_duration_seconds": 60,
                "direction": "INBOUND"
            }]
    
    mes.HubSpotService = MockHubSpot
    
    # Mock Twilio
    class MockTwilio:
        async def download_audio(self, url):
            return b"fake_audio_bytes"
            
    mes.TwilioService = MockTwilio
    
    # Mock OpenAI
    async def mock_analyze(*args, **kwargs):
        import json
        return json.dumps({
            "tipo_llamada": "test_typ",
            "score_val": 9,
            "score_feed": "Mocked feedback"
        })
        
    mes.analyze_audio_bytes = mock_analyze
    
    # --- 3. Execute Background Run ---
    print("\n--- Executing background run ---")
    await MassEvaluationService._execute_background_run(job_id, run_id, {"max_calls": 1})
    
    # --- 4. Validate ---
    print("\n--- Validating ---")
    async with SessionLocal() as db:
        # Check run status
        run_obj = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == run_id))).scalars().first()
        assert run_obj.status == "completed", f"Run status should be completed, got {run_obj.status}"
        assert run_obj.calls_analyzed == 1, "Should have analyzed 1 call"
        
        # Check main result
        res_main = (await db.execute(select(MassEvaluationResult).where(MassEvaluationResult.run_id == run_id))).scalars().first()
        assert res_main is not None, "Result main row missing"
        assert res_main.status == "completed"
        assert res_main.prompt_snapshot == "Test Snapshot Text"
        
        # Check granular criteria
        crit_res = (await db.execute(select(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.mass_analysis_id == res_main.mass_analysis_id))).scalars().all()
        assert len(crit_res) == 1, f"Expected 1 criterion result, got {len(crit_res)}"
        assert crit_res[0].numeric_value == 9
        assert crit_res[0].feedback == "Mocked feedback"
        assert crit_res[0].typology_key == "test_typ"

        print("First pass OK!")
        
        # --- 5. Test Overwrite ---
        print("\n--- Testing Overwrite ---")
        run2 = MassEvaluationRun(job_id=job_id, trigger_type="manual", status="running", started_at=datetime.now(timezone.utc))
        db.add(run2)
        await db.commit()
        
        async def mock_analyze_2(*args, **kwargs):
            import json
            return json.dumps({
                "tipo_llamada": "TEST_TYP",
                "score_val": 4,
                "score_feed": "Worse feedback"
            })
            
        mes.analyze_audio_bytes = mock_analyze_2
        
        await MassEvaluationService._execute_background_run(job_id, run2.run_id, {"max_calls": 1})
        
    async with SessionLocal() as db:
        run2_obj = (await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == run2.run_id))).scalars().first()
        assert run2_obj.status == "completed"
        
        main_count = await db.execute(select(func.count()).select_from(MassEvaluationResult).where(MassEvaluationResult.job_id == job_id, MassEvaluationResult.call_id == "test_call_999"))
        assert main_count.scalar() == 1, "Should only have 1 row for job+call (overwritten)"
        
        res_main_2 = (await db.execute(select(MassEvaluationResult).where(MassEvaluationResult.run_id == run2.run_id))).scalars().first()
        crit_res_2 = (await db.execute(select(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.mass_analysis_id == res_main_2.mass_analysis_id))).scalars().all()
        assert crit_res_2[0].numeric_value == 4
        assert crit_res_2[0].feedback == "Worse feedback"
        assert crit_res_2[0].typology_key == "test_typ", "Should match case-insensitively to test_typ"
        assert crit_res_2[0].typology_name == "Test Typology", "Should propagate correct typology name"
        
        print("Overwrite OK!")

    print("\nAll background logic tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_inactive_prompt_mass_evaluation())
