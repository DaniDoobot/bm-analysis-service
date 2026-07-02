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
import logging
from datetime import datetime, timezone
from sqlalchemy import select, delete

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine, SessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.services import Service
from app.models.typologies import Typology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.schemas.mass_evaluations import MassEvaluationJobCreate
from app.services.mass_evaluation_service import MassEvaluationService

class pytest:
    class raises:
        def __init__(self, expected_exception, match=None):
            self.expected_exception = expected_exception
            self.match = match
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                raise AssertionError(f"Expected {self.expected_exception} but no exception was raised.")
            if not issubclass(exc_type, self.expected_exception):
                return False
            if self.match and self.match not in str(exc_val):
                raise AssertionError(f"Exception message '{str(exc_val)}' did not match pattern '{self.match}'")
            return True

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_manual_job_selection_logic():
    print("Cleaning old test database file...")
    try:
        if os.path.exists("mass_eval_test.db"):
            os.remove("mass_eval_test.db")
    except Exception as e:
        print("Failed to remove old test DB file:", e)

    engine = get_engine()
    from app.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # 1. Setup Mock Services
    import app.services.mass_evaluation_service as mes
    
    class MockHubSpot:
        async def get_call(self, call_id: str):
            if call_id == "non_existent":
                raise Exception("HubSpot Call Not Found 404")
            return {
                "call_id": call_id,
                "recording_url": f"http://example.com/audio_{call_id}.mp3",
                "hubspot_owner_id": "owner_123",
                "call_timestamp": "2026-05-22T10:00:00Z",
                "call_duration": 120000, # 120 seconds in ms
                "call_direction": "INBOUND",
                "status": "COMPLETED"
            }
            
        async def search_calls_for_mass_evaluation(self, filters):
            return []
            
    mes.HubSpotService = MockHubSpot

    class MockTwilio:
        async def download_audio(self, url):
            return b"mock_audio_bytes"
    mes.TwilioService = MockTwilio

    async def mock_analyze(*args, **kwargs):
        import json
        return json.dumps({
            "tipo_llamada": "test_typ",
            "score_val": 8,
            "score_feed": "Good audio"
        })
    mes.analyze_audio_bytes = mock_analyze

    async with SessionLocal() as db:
        # Cleanup any previous matching jobs/data
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.in_(["manual_1", "manual_2"])))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_name.like("Test Manual%")))
        await db.commit()

        # Setup standard Prompt, Service, Typology
        s = Service(service_key="manual_srv", service_name="Manual Service")
        db.add(s)
        await db.flush()

        t = Typology(service_id=s.service_id, typology_key="test_typ", typology_name="Test Typology", is_active=True)
        db.add(t)
        await db.flush()

        prompt = Prompt(prompt_name="Manual Prompt", prompt_type="audio", is_active=True, service_id=s.service_id)
        db.add(prompt)
        await db.flush()

        v = PromptVersion(prompt_id=prompt.prompt_id, prompt="Test Snapshot", is_current=True)
        db.add(v)
        await db.flush()

        c1 = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="score_val", criterion_type="score_1_10", output_key="score_val", feed_key="score_feed")
        db.add(c1)
        await db.flush()

        assoc = PromptCriterionTypology(criterion_id=c1.criterion_id, typology_id=t.typology_id)
        db.add(assoc)
        await db.commit()

        print("\n--- TEST 1: Creation Normalization & Deduplication ---")
        # Empty call_ids should raise ValueError
        with pytest.raises(ValueError, match="Debe proporcionar al menos un ID de llamada"):
            payload = MassEvaluationJobCreate(
                job_name="Test Manual 1",
                prompt_id=prompt.prompt_id,
                selection_mode="manual_call_ids",
                call_ids=[]
            )
            await MassEvaluationService.create_job(db, payload)

        # > 200 call_ids should raise ValueError
        with pytest.raises(ValueError, match="El máximo permitido es de 200 IDs"):
            payload = MassEvaluationJobCreate(
                job_name="Test Manual 2",
                prompt_id=prompt.prompt_id,
                selection_mode="manual_call_ids",
                call_ids=[f"id_{i}" for i in range(201)]
            )
            await MassEvaluationService.create_job(db, payload)

        # Duplicate inputs should be cleaned and trimmed
        payload_dup = MassEvaluationJobCreate(
            job_name="Test Manual Job Duplicates",
            prompt_id=prompt.prompt_id,
            selection_mode="manual_call_ids",
            call_ids=[" manual_1 ", "manual_2", "manual_1", "  manual_2  "]
        )
        job_dup = await MassEvaluationService.create_job(db, payload_dup)
        assert job_dup.call_ids == ["manual_1", "manual_2"], f"Expected deduplicated call_ids, got {job_dup.call_ids}"
        print("[OK] Deduplication and normalization passed.")

        print("\n--- TEST 2: Dry-run and HubSpot lookup ---")
        # Create job with 1 existing ID and 1 non-existent ID
        payload_dry = MassEvaluationJobCreate(
            job_name="Test Manual Dry Run",
            prompt_id=prompt.prompt_id,
            selection_mode="manual_call_ids",
            call_ids=["manual_1", "non_existent"]
        )
        job_dry = await MassEvaluationService.create_job(db, payload_dry)
        
        dry_res = await MassEvaluationService.dry_run_job(db, job_dry.job_id)
        assert dry_res["calls_found"] == 1
        assert dry_res["found_call_ids"] == ["manual_1"]
        assert dry_res["not_found_call_ids"] == ["non_existent"]
        assert dry_res["duplicate_input_call_ids"] == []
        assert dry_res["normalized_call_ids"] == ["manual_1", "non_existent"]
        print("[OK] Dry-run call classification verified.")

        print("\n--- TEST 3: Background Execution with Not Found IDs ---")
        # Launch run
        run = await MassEvaluationService.run_job(db, job_dry.job_id)
        assert run.status == "running"
        
        # Wait a moment for background execution loop to complete
        for _ in range(20):
            await db.refresh(run)
            if run.status in ["completed", "completed_with_errors", "failed"]:
                break
            await asyncio.sleep(0.5)

        assert run.status == "completed", f"Expected completed status, got {run.status}"
        assert run.calls_analyzed == 1
        assert run.calls_skipped == 0
        assert run.calls_failed == 0
        
        # Verify run_summary contains not_found_call_ids
        summary = run.run_summary
        assert summary is not None
        assert summary["analyzed"] == 1
        assert summary["total"] == 1 # Only 1 call was selected since "non_existent" was omitted
        assert summary["not_found_call_ids"] == ["non_existent"]
        print("[OK] Real execution with partial 404 safety verified.")

        # Cleanup
        print("\nCleaning up test manual data...")
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.job_id.in_([job_dup.job_id, job_dry.job_id])))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id.in_([job_dup.job_id, job_dry.job_id])))
        await db.execute(delete(PromptVersion).where(PromptVersion.prompt_id == prompt.prompt_id))
        await db.execute(delete(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id == c1.criterion_id))
        await db.execute(delete(PromptCriterion).where(PromptCriterion.prompt_id == prompt.prompt_id))
        await db.execute(delete(Prompt).where(Prompt.prompt_id == prompt.prompt_id))
        await db.execute(delete(Typology).where(Typology.typology_id == t.typology_id))
        await db.execute(delete(Service).where(Service.service_id == s.service_id))
        await db.commit()
        print("=== TODAS LAS PRUEBAS DE JOB MANUAL HAN PASADO CON EXITO ===")

if __name__ == "__main__":
    asyncio.run(test_manual_job_selection_logic())
