"""
Unit and integration tests (100% mocked, zero real external AI/CRM calls) for the
Laboratory Multi-Provider AI Model Selector (`test_analysis`).

Covers all 18 mandatory scenarios:
1. No model_key -> exact current behavior.
2. gemini_3_8_flash -> Gemini 3.8 Flash direct audio.
3. gemini_3_5_flash_lite -> Gemini 3.5 Flash-Lite direct audio.
4. gemini_2_5_flash_lite -> Gemini 2.5 Flash-Lite direct audio.
5. Unknown model_key -> HTTP 400.
6. Unconfigured model (missing API key) -> controlled HTTP 400 error.
7. GET /bm/test-analysis/models & POST /bm/test-analysis/models/{model_key}/check -> configured/reachable, no silent fallback.
8. direct_audio (Gemini & Voxtral Small) maintains identical result schema + enforces context limit guard.
9. groq_whisper_gpt_luna calls Groq STT then OpenAI Luna (text only, no audio to Luna).
10. mistral_transcribe_gpt_luna calls Mistral STT then OpenAI Luna.
11. deepgram_nova3_gpt_luna calls Deepgram STT then OpenAI Luna.
12. STT failure aborts immediately and NEVER calls LLM.
13. LLM failure preserves STT + LLM error traceability in FinOps.
14. FinOps records module='test_analysis', provider, model, model_key.
15. Dual pipeline records both STT (audio minute pricing) and LLM (token pricing) events and sums total cost.
16. Same call_id + prompt_id + different models creates two distinct bm_analyses rows visible in history.
17. Multi-tenant isolation remains intact on historical/current listings.
18. Mass Evaluations never uses LAB_* catalog or variables.
"""
from datetime import datetime, timezone
from decimal import Decimal
import inspect
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from httpx import AsyncClient, ASGITransport
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.ext.compiler import compiles

from app.db import Base


@compiles(BigInteger, "sqlite")
def _compile_big_int_sqlite(type_, compiler, **kw):
    return "INTEGER"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"
from app.main import app
from app.dependencies import get_db
from app.core.tenant_context import TenantContext
from app.config import get_settings
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.typologies import Typology
from app.models.ai_usage import AIUsageEvent, AIModelPricing
from app.services.laboratory_ai_service import (
    CONTEXT_LIMIT_ERROR_MESSAGE,
)


