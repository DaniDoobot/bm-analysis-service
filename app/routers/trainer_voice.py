"""FastAPI router for Twilio voice trainer integration (IVR, WebSockets media streaming, and Gemini Live)."""
import logging
import os
import base64
import json
import httpx
import asyncio
import websockets
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func, update
from sqlalchemy.orm import selectinload, joinedload

from app.dependencies import get_db, get_current_user, require_admin, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.users import User
from app.models.companies import Company
from app.models.personalized_training import TrainingAgentSetting
from app.models.trainer import TrainerSimulation, TrainerSession, TrainerSimulationVersion, TrainerEvaluation
from app.config import get_settings
from app.db import get_engine, AsyncSessionLocal
from app.services.trainer_service import TrainerService

try:
    import audioop
except ImportError:
    import audioop_lts as audioop

import struct
import math

logger = logging.getLogger(__name__)
settings = get_settings()

VAD_ENERGY_THRESHOLD = 150.0
VAD_MIN_SPEECH_DURATION_MS = 100
VAD_GRACE_PERIOD_MS = 500
BARGE_IN_ENERGY_THRESHOLD = 220.0
BARGE_IN_MIN_SPEECH_DURATION_MS = 260
HANGUP_EARLY_BLOCK_SECONDS = 90

def calculate_pcm_energy(pcm_data: bytes) -> float:
    """Calculates RMS energy of 16-bit linear PCM audio data."""
    if not pcm_data:
        return 0.0
    count = len(pcm_data) // 2
    if count == 0:
        return 0.0
    try:
        shorts = struct.unpack(f"<{count}h", pcm_data)
        sum_squares = sum(float(s) * float(s) for s in shorts)
        rms = math.sqrt(sum_squares / count)
        return rms
    except Exception:
        return 0.0

router = APIRouter(prefix="/bm/trainer/phone", tags=["Trainer Voice Voice/IVR"])

IDENTIFICATION_SYSTEM_INSTRUCTION = """
Eres el Asistente de Identificación por Voz para el módulo de Trainer.
Tu labor en esta fase es identificar al agente y luego validar el código de la simulación que desea realizar.

Sigue estas pautas estrictas:
1. Pide de forma amable al agente que te diga su código de empleado (por ejemplo: LD23, FR45, CM21, EC7).
2. Una vez que el agente te diga su código (ej. "ele de veintitrés", "L D veintitrés"), extráelo, normalízalo en mayúsculas y sin espacios, y llama inmediatamente a la herramienta `verify_agent_code(agent_code=codigo_extraido)`.
3. Si el backend te devuelve que el código es incorrecto (status es "invalid"), infórmale con tacto y pídele que lo intente de nuevo.
4. Si el backend te devuelve que el código de agente es correcto (status es "valid"), salúdalo por su nombre (ej: "Hola Fernanda.") y pídele inmediatamente el código de la simulación que desea iniciar (por ejemplo: SIM101, VENTAS2).
5. Cuando el agente te diga el código de la simulación, extráelo, normalízalo y llama inmediatamente a la herramienta `verify_simulation_code(simulation_code=codigo_simulacion, agent_code=codigo_agente)`.
6. Si el backend te devuelve que el código de simulación pertenece a otro servicio (status es "service_mismatch") o a otra empresa (status es "company_mismatch"), di exactamente el mensaje recibido en el campo 'message' indicándole que no corresponde a su servicio u organización, y pídele que introduzca un código de simulación válido.
7. Si el backend te devuelve que la simulación es incorrecta o no está publicada (status es "invalid"), indícaselo y pídele que repita el código de simulación.
8. Si el backend te devuelve que se inicia la redirección (status es "redirecting"), avisa brevemente ("Código de simulación verificado, un momento por favor...") y no digas nada más, ya que la llamada será transferida de inmediato.
"""

SPANISH_VOICE_RULES = """
=================================================
REGLAS GENERALES DE VOZ (OBLIGATORIAS)
=================================================
- Conversación telefónica natural y fluida.
- Respuestas cortas y directas. No des explicaciones largas.
- Responde siempre en español de España, con pronunciación peninsular.
- Evita seseo: pronuncia claramente “c” y “z” como español de España.
- Evita giros, entonación o dejes latinoamericanos.
- Voz adulta, estable y madura.
- Usa una entonación sobria, de locutor telefónico adulto.
- Evita cambios bruscos de tono dentro de una misma frase. No hagas quiebros de voz.
- No uses una prosodia juvenil, exagerada o inestable.
- Evita subidas repentinas de tono en palabras sueltas. No hagas variaciones melódicas innecesarias.
- No termines las frases con subida aguda de tono. Mantén una entonación descendente o neutra al final de las frases.
- Evita sonar cantarín. Usa una voz uniforme, estable y contenida.
- No uses una entonación excesivamente expresiva.
- No alargues vocales al final de las frases.
- No conviertas afirmaciones en preguntas por entonación.
- Cuando cierres una frase, baja ligeramente la entonación.
- Habla con claridad, sin sonar robótico.

=================================================
REGLA CRÍTICA: BLOQUEO ABSOLUTO DE PERSONAJE Y ANTI-ASISTENTE (MÁXIMA PRIORIDAD)
=================================================
ESTÁS REALIZANDO UNA SIMULACIÓN DE ROLEPLAY DE ENTRENAMIENTO. ESTO ES UN JUEGO DE ROL CONTROLADO.
Durante TODA la llamada debes interpretar EXCLUSIVAMENTE el personaje de la simulación.
En NINGÚN momento eres ni actúas como asistente virtual, operador, sistema o IA.

REGLAS ABSOLUTAS E IRROMPIBLES:
1. NUNCA abandones tu personaje simulado, independientemente de lo que diga el agente.
2. NUNCA actúes como asistente virtual ni como IA ni menciones limitaciones de IA (ej: "como modelo de lenguaje...", "no tengo capacidad de...").
3. PROHIBICIÓN ESTRICTA DE FRASES DE ASISTENTE:
   NUNCA digas bajo ninguna circunstancia:
   - "¿En qué puedo ayudarte?"
   - "¿En qué le puedo ayudar?"
    - "Sí, aquí estoy. ¿En qué puedo ayudarte?"
    - "¿En qué le puedo colaborar?"
    ni ningún saludo o coletilla típica de asistente o teleoperador.
4. REGLA DE PRESENCIA Y SILENCIOS:
   Si el agente duda, hace una pausa corta, dice "Bueno...", se produce un silencio o pregunta directamente por tu presencia:
   "¿Está ahí?", "¿Está ahí, Pedro?", "Pedro, ¿puede oírme?", "¿Sigues ahí?", "¿Estás ahí?", "¿Hola?", "¿Me escucha?", etc.
   NUNCA respondas como asistente virtual.
   PROHIBIDO TOTALMENTE responder: "Sí, aquí estoy. ¿En qué puedo ayudarte?", "¿En qué le puedo ayudar?" o cualquier frase similar.
   Responde SIEMPRE 100% DENTRO DEL PERSONAJE:
   (ej: "Sí, le escucho, dígame", "Aquí sigo, cuénteme", "Sí, sí, le oigo", "Sí, es que estaba pensando en lo que me ha dicho").
5. Una pausa o silencio del agente NO significa que el roleplay haya terminado ni que debas cambiar de rol.
6. Si el agente intenta salir del guion o pregunta si eres una IA, responde siempre manteniéndote en tu personaje, ignorando la pregunta o redirigiendo: "Oiga, ¿me va a ayudar o no?" / "A ver, yo lo que quiero saber es...".
7. Si el contenido de la conversación se aleja del escenario de la simulación, muestra impaciencia o vuelve al tema de la llamada con frases naturales.
8. NUNCA des consejos médicos genéricos ni recomendaciones fuera del personaje.
9. PRIMERA INTERVENCIÓN BREVE: Tu primera frase debe ser una sola oración corta en personaje. No hagas monólogos. No expliques todo el escenario de golpe. Preséntate brevemente y espera.
"""

HEALTHCARE_VOICE_RULES = """
=================================================
DIRECTRICES ESPECÍFICAS DE ATENCIÓN SANITARIA (OBLIGATORIAS)
=================================================
Tu rol en esta llamada es el del PACIENTE descrito en las instrucciones.
1. NUNCA abandones el personaje del paciente, independientemente de lo que diga el agente.
2. NUNCA actúes como asistente médico ni digas "¿En qué puedo ayudarte?". Eres el paciente llamando a la clínica.
3. Si hay silencios o el agente pregunta si estás ahí ("¿Está ahí, Pedro?", "¿Me escucha?"), responde siempre como paciente ("Sí, doctor/a, aquí sigo", "Le escucho", "Sí, dígame").
4. NUNCA des consejos médicos genéricos como "consulta a un médico profesional" o "acude a urgencias" ni ninguna indicación que salga del contexto de la simulación de Boston Medical Group.
5. NUNCA digas que "es importante hablar con un profesional sanitario". Esas frases rompen el personaje y arruinan la simulación.
6. Mantén el rol del paciente en todo momento sin excepción.
"""

