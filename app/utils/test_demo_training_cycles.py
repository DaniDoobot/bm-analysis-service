"""
app/utils/test_demo_training_cycles.py
======================================
Comprehensive test suite for the enhanced training cycles, roleplay simulation prompts,
sessions, evaluations, and backfill for Empresa Demo (company_id=7).

Validates all 12 acceptance criteria:
- PASS 1: Completed cycles have strengths_json populated as a list (no "Sin datos").
- PASS 2: Completed cycles have weaknesses_json populated as a list (no "Sin datos").
- PASS 3: Completed simulations have sessions and evaluations linked (no warning banner).
- PASS 4: Active/pending cycles have >= 3 general objectives with complete fields.
- PASS 5: Active/pending cycles have >= 3 specific objectives with complete fields.
- PASS 6: Simulation prompts are long, detailed roleplay briefs (> 600 chars).
- PASS 7: Simulation prompts contain bot identity, persona, rules, 5-level resistance ladder.
- PASS 8: Content adapts dynamically to agent performance tiers.
- PASS 9: Content has genuine variety across agents (no cloned text).
- PASS 10: Backfill successfully fixes flawed demo data.
- PASS 11: Base calls (MassEvaluationResult) are never modified.
- PASS 12: Boston Medical (company_id=1) remains completely untouched.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# Force DATABASE_URL to a safe local SQLite DB
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///demo_training_cycles_test.db"

# Safety Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingEvaluationPrompt,
)
from app.services.personalized_training_service import PersonalizedTrainingService
from app.services.demo_training_cycle_enhancer import (
    DEMO_COMPANY_ID,
    generate_enhanced_training_cycle_data,
    generate_simulation_evaluation_data,
    build_roleplay_simulation_prompt,
)
from scripts.backfill_demo_training_cycles import backfill_training_cycles


class TestDemoTrainingCycles(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///demo_training_cycles_test.db", echo=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.db = AsyncSession(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.db.close()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()
        if os.path.exists("demo_training_cycles_test.db"):
            try:
                os.remove("demo_training_cycles_test.db")
            except Exception:
                pass

    # ── Unit Tests: Cycle Data Generation & Structure ─────────────────────────

    def test_active_cycle_has_sufficient_objectives(self):
        """PASS 4 & PASS 5: Active cycles have >= 3 general and >= 3 specific objectives."""
        data = generate_enhanced_training_cycle_data(
            agent_id="demo_agent_01",
            agent_name="Laura Gómez",
            agent_initials="LG",
            service_key="atencion-al-cliente",
            status="in_progress"
        )
        gen_objs = data["general_objectives_json"]
        spec_objs = data["specific_objectives_json"]

        self.assertGreaterEqual(len(gen_objs), 3, "Debe tener al menos 3 objetivos generales")
        self.assertGreaterEqual(len(spec_objs), 3, "Debe tener al menos 3 objetivos específicos")

        for obj in gen_objs:
            self.assertTrue(obj.get("title"), "Objetivo general debe tener título")
            self.assertTrue(obj.get("description"), "Objetivo general debe tener descripción")
            self.assertTrue(obj.get("rationale"), "Objetivo general debe tener rationale/justificación")
            self.assertTrue(obj.get("expected_behavior"), "Objetivo general debe tener expected_behavior")
            self.assertGreater(len(obj.get("success_indicators", [])), 0, "Debe tener indicadores de éxito")

        for obj in spec_objs:
            self.assertTrue(obj.get("title"), "Objetivo específico debe tener título")
            self.assertTrue(obj.get("description"), "Objetivo específico debe tener descripción")
            self.assertGreater(len(obj.get("related_criteria", [])), 0, "Debe tener criterios relacionados")
            self.assertTrue(obj.get("specific_behavior_to_improve"), "Debe especificar conducta a mejorar")
            self.assertGreater(len(obj.get("success_indicators", [])), 0, "Debe tener indicadores de éxito")

    def test_completed_cycle_has_strengths_and_weaknesses_list(self):
        """PASS 1 & PASS 2: Completed cycles have list of strengths and weaknesses with evidence."""
        data = generate_enhanced_training_cycle_data(
            agent_id="demo_agent_03",
            agent_name="Marcos Alonso",
            agent_initials="MA",
            service_key="ventas",
            status="completed"
        )
        strengths = data["strengths_json"]
        weaknesses = data["weaknesses_json"]

        self.assertIsInstance(strengths, list, "strengths_json debe ser una lista")
        self.assertIsInstance(weaknesses, list, "weaknesses_json debe ser una lista")
        self.assertGreaterEqual(len(strengths), 2, "Debe incluir al menos 2 fortalezas")
        self.assertGreaterEqual(len(weaknesses), 2, "Debe incluir al menos 2 debilidades")

        for s in strengths:
            self.assertTrue(s.get("title"), "Fortaleza debe tener título")
            self.assertTrue(s.get("description"), "Fortaleza debe tener descripción")
            self.assertTrue(s.get("evidence"), "Fortaleza debe tener evidencia concreta")

        for w in weaknesses:
            self.assertTrue(w.get("title"), "Debilidad debe tener título")
            self.assertTrue(w.get("description"), "Debilidad debe tener descripción")
            self.assertTrue(w.get("evidence"), "Debilidad debe tener evidencia concreta")

        # Check final report has objectives_status
        final_rep = data["final_report_json"]
        self.assertIsNotNone(final_rep, "Ciclo completado debe tener final_report_json")
        self.assertIn("objectives_status", final_rep, "final_report_json debe incluir objectives_status")
        self.assertGreater(len(final_rep["objectives_status"]), 0)
        for os_item in final_rep["objectives_status"]:
            self.assertIn(os_item["status"], ["SUPERADO", "NO SUPERADO"])
            self.assertIn("score", os_item)
            self.assertIn("improvement_delta", os_item)

    def test_simulation_prompt_length_and_structure(self):
        """PASS 6 & PASS 7: Roleplay prompts exceed 600 chars and contain all mandatory sections."""
        prompt = build_roleplay_simulation_prompt(
            prompt_number=1,
            agent_name="Elena Rivas",
            service_key="atencion-al-cliente",
            persona_index=2
        )
        text = prompt["prompt_text"]

        self.assertGreater(len(text), 600, "El prompt de simulación debe superar los 600 caracteres")
        self.assertIn("IDENTIDAD Y ROL DEL BOT", text)
        self.assertIn("PERSONAJE Y ANTECEDENTES", text)
        self.assertIn("REGLAS DE VOZ TELEFÓNICA", text)
        self.assertIn("CONTEXTO Y SITUACIÓN DE LA LLAMADA", text)
        self.assertIn("ESCALERA DE RESISTENCIA Y DIFICULTAD (5 NIVELES)", text)
        self.assertIn("Nivel 1", text)
        self.assertIn("Nivel 5", text)
        self.assertIn("DISPARADORES DE PROGRESIÓN", text)
        self.assertIn("CRITERIOS OBSERVABLES", text)

        # Check focus metadata
        focus = prompt["objective_focus_json"]
        self.assertIn("focus", focus)
        self.assertIn("objective_summary", focus)
        self.assertIn("expected_behavior", focus)

    def test_simulation_evaluation_generation(self):
        """PASS 3: Simulation evaluations contain score, feedback, transcription and criteria."""
        eval_data = generate_simulation_evaluation_data(
            prompt_title="Manejo de Objeción de Precio",
            prompt_number=1,
            agent_name="Sara Blanco",
            service_key="ventas",
            agent_score_tier="top"
        )
        self.assertGreaterEqual(float(eval_data["score"]), 8.0)
        self.assertIn("Agente:", eval_data["transcription"])
        self.assertIn("Paciente:", eval_data["transcription"])
        self.assertIn("score", eval_data["result_json"])
        self.assertIn("result_json", eval_data["result_json"])
        self.assertTrue(eval_data["result_json"]["result_json"]["agendar_cita"])
        self.assertGreater(len(eval_data["strengths"]), 0)
        self.assertGreater(len(eval_data["weaknesses"]), 0)

    def test_content_variety_across_agents(self):
        """PASS 8 & PASS 9: Dynamic adaptation to tiers and variety across agents."""
        c1 = generate_enhanced_training_cycle_data("demo_agent_03", "Agente 3", "A3", "atencion-al-cliente", "completed")
        c2 = generate_enhanced_training_cycle_data("demo_agent_10", "Agente 10", "A10", "ventas", "completed")

        self.assertEqual(c1["tier"], "top")
        self.assertEqual(c2["tier"], "developing")
        self.assertNotEqual(c1["summary_general"], c2["summary_general"], "Los resúmenes deben variar")
        self.assertNotEqual(c1["prompts"][0]["prompt_text"], c2["prompts"][0]["prompt_text"], "Los prompts deben variar")

    def test_mapper_serialization_avoids_sin_datos(self):
        """Integration check with PersonalizedTrainingService._map_report_to_dict."""
        cycle_data = generate_enhanced_training_cycle_data(
            agent_id="demo_agent_01",
            agent_name="Ana Soto",
            agent_initials="AS",
            service_key="atencion-al-cliente",
            status="completed"
        )
        report = TrainingAgentReport(
            training_report_id=101,
            company_id=7,
            hubspot_owner_id="demo_agent_01",
            agent_name="Ana Soto",
            agent_initials="AS",
            period_start=datetime.now(timezone.utc) - timedelta(days=30),
            period_end=datetime.now(timezone.utc),
            status="completed",
            evaluations_count=10,
            calls_count=10,
            avg_evaluacion_global=cycle_data["avg_score"],
            summary_general=cycle_data["summary_general"],
            evolution_summary=cycle_data["evolution_summary"],
            strengths_json=cycle_data["strengths_json"],
            weaknesses_json=cycle_data["weaknesses_json"],
            notable_data_json=cycle_data["notable_data_json"],
            general_objectives_json=cycle_data["general_objectives_json"],
            specific_objectives_json=cycle_data["specific_objectives_json"],
            final_report_json=cycle_data["final_report_json"],
            is_current=True,
        )

        prompt = TrainingSimulationPrompt(
            simulation_prompt_id=501,
            training_report_id=101,
            hubspot_owner_id="demo_agent_01",
            prompt_number=1,
            title="Simulación 1",
            scenario_type="roleplay",
            objective_focus_json=cycle_data["prompts"][0]["objective_focus_json"],
            prompt_text=cycle_data["prompts"][0]["prompt_text"],
        )

        ev_data = generate_simulation_evaluation_data("Sim 1", 1, "Ana Soto", "atencion-al-cliente", "top")
        evaluation = TrainingCallEvaluation(
            evaluation_id=801,
            session_id=701,
            cycle_id=101,
            conversation_id=501,
            agent_id="demo_agent_01",
            prompt_version_id=1,
            transcription=ev_data["transcription"],
            result_json=ev_data["result_json"],
            score=ev_data["score"],
            feedback=ev_data["feedback"],
        )

        comp = TrainingCompletionStatus(
            completion_id=901,
            training_report_id=101,
            simulation_prompt_id=501,
            hubspot_owner_id="demo_agent_01",
            status="completed",
            evaluation_id=801,
            call_session_id=701,
        )
        comp.evaluation = evaluation

        mapped = PersonalizedTrainingService._map_report_to_dict(
            report, prompts=[prompt], completions=[comp]
        )

        self.assertIsNotNone(mapped)
        # Verify PASS 1 & 2: Strengths and Weaknesses are NOT empty
        self.assertGreater(len(mapped["strengths_json"]), 0, "PASS 1: Fortalezas no debe estar vacío")
        self.assertGreater(len(mapped["weaknesses_json"]), 0, "PASS 2: Áreas de mejora no debe estar vacío")
        self.assertNotEqual(mapped["strengths_json"][0]["title"], "Fortalezas: Sin datos")

        # Verify PASS 3: Simulation has score, feedback, and criteria
        sim = mapped["simulations"][0]
        self.assertEqual(sim["status"], "completed")
        self.assertIsNotNone(sim["evaluation_id"], "PASS 3: Debe tener evaluation_id")
        self.assertIsNotNone(sim["score"], "PASS 3: Debe tener score")
        self.assertIsNotNone(sim["feedback"], "PASS 3: Debe tener feedback")

        # Verify PASS 4 & 5: Objectives are evaluated and mapped
        self.assertGreaterEqual(len(mapped["general_objectives_json"]), 3)
        self.assertGreaterEqual(len(mapped["specific_objectives_json"]), 3)
        self.assertTrue(mapped["general_objectives_json"][0]["is_evaluated"])
        self.assertEqual(mapped["general_objectives_json"][0]["status"], "SUPERADO")

    # ── Database Tests: Backfill Execution, Safety Airlock & Tenancy ──────────

    async def test_backfill_database_execution_and_airlock(self):
        """PASS 10, PASS 11 & PASS 12: Backfill updates demo company, preserves base calls and isolates tenant."""
        # 1. Setup Company 7 (Empresa Demo) and Company 1 (Boston Medical)
        c7 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
        c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False)
        self.db.add_all([c7, c1])
        await self.db.flush()

        # Services for Company 7
        svc_at = Service(service_id=701, company_id=7, service_name="Atención al Cliente", service_key="atencion-al-cliente")
        svc_vn = Service(service_id=702, company_id=7, service_name="Ventas", service_key="ventas")
        # Service for Company 1
        svc_bm = Service(service_id=101, company_id=1, service_name="Servicio BM", service_key="servicio-bm")
        self.db.add_all([svc_at, svc_vn, svc_bm])
        await self.db.flush()

        job = MassEvaluationJob(job_id=1, company_id=7, service_id=701, job_name="Job Demo", prompt_id=1, prompt_name="Prompt Demo")
        self.db.add(job)
        await self.db.flush()

        run_eval = MassEvaluationRun(run_id=1, job_id=1, company_id=7, service_id=701, trigger_type="manual", status="completed")
        self.db.add(run_eval)
        await self.db.flush()

        # Base Calls for Company 7 (MUST NOT BE TOUCHED - PASS 11)
        call_demo = MassEvaluationResult(
            run_id=1,
            job_id=1,
            company_id=7,
            call_id="demo_sep26_agent01_20260915_001",
            hubspot_owner_id="demo_agent_01",
            agent_name="Agente Demo 1",
            call_duration_seconds=180,
            prompt_id=1,
            prompt_snapshot="Snapshot test",
            status="completed",
        )
        self.db.add(call_demo)
        await self.db.flush()

        # Create flawed legacy report for Company 7 (Run 1 completed)
        rep_c7 = TrainingAgentReport(
            training_report_id=1,
            training_run_id=1,
            company_id=7,
            service_id=701,
            hubspot_owner_id="demo_agent_01",
            agent_name="Agente Demo 1",
            agent_initials="A1",
            period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 9, 15, tzinfo=timezone.utc),
            status="completed",
            strengths_json={"fortalezas": ["Empatía básica"]},  # FLAWED DICT
            weaknesses_json={"puntos_mejora": ["Objeciones"]},  # FLAWED DICT
            general_objectives_json={"objetivos": [{"titulo": "Cierre"}]},  # FLAWED DICT
            specific_objectives_json=None,  # MISSING
            final_report_json={"conclusiones": "Fin."},  # MISSING objectives_status
            is_current=True,
        )
        self.db.add(rep_c7)
        await self.db.flush()

        # Flawed prompt and completion (status completed but missing session and evaluation)
        prompt_c7 = TrainingSimulationPrompt(
            simulation_prompt_id=10,
            training_report_id=1,
            hubspot_owner_id="demo_agent_01",
            prompt_number=1,
            title="Simulacion 1",
            scenario_type="roleplay",
            prompt_text="Simula una llamada corta.",  # SHORT PLACEHOLDER
        )
        self.db.add(prompt_c7)
        await self.db.flush()

        comp_c7 = TrainingCompletionStatus(
            completion_id=20,
            training_report_id=1,
            simulation_prompt_id=10,
            hubspot_owner_id="demo_agent_01",
            status="completed",
            call_session_id=None,  # FLAWED: NO SESSION
            evaluation_id=None,    # FLAWED: NO EVALUATION
        )
        self.db.add(comp_c7)
        await self.db.flush()

        # Create a report for Company 1 (Boston Medical - MUST NOT BE TOUCHED - PASS 12)
        bm_summary_original = "Resumen intocable de Boston Medical"
        rep_c1 = TrainingAgentReport(
            training_report_id=99,
            training_run_id=99,
            company_id=1,
            service_id=101,
            hubspot_owner_id="bm_agent_01",
            agent_name="BM Agent 1",
            agent_initials="BM",
            period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 9, 15, tzinfo=timezone.utc),
            status="completed",
            summary_general=bm_summary_original,
            strengths_json=[{"title": "BM Strength", "description": "Desc", "evidence": "Ev"}],
            weaknesses_json=[{"title": "BM Weakness", "description": "Desc", "evidence": "Ev"}],
            is_current=True,
        )
        self.db.add(rep_c1)
        await self.db.commit()

        # ── Execute Backfill with --apply ─────────────────────────────────────
        stats = await backfill_training_cycles(self.db, apply=True)

        self.assertEqual(stats["reports_updated"], 1)
        self.assertEqual(stats["completed_reports_enriched"], 1)
        self.assertGreaterEqual(stats["sessions_created"], 1)
        self.assertGreaterEqual(stats["evaluations_created"], 1)

        # ── Verify PASS 10: Company 7 data is upgraded ────────────────────────
        await self.db.refresh(rep_c7)
        self.assertIsInstance(rep_c7.strengths_json, list, "strengths_json debe ser ahora una lista")
        self.assertIsInstance(rep_c7.weaknesses_json, list, "weaknesses_json debe ser ahora una lista")
        self.assertGreaterEqual(len(rep_c7.general_objectives_json), 3)
        self.assertGreaterEqual(len(rep_c7.specific_objectives_json), 3)
        self.assertIn("objectives_status", rep_c7.final_report_json)

        await self.db.refresh(prompt_c7)
        self.assertGreater(len(prompt_c7.prompt_text), 600, "El prompt debe tener >600 caracteres")

        await self.db.refresh(comp_c7)
        self.assertEqual(comp_c7.status, "completed")
        self.assertIsNotNone(comp_c7.call_session_id, "Debe tener call_session_id")
        self.assertIsNotNone(comp_c7.evaluation_id, "Debe tener evaluation_id")

        # Verify session and evaluation exist in DB
        sess = await self.db.get(TrainingCallSession, comp_c7.call_session_id)
        self.assertIsNotNone(sess)
        self.assertEqual(sess.cycle_id, 1)

        eval_obj = await self.db.get(TrainingCallEvaluation, comp_c7.evaluation_id)
        self.assertIsNotNone(eval_obj)
        self.assertIsNotNone(eval_obj.score)
        self.assertIn("Agente:", eval_obj.transcription)

        # ── Verify PASS 11: Base Calls were NOT modified ──────────────────────
        call_check = (await self.db.execute(select(MassEvaluationResult).where(MassEvaluationResult.company_id == 7))).scalars().first()
        self.assertIsNotNone(call_check)
        self.assertEqual(call_check.call_id, "demo_sep26_agent01_20260915_001")
        self.assertEqual(call_check.status, "completed")

        # ── Verify PASS 12: Boston Medical (Company 1) remains untouched ───────
        await self.db.refresh(rep_c1)
        self.assertEqual(rep_c1.summary_general, bm_summary_original, "Boston Medical no debe ser alterado")
        self.assertEqual(rep_c1.company_id, 1)

        # ── Idempotency Verification ──────────────────────────────────────────
        # Running backfill a second time should succeed without duplicating records
        stats_second_run = await backfill_training_cycles(self.db, apply=True)
        self.assertEqual(stats_second_run["reports_updated"], 1)

        # Check that session count and evaluation count for this report are still 2 (one per prompt)
        sess_count = (await self.db.execute(
            select(func.count(TrainingCallSession.session_id)).where(TrainingCallSession.cycle_id == 1)
        )).scalar()
        self.assertEqual(sess_count, 2, "No debe duplicar sesiones de llamada en ejecuciones idempotentes")


if __name__ == "__main__":
    unittest.main()
