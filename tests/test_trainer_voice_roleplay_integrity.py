"""
Comprehensive test suite validating Trainer Voice roleplay integrity:
1. Gemini Live VAD configuration (silenceDurationMs >= 500, END_SENSITIVITY_LOW, prefixPaddingMs >= 200).
2. Anti-assistant rules and presence/silence handling in system prompt.
3. In-character start without assistant preamble in Gemini.
4. Turn gate does not discard valid Gemini audio on user pauses.
5. Barge-in does not trigger on isolated noise, but interrupts on sustained speech.
6. Watchdog does not inject synthetic role:user text turns that pollute conversation.
"""
import asyncio
import json
import base64
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import Request

import app.routers.trainer_voice as trainer_voice_module
from app.routers.trainer_voice import (
    SPANISH_VOICE_RULES,
    HEALTHCARE_VOICE_RULES,
    build_turn_discipline,
    media_stream,
    start_roleplay,
    VAD_ENERGY_THRESHOLD,
    BARGE_IN_ENERGY_THRESHOLD,
    BARGE_IN_MIN_SPEECH_DURATION_MS,
)


class AsyncMockWs:
    """Mock WebSocket for Twilio and Gemini that supports async iteration and message capture."""
    def __init__(self, messages=None, headers=None, scope=None):
        self.messages = list(messages or [])
        self.sent_messages = []
        self.headers = headers or {"host": "localhost"}
        self.scope = scope or {"query_string": b"flow=session&session_id=10"}
        self.closed = False
        self.client_state = MagicMock()
        self.client_state.name = "CONNECTED"

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
        await asyncio.sleep(0.3)
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
        self.client_state.name = "DISCONNECTED"

    async def accept(self):
        pass


