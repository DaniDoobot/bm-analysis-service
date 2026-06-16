import sys
import os
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

# Mock environment to avoid engine creation crash during app.db import
os.environ["DATABASE_URL"] = "postgresql://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"

# Custom compilation rules for SQLite JSON/BigInteger handling
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import BigInteger

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.main import app
from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.services import Service
from app.models.typologies import Typology
from app.models.criteria import PromptCriterion, CriteriaSyncLog
from app.models.prompts import Prompt
from app.models.analyses import Analysis, AnalysisCriterionResult

# SQLite Memory engine specifically for our tests
engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autocommit=False, autoflush=False
)

# Active role for mock user
current_mock_role = "admin"

# Dependency overrides
async def override_get_db():
    async with AsyncSessionLocal() as session:
        yield session

def mock_get_current_user():
    return User(
        user_id=1,
        username="admin",
        email="admin@doobot.ai",
        role=current_mock_role,
        hubspot_owner_id=None,
        password_hash="fakehash"
    )

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_user] = mock_get_current_user

async def seed_data():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    async with AsyncSessionLocal() as db:
        # Seed Service
        srv = Service(service_id=1, service_key="front", service_name="Front Desk")
        db.add(srv)
        await db.flush()

        # Seed Prompt
        pr = Prompt(prompt_id=22, prompt_name="Test Prompt", prompt_type="audio", service_id=1, is_active=True)
        db.add(pr)
        await db.flush()

        # Seed Criteria
        # Criterion 1: normal matching
        c1 = PromptCriterion(
            criterion_id=100,
            prompt_id=22,
            criterion_key="empatia",
            criterion_name="Nombre_Actual",
            criterion_type="score_1_10",
            output_key="empatia",
            is_active=True
        )
        # Criterion 2: matching but already synced (no changes)
        c2 = PromptCriterion(
            criterion_id=101,
            prompt_id=22,
            criterion_key="saludo",
            criterion_name="Saludo_Actual",
            criterion_type="boolean",
            output_key="saludo",
            is_active=True
        )
        # Criterion 3: Case 15 - Same visible name with different key
        # (c1 has name "Nombre_Actual", c3 will also have name "Nombre_Actual" but key is different)
        c3 = PromptCriterion(
            criterion_id=102,
            prompt_id=22,
            criterion_key="empatia_dupe",
            criterion_name="Nombre_Actual",
            criterion_type="score_1_10",
            output_key="empatia_dupe",
            is_active=True
        )
        db.add_all([c1, c2, c3])
        await db.flush()

        # Seed Historical Analysis
        an = Analysis(
            analysis_id=50,
            analysis_type="audio",
            call_id="call_1",
            prompt_id=22,
            status="completed",
            result={"empatia": 8, "saludo": True}, # result_json
            payload={"raw": "test"},
            evaluacion_global=Decimal("8.00")
        )
        db.add(an)
        await db.flush()

        # Seed Historical Analysis Criterion Results
        acr1 = AnalysisCriterionResult(
            id=200,
            analysis_id=50,
            prompt_id=22,
            criterion_id=100,
            criterion_key="empatia",
            criterion_name="Nombre_Antiguo",
            numeric_value=Decimal("8.00"),
            is_applicable=True
        )
        acr2 = AnalysisCriterionResult(
            id=201,
            analysis_id=50,
            prompt_id=22,
            criterion_id=101,
            criterion_key="saludo",
            criterion_name="Saludo_Actual",
            boolean_value=True,
            is_applicable=True
        )
        # Case 5 - Historical result with non-existent criterion (criterion_id 999)
        acr3 = AnalysisCriterionResult(
            id=202,
            analysis_id=50,
            prompt_id=22,
            criterion_id=999,
            criterion_key="criterio_fantasma",
            criterion_name="Fantasma_Nombre",
            is_applicable=True
        )
        # Case 15 - Historical result for c3 (same visible name with different key)
        acr4 = AnalysisCriterionResult(
            id=203,
            analysis_id=50,
            prompt_id=22,
            criterion_id=102,
            criterion_key="empatia_dupe",
            criterion_name="Nombre_Antiguo_Dupe",
            numeric_value=Decimal("7.00"),
            is_applicable=True
        )
        db.add_all([acr1, acr2, acr3, acr4])
        await db.flush()

        # Seed Mass Evaluation objects
        job = MassEvaluationJob(job_id=1, job_name="Test Job", prompt_id=22, is_active=True, timezone="Europe/Madrid")
        db.add(job)
        await db.flush()

        run = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed", started_at=datetime.now(timezone.utc))
        db.add(run)
        await db.flush()

        # Seed Mass Evaluation Result (parent) with items_json mismatch
        items_json_data = [
            {"criterion_id": 100, "criterion_key": "empatia", "name": "Nombre_Antiguo", "value": 8, "output_key": "empatia"},
            {"criterion_id": 101, "criterion_key": "saludo", "name": "Saludo_Actual", "value": "Sí", "output_key": "saludo"},
            {"criterion_id": 102, "criterion_key": "empatia_dupe", "name": "Nombre_Antiguo_Dupe", "value": 7, "output_key": "empatia_dupe"}
        ]
        mer = MassEvaluationResult(
            mass_analysis_id=300,
            run_id=1,
            job_id=1,
            call_id="call_2",
            prompt_id=22,
            prompt_snapshot="{\"test\": 1}", # prompt_snapshot to verify preservation
            status="completed",
            result_json={"empatia": 8, "saludo": True, "empatia_dupe": 7}, # result_json to verify preservation
            items_json=items_json_data,
            evaluacion_global=Decimal("8.00"),
            analysis_timestamp=datetime.now(timezone.utc)
        )
        db.add(mer)
        await db.flush()

        # Seed Mass Evaluation Criterion Results
        mecr1 = MassEvaluationCriterionResult(
            id=400,
            mass_analysis_id=300,
            run_id=1,
            job_id=1,
            call_id="call_2",
            prompt_id=22,
            criterion_id=100,
            criterion_key="empatia",
            criterion_name="Nombre_Antiguo",
            numeric_value=Decimal("8.00")
        )
        mecr2 = MassEvaluationCriterionResult(
            id=401,
            mass_analysis_id=300,
            run_id=1,
            job_id=1,
            call_id="call_2",
            prompt_id=22,
            criterion_id=101,
            criterion_key="saludo",
            criterion_name="Saludo_Actual",
            boolean_value=True
        )
        mecr3 = MassEvaluationCriterionResult(
            id=402,
            mass_analysis_id=300,
            run_id=1,
            job_id=1,
            call_id="call_2",
            prompt_id=22,
            criterion_id=102,
            criterion_key="empatia_dupe",
            criterion_name="Nombre_Antiguo_Dupe",
            numeric_value=Decimal("7.00")
        )
        db.add_all([mecr1, mecr2, mecr3])
        await db.commit()

