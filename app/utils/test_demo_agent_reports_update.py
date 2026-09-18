"""
app/utils/test_demo_agent_reports_update.py
===========================================
Focused, fast unit test for TrainingAgentReport descriptive data updates
in Empresa Demo (company_id=7).
Runs fully in-memory via SQLite (< 5 seconds).
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
import unittest
from unittest.mock import MagicMock

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Numeric, Text, ForeignKey, select
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

from app.services.personalized_training_service import PersonalizedTrainingService
from app.services.demo_training_cycle_enhancer import (
    DEMO_COMPANY_ID,
    generate_enhanced_training_cycle_data,
    OBJECTIVES_CATALOG,
    _SW_POOL,
)
from scripts.update_demo_agent_reports import (
    update_agent_reports,
    MEDICAL_TERM_REGEX,
    GENERIC_JUSTIFICATION,
)

# SQLite JSONB shim
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


class TestDefensiveNormalization(unittest.TestCase):
    """Verifies that _map_report_to_dict correctly unpacks dicts and fallbacks."""

    def test_dict_wrapped_strengths_and_weaknesses(self):
        """Dict-wrapped strengths and weaknesses should be normalized to lists, not 'Sin datos.'"""
        mock_report = MagicMock()
        mock_report.training_report_id = 1
        mock_report.training_run_id = 1
        mock_report.hubspot_owner_id = "demo_agent_01"
        mock_report.agent_name = "Agente 1"
        mock_report.agent_initials = "A1"
        mock_report.period_start = None
        mock_report.period_end = None
        mock_report.status = "completed"
        mock_report.skipped_reason = None
        mock_report.evaluations_count = 3
        mock_report.calls_count = 3
        mock_report.avg_evaluacion_global = Decimal("8.20")
        mock_report.summary_general = "Resumen"
        mock_report.evolution_summary = "Evolución"
        mock_report.is_current = True
        mock_report.created_at = datetime.now(timezone.utc)
        mock_report.generated_at = datetime.now(timezone.utc)
        mock_report.error_message = None

        # Dict-wrapped!
        mock_report.strengths_json = {
            "fortalezas": [
                {"title": "Escucha Activa", "description": "No interrumpe", "evidence": "Evidencia 1"}
            ]
        }
        mock_report.weaknesses_json = {
            "areas_mejora": [
                {"title": "Cierre Rápido", "description": "Mejorar cierre", "evidence": "Evidencia 2"}
            ]
        }
        mock_report.notable_data_json = {
            "notable_data": [
                {"title": "Alto FCR", "description": "FCR 85%", "metric_or_pattern": "85%"}
            ]
        }
        mock_report.general_objectives_json = [
            {
                "title": "Objetivo G1",
                "description": "Desc G1",
                "rationale": "Rat G1",
                "expected_behavior": "Exp G1",
                "success_indicators": ["Ind 1"],
            }
        ]
        mock_report.specific_objectives_json = [
            {
                "title": "Objetivo E1",
                "description": "Desc E1",
                "related_criteria": ["Crit 1"],
                "specific_behavior_to_improve": "Beh 1",
                "success_indicators": ["Ind 2"],
            }
        ]
        mock_report.final_report_json = {
            "summary_final": "Resumen final",
            "objectives_status": [
                {
                    "title": "Objetivo G1",
                    "type": "general",
                    "status": "SUPERADO",
                    "base_score": 7.0,
                    "score": 8.5,
                    "improvement_delta": 1.5,
                    "justification": "Excelente evolución en las simulaciones de exploración.",
                },
                {
                    "title": "Objetivo E1",
                    "type": "specific",
                    "status": "SUPERADO",
                    "base_score": 6.8,
                    "score": 8.2,
                    "improvement_delta": 1.4,
                    "justification": "Aplica la doble alternativa en el 90% de los cierres.",
                },
            ]
        }

        mapped = PersonalizedTrainingService._map_report_to_dict(mock_report, prompts=None, completions=None)

        # Strengths & Weaknesses must be populated lists, NOT empty!
        self.assertEqual(len(mapped["strengths_json"]), 1)
        self.assertEqual(mapped["strengths_json"][0]["title"], "Escucha Activa")
        self.assertEqual(len(mapped["weaknesses_json"]), 1)
        self.assertEqual(mapped["weaknesses_json"][0]["title"], "Cierre Rápido")

        # Objectives must have individual justifications from final_report_json
        self.assertEqual(len(mapped["general_objectives_json"]), 1)
        self.assertEqual(mapped["general_objectives_json"][0]["status"], "SUPERADO")
        self.assertEqual(mapped["general_objectives_json"][0]["justification"],
                         "Excelente evolución en las simulaciones de exploración.")

        self.assertEqual(len(mapped["specific_objectives_json"]), 1)
        self.assertEqual(mapped["specific_objectives_json"][0]["status"], "SUPERADO")
        self.assertEqual(mapped["specific_objectives_json"][0]["justification"],
                         "Aplica la doble alternativa en el 90% de los cierres.")

    def test_fallback_from_final_report(self):
        """When strengths_json is None/empty, fallback to final_report_json.strengths."""
        mock_report = MagicMock()
        mock_report.training_report_id = 2
        mock_report.training_run_id = 1
        mock_report.hubspot_owner_id = "demo_agent_02"
        mock_report.agent_name = "Agente 2"
        mock_report.agent_initials = "A2"
        mock_report.period_start = None
        mock_report.period_end = None
        mock_report.status = "completed"
        mock_report.skipped_reason = None
        mock_report.evaluations_count = 3
        mock_report.calls_count = 3
        mock_report.avg_evaluacion_global = Decimal("8.00")
        mock_report.summary_general = "Resumen"
        mock_report.evolution_summary = "Evolución"
        mock_report.is_current = True
        mock_report.created_at = datetime.now(timezone.utc)
        mock_report.generated_at = datetime.now(timezone.utc)
        mock_report.error_message = None
        mock_report.strengths_json = None
        mock_report.weaknesses_json = []
        mock_report.notable_data_json = []
        mock_report.general_objectives_json = []
        mock_report.specific_objectives_json = []
        mock_report.final_report_json = {
            "strengths": [{"title": "Fortaleza Fallback", "description": "D", "evidence": "E"}],
            "weaknesses": [{"title": "Mejora Fallback", "description": "D", "evidence": "E"}],
            "objectives_status": [],
        }

        mapped = PersonalizedTrainingService._map_report_to_dict(mock_report, prompts=None, completions=None)
        self.assertEqual(len(mapped["strengths_json"]), 1)
        self.assertEqual(mapped["strengths_json"][0]["title"], "Fortaleza Fallback")
        self.assertEqual(len(mapped["weaknesses_json"]), 1)
        self.assertEqual(mapped["weaknesses_json"][0]["title"], "Mejora Fallback")


class TestEnhancedDataGenerator(unittest.TestCase):
    """Verifies that generate_enhanced_training_cycle_data produces 3-4 strengths/weaknesses and zero medical content."""

    def test_60_agents_generation(self):
        for agent_num in range(1, 61):
            agent_id = f"demo_agent_{agent_num:02d}"
            service_key = "ventas" if agent_num % 2 == 0 else "atencion-al-cliente"
            status = "completed" if agent_num <= 48 else "in_progress"

            data = generate_enhanced_training_cycle_data(
                agent_id=agent_id,
                agent_name=f"Agente {agent_num}",
                agent_initials=f"A{agent_num}",
                service_key=service_key,
                status=status,
            )

            # 1. Strengths: 3 to 4
            self.assertIn(len(data["strengths_json"]), (3, 4),
                          f"Agente {agent_num} debe tener 3 o 4 fortalezas")

            # 2. Weaknesses: 3 to 4
            self.assertIn(len(data["weaknesses_json"]), (3, 4),
                          f"Agente {agent_num} debe tener 3 o 4 áreas de mejora")

            # 3. Objectives: 3 to 4 each
            self.assertIn(len(data["general_objectives_json"]), (3, 4))
            self.assertIn(len(data["specific_objectives_json"]), (3, 4))

            # 4. Zero medical terms
            full_text = (
                f"{data['summary_general']} {data['evolution_summary']} "
                f"{json.dumps(data['strengths_json'])} {json.dumps(data['weaknesses_json'])} "
                f"{json.dumps(data['general_objectives_json'])} {json.dumps(data['specific_objectives_json'])} "
                f"{json.dumps(data['final_report_json'] or {})}"
            )
            med = MEDICAL_TERM_REGEX.findall(full_text)
            self.assertEqual(len(med), 0, f"Agente {agent_num} contiene términos médicos: {med}")

            # 5. For completed reports: individual justifications, NO generic template
            if status == "completed":
                final = data["final_report_json"]
                self.assertIsNotNone(final)
                objs_status = final.get("objectives_status", [])
                self.assertGreaterEqual(len(objs_status), 6)
                for obj in objs_status:
                    just = obj.get("justification", "")
                    self.assertTrue(len(just) > 20, f"Justificación vacía en agente {agent_num}")
                    self.assertNotEqual(just, GENERIC_JUSTIFICATION,
                                        f"Agente {agent_num} usa justificación genérica repetida!")


class TestUpdateAgentReportsScript(unittest.TestCase):
    """In-memory SQLite test for update_demo_agent_reports script."""

    def test_script_dry_run_and_apply(self):
        asyncio.run(self._async_test_flow())

    async def _async_test_flow(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

        # Create minimal tables
        async with engine.begin() as conn:
            from app.models.companies import Company
            from app.models.services import Service
            from app.models.personalized_training import (
                TrainingAgentReport,
                TrainingSimulationPrompt,
                TrainingCallSession,
                TrainingCallEvaluation,
                TrainingCompletionStatus,
            )
            await conn.run_sync(Company.metadata.create_all)

        async with AsyncSession(engine) as session:
            # Seed companies
            c_demo = Company(company_id=DEMO_COMPANY_ID, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            c_bm = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False)
            session.add_all([c_demo, c_bm])

            # Seed services
            s1 = Service(service_id=701, company_id=DEMO_COMPANY_ID, service_name="Atención", service_key="atencion-al-cliente")
            s2 = Service(service_id=702, company_id=DEMO_COMPANY_ID, service_name="Ventas", service_key="ventas")
            session.add_all([s1, s2])

            # Seed 60 reports for Company 7 (with old medical and dict-wrapped data)
            p_start = datetime(2026, 8, 1, tzinfo=timezone.utc)
            p_end = datetime(2026, 8, 31, tzinfo=timezone.utc)
            for i in range(1, 61):
                st = "completed" if i <= 48 else "in_progress"
                svc_id = 701 if i % 2 != 0 else 702
                rep = TrainingAgentReport(
                    training_report_id=i,
                    company_id=DEMO_COMPANY_ID,
                    training_run_id=1,
                    service_id=svc_id,
                    hubspot_owner_id=f"demo_agent_{i:02d}",
                    agent_name=f"Agente {i}",
                    agent_initials=f"A{i}",
                    period_start=p_start,
                    period_end=p_end,
                    status=st,
                    avg_evaluacion_global=Decimal("8.00"),
                    summary_general="Se consolidan las fortalezas en argumentación médica y tratamientos.",
                    evolution_summary="Evolución en consulta clínica.",
                    strengths_json={"fortalezas": [{"title": "Médico", "description": "D", "evidence": "E"}]},
                    weaknesses_json={"areas_mejora": [{"title": "Urólogo", "description": "D", "evidence": "E"}]},
                    notable_data_json=[{"title": "D", "description": "D", "metric_or_pattern": "M"}],
                    general_objectives_json=[{"title": "Objetivo Médico 1", "description": "D"}],
                    specific_objectives_json=[{"title": "Objetivo Clínico 1", "description": "D"}],
                    final_report_json={
                        "objectives_status": [
                            {"title": "Objetivo Médico 1", "justification": GENERIC_JUSTIFICATION}
                        ]
                    } if st == "completed" else None
                )
                session.add(rep)

            # Seed 1 report for Company 1 (Boston Medical) to verify isolation
            rep_bm = TrainingAgentReport(
                training_report_id=999,
                company_id=1,
                training_run_id=1,
                service_id=101,
                hubspot_owner_id="bm_agent_01",
                agent_name="BM Agente",
                agent_initials="BM",
                period_start=p_start,
                period_end=p_end,
                status="completed",
                summary_general="Informe real de Boston Medical con términos médicos legítimos.",
            )
            session.add(rep_bm)

            # Seed 2 prompts, 2 sessions, 2 evaluations
            prompt = TrainingSimulationPrompt(
                simulation_prompt_id=1001,
                training_report_id=1,
                hubspot_owner_id="demo_agent_01",
                prompt_number=1,
                title="Prompt ATC",
                scenario_type="ATC",
                prompt_text="Prompt text contact center",
                objective_focus_json={"focus": ["FCR"]},
            )
            session.add(prompt)

            await session.commit()

            # ── 1. Dry-Run Verification ──────────────────────────────────────
            stats_dry = await update_agent_reports(session, apply=False)
            self.assertEqual(stats_dry["reports_detected"], 60)
            self.assertEqual(stats_dry["reports_updated"], 60)
            self.assertEqual(stats_dry["reports_medical_after"], 0)
            self.assertEqual(stats_dry["reports_generic_justification_after"], 0)
            self.assertEqual(stats_dry["prompts_modified"], 0)

            # Verify that DB still has old medical content because of ROLLBACK
            res = await session.execute(
                select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 1)
            )
            rep1 = res.scalars().first()
            self.assertIn("médica", rep1.summary_general)

            # ── 2. Apply Verification ────────────────────────────────────────
            stats_apply = await update_agent_reports(session, apply=True)
            self.assertEqual(stats_apply["reports_detected"], 60)
            self.assertEqual(stats_apply["reports_updated"], 60)
            self.assertEqual(stats_apply["reports_medical_after"], 0)
            self.assertEqual(stats_apply["reports_generic_justification_after"], 0)

            # Verify in DB: rep 1 has new B2B Contact Center content!
            res = await session.execute(
                select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 1)
            )
            rep1_updated = res.scalars().first()
            self.assertNotIn("médica", rep1_updated.summary_general)
            self.assertIn(len(rep1_updated.strengths_json), (3, 4))
            self.assertIn(len(rep1_updated.weaknesses_json), (3, 4))
            self.assertIsInstance(rep1_updated.strengths_json, list)
            self.assertIsInstance(rep1_updated.weaknesses_json, list)

            # Verify Company 1 (Boston Medical) was NOT touched
            res_bm = await session.execute(
                select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 999)
            )
            bm_rep = res_bm.scalars().first()
            self.assertIn("Boston Medical", bm_rep.summary_general)

            # Verify Prompt was NOT touched
            res_p = await session.execute(
                select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.simulation_prompt_id == 1001)
            )
            p_check = res_p.scalars().first()
            self.assertEqual(p_check.title, "Prompt ATC")


if __name__ == "__main__":
    unittest.main()
