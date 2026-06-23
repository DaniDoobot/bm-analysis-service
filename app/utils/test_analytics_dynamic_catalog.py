"""Test suite for Analytics V2 Dynamic Catalog and Criteria mapping."""
import asyncio
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

async def test_dynamic_catalog():
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        print("=== INICIANDO PRUEBAS DE CATALOGO DINAMICO Y MAPEO ===")

        # 1. Clean up old test data
        test_keys = ["empatia", "claridad", "cierre_cita", "saludo_inicio", "gestion_objeciones"]
        await db.execute(delete(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.criterion_key.in_(test_keys)))
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.in_(["call_dyn_1", "call_dyn_2"])))
        await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.trigger_type == "test_dynamic"))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_name == "Dynamic Test Job"))
        await db.execute(delete(User).where(User.username.in_(["test_dyn_admin"])))
        await db.commit()

        # 2. Seed Admin User
        admin_user = User(
            username="test_dyn_admin",
            email="dyn_admin@boston.es",
            role="administrador",
            is_active=True,
            password_hash=hash_password("adminpass123")
        )
        db.add(admin_user)
        await db.commit()

        # 3. Seed Job and Run
        job = MassEvaluationJob(
            job_name="Dynamic Test Job",
            prompt_id=888,
            is_active=True,
            schedule_enabled=False,
            created_by="Tester"
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        run = MassEvaluationRun(
            job_id=job.job_id,
            trigger_type="test_dynamic",
            status="completed",
            started_at=datetime.now(timezone.utc) - timedelta(days=2),
            finished_at=datetime.now(timezone.utc)
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 4. Seed Results
        # Result 1: Agent Luci Test (owner 99999888), Global 8.0, 3 days ago
        res1 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_dyn_1",
            hubspot_owner_id="99999888",
            agent_name="Luci Test Agent",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=3),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=3),
            prompt_id=888,
            prompt_snapshot="Prompt snapshot",
            evaluacion_global=8.0,
            result_json={},
            items_json=[]
        )
        # Result 2: Agent Luci Test (owner 99999888), Global 6.0, 1 day ago
        res2 = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id="call_dyn_2",
            hubspot_owner_id="99999888",
            agent_name="Luci Test Agent",
            status="completed",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            prompt_id=888,
            prompt_snapshot="Prompt snapshot",
            evaluacion_global=6.0,
            result_json={},
            items_json=[]
        )
        db.add_all([res1, res2])
        await db.commit()
        await db.refresh(res1)
        await db.refresh(res2)

        # Criteria Results
        # Res 1 criteria: empatia=9.0, saludo_inicio=True (boolean), gestion_objeciones=8.0
        c1_emp = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_dyn_1", criterion_key="empatia", criterion_type="score_1_10",
            numeric_value=9.0, is_applicable=True
        )
        c1_sal = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_dyn_1", criterion_key="saludo_inicio", criterion_type="boolean",
            boolean_value=True, is_applicable=True
        )
        c1_obj = MassEvaluationCriterionResult(
            mass_analysis_id=res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_dyn_1", criterion_key="gestion_objeciones", criterion_type="score_1_10",
            numeric_value=8.0, is_applicable=True
        )

        # Res 2 criteria: empatia=5.0, saludo_inicio=False (boolean), gestion_objeciones=None (not applicable)
        c2_emp = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_dyn_2", criterion_key="empatia", criterion_type="score_1_10",
            numeric_value=5.0, is_applicable=True
        )
        c2_sal = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_dyn_2", criterion_key="saludo_inicio", criterion_type="boolean",
            boolean_value=False, is_applicable=True
        )
        c2_obj = MassEvaluationCriterionResult(
            mass_analysis_id=res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
            call_id="call_dyn_2", criterion_key="gestion_objeciones", criterion_type="score_1_10",
            numeric_value=None, is_applicable=False # Non applicable!
        )
        
        db.add_all([c1_emp, c1_sal, c1_obj, c2_emp, c2_sal, c2_obj])
        await db.commit()

        # Token
        token_admin = create_access_token({"user_id": admin_user.user_id, "username": admin_user.username, "email": admin_user.email})

        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers_admin = {"Authorization": f"Bearer {token_admin}"}

            # 1. Test: GET /bm/analytics/items returns all items including custom/fallback ones
            print("\nTest 1: Verify items endpoint returns extended catalog...")
            res = await client.get("/bm/analytics/items", headers=headers_admin)
            assert res.status_code == 200, f"Expected 200, got {res.status_code}"
            items = res.json()
            assert len(items) > 6, f"Expected more than 6 items, got {len(items)}"
            
            # Check expected keys exist in catalog
            expected_keys = [
                "evaluacion_global", "empatia", "claridad", "procedimiento",
                "saludo_inicio", "n3_preguntas", "uso_preguntas", "despedida_refuerzo",
                "gestion_objeciones", "uso_nombre_paciente", "explicaciones_medicas",
                "claridad_explicacion_economica"
            ]
            for ek in expected_keys:
                assert any(i["key"] == ek for i in items), f"Catalog missing expected key: {ek}"
            print("[OK] Extended catalog items successfully verified.")

            # 2. Test: agents-comparison calculates aggregates for new items
            print("\nTest 2: Verify agents-comparison calculates aggregates for new items...")
            res = await client.get("/bm/analytics/agents-comparison?agent_owner_ids[]=99999888&item_keys[]=gestion_objeciones&item_keys[]=saludo_inicio", headers=headers_admin)
            assert res.status_code == 200
            data = res.json()
            
            # check gestion_objeciones (Res 1: 8.0, Res 2: non applicable -> avg = 8.0, count = 1)
            obj_row = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999888" and r["item_key"] == "gestion_objeciones"), None)
            assert obj_row is not None
            assert obj_row["value"] == 8.0, f"Expected 8.0, got {obj_row['value']}"
            assert obj_row["count"] == 1, f"Expected count 1 (due to non-applicable fallback), got {obj_row['count']}"
            
            # check saludo_inicio (Res 1: True -> 10.0, Res 2: False -> 0.0 -> avg = 5.0, count = 2)
            sal_row = next((r for r in data["comparison"] if r["hubspot_owner_id"] == "99999888" and r["item_key"] == "saludo_inicio"), None)
            assert sal_row is not None
            assert sal_row["value"] == 5.0, f"Expected 5.0, got {sal_row['value']}"
            assert sal_row["count"] == 2, f"Expected count 2, got {sal_row['count']}"
            print("[OK] agents-comparison dynamic aggregates and non-applicable logic verified.")

            # 3. Test: items-evolution returns chronological timeline for new items
            print("\nTest 3: Verify items-evolution returns chronological points for new items...")
            res = await client.get("/bm/analytics/items-evolution?agent_owner_ids[]=99999888&item_keys[]=gestion_objeciones&item_keys[]=saludo_inicio", headers=headers_admin)
            assert res.status_code == 200
            evolution = res.json()
            
            # verify both series returned
            assert any(s["item_key"] == "gestion_objeciones" for s in evolution)
            assert any(s["item_key"] == "saludo_inicio" for s in evolution)
            
            obj_series = next(s for s in evolution if s["item_key"] == "gestion_objeciones")
            assert len(obj_series["points"]) > 0
            print("[OK] items-evolution series timeline points successfully verified.")

        # 5. Cleanup
        print("\nCleaning up seeded test data...")
        await db.execute(delete(MassEvaluationCriterionResult).where(MassEvaluationCriterionResult.criterion_key.in_(test_keys)))
        await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.call_id.in_(["call_dyn_1", "call_dyn_2"])))
        await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.trigger_type == "test_dynamic"))
        await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_name == "Dynamic Test Job"))
        await db.execute(delete(User).where(User.username.in_(["test_dyn_admin"])))
        await db.commit()
        print("=== TODAS LAS PRUEBAS DE CATALOGO DINAMICO PASARON CON ÉXITO ===")

if __name__ == "__main__":
    asyncio.run(test_dynamic_catalog())
