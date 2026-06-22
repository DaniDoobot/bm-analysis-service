"""Integration test suite for Analytics V2 endpoints."""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Add workspace directory to path
sys.path.append(".")

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.utils.security import create_access_token, hash_password

async def test_analytics_v2_workflow():
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        print("=== INICIANDO PRUEBAS INTEGRADAS DE ANALYTICS V2 ===")

        # 1. Clean up old test data
        await db.execute(delete(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.criterion_key.in_(["empatia", "claridad", "cierre_cita"])))
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.in_(["call_test_1", "call_test_2", "call_test_3"])))
        await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.trigger_type == "test_analytics"))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_name == "Analytics Test Job"))
        await db.execute(delete(User).where(User.username.in_(["test_analytics_admin", "test_analytics_agent"])))
        await db.commit()

        # 2. Seed Users
        admin_user = User(
            username="test_analytics_admin",
            email="analytics_admin@boston.es",
            role="administrador",
            is_active=True,
            password_hash=hash_password("adminpass123")
        )
        agent_user = User(
            username="test_analytics_agent",
            email="analytics_agent@boston.es",
            role="agente",
            hubspot_owner_id="99999999", # Luci test ID
            is_active=True,
            password_hash=hash_password("agentpass123")
        )
        db.add_all([admin_user, agent_user])
        await db.commit()

        # 3. Seed Mass Evaluation structure (Job, Run)
        job = MassEvaluationJob(
            job_name="Analytics Test Job",
            prompt_id=999,
            is_active=True,
            schedule_enabled=False,
            created_by="Tester"
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        run = MassEvaluationRun(
            job_id=job.job_id,
            trigger_type="test_analytics",
            status="completed",
            started_at=datetime.now(timezone.utc) - timedelta(days=2),
            finished_at=datetime.now(timezone.utc)
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. Seed Results and Criteria
        # Result 1: Luci (owner 99999999), Global 8.0, 5 days ago
        res1 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_test_1",
            hubspot_owner_id="99999999",
            agent_name="Luci Dos Santos Furtado",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=5),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=5),
            prompt_id=999,
            prompt_snapshot="Prompt snapshot",
            evaluacion_global=8.0,
            result_json={"tipo_llamada": "cita"},
            items_json=[]
        )
        # Result 2: Luci, Global 6.0, 2 days ago
        res2 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_test_2",
            hubspot_owner_id="99999999",
            agent_name="Luci Dos Santos Furtado",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
            prompt_id=999,
            prompt_snapshot="Prompt snapshot",
            evaluacion_global=6.0,
            result_json={"tipo_llamada": "soporte"},
            items_json=[]
        )
        # Result 3: Cristina (owner 99999998), Global 9.0, 1 day ago
        res3 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_test_3",
            hubspot_owner_id="99999998",
            agent_name="Cristina Montenegro",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            prompt_id=999,
            prompt_snapshot="Prompt snapshot",
            evaluacion_global=9.0,
            result_json={},
            items_json=[]
        )
        db.add_all([res1, res2, res3])
        await db.commit()
        await db.refresh(res1)
        await db.refresh(res2)
        await db.refresh(res3)

        # Criteria Results
        # Res 1 criteria: empatia=9.0, claridad=8.0, cierre_cita=True
        c1_emp = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_1", criterion_key="empatia", criterion_type="score_1_10",
            numeric_value=9.0, is_applicable=True
        )
        c1_cla = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_1", criterion_key="claridad", criterion_type="score_1_10",
            numeric_value=8.0, is_applicable=True
        )
        c1_cie = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_1", criterion_key="cierre_cita", criterion_type="boolean",
            boolean_value=True, is_applicable=True
        )

        # Res 2 criteria: empatia=5.0, claridad=None (not applicable), cierre_cita=False
        c2_emp = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_2", criterion_key="empatia", criterion_type="score_1_10",
            numeric_value=5.0, is_applicable=True
        )
        c2_cla = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_2", criterion_key="claridad", criterion_type="score_1_10",
            numeric_value=None, is_applicable=False # Not applicable!
        )
        c2_cie = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_2", criterion_key="cierre_cita", criterion_type="boolean",
            boolean_value=False, is_applicable=True
        )

        # Res 3 criteria: empatia=10.0, claridad=9.0, cierre_cita=None
        c3_emp = MassEvaluationCriterionResult(
            mass_analysis_id=res3.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_3", criterion_key="empatia", criterion_type="score_1_10",
            numeric_value=10.0, is_applicable=True
        )
        c3_cla = MassEvaluationCriterionResult(
            mass_analysis_id=res3.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_test_3", criterion_key="claridad", criterion_type="score_1_10",
            numeric_value=9.0, is_applicable=True
        )
        db.add_all([c1_emp, c1_cla, c1_cie, c2_emp, c2_cla, c2_cie, c3_emp, c3_cla])
        await db.commit()

        # Generate tokens
        token_admin = create_access_token({"user_id": admin_user.user_id, "username": admin_user.username, "email": admin_user.email})
        token_agent = create_access_token({"user_id": agent_user.user_id, "username": agent_user.username, "email": agent_user.email})

        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers_admin = {"Authorization": f"Bearer {token_admin}"}
            headers_agent = {"Authorization": f"Bearer {token_agent}"}

            # --- TEST 1: GET /bm/analytics/items (Admin) ---
            print("\nTest 1: GET /bm/analytics/items as admin...")
            res = await client.get("/bm/analytics/items", headers=headers_admin)
            assert res.status_code == 200, f"Expected 200, got {res.status_code}"
            items = res.json()
            assert isinstance(items, list), "Expected list of items"
            print(f"Items found: {[i['key'] for i in items]}")
            assert any(i["key"] == "evaluacion_global" for i in items)
            assert any(i["key"] == "cierre_cita" and i["type"] == "percentage" for i in items)
            assert any(i["key"] == "empatia" and i["type"] == "score" for i in items)

            # --- TEST 2: GET /bm/analytics/items (Agent Forbidden) ---
            print("\nTest 2: GET /bm/analytics/items as agent...")
            res = await client.get("/bm/analytics/items", headers=headers_agent)
            assert res.status_code == 403, f"Expected 403, got {res.status_code}"
            print("[OK] Agent successfully rejected with 403 Forbidden.")

            # --- TEST 3: GET /bm/analytics/agents-comparison (Admin) ---
            print("\nTest 3: GET /bm/analytics/agents-comparison...")
            res = await client.get("/bm/analytics/agents-comparison", headers=headers_admin)
            assert res.status_code == 200, f"Expected 200, got {res.status_code}"
            data = res.json()
            assert "agents" in data
            assert "items" in data
            assert "comparison" in data
            
            # Check Luci's aggregates (owner 99999999)
            luci_row_eg = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999999" and r["item_key"] == "evaluacion_global"), None)
            assert luci_row_eg is not None
            assert luci_row_eg["value"] == 7.0, f"Expected 7.0, got {luci_row_eg['value']}"
            assert luci_row_eg["count"] == 2

            luci_row_emp = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999999" and r["item_key"] == "empatia"), None)
            assert luci_row_emp is not None
            assert luci_row_emp["value"] == 7.0
            assert luci_row_emp["count"] == 2

            luci_row_cla = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999999" and r["item_key"] == "claridad"), None)
            assert luci_row_cla is not None
            assert luci_row_cla["value"] == 8.0, f"Expected 8.0, got {luci_row_cla['value']}"
            assert luci_row_cla["count"] == 1  # Result 2 clarity is not applicable, count should be 1!

            luci_row_cie = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999999" and r["item_key"] == "cierre_cita"), None)
            assert luci_row_cie is not None
            assert luci_row_cie["value"] == 50.0 # 50% rate
            assert luci_row_cie["count"] == 2

            # Check Cristina's aggregates (owner 99999998)
            cris_row_eg = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999998" and r["item_key"] == "evaluacion_global"), None)
            assert cris_row_eg is not None
            assert cris_row_eg["value"] == 9.0
            assert cris_row_eg["count"] == 1

            cris_row_cie = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999998" and r["item_key"] == "cierre_cita"), None)
            assert cris_row_cie is not None
            assert cris_row_cie["value"] is None
            assert cris_row_cie["count"] == 0

            print("[OK] Comparison calculations match expected aggregates exactly.")

            # --- TEST 4: Filter by agent_owner_ids[] (bracket array format) ---
            print("\nTest 4: GET with agent_owner_ids[]...")
            res = await client.get("/bm/analytics/agents-comparison?agent_owner_ids[]=99999999", headers=headers_admin)
            assert res.status_code == 200
            data = res.json()
            assert len(data["agents"]) == 1
            assert data["agents"][0]["hubspot_owner_id"] == "99999999"

            # --- TEST 5: Filter by agent_owner_ids (no bracket format) ---
            print("\nTest 5: GET with agent_owner_ids...")
            res = await client.get("/bm/analytics/agents-comparison?agent_owner_ids=99999999", headers=headers_admin)
            assert res.status_code == 200
            data = res.json()
            assert len(data["agents"]) == 1
            assert data["agents"][0]["hubspot_owner_id"] == "99999999"

            # --- TEST 6: Filter by item_keys[] ---
            print("\nTest 6: GET with item_keys[]...")
            res = await client.get("/bm/analytics/agents-comparison?item_keys[]=empatia", headers=headers_admin)
            assert res.status_code == 200
            data = res.json()
            assert len(data["items"]) == 1
            assert data["items"][0]["key"] == "empatia"
            assert all(r["item_key"] == "empatia" for r in data["comparison"])

            # --- TEST 7: GET /bm/analytics/items-evolution (Timeline Series) ---
            print("\nTest 7: GET /bm/analytics/items-evolution...")
            res = await client.get("/bm/analytics/items-evolution?agent_owner_ids[]=99999999&agent_owner_ids[]=99999998", headers=headers_admin)
            assert res.status_code == 200, f"Expected 200, got {res.status_code}"
            evolution = res.json()
            assert isinstance(evolution, list)
            assert any(s["item_key"] == "evaluacion_global" for s in evolution)
            assert any(s["item_key"] == "cierre_cita" for s in evolution)
            
            # Check timeline points
            global_series = next(s for s in evolution if s["item_key"] == "evaluacion_global")
            assert len(global_series["points"]) > 0
            print(f"Points returned for global: {global_series['points']}")

            # --- TEST 8: Verify OpenAPI documentation ---
            print("\nTest 8: Checking /openapi.json...")
            res_openapi = await client.get("/openapi.json")
            assert res_openapi.status_code == 200
            openapi = res_openapi.json()
            paths = openapi.get("paths", {})
            assert "/bm/analytics/items" in paths
            assert "/bm/analytics/agents-comparison" in paths
            assert "/bm/analytics/items-evolution" in paths
            print("[OK] Analytics V2 paths successfully documented in OpenAPI.")

            # --- TEST 9: Verify legacy endpoint does not break ---
            print("\nTest 9: Verifying /bm/me/evolution...")
            res_legacy = await client.get("/bm/me/evolution", headers=headers_agent)
            # Should not crash and return valid evolution structure
            assert res_legacy.status_code in [200, 400]  # Might be 400 if no data matched for this agent in past 30 days, but must not 500!
            print(f"Legacy evolution status: {res_legacy.status_code}")

            # 5. Cleanup
            print("\nCleaning up seeded test data...")
            await db.execute(delete(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.criterion_key.in_(["empatia", "claridad", "cierre_cita"])))
            await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.in_(["call_test_1", "call_test_2", "call_test_3"])))
            await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.trigger_type == "test_analytics"))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_name == "Analytics Test Job"))
            await db.execute(delete(User).where(User.username.in_(["test_analytics_admin", "test_analytics_agent"])))
            await db.commit()
            print("=== TODAS LAS PRUEBAS DE ANALYTICS V2 HAN PASADO CON ÉXITO ===")

if __name__ == "__main__":
    asyncio.run(test_analytics_v2_workflow())
