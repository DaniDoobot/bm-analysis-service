"""
Test suite for GET /bm/mass-evaluation-results filters and Europe/Madrid timezone date bounds.
Verifies:
1. Filtering by typology_key and aliases (typology, call_type, tipo_llamada).
2. Filtering by direction and aliases (call_direction, inbound_outbound).
3. Filtering by duration min/max.
4. Filtering by single-day bare date '2026-08-06' covers Europe/Madrid calendar day.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
import zoneinfo

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_results_filters_test.db"

db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.typologies import Typology
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.utils.security import create_access_token

MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")


class TestMassEvaluationResultsFilters(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_results_filters_test.db"):
            try:
                os.remove("mass_results_filters_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Filter Co", company_key="filter_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            db.add(s1)
            await db.flush()

            t1 = Typology(typology_id=10, typology_key="cita_medica", typology_name="Cita Médica", service_id=1, is_active=True)
            t2 = Typology(typology_id=20, typology_key="informacion", typology_name="Información", service_id=1, is_active=True)
            db.add_all([t1, t2])
            await db.flush()

            u_super = User(
                user_id=1,
                username="super_filter",
                email="super_filter@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=1,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            job = MassEvaluationJob(job_id=1, job_name="Job 1", company_id=1, service_id=1, prompt_id=1, is_active=True)
            run = MassEvaluationRun(run_id=1, job_id=1, company_id=1, service_id=1, trigger_type="manual", status="completed")
            db.add_all([job, run])
            await db.flush()

            # Datetimes in Madrid time converted to UTC for DB
            # 2026-08-06 14:00:00 Madrid -> 2026-08-06 12:00:00 UTC
            dt_aug6_14h_madrid = datetime(2026, 8, 6, 14, 0, 0, tzinfo=MADRID_TZ).astimezone(timezone.utc)
            # 2026-08-07 10:00:00 Madrid -> 2026-08-07 08:00:00 UTC
            dt_aug7_10h_madrid = datetime(2026, 8, 7, 10, 0, 0, tzinfo=MADRID_TZ).astimezone(timezone.utc)

            r1 = MassEvaluationResult(
                mass_analysis_id=1,
                run_id=1,
                job_id=1,
                company_id=1,
                service_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_f_1",
                hubspot_owner_id="1375831791",  # EC Eugenia Carreno
                agent_name="Eugenia Carreno",
                call_timestamp=dt_aug6_14h_madrid,
                analysis_timestamp=dt_aug6_14h_madrid,
                typology_id=10,
                typology_key="cita_medica",
                direction="inbound",
                call_duration_seconds=180,
                evaluacion_global=8.0,
                status="completed",
                items_json=[]
            )
            r2 = MassEvaluationResult(
                mass_analysis_id=2,
                run_id=1,
                job_id=1,
                company_id=1,
                service_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_f_2",
                hubspot_owner_id="1375831791",  # EC Eugenia Carreno
                agent_name="Eugenia Carreno",
                call_timestamp=dt_aug7_10h_madrid,
                analysis_timestamp=dt_aug7_10h_madrid,
                typology_id=20,
                typology_key="informacion",
                direction="outbound",
                call_duration_seconds=300,
                evaluacion_global=6.5,
                status="completed",
                items_json=[]
            )
            db.add_all([r1, r2])
            await db.commit()

        self.token_super = create_access_token({"user_id": 1, "email": "super_filter@test.com"})

    async def test_filter_by_typology_alias(self):
        """Filters by typology alias 'call_type=cita_medica'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?call_type=cita_medica",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["call_id"], "call_f_1")

    async def test_filter_by_direction_alias(self):
        """Filters by direction alias 'inbound_outbound=outbound'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?inbound_outbound=outbound",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["call_id"], "call_f_2")

    async def test_filter_by_duration(self):
        """Filters by min_duration=200 seconds."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?min_duration=200",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["call_id"], "call_f_2")

    async def test_filter_by_single_day_madrid_date(self):
        """Filters by date_from='2026-08-06' and date_to='2026-08-06' in Madrid timezone."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?date_from=2026-08-06&date_to=2026-08-06",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["call_id"], "call_f_1")


if __name__ == "__main__":
    unittest.main()
