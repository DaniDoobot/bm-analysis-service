"""
Unit and integration tests for TrainingAgentReport.company_id resolution,
guarding, cycle knowledge document tenant self-repair, tutor isolation,
and safe deterministic backfill.
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_training_company_id_backfill.db"

from sqlalchemy import BigInteger, delete, select, and_, func, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingAgentSetting,
    TrainingCallEvaluation,
    TrainingCallSession,
    TrainingCompletionStatus,
    TrainingEvaluationPrompt,
    TrainingSimulationPrompt,
    TrainingKnowledgeDocument,
    TrainingRun,
)
from app.services.personalized_training_service import PersonalizedTrainingService
from app.services.training_knowledge_service import TrainingKnowledgeService
from app.services.trainer_chatbot_service import TrainerChatbotService
from app.core.tenant_context import TenantContext


class TestTrainingCompanyIdBackfillAndGuard(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(TrainingKnowledgeDocument))
            await db.execute(delete(TrainingCallEvaluation))
            await db.execute(delete(TrainingCallSession))
            await db.execute(delete(TrainingCompletionStatus))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingEvaluationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(TrainingAgentSetting))
            await db.execute(delete(TrainingRun))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            # Base Companies
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False, is_active=True)
            c7 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True, is_active=True)
            db.add_all([c1, c7])

            # Base Services
            s1 = Service(service_id=1, service_name="Atención Médica", service_key="atencion-medica", company_id=1, is_active=True)
            s7 = Service(service_id=7, service_name="Ventas y Demo", service_key="ventas", company_id=7, is_active=True)
            db.add_all([s1, s7])

            # Base Users
            u_demo = User(
                user_id=10,
                username="demo01",
                email="demo01@empresa.com",
                password_hash="dummyhash",
                hubspot_owner_id="demo_owner_01",
                company_id=7,
                is_active=True,
            )
            u_boston = User(
                user_id=20,
                username="boston01",
                email="boston01@boston.com",
                password_hash="dummyhash",
                hubspot_owner_id="boston_owner_01",
                company_id=1,
                is_active=True,
            )
            db.add_all([u_demo, u_boston])

            await db.commit()

    async def _create_completed_cycle(
        self,
        db: AsyncSession,
        hubspot_owner_id: str = "demo_owner_01",
        agent_name: str = "Demo 01",
        agent_initials: str = "D1",
        company_id: int | None = 7,
    ) -> TrainingAgentReport:
        rep = TrainingAgentReport(
            hubspot_owner_id=hubspot_owner_id,
            agent_name=agent_name,
            agent_initials=agent_initials,
            company_id=company_id,
            period_start=datetime.now(timezone.utc) - timedelta(days=14),
            period_end=datetime.now(timezone.utc),
            status="completed",
            general_objectives_json=[{"title": "Obj 1"}],
            specific_objectives_json=[{"title": "Spec 1"}],
            final_report_json={"summary_final": "Completado"},
        )
        db.add(rep)
        await db.flush()

        prompt = TrainingSimulationPrompt(
            training_report_id=rep.training_report_id,
            hubspot_owner_id=hubspot_owner_id,
            prompt_number=1,
            title="P1",
            scenario_type="roleplay",
            prompt_text="Simulación 1",
        )
        db.add(prompt)
        await db.flush()

        session = TrainingCallSession(
            call_sid=f"call_{rep.training_report_id}_1",
            agent_id=hubspot_owner_id,
            cycle_id=rep.training_report_id,
            conversation_id=prompt.simulation_prompt_id,
            status="completed",
        )
        db.add(session)
        await db.flush()

        eval_prompt = TrainingEvaluationPrompt(
            service_id=7,
            company_id=company_id or 7,
            prompt_text="Prompt de evaluación general",
        )
        db.add(eval_prompt)
        await db.flush()

        eval_entry = TrainingCallEvaluation(
            session_id=session.session_id,
            cycle_id=rep.training_report_id,
            conversation_id=prompt.simulation_prompt_id,
            agent_id=hubspot_owner_id,
            prompt_version_id=eval_prompt.id,
            result_json={"score": 8.0},
            score=Decimal("8.0"),
            feedback="Buen desempeño",
        )
        db.add(eval_entry)
        await db.flush()

        comp = TrainingCompletionStatus(
            training_report_id=rep.training_report_id,
            simulation_prompt_id=prompt.simulation_prompt_id,
            hubspot_owner_id=hubspot_owner_id,
            status="completed",
            call_session_id=session.session_id,
            evaluation_id=eval_entry.evaluation_id,
        )
        db.add(comp)
        await db.commit()
        await db.refresh(rep)
        return rep

    async def test_01_auto_cycle_with_setting_company_id_preserved(self):
        """1. ciclo automático con setting.company_id presente -> lo conserva"""
        async with self.session_maker() as db:
            setting = TrainingAgentSetting(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=7,
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            start = datetime.now(timezone.utc) - timedelta(days=14)
            end = datetime.now(timezone.utc)

            with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = '{"general_objectives": [], "specific_objectives": [], "summary_general": "ok"}'
                report = await PersonalizedTrainingService.generate_report_for_agent(
                    db=db,
                    hubspot_owner_id="demo_owner_01",
                    period_start=start,
                    period_end=end,
                    run_id=None,
                    force_regenerate=True,
                )

            self.assertIsNotNone(report)
            self.assertEqual(report.company_id, 7)

    async def test_02_auto_cycle_setting_null_user_demo_7(self):
        """2. ciclo automático con setting.company_id NULL y User.company_id=7 -> report.company_id=7"""
        async with self.session_maker() as db:
            setting = TrainingAgentSetting(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=None,  # NULL setting
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            start = datetime.now(timezone.utc) - timedelta(days=14)
            end = datetime.now(timezone.utc)

            with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = '{"general_objectives": [], "specific_objectives": [], "summary_general": "ok"}'
                report = await PersonalizedTrainingService.generate_report_for_agent(
                    db=db,
                    hubspot_owner_id="demo_owner_01",
                    period_start=start,
                    period_end=end,
                    run_id=None,
                    force_regenerate=True,
                )

            self.assertIsNotNone(report)
            self.assertEqual(report.company_id, 7)

    async def test_03_auto_cycle_setting_null_user_boston_1(self):
        """3. equivalente Boston -> company_id=1"""
        async with self.session_maker() as db:
            setting = TrainingAgentSetting(
                hubspot_owner_id="boston_owner_01",
                agent_name="Bryan Herrera",
                agent_initials="BH",
                company_id=None,  # NULL setting
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            start = datetime.now(timezone.utc) - timedelta(days=14)
            end = datetime.now(timezone.utc)

            with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = '{"general_objectives": [], "specific_objectives": [], "summary_general": "ok"}'
                report = await PersonalizedTrainingService.generate_report_for_agent(
                    db=db,
                    hubspot_owner_id="boston_owner_01",
                    period_start=start,
                    period_end=end,
                    run_id=None,
                    force_regenerate=True,
                )

            self.assertIsNotNone(report)
            self.assertEqual(report.company_id, 1)

    async def test_04_approve_training_cycle_persists_resolved_company_id(self):
        """4. approve_training_cycle con report.company_id NULL -> persiste tenant resuelto"""
        async with self.session_maker() as db:
            rep = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=None,  # NULL
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="pending_approval",
                general_objectives_json=[{"title": "Obj 1"}],
                specific_objectives_json=[{"title": "Spec 1"}],
                weaknesses_json=[{"title": "W1"}],
            )
            db.add(rep)
            await db.commit()
            await db.refresh(rep)
            rep_id = rep.training_report_id

            with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = '{"prompts": [{"prompt_number": 1, "title": "P1", "prompt_text": "Texto 1"}]}'
                approved = await PersonalizedTrainingService.approve_training_cycle(
                    db=db,
                    report_id=rep_id,
                    approved_by_user_id=1,
                )

            self.assertEqual(approved.company_id, 7)

            # Query afresh from DB to verify persistence across transaction
            stmt = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == rep_id)
            res = await db.execute(stmt)
            fresh_rep = res.scalars().first()
            self.assertEqual(fresh_rep.company_id, 7)

    async def test_05_approve_training_cycle_does_not_overwrite_existing_company_id(self):
        """5. approve_training_cycle no cambia un company_id ya existente"""
        async with self.session_maker() as db:
            rep = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=1,  # Pre-existing company_id=1
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="pending_approval",
                general_objectives_json=[{"title": "Obj 1"}],
                specific_objectives_json=[{"title": "Spec 1"}],
                weaknesses_json=[{"title": "W1"}],
            )
            db.add(rep)
            await db.commit()
            await db.refresh(rep)

            with patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = '{"prompts": [{"prompt_number": 1, "title": "P1", "prompt_text": "Texto 1"}]}'
                approved = await PersonalizedTrainingService.approve_training_cycle(
                    db=db,
                    report_id=rep.training_report_id,
                    approved_by_user_id=1,
                )

            self.assertEqual(approved.company_id, 1)

    async def test_06_failed_report_persists_resolved_cid(self):
        """6. failed_report guarda resolved_cid"""
        async with self.session_maker() as db:
            setting = TrainingAgentSetting(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=7,
                is_enabled=True,
            )
            db.add(setting)
            await db.commit()

            start = datetime.now(timezone.utc) - timedelta(days=14)
            end = datetime.now(timezone.utc)

            with patch.object(PersonalizedTrainingService, "aggregate_agent_evaluations", new_callable=AsyncMock) as mock_agg, \
                 patch("app.services.personalized_training_service.complete_text", side_effect=RuntimeError("Simulated LLM Crash")):
                mock_agg.return_value = {
                    "evaluations_count": 5,
                    "calls_count": 5,
                    "avg_evaluacion_global": Decimal("7.5"),
                    "avg_scores": {},
                    "agent_id": "demo_owner_01",
                    "agent_name": "Demo 01",
                }
                returned_rep = await PersonalizedTrainingService.generate_report_for_agent(
                    db=db,
                    hubspot_owner_id="demo_owner_01",
                    period_start=start,
                    period_end=end,
                    run_id=None,
                    force_regenerate=True,
                )
            self.assertIsNotNone(returned_rep)
            self.assertEqual(returned_rep.status, "failed")
            self.assertEqual(returned_rep.company_id, 7)

            # Query the failed report
            stmt = select(TrainingAgentReport).where(
                TrainingAgentReport.hubspot_owner_id == "demo_owner_01",
                TrainingAgentReport.status == "failed"
            )
            res = await db.execute(stmt)
            failed_rep = res.scalars().first()
            self.assertIsNotNone(failed_rep)
            self.assertEqual(failed_rep.company_id, 7)

    async def test_07_manual_cycle_persists_company_id(self):
        """7. manual cycle sigue guardando tenant correctamente"""
        async with self.session_maker() as db:
            reports = await PersonalizedTrainingService.create_manual_cycles(
                db=db,
                hubspot_owner_ids=["demo_owner_01"],
                title="Ciclo Manual Test",
                general_objectives=["Objetivo manual"],
                specific_objectives=["Comportamiento manual"],
                approved_by_user_id=1,
            )
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].company_id, 7)

    async def test_08_cycle_knowledge_doc_uses_report_company_id_if_present(self):
        """8. cycle knowledge document usa report.company_id si existe"""
        async with self.session_maker() as db:
            rep = await self._create_completed_cycle(db, hubspot_owner_id="demo_owner_01", company_id=7)

            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(db, rep.training_report_id)
            cycle_docs = [d for d in docs if d.document_type == "cycle"]
            self.assertEqual(len(cycle_docs), 1)
            self.assertEqual(cycle_docs[0].company_id, 7)

    async def test_09_cycle_knowledge_doc_resolves_user_company_id_when_report_null(self):
        """9. cycle knowledge document resuelve User.company_id si report está NULL"""
        async with self.session_maker() as db:
            rep = await self._create_completed_cycle(db, hubspot_owner_id="demo_owner_01", company_id=None)

            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(db, rep.training_report_id)
            cycle_docs = [d for d in docs if d.document_type == "cycle"]
            self.assertEqual(len(cycle_docs), 1)
            self.assertEqual(cycle_docs[0].company_id, 7)
            self.assertEqual(cycle_docs[0].metadata_json.get("company_id"), 7)

    async def test_10_cycle_knowledge_doc_self_repairs_report_company_id(self):
        """10. al resolverlo, autorepara report.company_id"""
        async with self.session_maker() as db:
            rep = await self._create_completed_cycle(db, hubspot_owner_id="demo_owner_01", company_id=None)
            rep_id = rep.training_report_id

            await TrainingKnowledgeService.generate_cycle_knowledge_documents(db, rep_id)

            stmt = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == rep_id)
            res = await db.execute(stmt)
            refreshed_rep = res.scalars().first()
            self.assertEqual(refreshed_rep.company_id, 7)

    async def test_11_cycle_knowledge_doc_aborts_without_orphan_when_no_tenant(self):
        """11. si no existe tenant determinista, NO crea cycle knowledge doc huérfano"""
        async with self.session_maker() as db:
            rep = await self._create_completed_cycle(db, hubspot_owner_id="orphan_agent_no_user", company_id=None)

            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(db, rep.training_report_id)
            # Aborts without creating an orphan cycle document
            cycle_docs = [d for d in docs if d.document_type == "cycle"]
            self.assertEqual(len(cycle_docs), 0)

            # Ensure no cycle document was persisted in DB
            stmt = select(TrainingKnowledgeDocument).where(
                and_(
                    TrainingKnowledgeDocument.cycle_id == rep.training_report_id,
                    TrainingKnowledgeDocument.document_type == "cycle",
                )
            )
            res = await db.execute(stmt)
            self.assertIsNone(res.scalars().first())

    async def test_12_simulation_knowledge_doc_resolves_correct_company_id(self):
        """12. simulation knowledge document sigue obteniendo company_id correcto"""
        async with self.session_maker() as db:
            rep = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=None,  # Legacy NULL report
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="in_progress",
            )
            db.add(rep)
            await db.flush()

            prompt = TrainingSimulationPrompt(
                training_report_id=rep.training_report_id,
                hubspot_owner_id="demo_owner_01",
                prompt_number=1,
                title="P1",
                scenario_type="roleplay",
                prompt_text="Simulación 1",
            )
            db.add(prompt)
            await db.flush()

            session = TrainingCallSession(
                call_sid="call_sim_123",
                agent_id="demo_owner_01",
                cycle_id=rep.training_report_id,
                conversation_id=prompt.simulation_prompt_id,
                status="completed",
            )
            db.add(session)
            await db.flush()

            eval_prompt = TrainingEvaluationPrompt(
                service_id=7,
                company_id=7,
                prompt_text="Prompt de evaluación",
            )
            db.add(eval_prompt)
            await db.flush()

            eval_entry = TrainingCallEvaluation(
                session_id=session.session_id,
                cycle_id=rep.training_report_id,
                conversation_id=prompt.simulation_prompt_id,
                agent_id="demo_owner_01",
                prompt_version_id=eval_prompt.id,
                result_json={"score": 8.5},
                score=Decimal("8.5"),
                feedback="Buen desempeño",
            )
            db.add(eval_entry)
            await db.commit()

            sim_doc = await TrainingKnowledgeService.generate_simulation_knowledge_document(
                db, eval_entry.evaluation_id
            )
            self.assertIsNotNone(sim_doc)
            self.assertEqual(sim_doc.company_id, 7)
            self.assertEqual(sim_doc.metadata_json.get("company_id"), 7)

    async def test_13_tutor_historical_profile_isolation_intact(self):
        """13. tenant isolation Tutor permanece intacto"""
        async with self.session_maker() as db:
            rep = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=7,
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="completed",
                summary_general="Resumen Demo",
                final_report_json={"general_objectives_status": []},
            )
            db.add(rep)
            await db.commit()

            from app.core.roles import InternalRole
            ctx_demo = TenantContext(
                user_id=10,
                user_email="demo01@empresa.com",
                raw_role="agent",
                normalized_role=InternalRole.AGENT,
                company_id=7,
                is_super_admin=False,
            )

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                target_agent_id="demo_owner_01",
                context=ctx_demo,
            )
            self.assertIn("1 ciclos registrados", profile)
            self.assertNotIn("No existen evaluaciones históricas de llamadas reales, ciclos formativos ni simulaciones", profile)

    async def test_14_tutor_historical_profile_excludes_other_company_reports(self):
        """14. reports de otra empresa no entran en histórico"""
        async with self.session_maker() as db:
            rep_boston = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",  # Same agent id, but assigned to company 1
                agent_name="Demo in Boston",
                agent_initials="DB",
                company_id=1,
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="completed",
                summary_general="Resumen Boston",
                final_report_json={"general_objectives_status": []},
            )
            db.add(rep_boston)
            await db.commit()

            from app.core.roles import InternalRole
            ctx_demo = TenantContext(
                user_id=10,
                user_email="demo01@empresa.com",
                raw_role="agent",
                normalized_role=InternalRole.AGENT,
                company_id=7,
                is_super_admin=False,
            )

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                target_agent_id="demo_owner_01",
                context=ctx_demo,
            )
            # The company 1 report must NOT be included for company 7 context
            self.assertNotIn("Resumen Boston", profile)
            self.assertIn("No existen evaluaciones", profile)

    async def test_15_backfill_updates_owner_with_single_company_id(self):
        """15. backfill rellena owner con un único company_id"""
        async with self.session_maker() as db:
            # Report with NULL company_id for demo_owner_01 (who has exactly company_id=7 in bm_users)
            rep = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=None,
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="completed",
            )
            setting = TrainingAgentSetting(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=None,
                is_enabled=True,
            )
            db.add_all([rep, setting])
            await db.commit()
            await db.refresh(rep)
            await db.refresh(setting)

            # Execute the deterministic backfill logic
            # CTE equivalent:
            # 1. Select owners with COUNT(DISTINCT company_id) = 1
            cte_sub = (
                select(User.hubspot_owner_id, func.min(User.company_id).label("resolved_cid"))
                .where(and_(User.hubspot_owner_id != None, User.company_id != None))
                .group_by(User.hubspot_owner_id)
                .having(func.count(func.distinct(User.company_id)) == 1)
                .subquery()
            )

            # Update reports
            stmt_up_rep = (
                update(TrainingAgentReport)
                .where(
                    and_(
                        TrainingAgentReport.company_id == None,
                        TrainingAgentReport.hubspot_owner_id == cte_sub.c.hubspot_owner_id,
                    )
                )
                .values(company_id=cte_sub.c.resolved_cid)
            )
            await db.execute(stmt_up_rep)

            # Update settings
            stmt_up_set = (
                update(TrainingAgentSetting)
                .where(
                    and_(
                        TrainingAgentSetting.company_id == None,
                        TrainingAgentSetting.hubspot_owner_id == cte_sub.c.hubspot_owner_id,
                    )
                )
                .values(company_id=cte_sub.c.resolved_cid)
            )
            await db.execute(stmt_up_set)
            await db.commit()

            await db.refresh(rep)
            await db.refresh(setting)
            self.assertEqual(rep.company_id, 7)
            self.assertEqual(setting.company_id, 7)

    async def test_16_backfill_skips_ambiguous_owner_with_multiple_companies(self):
        """16. backfill NO rellena owner ambiguo con varios company_id"""
        async with self.session_maker() as db:
            from sqlalchemy import Table, Column, Text, Integer, MetaData
            test_meta = MetaData()
            test_users_tbl = Table(
                "temp_test_users_ambiguous",
                test_meta,
                Column("hubspot_owner_id", Text),
                Column("company_id", Integer),
            )
            async with self.engine.begin() as conn:
                await conn.run_sync(test_meta.create_all)
                await conn.execute(test_users_tbl.insert(), [
                    {"hubspot_owner_id": "ambiguous_owner", "company_id": 1},
                    {"hubspot_owner_id": "ambiguous_owner", "company_id": 7},
                    {"hubspot_owner_id": "clean_owner", "company_id": 7},
                ])

            # Run deterministic selection exactly as in migration
            stmt = (
                select(test_users_tbl.c.hubspot_owner_id, func.min(test_users_tbl.c.company_id))
                .where(and_(test_users_tbl.c.hubspot_owner_id != None, test_users_tbl.c.company_id != None))
                .group_by(test_users_tbl.c.hubspot_owner_id)
                .having(func.count(func.distinct(test_users_tbl.c.company_id)) == 1)
            )
            res = await db.execute(stmt)
            rows = res.all()
            resolved_map = {r[0]: r[1] for r in rows}
            self.assertIn("clean_owner", resolved_map)
            self.assertEqual(resolved_map["clean_owner"], 7)
            # Must be excluded due to having COUNT(DISTINCT company_id) > 1
            self.assertNotIn("ambiguous_owner", resolved_map)

    async def test_17_backfill_does_not_overwrite_existing_company_id(self):
        """17. backfill no pisa valores ya existentes"""
        async with self.session_maker() as db:
            rep = TrainingAgentReport(
                hubspot_owner_id="demo_owner_01",
                agent_name="Demo 01",
                agent_initials="D1",
                company_id=99,  # Already set to custom/existing company
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="completed",
            )
            db.add(rep)
            await db.commit()
            await db.refresh(rep)

            cte_sub = (
                select(User.hubspot_owner_id, func.min(User.company_id).label("resolved_cid"))
                .where(and_(User.hubspot_owner_id != None, User.company_id != None))
                .group_by(User.hubspot_owner_id)
                .having(func.count(func.distinct(User.company_id)) == 1)
                .subquery()
            )

            stmt_up = (
                update(TrainingAgentReport)
                .where(
                    and_(
                        TrainingAgentReport.company_id == None,  # only where NULL
                        TrainingAgentReport.hubspot_owner_id == cte_sub.c.hubspot_owner_id,
                    )
                )
                .values(company_id=cte_sub.c.resolved_cid)
            )
            await db.execute(stmt_up)
            await db.commit()

            await db.refresh(rep)
            # Did not get overwritten to 7
            self.assertEqual(rep.company_id, 99)

    async def test_18_migration_script_validity_and_idempotency(self):
        """18. migration script validity, CTE safety, and idempotency checks"""
        migration_path = os.path.join(os.path.dirname(__file__), "..", "migrations", "v020_backfill_training_company_id.sql")
        self.assertTrue(os.path.exists(migration_path), "Migration file v020 must exist")

        with open(migration_path, "r", encoding="utf-8") as f:
            sql_content = f.read()

        # Check required architectural rules:
        self.assertIn("WITH unique_owner_companies AS", sql_content)
        self.assertIn("HAVING COUNT(DISTINCT company_id) = 1", sql_content)
        self.assertIn("bm_training_agent_reports", sql_content)
        self.assertIn("bm_training_agent_settings", sql_content)
        self.assertIn("bm_training_knowledge_documents", sql_content)
        self.assertIn("company_id IS NULL", sql_content)
        
        # Rule 6: MUST NOT add NOT NULL constraint to column
        code_lines = [line for line in sql_content.splitlines() if not line.strip().startswith("--")]
        code_only = "\n".join(code_lines).upper()
        self.assertNotIn("SET NOT NULL", code_only)
        self.assertNotIn("ALTER COLUMN", code_only)


if __name__ == "__main__":
    unittest.main()
