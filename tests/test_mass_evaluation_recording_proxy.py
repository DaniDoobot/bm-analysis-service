"""
tests/test_mass_evaluation_recording_proxy.py
==============================================
Security and regression tests for Mass Evaluation & Dashboard recording audio proxy.

Verifies:
1. Authorized user + own call: 200 and audio bytes.
2. Unauthenticated request: 401 Unauthorized.
3. Cross-tenant access (Company A user requesting Company B audio): 403 Forbidden.
4. Service manager requesting call outside their service scope: 403 Forbidden.
5. Agent requesting call outside their allowed agent IDs: 403 Forbidden.
6. Non-existent mass_analysis_id: 404 Not Found.
7. Null recording_url in DB: controlled 404 Not Found without 500 error.
8. Twilio download failure: controlled 502 Bad Gateway, and 404 when upstream returns 404.
9. Security headers: Cache-Control: private, no-store, Content-Type: audio/mpeg, inline disposition.
10. Mass evaluations list (/bm/mass-evaluations/results): recording_url does NOT contain api.twilio.com.
11. Mass evaluations detail (/bm/mass-evaluations/results/{id}): recording_url does NOT contain api.twilio.com.
12. /me analysis results endpoints (/bm/me/analysis-results): recording_url does NOT contain api.twilio.com.
13. Dashboard latest-analysis (/bm/dashboard/latest-analyses/{identifier}): recording_url is rewritten to proxy.
14. External URL is never serialized in API payloads.
"""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_mass_eval_proxy_test.db"

import httpx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import BigInteger, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.main import app
from app.db import Base, get_engine
from app.dependencies import get_db, get_tenant_context, get_current_user
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)


