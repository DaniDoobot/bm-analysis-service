"""
Unit tests for AI Usage Metadata instrumentation and persistence (Phase 2).
Verifies parsing of Gemini SDK and WebSocket objects, non-blocking telemetry recording,
cumulative Live session usage tracking, and backward-compatible provider returns.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from app.models.ai_usage import AIUsageEvent
from app.services.ai_provider import AIProvider, AIResponse, GeminiProvider, get_ai_provider
from app.services.ai_usage_service import (
    AIUsageMetadata,
    LiveSessionUsageTracker,
    parse_usage_metadata,
    record_ai_usage_event,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. PARSER TESTS: parse_usage_metadata
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_usage_metadata_none():
    """Verify that None raw metadata returns AIUsageMetadata with None token counts."""
    parsed = parse_usage_metadata(
        None,
        model="models/gemini-2.0-flash",
        credential_alias="boston_front",
        response_id="resp-123",
        execution_latency_ms=450,
        audio_duration_seconds=12.5,
        status="error",
        attempt_number=2,
    )
    assert parsed.provider == "gemini"
    assert parsed.model == "models/gemini-2.0-flash"
    assert parsed.credential_alias == "boston_front"
    assert parsed.response_id == "resp-123"
    assert parsed.execution_latency_ms == 450
    assert parsed.audio_duration_seconds == 12.5
    assert parsed.status == "error"
    assert parsed.attempt_number == 2
    assert parsed.prompt_token_count is None
    assert parsed.candidates_token_count is None
    assert parsed.total_token_count is None
    assert parsed.cached_content_token_count is None
    assert parsed.thoughts_token_count is None
    assert parsed.input_text_tokens is None
    assert parsed.input_audio_tokens is None
    assert parsed.output_text_tokens is None
    assert parsed.output_audio_tokens is None


def test_parse_usage_metadata_sdk_text_only():
    """Verify parsing of Google GenAI SDK object with text input and text output."""
    raw = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=150,
        candidates_token_count=45,
        total_token_count=195,
        cached_content_token_count=10,
        thoughts_token_count=5,
        prompt_tokens_details=[
            types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=150)
        ],
        candidates_tokens_details=[
            types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=45)
        ],
    )
    parsed = parse_usage_metadata(raw, model="gemini-2.0-flash")
    assert parsed.prompt_token_count == 150
    assert parsed.candidates_token_count == 45
    assert parsed.total_token_count == 195
    assert parsed.cached_content_token_count == 10
    assert parsed.thoughts_token_count == 5
    # Modality breakdown: text is 150, audio is explicitly 0 (not None)
    assert parsed.input_text_tokens == 150
    assert parsed.input_audio_tokens == 0
    assert parsed.output_text_tokens == 45
    assert parsed.output_audio_tokens == 0


def test_parse_usage_metadata_sdk_multimodal_audio():
    """Verify parsing of Google GenAI SDK object with audio input and text output."""
    raw = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=1200,
        candidates_token_count=80,
        total_token_count=1280,
        cached_content_token_count=0,
        prompt_tokens_details=[
            types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=100),
            types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=1100),
        ],
        candidates_tokens_details=[
            types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=80),
        ],
    )
    parsed = parse_usage_metadata(raw, audio_duration_seconds=55.2)
    assert parsed.prompt_token_count == 1200
    assert parsed.candidates_token_count == 80
    assert parsed.total_token_count == 1280
    assert parsed.input_text_tokens == 100
    assert parsed.input_audio_tokens == 1100
    assert parsed.output_text_tokens == 80
    assert parsed.output_audio_tokens == 0
    assert parsed.audio_duration_seconds == 55.2


def test_parse_usage_metadata_websocket_camel_case_dict():
    """Verify parsing of WebSocket JSON dictionaries using camelCase keys."""
    raw = {
        "promptTokenCount": 350,
        "candidatesTokenCount": 120,
        "totalTokenCount": 470,
        "cachedContentTokenCount": 50,
        "thoughtsTokenCount": 15,
        "promptTokensDetails": [
            {"modality": "AUDIO", "tokenCount": 350}
        ],
        "candidatesTokensDetails": [
            {"modality": "AUDIO", "tokenCount": 120}
        ]
    }
    parsed = parse_usage_metadata(raw)
    assert parsed.prompt_token_count == 350
    assert parsed.candidates_token_count == 120
    assert parsed.total_token_count == 470
    assert parsed.cached_content_token_count == 50
    assert parsed.thoughts_token_count == 15
    assert parsed.input_text_tokens == 0
    assert parsed.input_audio_tokens == 350
    assert parsed.output_text_tokens == 0
    assert parsed.output_audio_tokens == 120


def test_parse_usage_metadata_without_modality_details():
    """If details array is absent, modalities must be None, not 0."""
    raw = {
        "promptTokenCount": 200,
        "candidatesTokenCount": 50,
        "totalTokenCount": 250,
    }
    parsed = parse_usage_metadata(raw)
    assert parsed.prompt_token_count == 200
    assert parsed.input_text_tokens is None
    assert parsed.input_audio_tokens is None
    assert parsed.output_text_tokens is None
    assert parsed.output_audio_tokens is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. PERSISTENCE TESTS: record_ai_usage_event
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_ai_usage_event_success():
    """Test non-blocking DB insertion of AIUsageEvent."""
    usage = AIUsageMetadata(
        provider="gemini",
        model="models/gemini-2.0-flash",
        credential_alias="boston_expac",
        prompt_token_count=100,
        candidates_token_count=20,
        total_token_count=120,
        cached_content_token_count=0,
        thoughts_token_count=0,
        input_text_tokens=100,
        input_audio_tokens=0,
        output_text_tokens=20,
        output_audio_tokens=0,
        response_id="gen-abc-123",
        execution_latency_ms=310,
        audio_duration_seconds=4.5,
    )

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    with patch("app.services.ai_usage_service.AsyncSessionLocal", return_value=mock_session):
        await record_ai_usage_event(
            company_id=1,
            service_id=2,
            module="mass_evaluations",
            operation="evaluate_call",
            provider="gemini",
            model="models/gemini-2.0-flash",
            credential_alias="boston_expac",
            usage=usage,
            call_id="call-9999",
            audio_duration_seconds=4.5,
        )

        mock_session.__aenter__.return_value.add.assert_called_once()
        added_event = mock_session.__aenter__.return_value.add.call_args[0][0]
        assert isinstance(added_event, AIUsageEvent)
        assert added_event.company_id == 1
        assert added_event.service_id == 2
        assert added_event.module == "mass_evaluations"
        assert added_event.operation == "evaluate_call"
        assert added_event.model == "models/gemini-2.0-flash"
        assert added_event.credential_alias == "boston_expac"
        assert added_event.call_id == "call-9999"
        assert added_event.prompt_token_count == 100
        assert added_event.candidates_token_count == 20
        assert added_event.total_token_count == 120
        assert added_event.input_text_tokens == 100
        assert added_event.input_audio_tokens == 0
        assert added_event.output_text_tokens == 20
        assert added_event.output_audio_tokens == 0
        assert added_event.audio_duration_seconds == Decimal("4.50")
        assert added_event.execution_latency_ms == 310
        assert added_event.status == "success"
        mock_session.__aenter__.return_value.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_ai_usage_event_db_failure_does_not_raise():
    """Verify that a DB exception does not raise or disrupt caller logic."""
    with patch("app.services.ai_usage_service.AsyncSessionLocal", side_effect=RuntimeError("Database connection lost")):
        # Must not raise
        await record_ai_usage_event(
            company_id=1,
            service_id=1,
            module="test",
            status="error",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. LIVE TRACKER TESTS: LiveSessionUsageTracker
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_session_usage_tracker_monotonic_cumulative():
    """Verify cumulative WebSocket messages do not cause duplicate counting."""
    tracker = LiveSessionUsageTracker(
        company_id=1,
        service_id=3,
        module="trainer_voice_session",
        model="models/gemini-2.0-flash-exp",
        credential_alias="boston_comerciales",
        trainer_session_id=42,
        call_id="CA12345",
    )

    # Turn 1: initial tokens
    tracker.update({
        "promptTokenCount": 100,
        "candidatesTokenCount": 30,
        "totalTokenCount": 130,
        "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 100}],
        "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 30}],
    })
    assert tracker.prompt_token_count == 100
    assert tracker.candidates_token_count == 30
    assert tracker.total_token_count == 130

    # Turn 2: server sends cumulative total for the session
    tracker.update({
        "promptTokenCount": 250,
        "candidatesTokenCount": 80,
        "totalTokenCount": 330,
        "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 250}],
        "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 80}],
    })
    assert tracker.prompt_token_count == 250
    assert tracker.candidates_token_count == 80
    assert tracker.total_token_count == 330
    assert tracker.input_audio_tokens == 250
    assert tracker.output_audio_tokens == 80

    # Turn 3: late or out-of-order partial message doesn't lower the peak
    tracker.update({
        "promptTokenCount": 200,
        "candidatesTokenCount": 70,
        "totalTokenCount": 270,
    })
    assert tracker.prompt_token_count == 250
    assert tracker.candidates_token_count == 80
    assert tracker.total_token_count == 330

    # Finalize should record exactly one event with 330 total tokens
    with patch("app.services.ai_usage_service.record_ai_usage_event", new_callable=AsyncMock) as mock_record:
        await tracker.finalize(audio_duration_seconds=45.0)
        mock_record.assert_awaited_once()
        k = mock_record.call_args[1]
        assert k["company_id"] == 1
        assert k["service_id"] == 3
        assert k["module"] == "trainer_voice_session"
        assert k["trainer_session_id"] == 42
        assert k["call_id"] == "CA12345"
        assert k["audio_duration_seconds"] == 45.0
        assert k["usage"].total_token_count == 330
        assert k["usage"].input_audio_tokens == 250
        assert k["usage"].output_audio_tokens == 80


# ─────────────────────────────────────────────────────────────────────────────
# 4. AIResponse & PROVIDER BACKWARD COMPATIBILITY TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_ai_response_properties_and_dict_access():
    """Verify AIResponse behaves like a string or dict for seamless backward compatibility."""
    resp = AIResponse(
        text='{"score": 8.5}',
        usage=AIUsageMetadata(total_token_count=200),
        response_id="r-1",
        data={"score": 8.5, "provider": "gemini"},
    )
    # String representation
    assert str(resp) == '{"score": 8.5}'
    # Direct attributes
    assert resp.text == '{"score": 8.5}'
    assert resp.usage.total_token_count == 200
    # Dict-like subscription for transcribe results
    assert resp["score"] == 8.5
    assert resp.get("provider") == "gemini"
    assert resp.get("nonexistent", "default") == "default"


@pytest.mark.asyncio
async def test_gemini_provider_complete_text_backwards_compatible():
    """When return_usage is False (default), complete_text returns raw str."""
    with patch("google.genai.Client") as mock_client_cls, \
         patch("app.services.ai_provider.record_ai_usage_event", new_callable=AsyncMock) as mock_record:
        mock_gen_resp = MagicMock()
        mock_gen_resp.text = '{"status": "ok"}'
        mock_gen_resp.response_id = "test-resp-id"
        mock_gen_resp.usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=50,
            candidates_token_count=10,
            total_token_count=60,
        )

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_gen_resp)
        mock_client_cls.return_value = mock_client

        provider = GeminiProvider(api_key="test-key", credential_alias="test_alias")
        result = await provider.complete_text(
            messages=[{"role": "user", "content": "Hola"}],
            company_id=1,
            service_id=1,
            module="test_module",
        )

        # 1. Backwards compatible return type: pure str
        assert isinstance(result, str)
        assert result == '{"status": "ok"}'

        # 2. Telemetry event was recorded in background
        mock_record.assert_awaited_once()
        call_kwargs = mock_record.call_args[1]
        assert call_kwargs["company_id"] == 1
        assert call_kwargs["service_id"] == 1
        assert call_kwargs["module"] == "test_module"
        assert call_kwargs["credential_alias"] == "test_alias"
        assert call_kwargs["usage"].total_token_count == 60


@pytest.mark.asyncio
async def test_gemini_provider_complete_text_with_usage():
    """When return_usage is True or complete_text_with_usage is called, returns AIResponse."""
    with patch("google.genai.Client") as mock_client_cls, \
         patch("app.services.ai_provider.record_ai_usage_event", new_callable=AsyncMock):
        mock_gen_resp = MagicMock()
        mock_gen_resp.text = "Hello world"
        mock_gen_resp.response_id = "resp-456"
        mock_gen_resp.usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=20,
            candidates_token_count=5,
            total_token_count=25,
        )

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_gen_resp)
        mock_client_cls.return_value = mock_client

        provider = GeminiProvider(api_key="test-key", credential_alias="test_alias")
        result = await provider.complete_text_with_usage(
            messages=[{"role": "user", "content": "Hi"}],
            company_id=7,
        )

        assert isinstance(result, AIResponse)
        assert result.text == "Hello world"
        assert result.response_id == "resp-456"
        assert result.usage.total_token_count == 25


@pytest.mark.asyncio
async def test_gemini_provider_retry_telemetry():
    """Verify that transient failures record status='transient_retry' before retrying."""
    with patch("google.genai.Client") as mock_client_cls, \
         patch("app.services.ai_provider.record_ai_usage_event", new_callable=AsyncMock) as mock_record, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_gen_resp = MagicMock()
        mock_gen_resp.text = '{"success": true}'
        mock_gen_resp.response_id = "retry-id"
        mock_gen_resp.usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=50, candidates_token_count=10, total_token_count=60
        )

        # 1st call fails with transient 503, 2nd call succeeds
        class Transient503(Exception):
            pass

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=[Exception("503 UNAVAILABLE service overloaded"), mock_gen_resp]
        )
        mock_client_cls.return_value = mock_client

        provider = GeminiProvider(api_key="test-key", credential_alias="demo_alias")
        res = await provider.analyze_audio_bytes(
            audio_bytes=b"dummy audio data",
            prompt_text="Analyze audio",
            company_id=7,
            service_id=1,
            module="mass_evaluations",
        )

        assert res == '{"success": true}'
        assert mock_record.await_count == 2
        # First call was transient retry
        first_call = mock_record.call_args_list[0][1]
        assert first_call["status"] == "transient_retry"
        assert first_call["attempt_number"] == 1
        assert first_call["usage"] is None  # no tokens on error

        # Second call was success
        second_call = mock_record.call_args_list[1][1]
        assert second_call["status"] == "success"
        assert second_call["attempt_number"] == 2
        assert second_call["usage"].total_token_count == 60
