import asyncio
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import app.routers.training_voice as training_voice_module
from app.routers.training_voice import (
    HANGUP_EARLY_BLOCK_SECONDS,
    twilio_media_stream,
)


class AsyncMockWs:
    """Mock WebSocket for Twilio and Gemini that supports async iteration and message capture."""
    def __init__(self, messages=None, headers=None):
        self.messages = list(messages or [])
        self.sent_messages = []
        self.headers = headers or {"host": "localhost"}
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self.messages:
            item = self.messages.pop(0)
            if isinstance(item, tuple) and item[0] == "sleep":
                await asyncio.sleep(item[1])
                continue
            if isinstance(item, Exception):
                raise item
            return item
        # Once exhausted, wait to simulate open socket unless done
        await asyncio.sleep(0.5)
        raise StopAsyncIteration

    async def iter_text(self):
        async for msg in self:
            yield msg

    async def send(self, data):
        self.sent_messages.append(data)

    async def send_text(self, data):
        self.sent_messages.append(data)

    async def close(self):
        self.closed = True

    async def accept(self):
        pass


class TestTrainingVoiceVadAndHangup(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        training_voice_module.settings.gemini_api_key = "fake_test_key"
        training_voice_module.settings.gemini_model = "models/gemini-3.1-flash-live-preview"

    def test_hangup_early_block_constant(self):
        """Verifica que la constante HANGUP_EARLY_BLOCK_SECONDS es 90."""
        self.assertEqual(HANGUP_EARLY_BLOCK_SECONDS, 90)

    @patch("app.routers.training_voice.get_engine")
    @patch("app.routers.training_voice.websockets.connect")
    async def test_vad_config_and_tool_definition_in_setup(self, mock_ws_connect, mock_get_engine):
        """Verifica que setup_msg incluye la configuración VAD tolerante y la descripción enriquecida de hangup_call."""
        mock_session = AsyncMock()
        mock_db_context = AsyncMock()
        mock_db_context.__aenter__.return_value = mock_session

        fake_sess = MagicMock(agent_id="agent_01", conversation_id=101, cycle_id=201)
        fake_prompt = MagicMock(prompt_text="Roleplay prompt test")
        fake_setting = MagicMock(agent_name="Demo Agente")

        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_prompt = MagicMock()
        res_prompt.scalars.return_value.first.return_value = fake_prompt
        res_rem = MagicMock(scalar=MagicMock(return_value=1))
        res_set = MagicMock()
        res_set.scalars.return_value.first.return_value = fake_setting

        mock_session.execute.side_effect = [res_sess, res_prompt, res_rem, res_set]
        mock_get_engine.return_value = MagicMock()

        mock_tw_ws = AsyncMockWs()
        mock_gem_ws = AsyncMockWs()
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        with patch("app.routers.training_voice.AsyncSession", return_value=mock_db_context):
            with patch("app.routers.training_voice.get_active_cycles_for_agent", AsyncMock(return_value=[])):
                task = asyncio.create_task(
                    twilio_media_stream(websocket=mock_tw_ws, flow=None, session_id=1)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Inspect setup_msg sent to Gemini
        self.assertTrue(len(mock_gem_ws.sent_messages) > 0, "No se envió setup_msg a Gemini WS")
        setup_raw = mock_gem_ws.sent_messages[0]
        setup_data = json.loads(setup_raw)

        self.assertIn("setup", setup_data)
        setup = setup_data["setup"]

        # VAD Config
        vad = setup["realtimeInputConfig"]["automaticActivityDetection"]
        self.assertEqual(vad["silenceDurationMs"], 600, "silenceDurationMs debe ser 600ms")
        self.assertEqual(vad["prefixPaddingMs"], 200, "prefixPaddingMs debe ser 200ms")
        self.assertEqual(vad["endOfSpeechSensitivity"], "END_SENSITIVITY_LOW", "endOfSpeechSensitivity debe ser END_SENSITIVITY_LOW")
        self.assertEqual(vad["startOfSpeechSensitivity"], "START_SENSITIVITY_LOW")

        # Tool definition
        tools = setup["tools"]
        hangup_decl = next(d for t in tools for d in t["functionDeclarations"] if d["name"] == "hangup_call")
        desc = hangup_decl["description"]
        self.assertIn("pausas", desc.lower())
        self.assertIn("supervisor", desc.lower())
        self.assertIn("despedida mutua", desc.lower())

    @patch("app.routers.training_voice.hangup_twilio_call", new_callable=AsyncMock)
    @patch("app.routers.training_voice.handle_roleplay_hangup", new_callable=AsyncMock)
    @patch("app.routers.training_voice.get_engine")
    @patch("app.routers.training_voice.websockets.connect")
    async def test_hangup_call_blocked_when_under_90_seconds(
        self, mock_ws_connect, mock_get_engine, mock_handle_hangup, mock_twilio_hangup
    ):
        """Si Gemini pide hangup_call antes de 90s, el backend lo bloquea y le ordena continuar."""
        mock_session = AsyncMock()
        mock_db_context = AsyncMock()
        mock_db_context.__aenter__.return_value = mock_session

        fake_sess = MagicMock(agent_id="agent_01", conversation_id=101, cycle_id=201)
        fake_prompt = MagicMock(prompt_text="Roleplay prompt test")
        fake_setting = MagicMock(agent_name="Demo Agente")

        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_prompt = MagicMock()
        res_prompt.scalars.return_value.first.return_value = fake_prompt
        res_rem = MagicMock(scalar=MagicMock(return_value=1))
        res_set = MagicMock()
        res_set.scalars.return_value.first.return_value = fake_setting

        mock_session.execute.side_effect = [res_sess, res_prompt, res_rem, res_set]
        mock_get_engine.return_value = MagicMock()

        # Controlled time simulation
        simulated_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        real_datetime = datetime

        class ControlledDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return simulated_time

        start_event = json.dumps({
            "event": "start",
            "start": {
                "streamSid": "stream_123",
                "callSid": "call_123",
                "customParameters": {"session_id": "1"}
            }
        })

        hangup_call_msg = json.dumps({
            "toolCall": {
                "functionCalls": [{
                    "id": "fn_call_1",
                    "name": "hangup_call",
                    "args": {"reason": "agente_menciona_supervisor"}
                }]
            }
        })

        mock_tw_ws = AsyncMockWs(messages=[start_event])
        mock_gem_ws = AsyncMockWs(messages=[("sleep", 0.05), hangup_call_msg])
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        with patch("app.routers.training_voice.datetime", ControlledDatetime):
            with patch("app.routers.training_voice.AsyncSession", return_value=mock_db_context):
                with patch("app.routers.training_voice.get_active_cycles_for_agent", AsyncMock(return_value=[])):
                    with patch("app.routers.training_voice.start_twilio_recording", AsyncMock(return_value="rec_1")):
                        with patch("app.routers.training_voice.duration_monitor_task", AsyncMock()):
                            task = asyncio.create_task(
                                twilio_media_stream(websocket=mock_tw_ws, flow=None, session_id=1)
                            )
                            # Wait until start event sets call_start_time
                            await asyncio.sleep(0.02)
                            # Advance time by 30 seconds (< 90s)
                            simulated_time = simulated_time + timedelta(seconds=30)
                            # Wait for hangup_call toolCall to be processed
                            await asyncio.sleep(0.1)
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

        # Verification: Twilio hangup must NOT be called for premature hangup
        mock_twilio_hangup.assert_not_called()

        # Verification: Gemini received blocked_too_early toolResponse
        sent_payloads = [json.loads(c) for c in mock_gem_ws.sent_messages if c.startswith("{")]
        tool_responses = [p for p in sent_payloads if "toolResponse" in p]
        self.assertTrue(len(tool_responses) > 0, "Debe haberse enviado toolResponse a Gemini")

        fn_resps = tool_responses[0]["toolResponse"]["functionResponses"]
        self.assertEqual(fn_resps[0]["response"]["result"], "blocked_too_early")
        self.assertIn("La simulación sigue activa", fn_resps[0]["response"]["instruction"])

    @patch("app.routers.training_voice.hangup_twilio_call", new_callable=AsyncMock)
    @patch("app.routers.training_voice.handle_roleplay_hangup", new_callable=AsyncMock)
    @patch("app.routers.training_voice.get_engine")
    @patch("app.routers.training_voice.websockets.connect")
    async def test_hangup_call_allowed_when_over_90_seconds(
        self, mock_ws_connect, mock_get_engine, mock_handle_hangup, mock_twilio_hangup
    ):
        """Si Gemini pide hangup_call tras >= 90s, el backend permite el cuelgue limpio."""
        mock_session = AsyncMock()
        mock_db_context = AsyncMock()
        mock_db_context.__aenter__.return_value = mock_session

        fake_sess = MagicMock(agent_id="agent_01", conversation_id=101, cycle_id=201)
        fake_prompt = MagicMock(prompt_text="Roleplay prompt test")
        fake_setting = MagicMock(agent_name="Demo Agente")

        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_prompt = MagicMock()
        res_prompt.scalars.return_value.first.return_value = fake_prompt
        res_rem = MagicMock(scalar=MagicMock(return_value=1))
        res_set = MagicMock()
        res_set.scalars.return_value.first.return_value = fake_setting

        mock_session.execute.side_effect = [res_sess, res_prompt, res_rem, res_set]
        mock_get_engine.return_value = MagicMock()

        simulated_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        class ControlledDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return simulated_time

        start_event = json.dumps({
            "event": "start",
            "start": {
                "streamSid": "stream_123",
                "callSid": "call_123",
                "customParameters": {"session_id": "1"}
            }
        })

        hangup_call_msg = json.dumps({
            "toolCall": {
                "functionCalls": [{
                    "id": "fn_call_1",
                    "name": "hangup_call",
                    "args": {"reason": "exito_conversacional"}
                }]
            }
        })

        mock_tw_ws = AsyncMockWs(messages=[start_event])
        mock_gem_ws = AsyncMockWs(messages=[("sleep", 0.05), hangup_call_msg])
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        with patch("app.routers.training_voice.datetime", ControlledDatetime):
            with patch("app.routers.training_voice.AsyncSession", return_value=mock_db_context):
                with patch("app.routers.training_voice.get_active_cycles_for_agent", AsyncMock(return_value=[])):
                    with patch("app.routers.training_voice.start_twilio_recording", AsyncMock(return_value="rec_1")):
                        with patch("app.routers.training_voice.duration_monitor_task", AsyncMock()):
                            task = asyncio.create_task(
                                twilio_media_stream(websocket=mock_tw_ws, flow=None, session_id=1)
                            )
                            # Wait until start event sets call_start_time
                            await asyncio.sleep(0.02)
                            # Advance time by 95 seconds (>= 90s)
                            simulated_time = simulated_time + timedelta(seconds=95)
                            # Wait for hangup_call toolCall to be processed
                            await asyncio.sleep(0.1)
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

        # Verification: Twilio hangup and roleplay hangup handler were executed
        mock_handle_hangup.assert_called_once_with(
            session_id=1,
            call_sid="call_123",
            call_start_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            reason="exito_conversacional"
        )
        mock_twilio_hangup.assert_called_once_with("call_123")

        # Verification: Gemini received ok: True response
        sent_payloads = [json.loads(c) for c in mock_gem_ws.sent_messages if c.startswith("{")]
        tool_responses = [p for p in sent_payloads if "toolResponse" in p]
        self.assertTrue(len(tool_responses) > 0, "Debe haberse enviado toolResponse a Gemini")
        fn_resps = tool_responses[0]["toolResponse"]["functionResponses"]
        self.assertEqual(fn_resps[0]["response"]["result"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