class TestMassEvaluationRecordingProxy(unittest.IsolatedAsyncioTestCase):
    """Full test suite covering all 14 mandatory security and regression requirements."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(MassEvaluationRun))
            await db.execute(delete(MassEvaluationJob))
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.execute(delete(User))

            # Companies
            c1 = Company(company_id=1, company_name="Boston Medical Group", company_key="bmg")
            c2 = Company(company_id=2, company_name="Other Company", company_key="other")
            db.add_all([c1, c2])
            await db.flush()

            # Services
            s1 = Service(service_id=1, company_id=1, service_key="atencion", service_name="Atención")
            s2 = Service(service_id=2, company_id=1, service_key="ventas", service_name="Ventas")
            db.add_all([s1, s2])
            await db.flush()

            # Job & Run for Mass Evaluation
            job = MassEvaluationJob(job_id=1, company_id=1, service_id=1, job_name="Job 1", prompt_id=1)
            db.add(job)
            await db.flush()

            run = MassEvaluationRun(run_id=1, job_id=1, company_id=1, service_id=1, trigger_type="manual", status="completed")
            db.add(run)
            await db.flush()

            # Call 1: Company 1, Service 1, Agent A, with Twilio recording_url
            self.res1 = MassEvaluationResult(
                mass_analysis_id=101,
                job_id=1,
                run_id=1,
                call_id="call_101",
                company_id=1,
                service_id=1,
                hubspot_owner_id="AGENT_A",
                agent_name="Agent A",
                status="completed",
                call_timestamp=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
                analysis_timestamp=datetime(2026, 9, 1, 10, 5, 0, tzinfo=timezone.utc),
                recording_url="https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE101.mp3",
                prompt_id=1,
                prompt_snapshot="{}",
                result_json={"evaluacion_global": 8.5},
            )

            # Call 2: Company 1, Service 2, Agent B, with Twilio recording_url
            self.res2 = MassEvaluationResult(
                mass_analysis_id=102,
                job_id=1,
                run_id=1,
                call_id="call_102",
                company_id=1,
                service_id=2,
                hubspot_owner_id="AGENT_B",
                agent_name="Agent B",
                status="completed",
                call_timestamp=datetime(2026, 9, 1, 11, 0, 0, tzinfo=timezone.utc),
                analysis_timestamp=datetime(2026, 9, 1, 11, 5, 0, tzinfo=timezone.utc),
                recording_url="https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE102.mp3",
                prompt_id=1,
                prompt_snapshot="{}",
                result_json={"evaluacion_global": 7.0},
            )

            # Call 3: Company 2 (Cross-tenant), Service 1, Agent C
            self.res3 = MassEvaluationResult(
                mass_analysis_id=103,
                job_id=1,
                run_id=1,
                call_id="call_103",
                company_id=2,
                service_id=1,
                hubspot_owner_id="AGENT_C",
                agent_name="Agent C",
                status="completed",
                call_timestamp=datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc),
                analysis_timestamp=datetime(2026, 9, 1, 12, 5, 0, tzinfo=timezone.utc),
                recording_url="https://api.twilio.com/2010-04-01/Accounts/AC456/Recordings/RE103.mp3",
                prompt_id=1,
                prompt_snapshot="{}",
                result_json={"evaluacion_global": 9.0},
            )

            # Call 4: Company 1, Service 1, Agent A, WITHOUT recording_url (None)
            self.res4 = MassEvaluationResult(
                mass_analysis_id=104,
                job_id=1,
                run_id=1,
                call_id="call_104",
                company_id=1,
                service_id=1,
                hubspot_owner_id="AGENT_A",
                agent_name="Agent A",
                status="completed",
                call_timestamp=datetime(2026, 9, 1, 13, 0, 0, tzinfo=timezone.utc),
                analysis_timestamp=datetime(2026, 9, 1, 13, 5, 0, tzinfo=timezone.utc),
                recording_url=None,
                prompt_id=1,
                prompt_snapshot="{}",
                result_json={"evaluacion_global": 6.5},
            )

            db.add_all([self.res1, self.res2, self.res3, self.res4])
            await db.commit()

        # Build reusable tenant contexts
        self.superadmin_context = TenantContext(
            user_id=1,
            raw_role="superadmin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1, 2],
        )

        self.c1_admin_context = TenantContext(
            user_id=10,
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
        )

        self.c2_admin_context = TenantContext(
            user_id=20,
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=2,
            allowed_company_ids=[2],
        )

        self.service1_manager_context = TenantContext(
            user_id=30,
            raw_role="service_manager",
            normalized_role=InternalRole.SERVICE_MANAGER,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
        )

        self.agent_a_context = TenantContext(
            user_id=40,
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=["AGENT_A"],
        )

    def tearDown(self):
        app.dependency_overrides.clear()

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 1: Usuario autorizado + llamada propia: 200 y bytes de audio
    # ─────────────────────────────────────────────────────────────────────────
    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_01_authorized_user_own_call_success(self, mock_download):
        mock_download.return_value = b"fake_mp3_audio_stream"
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101/recording-audio")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, b"fake_mp3_audio_stream")
            self.assertEqual(res.headers.get("content-type"), "audio/mpeg")
            mock_download.assert_awaited_once_with(
                "https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE101.mp3"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 2: Sin autenticación: 401/403 según comportamiento estándar
    # ─────────────────────────────────────────────────────────────────────────
    async def test_02_unauthenticated_request_fails(self):
        # Do not override get_tenant_context -> standard dependency raises 401
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101/recording-audio")
            self.assertEqual(res.status_code, 401)

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 3: Usuario company A intentando audio company B: bloqueado (403)
    # ─────────────────────────────────────────────────────────────────────────
    async def test_03_cross_tenant_access_blocked(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c2_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101/recording-audio")
            self.assertEqual(res.status_code, 403)
            self.assertIn("este resultado pertenece a otra empresa", res.json().get("detail", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 4: Service manager intentando llamada fuera de sus servicios: bloqueado (403)
    # ─────────────────────────────────────────────────────────────────────────
    async def test_04_service_manager_out_of_service_scope_blocked(self):
        # Manager only has service 1; call 102 belongs to service 2
        app.dependency_overrides[get_tenant_context] = lambda: self.service1_manager_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/102/recording-audio")
            self.assertEqual(res.status_code, 403)
            self.assertIn("este resultado pertenece a un servicio no asignado", res.json().get("detail", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 5: Agente intentando llamada fuera de sus allowed_agent_ids: bloqueado (403)
    # ─────────────────────────────────────────────────────────────────────────
    async def test_05_agent_out_of_agent_scope_blocked(self):
        # Agent A context requests call 102 (belongs to AGENT_B)
        app.dependency_overrides[get_tenant_context] = lambda: self.agent_a_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/102/recording-audio")
            self.assertEqual(res.status_code, 403)
            self.assertIn("No tienes permiso para consultar este análisis", res.json().get("detail", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 6: mass_analysis_id inexistente: 404
    # ─────────────────────────────────────────────────────────────────────────
    async def test_06_nonexistent_analysis_returns_404(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/99999/recording-audio")
            self.assertEqual(res.status_code, 404)
            self.assertIn("not found", res.json().get("detail", "").lower())

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 7: recording_url nula: respuesta controlada 404, sin error 500
    # ─────────────────────────────────────────────────────────────────────────
    async def test_07_null_recording_url_returns_controlled_404(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/104/recording-audio")
            self.assertEqual(res.status_code, 404)
            self.assertIn("Grabación no disponible", res.json().get("detail", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 8: Fallo de Twilio / download: 502 o 404 controlado
    # ─────────────────────────────────────────────────────────────────────────
    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_08_twilio_failures_return_controlled_responses(self, mock_download):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        # Case 8A: Twilio returns 401 Unauthorized
        req = httpx.Request("GET", "https://api.twilio.com/mock")
        resp_401 = httpx.Response(401, request=req)
        mock_download.side_effect = httpx.HTTPStatusError("Unauthorized", request=req, response=resp_401)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101/recording-audio")
            self.assertEqual(res.status_code, 502)
            self.assertIn("No se pudo recuperar la grabación desde Twilio", res.json().get("detail", ""))

        # Case 8B: Upstream returns 404 (e.g. synthetic or expired recording)
        resp_404 = httpx.Response(404, request=req)
        mock_download.side_effect = httpx.HTTPStatusError("Not Found", request=req, response=resp_404)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101/recording-audio")
            self.assertEqual(res.status_code, 404)
            self.assertIn("Grabación no disponible", res.json().get("detail", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 9: Headers: Cache-Control private, no-store
    # ─────────────────────────────────────────────────────────────────────────
    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_09_security_headers_present(self, mock_download):
        mock_download.return_value = b"sample_audio_stream"
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101/recording-audio")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertEqual(res.headers.get("content-type"), "audio/mpeg")
            self.assertIn("inline", res.headers.get("content-disposition", ""))
            self.assertIn("mass_result_101.mp3", res.headers.get("content-disposition", ""))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 10: Listado Mass Evaluations: recording_url NO contiene api.twilio.com
    # ─────────────────────────────────────────────────────────────────────────
    async def test_10_mass_evaluations_list_rewrites_recording_url(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            items = data.get("items", [])
            self.assertTrue(len(items) >= 2)

            for item in items:
                rec = item.get("recording_url")
                if rec:
                    self.assertNotIn("api.twilio.com", rec)
                    self.assertTrue(rec.startswith("/bm/mass-evaluations/results/"))
                    self.assertTrue(rec.endswith("/recording-audio"))

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 11: Detalle Mass Evaluations: recording_url NO contiene api.twilio.com
    # ─────────────────────────────────────────────────────────────────────────
    async def test_11_mass_evaluations_detail_rewrites_recording_url(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/mass-evaluations/results/101")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            rec = data.get("recording_url")
            self.assertIsNotNone(rec)
            self.assertNotIn("api.twilio.com", rec)
            self.assertEqual(rec, "/bm/mass-evaluations/results/101/recording-audio")

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 12: Endpoint /me: recording_url NO contiene api.twilio.com
    # ─────────────────────────────────────────────────────────────────────────
    async def test_12_me_endpoints_rewrite_recording_url(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.agent_a_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 12A: List /bm/me/analysis-results
            res_list = await client.get("/bm/me/analysis-results")
            self.assertEqual(res_list.status_code, 200)
            items = res_list.json().get("items", [])
            self.assertTrue(len(items) >= 1)
            for it in items:
                rec = it.get("recording_url")
                if rec:
                    self.assertNotIn("api.twilio.com", rec)
                    self.assertEqual(rec, f"/bm/mass-evaluations/results/{it['mass_analysis_id']}/recording-audio")

            # 12B: Detail /bm/me/analysis-results/101
            res_detail = await client.get("/bm/me/analysis-results/101")
            self.assertEqual(res_detail.status_code, 200)
            rec_detail = res_detail.json().get("recording_url")
            self.assertIsNotNone(rec_detail)
            self.assertNotIn("api.twilio.com", rec_detail)
            self.assertEqual(rec_detail, "/bm/mass-evaluations/results/101/recording-audio")

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 13: Dashboard latest-analysis: recording_url NO contiene api.twilio.com
    # ─────────────────────────────────────────────────────────────────────────
    async def test_13_dashboard_latest_analysis_rewrites_recording_url(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Queried by mass_analysis_id
            res_by_id = await client.get("/bm/dashboard/latest-analyses/101")
            self.assertEqual(res_by_id.status_code, 200)
            rec_by_id = res_by_id.json().get("recording_url")
            self.assertIsNotNone(rec_by_id)
            self.assertNotIn("api.twilio.com", rec_by_id)
            self.assertEqual(rec_by_id, "/bm/mass-evaluations/results/101/recording-audio")

            # Queried by call_id string
            res_by_call = await client.get("/bm/dashboard/latest-analyses/call_101")
            self.assertEqual(res_by_call.status_code, 200)
            rec_by_call = res_by_call.json().get("recording_url")
            self.assertIsNotNone(rec_by_call)
            self.assertNotIn("api.twilio.com", rec_by_call)
            self.assertEqual(rec_by_call, "/bm/mass-evaluations/results/101/recording-audio")

    # ─────────────────────────────────────────────────────────────────────────
    # Requirement 14: Comprobar que una URL externa nunca aparece serializada accidentalmente
    # ─────────────────────────────────────────────────────────────────────────
    async def test_14_external_twilio_url_never_serialized_in_payloads(self):
        app.dependency_overrides[get_tenant_context] = lambda: self.c1_admin_context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            endpoints_to_test = [
                "/bm/mass-evaluations/results",
                "/bm/mass-evaluation-results",
                "/bm/mass-evaluations/results/101",
                "/bm/mass-evaluation-results/101",
                "/bm/me/analysis-results",
                "/bm/me/analysis-results/101",
                "/bm/dashboard/latest-analyses/101",
                "/bm/dashboard/latest-analyses/call_101",
            ]
            for endpoint in endpoints_to_test:
                res = await client.get(endpoint)
                self.assertEqual(res.status_code, 200, f"Endpoint {endpoint} failed")
                self.assertNotIn(
                    "api.twilio.com",
                    res.text,
                    f"Leak detected: 'api.twilio.com' was serialized in response from {endpoint}"
                )


if __name__ == "__main__":
    unittest.main()