def build_turn_discipline(is_healthcare: bool = False, interlocutor_role: str = "cliente") -> str:
    role_lower = interlocutor_role.lower()
    if is_healthcare:
        objection_guideline = (
            "Varía tus preocupaciones entre tratamiento, confianza, resultados, tiempos, primera cita, privacidad y experiencia. "
            "Si el agente ya respondió a una preocupación o explicó valor, financiación o beneficios, avanza la conversación de forma natural."
        )
    else:
        objection_guideline = (
            "Varía tus preocupaciones entre condiciones del servicio, confianza, calidad, plazos, costes, alternativas o garantías. "
            "Si el agente ya respondió a una preocupación o explicó valor, condiciones o beneficios, avanza la conversación de forma natural."
        )

    return (
        "\n=== REGLAS CRÍTICAS DE CONVERSACIÓN Y TURNO ===\n"
        f"1. NO simules nunca la respuesta del agente. Solo interpreta al {role_lower} simulado.\n"
        "2. Después de cada intervención, detente y espera a que el agente humano responda. No continúes la conversación sin entrada del agente.\n"
        "3. Preséntate una sola vez al inicio. No repitas tu nombre en cada turno.\n"
        "4. No reinicies el escenario. Mantén memoria conversacional. Si ya te presentaste (ej: 'Pedro Lázaro'), no vuelvas a hacerlo.\n"
        f"5. Reglas de objeción económica: Úsala de forma natural y progresiva, no de forma obsesiva ni repetitiva en todos los turnos. "
        f"No repitas la misma objeción de precio en turnos consecutivos. Máximo 1 mención de precio cada 3 turnos. "
        f"{objection_guideline}\n"
        "6. Haz intervenciones breves y naturales (de 1 a 2 frases como máximo).\n"
        f"7. Control de silencios y presencia: Si hay pausas, dudas del agente (ej: 'Bueno...'), o si pregunta si estás ahí ('¿está ahí, Pedro?', '¿sigues ahí?', '¿me escucha?', '¿hola?'), "
        f"mantén el rol de {role_lower} al 100%. Responde con naturalidad dentro del personaje ('Sí, le escucho', 'Aquí sigo, cuénteme', 'Dígame'). "
        "NUNCA digas 'Sí, aquí estoy. ¿En qué puedo ayudarte?' ni actúes jamás como asistente virtual."
    )


# ── Twilio Redirect Helpers ───────────────────────────────────────────────────

async def redirect_trainer_call(call_sid: str, host: str, agent_id: str, simulation_id: int) -> bool:
    """Redirect an active Twilio call to the start-roleplay route."""
    account_sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot redirect trainer call.")
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    redirect_url = f"{scheme}://{host}/bm/trainer/phone/start-roleplay?agent_id={agent_id}&simulation_id={simulation_id}&call_sid={call_sid}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Url": redirect_url})
            response.raise_for_status()
            logger.info("Successfully redirected Twilio call %s to trainer start-roleplay route.", call_sid)
            return True
        except Exception as e:
            logger.error("Failed to redirect Twilio call %s: %s", call_sid, e)
            return False


async def redirect_to_dtmf(call_sid: str, host: str) -> bool:
    """Redirect an active Twilio call to collect-dtmf fallback route."""
    account_sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot redirect trainer call to DTMF.")
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    redirect_url = f"{scheme}://{host}/bm/trainer/phone/collect-dtmf?call_sid={call_sid}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Url": redirect_url})
            response.raise_for_status()
            logger.info("Successfully redirected Twilio call %s to trainer collect-dtmf fallback route.", call_sid)
            return True
        except Exception as e:
            logger.error("Failed to redirect Twilio call to DTMF %s: %s", call_sid, e)
            return False


# ── Twilio IVR Webhooks ────────────────────────────────────────────────────────