class TestLaboratoryModelSelector(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.db_session = self.session_factory()

        # Seed Company 1 & Company 2
        c1 = Company(company_id=1, company_key="boston", company_name="Boston Medical", is_active=True, is_demo=False)
        c2 = Company(company_id=2, company_key="other", company_name="Other Corp", is_active=True, is_demo=False)
        self.db_session.add_all([c1, c2])

        s1 = Service(service_id=1, company_id=1, service_key="front", service_name="Front", is_active=True)
        s2 = Service(service_id=2, company_id=2, service_key="other_svc", service_name="Other Svc", is_active=True)
        self.db_session.add_all([s1, s2])

        t1 = Typology(typology_id=1, service_id=1, typology_key="cita", typology_name="Cita", is_active=True)
        self.db_session.add(t1)

        p1 = Prompt(
            prompt_id=76,
            company_id=1,
            service_id=1,
            prompt_name="Estructura Front Test",
            prompt_type="audio",
            is_active=True,
            is_archived=False,
        )
        self.db_session.add(p1)
        await self.db_session.flush()

        pv1 = PromptVersion(
            id=760,
            prompt_id=76,
            version_label="v1",
            prompt='Evalúa empatia y tipo_llamada. ### FORMATO DE SALIDA JSON: {"tipo_llamada": "cita", "empatia": 9}',
            is_current=True,
        )
        self.db_session.add(pv1)

        crit1 = PromptCriterion(
            criterion_id=501,
            prompt_id=76,
            criterion_key="empatia",
            criterion_name="Empatía",
            criterion_type="score_1_10",
            output_key="empatia",
            is_active=True,
        )
        self.db_session.add(crit1)

        dt_from = datetime(2025, 1, 1, tzinfo=timezone.utc)
        pricings = [
            AIModelPricing(
                provider="gemini",
                model="gemini-3.8-flash",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.7500"),
                input_audio_price_per_million=Decimal("0.7500"),
                output_text_price_per_million=Decimal("3.7500"),
                output_audio_price_per_million=Decimal("3.7500"),
                pricing_version="gemini-3.8-v1",
                is_active=True,
            ),
            AIModelPricing(
                provider="gemini",
                model="gemini-2.5-flash-lite",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.1000"),
                input_audio_price_per_million=Decimal("0.3000"),
                output_text_price_per_million=Decimal("0.4000"),
                output_audio_price_per_million=Decimal("0.4000"),
                cached_input_price_per_million=Decimal("0.0250"),
                pricing_version="gemini-2.5-lite-v1",
                is_active=True,
            ),
            AIModelPricing(
                provider="gemini",
                model="gemini-3.5-flash-lite",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.3000"),
                input_audio_price_per_million=Decimal("0.3000"),
                output_text_price_per_million=Decimal("2.5000"),
                output_audio_price_per_million=Decimal("2.5000"),
                cached_input_price_per_million=Decimal("0.0300"),
                pricing_version="gemini-3.5-flash-lite-v1",
                is_active=True,
            ),
            AIModelPricing(
                provider="mistral",
                model="voxtral-small-latest",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.1000"),
                output_text_price_per_million=Decimal("0.4000"),
                audio_price_per_minute=Decimal("0.004000"),
                pricing_version="mistral-voxtral-small-v1",
                is_active=True,
            ),
            AIModelPricing(
                provider="groq",
                model="whisper-large-v3-turbo",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.0000"),
                input_audio_price_per_million=Decimal("0.0000"),
                output_text_price_per_million=Decimal("0.0000"),
                output_audio_price_per_million=Decimal("0.0000"),
                audio_price_per_minute=Decimal("0.000667"),
                pricing_version="groq-whisper-v1",
                is_active=True,
            ),
            AIModelPricing(
                provider="mistral",
                model="voxtral-mini-latest",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.0000"),
                input_audio_price_per_million=Decimal("0.0000"),
                output_text_price_per_million=Decimal("0.0000"),
                output_audio_price_per_million=Decimal("0.0000"),
                audio_price_per_minute=Decimal("0.003000"),
                pricing_version="mistral-voxtral-mini-stt-v2",
                is_active=True,
            ),
            AIModelPricing(
                provider="deepgram",
                model="nova-3",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.0000"),
                input_audio_price_per_million=Decimal("0.0000"),
                output_text_price_per_million=Decimal("0.0000"),
                output_audio_price_per_million=Decimal("0.0000"),
                audio_price_per_minute=Decimal("0.004300"),
                pricing_version="deepgram-nova3-v1",
                is_active=True,
            ),
            AIModelPricing(
                provider="openai",
                model="gpt-5.6-luna",
                effective_from=dt_from,
                input_text_price_per_million=Decimal("0.2000"),
                cached_input_price_per_million=Decimal("0.0200"),
                output_text_price_per_million=Decimal("1.2000"),
                pricing_version="openai-gpt-5.6-luna-v1",
                is_active=True,
            ),
        ]
        self.db_session.add_all(pricings)
        await self.db_session.commit()

        # Configure LAB_* settings in memory
        s = get_settings()
        self._orig_settings = {
            "lab_gemini_api_key": s.lab_gemini_api_key,
            "lab_gemini_3_8_model": s.lab_gemini_3_8_model,
            "lab_gemini_3_5_flash_lite_model": s.lab_gemini_3_5_flash_lite_model,
            "lab_gemini_2_5_flash_lite_model": s.lab_gemini_2_5_flash_lite_model,
            "lab_mistral_api_key": s.lab_mistral_api_key,
            "lab_mistral_voxtral_small_model": s.lab_mistral_voxtral_small_model,
            "lab_mistral_transcribe_model": s.lab_mistral_transcribe_model,
            "lab_groq_api_key": s.lab_groq_api_key,
            "lab_groq_whisper_model": s.lab_groq_whisper_model,
            "lab_deepgram_api_key": s.lab_deepgram_api_key,
            "lab_deepgram_model": s.lab_deepgram_model,
            "lab_openai_api_key": s.lab_openai_api_key,
            "lab_openai_luna_model": s.lab_openai_luna_model,
            "gemini_api_key": s.gemini_api_key,
        }
        s.lab_gemini_api_key = "test-lab-gemini-key"
        s.lab_gemini_3_8_model = "gemini-3.8-flash"
        s.lab_gemini_3_5_flash_lite_model = "gemini-3.5-flash-lite"
        s.lab_gemini_2_5_flash_lite_model = "gemini-2.5-flash-lite"
        s.lab_mistral_api_key = "test-lab-mistral-key"
        s.lab_mistral_voxtral_small_model = "voxtral-small-latest"
        s.lab_mistral_transcribe_model = "voxtral-mini-latest"
        s.lab_groq_api_key = "test-lab-groq-key"
        s.lab_groq_whisper_model = "whisper-large-v3-turbo"
        s.lab_deepgram_api_key = "test-lab-deepgram-key"
        s.lab_deepgram_model = "nova-3"
        s.lab_openai_api_key = "test-lab-openai-key"
        s.lab_openai_luna_model = "gpt-5.6-luna"

    async def asyncTearDown(self):
        s = get_settings()
        for k, v in self._orig_settings.items():
            setattr(s, k, v)
        app.dependency_overrides.clear()
        await self.db_session.close()
        await self.engine.dispose()

    # ── 1. No model_key -> Exact Current Behavior ────────────────────────────

    async def test_01_no_model_key_uses_default_pipeline(self):
        with (
            patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_hs,
            patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl,
            patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr,
            patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ct,
            patch("app.services.laboratory_ai_service.LaboratoryAIProviderService.execute_laboratory_analysis", new_callable=AsyncMock) as mock_lab,
        ):
            mock_hs.return_value = {"call_id": "1001", "recording_url": "https://api.twilio.com/rec1.mp3"}
            mock_dl.return_value = b"FAKE_MP3_BYTES"
            mock_tr.return_value = {"text": "Hola buenos dias quiero una cita"}
            mock_ct.return_value = '{"tipo_llamada": "cita", "empatia": 9}'

            from app.services.transcription_analysis_service import analyze_transcription_pipeline

            res = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id="1001",
                prompt_id=76,
                service_id=1,
                model_key=None,
            )

            self.assertTrue(res["ok"])
            self.assertIsNone(res["requested_model_key"])
            mock_tr.assert_awaited_once()
            mock_ct.assert_awaited_once()
            mock_lab.assert_not_awaited()

    # ── 2, 3, 4. Gemini Direct Audio Models ──────────────────────────────────

    async def _assert_gemini_direct_model(self, model_key: str, expected_model_name: str):
        fake_response = SimpleNamespace(
            text='{"tipo_llamada": "cita", "empatia": 8, "transcription": "Paciente solicita cita"}',
            response_id="gem-resp-1",
            usage_metadata={
                "prompt_token_count": 1200,
                "candidates_token_count": 300,
                "total_token_count": 1500,
                "prompt_tokens_details": [
                    {"modality": "TEXT", "token_count": 200},
                    {"modality": "AUDIO", "token_count": 1000},
                ],
                "candidates_tokens_details": [
                    {"modality": "TEXT", "token_count": 300},
                ],
            },
        )
        mock_gen = AsyncMock(return_value=fake_response)
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = mock_gen

        with (
            patch("app.services.ai_usage_service.AsyncSessionLocal", self.session_factory),
            patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_hs,
            patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl,
            patch("google.genai.Client", return_value=mock_client),
        ):
            mock_hs.return_value = {"call_id": f"200-{model_key}", "recording_url": "https://api.twilio.com/rec2.mp3", "duration_seconds": 60}
            mock_dl.return_value = b"FAKE_MP3_AUDIO"

            from app.services.transcription_analysis_service import analyze_transcription_pipeline

            res = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id=f"200-{model_key}",
                prompt_id=76,
                service_id=1,
                model_key=model_key,
            )

            self.assertTrue(res["ok"])
            self.assertEqual(res["requested_model_key"], model_key)
            self.assertEqual(res["actual_model"], expected_model_name)
            self.assertEqual(res["provider"], "gemini")
            self.assertEqual(res["pipeline_mode"], "direct_audio")
            self.assertEqual(res["result"]["empatia"], 8)
            self.assertEqual(res["pricing_status"], "priced")
            self.assertIsNotNone(res["estimated_cost_usd"])
            self.assertGreater(res["estimated_cost_usd"], 0)
            self.assertEqual(mock_gen.call_args.kwargs["model"], expected_model_name)

    async def test_02_gemini_3_8_flash_direct_audio(self):
        await self._assert_gemini_direct_model("gemini_3_8_flash", "gemini-3.8-flash")

    async def test_03_gemini_3_5_flash_lite_direct_audio(self):
        await self._assert_gemini_direct_model("gemini_3_5_flash_lite", "gemini-3.5-flash-lite")

    async def test_04_gemini_2_5_flash_lite_direct_audio(self):
        await self._assert_gemini_direct_model("gemini_2_5_flash_lite", "gemini-2.5-flash-lite")

    # ── 5. Unknown model_key -> HTTP 400 ─────────────────────────────────────

    async def test_05_unknown_model_key_returns_400(self):
        async def override_get_db():
            yield self.db_session

        app.dependency_overrides[get_db] = override_get_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/bm/test-analysis/by-call-id",
                json={
                    "call_id": "3001",
                    "prompt_id": 76,
                    "service_id": 1,
                    "model_key": "non_existent_model_xyz",
                },
            )
            self.assertEqual(resp.status_code, 400)
            data = resp.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["stage"], "model_validation")
            self.assertIn("non_existent_model_xyz", data["error_message"])

    # ── 6. Model without API key -> Controlled Error ─────────────────────────

    async def test_06_model_without_api_key_returns_controlled_error(self):
        s = get_settings()
        s.lab_mistral_api_key = ""

        async def override_get_db():
            yield self.db_session

        app.dependency_overrides[get_db] = override_get_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/bm/test-analysis/by-call-id",
                json={
                    "call_id": "3002",
                    "prompt_id": 76,
                    "service_id": 1,
                    "model_key": "voxtral_small",
                },
            )
            self.assertEqual(resp.status_code, 400)
            data = resp.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["stage"], "model_config")
            self.assertIn("LAB_MISTRAL_API_KEY", data["error_message"])

    # ── 7. Models List & Check Endpoint ──────────────────────────────────────

    async def test_07_models_list_and_check_endpoint(self):
        async def override_get_db():
            yield self.db_session

        app.dependency_overrides[get_db] = override_get_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r_list = await ac.get("/bm/test-analysis/models")
            self.assertEqual(r_list.status_code, 200)
            models = r_list.json()
            keys = [m["model_key"] for m in models]
            self.assertEqual(
                keys,
                [
                    "gemini_3_8_flash",
                    "gemini_3_5_flash_lite",
                    "gemini_2_5_flash_lite",
                    "voxtral_small",
                    "groq_whisper_gpt_luna",
                    "mistral_transcribe_gpt_luna",
                    "deepgram_nova3_gpt_luna",
                ],
            )
            for m in models:
                self.assertTrue(m["configured"])
                self.assertNotIn("api_key", m)

            # Check reachable Gemini 2.5
            mock_client_ok = MagicMock()
            mock_client_ok.aio.models.get = AsyncMock(return_value=SimpleNamespace(name="models/gemini-2.5-flash-lite"))
            with patch("google.genai.Client", return_value=mock_client_ok):
                r_chk_ok = await ac.post("/bm/test-analysis/models/gemini_2_5_flash_lite/check")
                self.assertEqual(r_chk_ok.status_code, 200)
                chk_data = r_chk_ok.json()
                self.assertTrue(chk_data["configured"])
                self.assertTrue(chk_data["reachable"])

            # Check unreachable Gemini 2.5 when Google returns 403 Permission Denied (NO fallback!)
            mock_client_denied = MagicMock()
            mock_client_denied.aio.models.get = AsyncMock(
                side_effect=Exception("403 PERMISSION_DENIED: Project does not have access")
            )
            with patch("google.genai.Client", return_value=mock_client_denied):
                r_chk_fail = await ac.post("/bm/test-analysis/models/gemini_2_5_flash_lite/check")
                self.assertEqual(r_chk_fail.status_code, 200)
                chk_fail = r_chk_fail.json()
                self.assertTrue(chk_fail["configured"])
                self.assertFalse(chk_fail["reachable"])
                self.assertIn("acceso", chk_fail["detail"].lower())

    # ── 8. Voxtral Small Direct Audio & Context Limit Guard ──────────────────

    async def test_08_voxtral_small_direct_audio_and_context_limit(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "mistral-cmpl-1",
            "choices": [
                {"message": {"content": '{"tipo_llamada": "cita", "empatia": 10, "transcription": "Audio Voxtral"}'}}
            ],
            "usage": {"prompt_tokens": 800, "completion_tokens": 150, "total_tokens": 950},
        }

        with (
            patch("app.services.ai_usage_service.AsyncSessionLocal", self.session_factory),
            patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_hs,
            patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl,
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post,
        ):
            mock_hs.return_value = {"call_id": "4001", "recording_url": "https://api.twilio.com/rec4.mp3", "duration_seconds": 60}
            mock_dl.return_value = b"FAKE_VOXTRAL_AUDIO"

            from app.services.transcription_analysis_service import analyze_transcription_pipeline

            res = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id="4001",
                prompt_id=76,
                service_id=1,
                model_key="voxtral_small",
            )

            self.assertTrue(res["ok"])
            self.assertEqual(res["requested_model_key"], "voxtral_small")
            self.assertEqual(res["provider"], "mistral")
            self.assertEqual(res["actual_model"], "voxtral-small-latest")
            self.assertEqual(res["result"]["empatia"], 10)
            self.assertEqual(res["pricing_status"], "priced")
            # Audio: (60/60)*0.004 = 0.004000
            # Input: 800 @ 0.10/M = 0.000080
            # Output: 150 @ 0.40/M = 0.000060
            # Total = 0.004140
            self.assertEqual(res["estimated_cost_usd"], 0.00414)
            self.assertEqual(mock_post.await_count, 1)

            # Verify context limit guard when prompt is huge (> 32,000 tokens -> > 96,000 chars)
            huge_prompt = "A" * 105000
            res_huge = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id="4002",
                custom_prompt_text=huge_prompt,
                service_id=1,
                model_key="voxtral_small",
                audio_bytes=b"FAKE_AUDIO",
            )
            self.assertFalse(res_huge["ok"])
            self.assertEqual(res_huge["stage"], "context_limit")
            self.assertEqual(res_huge["error_message"], CONTEXT_LIMIT_ERROR_MESSAGE)

    # ── 9, 10, 11, 14, 15. STT + LLM Pipelines & FinOps Dual Cost Attribution ──

    async def _assert_stt_plus_llm_pipeline(
        self,
        model_key: str,
        expected_stt_provider: str,
        expected_stt_model: str,
        stt_mock_payload: dict,
    ):
        calls_log = []

        async def fake_httpx_post(self_client, url, **kwargs):
            calls_log.append((url, kwargs))
            resp = MagicMock()
            resp.status_code = 200
            if "openai.com/v1/chat/completions" in url:
                body = kwargs.get("json", {})
                self.assertNotIn("input_audio", str(body))
                resp.json.return_value = {
                    "id": "chatcmpl-luna-1",
                    "choices": [{"message": {"content": '{"tipo_llamada": "cita", "empatia": 9}'}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
                }
            else:
                resp.json.return_value = stt_mock_payload
            return resp

        with (
            patch("app.services.ai_usage_service.AsyncSessionLocal", self.session_factory),
            patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_hs,
            patch("app.services.audio_download_service.AudioDownloadService.download_audio", new_callable=AsyncMock) as mock_dl,
            patch("httpx.AsyncClient.post", new=fake_httpx_post),
        ):
            call_id = f"500-{model_key}"
            mock_hs.return_value = {"call_id": call_id, "recording_url": "https://api.twilio.com/rec5.mp3"}
            mock_dl.return_value = b"FAKE_CALL_MP3"

            from app.services.transcription_analysis_service import analyze_transcription_pipeline

            res = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id=call_id,
                prompt_id=76,
                service_id=1,
                model_key=model_key,
            )

            self.assertTrue(res["ok"])
            self.assertEqual(res["requested_model_key"], model_key)
            self.assertEqual(res["pipeline_mode"], "stt_plus_llm")
            self.assertEqual(res["stt_provider"], expected_stt_provider)
            self.assertEqual(res["stt_model"], expected_stt_model)
            self.assertEqual(res["analysis_provider"], "openai")
            self.assertEqual(res["analysis_model"], "gpt-5.6-luna")
            self.assertEqual(len(calls_log), 2)
            self.assertIn("openai.com/v1/chat/completions", calls_log[1][0])

            # Verify FinOps recorded 2 events (transcription + analysis) with priced cost
            ev_res = await self.db_session.execute(
                select(AIUsageEvent)
                .where(AIUsageEvent.call_id == call_id)
                .order_by(AIUsageEvent.id.asc())
            )
            events = list(ev_res.scalars().all())
            self.assertEqual(len(events), 2)
            stt_ev, llm_ev = events[0], events[1]

            self.assertEqual(stt_ev.module, "test_analysis")
            self.assertEqual(stt_ev.operation, "transcription")
            self.assertEqual(stt_ev.provider, expected_stt_provider)
            self.assertEqual(stt_ev.model, expected_stt_model)
            self.assertEqual(stt_ev.model_key, model_key)
            self.assertEqual(stt_ev.pricing_status, "priced")
            self.assertGreater(stt_ev.estimated_cost_usd, Decimal("0"))

            self.assertEqual(llm_ev.module, "test_analysis")
            self.assertEqual(llm_ev.operation, "analysis")
            self.assertEqual(llm_ev.provider, "openai")
            self.assertEqual(llm_ev.model, "gpt-5.6-luna")
            self.assertEqual(llm_ev.model_key, model_key)
            self.assertEqual(llm_ev.pricing_status, "priced")
            self.assertGreater(llm_ev.estimated_cost_usd, Decimal("0"))

            self.assertEqual(res["pricing_status"], "priced")
            self.assertAlmostEqual(res["stt_cost_usd"], float(stt_ev.estimated_cost_usd), places=5)
            self.assertAlmostEqual(res["llm_cost_usd"], float(llm_ev.estimated_cost_usd), places=5)
            expected_total = round(float(stt_ev.estimated_cost_usd) + float(llm_ev.estimated_cost_usd), 6)
            self.assertAlmostEqual(res["estimated_cost_usd"], expected_total, places=5)

    async def test_09_groq_whisper_gpt_luna_pipeline(self):
        await self._assert_stt_plus_llm_pipeline(
            "groq_whisper_gpt_luna",
            "groq",
            "whisper-large-v3-turbo",
            {"text": "Transcripción desde Groq Whisper", "duration": 120.0},
        )

    async def test_10_mistral_transcribe_gpt_luna_pipeline(self):
        # 90s audio @ $0.003/min = $0.004500 STT
        # 1000 in @ $0.20/M + 200 out @ $1.20/M = $0.000440 LLM
        # Total = $0.004940
        await self._assert_stt_plus_llm_pipeline(
            "mistral_transcribe_gpt_luna",
            "mistral",
            "voxtral-mini-latest",
            {"text": "Transcripción desde Mistral Transcribe", "usage": {"prompt_audio_seconds": 90.0}},
        )

    async def test_11_deepgram_nova3_gpt_luna_pipeline(self):
        # 60s audio @ $0.0043/min = $0.004300 STT
        # 1000 in @ $0.20/M + 200 out @ $1.20/M = $0.000440 LLM
        # Total = $0.004740
        await self._assert_stt_plus_llm_pipeline(
            "deepgram_nova3_gpt_luna",
            "deepgram",
            "nova-3",
            {
                "metadata": {"duration": 60.0, "request_id": "dg-req-1"},
                "results": {"channels": [{"alternatives": [{"transcript": "Transcripción desde Deepgram Nova-3"}]}]},
            },
        )

    # ── 12. STT Failure Does NOT Call LLM ────────────────────────────────────

    async def test_12_stt_failure_does_not_call_llm(self):
        called_urls = []

        async def fake_post_stt_fail(self_client, url, **kwargs):
            called_urls.append(url)
            resp = MagicMock()
            resp.status_code = 500
            resp.text = "Groq STT internal error"
            return resp

        with (
            patch("app.services.ai_usage_service.AsyncSessionLocal", self.session_factory),
            patch("httpx.AsyncClient.post", new=fake_post_stt_fail),
        ):
            from app.services.transcription_analysis_service import analyze_transcription_pipeline

            res = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id="6001",
                prompt_id=76,
                service_id=1,
                model_key="groq_whisper_gpt_luna",
                audio_bytes=b"FAKE_AUDIO",
            )

            self.assertFalse(res["ok"])
            self.assertEqual(res["stage"], "transcription")
            self.assertEqual(len(called_urls), 1)
            self.assertIn("groq.com", called_urls[0])

    # ── 13. LLM Failure Preserves FinOps Traceability ────────────────────────

    async def test_13_llm_failure_preserves_finops_traceability(self):
        async def fake_post_llm_fail(self_client, url, **kwargs):
            resp = MagicMock()
            if "groq.com" in url:
                resp.status_code = 200
                resp.json.return_value = {"text": "Transcripción válida antes de fallo LLM", "duration": 60.0}
            else:
                resp.status_code = 502
                resp.text = "OpenAI upstream failure"
            return resp

        with (
            patch("app.services.ai_usage_service.AsyncSessionLocal", self.session_factory),
            patch("httpx.AsyncClient.post", new=fake_post_llm_fail),
        ):
            from app.services.transcription_analysis_service import analyze_transcription_pipeline

            res = await analyze_transcription_pipeline(
                db=self.db_session,
                call_id="6002",
                prompt_id=76,
                service_id=1,
                model_key="groq_whisper_gpt_luna",
                audio_bytes=b"FAKE_AUDIO",
            )

            self.assertFalse(res["ok"])
            self.assertEqual(res["stage"], "llm")

            ev_res = await self.db_session.execute(
                select(AIUsageEvent)
                .where(AIUsageEvent.call_id == "6002")
                .order_by(AIUsageEvent.id.asc())
            )
            events = list(ev_res.scalars().all())
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].operation, "transcription")
            self.assertEqual(events[0].status, "success")
            self.assertEqual(events[1].operation, "analysis")
            self.assertEqual(events[1].status, "error")

    # ── 16 & 17. A/B Comparability & Multi-Tenant Scope in History Listing ───

    async def test_16_17_ab_comparability_and_multitenant_scope(self):
        def make_gemini_mock(empatia_val: int):
            fake_resp = SimpleNamespace(
                text=f'{{"tipo_llamada": "cita", "empatia": {empatia_val}, "transcription": "Test AB"}}',
                response_id=f"resp-{empatia_val}",
                usage_metadata={"prompt_token_count": 500, "candidates_token_count": 100, "total_token_count": 600},
            )
            m_client = MagicMock()
            m_client.aio.models.generate_content = AsyncMock(return_value=fake_resp)
            return m_client

        from app.services.transcription_analysis_service import analyze_transcription_pipeline

        with patch("app.services.ai_usage_service.AsyncSessionLocal", self.session_factory):
            with patch("google.genai.Client", return_value=make_gemini_mock(9)):
                res_a = await analyze_transcription_pipeline(
                    db=self.db_session,
                    call_id="CALL_AB_123",
                    prompt_id=76,
                    service_id=1,
                    model_key="gemini_3_8_flash",
                    audio_bytes=b"AUDIO_AB",
                )
            with patch("google.genai.Client", return_value=make_gemini_mock(7)):
                res_b = await analyze_transcription_pipeline(
                    db=self.db_session,
                    call_id="CALL_AB_123",
                    prompt_id=76,
                    service_id=1,
                    model_key="gemini_2_5_flash_lite",
                    audio_bytes=b"AUDIO_AB",
                )

        self.assertTrue(res_a["ok"])
        self.assertTrue(res_b["ok"])
        self.assertNotEqual(res_a["analysis_id"], res_b["analysis_id"])

        from app.services.analyses_service import list_analyses_history
        from app.core.roles import InternalRole

        ctx_company_1 = TenantContext(
            user_id=1,
            user_email="admin@boston.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            company_id=1,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=None,
        )
        history_c1 = await list_analyses_history(
            db=self.db_session,
            call_id="CALL_AB_123",
            context=ctx_company_1,
        )
        self.assertEqual(len(history_c1), 2)
        by_key = {item.requested_model_key: item for item in history_c1}
        self.assertIn("gemini_3_8_flash", by_key)
        self.assertIn("gemini_2_5_flash_lite", by_key)
        self.assertEqual(by_key["gemini_3_8_flash"].actual_model, "gemini-3.8-flash")
        self.assertEqual(by_key["gemini_3_8_flash"].provider, "gemini")
        self.assertEqual(by_key["gemini_2_5_flash_lite"].actual_model, "gemini-2.5-flash-lite")
        self.assertEqual(by_key["gemini_2_5_flash_lite"].provider, "gemini")

        # Multi-tenant isolation: Company 2 must see 0 rows for Company 1's call
        ctx_company_2 = TenantContext(
            user_id=2,
            user_email="user@other.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            company_id=2,
            is_super_admin=False,
            allowed_company_ids=[2],
            allowed_service_ids=[2],
            allowed_agent_ids=None,
        )
        history_c2 = await list_analyses_history(
            db=self.db_session,
            call_id="CALL_AB_123",
            context=ctx_company_2,
        )
        self.assertEqual(len(history_c2), 0)

    # ── 18. Mass Evaluations Isolation ───────────────────────────────────────

    def test_18_mass_evaluations_does_not_use_laboratory_catalog(self):
        s = get_settings()
        s.lab_gemini_api_key = "SECRET_LAB_ONLY_KEY"
        s.gemini_api_key = "GLOBAL_PROD_GEMINI_KEY"

        from app.services.ai_credentials import resolve_gemini_credentials
        from app.services import mass_evaluation_service

        api_key, _ = resolve_gemini_credentials(company_id=1, service_id=1, module="mass_evaluations")
        self.assertNotEqual(api_key, "SECRET_LAB_ONLY_KEY")

        src = inspect.getsource(mass_evaluation_service)
        self.assertNotIn("laboratory_ai_service", src)
        self.assertNotIn("LAB_MODEL_CATALOG", src)


if __name__ == "__main__":
    unittest.main()
