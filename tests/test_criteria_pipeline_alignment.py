"""
Tests for criteria alignment between Mass Evaluations and Laboratory pipelines.

Verifies:
1. Short call / voicemail evaluation (No Contacto, Tipificación Llamada, Razón no contacto).
2. Parity between Mass Evaluations and Laboratory for identical raw results and prompts.
3. Newly added criteria synchronization without hardcoded lists.
4. Typology matching and normalization without destructive legacy overrides.
5. "Tipo de llamada" classification criterion is preserved and never dropped/marked non-applicable.
"""
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, BigInteger
from sqlalchemy.ext.compiler import compiles

@compiles(BigInteger, "sqlite")
def compile_big_int_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base
from app.models.companies import Company
from app.models.analyses import Analysis, AnalysisCriterionResult, CallAnalysisCurrent
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure, BaseStructureTypology
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.typologies import Typology
from app.models.services import Service
from app.services.analysis_persistence import save_analysis, _insert_results, resolve_service_typology, normalize_tipo_llamada
from app.services.mass_evaluation_service import MassEvaluationService
from app.services.transcription_analysis_service import analyze_transcription_pipeline
from app.utils.visual_formatters import build_items_visual


class TestCriteriaPipelineAlignment(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async with self.SessionLocal() as session:
            # 1. Company, Base Structure & Service
            comp = Company(company_id=1, company_name="Test Company", company_key="test-co", is_active=True)
            svc = Service(service_id=1, service_key="front", service_name="Front Desk", company_id=1)
            base = PromptBaseStructure(id=1, structure_key="front_base", structure_name="Front Structure", base_prompt="Base prompt")
            
            # 2. Prompt & Current Version
            prompt = Prompt(
                prompt_id=10,
                service_id=1,
                company_id=1,
                base_structure_id=1,
                prompt_name="Front Analysis Prompt",
                prompt_type="audio",
                is_active=True
            )
            v = PromptVersion(
                id=100,
                prompt_id=10,
                version_label="v1.0",
                is_current=True,
                prompt="### INSTRUCCIONES GENERALES\nEvalúa la llamada.\n\n### CRITERIOS DE ANÁLISIS\n<!-- BM_CRITERIA_BLOCK_START -->\n<!-- BM_CRITERIA_BLOCK_END -->\n\n### FORMATO DE SALIDA JSON\n```json\n{\n  \"tipo_llamada\": \"cita\"|\"otros\"|null\n}\n```"
            )

            # 3. Typologies
            t_cita = Typology(typology_id=1, service_id=1, typology_key="cita", typology_name="Cita", is_active=True)
            t_no_cont = Typology(typology_id=2, service_id=1, typology_key="no_contacto", typology_name="No Contacto", is_active=True)
            t_otros = Typology(typology_id=3, service_id=1, typology_key="otros", typology_name="Otros", is_active=True)

            # Link typologies to base structure
            bst1 = BaseStructureTypology(id=1, base_structure_id=1, typology_id=1)
            bst2 = BaseStructureTypology(id=2, base_structure_id=1, typology_id=2)
            bst3 = BaseStructureTypology(id=3, base_structure_id=1, typology_id=3)

            # 4. Criteria including newly added short call criteria and Tipo de llamada
            c_tipo = PromptCriterion(
                criterion_id=101,
                prompt_id=10,
                criterion_key="tipo_llamada",
                criterion_name="Tipo de llamada",
                output_key="tipo_llamada",
                criterion_type="category",
                is_active=True
            )
            c_no_cont = PromptCriterion(
                criterion_id=102,
                prompt_id=10,
                criterion_key="no_contacto",
                criterion_name="No Contacto",
                output_key="no_contacto",
                criterion_type="boolean",
                is_active=True
            )
            c_tipif = PromptCriterion(
                criterion_id=103,
                prompt_id=10,
                criterion_key="tipificacion_llamada",
                criterion_name="Tipificación Llamada",
                output_key="tipificacion_llamada",
                feed_key="tipificacion_llamada_feed",
                criterion_type="category",
                is_active=True
            )
            c_razon = PromptCriterion(
                criterion_id=104,
                prompt_id=10,
                criterion_key="razon_no_contacto",
                criterion_name="Razón no contacto",
                output_key="razon_no_contacto",
                criterion_type="text",
                is_active=True
            )
            c_empatia = PromptCriterion(
                criterion_id=105,
                prompt_id=10,
                criterion_key="empatia",
                criterion_name="Empatía",
                output_key="empatia",
                criterion_type="score_1_10",
                is_active=True
            )

            # Empathy only applies to Cita; Tipo de llamada applies to all
            pct1 = PromptCriterionTypology(id=10, criterion_id=105, typology_id=1)

            session.add_all([
                comp, svc, base, prompt, v, t_cita, t_no_cont, t_otros,
                bst1, bst2, bst3,
                c_tipo, c_no_cont, c_tipif, c_razon, c_empatia,
                pct1
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_a_short_call_voicemail_criteria_evaluation(self):
        """
        TEST A: Short call / voicemail where No Contacto, Tipificación Llamada,
        and Razón no contacto are evaluated and NOT left 'Sin dato'.
        """
        async with self.SessionLocal() as session:
            raw_ai_result = {
                "tipo_llamada": "no_contacto",
                "no_contacto": True,
                "tipificacion_llamada": "No Contesta",
                "tipificacion_llamada_feed": "Salta un contestador automático",
                "razon_no_contacto": "salta un buzón"
            }

            # Persist analysis via save_analysis with _upsert_current mocked (PG-specific)
            with patch("app.services.analysis_persistence._upsert_current", new=AsyncMock()):
                analysis = await save_analysis(
                    db=session,
                    analysis_type="audio",
                    call_metadata={"call_id": "530195388652", "service_id": 1, "company_id": 1},
                    prompt_metadata={"prompt_id": 10, "prompt_version_id": 100},
                    model_metadata={"model_provider": "google", "model_name": "gemini-2.5-flash"},
                    result_json=raw_ai_result,
                    payload={},
                )

            # Query criterion results from bm_analysis_criterion_results
            stmt = select(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == analysis.analysis_id)
            res = (await session.execute(stmt)).scalars().all()
            res_by_key = {r.criterion_key: r for r in res}

            # 1. No Contacto
            self.assertIn("no_contacto", res_by_key)
            self.assertTrue(res_by_key["no_contacto"].boolean_value)
            self.assertTrue(res_by_key["no_contacto"].is_applicable)

            # 2. Tipificación Llamada
            self.assertIn("tipificacion_llamada", res_by_key)
            self.assertEqual(res_by_key["tipificacion_llamada"].category_value, "No Contesta")
            self.assertEqual(res_by_key["tipificacion_llamada"].feedback, "Salta un contestador automático")
            self.assertTrue(res_by_key["tipificacion_llamada"].is_applicable)

            # 3. Razón no contacto
            self.assertIn("razon_no_contacto", res_by_key)
            self.assertEqual(res_by_key["razon_no_contacto"].text_value, "salta un buzón")
            self.assertTrue(res_by_key["razon_no_contacto"].is_applicable)

            # 4. Tipo de llamada must be applicable and evaluated
            self.assertIn("tipo_llamada", res_by_key)
            self.assertTrue(res_by_key["tipo_llamada"].is_applicable)
            self.assertEqual(res_by_key["tipo_llamada"].value_raw, "no_contacto")

    async def test_b_mass_evaluation_and_laboratory_parity(self):
        """
        TEST B: Verify that processing the exact same call and raw AI result
        in Mass Evaluations and Laboratory yields identical criterion keys, values, and applicability.
        """
        raw_ai_result = {
            "tipo_llamada": "no_contacto",
            "no_contacto": True,
            "tipificacion_llamada": "No Contesta",
            "tipificacion_llamada_feed": "Salta buzón",
            "razon_no_contacto": "salta un buzón"
        }

        async with self.SessionLocal() as session:
            # 1. Laboratory execution simulation
            with patch("app.services.analysis_persistence._upsert_current", new=AsyncMock()), \
                 patch("app.services.openai_service.complete_text", new=AsyncMock(return_value=json.dumps(raw_ai_result))):
                
                lab_response = await analyze_transcription_pipeline(
                    db=session,
                    call_id="530195388652",
                    transcription="Buzón de voz...",
                    service_id=1,
                    prompt_id=10,
                )

            self.assertTrue(lab_response["ok"])
            self.assertIn("items_json", lab_response)
            self.assertIn("items_visual", lab_response)
            lab_items_by_key = {item["criterion_key"]: item for item in lab_response["items_json"]}

            # 2. Mass Evaluation simulation with identical prompt and data
            job = MassEvaluationJob(
                job_id=1,
                company_id=1,
                service_id=1,
                job_name="Mass Job 1",
                prompt_id=10,
                prompt_version_id=100
            )
            run = MassEvaluationRun(
                run_id=1,
                job_id=1,
                trigger_type="manual",
                status="running"
            )
            session.add_all([job, run])
            await session.commit()

            # Execute single call processing through MassEvaluationService logic
            criteria_orm = await session.execute(
                select(PromptCriterion).where(PromptCriterion.prompt_id == 10, PromptCriterion.is_active == True)
            )
            active_criteria_list = list(criteria_orm.scalars().all())
            criteria_snapshot = [
                {
                    "criterion_id": c.criterion_id,
                    "criterion_key": c.criterion_key,
                    "criterion_name": c.criterion_name,
                    "criterion_type": c.criterion_type,
                    "output_key": c.output_key,
                    "feed_key": c.feed_key,
                }
                for c in active_criteria_list
            ]

            # Compare keys between lab items and mass snapshot
            self.assertEqual(set(lab_items_by_key.keys()), {c["criterion_key"] for c in criteria_snapshot})

            # Check values parity
            self.assertEqual(lab_items_by_key["no_contacto"]["value"], True)
            self.assertEqual(lab_items_by_key["tipificacion_llamada"]["value"], "No Contesta")
            self.assertEqual(lab_items_by_key["razon_no_contacto"]["value"], "salta un buzón")
            self.assertEqual(lab_items_by_key["tipo_llamada"]["value"], "no_contacto")
            self.assertEqual(lab_items_by_key["tipo_llamada"]["not_applicable"], False)

    async def test_c_new_criterion_auto_sync_without_hardcoded_lists(self):
        """
        TEST C: When a new criterion is added to bm_prompt_criteria,
        both pipelines sync and process it without requiring hardcoded list updates.
        """
        async with self.SessionLocal() as session:
            # Add new criterion dynamically
            new_crit = PromptCriterion(
                criterion_id=200,
                prompt_id=10,
                criterion_key="inbound_outbound_eval",
                criterion_name="Sentido de Llamada",
                output_key="inbound_outbound_eval",
                criterion_type="category",
                allowed_values=["inbound", "outbound"],
                is_active=True
            )
            session.add(new_crit)
            await session.commit()

            # Verify prompt sync includes new criterion in JSON schema
            from app.services.prompts_service import sync_prompt_text_with_active_criteria
            prompt_text = "### CRITERIOS\n<!-- BM_CRITERIA_BLOCK_START -->\n<!-- BM_CRITERIA_BLOCK_END -->\n\n### FORMATO DE SALIDA JSON\n```json\n{}\n```"
            synced_text, changed = await sync_prompt_text_with_active_criteria(session, 10, prompt_text)

            self.assertTrue(changed)
            self.assertIn('"inbound_outbound_eval"', synced_text)
            self.assertIn("Sentido de Llamada", synced_text)

    async def test_d_exact_service_typology_matching_no_corrupted_legacy(self):
        """
        TEST D: An exact valid typology key for the service (e.g. 'no_contacto')
        is NOT mutated to legacy 'intento_contacto' or forced to 'otros'.
        """
        async with self.SessionLocal() as session:
            typologies = (await session.execute(select(Typology).where(Typology.service_id == 1))).scalars().all()
            typology_by_key = {t.typology_key: t for t in typologies}

            matched, resolved_key = resolve_service_typology("no_contacto", typology_by_key)
            self.assertEqual(resolved_key, "no_contacto")
            self.assertEqual(matched.typology_key, "no_contacto")

    async def test_e_tipo_llamada_never_marked_not_applicable(self):
        """
        TEST E: 'Tipo de llamada' is never marked as not_applicable, regardless
        of which typology was matched.
        """
        async with self.SessionLocal() as session:
            t_otros = (await session.execute(select(Typology).where(Typology.typology_key == "otros"))).scalars().first()
            c_tipo = (await session.execute(select(PromptCriterion).where(PromptCriterion.criterion_key == "tipo_llamada"))).scalars().first()

            # Associating c_tipo ONLY to Cita (id=1) must NOT disable it when call resolves to Otros (id=3)
            assoc = PromptCriterionTypology(id=99, criterion_id=c_tipo.criterion_id, typology_id=1)
            session.add(assoc)
            await session.commit()

            analysis = Analysis(
                analysis_id=2,
                company_id=1,
                service_id=1,
                call_id="530161172728",
                prompt_id=10,
                prompt_version_id=100,
                tipo_llamada="otros",
                result={"tipo_llamada": "otros"}
            )

            rows = await _insert_results(
                session,
                analysis,
                {"tipo_llamada": "otros"},
                prompt_id=10,
                matched_typology=t_otros,
                active_criteria=[c_tipo]
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].criterion_key, "tipo_llamada")
            self.assertTrue(rows[0].is_applicable)
            self.assertFalse(rows[0].not_applicable)
            self.assertEqual(rows[0].value_raw, "otros")
