import asyncio
import logging
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
import json
import pytest

from app.main import app
from app.db import get_engine, SessionLocal, enforce_destructive_safety
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.prompts import Prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_cleanup_and_delete_criteria():
    enforce_destructive_safety(is_test=True)
    engine = get_engine()
    
    # 1. Setup test data
    async with SessionLocal() as db:
        # Create a dummy prompt
        prompt = Prompt(prompt_name="Test Prompt", prompt_type="text")
        db.add(prompt)
        await db.flush()
        
        # Create a criterion normal
        crit_normal = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="normal_crit", is_active=True)
        db.add(crit_normal)
        
        # Create a criterion with relations (used in mass eval)
        crit_used = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="used_crit", is_active=True)
        db.add(crit_used)
        
        # Create a tipo_llamada required criterion
        crit_tipo = PromptCriterion(prompt_id=prompt.prompt_id, criterion_key="tipo_llamada", is_required=True, is_active=True)
        db.add(crit_tipo)
        
        await db.flush()
        
        # Add a job, run, result using the prompt
        job = MassEvaluationJob(job_name="Test Job", prompt_id=prompt.prompt_id)
        db.add(job)
        await db.flush()
        
        run = MassEvaluationRun(job_id=job.job_id, trigger_type="manual")
        db.add(run)
        await db.flush()
        
        result = MassEvaluationResult(
            run_id=run.run_id, 
            job_id=job.job_id, 
            call_id="call-123", 
            prompt_id=prompt.prompt_id,
            prompt_snapshot="{}",
            status="completed"
        )
        db.add(result)
        await db.commit()
        
        prompt_id = prompt.prompt_id
        normal_crit_id = crit_normal.criterion_id
        used_crit_id = crit_used.criterion_id
        tipo_crit_id = crit_tipo.criterion_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # TEST 1: Delete normal criterion (should be hard deleted)
        res = await client.request(
            "DELETE",
            f"/bm/prompt-criteria/{normal_crit_id}",
            json={"performed_by_email": "test@doobot.ai"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["action"] == "deleted"

        # TEST 2: Delete used criterion (should be soft deleted)
        res = await client.request(
            "DELETE",
            f"/bm/prompt-criteria/{used_crit_id}",
            json={"performed_by_email": "test@doobot.ai"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["action"] == "soft_deleted"
        
        # Verify in DB
        async with SessionLocal() as db:
            c = await db.execute(select(PromptCriterion).where(PromptCriterion.criterion_id == used_crit_id))
            used_c = c.scalars().first()
            assert used_c is not None
            assert used_c.is_active is False
            assert used_c.deleted_at is not None

        # TEST 3: Delete tipo_llamada required (should block)
        res = await client.request(
            "DELETE",
            f"/bm/prompt-criteria/{tipo_crit_id}",
            json={"performed_by_email": "test@doobot.ai"}
        )
        assert res.status_code == 400
        data = res.json()
        assert "tipo_llamada porque es necesario" in data["detail"]

        # TEST 4: Cleanup Mass Evaluations Dry Run
        res = await client.post(
            "/bm/admin/cleanup-mass-evaluations",
            json={"mode": "dry_run", "performed_by_email": "test@doobot.ai"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["results_count"] >= 1
        
        # Verify nothing deleted
        async with SessionLocal() as db:
            r = await db.execute(select(MassEvaluationResult))
            assert len(r.scalars().all()) >= 1

        # TEST 5: Cleanup Mass Evaluations Execute
        res = await client.post(
            "/bm/admin/cleanup-mass-evaluations",
            json={"mode": "execute", "performed_by_email": "test@doobot.ai"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["deleted_results"] >= 1
        
        # Verify all deleted
        async with SessionLocal() as db:
            r = await db.execute(select(MassEvaluationResult))
            assert len(r.scalars().all()) == 0
            
            r = await db.execute(select(MassEvaluationRun))
            assert len(r.scalars().all()) == 0
            
            r = await db.execute(select(MassEvaluationJob))
            assert len(r.scalars().all()) == 0

    print("All tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_cleanup_and_delete_criteria())
