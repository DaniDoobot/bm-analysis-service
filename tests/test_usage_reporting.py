"""
Unit test suite for FinOps AI Usage Reporting (Phase 4).
Covers:
- GET /bm/usage/summary: tokens, costs, audio duration, coverage, latencies.
- GET /bm/usage/by-company: grouping, company join, percentages summing to 100%.
- GET /bm/usage/by-service: service and company join, percentages.
- GET /bm/usage/by-module: module aggregation, functional area mapping.
- GET /bm/usage/by-model: model and provider breakdown.
- GET /bm/usage/timeseries: granularities, group_by dimensions.
- Cost separation: audio_related_cost strictly from audio events, no contamination from text calls.
- Timezone handling: Europe/Madrid period boundaries (day, week, month, quarter, custom).
- Strict tenant isolation: Super Admin, Company Admin, Service Manager, Agent (403), Coordinator (403).
- Zero events / empty dataset handling.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.dependencies import get_db
from app.main import app
from app.models.ai_usage import AIUsageEvent
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.services.usage_service import (
    compute_derived_metrics,
    resolve_functional_area,
    resolve_period_bounds,
)
from app.utils.dates import MADRID_TZ
from app.utils.security import create_access_token, hash_password


class TestFinOpsUsageReporting(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            pwd = hash_password("Secret123!")

            # 1. Companies
            self.c_boston = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c_other = Company(company_id=2, company_name="Acme Corp", company_key="acme-corp", is_active=True)
            db.add_all([self.c_boston, self.c_other])
            await db.flush()

            # 2. Services
            self.s_front = Service(service_id=1, service_name="Front", service_key="front", company_id=1, is_active=True)
            self.s_expac = Service(service_id=2, service_name="EXPAC", service_key="expac", company_id=1, is_active=True)
            self.s_acme_service = Service(service_id=3, service_name="Acme Sales", service_key="acme_sales", company_id=2, is_active=True)
            db.add_all([self.s_front, self.s_expac, self.s_acme_service])
            await db.flush()

            # 3. Users for tenant roles
            self.u_super = User(
                user_id=1,
                username="superadmin",
                email="super@speechbm.com",
                role="super_admin",
                password_hash=pwd,
                is_active=True,
            )
            self.u_company_admin = User(
                user_id=2,
                username="boston_admin",
                email="admin@boston.es",
                role="company_admin",
                company_id=1,
                password_hash=pwd,
                is_active=True,
            )
            self.u_service_mgr = User(
                user_id=3,
                username="front_mgr",
                email="mgr@boston.es",
                role="responsable_servicio",
                company_id=1,
                primary_service_id=1,
                password_hash=pwd,
                is_active=True,
            )
            self.u_coordinator = User(
                user_id=4,
                username="coord",
                email="coord@boston.es",
                role="coordinador_equipo",
                company_id=1,
                password_hash=pwd,
                is_active=True,
            )
            self.u_agent = User(
                user_id=5,
                username="agent",
                email="agent@boston.es",
                role="agente",
                company_id=1,
                password_hash=pwd,
                is_active=True,
            )
            self.u_unknown = User(
                user_id=6,
                username="unknown_user",
                email="unknown@boston.es",
                role="unknown_custom_role",
                company_id=1,
                password_hash=pwd,
                is_active=True,
            )
            db.add_all([self.u_super, self.u_company_admin, self.u_service_mgr, self.u_coordinator, self.u_agent, self.u_unknown])
            await db.flush()

            # Base anchor datetime in UTC for test events (2026-10-10 12:00:00 UTC)
            self.base_dt = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

            # 4. AI Usage Events
            # Event 1: Audio Mass Evaluation (Boston Front) - priced
            self.ev1 = AIUsageEvent(
                id=1,
                created_at=self.base_dt,
                company_id=1,
                service_id=1,
                module="mass_evaluations",
                operation="mass_eval",
                provider="gemini",
                model="gemini-3.8-flash",
                prompt_token_count=1000,
                candidates_token_count=200,
                total_token_count=1200,
                cached_content_token_count=100,
                thoughts_token_count=50,
                input_text_tokens=200,
                input_audio_tokens=800,
                output_text_tokens=150,
                output_audio_tokens=0,
                audio_duration_seconds=Decimal("120.00"),  # 2.0 mins
                execution_latency_ms=1500,
                status="success",
                estimated_cost_usd=Decimal("0.004000"),
                pricing_version="v1",
                pricing_status="priced",
            )

            # Event 2: Audio Live Trainer (Boston EXPAC) - partial
            self.ev2 = AIUsageEvent(
                id=2,
                created_at=self.base_dt + timedelta(hours=2),
                company_id=1,
                service_id=2,
                module="trainer_voice_session",
                operation="live_voice",
                provider="gemini",
                model="gemini-3.1-flash-live-preview",
                prompt_token_count=500,
                candidates_token_count=100,
                total_token_count=600,
                cached_content_token_count=0,
                thoughts_token_count=0,
                input_text_tokens=0,
                input_audio_tokens=500,
                output_text_tokens=0,
                output_audio_tokens=100,
                audio_duration_seconds=Decimal("60.00"),  # 1.0 min
                execution_latency_ms=800,
                status="success",
                estimated_cost_usd=Decimal("0.002000"),
                pricing_version="v1",
                pricing_status="partial",
            )

            # Event 3: Text-only Tutor Chat (Boston Front) - priced
            # NOTE: audio_duration_seconds = 0 -> MUST NOT contaminate audio cost/min!
            self.ev3 = AIUsageEvent(
                id=3,
                created_at=self.base_dt + timedelta(hours=4),
                company_id=1,
                service_id=1,
                module="tutor_chat",
                operation="chat_message",
                provider="gemini",
                model="gemini-3.8-flash",
                prompt_token_count=300,
                candidates_token_count=100,
                total_token_count=400,
                cached_content_token_count=0,
                thoughts_token_count=0,
                input_text_tokens=300,
                input_audio_tokens=0,
                output_text_tokens=100,
                output_audio_tokens=0,
                audio_duration_seconds=Decimal("0.00"),
                execution_latency_ms=600,
                status="success",
                estimated_cost_usd=Decimal("0.001000"),
                pricing_version="v1",
                pricing_status="priced",
            )

            # Event 4: Unpriced Test Analysis in Acme Corp (company 2) - unpriced
            self.ev4 = AIUsageEvent(
                id=4,
                created_at=self.base_dt + timedelta(hours=6),
                company_id=2,
                service_id=3,
                module="test_analysis",
                operation="single_test",
                provider="gemini",
                model="unknown-legacy-model",
                prompt_token_count=150,
                candidates_token_count=50,
                total_token_count=200,
                cached_content_token_count=0,
                thoughts_token_count=0,
                input_text_tokens=150,
                input_audio_tokens=0,
                output_text_tokens=50,
                output_audio_tokens=0,
                audio_duration_seconds=None,
                execution_latency_ms=500,
                status="success",
                estimated_cost_usd=Decimal("0.000000"),
                pricing_version=None,
                pricing_status="unpriced",
            )

            # Event 5: Prompt Builder in Boston (company 1, no service) - priced
            self.ev5 = AIUsageEvent(
                id=5,
                created_at=self.base_dt + timedelta(hours=8),
                company_id=1,
                service_id=None,
                module="prompt_builder",
                operation="generate_system_prompt",
                provider="gemini",
                model="gemini-3.8-flash",
                prompt_token_count=800,
                candidates_token_count=400,
                total_token_count=1200,
                cached_content_token_count=200,
                thoughts_token_count=100,
                input_text_tokens=800,
                input_audio_tokens=0,
                output_text_tokens=400,
                output_audio_tokens=0,
                audio_duration_seconds=Decimal("0.00"),
                execution_latency_ms=1200,
                status="success",
                estimated_cost_usd=Decimal("0.003000"),
                pricing_version="v1",
                pricing_status="priced",
            )

            db.add_all([self.ev1, self.ev2, self.ev3, self.ev4, self.ev5])
            await db.commit()

        # Auth tokens
        self.tok_super = create_access_token({"user_id": 1, "email": "super@speechbm.com"})
        self.tok_comp_admin = create_access_token({"user_id": 2, "email": "admin@boston.es"})
        self.tok_service_mgr = create_access_token({"user_id": 3, "email": "mgr@boston.es"})
        self.tok_coord = create_access_token({"user_id": 4, "email": "coord@boston.es"})
        self.tok_agent = create_access_token({"user_id": 5, "email": "agent@boston.es"})
        self.tok_unknown = create_access_token({"user_id": 6, "email": "unknown@boston.es"})

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.client.aclose()
        await self.engine.dispose()

    # ── 1. SUMMARY METRICS & CONTAMINATION PREVENTION ─────────────────────────
    async def test_get_summary_metrics_and_audio_cost_isolation(self):
        """
        Verify:
        - Total events = 5
        - Total cost = 0.004 + 0.002 + 0.001 + 0.000 + 0.003 = 0.010000 USD
        - Audio duration = 120s + 60s = 180s = 3.0 minutes
        - Audio-related cost = 0.004 + 0.002 = 0.006000 USD (strictly ev1 and ev2)
        - Cost per audio min = 0.006 / 3.0 = 0.002000 USD/min
          (ev3, ev4, ev5 have 0 audio duration and MUST NOT contaminate cost/min)
        - Cost per event = 0.010 / 5 = 0.002000 USD/event
        - Token breakdowns match exact sums
        - Coverage = (3 priced + 1 partial) / 5 = 80.0%
        """
        res = await self.client.get(
            "/bm/usage/summary?date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()

        self.assertEqual(data["total_events"], 5)
        self.assertAlmostEqual(data["total_cost_usd"], 0.010000, places=5)
        self.assertEqual(data["currency"], "USD")

        # Audio checks
        self.assertAlmostEqual(data["audio_seconds"], 180.0, places=1)
        self.assertAlmostEqual(data["audio_minutes"], 3.0, places=2)
        self.assertAlmostEqual(data["audio_related_cost_usd"], 0.006000, places=5)
        self.assertAlmostEqual(data["cost_per_audio_minute_usd"], 0.002000, places=5)
        self.assertAlmostEqual(data["cost_per_event_usd"], 0.002000, places=5)

        # Tokens
        # total_tokens: 1200 + 600 + 400 + 200 + 1200 = 3600
        self.assertEqual(data["total_tokens"], 3600)
        # prompt: 1000 + 500 + 300 + 150 + 800 = 2750
        self.assertEqual(data["prompt_tokens"], 2750)
        # candidates: 200 + 100 + 100 + 50 + 400 = 850
        self.assertEqual(data["candidates_tokens"], 850)
        # cached: 100 + 0 + 0 + 0 + 200 = 300
        self.assertEqual(data["cached_tokens"], 300)
        # thoughts: 50 + 0 + 0 + 0 + 100 = 150
        self.assertEqual(data["thoughts_tokens"], 150)

        # Coverage
        self.assertEqual(data["priced_events"], 3)
        self.assertEqual(data["partial_events"], 1)
        self.assertEqual(data["unpriced_events"], 1)
        self.assertAlmostEqual(data["cost_coverage_percentage"], 80.0, places=1)

    # ── 2. BREAKDOWN BY COMPANY ───────────────────────────────────────────────
    async def test_get_usage_by_company(self):
        """
        Verify:
        - Boston Medical (company_id=1): 4 events, cost 0.010000 USD (100.0%)
        - Acme Corp (company_id=2): 1 event, cost 0.000000 USD (0.0%)
        - Percentages sum to 100.0%
        - Company names joined correctly from bm_companies
        """
        res = await self.client.get(
            "/bm/usage/by-company?date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()

        self.assertAlmostEqual(data["total_cost_usd"], 0.010000, places=5)
        items = data["items"]
        self.assertEqual(len(items), 2)

        boston = next((i for i in items if i["company_id"] == 1), None)
        self.assertIsNotNone(boston)
        self.assertEqual(boston["company_name"], "Boston Medical")
        self.assertEqual(boston["total_events"], 4)
        self.assertAlmostEqual(boston["total_cost_usd"], 0.010000, places=5)
        self.assertAlmostEqual(boston["percentage_of_total_cost"], 100.0, places=1)

        acme = next((i for i in items if i["company_id"] == 2), None)
        self.assertIsNotNone(acme)
        self.assertEqual(acme["company_name"], "Acme Corp")
        self.assertEqual(acme["total_events"], 1)
        self.assertAlmostEqual(acme["total_cost_usd"], 0.000000, places=5)
        self.assertAlmostEqual(acme["percentage_of_total_cost"], 0.0, places=1)

    # ── 3. BREAKDOWN BY SERVICE ───────────────────────────────────────────────
    async def test_get_usage_by_service(self):
        """
        Verify:
        - Front (service_id=1): 2 events (ev1 + ev3), cost 0.005000 USD (50.0%)
        - Prompt Builder (service_id=None): 1 event (ev5), cost 0.003000 USD (30.0%) -> 'Sin Servicio'
        - EXPAC (service_id=2): 1 event (ev2), cost 0.002000 USD (20.0%)
        - Acme Sales (service_id=3): 1 event (ev4), cost 0.000000 USD (0.0%)
        - Percentages sum to 100.0%
        """
        res = await self.client.get(
            "/bm/usage/by-service?date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()

        items = data["items"]
        self.assertEqual(len(items), 4)

        pct_sum = sum(i["percentage_of_total_cost"] for i in items)
        self.assertAlmostEqual(pct_sum, 100.0, places=1)

        front = next((i for i in items if i["service_id"] == 1), None)
        self.assertIsNotNone(front)
        self.assertEqual(front["service_name"], "Front")
        self.assertEqual(front["total_events"], 2)
        self.assertAlmostEqual(front["total_cost_usd"], 0.005000, places=5)
        self.assertAlmostEqual(front["percentage_of_total_cost"], 50.0, places=1)

        no_service = next((i for i in items if i["service_id"] is None), None)
        self.assertIsNotNone(no_service)
        self.assertEqual(no_service["service_name"], "Sin Servicio")
        self.assertEqual(no_service["total_events"], 1)
        self.assertAlmostEqual(no_service["total_cost_usd"], 0.003000, places=5)
        self.assertAlmostEqual(no_service["percentage_of_total_cost"], 30.0, places=1)

    # ── 4. BREAKDOWN BY MODULE AND FUNCTIONAL AREA ────────────────────────────
    async def test_get_usage_by_module_and_functional_area(self):
        """
        Verify module aggregation and functional area taxonomy:
        - mass_evaluations -> MASIVAS
        - trainer_voice_session -> TRAINER
        - tutor_chat -> TUTOR_IA
        - test_analysis -> LABORATORIO
        - prompt_builder -> CONFIGURACION_IA
        """
        res = await self.client.get(
            "/bm/usage/by-module?date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()

        items = {i["module"]: i for i in data["items"]}
        self.assertEqual(items["mass_evaluations"]["functional_area"], "MASIVAS")
        self.assertEqual(items["trainer_voice_session"]["functional_area"], "TRAINER")
        self.assertEqual(items["tutor_chat"]["functional_area"], "TUTOR_IA")
        self.assertEqual(items["test_analysis"]["functional_area"], "LABORATORIO")
        self.assertEqual(items["prompt_builder"]["functional_area"], "CONFIGURACION_IA")

    # ── 5. BREAKDOWN BY MODEL ─────────────────────────────────────────────────
    async def test_get_usage_by_model(self):
        """
        Verify:
        - gemini-3.8-flash: 3 events (ev1, ev3, ev5), cost 0.008000 USD (80.0%)
        - gemini-3.1-flash-live-preview: 1 event (ev2), cost 0.002000 USD (20.0%)
        - unknown-legacy-model: 1 event (ev4), cost 0.000000 USD (0.0%)
        """
        res = await self.client.get(
            "/bm/usage/by-model?date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()

        items = {i["model"]: i for i in data["items"]}
        self.assertAlmostEqual(items["gemini-3.8-flash"]["total_cost_usd"], 0.008000, places=5)
        self.assertAlmostEqual(items["gemini-3.8-flash"]["percentage_of_total_cost"], 80.0, places=1)
        self.assertAlmostEqual(items["gemini-3.1-flash-live-preview"]["total_cost_usd"], 0.002000, places=5)
        self.assertAlmostEqual(items["gemini-3.1-flash-live-preview"]["percentage_of_total_cost"], 20.0, places=1)

    # ── 6. TIMESERIES TRENDS ──────────────────────────────────────────────────
    async def test_get_usage_timeseries(self):
        """
        Verify:
        - Timeseries query with granularity='day' and group_by='none'.
        - Timeseries query with group_by='functional_area'.
        - Valid timestamps and points.
        """
        res = await self.client.get(
            "/bm/usage/timeseries?date_from=2026-10-01&date_to=2026-10-31&granularity=day&group_by=none",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["granularity"], "day")
        self.assertEqual(data["group_by"], "none")
        self.assertGreaterEqual(len(data["points"]), 1)
        self.assertAlmostEqual(data["total_cost_usd"], 0.010000, places=5)

        # Test group_by=functional_area
        res_fa = await self.client.get(
            "/bm/usage/timeseries?date_from=2026-10-01&date_to=2026-10-31&granularity=day&group_by=functional_area",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res_fa.status_code, 200, res_fa.text)
        data_fa = res_fa.json()
        self.assertEqual(data_fa["group_by"], "functional_area")
        keys = {p["group_key"] for p in data_fa["points"]}
        self.assertTrue("MASIVAS" in keys or "TRAINER" in keys)

    # ── 7. TENANT ISOLATION: SUPER ADMIN ──────────────────────────────────────
    async def test_tenant_super_admin_unrestricted(self):
        """Super Admin can query all companies and filter by any specific company."""
        # Query all
        res_all = await self.client.get(
            "/bm/usage/summary?date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res_all.status_code, 200)
        self.assertEqual(res_all.json()["total_events"], 5)

        # Filter Acme (company 2)
        res_acme = await self.client.get(
            "/bm/usage/summary?company_id=2&date_from=2026-10-01&date_to=2026-10-31",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res_acme.status_code, 200)
        self.assertEqual(res_acme.json()["total_events"], 1)

    # ── 8. TENANT ISOLATION: COMPANY ADMIN FORBIDDEN ──────────────────────────
    async def test_tenant_company_admin_forbidden(self):
        """Company Admin must receive 403 Forbidden on all usage endpoints."""
        endpoints = [
            "/bm/usage/summary",
            "/bm/usage/timeseries",
            "/bm/usage/by-company",
            "/bm/usage/by-service",
            "/bm/usage/by-module",
            "/bm/usage/by-model",
        ]
        for ep in endpoints:
            res = await self.client.get(
                ep,
                headers={"Authorization": f"Bearer {self.tok_comp_admin}"},
            )
            self.assertEqual(res.status_code, 403, f"Expected 403 for company admin on {ep}, got {res.status_code}")
            self.assertIn("Acceso denegado", res.json()["detail"])

    # ── 9. TENANT ISOLATION: SERVICE MANAGER FORBIDDEN ────────────────────────
    async def test_tenant_service_manager_forbidden(self):
        """Service Manager must receive 403 Forbidden on all usage endpoints."""
        endpoints = [
            "/bm/usage/summary",
            "/bm/usage/timeseries",
            "/bm/usage/by-company",
            "/bm/usage/by-service",
            "/bm/usage/by-module",
            "/bm/usage/by-model",
        ]
        for ep in endpoints:
            res = await self.client.get(
                ep,
                headers={"Authorization": f"Bearer {self.tok_service_mgr}"},
            )
            self.assertEqual(res.status_code, 403, f"Expected 403 for service manager on {ep}, got {res.status_code}")
            self.assertIn("Acceso denegado", res.json()["detail"])

    # ── 10. FORBIDDEN FOR AGENTS, COORDINATORS AND UNKNOWN ROLES ──────────────
    async def test_non_superadmin_roles_forbidden(self):
        """Agents, Team Coordinators, and Unknown roles must receive 403 Forbidden on all usage endpoints."""
        endpoints = [
            "/bm/usage/summary",
            "/bm/usage/timeseries",
            "/bm/usage/by-company",
            "/bm/usage/by-service",
            "/bm/usage/by-module",
            "/bm/usage/by-model",
        ]
        for token, role_label in [
            (self.tok_coord, "coordinator"),
            (self.tok_agent, "agent"),
            (self.tok_unknown, "unknown_role"),
        ]:
            for ep in endpoints:
                res = await self.client.get(
                    ep,
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(res.status_code, 403, f"Expected 403 for {role_label} on {ep}, got {res.status_code}")
                self.assertIn("Acceso denegado", res.json()["detail"])

    # ── 11. TIMEZONE PERIOD BOUNDS (MADRID -> UTC) ────────────────────────────
    def test_madrid_period_bounds_calculations(self):
        """Test unit resolution of Europe/Madrid natural bounds to UTC."""
        ref_dt = datetime(2026, 10, 13, 15, 30, 0, tzinfo=MADRID_TZ)

        # 1. Day
        p, dt_from, dt_to = resolve_period_bounds(period="day", ref_dt=ref_dt)
        self.assertEqual(p, "day")
        # In Madrid October (CEST = UTC+2): 2026-10-13 00:00:00 is 2026-10-12 22:00:00 UTC
        self.assertEqual(dt_from.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-13 00:00:00")
        self.assertEqual(dt_to.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-13 23:59:59")

        # 2. Week (Monday 2026-10-12 to Sunday 2026-10-18)
        p, dt_from, dt_to = resolve_period_bounds(period="week", ref_dt=ref_dt)
        self.assertEqual(p, "week")
        self.assertEqual(dt_from.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-12 00:00:00")
        self.assertEqual(dt_to.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-18 23:59:59")

        # 3. Month (2026-10-01 to 2026-10-31)
        p, dt_from, dt_to = resolve_period_bounds(period="month", ref_dt=ref_dt)
        self.assertEqual(p, "month")
        self.assertEqual(dt_from.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-01 00:00:00")
        self.assertEqual(dt_to.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-31 23:59:59")

        # 4. Quarter (Q4: 2026-10-01 to 2026-12-31)
        p, dt_from, dt_to = resolve_period_bounds(period="quarter", ref_dt=ref_dt)
        self.assertEqual(p, "quarter")
        self.assertEqual(dt_from.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-01 00:00:00")
        self.assertEqual(dt_to.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-12-31 23:59:59")

        # 5. Custom
        p, dt_from, dt_to = resolve_period_bounds(date_from="2026-05-01", date_to="2026-05-15")
        self.assertEqual(p, "custom")
        self.assertEqual(dt_from.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-05-01 00:00:00")
        self.assertEqual(dt_to.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S"), "2026-05-15 23:59:59")

    # ── 12. ZERO EVENTS / EMPTY STATE ─────────────────────────────────────────
    async def test_zero_events_empty_state_returns_cleanly(self):
        """Querying a date window with no events returns zeros and empty arrays without errors."""
        res = await self.client.get(
            "/bm/usage/summary?date_from=2025-01-01&date_to=2025-01-02",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total_events"], 0)
        self.assertEqual(data["total_cost_usd"], 0.0)
        self.assertEqual(data["audio_minutes"], 0.0)
        self.assertIsNone(data["cost_per_audio_minute_usd"])
        self.assertIsNone(data["cost_per_event_usd"])
        self.assertEqual(data["cost_coverage_percentage"], 100.0)

        # Empty breakdown
        res_comp = await self.client.get(
            "/bm/usage/by-company?date_from=2025-01-01&date_to=2025-01-02",
            headers={"Authorization": f"Bearer {self.tok_super}"},
        )
        self.assertEqual(res_comp.status_code, 200)
        self.assertEqual(res_comp.json()["items"], [])
        self.assertEqual(res_comp.json()["total_cost_usd"], 0.0)