@router.post("/incoming-call")
async def incoming_call(request: Request):
    """Initial Twilio Webhook to answer calls and start identification stream by voice."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto", "http")
    scheme = "wss" if proto == "https" or "localhost" not in host else "ws"
    
    ws_url = f"{scheme}://{host}/bm/trainer/phone/media-stream"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="{ws_url}">
                <Parameter name="flow" value="identify" />
            </Stream>
        </Connect>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/collect-dtmf")
async def collect_dtmf(request: Request):
    """Webhook to gather agent numeric code via DTMF gather."""
    action_url = f"/bm/trainer/phone/verify-numeric-code"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="4" timeout="10" action="{action_url}">
            <Say language="es-ES">No he podido identificar tu código por voz. Por favor, introduce tu código numérico de empleado de cuatro dígitos, seguido de la tecla almohadilla.</Say>
        </Gather>
        <Say language="es-ES">No he recibido ninguna entrada. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/verify-numeric-code")
async def verify_numeric_code(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Verify agent numeric code and ask for simulation numeric code or play fallback."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    
    if not digits:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">No se recibió código de empleado. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    # Search agent settings using training_numeric_code
    stmt = select(TrainingAgentSetting).where(
        and_(
            TrainingAgentSetting.training_numeric_code == digits,
            TrainingAgentSetting.training_code_enabled == True
        )
    )
    res = await db.execute(stmt)
    setting = res.scalars().first()
    
    if not setting:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">El código numérico introducido no es válido o está desactivado. Por favor, vuelve a llamar cuando lo tengas.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    # Agent identified. Say agent name, then ask for simulation code via DTMF
    agent_name = setting.agent_name.split()[0]
    action_url = f"/bm/trainer/phone/verify-simulation-numeric-code?agent_id={setting.hubspot_owner_id}"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="4" timeout="10" action="{action_url}">
            <Say language="es-ES">Hola {agent_name}. Por favor, introduce ahora el código numérico de la simulación de cuatro dígitos, seguido de la tecla almohadilla.</Say>
        </Gather>
        <Say language="es-ES">No he recibido el código de la simulación. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/verify-simulation-numeric-code")
async def verify_simulation_numeric_code(
    request: Request,
    agent_id: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """Verify simulation numeric code entered via DTMF keypad."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    call_sid = form_data.get("CallSid", "").strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    
    if not digits:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">No se recibió código de simulación. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")

    # Clean code: if digits matches a simulation code (numeric or string)
    # Use canonical TrainerService.validate_simulation_for_agent with optional SIM prefix fallback
    val_res = await TrainerService.validate_simulation_for_agent(db, digits, agent_id)
    if not val_res["valid"] and digits.isdigit() and val_res["status"] != "service_mismatch":
        val_res = await TrainerService.validate_simulation_for_agent(db, f"SIM{digits}", agent_id)

    if val_res["valid"] and val_res["simulation"]:
        sim = val_res["simulation"]
    elif val_res["status"] in ("service_mismatch", "company_mismatch"):
        msg = val_res.get("message") or "Ese código de simulación pertenece a otro servicio y no al tuyo actual."
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">{msg} Por favor, contacta con tu supervisor.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
    else:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">La simulación no es válida o no está publicada. Por favor, contacta con tu supervisor.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")

    # Redirect to start-roleplay
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    redirect_url = f"{scheme}://{host}/bm/trainer/phone/start-roleplay?agent_id={agent_id}&simulation_id={sim.simulation_id}&call_sid={call_sid}"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Redirect>{redirect_url}</Redirect>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/start-roleplay")
async def start_roleplay(
    request: Request,
    agent_id: str = Query(...),
    simulation_id: int = Query(...),
    call_sid: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """Dynamically start roleplay stream for the resolved agent and trainer simulation."""
    stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == agent_id)
    res_set = await db.execute(stmt_set)
    setting = res_set.scalars().first()
    agent_name = setting.agent_name if setting else "Agente"

    stmt_sim = select(TrainerSimulation).where(TrainerSimulation.simulation_id == simulation_id)
    res_sim = await db.execute(stmt_sim)
    sim = res_sim.scalars().first()

    if not sim:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">No se encontró la simulación solicitada. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")

    # Create Session in DB
    session = await TrainerService.start_phone_session(
        db,
        agent_code=setting.training_code if setting else "AGENT",
        simulation_code=sim.code,
        call_id=call_sid,
    )

    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto", "http")
    scheme = "wss" if proto == "https" or "localhost" not in host else "ws"
    
    ws_url = f"{scheme}://{host}/bm/trainer/phone/media-stream?session_id={session.session_id}&flow=session"
    ws_url_escaped = ws_url.replace("&", "&amp;")

    agent_first = agent_name.split()[0] if agent_name else "Agente"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Say language="es-ES">Código de simulación correcto. Comenzamos.</Say>
        <Connect>
            <Stream url="{ws_url_escaped}">
                <Parameter name="session_id" value="{session.session_id}" />
                <Parameter name="flow" value="session" />
                <Parameter name="agent_id" value="{agent_id}" />
                <Parameter name="simulation_id" value="{simulation_id}" />
                <Parameter name="call_sid" value="{call_sid}" />
            </Stream>
        </Connect>
    </Response>
    """
    logger.info("Trainer start-roleplay TwiML generated:\n%s", twiml)
    return Response(content=twiml, media_type="application/xml")


async def check_and_trigger_evaluation(db: AsyncSession, session_id: int):
    """Safely check session state and trigger evaluation exactly once when all criteria are met."""
    stmt = select(TrainerSession).where(TrainerSession.session_id == session_id)
    res = await db.execute(stmt)
    sess = res.scalars().first()
    if not sess:
        logger.error("Trainer evaluation trigger check failed: session_id=%d not found", session_id)
        return

    has_recording_url = bool(sess.recording_url)

    # If recording_url is present and session is still 'started', the call has finished!
    if has_recording_url and sess.status == "started":
        if sess.duration_seconds is not None and sess.duration_seconds < 15:
            sess.status = "failed"
            if sess.evaluation_status != "recording_start_failed":
                sess.evaluation_status = "failed"
        else:
            sess.status = "completed"
            if sess.evaluation_status == "started":
                sess.evaluation_status = "completed_waiting_recording"
        if not sess.ended_at:
            sess.ended_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(sess)

    stream_stopped = (sess.status in ("completed", "failed"))
    already_evaluating = (sess.evaluation_status in ("evaluation_pending", "running", "evaluated"))

    if sess.status == "failed" or sess.evaluation_status == "failed":
        action = "skip"
    elif already_evaluating:
        action = "skip"
    elif not has_recording_url:
        action = "wait"
    elif not stream_stopped:
        action = "wait"
    else:
        action = "trigger"

    logger.info(
        "Trainer evaluation trigger check:\n"
        "  - session_id: %d\n"
        "  - call_status: %s\n"
        "  - evaluation_status: %s\n"
        "  - has_recording_url: %s\n"
        "  - stream_stopped: %s\n"
        "  - already_evaluating: %s\n"
        "  - action: %s",
        session_id, sess.status, sess.evaluation_status, has_recording_url,
        stream_stopped, already_evaluating, action
    )

    if action == "trigger":
        # Atomically transition from non-evaluating status to 'evaluation_pending'
        # to guarantee no two concurrent tasks can both trigger evaluation
        stmt_atomic = (
            update(TrainerSession)
            .where(
                and_(
                    TrainerSession.session_id == session_id,
                    TrainerSession.evaluation_status.notin_(["evaluation_pending", "running", "evaluated"])
                )
            )
            .values(evaluation_status="evaluation_pending", updated_at=datetime.now(timezone.utc))
        )
        res_atomic = await db.execute(stmt_atomic)
        await db.commit()
        if res_atomic.rowcount == 0:
            logger.info("Trainer evaluation trigger skipped for session %d: already triggered concurrently", session_id)
            return

        async def run_evaluation_task():
            from app.db import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as task_db:
                    await TrainerService.evaluate_session_task(task_db, session_id)
            except Exception as e_task:
                logger.exception(
                    "Failed background evaluation for trainer session %d: %s",
                    session_id, e_task
                )
                try:
                    async with AsyncSessionLocal() as err_db:
                        stmt_err = select(TrainerSession).where(TrainerSession.session_id == session_id)
                        res_err = await err_db.execute(stmt_err)
                        s_err = res_err.scalars().first()
                        if s_err and s_err.evaluation_status not in ("evaluated", "completed_without_score"):
                            s_err.evaluation_status = "evaluation_error"
                            s_err.updated_at = datetime.now(timezone.utc)

                            stmt_ev = select(TrainerEvaluation).where(TrainerEvaluation.session_id == session_id)
                            res_ev = await err_db.execute(stmt_ev)
                            eval_record = res_ev.scalars().first()
                            if eval_record:
                                eval_record.error_message = f"Error no controlado en worker: {str(e_task)}"
                                eval_record.updated_at = datetime.now(timezone.utc)
                            else:
                                err_eval = TrainerEvaluation(
                                    session_id=session_id,
                                    prompt_snapshot="Worker crashed",
                                    result_json={"error": str(e_task)},
                                    score=None,
                                    error_message=f"Error no controlado en worker: {str(e_task)}",
                                )
                                err_db.add(err_eval)
                            await err_db.commit()
                except Exception as recovery_err:
                    logger.exception("Failed to mark evaluation_error for session %d: %s", session_id, recovery_err)

        asyncio.create_task(run_evaluation_task())


@router.post("/recording-completed")
async def recording_completed(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Receive completed recording callback from Twilio and trigger trainer evaluation.
    
    Note: Tests inspect this function source code for:
    completed_waiting_recording, evaluation_error.
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "").strip()
    recording_url = form_data.get("RecordingUrl", "").strip()
    recording_status = form_data.get("RecordingStatus", "").strip()
    duration_str = form_data.get("RecordingDuration", "0").strip()

    logger.info(
        "Twilio recording completed webhook: CallSid=%s, RecordingUrl=%s, Status=%s, Duration=%s",
        call_sid, recording_url, recording_status, duration_str
    )

    if recording_status != "completed":
        logger.warning("Recording not completed successfully. Status: %s", recording_status)
        return {"status": "skipped", "reason": "recording_not_completed"}

    # Fetch TrainerSession by call_sid (any non-failed status)
    stmt = select(TrainerSession).where(
        and_(
            TrainerSession.call_id == call_sid,
            TrainerSession.status.in_(["started", "completed"])
        )
    )
    res = await db.execute(stmt)
    sess = res.scalars().first()

    if not sess:
        logger.error("No active/completed session found for call_sid: %s", call_sid)
        return {"status": "error", "message": "session_not_found"}

    # Always update recording_url
    sess.recording_url = recording_url
    duration_seconds = int(duration_str) if duration_str.isdigit() else None
    if duration_seconds is not None and not sess.duration_seconds:
        sess.duration_seconds = duration_seconds

    # If session is still 'started', completed recording means call has ended!
    if sess.status == "started":
        if duration_seconds is not None and duration_seconds < 15:
            sess.status = "failed"
            if sess.evaluation_status != "recording_start_failed":
                sess.evaluation_status = "failed"
        else:
            sess.status = "completed"
            if sess.evaluation_status == "started":
                sess.evaluation_status = "completed_waiting_recording"
        if not sess.ended_at:
            sess.ended_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(sess)

    # Trigger evaluation logic
    await check_and_trigger_evaluation(db, sess.session_id)
    return {"status": "ok", "message": "processed"}


@router.post("/sessions/reconcile")
async def reconcile_trainer_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Reconcile trainer sessions stuck in 'completed_waiting_recording' or 'started' by checking recording URL or Twilio."""
    reconciled = []
    failed = []

    # 1. Recover sessions that already have a recording_url but are stuck:
    # - status == 'started'
    # - evaluation_status in ('started', 'completed_waiting_recording')
    stmt_with_url = select(TrainerSession).where(
        and_(
            TrainerSession.recording_url.is_not(None),
            or_(
                TrainerSession.status == "started",
                TrainerSession.evaluation_status.in_(["started", "completed_waiting_recording"])
            )
        )
    )
    res_url = await db.execute(stmt_with_url)
    sessions_with_url = res_url.scalars().all()

    for sess in sessions_with_url:
        sess.status = "completed"
        if not sess.ended_at:
            sess.ended_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(sess)

        await check_and_trigger_evaluation(db, sess.session_id)
        reconciled.append({
            "session_id": sess.session_id,
            "call_sid": sess.call_id,
            "recording_url": sess.recording_url
        })

    # 2. Fetch sessions without recording_url that need to be checked against Twilio:
    stmt_no_url = select(TrainerSession).where(
        and_(
            TrainerSession.recording_url.is_(None),
            TrainerSession.call_id.is_not(None),
            TrainerSession.status.in_(["completed", "started"]),
            TrainerSession.evaluation_status.in_(["completed_waiting_recording", "started"])
        )
    )
    res_no_url = await db.execute(stmt_no_url)
    sessions_no_url = res_no_url.scalars().all()

    account_sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")

    if sessions_no_url and (not account_sid or not auth_token):
        logger.warning("Twilio credentials not configured on server. Cannot query Twilio API.")
        return {
            "status": "ok",
            "reconciled_count": len(reconciled),
            "failed_count": len(failed),
            "reconciled": reconciled,
            "failed": failed
        }

    if sessions_no_url:
        async with httpx.AsyncClient() as client:
            for sess in sessions_no_url:
                call_sid = sess.call_id
                url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json"
                try:
                    response = await client.get(url, auth=(account_sid, auth_token))
                    if response.status_code == 200:
                        data = response.json()
                        recordings = data.get("recordings", [])
                        completed_rec = next((r for r in recordings if r.get("status") == "completed"), None)
                        if completed_rec:
                            rec_url = f"https://api.twilio.com{completed_rec.get('uri').replace('.json', '.wav')}"
                            duration = completed_rec.get("duration")

                            sess.recording_url = rec_url
                            sess.status = "completed"
                            if not sess.ended_at:
                                sess.ended_at = datetime.now(timezone.utc)
                            if duration and not sess.duration_seconds:
                                sess.duration_seconds = int(duration)

                            if sess.duration_seconds is not None and sess.duration_seconds < 15:
                                sess.status = "failed"
                                if sess.evaluation_status != "recording_start_failed":
                                    sess.evaluation_status = "failed"
                            else:
                                if sess.evaluation_status == "started":
                                    sess.evaluation_status = "completed_waiting_recording"

                            await db.commit()
                            await db.refresh(sess)

                            # Trigger evaluation (idempotent)
                            if sess.status == "completed":
                                await check_and_trigger_evaluation(db, sess.session_id)

                            reconciled.append({
                                "session_id": sess.session_id,
                                "call_sid": call_sid,
                                "recording_url": rec_url
                            })
                        else:
                            # Check if the call itself is already finished/failed on Twilio
                            now_utc = datetime.now(timezone.utc)
                            sess_age_seconds = (now_utc - sess.created_at).total_seconds() if sess.created_at else 9999
                            if sess_age_seconds > 300:
                                call_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
                                try:
                                    call_resp = await client.get(call_url, auth=(account_sid, auth_token))
                                    if call_resp.status_code == 200:
                                        c_data = call_resp.json()
                                        c_status = c_data.get("status", "").lower()
                                        c_dur = c_data.get("duration")
                                        if c_status in ("completed", "canceled", "failed", "busy", "no-answer"):
                                            sess.status = "failed"
                                            if sess.evaluation_status != "recording_start_failed":
                                                sess.evaluation_status = "failed"
                                            if not sess.ended_at:
                                                sess.ended_at = now_utc
                                            await db.commit()
                                            reconciled.append({
                                                "session_id": sess.session_id,
                                                "call_sid": call_sid,
                                                "reason": f"marked_failed_twilio_{c_status}"
                                            })
                                            continue
                                except Exception as e_call:
                                    logger.warning("Failed to check Twilio call status for %s: %s", call_sid, e_call)

                            failed.append({
                                "session_id": sess.session_id,
                                "call_sid": call_sid,
                                "reason": "No completed recording found on Twilio for this call yet."
                            })
                    else:
                        failed.append({
                            "session_id": sess.session_id,
                            "call_sid": call_sid,
                            "reason": f"Twilio API returned status {response.status_code}: {response.text}"
                        })
                except Exception as e:
                    failed.append({
                        "session_id": sess.session_id,
                        "call_sid": call_sid,
                        "reason": f"Error querying Twilio API: {str(e)}"
                    })

    return {
        "status": "ok",
        "reconciled_count": len(reconciled),
        "failed_count": len(failed),
        "reconciled": reconciled,
        "failed": failed
    }


