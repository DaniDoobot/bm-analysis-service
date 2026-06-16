"""
Test de regresión: verifica que `Decimal` está correctamente importado
en `mass_evaluation_service.py` y que la rama de persistencia de
`evaluacion_global` en análisis masivos funciona sin NameError.

Este test falla inmediatamente si `from decimal import Decimal` falta
en el módulo, replicando el error de producción observado en
mass_analysis_id=796, run_id=33, job_id=30.
"""
import asyncio
import logging
import sys
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db import SessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.services import Service
from app.models.typologies import Typology
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.services.mass_evaluation_service import MassEvaluationService

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Unique suffix per test run to avoid conflicts on repeated executions
_SUFFIX = uuid.uuid4().hex[:8]
SERVICE_KEY = f"test_dec_{_SUFFIX}"
JOB_NAME = f"Decimal Test Job {_SUFFIX}"
PROMPT_NAME = f"Test Decimal Prompt {_SUFFIX}"


async def _cleanup(db, ids: dict):
    """Clean up all test rows in dependency order."""
    if ids.get("mass_analysis_id"):
        await db.execute(
            MassEvaluationCriterionResult.__table__.delete().where(
                MassEvaluationCriterionResult.mass_analysis_id == ids["mass_analysis_id"]
            )
        )
    if ids.get("run_id"):
        await db.execute(
            MassEvaluationResult.__table__.delete().where(
                MassEvaluationResult.run_id == ids["run_id"]
            )
        )
        await db.execute(
            MassEvaluationRun.__table__.delete().where(
                MassEvaluationRun.run_id == ids["run_id"]
            )
        )
    if ids.get("job_id"):
        await db.execute(
            MassEvaluationJob.__table__.delete().where(
                MassEvaluationJob.job_id == ids["job_id"]
            )
        )
    if ids.get("criterion_id"):
        await db.execute(
            PromptCriterionTypology.__table__.delete().where(
                PromptCriterionTypology.criterion_id == ids["criterion_id"]
            )
        )
        await db.execute(
            PromptCriterion.__table__.delete().where(
                PromptCriterion.criterion_id == ids["criterion_id"]
            )
        )
    if ids.get("version_id"):
        await db.execute(
            PromptVersion.__table__.delete().where(
                PromptVersion.id == ids["version_id"]
            )
        )
    if ids.get("prompt_id"):
        await db.execute(
            Prompt.__table__.delete().where(
                Prompt.prompt_id == ids["prompt_id"]
            )
        )
    if ids.get("typology_id"):
        await db.execute(
            Typology.__table__.delete().where(
                Typology.typology_id == ids["typology_id"]
            )
        )
    if ids.get("service_id"):
        await db.execute(
            Service.__table__.delete().where(
                Service.service_id == ids["service_id"]
            )
        )
    await db.commit()


