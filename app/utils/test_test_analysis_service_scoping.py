"""
Regression test suite for Test Analysis service & prompt selection.

Validates that:
1. Test Analysis by call_id / audio upload respects the selected service_id / prompt_id.
2. Does NOT fall back to Front when a different service_id is selected.
3. Explicit prompt_id derives service_id and company_id, and rejects discordant service_id with 400/422.
4. If a service has no active audio prompt, returns 422 error and does NOT fall back to Front.
5. Re-analyzing the same call_id with a different service creates separate history in bm_analyses and updates bm_call_analysis_current.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

# Force local sqlite test database
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_analysis_scoping_test.db"

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
from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.analyses import Analysis, CallAnalysisCurrent
from app.utils.security import create_access_token
from app.main import app
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class TestAnalysisServiceScoping(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("test_analysis_scoping_test.db"):
            try:
                os.remove("test_analysis_scoping_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            # 1. Company
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            db.add(c1)
            await db.flush()

            # 2. Services: Service 1 (Front), Service 3 (Asesores), Service 4 (No active prompt)
            s1 = Service(service_id=1, service_name="Front Desk Boston", service_key="front", company_id=1)
            s3 = Service(service_id=3, service_name="Asesores Comerciales", service_key="asesores", company_id=1)
            s4 = Service(service_id=4, service_name="Servicio Sin Prompts", service_key="sin-prompts", company_id=1)
            db.add_all([s1, s3, s4])
            await db.flush()

            # 3. User
            u1 = User(user_id=1, username="admin_boston", email="admin@boston.com", role="company_admin", company_id=1, password_hash="dummy")
            db.add(u1)
            await db.flush()

            # 4. Prompts & Versions
            # Prompt 10: Front (service_id=1)
            p10 = Prompt(prompt_id=10, prompt_name="EE Front V8", prompt_type="audio", service_id=1, company_id=1, is_active=True)
            v10 = PromptVersion(id=10, prompt_id=10, prompt="Prompt text for Front", version_label="v1", is_current=True)

            # Prompt 30: Asesores (service_id=3)
            p30 = Prompt(prompt_id=30, prompt_name="Pruebas ESIC Asesores", prompt_type="audio", service_id=3, company_id=1, is_active=True)
            v30 = PromptVersion(id=30, prompt_id=30, prompt="Prompt text for Asesores", version_label="v1", is_current=True)

            db.add_all([p10, v10, p30, v30])
            await db.commit()

        self.token = create_access_token({"user_id": 1, "email": "admin@boston.com"})
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        if os.path.exists("test_analysis_scoping_test.db"):
            try:
                os.remove("test_analysis_scoping_test.db")
            except Exception:
                pass

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    @patch("app.services.twilio_service.TwilioService")
    @patch("app.services.hubspot_service.HubSpotService")
    async def test_test_analysis_uses_selected_service_asesores(
        self, MockHubspot, MockTwilio, MockCompleteText, MockTranscribe
    ):
        """Test Analysis with service_id=3 (Asesores) uses Prompt 30 (Pruebas ESIC) and saves service_id=3, not Front."""
        MockHubspot.return_value.get_call = AsyncMock(return_value={"recording_url": "http://twilio.test/audio.mp3"})
        MockTwilio.return_value.download_audio = AsyncMock(return_value=b"AUDIOBYTES")
        MockTranscribe.return_value = {"text": "Transcripcion de prueba asesores"}
        MockCompleteText.return_value = '{"tipo_llamada": "otros", "evaluacion_global": 9.0}'

        res = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-503879322856", "service_id": 3},
            headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["service_id"], 3, "Returned service_id must be 3 (Asesores)")
        self.assertEqual(data["service_name"], "Asesores Comerciales")
        self.assertEqual(data["prompt_id"], 30, "Returned prompt_id must be 30 (Pruebas ESIC)")
        self.assertEqual(data["prompt_name"], "Pruebas ESIC Asesores")

        # Verify DB records
        async with AsyncSession(self.engine) as db:
            a_stmt = select(Analysis).where(Analysis.call_id == "call-503879322856")
            a_res = await db.execute(a_stmt)
            analysis = a_res.scalars().first()

            self.assertIsNotNone(analysis)
            self.assertEqual(analysis.service_id, 3, "DB bm_analyses service_id must be 3")
            self.assertEqual(analysis.prompt_id, 30, "DB bm_analyses prompt_id must be 30")

            c_stmt = select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == "call-503879322856")
            c_res = await db.execute(c_stmt)
            curr = c_res.scalars().first()

            self.assertIsNotNone(curr)
            self.assertEqual(curr.service_id, 3, "DB bm_call_analysis_current service_id must be 3")
            self.assertEqual(curr.prompt_id, 30, "DB bm_call_analysis_current prompt_id must be 30")

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    @patch("app.services.twilio_service.TwilioService")
    @patch("app.services.hubspot_service.HubSpotService")
    async def test_test_analysis_uses_selected_service_front(
        self, MockHubspot, MockTwilio, MockCompleteText, MockTranscribe
    ):
        """Test Analysis with service_id=1 (Front) uses Prompt 10 (EE Front V8)."""
        MockHubspot.return_value.get_call = AsyncMock(return_value={"recording_url": "http://twilio.test/audio.mp3"})
        MockTwilio.return_value.download_audio = AsyncMock(return_value=b"AUDIOBYTES")
        MockTranscribe.return_value = {"text": "Transcripcion de prueba front"}
        MockCompleteText.return_value = '{"tipo_llamada": "otros", "evaluacion_global": 7.5}'

        res = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-front-100", "service_id": 1},
            headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["service_id"], 1)
        self.assertEqual(data["prompt_id"], 10)

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    @patch("app.services.twilio_service.TwilioService")
    @patch("app.services.hubspot_service.HubSpotService")
    async def test_test_analysis_explicit_prompt_id_derives_service(
        self, MockHubspot, MockTwilio, MockCompleteText, MockTranscribe
    ):
        """Test Analysis with explicit prompt_id=30 derives service_id=3 automatically."""
        MockHubspot.return_value.get_call = AsyncMock(return_value={"recording_url": "http://twilio.test/audio.mp3"})
        MockTwilio.return_value.download_audio = AsyncMock(return_value=b"AUDIOBYTES")
        MockTranscribe.return_value = {"text": "Transcripcion con prompt explicito"}
        MockCompleteText.return_value = '{"tipo_llamada": "otros", "evaluacion_global": 8.0}'

        res = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-explicit-prompt", "prompt_id": 30},
            headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["service_id"], 3)
        self.assertEqual(data["prompt_id"], 30)

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    @patch("app.services.twilio_service.TwilioService")
    @patch("app.services.hubspot_service.HubSpotService")
    async def test_test_analysis_discordant_prompt_and_service_returns_error(
        self, MockHubspot, MockTwilio, MockCompleteText, MockTranscribe
    ):
        """Prompt 30 belongs to service 3, but service_id=1 requested -> error."""
        res = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-discordant", "prompt_id": 30, "service_id": 1},
            headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertNotEqual(res.status_code, 200)
        self.assertIn("pertenece al servicio", res.text)

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    @patch("app.services.twilio_service.TwilioService")
    @patch("app.services.hubspot_service.HubSpotService")
    async def test_test_analysis_service_without_active_prompt_returns_422(
        self, MockHubspot, MockTwilio, MockCompleteText, MockTranscribe
    ):
        """Service 4 has no active audio prompt -> 422 error, DOES NOT fall back to Front!"""
        res = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-no-prompt", "service_id": 4},
            headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertEqual(res.status_code, 422, res.text)
        data = res.json()
        self.assertFalse(data["ok"])
        self.assertIn("No hay estructura activa", data["error_message"])

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    @patch("app.services.twilio_service.TwilioService")
    @patch("app.services.hubspot_service.HubSpotService")
    async def test_reanalyzing_same_call_with_different_service_updates_current_and_keeps_history(
        self, MockHubspot, MockTwilio, MockCompleteText, MockTranscribe
    ):
        """Analyzing call-multi-1 with service 1, then service 3: creates 2 history rows, updates current to service 3."""
        MockHubspot.return_value.get_call = AsyncMock(return_value={"recording_url": "http://twilio.test/audio.mp3"})
        MockTwilio.return_value.download_audio = AsyncMock(return_value=b"AUDIOBYTES")
        MockTranscribe.return_value = {"text": "Multi-analysis call transcription"}
        MockCompleteText.return_value = '{"tipo_llamada": "otros", "evaluacion_global": 8.0}'

        # 1. Run 1 with service 1 (Front)
        res1 = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-multi-1", "service_id": 1},
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(res1.status_code, 200)

        # 2. Run 2 with service 3 (Asesores)
        res2 = await self.client.post(
            "/bm/test-analysis/by-call-id",
            json={"call_id": "call-multi-1", "service_id": 3},
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(res2.status_code, 200)

        async with AsyncSession(self.engine) as db:
            # Check bm_analyses history has 2 records
            a_stmt = select(Analysis).where(Analysis.call_id == "call-multi-1").order_by(Analysis.analysis_id.asc())
            a_res = await db.execute(a_stmt)
            history = a_res.scalars().all()

            self.assertEqual(len(history), 2, "Must have 2 history records in bm_analyses")
            self.assertEqual(history[0].service_id, 1, "First analysis must be service 1")
            self.assertEqual(history[1].service_id, 3, "Second analysis must be service 3")

            # Check bm_call_analysis_current is updated to service 3 & latest analysis_id
            c_stmt = select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == "call-multi-1")
            c_res = await db.execute(c_stmt)
            curr = c_res.scalars().first()

            self.assertEqual(curr.service_id, 3, "Current record must be updated to service 3")
            self.assertEqual(curr.latest_analysis_id, history[1].analysis_id)

    @patch("app.services.transcription_analysis_service.openai_service.transcribe_audio")
    @patch("app.services.transcription_analysis_service.openai_service.complete_text")
    async def test_audio_upload_uses_selected_service(self, MockCompleteText, MockTranscribe):
        """Audio upload selecting service_id=3 uses Asesores prompt and saves service_id=3."""
        MockTranscribe.return_value = {"text": "Uploaded audio transcription"}
        MockCompleteText.return_value = '{"tipo_llamada": "otros", "evaluacion_global": 9.2}'

        files = {"file": ("test.mp3", b"DUMMY_MP3_CONTENT", "audio/mpeg")}
        data = {"service_id": "3"}

        res = await self.client.post(
            "/bm/test-analysis/by-audio-upload",
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertEqual(res.status_code, 200, res.text)
        resp = res.json()
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["service_id"], 3, "Uploaded audio must save service_id=3")
        self.assertEqual(resp["prompt_id"], 30, "Uploaded audio must use prompt_id=30")


if __name__ == "__main__":
    asyncio.run(unittest.main())