async def run_tests():
    global current_mock_role
    print("=== SEEDING TEST DATABASE ===")
    await seed_data()

    print("\n=== STARTING INTEGRATION TESTS ===")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        
        # 1. Preview without write
        print("\nTest 1: Preview without write")
        r = await client.post("/bm/admin/sync-criteria-names/preview", json={"prompt_id": 22})
        assert r.status_code == 200
        preview_data = r.json()
        print(f"Preview: {preview_data}")
        # Verify 2 criteria have mismatches (c1: empatia, c3: empatia_dupe)
        assert preview_data["total_criteria_to_sync"] == 2
        assert preview_data["individual_results_to_update"] == 2 # acr1 (id 200) and acr4 (id 203)
        assert preview_data["mass_results_to_update"] == 2       # mecr1 (id 400) and mecr3 (id 402)
        
        # Verify db wasn't modified
        async with AsyncSessionLocal() as db:
            acr = await db.get(AnalysisCriterionResult, 200)
            assert acr.criterion_name == "Nombre_Antiguo"
        print("[OK] Preview endpoint returned correct metrics and did not write to the database.")

        # 6. User non-admin security
        print("\nTest 6: User non-admin security")
        current_mock_role = "agent"  # Mock non-admin role
        r = await client.post("/bm/admin/sync-criteria-names/preview", json={"prompt_id": 22})
        assert r.status_code == 403
        r = await client.post("/bm/admin/sync-criteria-names/execute", json={"prompt_id": 22})
        assert r.status_code == 403
        current_mock_role = "admin"  # Restore admin role
        print("[OK] Non-admin users are successfully blocked with 403 Forbidden.")

        # 7. Empty or too long name validation
        print("\nTest 7: Empty or too long name validation")
        async with AsyncSessionLocal() as db:
            c_empatia = await db.get(PromptCriterion, 100)
            original_name = c_empatia.criterion_name
            # Empty name
            c_empatia.criterion_name = ""
            await db.commit()
            
        r = await client.post("/bm/admin/sync-criteria-names/preview", json={"prompt_id": 22})
        assert r.status_code == 422
        assert "no puede estar vacío" in r.text

        async with AsyncSessionLocal() as db:
            c_empatia = await db.get(PromptCriterion, 100)
            # Excessively long name
            c_empatia.criterion_name = "A" * 300
            await db.commit()
            
        r = await client.post("/bm/admin/sync-criteria-names/preview", json={"prompt_id": 22})
        assert r.status_code == 422
        assert "excesivamente largo" in r.text

        # Restore original name
        async with AsyncSessionLocal() as db:
            c_empatia = await db.get(PromptCriterion, 100)
            c_empatia.criterion_name = original_name
            await db.commit()
        print("[OK] Empty and excessively long names are rejected with HTTP 422.")

        # 4. Concurrency conflict check
        print("\nTest 4: Concurrency conflict check")
        # Send expected counts that do not match current counts (current is 2)
        r = await client.post("/bm/admin/sync-criteria-names/execute", json={
            "prompt_id": 22,
            "expected_individual_results_to_update": 10,
            "expected_mass_results_to_update": 2
        })
        assert r.status_code == 409
        assert "Concurrency conflict" in r.text

        # Verify db wasn't modified
        async with AsyncSessionLocal() as db:
            acr = await db.get(AnalysisCriterionResult, 200)
            assert acr.criterion_name == "Nombre_Antiguo"
        print("[OK] Execution with mismatched counts throws 409 and does zero modifications.")

        # 3. Rollback
        print("\nTest 3: Transactional Rollback")
        # We will mock/raise an error on execution by passing invalid parameter in a path or triggering DB error.
        # Let's verify that the execute endpoint rolls back if an error happens.
        # We can temporarily corrupt the table structure or mock DB update to fail.
        # But wait! A simpler way is to test that if execute fails, everything is rolled back.
        # We can verify transactional integrity by knowing it runs under a single db transaction context.
        # Let's trigger a failure inside execute_sync_criteria_names dynamically or verify rollback.
        # We can trigger it by causing an integrity error or validation error during execution (e.g. invalid type).
        # We will mock the service to fail during updates:
        from unittest.mock import patch
        with patch("app.services.criteria_sync_service.flag_modified", side_effect=RuntimeError("Simulated DB Crash")):
            r = await client.post("/bm/admin/sync-criteria-names/execute", json={
                "prompt_id": 22,
                "expected_individual_results_to_update": 2,
                "expected_mass_results_to_update": 2
            })
            assert r.status_code == 400
            assert "Simulated DB Crash" in r.text

        # Verify that even though individual results updates were executed before items_json, they were rolled back!
        async with AsyncSessionLocal() as db:
            acr = await db.get(AnalysisCriterionResult, 200)
            assert acr.criterion_name == "Nombre_Antiguo", "Did not rollback!"
        print("[OK] Rollback successful on simulated failure; zero database modifications persisted.")

        # 2. Correct Execution
        print("\nTest 2: Correct Execution")
        r = await client.post("/bm/admin/sync-criteria-names/execute", json={
            "prompt_id": 22,
            "expected_individual_results_to_update": 2,
            "expected_mass_results_to_update": 2
        })
        assert r.status_code == 200
        exec_data = r.json()
        print(f"Execute result: {exec_data}")
        assert exec_data["ok"] is True
        assert exec_data["individual_criteria_rows_updated"] == 2 # acr1 and acr4
        assert exec_data["mass_criteria_rows_updated"] == 2       # mecr1 and mecr3
        assert exec_data["mass_results_rows_updated"] == 1        # Only one parent result row 300 contains items_json mismatches
        print("[OK] Execute completed with correct row update counts.")

        # 13. Persisted Audit Log
        print("\nTest 13: Persisted Audit Log Check")
        async with AsyncSessionLocal() as db:
            logs_res = await db.execute(select(CriteriaSyncLog).order_by(CriteriaSyncLog.id))
            logs = logs_res.scalars().all()
            assert len(logs) == 2  # One log for empatia, one for empatia_dupe
            
            # Check log for empatia (criterion_id 100)
            log_empatia = next(l for l in logs if l.criterion_id == 100)
            assert log_empatia.prompt_id == 22
            assert log_empatia.criterion_key == "empatia"
            assert log_empatia.old_name == "Nombre_Antiguo"
            assert log_empatia.new_name == "Nombre_Actual"
            assert log_empatia.individual_rows_affected == 1
            assert log_empatia.mass_rows_affected == 1
            assert log_empatia.mass_results_rows_affected == 1
            assert log_empatia.performed_by_email == "admin@doobot.ai"
            assert isinstance(log_empatia.created_at, datetime)

            # Check log for empatia_dupe (criterion_id 102)
            log_dupe = next(l for l in logs if l.criterion_id == 102)
            assert log_dupe.prompt_id == 22
            assert log_dupe.criterion_key == "empatia_dupe"
            assert log_dupe.old_name == "Nombre_Antiguo_Dupe"
            assert log_dupe.new_name == "Nombre_Actual"
            assert log_dupe.individual_rows_affected == 1
            assert log_dupe.mass_rows_affected == 1
            assert log_dupe.mass_results_rows_affected == 1
            assert log_dupe.performed_by_email == "admin@doobot.ai"
        print("[OK] Persisted audit log row saved with correct user, names, affected counts and timestamps.")

        # 8, 9, 10, 11, 12. Verification of values updated and preserved
        print("\nTests 8, 9, 10, 11, 12: Mappings and preservation check")
        async with AsyncSessionLocal() as db:
            # Case 8: Match by criterion_id
            acr1 = await db.get(AnalysisCriterionResult, 200)
            assert acr1.criterion_name == "Nombre_Actual"

            # Case 10: Preservation of keys, values, and application flags
            assert acr1.criterion_key == "empatia"
            assert acr1.numeric_value == Decimal("8.00")
            assert acr1.is_applicable is True

            acr2 = await db.get(AnalysisCriterionResult, 201)
            assert acr2.criterion_name == "Saludo_Actual"
            assert acr2.boolean_value is True

            # Case 5: Non-existent criterion (criterion_id = 999) remains untouched
            acr3 = await db.get(AnalysisCriterionResult, 202)
            assert acr3.criterion_name == "Fantasma_Nombre"

            # Case 15: Same visible name with different key (c3: empatia_dupe) updated correctly
            acr4 = await db.get(AnalysisCriterionResult, 203)
            assert acr4.criterion_name == "Nombre_Actual"
            assert acr4.criterion_key == "empatia_dupe"
            assert acr4.numeric_value == Decimal("7.00")

            # Case 11: Preservation of result_json
            an = await db.get(Analysis, 50)
            assert an.result == {"empatia": 8, "saludo": True}

            # Case 12: Preservation of prompt_snapshot and items_json structure/values
            mer = await db.get(MassEvaluationResult, 300)
            assert mer.prompt_snapshot == "{\"test\": 1}"
            assert mer.result_json == {"empatia": 8, "saludo": True, "empatia_dupe": 7}
            
            # Case 9: JSON match check
            items = mer.items_json
            assert len(items) == 3
            # empatia updated
            assert items[0]["criterion_id"] == 100
            assert items[0]["criterion_key"] == "empatia"
            assert items[0]["name"] == "Nombre_Actual"
            assert items[0]["value"] == 8
            # saludo untouched
            assert items[1]["criterion_id"] == 101
            assert items[1]["criterion_key"] == "saludo"
            assert items[1]["name"] == "Saludo_Actual"
            assert items[1]["value"] == "Sí"
            # empatia_dupe updated
            assert items[2]["criterion_id"] == 102
            assert items[2]["criterion_key"] == "empatia_dupe"
            assert items[2]["name"] == "Nombre_Actual"
            assert items[2]["value"] == 7
        print("[OK] IDs and keys successfully matched. Original values, scores, result_json, and prompt_snapshots were preserved.")

        # 14. Idempotence
        print("\nTest 14: Idempotence check")
        # Call preview again (should be 0)
        r = await client.post("/bm/admin/sync-criteria-names/preview", json={"prompt_id": 22})
        assert r.status_code == 200
        data = r.json()
        assert data["total_criteria_to_sync"] == 0
        assert data["individual_results_to_update"] == 0
        assert data["mass_results_to_update"] == 0
        assert len(data["details"]) == 0

        # Execute again (should do 0 updates and succeed)
        r = await client.post("/bm/admin/sync-criteria-names/execute", json={
            "prompt_id": 22,
            "expected_individual_results_to_update": 0,
            "expected_mass_results_to_update": 0
        })
        assert r.status_code == 200
        exec_data = r.json()
        assert exec_data["individual_criteria_rows_updated"] == 0
        assert exec_data["mass_criteria_rows_updated"] == 0
        assert exec_data["mass_results_rows_updated"] == 0
        print("[OK] Execution is fully idempotent; running it a second time makes 0 changes.")

    print("\n=== ALL INTEGRATION TESTS COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(run_tests())
