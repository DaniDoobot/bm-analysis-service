"""Verification test script for Agent list and Agent evolution dashboards sourced exclusively from Mass Evaluations."""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Add app to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.services.dashboard_service import get_agents_list, get_agent_evolution
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

async def test_dashboard_endpoints():
    print("=== STARTING AGENT EVOLUTION DASHBOARD VERIFICATION ===")

    try:
        engine = get_engine()
    except RuntimeError as re:
        print(f"Skipping DB verification (DATABASE_URL is not set): {re}")
        return

    async with AsyncSession(engine, expire_on_commit=False) as db:
        print("\nStep 1: Setting up clean mock data...")
        
        # Cleanup potential leftover test data
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.hubspot_owner_id == "99999995"))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_name == "Dashboard Test Job"))
        await db.commit()
        
        # 1. Create a mock job
        job = MassEvaluationJob(
            job_name="Dashboard Test Job",
            prompt_id=1,
            is_active=True,
            schedule_enabled=False,
            created_by="Tester"
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        print(f"Created dummy job_id: {job.job_id}")

        # 2. Create a mock run
        run = MassEvaluationRun(
            job_id=job.job_id,
            trigger_type="manual",
            status="completed",
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            finished_at=datetime.now(timezone.utc)
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        print(f"Created dummy run_id: {run.run_id}")

        # 3. Create mock Mass Evaluation Results for Cristina Montenegro (owner_id: 33013276)
        # We will create 2 results to test delta/trend calculations.
        res1 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_mock_1",
            hubspot_owner_id="99999995",
            agent_name="Cristina Montenegro",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=5),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=5),
            prompt_id=1,
            prompt_snapshot="Prompt snapshot dummy",
            result_json={
                "evaluacion_global": 7.0,
                "sentimiento": 6.5,
                "empatia": 8.0,
                "simpatia": 7.0,
                "claridad": 6.0,
                "adherencia_procedimiento": 5.0,
                "tipo_llamada": "cita",
                "objeciones": "Le parece muy caro el presupuesto."
            },
            items_json=[
                {"key": "evaluacion_global", "value": 7.0},
                {"key": "sentiment", "value": 6.5},
                {"key": "empatia", "value": 8.0},
                {"key": "simpatia", "value": 7.0},
                {"key": "claridad", "value": 6.0},
                {"key": "procedimiento", "value": 5.0}
            ]
        )

        res2 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_mock_2",
            hubspot_owner_id="99999995",
            agent_name="Cristina Montenegro",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
            prompt_id=1,
            prompt_snapshot="Prompt snapshot dummy",
            result_json={
                "evaluacion_global": 9.0,
                "sentimiento": 8.5,
                "empatia": 9.0,
                "simpatia": 9.5,
                "claridad": 8.0,
                "adherencia_procedimiento": 9.0,
                "tipo_llamada": "cita",
                "objeciones": None
            },
            items_json=[
                {"key": "evaluacion_global", "value": 9.0},
                {"key": "sentiment", "value": 8.5},
                {"key": "empatia", "value": 9.0},
                {"key": "simpatia", "value": 9.5},
                {"key": "claridad", "value": 8.0},
                {"key": "procedimiento", "value": 9.0}
            ]
        )

        db.add_all([res1, res2])
        await db.commit()
        print("Mock mass evaluation results inserted successfully.")

        # ── Test 1: get_agents_list ──────────────────────────────────────────
        print("\nStep 2: Testing get_agents_list()...")
        agents = await get_agents_list(db)
        
        # Verify Cristina Montenegro is in results and has correct metrics
        cristina = next((a for a in agents if a["hubspot_owner_id"] == "99999995"), None)
        assert cristina is not None, "Cristina Montenegro not found in agents list!"
        print(f"Verified Cristina Montenegro in List: {cristina}")
        assert cristina["total_analyses"] == 2, f"Expected 2 analyses, got {cristina['total_analyses']}"
        assert cristina["avg_evaluacion_global"] == 8.0, f"Expected avg evaluation 8.0, got {cristina['avg_evaluacion_global']}"
        assert cristina["last_analysis_at"] is not None, "last_analysis_at is None!"

        # ── Test 2: get_agent_evolution for Cristina Montenegro ───────────────
        print("\nStep 3: Testing get_agent_evolution() for Cristina Montenegro...")
        evo = await get_agent_evolution(db, "99999995", period="30d")
        
        print(f"Agent name resolved: {evo['agent']['agent_name']}")
        assert evo["agent"]["agent_name"] == "Cristina Montenegro", "Agent name mismatch!"
        assert evo["source"] == "mass_evaluations", "Source field must be 'mass_evaluations'"
        
        summary = evo["summary"]
        print(f"Summary metrics: {summary}")
        assert summary["total_analyses"] == 2
        assert summary["avg_evaluacion_global"] == 8.0
        assert summary["avg_sentiment"] == 7.5
        assert summary["avg_empatia"] == 8.5
        assert summary["avg_simpatia"] == 8.2
        assert summary["avg_claridad"] == 7.0
        assert summary["avg_procedimiento"] == 7.0
        assert summary["cita_rate"] == 100
        assert summary["total_objeciones"] == 1 # first call has objections, second does not

        # Trend calculations
        trend = evo["trend"]
        print(f"Trend metrics: {trend}")
        assert trend["evaluacion_global_slope"] == 2.0  # (9.0 - 7.0)
        assert trend["evaluacion_global_direction"] == "up"

        # Timeline
        timeline = evo["timeline"]
        print(f"Timeline buckets count: {len(timeline)}")
        assert len(timeline) > 0

        # Latest Analyses
        latest = evo["latest_analyses"]
        print(f"Latest analyses list length: {len(latest)}")
        assert len(latest) == 2
        assert latest[0]["evaluacion_global"] == 9.0
        assert latest[1]["evaluacion_global"] == 7.0

        # Strengths & Weaknesses
        strengths = evo["strengths"]
        weaknesses = evo["weaknesses"]
        print(f"Strengths: {strengths}")
        print(f"Weaknesses: {weaknesses}")
        assert len(strengths) > 0
        assert len(weaknesses) > 0

        # ── Test 3: get_agent_evolution with No Data ──────────────────────────
        print("\nStep 4: Testing get_agent_evolution() for agent with no data...")
        empty_evo = await get_agent_evolution(db, "999999999", period="30d")
        assert empty_evo["summary"]["total_analyses"] == 0
        assert empty_evo["trend"]["evaluacion_global_direction"] == "no_data"
        print("Success! Empty agent evolution returned correctly without crashing.")

        # ── Step 5: Clean up ──────────────────────────────────────────────────
        print("\nStep 5: Cleaning up test data...")
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.job_id == job.job_id))
        await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.job_id == job.job_id))
        await db.delete(job)
        await db.commit()
        print("Cleanup completed successfully.")

    print("\n=== ALL DASHBOARD VERIFICATION ENDPOINT TESTS PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(test_dashboard_endpoints())
