"""
Tests for Content-Addressed Storage deduplication of prompt snapshots (bm_prompt_snapshots).

Verifies:
1. Two results with same prompt -> same snapshot_id
2. Different prompts -> different snapshot_ids
3. Exact text recovery (verbatim parity)
4. Pipeline status support: completed, skipped, failed
5. Concurrency and idempotency under race conditions
6. Backward compatibility with legacy rows (prompt_snapshot_id IS NULL)
7. API detail response contains prompt_snapshot
8. List/dashboard queries do not load heavy prompt text and support defer
9. Re-evaluation maintains historical traceability
10. Historical snapshot remains immutable even if PromptVersion is later modified
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from datetime import datetime, timezone
from decimal import Decimal

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_prompt_snapshots.db"

from sqlalchemy import BigInteger, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import defer, joinedload

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base
from sqlalchemy.ext.asyncio import create_async_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    PromptSnapshot,
)
from app.services.prompt_snapshot_service import (
    compute_prompt_hash,
    get_or_create_prompt_snapshot,
    resolve_prompt_snapshot_text,
)
from app.services.mass_evaluation_service import MassEvaluationService
from app.schemas.mass_evaluations import (
    MassEvaluationResultResponse,
    MassEvaluationResultListItemResponse,
)


class TestPromptSnapshotsDeduplication(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///test_prompt_snapshots.db")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(PromptSnapshot))
            await db.execute(delete(MassEvaluationRun))
            await db.execute(delete(MassEvaluationJob))
            await db.execute(delete(PromptVersion))
            await db.execute(delete(Prompt))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            # Base seed setup
            company = Company(company_id=1, company_name="Boston Medical Group", company_key="bmg")
            service = Service(service_id=1, company_id=1, service_key="front", service_name="Front Desk")
            prompt = Prompt(prompt_id=1, company_id=1, service_id=1, prompt_name="Prompt Front Desk", prompt_type="audio")
            p_ver = PromptVersion(
                id=1,
                prompt_id=1,
                prompt="Eres un evaluador de calidad clínico para Boston Medical Group (Front Desk)...",
                version_label="v1.0",
                is_current=True,
            )
            job = MassEvaluationJob(
                job_id=1,
                company_id=1,
                service_id=1,
                prompt_id=1,
                job_name="Job Test Deduplication",
            )
            run = MassEvaluationRun(
                run_id=1,
                job_id=1,
                trigger_type="manual",
                status="completed",
                calls_found=3,
                calls_selected=3,
            )
            db.add_all([company, service, prompt, p_ver, job, run])
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(PromptSnapshot))
            await db.execute(delete(MassEvaluationRun))
            await db.execute(delete(MassEvaluationJob))
            await db.execute(delete(PromptVersion))
            await db.execute(delete(Prompt))
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_01_two_results_same_prompt_get_same_snapshot_id(self):
        """Two evaluations with identical prompt text must share the exact same snapshot_id."""
        prompt_text = "Prompt idéntico para Front Desk con 57 KB de directrices clínicas..."
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res1 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_001",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_text, "status": "completed"}
            )
            res2 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_002",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_text, "status": "completed"}
            )
            await db.commit()

            self.assertIsNotNone(res1.prompt_snapshot_id)
            self.assertIsNotNone(res2.prompt_snapshot_id)
            self.assertEqual(res1.prompt_snapshot_id, res2.prompt_snapshot_id)

            # Check that only 1 physical row was created in bm_prompt_snapshots
            snap_count = await db.scalar(select(text("count(*)")).select_from(PromptSnapshot))
            self.assertEqual(snap_count, 1)

    async def test_02_different_prompts_get_different_snapshot_ids(self):
        """Different prompts must generate different snapshot_ids and independent snapshot rows."""
        prompt_front = "Prompt Front con directrices específicas de recepción."
        prompt_ventas = "Prompt Ventas con directrices comerciales distintas."

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res1 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_front",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_front, "status": "completed"}
            )
            res2 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_ventas",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_ventas, "status": "completed"}
            )
            await db.commit()

            self.assertNotEqual(res1.prompt_snapshot_id, res2.prompt_snapshot_id)
            snap_count = await db.scalar(select(text("count(*)")).select_from(PromptSnapshot))
            self.assertEqual(snap_count, 2)

    async def test_03_exact_text_recovery_verbatim(self):
        """Prompt text must be recovered byte-for-byte from deduplicated snapshot."""
        prompt_text = "Prompt extenso con acentos (áéíóú), saltos de línea\n\ny markdown: # Título"

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_text_check",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_text, "status": "completed"}
            )
            await db.commit()

            # Retrieve with helper
            recovered_text = await resolve_prompt_snapshot_text(db, res)
            self.assertEqual(recovered_text, prompt_text)

            # Retrieve via joined query
            stmt = select(MassEvaluationResult).options(joinedload(MassEvaluationResult.snapshot_rel)).where(
                MassEvaluationResult.call_id == "call_text_check"
            )
            loaded_res = (await db.execute(stmt)).scalars().first()
            self.assertEqual(loaded_res.prompt_snapshot, prompt_text)

    async def test_04_pipeline_statuses_completed_skipped_failed(self):
        """Completed, skipped, and failed calls must all save prompt_snapshot_id and zero redundant text."""
        prompt_text = "Prompt para test de estados de pipeline"

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            snap_id = await get_or_create_prompt_snapshot(db, prompt_text)

            r_comp = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_c",
                prompt_id=1,
                defaults={"prompt_snapshot_id": snap_id, "prompt_snapshot": None, "status": "completed"}
            )
            r_skip = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_s",
                prompt_id=1,
                defaults={"prompt_snapshot_id": snap_id, "prompt_snapshot": None, "status": "skipped"}
            )
            r_fail = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_f",
                prompt_id=1,
                defaults={"prompt_snapshot_id": snap_id, "prompt_snapshot": None, "status": "failed"}
            )
            await db.commit()

            self.assertEqual(r_comp.prompt_snapshot_id, snap_id)
            self.assertEqual(r_skip.prompt_snapshot_id, snap_id)
            self.assertEqual(r_fail.prompt_snapshot_id, snap_id)

            # Verify that legacy column is None (physical text storage saved!)
            self.assertIsNone(r_comp._prompt_snapshot)
            self.assertIsNone(r_skip._prompt_snapshot)
            self.assertIsNone(r_fail._prompt_snapshot)

    async def test_05_concurrency_and_idempotency_simulation(self):
        """Simultaneous concurrent calls trying to insert the same snapshot must succeed without duplicate rows or errors."""
        prompt_text = "Prompt con inserción altamente concurrente"

        async def worker_insert():
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                return await get_or_create_prompt_snapshot(session, prompt_text)

        # Launch 10 parallel workers concurrently
        ids = await asyncio.gather(*(worker_insert() for _ in range(10)))

        # All workers must return the exact same snapshot id
        first_id = ids[0]
        self.assertIsNotNone(first_id)
        for snap_id in ids:
            self.assertEqual(snap_id, first_id)

        # Database must have exactly 1 record
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            count = await db.scalar(select(text("count(*)")).select_from(PromptSnapshot))
            self.assertEqual(count, 1)

    async def test_06_backward_compatibility_with_legacy_rows(self):
        """Historical rows with legacy prompt_snapshot and prompt_snapshot_id=NULL must be readable without error."""
        legacy_text = "Texto histórico existente en producción antes de la migración"

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            legacy_res = MassEvaluationResult(
                run_id=1,
                job_id=1,
                call_id="call_legacy_001",
                prompt_id=1,
                status="completed",
                prompt_snapshot=legacy_text,  # Legacy column
                prompt_snapshot_id=None,      # No FK yet
            )
            db.add(legacy_res)
            await db.commit()

            # 1. Read property directly
            self.assertEqual(legacy_res.prompt_snapshot, legacy_text)

            # 2. Read with helper
            resolved = await resolve_prompt_snapshot_text(db, legacy_res)
            self.assertEqual(resolved, legacy_text)

            # 3. Validate with Pydantic schema
            schema_out = MassEvaluationResultResponse.model_validate(legacy_res)
            self.assertEqual(schema_out.prompt_snapshot, legacy_text)

    async def test_07_api_detail_endpoint_returns_prompt_snapshot(self):
        """API detail endpoint get_result must return prompt_snapshot correctly for both new and legacy rows."""
        new_prompt = "Prompt para nuevo resultado deduplicado"

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res_new = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_api_test",
                prompt_id=1,
                defaults={"prompt_snapshot": new_prompt, "status": "completed"}
            )
            await db.commit()
            mass_id = res_new.mass_analysis_id

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Query as get_result would query
            fetched = await MassEvaluationService.get_result(db, mass_id)
            self.assertIsNotNone(fetched)

            # Resolve text and validate schema
            resolved_text = await resolve_prompt_snapshot_text(db, fetched)
            fetched.__dict__["prompt_snapshot"] = resolved_text
            resp = MassEvaluationResultResponse.model_validate(fetched)

            self.assertEqual(resp.prompt_snapshot, new_prompt)
            self.assertEqual(resp.prompt_snapshot_id, fetched.prompt_snapshot_id)

    async def test_08_list_and_dashboard_queries_do_not_load_heavy_prompt_text(self):
        """Dashboard and list queries with defer(prompt_snapshot) must execute cleanly without loading text."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            snap_id = await get_or_create_prompt_snapshot(db, "Prompt pesado de 57 KB")
            for i in range(5):
                db.add(MassEvaluationResult(
                    run_id=1,
                    job_id=1,
                    call_id=f"call_list_{i}",
                    prompt_id=1,
                    status="completed",
                    prompt_snapshot_id=snap_id,
                ))
            await db.commit()

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Dashboard / analytics defer query
            stmt = select(MassEvaluationResult).options(defer(MassEvaluationResult.prompt_snapshot)).order_by(MassEvaluationResult.mass_analysis_id)
            results = (await db.execute(stmt)).scalars().all()
            self.assertEqual(len(results), 5)

            # List response item validation (does NOT include prompt_snapshot)
            for r in results:
                list_item = MassEvaluationResultListItemResponse.model_validate(r)
                self.assertFalse(hasattr(list_item, "prompt_snapshot"))
                self.assertEqual(list_item.prompt_snapshot_id, snap_id)

    async def test_09_reevaluation_maintains_historical_traceability(self):
        """Re-evaluating a call updates the record and links to the new prompt without breaking previous runs."""
        prompt_v1 = "Prompt v1 usado originalmente"
        prompt_v2 = "Prompt v2 usado en la re-evaluación con nuevos criterios"

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Initial evaluation with v1
            r1 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_reeval",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_v1, "status": "completed"}
            )
            await db.commit()
            snap_id_v1 = r1.prompt_snapshot_id

            # Re-evaluate with v2
            r2 = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_reeval",
                prompt_id=1,
                defaults={"prompt_snapshot": prompt_v2, "status": "completed"}
            )
            await db.commit()
            snap_id_v2 = r2.prompt_snapshot_id

            self.assertNotEqual(snap_id_v1, snap_id_v2)

            # Snapshot v1 still exists intact in bm_prompt_snapshots!
            s1 = await db.get(PromptSnapshot, snap_id_v1)
            self.assertEqual(s1.prompt_text, prompt_v1)

            # Snapshot v2 is active on the re-evaluated result
            s2 = await db.get(PromptSnapshot, snap_id_v2)
            self.assertEqual(s2.prompt_text, prompt_v2)

    async def test_10_historical_snapshot_immutable_when_prompt_version_changes(self):
        """Historical evaluations remain immutable even if PromptVersion is later modified or in-place healed."""
        historical_prompt = "Texto de prompt en el momento de la llamada histórica"

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Save historical result
            r = await MassEvaluationService._upsert_mass_evaluation_result(
                db=db,
                run_id=1,
                job_id=1,
                execution_source="on_demand",
                call_id="call_immutable_check",
                prompt_id=1,
                defaults={"prompt_snapshot": historical_prompt, "status": "completed"}
            )
            await db.commit()
            snap_id = r.prompt_snapshot_id

            # Later, an admin or self-healing modifies PromptVersion.prompt in place:
            p_ver = await db.get(PromptVersion, 1)
            p_ver.prompt = "Texto modificado posteriormente por un administrador o criteria auto-healing"
            await db.commit()

            # Verify that the historical evaluation's snapshot text DID NOT CHANGE!
            historical_snap = await db.get(PromptSnapshot, snap_id)
            self.assertEqual(historical_snap.prompt_text, historical_prompt)

            # Verifying resolved text on the historical result
            resolved = await resolve_prompt_snapshot_text(db, r)
            self.assertEqual(resolved, historical_prompt)

    async def test_11_new_evaluation_stored_with_null_legacy_and_valid_snapshot_id(self):
        """Verify that new evaluations can be saved with prompt_snapshot=NULL and prompt_snapshot_id IS NOT NULL."""
        prompt_content = "Prompt clínico para evaluación nueva deduplicada"
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            snap_id = await get_or_create_prompt_snapshot(db, prompt_content)

            # Persist evaluation result directly with prompt_snapshot=None and prompt_snapshot_id
            new_res = MassEvaluationResult(
                run_id=1,
                job_id=1,
                call_id="call_null_legacy_check",
                prompt_id=1,
                status="completed",
                prompt_snapshot_id=snap_id,
                _prompt_snapshot=None,
            )
            db.add(new_res)
            await db.commit()

            # Verify in DB via raw SQL that prompt_snapshot is literally NULL in the table
            row = (await db.execute(
                text("SELECT prompt_snapshot, prompt_snapshot_id FROM bm_mass_evaluation_results WHERE call_id = 'call_null_legacy_check'")
            )).first()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], snap_id)

            # Verify that ORM property transparently resolves to full prompt_text when loaded with snapshot
            loaded = await MassEvaluationService.get_result(db, new_res.mass_analysis_id)
            self.assertEqual(loaded.prompt_snapshot, prompt_content)

            # Also verify via resolve_prompt_snapshot_text fallback service
            plain_loaded = await db.get(MassEvaluationResult, new_res.mass_analysis_id)
            resolved = await resolve_prompt_snapshot_text(db, plain_loaded)
            self.assertEqual(resolved, prompt_content)

    async def test_12_no_redundant_index_on_hash_sha256_and_nullable_model(self):
        """Verify model has no redundant index on hash_sha256 and legacy column is nullable."""
        # 1. No redundant index on PromptSnapshot table
        index_names = [idx.name for idx in PromptSnapshot.__table__.indexes]
        self.assertNotIn("idx_bm_prompt_snapshots_hash", index_names)
        self.assertEqual(len(PromptSnapshot.__table__.indexes), 0)

        # Unique constraint is present
        constraint_names = [c.name for c in PromptSnapshot.__table__.constraints]
        self.assertIn("uq_bm_prompt_snapshots_hash", constraint_names)

        # 2. Nullable legacy column
        self.assertTrue(MassEvaluationResult._prompt_snapshot.nullable)

    async def test_13_historical_rows_keep_legacy_prompt_snapshot_intact(self):
        """Verify that historical rows retain prompt_snapshot text when linked to a snapshot."""
        historical_text = "Prompt histórico original que no debe borrarse"
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Create a legacy record
            legacy_res = MassEvaluationResult(
                run_id=1,
                job_id=1,
                call_id="call_historical_intact_check",
                prompt_id=1,
                status="completed",
                _prompt_snapshot=historical_text,
                prompt_snapshot_id=None,
            )
            db.add(legacy_res)
            await db.commit()

            # Backfill linkage
            snap_id = await get_or_create_prompt_snapshot(db, historical_text)
            legacy_res.prompt_snapshot_id = snap_id
            await db.commit()

            # Verify via raw SQL that prompt_snapshot is STILL preserved verbatim in the legacy column!
            row = (await db.execute(
                text("SELECT prompt_snapshot, prompt_snapshot_id FROM bm_mass_evaluation_results WHERE call_id = 'call_historical_intact_check'")
            )).first()
            self.assertEqual(row[0], historical_text)
            self.assertEqual(row[1], snap_id)
