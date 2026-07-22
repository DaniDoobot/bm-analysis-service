import os
import sys
import unittest
import io
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///audio_upload_test.db"

# Setup path
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

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.typologies import Typology
from app.models.users import User
from app.dependencies import get_current_user
from app.main import app

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class TestAudioUploadAnalysis(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("audio_upload_test.db"):
            try:
                os.remove("audio_upload_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Populate test fixtures
        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Company
            self.c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            db.add(self.c1)
            await db.flush()

            # 2. Service
            self.s1 = Service(service_id=1, service_name="Front Desk", service_key="front", company_id=1)
            db.add(self.s1)
            await db.flush()

            # 3. Typologies
            self.t1 = Typology(typology_id=1, service_id=1, typology_key="cita", typology_name="Cita", is_active=True)
            db.add(self.t1)
            await db.flush()

            # 4. Prompt
            self.p1 = Prompt(prompt_id=1, prompt_name="Default Audio Prompt", prompt_type="audio", service_id=1)
            db.add(self.p1)
            await db.flush()

            # 5. PromptVersion
            self.pv1 = PromptVersion(
                id=1,
                prompt_id=1,
                version_label="v1.0",
                prompt="### FORMATO DE RESPUESTA\nDevuelve exclusivamente un JSON con las claves: tipo_llamada, evaluacion_global, empatia, simpatia, claridad, procedimiento, agente_telefonico, objeciones, propension, sentiment.",
                is_current=True,
                is_archived=False
            )
            db.add(self.pv1)
            await db.flush()

            # 6. Criteria
            self.crit1 = PromptCriterion(
                criterion_id=1,
                prompt_id=1,
                criterion_name="Empatía",
                output_key="empatia",
                feed_key="feedback_empatia",
                criterion_type="score_1_10",
                is_active=True
            )
            db.add(self.crit1)

            # 7. User
            self.u_super = User(user_id=1, username="super_admin", email="super@test.com", role="administrador", password_hash="dummy")
            db.add(self.u_super)

            await db.commit()

            self.super_user = self.u_super

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("audio_upload_test.db"):
            try:
                os.remove("audio_upload_test.db")
            except Exception:
                pass

    @patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock)
    @patch("app.services.openai_service.complete_text", new_callable=AsyncMock)
    async def test_audio_upload_mp3_success(self, mock_complete, mock_transcribe):
        """1. Test successful mp3 file upload with custom_prompt."""
        app.dependency_overrides[get_current_user] = lambda: self.super_user

        mock_transcribe.return_value = {"text": "Boston Medical, buenos días."}
        mock_complete.return_value = """{
            "tipo_llamada": "cita",
            "evaluacion_global": 9.0,
            "empatia": 9.0,
            "simpatia": 8.0,
            "claridad": 10.0,
            "procedimiento": 9.0,
            "agente_telefonico": "Pedro",
            "objeciones": "Ninguna",
            "propension": "Alta",
            "sentiment": "positivo"
        }"""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            fake_audio = io.BytesIO(b"dummy mp3 data")
            files = {"file": ("test.mp3", fake_audio, "audio/mpeg")}
            data = {"custom_prompt": "Utiliza este prompt personalizado para testear"}

            res = await ac.post("/bm/test-analysis/by-audio-upload", files=files, data=data)
            self.assertEqual(res.status_code, 200)

            res_json = res.json()
            self.assertTrue(res_json["ok"])
            self.assertEqual(res_json["status"], "completed")
            self.assertEqual(res_json["transcription"], "Boston Medical, buenos días.")
            self.assertEqual(res_json["summary"]["tipo_llamada"], "cita")
            self.assertEqual(res_json["result"]["empatia"], 9.0)

            # Check that transcribe_audio was called
            mock_transcribe.assert_called_once()
            # Check that complete_text was called with the custom prompt
            args, kwargs = mock_complete.call_args
            system_msg = kwargs["messages"][0]["content"]
            self.assertIn("Utiliza este prompt personalizado para testear", system_msg)

    @patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock)
    @patch("app.services.openai_service.complete_text", new_callable=AsyncMock)
    async def test_audio_upload_wav_success(self, mock_complete, mock_transcribe):
        """2. Test successful wav file upload with 'prompt' alias compatibility."""
        app.dependency_overrides[get_current_user] = lambda: self.super_user

        mock_transcribe.return_value = {"text": "GesDent, buenas tardes."}
        mock_complete.return_value = """{
            "tipo_llamada": "cita",
            "evaluacion_global": 8.0,
            "empatia": 8.0,
            "simpatia": 8.0,
            "claridad": 8.0,
            "procedimiento": 8.0,
            "agente_telefonico": "Marta",
            "objeciones": "Ninguna",
            "propension": "Media",
            "sentiment": "neutral"
        }"""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            fake_audio = io.BytesIO(b"dummy wav data")
            files = {"file": ("test.wav", fake_audio, "audio/wav")}
            data = {"prompt": "Prompt alias de compatibilidad"}

            res = await ac.post("/bm/test-analysis/by-audio-upload", files=files, data=data)
            self.assertEqual(res.status_code, 200)

            res_json = res.json()
            self.assertTrue(res_json["ok"])
            self.assertEqual(res_json["transcription"], "GesDent, buenas tardes.")

            args, kwargs = mock_complete.call_args
            system_msg = kwargs["messages"][0]["content"]
            self.assertIn("Prompt alias de compatibilidad", system_msg)

    async def test_audio_upload_invalid_extension(self):
        """3. Test rejection of file with invalid extension and content type."""
        app.dependency_overrides[get_current_user] = lambda: self.super_user

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            fake_text = io.BytesIO(b"not an audio file")
            files = {"file": ("test.txt", fake_text, "text/plain")}

            res = await ac.post("/bm/test-analysis/by-audio-upload", files=files)
            self.assertEqual(res.status_code, 400)
            self.assertIn("Solo se admiten archivos .mp3 y .wav", res.json()["detail"])

    @patch("app.services.hubspot_service.HubSpotService", new_callable=MagicMock)
    @patch("app.services.twilio_service.TwilioService", new_callable=MagicMock)
    @patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock)
    @patch("app.services.openai_service.complete_text", new_callable=AsyncMock)
    async def test_by_call_id_regression(self, mock_complete, mock_transcribe, mock_twilio_cls, mock_hubspot_cls):
        """4. Test that by-call-id endpoint continues to work without regression."""
        app.dependency_overrides[get_current_user] = lambda: self.super_user

        # Setup mocks for HubSpot and Twilio downloading
        mock_hs = MagicMock()
        mock_hs.get_call = AsyncMock(return_value={"recording_url": "https://api.twilio.com/fake_recording.mp3"})
        mock_hubspot_cls.return_value = mock_hs

        mock_tw = MagicMock()
        mock_tw.download_audio = AsyncMock(return_value=b"twilio audio bytes")
        mock_twilio_cls.return_value = mock_tw

        mock_transcribe.return_value = {"text": "Transcripción por call_id."}
        mock_complete.return_value = """{
            "tipo_llamada": "cita",
            "evaluacion_global": 7.0,
            "empatia": 7.0,
            "simpatia": 7.0,
            "claridad": 7.0,
            "procedimiento": 7.0,
            "agente_telefonico": "Leticia",
            "objeciones": "Ninguna",
            "propension": "Baja",
            "sentiment": "positivo"
        }"""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            req_body = {
                "call_id": "hs_call_9999",
                "custom_prompt": "Un prompt directo"
            }
            res = await ac.post("/bm/test-analysis/by-call-id", json=req_body)
            self.assertEqual(res.status_code, 200)

            res_json = res.json()
            self.assertTrue(res_json["ok"])
            self.assertEqual(res_json["call_id"], "hs_call_9999")
            self.assertEqual(res_json["summary"]["tipo_llamada"], "cita")

            mock_hs.get_call.assert_called_once_with("hs_call_9999")
            mock_tw.download_audio.assert_called_once()
            mock_transcribe.assert_called_once()

    @patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock)
    @patch("app.services.openai_service.complete_text", new_callable=AsyncMock)
    async def test_by_call_id_with_specific_service_id(self, mock_complete, mock_transcribe):
        """Test by-call-id resolves prompt scoped to service_id."""
        app.dependency_overrides[get_current_user] = lambda: self.super_user

        # Create a second service (Asesores, service_id=2) and its active audio prompt
        async with AsyncSession(self.session_factory) as db:
            s2 = Service(service_id=2, service_name="Asesores", service_key="asesores", company_id=1)
            db.add(s2)
            await db.flush()

            p2 = Prompt(prompt_id=2, prompt_name="Asesores Audio Prompt", prompt_type="audio", service_id=2, is_active=True)
            db.add(p2)
            await db.flush()

            pv2 = PromptVersion(
                id=2,
                prompt_id=2,
                version_label="v1.0",
                prompt="### FORMATO DE RESPUESTA\nDevuelve JSON de Asesores.",
                is_current=True,
                is_archived=False
            )
            db.add(pv2)
            await db.commit()

        mock_transcribe.return_value = {"text": "Audio transcrito Asesores."}
        mock_complete.return_value = """{
            "tipo_llamada": "cita",
            "evaluacion_global": 8.0,
            "empatia": 8.0,
            "simpatia": 8.0,
            "claridad": 8.0,
            "procedimiento": 8.0,
            "agente_telefonico": "Juan",
            "objeciones": "Ninguna",
            "propension": "Alta",
            "sentiment": "positivo"
        }"""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 1. Test specifying service_id=2 resolves prompt for service 2
            req_body = {
                "call_id": "call_service_2",
                "service_id": 2
            }
            with patch("app.services.hubspot_service.HubSpotService") as mock_hs_cls, \
                 patch("app.services.twilio_service.TwilioService") as mock_tw_cls:
                mock_hs = MagicMock()
                mock_hs.get_call = AsyncMock(return_value={"recording_url": "http://audio.url"})
                mock_hs_cls.return_value = mock_hs

                mock_tw = MagicMock()
                mock_tw.download_audio = AsyncMock(return_value=b"audio bytes")
                mock_tw_cls.return_value = mock_tw

                res = await ac.post("/bm/test-analysis/by-call-id", json=req_body)
                self.assertEqual(res.status_code, 200)
                res_json = res.json()
                self.assertTrue(res_json["ok"])

                # Verify saved analysis record in DB linked to prompt_id 2
                async with AsyncSession(self.session_factory) as db:
                    from app.models.analyses import Analysis
                    stmt = select(Analysis).where(Analysis.call_id == "call_service_2")
                    analysis_rec = (await db.execute(stmt)).scalars().first()
                    self.assertIsNotNone(analysis_rec)
                    self.assertEqual(analysis_rec.prompt_id, 2)

            # 2. Test requesting non-existent prompt service_id=99 returns clear error
            req_body_err = {
                "call_id": "call_service_99",
                "service_id": 99
            }
            with patch("app.services.hubspot_service.HubSpotService") as mock_hs_cls, \
                 patch("app.services.twilio_service.TwilioService") as mock_tw_cls:
                mock_hs = MagicMock()
                mock_hs.get_call = AsyncMock(return_value={"recording_url": "http://audio.url"})
                mock_hs_cls.return_value = mock_hs

                mock_tw = MagicMock()
                mock_tw.download_audio = AsyncMock(return_value=b"audio bytes")
                mock_tw_cls.return_value = mock_tw

                res_err = await ac.post("/bm/test-analysis/by-call-id", json=req_body_err)
                self.assertEqual(res_err.status_code, 422)
                err_json = res_err.json()
                self.assertFalse(err_json["ok"])
                self.assertIn("No hay estructura activa para llamadas en el servicio seleccionado", err_json["error_message"])


if __name__ == "__main__":
    unittest.main()
