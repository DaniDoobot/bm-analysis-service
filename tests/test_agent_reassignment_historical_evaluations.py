"""
tests/test_agent_reassignment_historical_evaluations.py
=======================================================
Test suite proving data integrity and immutability of historical evaluations
when an agent is reassigned to a different team and/or service.

Verifies:
1. An agent originally in Service A / Team A accumulates historical records across:
   - bm_analyses (individual call evaluations)
   - bm_mass_evaluation_results (mass/batch evaluation records)
   - bm_trainer_sessions (Trainer roleplay sessions)
   - bm_training_agent_reports (cycle training reports)
2. The agent is reassigned to Service B / Team B (updating User and associations).
3. Historical evaluation records remain strictly bound to their original Service A and Team A,
   without being mutated, reassigned or deleted.
4. Historical queries filtered by Service A continue to return the agent's historical evaluations.
5. New evaluations created post-reassignment correctly bind to the new Service B,
   preserving auditability and time-travel reporting integrity.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import BigInteger, select, func

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

sys.path.insert(0, os.path.abspath("."))

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation
from app.models.users import User
from app.models.analyses import Analysis
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.models.trainer import TrainerSimulation, TrainerSession
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport


class TestAgentReassignmentHistoricalEvaluations(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            # 1. Company
            company = Company(
                company_id=1,
                company_name="Empresa Global S.L.",
                company_key="empresa-global",
                is_active=True,
            )
            db.add(company)
            await db.flush()

            # 2. Services
            svc_front = Service(
                service_id=10,
                company_id=1,
                service_name="Servicio Frontal / Citas",
                service_key="servicio-frontal",
                is_active=True,
            )
            svc_comercial = Service(
                service_id=20,
                company_id=1,
                service_name="Servicio Comercial / Ventas",
                service_key="servicio-comercial",
                is_active=True,
            )
            db.add_all([svc_front, svc_comercial])
            await db.flush()

            # 3. Teams
            team_front = Team(
                team_id=100,
                company_id=1,
                service_id=10,
                team_name="Equipo Frontal Mañana",
            )
            team_comercial = Team(
                team_id=200,
                company_id=1,
                service_id=20,
                team_name="Equipo Ventas Outbound",
            )
            db.add_all([team_front, team_comercial])
            await db.flush()

            # 4. User Agent initially assigned to Service Frontal (10) and Team Frontal (100)
            user_agent = User(
                user_id=1,
                username="agente_maria",
                email="maria@empresaglobal.com",
                name="María Gómez",
                role="agent",
                company_id=1,
                primary_service_id=10,
                primary_team_id=100,
                hubspot_owner_id="HS_OWNER_MARIA",
                agent_initials="MG",
                password_hash="fakehash",
                is_active=True,
            )
            agent_setting = TrainingAgentSetting(
                setting_id=1,
                company_id=1,
                hubspot_owner_id="HS_OWNER_MARIA",
                agent_name="María Gómez",
                agent_initials="MG",
                training_code="MG01",
                training_numeric_code="1001",
                training_code_enabled=True,
            )
            assoc_svc = UserServiceAssociation(user_id=1, service_id=10)
            assoc_team = UserTeamAssociation(user_id=1, team_id=100)

            db.add_all([user_agent, agent_setting, assoc_svc, assoc_team])
            await db.flush()

            # 5. Historical records generated during Service Frontal tenure
            # 5a. Individual Analysis in bm_analyses
            analysis_hist = Analysis(
                analysis_id=501,
                company_id=1,
                service_id=10,
                call_id="CALL_HIST_001",
                hubspot_owner_id="HS_OWNER_MARIA",
                agente_telefonico="María Gómez",
                evaluacion_global=Decimal("8.50"),
                status="completed",
                call_timestamp=datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc),
            )

            # 5b. Mass evaluation job, run, and record in bm_mass_evaluation_results
            mass_job = MassEvaluationJob(
                job_id=1,
                company_id=1,
                service_id=10,
                job_name="Job Histórico Frontal",
                prompt_id=1,
            )
            mass_run = MassEvaluationRun(
                run_id=1,
                job_id=1,
                trigger_type="manual",
                status="completed",
                calls_found=1,
                calls_analyzed=1,
            )
            db.add_all([mass_job, mass_run])
            await db.flush()

            mass_eval_hist = MassEvaluationResult(
                mass_analysis_id=601,
                run_id=1,
                job_id=1,
                company_id=1,
                service_id=10,
                service_name="Servicio Frontal / Citas",
                call_id="CALL_MASS_HIST_001",
                hubspot_owner_id="HS_OWNER_MARIA",
                agent_name="María Gómez",
                evaluacion_global=Decimal("9.00"),
                status="completed",
                prompt_id=1,
                prompt_snapshot="Snapshot prompt citas",
                call_timestamp=datetime(2026, 1, 20, 11, 0, tzinfo=timezone.utc),
            )

            # 5c. Trainer session in bm_trainer_sessions
            sim_front = TrainerSimulation(
                simulation_id=701,
                company_id=1,
                service_id=10,
                name="Simulación Citas Frontal",
                code="CITAS01",
                roleplay_prompt="Roleplay citas...",
                status="published",
            )
            db.add(sim_front)
            await db.flush()

            trainer_session_hist = TrainerSession(
                session_id=801,
                company_id=1,
                service_id=10,
                simulation_id=701,
                call_id="CALL_TRAINER_HIST_001",
                agent_id="HS_OWNER_MARIA",
                agent_code="MG01",
                duration_seconds=180,
                status="completed",
                evaluation_status="evaluated",
                created_at=datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc),
            )

            # 5d. Personalized training report in bm_training_agent_reports
            training_report_hist = TrainingAgentReport(
                training_report_id=901,
                company_id=1,
                service_id=10,
                hubspot_owner_id="HS_OWNER_MARIA",
                agent_name="María Gómez",
                agent_initials="MG",
                period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                period_end=datetime(2026, 1, 31, tzinfo=timezone.utc),
                status="completed",
                evaluations_count=20,
                calls_count=20,
                avg_evaluacion_global=Decimal("8.65"),
            )

            db.add_all([analysis_hist, mass_eval_hist, trainer_session_hist, training_report_hist])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_reassignment_preserves_historical_evaluations_immutability(self):
        """
        When an agent is reassigned to Service B and Team B:
        - The user profile reflects the new service and team.
        - Historical evaluations retain service_id=10 and are not altered.
        """
        async with self.session_maker() as db:
            # 1. Reassign agent to Service Comercial (20) and Team Comercial (200)
            stmt_user = select(User).where(User.user_id == 1)
            user = (await db.execute(stmt_user)).scalar_one()
            user.primary_service_id = 20
            user.primary_team_id = 200

            # Update associations
            from sqlalchemy import delete
            await db.execute(delete(UserServiceAssociation).where(UserServiceAssociation.user_id == 1))
            await db.execute(delete(UserTeamAssociation).where(UserTeamAssociation.user_id == 1))
            db.add(UserServiceAssociation(user_id=1, service_id=20))
            db.add(UserTeamAssociation(user_id=1, team_id=200))
            await db.commit()

        # 2. Verify in a fresh session that user has new assignment
        async with self.session_maker() as db:
            user_fresh = (await db.execute(select(User).where(User.user_id == 1))).scalar_one()
            self.assertEqual(user_fresh.primary_service_id, 20)
            self.assertEqual(user_fresh.primary_team_id, 200)

            # 3. Verify that all historical records in all tables still point to Service 10
            # 3a. bm_analyses
            a_row = (await db.execute(select(Analysis).where(Analysis.analysis_id == 501))).scalar_one()
            self.assertEqual(a_row.service_id, 10, "bm_analyses record must remain in historical service_id=10")
            self.assertEqual(a_row.hubspot_owner_id, "HS_OWNER_MARIA")
            self.assertEqual(a_row.evaluacion_global, Decimal("8.50"))

            # 3b. bm_mass_evaluation_results
            m_row = (await db.execute(select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == 601))).scalar_one()
            self.assertEqual(m_row.service_id, 10, "bm_mass_evaluation_results record must remain in historical service_id=10")
            self.assertEqual(m_row.service_name, "Servicio Frontal / Citas")

            # 3c. bm_trainer_sessions
            t_row = (await db.execute(select(TrainerSession).where(TrainerSession.session_id == 801))).scalar_one()
            self.assertEqual(t_row.service_id, 10, "bm_trainer_sessions record must remain in historical service_id=10")

            # 3d. bm_training_agent_reports
            r_row = (await db.execute(select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 901))).scalar_one()
            self.assertEqual(r_row.service_id, 10, "bm_training_agent_reports record must remain in historical service_id=10")

    async def test_historical_service_filtered_queries_retain_agent_records(self):
        """
        Service-level metric queries for Service 10 must still count historical evaluations
        of the agent performed while she was in Service 10.
        """
        async with self.session_maker() as db:
            # Reassign user to Service 20
            user = (await db.execute(select(User).where(User.user_id == 1))).scalar_one()
            user.primary_service_id = 20
            await db.commit()

        async with self.session_maker() as db:
            # Query evaluations by service_id == 10
            stmt_analyses = select(func.count(Analysis.analysis_id)).where(
                Analysis.service_id == 10,
                Analysis.hubspot_owner_id == "HS_OWNER_MARIA"
            )
            count_analyses = (await db.execute(stmt_analyses)).scalar()
            self.assertEqual(count_analyses, 1, "Historical analysis must be found when querying Service 10")

            # Query mass evaluations by service_id == 10
            stmt_mass = select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                MassEvaluationResult.service_id == 10,
                MassEvaluationResult.hubspot_owner_id == "HS_OWNER_MARIA"
            )
            count_mass = (await db.execute(stmt_mass)).scalar()
            self.assertEqual(count_mass, 1, "Historical mass evaluation must be found when querying Service 10")

            # Query for Service 20 should currently have 0 evaluations for this agent
            stmt_analyses_s20 = select(func.count(Analysis.analysis_id)).where(
                Analysis.service_id == 20,
                Analysis.hubspot_owner_id == "HS_OWNER_MARIA"
            )
            count_s20 = (await db.execute(stmt_analyses_s20)).scalar()
            self.assertEqual(count_s20, 0, "Service 20 must not inherit past evaluations from Service 10")

    async def test_new_evaluations_post_reassignment_bind_to_new_service(self):
        """
        New evaluations created after the reassignment automatically attach to Service 20,
        maintaining a clear historical separation without data contamination.
        """
        async with self.session_maker() as db:
            # Reassign agent to Service 20
            user = (await db.execute(select(User).where(User.user_id == 1))).scalar_one()
            user.primary_service_id = 20
            await db.commit()

            # Record a new analysis in Service 20
            new_analysis = Analysis(
                analysis_id=502,
                company_id=1,
                service_id=user.primary_service_id,  # 20
                call_id="CALL_NEW_VENTAS_001",
                hubspot_owner_id=user.hubspot_owner_id,
                agente_telefonico=user.name,
                evaluacion_global=Decimal("9.20"),
                status="completed",
                call_timestamp=datetime(2026, 3, 1, tzinfo=timezone.utc),
            )
            db.add(new_analysis)
            await db.commit()

        async with self.session_maker() as db:
            # Total analyses for María: 2 (one in Service 10, one in Service 20)
            all_maria = (await db.execute(
                select(Analysis.analysis_id, Analysis.service_id)
                .where(Analysis.hubspot_owner_id == "HS_OWNER_MARIA")
                .order_by(Analysis.analysis_id)
            )).all()
            self.assertEqual(len(all_maria), 2)
            self.assertEqual(all_maria[0], (501, 10), "First call remains in Service 10")
            self.assertEqual(all_maria[1], (502, 20), "Second call is recorded in Service 20")


if __name__ == "__main__":
    unittest.main()
