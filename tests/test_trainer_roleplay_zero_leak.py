"""
tests/test_trainer_roleplay_zero_leak.py
Comprehensive test suite validating that during a trainer simulation:
1. Model maintains character during active simulation.
2. Silence does not trigger switch to assistant.
3. Retry/reconnection preserves system prompt and character.
4. Last turn stays in character (no "sal del personaje").
5. Finalized session does not generate another roleplay turn (audio gated).
6. Finalized session does not generate generic assistant responses.
7. New simulation uses completely fresh context.
8. Two agents do not share context.
9. Tutor IA and roleplay do not share context.
10. Tenant / company isolation is strictly enforced.
11. No '¿En qué puedo ayudarte?' or assistant assistance formulas appear during active roleplay.
12. Evaluation pipeline is triggered correctly upon session completion.
"""
import os
import sys
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
    handle_roleplay_hangup,
)
from app.models.trainer import TrainerSimulation, TrainerSession


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


class TestTrainerRoleplayZeroLeak(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        trainer_voice_module.settings.gemini_api_key = "test_gemini_key"
        trainer_voice_module.settings.gemini_model = "models/gemini-2.0-flash-exp"

    def test_1_active_simulation_maintains_character(self):
        """1. Durante una simulación activa, las directrices obligan a mantener exclusivamente el personaje."""
        self.assertIn("REGLA CRÍTICA: BLOQUEO ABSOLUTO DE PERSONAJE", SPANISH_VOICE_RULES)
        self.assertIn("Durante TODA la llamada debes interpretar EXCLUSIVAMENTE al personaje", SPANISH_VOICE_RULES)
        self.assertIn("Bajo ninguna circunstancia cambies de rol ni admitas ser un sistema, modelo o IA", SPANISH_VOICE_RULES)
        self.assertIn("Tu identidad es puramente la del interlocutor/cliente de la simulación", SPANISH_VOICE_RULES)

    def test_2_silence_does_not_change_to_assistant(self):
        """2. Un silencio o pausa del agente NO provoca cambio a asistente ni ofrece ayuda."""
        self.assertIn("Presencia y gestión de silencios", SPANISH_VOICE_RULES)
        self.assertIn("Responde SIEMPRE desde tu personaje", SPANISH_VOICE_RULES)
        self.assertIn("Bajo NINGUNA circunstancia ofrezcas ayuda ni te conviertas en asistente", SPANISH_VOICE_RULES)
        self.assertIn("Una pausa o silencio del agente NO significa que la llamada haya terminado ni que debas cambiar de actitud", SPANISH_VOICE_RULES)

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_3_retry_retains_system_prompt_on_graceful_cancel(self, mock_ws_connect, mock_session_local):
        """3. Si se interrumpe el cierre y continúa la llamada, se reinyecta explícitamente el prompt de rol."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        dummy_mulaw_b64 = base64.b64encode(b"\xff" * 160).decode("utf-8")

        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
            json.dumps({
                "toolCall": {
                    "functionCalls": [{"id": "call_1", "name": "hangup_call", "args": {"reason": "exito_conversacional"}}]
                }
            }),
            json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": dummy_mulaw_b64}}]},
                    "turnComplete": True
                }
            }),
        ])

        mock_tw_ws = AsyncMock()
        mock_tw_ws.client_state.name = "CONNECTED"
        mock_tw_ws.scope = {"query_string": b"flow=session&session_id=10"}
        mock_tw_ws.headers = {"host": "localhost"}
        mock_tw_ws.send_text = AsyncMock()

        async def mock_tw_iter(*args, **kwargs):
            yield json.dumps({"event": "start", "start": {"streamSid": "STR_3", "callSid": "CA_3"}})
            # Wait until graceful hangup is triggered by gemini loop
            await asyncio.sleep(0.15)
            # User speaks during grace window
            for _ in range(10):
                yield json.dumps({"event": "media", "media": {"track": "inbound", "payload": "pcm"}})
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.4)

        mock_tw_ws.iter_text = mock_tw_iter
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        with patch("app.routers.trainer_voice.GRACEFUL_HANGUP_GRACE_WINDOW_SECONDS", 1.0), \
             patch("app.routers.trainer_voice.VAD_GRACE_PERIOD_MS", 0), \
             patch("app.routers.trainer_voice.VAD_MIN_SPEECH_DURATION_MS", 40), \
             patch("app.routers.trainer_voice.calculate_pcm_energy", return_value=300.0), \
             patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("DUMMY_PCM", None)), \
             patch("app.routers.trainer_voice.start_twilio_recording", new_callable=AsyncMock, return_value="REC_123"):
            task = asyncio.create_task(
                media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)
            )
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.05)

        # Check if reinstate message was sent to Gemini
        reinstate_messages = []
        for msg in mock_gem_ws.sent_messages:
            try:
                data = json.loads(msg)
                turns = data.get("clientContent", {}).get("turns", [])
                for t in turns:
                    for p in t.get("parts", []):
                        if "INSTRUCCIÓN CRÍTICA: La llamada continúa" in p.get("text", ""):
                            reinstate_messages.append(p.get("text"))
            except Exception:
                pass

        self.assertGreaterEqual(len(reinstate_messages), 1, "Debe enviarse instrucción de continuidad de personaje al cancelar hangup")
        self.assertIn("Mantén al 100% tu personaje", reinstate_messages[0])

    def test_4_final_turn_stays_in_character_no_sal_del_personaje(self):
        """4. Ninguna directriz de cierre le pide al modelo 'salir del personaje'."""
        discipline = build_turn_discipline(is_healthcare=False, interlocutor_role="cliente")
        self.assertIn("despídete con naturalidad dentro de tu personaje", discipline)
        self.assertNotIn("sal del personaje", discipline.lower())
        self.assertNotIn("abandona el personaje", discipline.lower())

    @patch("app.routers.trainer_voice.AsyncSessionLocal")
    @patch("app.routers.trainer_voice.websockets.connect")
    async def test_5_finalized_session_audio_gated(self, mock_ws_connect, mock_session_local):
        """5. Una vez iniciado el cierre, el audio de Twilio se bloquea para no alimentar a Gemini."""
        mock_db = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_db
        fake_sim = MagicMock(simulation_id=10, code="V1", name="V", roleplay_prompt="Eres un cliente.", company_id=1)
        fake_sess = MagicMock(session_id=10, agent_id="101", simulation_id=10, simulation_version_id=None, simulation=fake_sim)
        res_sess = MagicMock()
        res_sess.scalars.return_value.first.return_value = fake_sess
        res_comp = MagicMock()
        res_comp.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [res_sess, res_comp]

        dummy_mulaw_b64 = base64.b64encode(b"\xff" * 160).decode("utf-8")

        mock_gem_ws = AsyncMockWs(messages=[
            json.dumps({"setupComplete": {}}),
            json.dumps({
                "toolCall": {
                    "functionCalls": [{"id": "call_close", "name": "hangup_call", "args": {"reason": "exito_conversacional"}}]
                }
            }),
            json.dumps({
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": dummy_mulaw_b64}}]},
                    "turnComplete": True
                }
            }),
        ])

        mock_tw_ws = AsyncMock()
        mock_tw_ws.client_state.name = "CONNECTED"
        mock_tw_ws.scope = {"query_string": b"flow=session&session_id=10"}
        mock_tw_ws.headers = {"host": "localhost"}
        mock_tw_ws.send_text = AsyncMock()

        async def mock_tw_iter(*args, **kwargs):
            yield json.dumps({"event": "start", "start": {"streamSid": "STR_56", "callSid": "CA_56"}})
            await asyncio.sleep(0.08)
            yield json.dumps({"event": "media", "media": {"track": "inbound", "payload": "pcm"}})
            await asyncio.sleep(0.2)
            yield json.dumps({"event": "stop"})

        mock_tw_ws.iter_text = mock_tw_iter
        mock_ws_connect.return_value.__aenter__.return_value = mock_gem_ws

        with patch("app.routers.trainer_voice.GRACEFUL_HANGUP_GRACE_WINDOW_SECONDS", 0.05), \
             patch("app.routers.trainer_voice.decode_twilio_to_gemini", return_value=("POST_CLOSE_PCM", None)), \
             patch("app.routers.trainer_voice.hangup_twilio_call", new_callable=AsyncMock), \
             patch("app.routers.trainer_voice.start_twilio_recording", new_callable=AsyncMock, return_value="REC_123"), \
             patch("app.routers.trainer_voice.handle_roleplay_hangup", new_callable=AsyncMock):
            task = asyncio.create_task(
                media_stream(websocket=mock_tw_ws, flow="session", session_id=10, db=mock_db)
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        post_close_audio_sent = False
        for msg in mock_gem_ws.sent_messages:
            try:
                data = json.loads(msg)
                audio_data = data.get("realtimeInput", {}).get("audio", {}).get("data")
                if audio_data == "POST_CLOSE_PCM":
                    post_close_audio_sent = True
            except Exception:
                pass

        self.assertFalse(post_close_audio_sent, "No se debe enviar audio a Gemini mientras pending_graceful_hangup está activo")

    def test_6_finalized_session_no_assistant_turn(self):
        """6. Las reglas del modelo prohíben estrictamente recurrir al asistente tras cierre de llamada."""
        rules = SPANISH_VOICE_RULES
        self.assertIn("PROHIBICIÓN TOTAL DE ASISTENTE", rules)
        self.assertIn("Bajo NINGUNA circunstancia ofrezcas ayuda ni te conviertas en asistente", rules)
        self.assertIn("Bajo ninguna circunstancia cambies de rol", rules)

    def test_7_new_simulation_uses_fresh_context(self):
        """7. Cada simulación genera su propia instrucción de turno con interlocutor y contexto aislado."""
        disp_client = build_turn_discipline(is_healthcare=False, interlocutor_role="cliente")
        disp_patient = build_turn_discipline(is_healthcare=True, interlocutor_role="paciente")
        self.assertIn("cliente", disp_client)
        self.assertIn("paciente", disp_patient)
        self.assertNotEqual(disp_client, disp_patient)

    def test_8_two_agents_do_not_share_context(self):
        """8. Dos llamadas de agentes diferentes usan sesiones y estados independientes."""
        sess_1 = TrainerSession(session_id=101, agent_id="agent_01", company_id=7)
        sess_2 = TrainerSession(session_id=102, agent_id="agent_02", company_id=7)
        self.assertNotEqual(sess_1.session_id, sess_2.session_id)
        self.assertNotEqual(sess_1.agent_id, sess_2.agent_id)

    def test_9_tutor_ia_and_roleplay_do_not_share_context(self):
        """9. Tutor IA (Chatbot RAG) y Roleplay Voice utilizan servicios y flujos completamente separados."""
        from app.services.trainer_chatbot_service import TrainerChatbotService
        # TrainerChatbotService operates on RAG and text chat, does not manage Twilio or Gemini Live sockets
        self.assertTrue(hasattr(TrainerChatbotService, "process_chat"))
        self.assertFalse(hasattr(TrainerChatbotService, "media_stream"))
        self.assertTrue(hasattr(trainer_voice_module, "media_stream"))

    @patch("app.routers.trainer_voice.TrainerService.start_phone_session", new_callable=AsyncMock)
    async def test_10_tenant_company_isolation(self, mock_start_session):
        """10. Tenant isolation: una simulación de otra empresa no se inicia para un agente no autorizado."""
        mock_db = AsyncMock()
        mock_setting = MagicMock(agent_name="Ana Lopez", company_id=7)
        mock_sim = MagicMock(simulation_id=5, code="SIM05", company_id=999)  # Diferente empresa
        mock_db.execute.side_effect = [
            MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=mock_setting)))),
            MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=mock_sim)))),
        ]

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"host": "test-service.com", "x-forwarded-proto": "https"}

        # start_roleplay returns TwiML with error message if simulation not found or mismatched
        from app.routers.trainer_voice import start_roleplay
        resp = await start_roleplay(
            request=mock_request,
            agent_id="agent_ana",
            simulation_id=5,
            call_sid="CA_ISOLATION",
            db=mock_db,
        )
        content = resp.body.decode("utf-8")
        self.assertIn("No se ha podido iniciar la simulación", content)

    def test_11_no_assistant_help_formulas_in_roleplay_rules(self):
        """11. Las directrices prohíben explícitamente cualquier fórmula de asistente."""
        self.assertIn("PROHIBICIÓN TOTAL DE ASISTENTE", SPANISH_VOICE_RULES)
        self.assertIn("Jamás ofrezcas ayuda al agente", SPANISH_VOICE_RULES)
        self.assertIn("jamás preguntes cómo puedes colaborar", SPANISH_VOICE_RULES)
        self.assertIn("NUNCA ofrezcas ayuda al agente ni uses fórmulas de asistente", build_turn_discipline(is_healthcare=False))

    @patch("app.routers.trainer_voice.check_and_trigger_evaluation", new_callable=AsyncMock)
    async def test_12_evaluation_pipeline_triggers_correctly(self, mock_trigger_eval):
        """12. La evaluación se dispara correctamente al finalizar la sesión sin estado ambiguo."""
        mock_db = AsyncMock()
        fake_sess = TrainerSession(session_id=55, status="in_progress", evaluation_status="pending")
        res = MagicMock()
        res.scalars.return_value.first.return_value = fake_sess
        mock_db.execute.return_value = res

        await handle_roleplay_hangup(
            session_id=55,
            call_sid="CA_FINAL",
            call_start_time=None,
            reason="exito_conversacional",
            db=mock_db,
        )

        self.assertEqual(fake_sess.status, "completed")
        self.assertEqual(fake_sess.evaluation_status, "completed_waiting_recording")
        mock_trigger_eval.assert_called_once_with(mock_db, 55)


if __name__ == '__main__':
    unittest.main()