async def test_mass_eval_decimal_persistence():
    """
    Ejecuta _execute_background_run con datos de prueba controlados y verifica:
    - El run termina con status=completed (no failed).
    - evaluacion_global se persiste con un valor Decimal (no None).
    - global_score (propiedad del modelo) devuelve un float.
    - El NameError 'Decimal is not defined' NO se produce.
    """
    print("=== Test: mass_evaluation_service Decimal NameError regression ===")

    ids: dict = {}

    # ── 1. Preparar datos ─────────────────────────────────────────────────────
    async with SessionLocal() as db:
        try:
            # Servicio + tipología con claves únicas por ejecución
            svc = Service(service_key=SERVICE_KEY, service_name="Test Decimal Service")
            db.add(svc)
            await db.flush()
            ids["service_id"] = svc.service_id

            typ = Typology(
                service_id=svc.service_id,
                typology_key=f"cita_{_SUFFIX}",
                typology_name="Cita Test",
                is_active=True,
            )
            db.add(typ)
            await db.flush()
            ids["typology_id"] = typ.typology_id

            prompt = Prompt(
                prompt_name=PROMPT_NAME,
                prompt_type="audio",
                is_active=True,
                service_id=svc.service_id,
            )
            db.add(prompt)
            await db.flush()
            ids["prompt_id"] = prompt.prompt_id

            version = PromptVersion(
                prompt_id=prompt.prompt_id,
                prompt="Test snapshot content",
                is_current=True,
            )
            db.add(version)
            await db.flush()
            ids["version_id"] = version.id

            # Criterio evaluativo: usa clave reconocida por EVALUATIVE_CRITERIA_KEYS
            c1 = PromptCriterion(
                prompt_id=prompt.prompt_id,
                criterion_key="empatia",
                criterion_type="score_1_10",
                output_key="empatia",
                criterion_name="Empatia",
                is_active=True,
            )
            db.add(c1)
            await db.flush()
            ids["criterion_id"] = c1.criterion_id

            assoc = PromptCriterionTypology(
                criterion_id=c1.criterion_id,
                typology_id=typ.typology_id,
            )
            db.add(assoc)
            await db.flush()

            job = MassEvaluationJob(
                job_name=JOB_NAME,
                prompt_id=prompt.prompt_id,
                max_calls=1,
            )
            db.add(job)
            await db.flush()
            ids["job_id"] = job.job_id

            run = MassEvaluationRun(
                job_id=job.job_id,
                trigger_type="manual",
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            db.add(run)
            await db.commit()
            ids["run_id"] = run.run_id

        except Exception:
            await db.rollback()
            async with SessionLocal() as cleanup_db:
                await _cleanup(cleanup_db, ids)
            raise

    print(f"  Setup: job_id={ids['job_id']}, run_id={ids['run_id']}, prompt_id={ids['prompt_id']}")

    # ── 2. Parchear dependencias externas ─────────────────────────────────────
    import app.services.mass_evaluation_service as mes

    call_id = f"test_decimal_call_{ids['run_id']}"

    class MockHubSpot:
        async def search_calls_for_mass_evaluation(self, filters):
            return [
                {
                    "call_id": call_id,
                    "recording_url": "http://example.com/fake.mp3",
                    "hubspot_owner_id": "99001",
                    "hs_object_id": f"9900{ids['run_id']}",
                    "call_timestamp": "2026-06-16T10:00:00Z",
                    "call_duration_seconds": 120,
                    "direction": "INBOUND",
                }
            ]

    class MockTwilio:
        async def download_audio(self, url):
            return b"fake_audio_bytes"

    async def mock_analyze(*args, **kwargs):
        import json
        # tipo_llamada matches the typology_key without suffix (worker does case-insensitive match)
        return json.dumps({
            "tipo_llamada": f"cita_{_SUFFIX}",
            "empatia": 8,
            "empatia_feed": "Buen tono de test",
        })

    mes.HubSpotService = MockHubSpot
    mes.TwilioService = MockTwilio
    mes.analyze_audio_bytes = mock_analyze

    # ── 3. Ejecutar el worker ──────────────────────────────────────────────────
    print("  Running _execute_background_run...")
    try:
        await MassEvaluationService._execute_background_run(
            ids["job_id"], ids["run_id"], {"max_calls": 1}
        )
    except NameError as e:
        raise AssertionError(
            f"[FAIL] NameError raised in _execute_background_run: {e}\n"
            "This means 'from decimal import Decimal' is missing in mass_evaluation_service.py"
        )

    # ── 4. Validar y limpiar ───────────────────────────────────────────────────
    async with SessionLocal() as db:
        run_obj = (
            await db.execute(
                select(MassEvaluationRun).where(MassEvaluationRun.run_id == ids["run_id"])
            )
        ).scalars().first()
        assert run_obj is not None, "[FAIL] Run row not found"
        assert run_obj.status == "completed", (
            f"[FAIL] Run should be 'completed', got '{run_obj.status}'. "
            f"Error: {run_obj.error_message}"
        )
        print(f"  [OK] Run status = {run_obj.status}")

        res_row = (
            await db.execute(
                select(MassEvaluationResult).where(
                    MassEvaluationResult.run_id == ids["run_id"]
                )
            )
        ).scalars().first()
        assert res_row is not None, "[FAIL] MassEvaluationResult row not found"
        assert res_row.status == "completed", (
            f"[FAIL] Result status should be 'completed', got '{res_row.status}'"
        )
        print(f"  [OK] Result status = {res_row.status}")

        # evaluacion_global debe ser Decimal, no None
        assert res_row.evaluacion_global is not None, (
            "[FAIL] evaluacion_global is None — scoring branch was not reached or Decimal() failed"
        )
        assert isinstance(res_row.evaluacion_global, Decimal), (
            f"[FAIL] evaluacion_global type is {type(res_row.evaluacion_global)}, expected Decimal"
        )
        print(f"  [OK] evaluacion_global = {res_row.evaluacion_global} (Decimal)")

        # global_score (propiedad) debe ser float
        assert res_row.global_score is not None, "[FAIL] global_score property returned None"
        assert isinstance(res_row.global_score, float), (
            f"[FAIL] global_score type is {type(res_row.global_score)}, expected float"
        )
        print(f"  [OK] global_score = {res_row.global_score} (float)")

        # Guardar mass_analysis_id para el cleanup
        ids["mass_analysis_id"] = res_row.mass_analysis_id

        # Cleanup
        await _cleanup(db, ids)
        print("  [OK] Cleanup done")

    print("=== PASSED: Decimal NameError regression test ===")


if __name__ == "__main__":
    asyncio.run(test_mass_eval_decimal_persistence())
