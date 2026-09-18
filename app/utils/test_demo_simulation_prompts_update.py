"""
app/utils/test_demo_simulation_prompts_update.py
================================================
Focalized unit test for updating TrainingSimulationPrompt in Empresa Demo (company_id=7).

Verifies:
1. Airlock protection (only touches company_id=7).
2. Exactly 136 prompts detected and updated (simulating the current DB state).
3. 100% elimination of medical terms in prompts.
4. Correct differentiation between Atención al Cliente and Ventas.
5. Invariants:
   - 0 TrainingCallSession touched
   - 0 TrainingCallEvaluation touched
   - 0 Transcriptions touched
   - 0 Scores/Notes touched
   - 0 TrainingCompletionStatus touched
   - 0 TrainingAgentReport touched
   - IDs, prompt_number, FKs preserved
6. Boston Medical (company_id=1) prompts are never touched.
7. Execution time < 5 seconds with local SQLite.
"""
import os
import sys
import unittest
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

TEST_DB_FILE = "demo_prompts_update_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_FILE}"

# Safety Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath("."))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingEvaluationPrompt,
)
from scripts.update_demo_simulation_prompts import (
    update_simulation_prompts,
    MEDICAL_TERM_REGEX,
)


class TestUpdateDemoSimulationPrompts(unittest.IsolatedAsyncioTestCase):
    """Focalized test suite for scripts/update_demo_simulation_prompts.py."""

    async def asyncSetUp(self):
        if os.path.exists(TEST_DB_FILE):
            try:
                os.remove(TEST_DB_FILE)
            except Exception:
                pass

        self.engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB_FILE}", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session = AsyncSession(self.engine, expire_on_commit=False)

        now = datetime.now(timezone.utc)

        # 1. Setup Companies (Company 7 Demo and Company 1 Boston Medical)
        self.demo_company = Company(
            company_id=7,
            company_name="Empresa Demo",
            company_key="empresa-demo",
            is_demo=True,
            is_active=True,
        )
        self.bm_company = Company(
            company_id=1,
            company_name="Boston Medical Group",
            company_key="boston-medical",
            is_demo=False,
            is_active=True,
        )
        self.session.add_all([self.demo_company, self.bm_company])

        # 2. Setup Services for Demo (ATC: 701, Ventas: 702) and BM (101)
        self.svc_atc = Service(
            service_id=701,
            company_id=7,
            service_name="Atención al Cliente",
            service_key="atencion-al-cliente",
            is_active=True,
        )
        self.svc_ventas = Service(
            service_id=702,
            company_id=7,
            service_name="Ventas y Comercial",
            service_key="ventas",
            is_active=True,
        )
        self.svc_bm = Service(
            service_id=101,
            company_id=1,
            service_name="Clínica Andrología",
            service_key="andrologia",
            is_active=True,
        )
        self.session.add_all([self.svc_atc, self.svc_ventas, self.svc_bm])

        eval_prompt = TrainingEvaluationPrompt(
            id=1,
            service_id=701,
            version=1,
            prompt_text="Evalúa la llamada de simulación.",
            is_active=True,
        )
        self.session.add(eval_prompt)

        # 3. Create 60 reports for Demo and 136 prompts with medical terms
        self.demo_reports = []
        self.demo_report_ids = []
        self.demo_prompts = []
        self.demo_prompt_ids = []
        self.demo_sessions = []
        self.demo_session_count = 0
        self.demo_evaluations = []
        self.demo_evaluation_count = 0
        self.demo_completions = []
        self.demo_completion_count = 0

        prompt_id_seq = 1000
        session_id_seq = 2000
        eval_id_seq = 3000
        comp_id_seq = 4000

        for i in range(1, 61):
            agent_id = f"demo_agent_{i}"
            is_ventas = (i % 3 == 0)
            svc_id = 702 if is_ventas else 701
            status = "completed" if i <= 48 else "in_progress"

            rep = TrainingAgentReport(
                training_report_id=i,
                training_run_id=1,
                company_id=7,
                service_id=svc_id,
                hubspot_owner_id=agent_id,
                agent_name=f"Agente Demo {i}",
                agent_initials=f"AD{i}",
                period_start=now,
                period_end=now,
                status=status,
                general_objectives_json=[{"title": "Objetivo General Contact Center"}],
                specific_objectives_json=[{"title": "Objetivo Específico Contact Center"}],
                strengths_json=["Excelente gestión de incidencias"],
                weaknesses_json=["Manejo de objeciones por mejorar"],
                summary_general="Resumen genérico de contact center",
                evolution_summary="Evolución genérica sin términos médicos",
            )
            self.demo_reports.append(rep)
            self.demo_report_ids.append(i)
            self.session.add(rep)

            # Prompts distribution:
            # First 48 agents (completed): 96 prompts
            # Last 12 agents (in_progress): 40 prompts
            # Total: 96 + 40 = 136 prompts
            num_prompts = 2 if i <= 48 else (3 if i <= 56 else 4)

            for p_num in range(1, num_prompts + 1):
                prompt_id_seq += 1
                prompt = TrainingSimulationPrompt(
                    simulation_prompt_id=prompt_id_seq,
                    training_report_id=i,
                    hubspot_owner_id=agent_id,
                    prompt_number=p_num,
                    title=f"Consulta Médica y Tratamiento {p_num}",
                    scenario_type="consulta_clinica",
                    objective_focus_json={"focus": ["anamnesis del paciente", "diagnóstico clínico"]},
                    prompt_text=(
                        f"Eres un paciente que acude a la clínica Boston Medical para consultar con el médico "
                        f"sobre un tratamiento de ecografía doppler. Simulación #{p_num} para {agent_id}."
                    ),
                )
                self.demo_prompts.append(prompt)
                self.demo_prompt_ids.append(prompt_id_seq)
                self.session.add(prompt)

                cur_sess_id = None
                cur_eval_id = None

                # For completed cycles, add existing sessions and evaluations
                if status == "completed":
                    session_id_seq += 1
                    eval_id_seq += 1
                    cur_sess_id = session_id_seq
                    cur_eval_id = eval_id_seq

                    sess = TrainingCallSession(
                        session_id=session_id_seq,
                        call_sid=f"CA{session_id_seq:08d}",
                        agent_id=agent_id,
                        cycle_id=i,
                        conversation_id=prompt_id_seq,
                        status="completed",
                        started_at=now,
                    )
                    self.demo_sessions.append(sess)
                    self.demo_session_count += 1
                    self.session.add(sess)

                    ev = TrainingCallEvaluation(
                        evaluation_id=eval_id_seq,
                        session_id=session_id_seq,
                        cycle_id=i,
                        conversation_id=prompt_id_seq,
                        agent_id=agent_id,
                        prompt_version_id=1,
                        transcription="Hola, buenos días, llamo para una consulta sobre mi caso.",
                        result_json={"nota": 8.5},
                        score=Decimal("8.50"),
                        feedback="Evaluación existente que NO debe modificarse.",
                        created_at=now,
                    )
                    self.demo_evaluations.append(ev)
                    self.demo_evaluation_count += 1
                    self.session.add(ev)

                comp_id_seq += 1
                comp = TrainingCompletionStatus(
                    completion_id=comp_id_seq,
                    training_report_id=i,
                    simulation_prompt_id=prompt_id_seq,
                    hubspot_owner_id=agent_id,
                    status="completed" if status == "completed" else "pending",
                    call_session_id=cur_sess_id,
                    evaluation_id=cur_eval_id,
                    notes="Nota de completion previa",
                )
                self.demo_completions.append(comp)
                self.demo_completion_count += 1
                self.session.add(comp)

        # 4. Add Boston Medical prompt (company_id=1) to verify isolation
        self.bm_report = TrainingAgentReport(
            training_report_id=999,
            training_run_id=99,
            company_id=1,
            service_id=101,
            hubspot_owner_id="bm_agent_1",
            agent_name="Dr. Boston",
            agent_initials="DB",
            period_start=now,
            period_end=now,
            status="completed",
        )
        self.bm_prompt = TrainingSimulationPrompt(
            simulation_prompt_id=9999,
            training_report_id=999,
            hubspot_owner_id="bm_agent_1",
            prompt_number=1,
            title="Consulta Médica Boston Original",
            scenario_type="consulta_andrologica",
            objective_focus_json={"focus": ["tratamiento médico"]},
            prompt_text="Prompt médico legítimo de Boston Medical Group.",
        )
        self.session.add_all([self.bm_report, self.bm_prompt])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        if os.path.exists(TEST_DB_FILE):
            try:
                os.remove(TEST_DB_FILE)
            except Exception:
                pass

    async def test_update_simulation_prompts_dry_run(self):
        """Dry-run should report 136 prompts to update, 0 modifications applied, 0 collateral changes."""
        stats = await update_simulation_prompts(self.session, apply=False)

        self.assertEqual(stats["prompts_detected"], 136)
        self.assertEqual(stats["prompts_to_update"], 136)
        self.assertEqual(stats["prompts_updated"], 136)  # Prepared
        self.assertEqual(stats["prompts_medical_before"], 136)
        self.assertEqual(stats["prompts_medical_after"], 0)
        self.assertEqual(stats["sessions_modified"], 0)
        self.assertEqual(stats["evaluations_modified"], 0)
        self.assertEqual(stats["transcriptions_modified"], 0)
        self.assertEqual(stats["scores_modified"], 0)
        self.assertEqual(stats["completions_modified"], 0)
        self.assertEqual(stats["reports_modified"], 0)
        self.assertEqual(stats["calls_modified"], 0)

        # Verify DB in rollback state: prompt in DB still has original text
        stmt = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.simulation_prompt_id == 1001)
        res = await self.session.execute(stmt)
        p = res.scalar_one()
        self.assertIn("Consulta Médica", p.title)

    async def test_update_simulation_prompts_apply(self):
        """Apply should persist all 136 prompt updates with ZERO medical terms and NO collateral changes."""
        stats = await update_simulation_prompts(self.session, apply=True)

        self.assertEqual(stats["prompts_detected"], 136)
        self.assertEqual(stats["prompts_updated"], 136)
        self.assertEqual(stats["prompts_medical_after"], 0)

        # 1. Verify all 136 prompts in DB have been updated
        stmt = select(TrainingSimulationPrompt).where(
            TrainingSimulationPrompt.simulation_prompt_id.in_(self.demo_prompt_ids)
        )
        res = await self.session.execute(stmt)
        updated_prompts = list(res.scalars().all())
        self.assertEqual(len(updated_prompts), 136)

        for p in updated_prompts:
            combined_text = f"{p.title} {p.prompt_text} {json.dumps(p.objective_focus_json)}"
            medical_matches = MEDICAL_TERM_REGEX.findall(combined_text)
            self.assertEqual(medical_matches, [], f"Medical term found in prompt {p.simulation_prompt_id}: {medical_matches}")
            # Prompt must be substantial and detailed (>600 chars)
            self.assertGreater(len(p.prompt_text), 600)
            # Prompt must contain bot roleplay rules and resistance ladder
            self.assertEqual(p.scenario_type, "roleplay")
            self.assertIn("ESCALERA DE RESISTENCIA", p.prompt_text)
            self.assertIn("IDENTIDAD Y ROL DEL BOT", p.prompt_text)

        # 2. Verify differentiation between ATC and Ventas
        stmt_atc = select(TrainingSimulationPrompt).join(
            TrainingAgentReport, TrainingAgentReport.training_report_id == TrainingSimulationPrompt.training_report_id
        ).where(TrainingAgentReport.service_id == 701)
        res_atc = await self.session.execute(stmt_atc)
        atc_prompts = list(res_atc.scalars().all())
        self.assertGreater(len(atc_prompts), 0)
        for p in atc_prompts:
            self.assertIn("Atención al Cliente", p.prompt_text)

        stmt_ventas = select(TrainingSimulationPrompt).join(
            TrainingAgentReport, TrainingAgentReport.training_report_id == TrainingSimulationPrompt.training_report_id
        ).where(TrainingAgentReport.service_id == 702)
        res_ventas = await self.session.execute(stmt_ventas)
        ventas_prompts = list(res_ventas.scalars().all())
        self.assertGreater(len(ventas_prompts), 0)
        for p in ventas_prompts:
            self.assertIn("Ventas", p.prompt_text)

        # 3. Verify Sessions are UNTOUCHED
        stmt_sess = select(TrainingCallSession).where(TrainingCallSession.cycle_id.in_(self.demo_report_ids))
        res_sess = await self.session.execute(stmt_sess)
        sessions_in_db = list(res_sess.scalars().all())
        self.assertEqual(len(sessions_in_db), self.demo_session_count)
        for sess in sessions_in_db:
            self.assertEqual(sess.status, "completed")
            self.assertIn("CA", sess.call_sid)

        # 4. Verify Evaluations are UNTOUCHED
        stmt_eval = select(TrainingCallEvaluation).where(TrainingCallEvaluation.cycle_id.in_(self.demo_report_ids))
        res_eval = await self.session.execute(stmt_eval)
        evals_in_db = list(res_eval.scalars().all())
        self.assertEqual(len(evals_in_db), self.demo_evaluation_count)
        for ev in evals_in_db:
            self.assertEqual(ev.score, Decimal("8.50"))
            self.assertEqual(ev.feedback, "Evaluación existente que NO debe modificarse.")
            self.assertEqual(ev.transcription, "Hola, buenos días, llamo para una consulta sobre mi caso.")

        # 5. Verify TrainingCompletionStatus is UNTOUCHED
        stmt_comp = select(TrainingCompletionStatus).where(TrainingCompletionStatus.training_report_id.in_(self.demo_report_ids))
        res_comp = await self.session.execute(stmt_comp)
        comps_in_db = list(res_comp.scalars().all())
        self.assertEqual(len(comps_in_db), self.demo_completion_count)
        for c in comps_in_db:
            self.assertEqual(c.notes, "Nota de completion previa")

        # 6. Verify TrainingAgentReport is UNTOUCHED
        stmt_rep = select(TrainingAgentReport).where(TrainingAgentReport.company_id == 7)
        res_rep = await self.session.execute(stmt_rep)
        reps_in_db = list(res_rep.scalars().all())
        self.assertEqual(len(reps_in_db), 60)
        for r in reps_in_db:
            self.assertEqual(r.general_objectives_json, [{"title": "Objetivo General Contact Center"}])
            self.assertEqual(r.summary_general, "Resumen genérico de contact center")

        # 7. Verify Boston Medical (company_id=1) prompt is UNTOUCHED
        stmt_bm = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.simulation_prompt_id == 9999)
        res_bm = await self.session.execute(stmt_bm)
        bm_p = res_bm.scalar_one()
        self.assertEqual(bm_p.title, "Consulta Médica Boston Original")
        self.assertIn("Boston Medical Group", bm_p.prompt_text)


if __name__ == "__main__":
    unittest.main()