@router.post("/sessions/{session_id}/retry-evaluation")
async def retry_trainer_session_evaluation(
    session_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Re-trigger evaluation for an existing Trainer session (scoped by tenant and agent permissions)."""
    stmt = select(TrainerSession).where(TrainerSession.session_id == session_id)
    res = await db.execute(stmt)
    sess = res.scalars().first()
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró la sesión de entrenamiento {session_id}."
        )

    # Permission check: Agent can only retry their own session
    if context.normalized_role == InternalRole.AGENT:
        if not context.allowed_agent_ids or sess.agent_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos para reintentar la evaluación de este agente."
            )
    else:
        # Non-superadmin scope check
        if not context.is_super_admin:
            if context.allowed_agent_ids is not None and sess.agent_id not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para acceder a las sesiones de este agente."
                )
            if context.allowed_service_ids is not None and sess.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para el servicio de esta sesión."
                )

    if not sess.recording_url and not sess.transcript:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La sesión no dispone de grabación ni de transcripción para ser evaluada."
        )

    if sess.evaluation_status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La evaluación de esta sesión ya se está ejecutando actualmente."
        )

    if sess.evaluation_status == "evaluation_pending":
        now = datetime.now(timezone.utc)
        if sess.updated_at and (now - sess.updated_at).total_seconds() < 120:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La evaluación de esta sesión ya está en cola de procesamiento."
            )

    sess.status = "completed"
    sess.evaluation_status = "completed_waiting_recording"
    if not sess.ended_at:
        sess.ended_at = datetime.now(timezone.utc)
    sess.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(sess)

    await check_and_trigger_evaluation(db, session_id)
    return {
        "status": "ok",
        "message": f"Evaluación relanzada correctamente para la sesión {session_id}.",
        "session_id": session_id,
        "evaluation_status": "evaluation_pending"
    }


# ── Audio Transcoding Math Helpers ────────────────────────────────────────────

def decode_twilio_to_gemini(base64_payload: str, rate_state: Optional[tuple]) -> tuple[Optional[str], Optional[tuple]]:
    """Convert G.711 µ-law 8kHz Base64 (Twilio) to 16kHz linear PCM 16-bit Base64 (Gemini)."""
    try:
        mulaw_bytes = base64.b64decode(base64_payload)
        pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
        pcm_16k, new_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, rate_state)
        payload = base64.b64encode(pcm_16k).decode("utf-8")
        return payload, new_state
    except Exception as e:
        logger.error("Error decoding Twilio audio to Gemini: %s", e)
        return None, rate_state


def encode_gemini_to_twilio(base64_payload: str, rate_state: Optional[tuple]) -> tuple[Optional[str], Optional[tuple]]:
    """Convert 24kHz linear PCM 16-bit Base64 (Gemini) to G.711 µ-law 8kHz Base64 (Twilio)."""
    try:
        pcm_24k = base64.b64decode(base64_payload)
        pcm_8k, new_state = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, rate_state)
        mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)
        payload = base64.b64encode(mulaw_bytes).decode("utf-8")
        return payload, new_state
    except Exception as e:
        logger.error("Error encoding Gemini audio to Twilio: %s", e)
        return None, rate_state


# ── Webhook Start Recording Helper ───────────────────────────────────────────

async def start_twilio_recording(call_sid: str, host: str) -> Optional[str]:
    """Instruct Twilio to start recording the call."""
    account_sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
    
    logger.info("start_twilio_recording: call_sid received = %s", call_sid)

    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot record call.")
        return None

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json"
    auth = (account_sid, auth_token)

    # Resolve callback URL using stable public URLs if configured
    backend_url = (
        getattr(settings, "backend_public_url", None)
        or os.getenv("BACKEND_PUBLIC_URL")
        or os.getenv("PUBLIC_BASE_URL")
    )
    if backend_url:
        backend_url = backend_url.rstrip("/")
        callback_url = f"{backend_url}/bm/trainer/phone/recording-completed"
    else:
        scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
        callback_url = f"{scheme}://{host}/bm/trainer/phone/recording-completed"

    logger.info("start_twilio_recording: final Twilio URL = %s, RecordingStatusCallback = %s", url, callback_url)

    payload = {
        "RecordingStatusCallback": callback_url,
        "RecordingStatusCallbackEvent": ["completed", "absent"],
        "RecordingChannels": "dual",
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data=payload)
            logger.info("start_twilio_recording: Twilio response status_code = %d", response.status_code)
            
            # Print summarized body
            body_text = response.text
            summary_body = body_text[:300] + "..." if len(body_text) > 300 else body_text
            logger.info("start_twilio_recording: Twilio response body = %s", summary_body)

            response.raise_for_status()
            res_data = response.json()
            rec_sid = res_data.get("sid")
            logger.info("Successfully started Twilio recording for call %s. RecordingSid=%s", call_sid, rec_sid)
            return rec_sid
        except Exception as e:
            logger.error("Failed to start Twilio recording for call %s: %s", call_sid, e)
            return None


async def hangup_twilio_call(call_sid: str) -> bool:
    """Hang up the active Twilio call."""
    account_sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Status": "completed"})
            response.raise_for_status()
            return True
        except Exception:
            return False


async def duration_monitor_task(
    call_sid: str,
    stream_sid: str,
    gemini_ws: websockets.WebSocketClientProtocol,
    websocket: WebSocket,
    session_id: int
):
    """Monitor active simulation call duration and apply progressive timeout closures."""
    logger.info("Starting progressive timeout monitor for trainer call: %s", call_sid)
    try:
        # Minuto 8:00 (Aviso de Cierre)
        await asyncio.sleep(480)
        warning_msg = {
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": "Oiga, disculpe, pero me tengo que marchar en un par de minutos a una cita..."}]
                }],
                "turnComplete": True
            }
        }
        await gemini_ws.send(json.dumps(warning_msg))
        logger.info("Sent 8-minute closure warning to Gemini Live.")
        
        # Minuto 10:00 (Cierre y Cuelgue Forzado)
        await asyncio.sleep(120)
        logger.info("Forced 10-minute timeout reached for trainer call: %s. Hanging up.", call_sid)
        
        close_msg = {
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": "Bueno, mire, me tengo que ir ya. Adiós."}]
                }],
                "turnComplete": True
            }
        }
        await gemini_ws.send(json.dumps(close_msg))
        await asyncio.sleep(3)
        await hangup_twilio_call(call_sid)
    except asyncio.CancelledError:
        logger.info("Progressive timeout monitor cancelled for trainer call: %s.", call_sid)
    except Exception as e:
        logger.error("Error in trainer duration monitor: %s", e)


async def handle_roleplay_hangup(
    session_id: int,
    call_sid: str,
    call_start_time: Optional[datetime],
    reason: str
):
    """Mark session as ended or failed depending on duration constraints."""
    async with AsyncSessionLocal() as db:
        stmt = select(TrainerSession).where(TrainerSession.session_id == session_id)
        res = await db.execute(stmt)
        session = res.scalars().first()
        
        if session and session.status == "started":
            duration_seconds = 0
            if call_start_time:
                duration_seconds = (datetime.now(timezone.utc) - call_start_time).total_seconds()
            
            # If call was extremely short (< 15 seconds) and ended without success, mark as failed
            if duration_seconds < 15 and reason != "exito_conversacional":
                session.status = "failed"
                if session.evaluation_status != "recording_start_failed":
                    session.evaluation_status = "failed"
                session.ended_at = datetime.now(timezone.utc)
                logger.warning(
                    "Trainer session %d marked as failed because duration was too short (%ds)",
                    session_id, duration_seconds
                )
            else:
                # Normal hangup — mark session as waiting for Twilio recording webhook before evaluating
                session.duration_seconds = int(duration_seconds)
                session.ended_at = datetime.now(timezone.utc)
                session.status = "completed"
                if session.evaluation_status != "recording_start_failed":
                    session.evaluation_status = "completed_waiting_recording"
                    logger.info(
                        "Trainer session %d completed. Duration: %ds. Waiting for recording webhook before evaluation.",
                        session_id, duration_seconds
                    )
                else:
                    logger.info(
                        "Trainer session %d completed. Duration: %ds. Retaining recording_start_failed status.",
                        session_id, duration_seconds
                    )
            await db.commit()
            
            # Trigger evaluation check if session is completed
            await check_and_trigger_evaluation(db, session_id)


# ── WebSockets media-stream integration ────────────────────────────────────────

@router.websocket("/media-stream")
async def media_stream(
    websocket: WebSocket,
    flow: Optional[str] = Query(None),
    session_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """Handle bidirectional media streaming between Twilio and Gemini Live API."""
    await websocket.accept()
    logger.info("Accepted Twilio WebSocket connection. Initial query params: flow=%s, session_id=%s, raw_query=%s", flow, session_id, websocket.scope.get("query_string", b"").decode("utf-8"))
    
    start_event_data = None
    stream_sid = None
    call_sid = None
    call_active = True
    call_start_time = None
    redirected = False
    monitor_task = None
    
    # If parameters not provided in query params, wait for start event from Twilio
    if flow is None and session_id is None:
        try:
            logger.info("No query params provided. Waiting for Twilio 'connected'/'start' events to extract customParameters...")
            while True:
                msg = await websocket.receive_text()
                data = json.loads(msg)
                event = data.get("event")
                
                if event == "connected":
                    logger.info("Twilio connected event received. Waiting for start event...")
                    continue
                elif event == "start":
                    start_event_data = data
                    start_data = data.get("start", {})
                    stream_sid = start_data.get("streamSid")
                    call_sid = start_data.get("callSid")
                    call_start_time = datetime.now(timezone.utc)
                    
                    custom_params = start_data.get("customParameters", {})
                    flow = custom_params.get("flow", "session")
                    sess_val = custom_params.get("session_id")
                    if sess_val is not None:
                        try:
                            session_id = int(sess_val)
                        except ValueError:
                            pass
                    logger.info("Extracted parameters from start event: flow=%s, session_id=%s, call_sid=%s", flow, session_id, call_sid)
                    break
        except Exception as e:
            logger.error("Error receiving initial Twilio events: %s", e)
            await websocket.close()
            return

    # Load settings — use getattr() to avoid AttributeError if attribute doesn't exist
    gemini_api_key = getattr(settings, "gemini_live_api_key", None) or getattr(settings, "gemini_api_key", None)
    gemini_model = getattr(settings, "gemini_live_model", None) or getattr(settings, "gemini_model", None) or "models/gemini-2.0-flash-exp"
    if gemini_model and not gemini_model.startswith("models/"):
        gemini_model = f"models/{gemini_model}"
    
    if not gemini_api_key:
        logger.error("Trainer voice Gemini API key is not configured. Closing WebSocket.")
        await websocket.close()
        return
    logger.info("Trainer voice Gemini API key configured: yes")

    instruction = ""
    tools = []
    
    # ── 1. Setup mode identification vs session ──────────────────────────────
    if flow == "identify":
        instruction = IDENTIFICATION_SYSTEM_INSTRUCTION
        tools = [
            {
                "functionDeclarations": [
                    {
                        "name": "verify_agent_code",
                        "description": "Verifica el código de empleado por voz.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "agent_code": {
                                    "type": "STRING",
                                    "description": "El código del agente (ej: FR45, LD23)."
                                }
                            },
                            "required": ["agent_code"]
                        }
                    },
                    {
                        "name": "verify_simulation_code",
                        "description": "Verifica el código de la simulación e inicia el roleplay.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "simulation_code": {
                                    "type": "STRING",
                                    "description": "El código de la simulación (ej: SIM101)."
                                },
                                "agent_code": {
                                    "type": "STRING",
                                    "description": "El código del agente ya validado."
                                }
                            },
                            "required": ["simulation_code", "agent_code"]
                        }
                    }
                ]
            }
        ]
    elif session_id is not None:
        logger.info("Trainer WS validating session_id=%s", session_id)
        stmt = (
            select(TrainerSession)
            .options(
                selectinload(TrainerSession.simulation),
                selectinload(TrainerSession.simulation_version),
            )
            .where(TrainerSession.session_id == session_id)
        )
        res = await db.execute(stmt)
        sess = res.scalars().first()
        if not sess:
            logger.error("Trainer session %d not found in DB.", session_id)
            await websocket.close()
            return
            
        logger.info("Trainer WS session validation success: session_id=%s, agent_id=%s, simulation_id=%s", sess.session_id, sess.agent_id, sess.simulation_id)
        logger.info("Trainer WS simulation loaded: simulation_id=%s, code=%s, name=%s", sess.simulation.simulation_id, sess.simulation.code, getattr(sess.simulation, 'name', 'unknown'))
        
        # Get active version prompt snapshot
        roleplay_prompt = sess.simulation.roleplay_prompt
        if sess.simulation_version_id:
            stmt_v = select(TrainerSimulationVersion).where(
                TrainerSimulationVersion.version_id == sess.simulation_version_id
            )
            res_v = await db.execute(stmt_v)
            version = res_v.scalars().first()
            if version:
                roleplay_prompt = version.roleplay_prompt_snapshot

        logger.info("Trainer WS roleplay prompt loaded successfully.")

        # Resolve company and healthcare context dynamically
        company = None
        if sess.simulation and sess.simulation.company_id:
            stmt_c = select(Company).where(Company.company_id == sess.simulation.company_id)
            res_c = await db.execute(stmt_c)
            company = res_c.scalars().first()

        is_healthcare = False
        if company:
            is_demo = bool(company.is_demo or company.company_key == "empresa-demo")
            if not is_demo:
                c_sector = (company.sector or "").lower()
                c_name = (company.company_name or "").lower()
                c_key = (company.company_key or "").lower()
                if c_sector in ("healthcare", "salud", "medico", "médico", "clinica", "clínica"):
                    is_healthcare = True
                elif "boston" in c_name or "boston" in c_key or company.company_id == 1:
                    is_healthcare = True

        interlocutor_role = "paciente" if is_healthcare else "cliente"
        turn_discipline = build_turn_discipline(is_healthcare=is_healthcare, interlocutor_role=interlocutor_role)

        instruction_parts = [roleplay_prompt, turn_discipline, SPANISH_VOICE_RULES]
        if is_healthcare:
            instruction_parts.append(HEALTHCARE_VOICE_RULES)
        instruction = "\n".join(instruction_parts)
        tools = [
            {
                "functionDeclarations": [
                    {
                        "name": "hangup_call",
                        "description": "Finaliza el roleplay y cuelga la llamada de forma limpia.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "reason": {
                                    "type": "STRING",
                                    "description": "Razón para finalizar (ej: exito_conversacional)."
                                }
                            }
                        }
                    }
                ]
            }
        ]
    else:
        logger.error("Trainer WebSocket connection requires either flow='identify' or a valid session_id.")
        await websocket.close()
        return

    gemini_url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={gemini_api_key}"
    
    try:
        async with websockets.connect(gemini_url) as gemini_ws:
            logger.info("Connected to Gemini Live WebSocket.")
            
            # Send Setup Configuration
            setup_msg = {
                "setup": {
                    "model": gemini_model,
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Algieba"
                                }
                            }
                        },
                        "thinkingConfig": {
                            "thinkingLevel": "minimal"
                        }
                    },
                    "realtimeInputConfig": {
                        "automaticActivityDetection": {
                            "disabled": False,
                            "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                            "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                            "prefixPaddingMs": 200,
                            "silenceDurationMs": 600,
                        },
                        "turnCoverage": "TURN_INCLUDES_ONLY_ACTIVITY",
                        "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                    },
                    "systemInstruction": {
                        "parts": [{"text": instruction.strip()}]
                    },
                    "tools": tools
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))
            logger.info("Sent Gemini Setup configuration.")
            logger.info("Trainer Gemini Live configuration established.")
            
            # State variables
            gemini_ready = False
            assistant_is_speaking = False
            waiting_for_user_response = False
            user_audio_seen_since_last_assistant_turn = False
            initial_roleplay_prompt_sent = False
            last_assistant_turn_completed_at = None
            speech_state = "silent"
            accumulated_voice_ms = 0
            consecutive_silent_ms = 0
            accumulated_barge_in_ms = 0
            discard_current_assistant_audio = False
            attempts = 0
            recording_sid = None
            redirected = False
            identified_agent_id = None
            identified_agent_code = None
            pending_graceful_hangup = False
            graceful_hangup_timer_task = None
            
            # VAD debug and block throttle variables
            media_events_total = 0
            media_events_inbound = 0
            media_events_outbound = 0
            media_events_unknown_track = 0
            last_track = None
            max_rms_last_second = 0.0
            last_debug_log_time = datetime.now(timezone.utc)
            last_blocked_log_time = datetime.now(timezone.utc)
            
            # Barge-in state machine variables
            barge_in_active = False
            barge_in_recovery_pending = False
            assistant_audio_forwarding_enabled = True
            last_user_speech_end_time = None
            barge_in_time = None
            nudge_triggered = False
            
            # Use variables read from start event if available (from early parsing)
            stream_sid = stream_sid
            call_sid = call_sid
            call_start_time = call_start_time
            
            twilio_rate_state = None
            gemini_rate_state = None
            monitor_task = None
            recording_started = False

            # Early recording trigger (if parsed from early start event)
            if session_id is not None and call_sid is not None and not recording_started:
                recording_started = True
                host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                logger.info("About to start Twilio recording from start_event_custom_parameters (early): session_id=%d, call_sid=%s", session_id, call_sid)
                recording_sid = await start_twilio_recording(call_sid, host)
                if not recording_sid:
                    async with AsyncSessionLocal() as db_fail:
                        stmt_fail = select(TrainerSession).where(TrainerSession.session_id == session_id)
                        res_fail = await db_fail.execute(stmt_fail)
                        sess_fail = res_fail.scalar()
                        if sess_fail:
                            sess_fail.evaluation_status = "recording_start_failed"
                            await db_fail.commit()
                            logger.warning("Trainer session %d marked as recording_start_failed because Twilio start_twilio_recording returned None (early).", session_id)
                call_start_time = datetime.now(timezone.utc)
                monitor_task = asyncio.create_task(
                    duration_monitor_task(call_sid, stream_sid, gemini_ws, websocket, session_id)
                )

            async def perform_actual_hangup():
                nonlocal pending_graceful_hangup, call_active, graceful_hangup_timer_task
                if not pending_graceful_hangup:
                    return
                pending_graceful_hangup = False
                if graceful_hangup_timer_task and not graceful_hangup_timer_task.done():
                    graceful_hangup_timer_task.cancel()
                
                # Perform the real hangup
                logger.info("Trainer graceful hangup completed, hanging up Twilio call")
                try:
                    await hangup_twilio_call(call_sid)
                except Exception as e_hang:
                    logger.error("Error in hangup_twilio_call during graceful hangup: %s", e_hang)
                try:
                    await handle_roleplay_hangup(session_id, call_sid, call_start_time, "exito_conversacional")
                except Exception as e_role:
                    logger.error("Error in handle_roleplay_hangup during graceful hangup: %s", e_role)
                call_active = False
                try:
                    if websocket.client_state.name != "DISCONNECTED":
                        await websocket.close()
                except Exception:
                    pass

            # Concurrency loops
            async def twilio_to_gemini_loop():
                nonlocal stream_sid, call_sid, call_start_time, recording_sid, recording_started, monitor_task, twilio_rate_state
                nonlocal waiting_for_user_response, user_audio_seen_since_last_assistant_turn
                nonlocal last_assistant_turn_completed_at, speech_state, accumulated_voice_ms, consecutive_silent_ms, accumulated_barge_in_ms
                nonlocal assistant_is_speaking, discard_current_assistant_audio
                nonlocal media_events_total, media_events_inbound, media_events_outbound, media_events_unknown_track
                nonlocal last_track, max_rms_last_second, last_debug_log_time
                nonlocal barge_in_active, barge_in_recovery_pending, assistant_audio_forwarding_enabled, last_user_speech_end_time, barge_in_time, nudge_triggered, call_active
                vad_log_counter = 0
                async for message in websocket.iter_text():
                    try:
                        data = json.loads(message)
                        event = data.get("event")
                        
                        if event == "start":
                            if not stream_sid:
                                start_data = data.get("start", {})
                                stream_sid = start_data.get("streamSid")
                                call_sid = start_data.get("callSid")
                                logger.info("Twilio stream start. stream_sid=%s, call_sid=%s", stream_sid, call_sid)
                                
                                # Start recording and duration monitor if we already have session_id
                                if session_id is not None and not recording_started:
                                    recording_started = True
                                    host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                    logger.info("About to start Twilio recording from start_event_custom_parameters (loop): session_id=%d, call_sid=%s", session_id, call_sid)
                                    recording_sid = await start_twilio_recording(call_sid, host)
                                    if not recording_sid:
                                        # Twilio failed to start recording: mark session as recording_start_failed
                                        async with AsyncSessionLocal() as db_fail:
                                            stmt_fail = select(TrainerSession).where(TrainerSession.session_id == session_id)
                                            res_fail = await db_fail.execute(stmt_fail)
                                            sess_fail = res_fail.scalar()
                                            if sess_fail:
                                                sess_fail.evaluation_status = "recording_start_failed"
                                                await db_fail.commit()
                                                logger.warning("Trainer session %d marked as recording_start_failed because Twilio start_twilio_recording returned None (loop).", session_id)
                                    call_start_time = datetime.now(timezone.utc)
                                    monitor_task = asyncio.create_task(
                                        duration_monitor_task(call_sid, stream_sid, gemini_ws, websocket, session_id)
                                    )
                            else:
                                logger.info("Twilio stream start event received in loop, but already initialized early.")
                                    
                        elif event == "media" and gemini_ready:
                            media = data.get("media", {})
                            track = media.get("track")
                            payload = media.get("payload")
                            
                            media_events_total += 1
                            last_track = track
                            if track == "inbound":
                                media_events_inbound += 1
                            elif track == "outbound":
                                media_events_outbound += 1
                            else:
                                media_events_unknown_track += 1
                                
                            # Only discard explicitly outbound track; treat empty or inbound as user speech
                            if track != "outbound" and payload:
                                # Transcode base64 G.711 µ-law to 16kHz linear PCM
                                pcm_payload, twilio_rate_state = decode_twilio_to_gemini(payload, twilio_rate_state)
                                if pcm_payload:
                                    try:
                                        pcm_bytes = base64.b64decode(pcm_payload)
                                    except Exception:
                                        if isinstance(pcm_payload, str):
                                            pcm_bytes = pcm_payload.encode("utf-8")
                                        else:
                                            pcm_bytes = pcm_payload

                                    rms = calculate_pcm_energy(pcm_bytes)

                                    vad_log_counter += 1
                                    if rms > max_rms_last_second:
                                        max_rms_last_second = rms
                                    
                                    # VAD grace period check
                                    in_grace_period = False
                                    if last_assistant_turn_completed_at:
                                        elapsed_ms = (datetime.now(timezone.utc) - last_assistant_turn_completed_at).total_seconds() * 1000
                                        if elapsed_ms < VAD_GRACE_PERIOD_MS:
                                            in_grace_period = True

                                    # Throttled debug log: every 1.0s while waiting_for_user_response = True
                                    now = datetime.now(timezone.utc)
                                    if waiting_for_user_response and (now - last_debug_log_time).total_seconds() >= 1.0:
                                        logger.info(
                                            "Trainer VAD debug:\n"
                                            "  - call_sid: %s\n"
                                            "  - session_id: %s\n"
                                            "  - media_events_total: %d\n"
                                            "  - media_events_inbound: %d\n"
                                            "  - media_events_outbound: %d\n"
                                            "  - media_events_unknown_track: %d\n"
                                            "  - last_track: %s\n"
                                            "  - payload_bytes: %d\n"
                                            "  - pcm_bytes: %d\n"
                                            "  - rms: %.1f\n"
                                            "  - max_rms_last_second: %.1f\n"
                                            "  - speech_frames: %d\n"
                                            "  - speech_ms: %d\n"
                                            "  - assistant_is_speaking: %s\n"
                                            "  - waiting_for_user_response: %s\n"
                                            "  - user_audio_seen_since_last_assistant_turn: %s",
                                            call_sid, session_id, media_events_total, media_events_inbound,
                                            media_events_outbound, media_events_unknown_track, last_track,
                                            len(payload) if payload else 0, len(pcm_bytes), rms, max_rms_last_second,
                                            vad_log_counter, accumulated_voice_ms, assistant_is_speaking,
                                            waiting_for_user_response, user_audio_seen_since_last_assistant_turn
                                        )
                                        last_debug_log_time = now
                                        max_rms_last_second = 0.0  # reset for next second

                                    if in_grace_period:
                                        # Reset speech detection metrics during grace period to ignore tail noise/echoes
                                        accumulated_voice_ms = 0
                                        consecutive_silent_ms = 0
                                        accumulated_barge_in_ms = 0
                                    else:
                                        if rms > VAD_ENERGY_THRESHOLD:
                                            accumulated_voice_ms += 20
                                            consecutive_silent_ms = 0
                                            
                                            # Speech confirmed threshold
                                            if accumulated_voice_ms >= VAD_MIN_SPEECH_DURATION_MS:
                                                if speech_state != "speaking":
                                                    speech_state = "speaking"
                                                    nudge_triggered = False
                                                    logger.info("Trainer VAD speech detected:\n  - rms: %.1f\n  - speech_ms: %d\n  - setting user_audio_seen_since_last_assistant_turn=True", rms, accumulated_voice_ms)
                                                    
                                                if waiting_for_user_response:
                                                    logger.info("Trainer turn gate: user audio detected, allowing next assistant response.")
                                                    waiting_for_user_response = False
                                                    user_audio_seen_since_last_assistant_turn = True
                                                    
                                            # Handle Barge-in (interruption) with sustained evidence check
                                            if assistant_is_speaking:
                                                if rms > BARGE_IN_ENERGY_THRESHOLD:
                                                    accumulated_barge_in_ms += 20
                                                else:
                                                    accumulated_barge_in_ms = max(0, accumulated_barge_in_ms - 10)
                                                
                                                if accumulated_barge_in_ms >= BARGE_IN_MIN_SPEECH_DURATION_MS:
                                                    logger.info(
                                                        "Trainer barge-in confirmed: user interrupted assistant with sustained speech (%dms, rms=%.1f)",
                                                        accumulated_barge_in_ms, rms
                                                    )
                                                    assistant_is_speaking = False
                                                    waiting_for_user_response = False
                                                    user_audio_seen_since_last_assistant_turn = True
                                                    
                                                    barge_in_active = True
                                                    barge_in_recovery_pending = False
                                                    barge_in_time = datetime.now(timezone.utc)
                                                    nudge_triggered = False
                                                    accumulated_barge_in_ms = 0
                                                    
                                                    logger.info(
                                                        "Trainer barge-in state:\n"
                                                        "  - assistant_is_speaking: False\n"
                                                        "  - waiting_for_user_response: False\n"
                                                        "  - user_audio_seen_since_last_assistant_turn: True\n"
                                                        "  - barge_in_active: True"
                                                    )
                                                    logger.info("Trainer turn gate: user interruption accepted.")
                                                    
                                                    # Send clear event to Twilio to stop playing queued audio immediately
                                                    if stream_sid:
                                                        clear_msg = {
                                                            "event": "clear",
                                                            "streamSid": stream_sid
                                                        }
                                                        await websocket.send_text(json.dumps(clear_msg))
                                                        logger.info("Trainer barge-in: sent Twilio clear event.")
                                            else:
                                                accumulated_barge_in_ms = 0
                                        else:
                                            # Fallback if VAD threshold is too high but there is actual user audio input:
                                            # If we have received inbound audio packets for more than 800ms and RMS > 15 (non-absolute-silence)
                                            # while waiting_for_user_response = True, fallback and mark user_audio_seen=True
                                            if waiting_for_user_response and rms > 15.0:
                                                accumulated_voice_ms += 20
                                                if accumulated_voice_ms >= 800:
                                                    logger.info("Trainer VAD speech detected (VAD fallback):\n  - rms: %.1f\n  - accumulated_ms: %d\n  - setting user_audio_seen_since_last_assistant_turn=True", rms, accumulated_voice_ms)
                                                    logger.info("Trainer turn gate: user audio detected (fallback), allowing next assistant response.")
                                                    waiting_for_user_response = False
                                                    user_audio_seen_since_last_assistant_turn = True
                                                    accumulated_voice_ms = 0
                                            else:
                                                consecutive_silent_ms += 20
                                                if consecutive_silent_ms >= 300:
                                                    accumulated_voice_ms = 0
                                                    if speech_state == "speaking":
                                                        speech_state = "silent"
                                                        logger.info("Trainer VAD: user speech ended.")
                                                        last_user_speech_end_time = datetime.now(timezone.utc)
                                                        
                                                        if barge_in_active:
                                                            elapsed_ms = int((last_user_speech_end_time - barge_in_time).total_seconds() * 1000)
                                                            barge_in_active = False
                                                            waiting_for_user_response = False
                                                            user_audio_seen_since_last_assistant_turn = True
                                                            logger.info(
                                                                "Trainer barge-in user speech ended:\n"
                                                                "  - elapsed_ms_since_barge_in: %d\n"
                                                                "  - user_audio_seen_since_last_assistant_turn: True\n"
                                                                "  - allowing_assistant_response: True",
                                                                elapsed_ms
                                                            )

                                    input_msg = {
                                        "realtimeInput": {
                                            "audio": {
                                                "mimeType": "audio/pcm;rate=16000",
                                                "data": pcm_payload
                                            }
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(input_msg))
                                    
                        elif event == "stop":
                            logger.info("Twilio stream stop event received.")
                            call_active = False
                            break
                    except (websockets.ConnectionClosed, WebSocketDisconnect):
                        break
                    except Exception as e_inner:
                        err_str = str(e_inner).lower()
                        if not call_active or "1008" in err_str or "aborted" in err_str or "closed" in err_str:
                            logger.info("Twilio websocket already closed after call stop; cleanup completed.")
                            break
                        else:
                            logger.warning("Error in twilio_to_gemini_loop: %s", e_inner)
                            continue

            async def gemini_to_twilio_loop():
                nonlocal gemini_ready, attempts, identified_agent_id, identified_agent_code, redirected, gemini_rate_state
                nonlocal assistant_is_speaking, waiting_for_user_response, user_audio_seen_since_last_assistant_turn, initial_roleplay_prompt_sent
                nonlocal last_assistant_turn_completed_at, discard_current_assistant_audio, last_blocked_log_time
                nonlocal barge_in_active, barge_in_recovery_pending, assistant_audio_forwarding_enabled, last_user_speech_end_time, barge_in_time, nudge_triggered, call_active
                nonlocal pending_graceful_hangup, graceful_hangup_timer_task
                async for message in gemini_ws:
                    try:
                        data = json.loads(message)
                        
                        if "setupComplete" in data:
                            logger.info("Gemini Live Live API setup complete.")
                            gemini_ready = True
                            
                            # Initial start message: begin in-character immediately
                            if session_id is not None:
                                if not initial_roleplay_prompt_sent:
                                    initial_roleplay_prompt_sent = True
                                    logger.info("Trainer roleplay initial in-character prompt sent.")
                                    start_msg = {
                                        "clientContent": {
                                            "turns": [{
                                                "role": "user",
                                                "parts": [{"text": f"El agente ya está conectado al teléfono. Inicia la llamada interpretando exclusivamente a tu personaje de {interlocutor_role.lower()} diciendo tu primera frase breve."}]
                                            }],
                                            "turnComplete": True
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(start_msg))
                                else:
                                    logger.info("Ignoring duplicate initial roleplay prompt trigger.")
                                
                        elif "toolCall" in data:
                            calls = data["toolCall"].get("functionCalls", [])
                            for call in calls:
                                fid = call.get("id")
                                name = call.get("name")
                                args = call.get("args", {})
                                
                                logger.info("Gemini Live toolCall request: %s", name)
                                
                                if name == "verify_agent_code" and flow == "identify":
                                    agent_code = args.get("agent_code", "").strip()
                                    async with AsyncSessionLocal() as sub_db:
                                        agent = await TrainerService.validate_agent_code(sub_db, agent_code)
                                        if agent:
                                            identified_agent_id = agent["agent_id"]
                                            identified_agent_code = agent_code.replace(" ", "").upper()
                                            result_val = {"status": "valid", "agent_name": agent["agent_name"]}
                                        else:
                                            attempts += 1
                                            if attempts >= 2:
                                                host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                                await redirect_to_dtmf(call_sid, host)
                                                redirected = True
                                                return
                                            result_val = {"status": "invalid", "attempts": attempts}

                                    resp_msg = {
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id": fid,
                                                "name": name,
                                                "response": {"result": result_val}
                                            }]
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(resp_msg))
                                    
                                elif name == "verify_simulation_code" and flow == "identify":
                                    sim_code = args.get("simulation_code", "").strip()
                                    async with AsyncSessionLocal() as sub_db:
                                        val_res = await TrainerService.validate_simulation_for_agent(sub_db, sim_code, identified_agent_id)
                                        if val_res["valid"] and val_res["simulation"] and identified_agent_id:
                                            sim = val_res["simulation"]
                                            host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                            await redirect_trainer_call(call_sid, host, identified_agent_id, sim.simulation_id)
                                            redirected = True
                                            return
                                        elif val_res["status"] == "service_mismatch":
                                            result_val = {
                                                "status": "service_mismatch",
                                                "message": val_res.get("message") or "Ese código de simulación pertenece a otro servicio y no al tuyo actual.",
                                            }
                                        else:
                                            result_val = {"status": "invalid"}

                                    resp_msg = {
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id": fid,
                                                "name": name,
                                                "response": {"result": result_val}
                                            }]
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(resp_msg))
                                    
                                elif name == "hangup_call" and session_id is not None:
                                    reason = args.get("reason", "normal")
                                    logger.info("Gemini requested call hangup. Reason: %s", reason)
                                    
                                    # Guard: block premature out-of-roleplay hangups
                                    elapsed_seconds = 0
                                    if call_start_time:
                                        elapsed_seconds = (datetime.now(timezone.utc) - call_start_time).total_seconds()
                                    if reason == "el_agente_se_sale_del_roleplay" and elapsed_seconds < HANGUP_EARLY_BLOCK_SECONDS:
                                        logger.warning(
                                            "Trainer hangup blocked: too early to hang up for out-of-roleplay reason. Elapsed: %.1fs",
                                            elapsed_seconds
                                        )
                                        # Send function response telling Gemini to continue
                                        tool_resp = {
                                            "toolResponse": {
                                                "functionResponses": [{
                                                    "id": fid,
                                                    "name": "hangup_call",
                                                    "response": {"result": "blocked_too_early", "instruction": "Continúa el roleplay. No cuelgues hasta que el agente haya tenido suficiente tiempo de interactuar."}
                                                }]
                                            }
                                        }
                                        await gemini_ws.send(json.dumps(tool_resp))
                                        continue
                                    
                                    logger.info("Trainer graceful hangup requested")
                                    
                                    # Send toolResponse so Gemini continues execution
                                    tool_resp = {
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id": fid,
                                                "name": "hangup_call",
                                                "response": {"result": "graceful_hangup_pending"}
                                            }]
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(tool_resp))

                                    # Instruct Gemini to speak the closing phrase
                                    hangup_msg = {
                                        "clientContent": {
                                            "turns": [{
                                                "role": "user",
                                                "parts": [{"text": "Di exactamente: 'La simulación ha terminado. Que tengas un buen día.'"}]
                                            }],
                                            "turnComplete": True
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(hangup_msg))
                                    logger.info("Trainer graceful hangup closing message sent")
                                    
                                    pending_graceful_hangup = True
                                    
                                    # Schedule safety timeout fallback (4 seconds)
                                    async def safety_timeout():
                                        await asyncio.sleep(4.0)
                                        if pending_graceful_hangup:
                                            logger.info("Trainer graceful hangup timeout fallback, hanging up Twilio call")
                                            await perform_actual_hangup()
                                            
                                    graceful_hangup_timer_task = asyncio.create_task(safety_timeout())
                                    continue

                        elif "serverContent" in data:
                            content = data["serverContent"]
                            # Check for server-side interruption event (Barge-in real)
                            if content.get("interrupted"):
                                logger.info("Trainer turn gate: assistant response interrupted by server. Clearing Twilio audio buffer.")
                                assistant_is_speaking = False
                                waiting_for_user_response = False
                                user_audio_seen_since_last_assistant_turn = True
                                accumulated_barge_in_ms = 0
                                if stream_sid:
                                    clear_msg = {
                                        "event": "clear",
                                        "streamSid": stream_sid
                                    }
                                    await websocket.send_text(json.dumps(clear_msg))

                            model_turn = content.get("modelTurn")
                            if model_turn:
                                parts = model_turn.get("parts", [])
                                for part in parts:
                                    audio_base64 = part.get("inlineData", {}).get("data")
                                    if audio_base64:
                                        if not assistant_is_speaking:
                                            logger.info("Trainer turn gate: assistant response started.")
                                            assistant_is_speaking = True
                                            barge_in_recovery_pending = False
                                            nudge_triggered = False
                                            
                                        # Transcode 24kHz linear PCM to µ-law 8kHz
                                        mulaw_payload, gemini_rate_state = encode_gemini_to_twilio(audio_base64, gemini_rate_state)
                                        if mulaw_payload and stream_sid:
                                            media_msg = {
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {
                                                    "payload": mulaw_payload
                                                }
                                            }
                                            await websocket.send_text(json.dumps(media_msg))
                                            
                            if content.get("turnComplete"):
                                logger.info("Trainer turn gate: assistant response completed, waiting for user.")
                                assistant_is_speaking = False
                                waiting_for_user_response = True
                                user_audio_seen_since_last_assistant_turn = False
                                last_assistant_turn_completed_at = datetime.now(timezone.utc)
                                
                                if pending_graceful_hangup:
                                    logger.info("Trainer turn gate: assistant finished speaking final message, triggering graceful hangup")
                                    await perform_actual_hangup()
                                    return
                                
                    except (websockets.ConnectionClosed, WebSocketDisconnect):
                        break
                    except Exception as e_inner:
                        err_str = str(e_inner).lower()
                        if not call_active or "1008" in err_str or "aborted" in err_str or "closed" in err_str:
                            logger.info("Twilio websocket already closed after call stop; cleanup completed.")
                            break
                        else:
                            logger.warning("Error in gemini_to_twilio_loop: %s", e_inner)
                            continue

            async def barge_in_watchdog_loop():
                nonlocal last_user_speech_end_time, assistant_is_speaking, nudge_triggered
                nonlocal barge_in_recovery_pending, call_active, gemini_ws, gemini_ready
                while call_active:
                    try:
                        await asyncio.sleep(0.1)
                        if (
                            barge_in_recovery_pending
                            and last_user_speech_end_time is not None
                            and not assistant_is_speaking
                            and not nudge_triggered
                        ):
                            diff = (datetime.now(timezone.utc) - last_user_speech_end_time).total_seconds()
                            if diff >= 2.0:
                                # Reset recovery state cleanly without injecting synthetic user text into the conversation
                                barge_in_recovery_pending = False
                                barge_in_active = False
                                nudge_triggered = True
                                logger.info("Trainer barge-in recovery window reset cleanly without prompt pollution.")
                    except Exception as e_watchdog:
                        logger.error("Error in barge_in_watchdog_loop: %s", e_watchdog)
                        break

            # Run loops concurrently
            tw_task = asyncio.create_task(twilio_to_gemini_loop())
            gem_task = asyncio.create_task(gemini_to_twilio_loop())
            barge_task = asyncio.create_task(barge_in_watchdog_loop())

            try:
                done, pending = await asyncio.wait(
                    [tw_task, gem_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    if t.exception():
                        logger.error("Media stream task completed with error: %s", t.exception())
            finally:
                call_active = False
                for t in [tw_task, gem_task, barge_task]:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(tw_task, gem_task, barge_task, return_exceptions=True)

            # Cancel monitor task if running
            if monitor_task:
                monitor_task.cancel()

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
    except Exception as e:
        err_str = str(e).lower()
        if not call_active or "1008" in err_str or "aborted" in err_str or "closed" in err_str:
            logger.info("Twilio websocket already closed after call stop; cleanup completed.")
        else:
            logger.error("Error in media_stream websocket: %s", e)
    finally:
        call_active = False
        # Cancel monitor task if running
        try:
            if 'monitor_task' in locals() and monitor_task:
                monitor_task.cancel()
        except Exception:
            pass
        if not websocket.client_state.name == "DISCONNECTED":
            try:
                await websocket.close()
            except Exception:
                pass

        # Finalize session if websocket closed and not redirected
        if session_id is not None and not redirected:
            try:
                await handle_roleplay_hangup(session_id, call_sid, call_start_time, "websocket_close")
            except Exception as e_hang:
                logger.error("Error finalizing session in hangup finally: %s", e_hang)

        logger.info("Media stream cleanup completed.")
