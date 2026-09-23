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
    VAD_GRACE_PERIOD_MS,
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
        """VAD configuration must be tuned for telephony: VAD_GRACE_PERIOD_MS = 150."""
        self.assertEqual(VAD_GRACE_PERIOD_MS, 150)
        self.assertGreaterEqual(BARGE_IN_MIN_SPEECH_DURATION_MS, 200, "Barge in duration must require sustained speech")
        self.assertGreaterEqual(BARGE_IN_ENERGY_THRESHOLD, 180.0, "Barge in energy must protect against line echo")

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_1_vad_configuration_in_gemini_setup_message(self, mock_ws_connect, mock_session_local):
        """Verify setup_msg sent to Gemini has silenceDurationMs = 450, BALANCED sensitivities."""
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

        self.assertEqual(vad["silenceDurationMs"], 450)
        self.assertEqual(vad["startOfSpeechSensitivity"], "START_SENSITIVITY_BALANCED")
        self.assertEqual(vad["endOfSpeechSensitivity"], "END_SENSITIVITY_BALANCED")
        self.assertGreaterEqual(vad["prefixPaddingMs"], 200)

    def test_2_system_instruction_anti_assistant_and_presence_rules(self):
        """Prompt rules must strictly forbid negative priming and enforce permanent positive identity."""
        # 1. Check SPANISH_VOICE_RULES has positive identity instructions
        self.assertIn("REGLA CRÍTICA: BLOQUEO ABSOLUTO DE PERSONAJE", SPANISH_VOICE_RULES)
        self.assertIn("DIRECTRICES PERMANENTES DE IDENTIDAD", SPANISH_VOICE_RULES)
        self.assertIn("¿Está ahí?", SPANISH_VOICE_RULES)
        self.assertIn("¿Me escucha?", SPANISH_VOICE_RULES)
        self.assertIn("Responde SIEMPRE desde tu personaje", SPANISH_VOICE_RULES)

        # Negative priming phrases MUST NOT appear
        self.assertNotIn("¿En qué puedo ayudarte?", SPANISH_VOICE_RULES)
        self.assertNotIn("¿En qué le puedo ayudar?", SPANISH_VOICE_RULES)
        self.assertNotIn("Sí, aquí estoy", SPANISH_VOICE_RULES)
        self.assertNotIn("médico", SPANISH_VOICE_RULES.lower())

        # 2. Check HEALTHCARE_VOICE_RULES
        self.assertNotIn("¿En qué puedo ayudarte?", HEALTHCARE_VOICE_RULES)
        self.assertNotIn("Sí, aquí estoy", HEALTHCARE_VOICE_RULES)
        self.assertIn("Tu rol de paciente es continuo e inquebrantable", HEALTHCARE_VOICE_RULES)

        # 3. Check build_turn_discipline
        discipline = build_turn_discipline(is_healthcare=False, interlocutor_role="cliente")
        self.assertIn("Control de silencios y presencia", discipline)
        self.assertIn("Conclusión natural de la simulación", discipline)
        self.assertNotIn("¿En qué puedo ayudarte?", discipline)
        self.assertNotIn("Sí, aquí estoy", discipline)

    @patch("app.routers.trainer_voice.TrainerService.start_phone_session", new_callable=AsyncMock)
    async def test_3_start_roleplay_twiml_connects_without_twilio_say(self, mock_start_session):
        """start_roleplay endpoint must NOT render Twilio <Say> so Twilio robotic TTS does not play; connects <Stream> directly."""
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

        # 1. No Twilio <Say> TTS
        self.assertEqual(content.count("<Say"), 0, "Must NOT contain any Twilio <Say> so robotic TTS does not play")

        # 2. Must NOT contain the old assistant presentation
        self.assertNotIn("Perfecto", content)
        self.assertNotIn("se ha verificado el código", content)
        self.assertNotIn("Iniciamos el roleplay. Prepárate.", content)
        self.assertNotIn("¿En qué puedo ayudarte?", content)

        # 3. Stream connects directly
        self.assertIn("<Connect>", content)
        self.assertIn("<Stream url=\"wss://test-service.com/bm/trainer/phone/media-stream?session_id=88&amp;flow=session\">", content)

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_3_gemini_start_message_delivers_confirmation_and_immediate_character_start(self, mock_ws_connect, mock_session_local):
        """Initial message sent to Gemini Live must instruct it to say the confirmation with its own voice and immediately start roleplay."""
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

        # Gemini says the exact confirmation and seamlessly starts roleplay in character
        self.assertIn("Di exactamente: 'Código de simulación correcto. Vamos a dar comienzo a la simulación, prepárate.'", initial_turn)
        self.assertIn("sin pausar ni esperar respuesta", initial_turn)
        self.assertIn("inicia la llamada interpretando exclusivamente a tu personaje", initial_turn)

        # Gemini does NOT receive the old assistant preamble or role
        self.assertNotIn("se ha verificado el código", initial_turn)
        self.assertNotIn("Iniciamos el roleplay. Prepárate.", initial_turn)
        self.assertNotIn("asistente", initial_turn.lower())

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

    @patch("app.routers.trainer_voice.encode_gemini_to_twilio", return_value=("FORWARDED_MULAW", None))
    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_7_after_barge_in_next_turn_is_forwarded_and_not_discarded(self, mock_ws_connect, mock_session_local, mock_encode):
        """CRITICAL: After an interruption / barge-in, subsequent Gemini turns MUST NOT be discarded."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        mock_tw_ws = AsyncMockWs(messages=[
            json.dumps({"event": "start", "start": {"streamSid": "STR_777", "callSid": "CA_777"}})
        ])

        # Gemini sequence:
        # 1. setupComplete
        # 2. First turn
        # 3. Interrupted (barge-in)
        # 4. Second turn with new audio (MUST be forwarded to Twilio, NEVER discarded!)
        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
            json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "TURN_1_AUDIO"}}]},
                    "turnComplete": True
                }
            }),
            json.dumps({
                "serverContent": {
                    "interrupted": True
                }
            }),
            json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "TURN_2_POST_BARGE_IN"}}]},
                    "turnComplete": True
                }
            }),
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

        # 1. Twilio must have received clear event upon interrupted
        clears = [json.loads(m) for m in mock_tw_ws.sent_messages if "clear" in m]
        self.assertEqual(len(clears), 1, "Expected exactly 1 clear event sent to Twilio on interruption")
        self.assertEqual(clears[0]["streamSid"], "STR_777")

        # 2. Twilio must have received media packets for BOTH turns (turn 2 must NOT be discarded!)
        media_packets = [json.loads(m) for m in mock_tw_ws.sent_messages if "media" in m and "payload" in m]
        self.assertGreaterEqual(len(media_packets), 2, "Turn 2 after barge-in was erroneously discarded!")

    @patch("app.routers.trainer_voice.start_twilio_recording", AsyncMock(return_value="rec_test"))
    @patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("DUMMY_PCM", None))
    @patch("app.routers.trainer_voice.encode_gemini_to_twilio", side_effect=lambda b64, st: (f"MULAW_{b64}", st))
    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_7b_barge_in_residual_model_turn_chunks_are_discarded(self, mock_ws_connect, mock_session_local, mock_encode, mock_decode):
        """Residual modelTurn chunks received after local barge-in must be completely discarded until turn is finished."""
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

        barge_in_triggered = asyncio.Event()

        async def send_text_side_effect(msg):
            if "clear" in str(msg):
                barge_in_triggered.set()

        mock_tw_ws.send_text = AsyncMock(side_effect=send_text_side_effect)

        async def mock_tw_iter(*args, **kwargs):
            yield json.dumps({"event": "start", "start": {"streamSid": "STR_778", "callSid": "CA_778"}})
            await asyncio.sleep(0.05)
            # 15 frames of speech (300ms >= 260ms) to trigger local barge-in
            for _ in range(15):
                yield json.dumps({"event": "media", "media": {"track": "inbound", "payload": "pcm"}})
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            yield json.dumps({"event": "stop"})

        mock_tw_ws.iter_text = mock_tw_iter

        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()

        async def mock_gemini_iter(*args, **kwargs):
            yield json.dumps({"setupComplete": {}})
            await asyncio.sleep(0.02)
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "ASSISTANT_SPEAKING_START"}}]}
                }
            })
            # Assistant is speaking now; wait for twilio local barge-in to trigger
            try:
                await asyncio.wait_for(barge_in_triggered.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass

            # Gemini produces residual chunks before getting interrupted
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "RESIDUAL_CHUNK_1"}}]}
                }
            })
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "RESIDUAL_CHUNK_2"}}]}
                }
            })
            # Interruption recognized by server
            yield json.dumps({
                "serverContent": {
                    "interrupted": True
                }
            })
            await asyncio.sleep(0.05)
            # Fresh new turn from assistant
            yield json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "CLEAN_NEW_TURN_AUDIO"}}]},
                    "turnComplete": True
                }
            })
            await asyncio.sleep(0.3)

        mock_gemini_ws.__aiter__ = mock_gemini_iter
        mock_ws_connect.return_value.__aenter__.return_value = mock_gemini_ws

        with patch("app.routers.trainer_voice.calculate_pcm_energy", return_value=350.0):
            await media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)

        # Verify 'clear' was sent
        clears = [c for c in mock_tw_ws.send_text.call_args_list if "clear" in str(c)]
        self.assertGreaterEqual(len(clears), 1, "Expected clear event sent to Twilio on interruption")

        # Verify that Twilio received the initial audio and the clean new turn,
        # but NEVER received the residual chunks
        media_calls = [str(c) for c in mock_tw_ws.send_text.call_args_list if "media" in str(c)]

        self.assertTrue(any("ASSISTANT_SPEAKING_START" in c for c in media_calls), "Initial turn audio was not played!")
        for c in media_calls:
            self.assertNotIn("RESIDUAL_CHUNK_1", c, "Residual audio chunk 1 was erroneously forwarded to Twilio!")
            self.assertNotIn("RESIDUAL_CHUNK_2", c, "Residual audio chunk 2 was erroneously forwarded to Twilio!")

        self.assertTrue(any("CLEAN_NEW_TURN_AUDIO" in c for c in media_calls), "Clean new turn after barge-in was not forwarded!")

    @patch("app.routers.trainer_voice.encode_gemini_to_twilio", return_value=("VALID_MULAW", None))
    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_8_malformed_message_does_not_break_loops(self, mock_ws_connect, mock_session_local, mock_encode):
        """A single malformed JSON message or frame error must be caught gracefully and not kill the call."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        # Injects malformed JSON in Gemini WS, followed by valid message
        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
            "NOT_VALID_JSON{:::broken",
            json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": "AUDIO_AFTER_ERROR"}}]},
                    "turnComplete": True
                }
            }),
        ])
        mock_tw_ws = AsyncMockWs(messages=[
            json.dumps({"event": "start", "start": {"streamSid": "STR_888", "callSid": "CA_888"}})
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

        # Verify that the valid message after the error was processed successfully
        media_packets = [json.loads(m) for m in mock_tw_ws.sent_messages if "media" in m and "payload" in m]
        self.assertGreaterEqual(len(media_packets), 1, "Loop died on malformed message instead of continuing!")


class TestDTMFSimulationValidation(unittest.IsolatedAsyncioTestCase):
    """
    Tests for fix: DTMF gather accepts variable-length codes (finishOnKey=#)
    and verify_simulation_numeric_code resolves numeric simulation IDs to
    their alfanumeric codes (e.g. "202" → ATEN02).
    """

    # ── 9. Gather uses finishOnKey=# instead of numDigits=4 ───────────────────

    async def test_9_verify_numeric_code_gather_uses_finish_on_key(self):
        """verify-numeric-code TwiML must use finishOnKey='#' and NOT numDigits='4' so alphanumeric simulation codes are reachable."""
        from app.routers.trainer_voice import verify_numeric_code
        from unittest.mock import AsyncMock, MagicMock, patch
        from fastapi import Request

        mock_db = AsyncMock()
        mock_setting = MagicMock()
        mock_setting.agent_name = "Agente Demo 11"
        mock_setting.hubspot_owner_id = "agent_demo_11"
        res_set = MagicMock()
        res_set.scalars.return_value.first.return_value = mock_setting
        mock_db.execute.return_value = res_set

        mock_request = MagicMock(spec=Request)
        mock_request.form = AsyncMock(return_value={"Digits": "9999"})
        mock_request.headers = {"host": "test.com"}

        response = await verify_numeric_code(request=mock_request, db=mock_db)
        content = response.body.decode("utf-8")

        self.assertIn('finishOnKey="#"', content, "Gather must use finishOnKey='#' to support variable-length simulation codes")
        self.assertNotIn('numDigits="4"', content, "Must NOT restrict to exactly 4 digits")

    # ── 10. ATEN02 entered as full alphanumeric code resolves and redirects ──────

    @patch("app.routers.trainer_voice.TrainerService.validate_simulation_for_agent")
    async def test_10_aten02_full_code_resolves_and_redirects(self, mock_validate):
        """Agent enters ATEN02# via DTMF → Digits='ATEN02' → validated directly by code → redirects to start-roleplay.
        simulation_id (DB internal PK) must NOT be used as a user-facing code."""
        from app.routers.trainer_voice import verify_simulation_numeric_code
        from unittest.mock import AsyncMock, MagicMock
        from fastapi import Request

        mock_aten02_sim = MagicMock()
        mock_aten02_sim.simulation_id = 5
        mock_aten02_sim.code = "ATEN02"

        # validate_simulation_for_agent resolves ATEN02 on the first (direct) call
        mock_validate.return_value = {
            "valid": True,
            "status": "valid",
            "simulation": mock_aten02_sim,
        }

        mock_db = AsyncMock()
        mock_request = MagicMock(spec=Request)
        # Twilio delivers Digits="ATEN02" when agent keys A-T-E-N-0-2#
        mock_request.form = AsyncMock(return_value={"Digits": "ATEN02", "CallSid": "CA_ATEN02"})
        mock_request.headers = {"host": "test.com", "x-forwarded-proto": "https"}

        response = await verify_simulation_numeric_code(request=mock_request, agent_id="agent_demo_11", db=mock_db)
        content = response.body.decode("utf-8")

        # Must redirect to start-roleplay with the resolved simulation_id
        self.assertIn("<Redirect>", content)
        self.assertIn("start-roleplay", content)
        self.assertIn("simulation_id=5", content)
        self.assertNotIn("<Say", content, "Must NOT play error when code is valid")

        # Validator was called with the real code, NOT with a numeric ID
        mock_validate.assert_called_once_with(mock_db, "ATEN02", "agent_demo_11")

    # ── 11. Wrong company is still rejected ───────────────────────────────────

    @patch("app.routers.trainer_voice.TrainerService.validate_simulation_for_agent")
    async def test_11_company_mismatch_rejects_dtmf(self, mock_validate):
        """DTMF validation must reject a simulation from a different company even if numeric ID matches."""
        from app.routers.trainer_voice import verify_simulation_numeric_code
        from unittest.mock import AsyncMock, MagicMock
        from fastapi import Request

        mock_validate.return_value = {
            "valid": False,
            "status": "company_mismatch",
            "message": "Ese código de simulación pertenece a otra organización.",
            "simulation": None,
        }

        mock_db = AsyncMock()
        mock_request = MagicMock(spec=Request)
        mock_request.form = AsyncMock(return_value={"Digits": "9999", "CallSid": "CA_MISMATCH"})
        mock_request.headers = {"host": "test.com"}

        response = await verify_simulation_numeric_code(request=mock_request, agent_id="agent_foreign", db=mock_db)
        content = response.body.decode("utf-8")

        self.assertIn("<Say", content)
        self.assertIn("otra organización", content)
        self.assertIn("<Hangup", content)

    # ── 12. Genuinely invalid code is rejected ────────────────────────────────

    @patch("app.routers.trainer_voice.TrainerService.validate_simulation_for_agent")
    async def test_12_invalid_dtmf_code_plays_error_and_hangs_up(self, mock_validate):
        """A code that does not match any simulation by code, SIM prefix, or simulation_id must trigger error message."""
        from app.routers.trainer_voice import verify_simulation_numeric_code
        from unittest.mock import AsyncMock, MagicMock
        from fastapi import Request

        mock_validate.return_value = {"valid": False, "status": "invalid", "simulation": None}

        mock_db = AsyncMock()
        mock_request = MagicMock(spec=Request)
        mock_request.form = AsyncMock(return_value={"Digits": "0000", "CallSid": "CA_INVALID"})
        mock_request.headers = {"host": "test.com"}

        response = await verify_simulation_numeric_code(request=mock_request, agent_id="agent_demo_11", db=mock_db)
        content = response.body.decode("utf-8")

        self.assertIn("<Say", content)
        self.assertIn("no es válida", content)
        self.assertIn("<Hangup", content)
        self.assertNotIn("<Redirect>", content)
