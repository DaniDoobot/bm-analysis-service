"""
Unit test suite for random quality monitoring mass evaluation jobs.
Validates job mode creation, validation rules, daily random sampling, metadata trace, and multitenant scoping.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///random_quality_jobs_test.db"
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from datetime import datetime, time, timedelta, timezone
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, get_engine
from app.main import app
from app.models.companies import Company
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun
from app.models.prompts import Prompt, PromptVersion
from app.models.services import Service
from app.models.teams import AgentTeamAssociation, Team, UserTeamAssociation
from app.models.users import User
from app.services.hubspot_service import HubSpotService
from app.services.mass_evaluation_service import MassEvaluationService
from app.utils.security import create_access_token, hash_password


class TestRandomQualityMonitoringJobs(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Engine URL points to production host!"

        if os.path.exists("random_quality_jobs_test.db"):
            try:
                os.remove("random_quality_jobs_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine) as db:
            company = Company(
                company_id=1,
                company_name="Boston Medical Test",
                company_key="boston_test",
                brand_name="Boston Medical",
                is_active=True
            )
            service = Service(
                service_id=10,
                company_id=1,
                service_name="Experiencia Paciente",
                service_key="exp_paciente",
                is_active=True
            )
            db.add_all([company, service])
            await db.flush()

            prompt = Prompt(
                prompt_id=100,
                company_id=1,
                service_id=10,
                prompt_name="Evaluación Calidad Paciente",
                prompt_type="text",
                is_active=True
            )
            db.add(prompt)
            await db.flush()

            p_version = PromptVersion(
                id=1001,
                prompt_id=100,
                version_label="v1",
                prompt="Prompt content test",
                is_current=True
            )
            db.add(p_version)
            prompt.current_version_id = 1001
            await db.flush()

            team = Team(
                team_id=20,
                company_id=1,
                service_id=10,
                team_name="Equipo Principal",
                is_active=True
            )
            db.add(team)
            await db.flush()

            super_admin = User(
                user_id=101,
                username="super_admin",
                email="super@doobot.ai",
                password_hash=hash_password("Pass1234!"),
                role="super_admin",
                company_id=1,
                is_active=True
            )
            team_coord = User(
                user_id=102,
                username="team_coord",
                email="coord@boston.es",
                password_hash=hash_password("Pass1234!"),
                role="team_coordinator",
                company_id=1,
                primary_service_id=10,
                primary_team_id=20,
                is_active=True
            )
            agent_user = User(
                user_id=103,
                username="agent_maria",
                email="maria@boston.es",
                password_hash=hash_password("Pass1234!"),
                role="agent",
                company_id=1,
                primary_service_id=10,
                primary_team_id=20,
                hubspot_owner_id="31499194",
                is_active=True
            )
            db.add_all([super_admin, team_coord, agent_user])
            await db.flush()

            coord_assoc = UserTeamAssociation(user_id=102, team_id=20)
            agent_assoc = AgentTeamAssociation(user_id=103, team_id=20)
            db.add_all([coord_assoc, agent_assoc])
            await db.commit()

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

        if os.path.exists("test_random_quality_jobs.db"):
            try:
                os.remove("test_random_quality_jobs.db")
            except Exception:
                pass

    def make_token(self, user_id: Any, email: str, role: str) -> str:
        return create_access_token(data={"user_id": user_id, "email": email, "role": role})

    async def test_create_random_quality_monitoring_job_success(self):
        token = self.make_token(101, "super@doobot.ai", "super_admin")
        headers = {"Authorization": f"Bearer {token}"}

        payload = {
            "job_name": "Auditoría Aleatoria Julio",
            "job_mode": "random_quality_monitoring",
            "prompt_id": 100,
            "calls_per_day": 20,
            "date_from": "2026-07-01T00:00:00+02:00",
            "date_to": "2026-07-05T23:59:59+02:00",
            "time_from": "09:00",
            "time_to": "18:00",
            "min_duration_minutes": 1.5,
            "max_duration_minutes": 10.0,
            "direction": "inbound",
            "agent_owner_ids": ["31499194"]
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/bm/mass-evaluation-jobs", json=payload, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            data = response.json()
            self.assertEqual(data["job_name"], "Auditoría Aleatoria Julio")
            self.assertEqual(data["job_mode"], "random_quality_monitoring")
            self.assertEqual(data["calls_per_day"], 20)
            self.assertEqual(data["company_id"], 1)
            self.assertEqual(data["service_id"], 10)
            self.assertEqual(data["direction"], "inbound")
            self.assertEqual(data["min_duration_minutes"], 1.5)
            self.assertEqual(data["max_duration_minutes"], 10.0)

    async def test_create_random_quality_monitoring_job_validations(self):
        token = self.make_token(101, "super@doobot.ai", "super_admin")
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Missing / invalid calls_per_day
        p1 = {
            "job_name": "Job sin calls_per_day",
            "job_mode": "random_quality_monitoring",
            "prompt_id": 100,
            "date_from": "2026-07-01T00:00:00Z",
            "date_to": "2026-07-05T23:59:59Z"
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r1 = await ac.post("/bm/mass-evaluation-jobs", json=p1, headers=headers)
            self.assertIn(r1.status_code, (400, 422))

        # 2. date_to < date_from
        p2 = {
            "job_name": "Job fechas invertidas",
            "job_mode": "random_quality_monitoring",
            "prompt_id": 100,
            "calls_per_day": 10,
            "date_from": "2026-07-10T00:00:00Z",
            "date_to": "2026-07-05T23:59:59Z"
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r2 = await ac.post("/bm/mass-evaluation-jobs", json=p2, headers=headers)
            self.assertIn(r2.status_code, (400, 422))

        # 3. time_to < time_from
        p3 = {
            "job_name": "Job horas invertidas",
            "job_mode": "random_quality_monitoring",
            "prompt_id": 100,
            "calls_per_day": 10,
            "date_from": "2026-07-01T00:00:00Z",
            "date_to": "2026-07-05T23:59:59Z",
            "time_from": "18:00",
            "time_to": "09:00"
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r3 = await ac.post("/bm/mass-evaluation-jobs", json=p3, headers=headers)
            self.assertIn(r3.status_code, (400, 422))

        # 4. min_duration > max_duration
        p4 = {
            "job_name": "Job duraciones invertidas",
            "job_mode": "random_quality_monitoring",
            "prompt_id": 100,
            "calls_per_day": 10,
            "date_from": "2026-07-01T00:00:00Z",
            "date_to": "2026-07-05T23:59:59Z",
            "min_duration_minutes": 10.0,
            "max_duration_minutes": 2.0
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r4 = await ac.post("/bm/mass-evaluation-jobs", json=p4, headers=headers)
            self.assertIn(r4.status_code, (400, 422))

    async def test_create_random_job_with_relative_dates_and_max_calls(self):
        token = self.make_token(101, "super@doobot.ai", "super_admin")
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Creation with relative date preset (last 2 days) and max_calls included
        payload = {
            "job_name": "Job Relativo 2 Días con max_calls",
            "job_mode": "random_quality_monitoring",
            "prompt_id": 100,
            "calls_per_day": 10,
            "relative_days": 2,
            "max_calls": 50,
            "agent_owner_ids": ["31499194"]
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/mass-evaluation-jobs", json=payload, headers=headers)
            self.assertEqual(res.status_code, 201, res.text)
            data = res.json()
            self.assertEqual(data["job_name"], "Job Relativo 2 Días con max_calls")
            self.assertEqual(data["job_mode"], "random_quality_monitoring")
            self.assertEqual(data["calls_per_day"], 10)
            self.assertEqual(data["relative_days"], 2)
            self.assertEqual(data["date_mode"], "relative")

    async def test_role_permissions_for_random_quality_monitoring(self):
        # Agent role: 403 Forbidden
        agent_token = self.make_token(103, "maria@boston.es", "agent")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/mass-evaluation-jobs", json={
                "job_name": "Agent Job",
                "job_mode": "random_quality_monitoring",
                "prompt_id": 100,
                "calls_per_day": 5,
                "date_from": "2026-07-01T00:00:00Z",
                "date_to": "2026-07-02T23:59:59Z"
            }, headers={"Authorization": f"Bearer {agent_token}"})
            self.assertEqual(res.status_code, 403)

        # Team coordinator including unauthorized agent: 403 Forbidden
        coord_token = self.make_token(102, "coord@boston.es", "team_coordinator")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/mass-evaluation-jobs", json={
                "job_name": "Coord Unauthorized Agent",
                "job_mode": "random_quality_monitoring",
                "prompt_id": 100,
                "calls_per_day": 5,
                "date_from": "2026-07-01T00:00:00Z",
                "date_to": "2026-07-02T23:59:59Z",
                "agent_owner_ids": ["99999999"]
            }, headers={"Authorization": f"Bearer {coord_token}"})
            self.assertEqual(res.status_code, 403)

    async def test_random_sampling_per_day_algorithm(self):
        hs_service = HubSpotService()
        
        async def mock_search_calls(filters):
            d_from = filters.get("date_from")
            if not d_from:
                return []
            day_str = d_from.strftime("%Y-%m-%d")
            if day_str == "2026-07-01":
                return [{"call_id": f"call_01_{i}", "recording_url": f"http://rec/01/{i}", "hubspot_owner_id": "31499194"} for i in range(50)]
            elif day_str == "2026-07-02":
                return [{"call_id": f"call_02_{i}", "recording_url": f"http://rec/02/{i}", "hubspot_owner_id": "31499194"} for i in range(7)]
            elif day_str == "2026-07-03":
                return []
            return []

        hs_service.search_calls_for_mass_evaluation = mock_search_calls

        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Madrid")
        d1 = datetime(2026, 7, 1, 0, 0, 0, tzinfo=tz)
        d3 = datetime(2026, 7, 3, 23, 59, 59, tzinfo=tz)

        filters = {
            "calls_per_day": 20,
            "date_from": d1,
            "date_to": d3,
            "timezone": "Europe/Madrid"
        }

        selected_calls, trace = await MassEvaluationService.select_random_calls_for_quality_monitoring(hs_service, filters)

        self.assertEqual(trace["total_candidates"], 57)
        self.assertEqual(trace["total_selected"], 27)
        self.assertEqual(len(selected_calls), 27)
        self.assertEqual(trace["candidates_count_by_day"], {"2026-07-01": 50, "2026-07-02": 7, "2026-07-03": 0})
        self.assertEqual(trace["selected_count_by_day"], {"2026-07-01": 20, "2026-07-02": 7, "2026-07-03": 0})
        self.assertEqual(len(trace["selected_call_ids_by_day"]["2026-07-01"]), 20)
        self.assertEqual(len(trace["selected_call_ids_by_day"]["2026-07-02"]), 7)
        self.assertEqual(len(trace["selected_call_ids_by_day"]["2026-07-03"]), 0)


if __name__ == "__main__":
    unittest.main()