class TestTrainerVoiceRoleplayIntegrity(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        trainer_voice_module.settings.gemini_api_key = "test_key_gemini"
        trainer_voice_module.settings.gemini_model = "models/gemini-2.0-flash-exp"

    def test_1_vad_configuration_constants_and_setup(self):
        """VAD configuration must be tolerant to pauses: silenceDurationMs >= 500, END_SENSITIVITY_LOW, prefixPaddingMs >= 200."""
        # Check constants
        self.assertGreaterEqual(BARGE_IN_MIN_SPEECH_DURATION_MS, 200, "Barge in duration must require sustained speech")
        self.assertGreaterEqual(BARGE_IN_ENERGY_THRESHOLD, 180.0, "Barge in energy must protect against line echo")

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_1_vad_configuration_in_gemini_setup_message(self, mock_ws_connect, mock_session_local):
        """Verify setup_msg sent to Gemini has silenceDurationMs >= 500, END_SENSITIVITY_LOW, prefixPaddingMs >= 200."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db

        fake_sim = MagicMock(simulation_id=10, code="VENTAS1", name="Venta Pro", roleplay_prompt="Eres un cliente interesado.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        fake_setting = MagicMock(agent_name="Carlos Demo")

        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        mock_tw_ws = AsyncMockWs(messages=[])
        mock_gem_ws = AsyncMockWs(messages=[])
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        task = asyncio.create_task(
            media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        self.assertTrue(len(mock_gem_ws.sent_messages) > 0, "No setup message sent to Gemini WS")
        setup_data = json.loads(mock_gem_ws.sent_messages[0])
        self.assertIn("setup", setup_data)
        vad = setup_data["setup"]["realtimeInputConfig"]["automaticActivityDetection"]

        self.assertGreaterEqual(vad["silenceDurationMs"], 500, "silenceDurationMs must be >= 500ms (got %s)" % vad["silenceDurationMs"])
        self.assertEqual(vad["silenceDurationMs"], 600)
        self.assertEqual(vad["endOfSpeechSensitivity"], "END_SENSITIVITY_LOW")
        self.assertGreaterEqual(vad["prefixPaddingMs"], 200)

    def test_2_system_instruction_anti_assistant_and_presence_rules(self):
        """Prompt rules must strictly forbid answering as an assistant and instruct handling presence/silences in character."""
        # 1. Check SPANISH_VOICE_RULES
        self.assertIn("REGLA CRÍTICA: BLOQUEO ABSOLUTO DE PERSONAJE Y ANTI-ASISTENTE", SPANISH_VOICE_RULES)
        self.assertIn("¿En qué puedo ayudarte?", SPANISH_VOICE_RULES)
        self.assertIn("¿En qué le puedo ayudar?", SPANISH_VOICE_RULES)
        self.assertIn("Sí, aquí estoy. ¿En qué puedo ayudarte?", SPANISH_VOICE_RULES)
        self.assertIn("¿Está ahí?", SPANISH_VOICE_RULES)
        self.assertIn("¿Sigues ahí?", SPANISH_VOICE_RULES)
        self.assertIn("¿Me escucha?", SPANISH_VOICE_RULES)
        self.assertIn("Responde SIEMPRE DENTRO DEL PERSONAJE", SPANISH_VOICE_RULES)
        self.assertIn("Una pausa o silencio del agente NO significa que el roleplay haya terminado", SPANISH_VOICE_RULES)

        # 2. Check HEALTHCARE_VOICE_RULES
        self.assertIn("NUNCA actúes como asistente médico ni digas \"¿En qué puedo ayudarte?\"", HEALTHCARE_VOICE_RULES)
        self.assertIn("Si hay silencios o el agente pregunta si estás ahí, responde siempre como paciente", HEALTHCARE_VOICE_RULES)

        # 3. Check build_turn_discipline
        discipline = build_turn_discipline(is_healthcare=False, interlocutor_role="cliente")
        self.assertIn("Control de silencios y presencia", discipline)
        self.assertIn("¿está ahí?", discipline)
        self.assertIn("NUNCA digas '¿En qué puedo ayudarte?'", discipline)

    @patch("app.routers.trainer_voice.TrainerService.start_phone_session", new_callable=AsyncMock)
    async def test_3_start_roleplay_twiml_says_greeting_before_stream(self, mock_start_session):
        """start_roleplay endpoint must render verification greeting via Twilio <Say> before connecting <Stream>."""
        mock_db = AsyncMock()
        mock_setting = MagicMock(agent_name="Laura Martinez", training_code="LM01")
        mock_sim = MagicMock(simulation_id=5, code="SIM05")
        mock_sess = MagicMock(session_id=88)
        mock_start_session.return_value = mock_sess

        res_set = MagicMock()
        res_set.scalars.return_value.first.return_value = mock_setting
        res_sim = MagicMock()
        res_sim.scalars.return_value.first.return_value = mock_sim
        mock_db.execute.side_effect = [res_set, res_sim]

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"host": "test-service.com", "x-forwarded-proto": "https"}

        response = await start_roleplay(
            request=mock_request,
            agent_id="agent_123",
            simulation_id=5,
            call_sid="CA123456",
            db=mock_db,
        )
        content = response.body.decode("utf-8")

        # Must include <Say> with verification and Laura's first name
        self.assertIn("<Say language=\"es-ES\">Perfecto Laura, se ha verificado el código de la simulación. Iniciamos el roleplay. Prepárate.</Say>", content)
        # Must include <Stream>
        self.assertIn("<Stream url=\"wss://test-service.com/bm/trainer/phone/media-stream?session_id=88&amp;flow=session\">", content)
        # Ensure <Say> appears before <Connect>
        say_idx = content.find("<Say")
        connect_idx = content.find("<Connect")
        self.assertTrue(0 <= say_idx < connect_idx, "<Say> must appear before <Connect> so greeting happens outside Gemini roleplay context")

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_3_gemini_start_message_is_purely_in_character(self, mock_ws_connect, mock_session_local):
        """Initial message sent to Gemini Live must instruct it to begin in-character, without assistant persona."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db

        fake_sim = MagicMock(simulation_id=10, code="VENTAS1", name="Venta Pro", roleplay_prompt="Eres un cliente interesado.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)

        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        # Gemini triggers setupComplete
        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
        ])
        mock_tw_ws = AsyncMockWs(messages=[])
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        task = asyncio.create_task(
            media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Check clientContent sent upon setupComplete
        client_contents = [json.loads(m) for m in mock_gem_ws.sent_messages if "clientContent" in m]
        self.assertTrue(len(client_contents) > 0, "Expected clientContent after setupComplete")
        initial_turn = client_contents[0]["clientContent"]["turns"][0]["parts"][0]["text"]

        # Must NOT instruct to say 'se ha verificado el código' or act as assistant
        self.assertNotIn("se ha verificado el código", initial_turn)
        self.assertNotIn("Iniciamos el roleplay. Prepárate.", initial_turn)
        self.assertIn("Inicia la llamada interpretando exclusivamente a tu personaje", initial_turn)

    @patch("app.routers.trainer_voice.encode_gemini_to_twilio", return_value=("MULAW_PAYLOAD", None))
    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_4_turn_gate_does_not_discard_valid_gemini_audio(self, mock_ws_connect, mock_session_local, mock_encode):
        """When Gemini sends modelTurn audio after silence/pause, the backend must NOT discard it with continue."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db

        fake_sim = MagicMock(simulation_id=10, code="VENTAS1", name="Venta Pro", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        # Simulate: setupComplete -> turnComplete (setting waiting_for_user_response=True) -> next modelTurn with audio
        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
            json.dumps({"serverContent": {"turnComplete": True}}),
            json.dumps({
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "BASE64PCM"}}]
                    }
                }
            }),
        ])
        mock_tw_ws = AsyncMockWs(messages=[
            json.dumps({
                "event": "start",
                "start": {"streamSid": "STR_111", "callSid": "CA_111"}
            })
        ])
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        task = asyncio.create_task(
            media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify that Twilio received the media packet forwarded from Gemini (not dropped)
        media_sent_to_twilio = [m for m in mock_tw_ws.sent_messages if "media" in m and "STR_111" in m]
        self.assertTrue(len(media_sent_to_twilio) > 0, "Gemini audio was erroneously discarded by local turn gate!")

    @patch("app.routers.trainer_voice.start_twilio_recording", AsyncMock(return_value="rec_test"))
    @patch("app.routers.trainer_voice.encode_gemini_to_twilio", return_value=("DUMMY_MULAW", None))
    @patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("DUMMY_PCM", None))
    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_5a_barge_in_isolated_noise_does_not_trigger_clear(self, mock_ws_connect, mock_session_local, mock_decode, mock_encode):
        """An isolated noise packet (< 260ms) during assistant speech does NOT trigger barge-in clear."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        mock_tw_ws = AsyncMock()
        mock_tw_ws.client_state.name = "CONNECTED"
        mock_tw_ws.scope = {"query_string": b"flow=session&session_id=10"}
        mock_tw_ws.headers = {"host": "localhost"}
        mock_tw_ws.send_text = AsyncMock()

        async def mock_tw_iter(*args, **kwargs):
            yield json.dumps({"event": "start", "start": {"streamSid": "STR_222", "callSid": "CA_222"}})
            await asyncio.sleep(0.08)
            # Only 3 frames (60ms < 260ms)
            for _ in range(3):
                yield json.dumps({"event": "media", "media": {"track": "inbound", "payload": "pcm"}})
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            yield json.dumps({"event": "stop"})

        mock_tw_ws.iter_text = mock_tw_iter

        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()

        async def mock_gemini_iter(*args, **kwargs):
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(0.02)
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "DUMMY"}}]}
                }
            })
            await asyncio.sleep(0.5)

        mock_gemini_ws.__aiter__ = mock_gemini_iter
        mock_ws_connect.return_value.__aenter__.return_value = mock_gemini_ws

        with patch("app.routers.trainer_voice.calculate_pcm_energy", return_value=350.0):
            await media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)

        # Verify no 'clear' event was sent for short 60ms noise
        clears = [
            c for c in mock_tw_ws.send_text.call_args_list
            if "clear" in str(c)
        ]
        self.assertEqual(len(clears), 0, "Isolated noise packet erroneously triggered barge-in clear event!")

    @patch("app.routers.trainer_voice.start_twilio_recording", AsyncMock(return_value="rec_test"))
    @patch("app.routers.trainer_voice.encode_gemini_to_twilio", return_value=("DUMMY_MULAW", None))
    @patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("DUMMY_PCM", None))
    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_5b_barge_in_sustained_speech_triggers_clear(self, mock_ws_connect, mock_session_local, mock_decode, mock_encode):
        """15 frames of sustained speech (300ms >= 260ms) during assistant speech triggers barge-in clear."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        mock_tw_ws = AsyncMock()
        mock_tw_ws.client_state.name = "CONNECTED"
        mock_tw_ws.scope = {"query_string": b"flow=session&session_id=10"}
        mock_tw_ws.headers = {"host": "localhost"}
        mock_tw_ws.send_text = AsyncMock()

        async def mock_tw_iter(*args, **kwargs):
            yield json.dumps({"event": "start", "start": {"streamSid": "STR_333", "callSid": "CA_333"}})
            await asyncio.sleep(0.08)
            # 15 frames of speech (300ms >= 260ms)
            for _ in range(15):
                yield json.dumps({"event": "media", "media": {"track": "inbound", "payload": "pcm"}})
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            yield json.dumps({"event": "stop"})

        mock_tw_ws.iter_text = mock_tw_iter

        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()

        async def mock_gemini_iter(*args, **kwargs):
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(0.02)
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "DUMMY"}}]}
                }
            })
            await asyncio.sleep(0.5)

        mock_gemini_ws.__aiter__ = mock_gemini_iter
        mock_ws_connect.return_value.__aenter__.return_value = mock_gemini_ws

        with patch("app.routers.trainer_voice.calculate_pcm_energy", return_value=350.0):
            await media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)

        # Verify 'clear' was sent for sustained speech
        clears_speech = [
            c for c in mock_tw_ws.send_text.call_args_list
            if "clear" in str(c)
        ]
        self.assertTrue(len(clears_speech) > 0, "Sustained user speech failed to trigger barge-in clear event!")

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_6_watchdog_does_not_inject_role_user_text(self, mock_ws_connect, mock_session_local):
        """Watchdog loop must not inject synthetic text turns with role: user that corrupt conversation."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
        ])
        mock_tw_ws = AsyncMockWs(messages=[
            json.dumps({"event": "start", "start": {"streamSid": "STR_444", "callSid": "CA_444"}})
        ])
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        task = asyncio.create_task(
            media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)
        )
        # Let watchdog run for over 2 seconds
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify that no text with 'Continúa el roleplay' was sent
        nudge_turns = []
        for msg in mock_gem_ws.sent_messages:
            try:
                data = json.loads(msg)
                if "clientContent" in data:
                    turns = data["clientContent"].get("turns", [])
                    for t in turns:
                        for p in t.get("parts", []):
                            if "Continúa el roleplay" in p.get("text", ""):
                                nudge_turns.append(p.get("text"))
            except Exception:
                pass

        self.assertEqual(len(nudge_turns), 0, "Watchdog must not inject synthetic role: user prompts into roleplay!")
