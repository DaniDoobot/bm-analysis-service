"""
FastAPI router for Twilio voice training integration (IVR, WebSockets media streaming, and Gemini Live).
"""
import logging
import base64
import json
import httpx
import asyncio
import websockets
from decimal import Decimal
from datetime import datetime, timezone
from typing import List, Optional, Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, desc, func

from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingEvaluationPrompt,
    TrainingCallEvaluation,
)
from app.config import get_settings
from app.db import get_engine

try:
    import audioop
except ImportError:
    import audioop_lts as audioop

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/bm/training", tags=["Training Voice Voice/IVR"])

IDENTIFICATION_SYSTEM_INSTRUCTION = """
Eres el Asistente de Identificación por Voz de Boston Medical Group.
Tu labor en esta fase es identificar al agente y, si tiene varios ciclos de entrenamiento pendientes, ayudarle a seleccionar uno de ellos por voz.

Sigue estas pautas estrictas:
1. Pide de forma amable al agente que te diga su código corto de empleado (que consiste en dos letras seguidas de números, por ejemplo: LD23, FR45, CM21, EC7).
2. Una vez que el agente te diga su código (ej. "ele de veintitrés", "L D veintitrés", "efe erre cuarenta y cinco"), extráelo, normalízalo en mayúsculas y sin espacios, y llamar inmediatamente a la herramienta `verify_agent_code(agent_code=codigo_extraido)`.
3. Si el backend te devuelve que el código es incorrecto (status es "invalid"), infórmale con tacto y pídele que lo intente de nuevo.
4. Si el backend te devuelve que se inicia la redirección directamente (status es "redirecting"), avisa brevemente ("Código verificado, un momento por favor...") y no digas nada más.
5. Si el backend te devuelve que no hay ciclos activos (status es "no_active_cycles"), indícaselo claramente diciendo exactamente: "He visto que no tienes ningún entrenamiento en proceso, vas al día con todo." y luego despídete amablemente. No intentes pedir el código otra vez.
6. Si el backend te devuelve que hay varios ciclos activos (status es "multiple_cycles"), debes saludar amistosamente al agente usando su nombre (ej. "Perfecto Fernanda, vamos a ver qué ciclos tienes pendientes.") y luego presentarle las opciones de ciclos de forma muy clara usando sus fechas reales provistas en la respuesta de la herramienta (debes pronunciar las fechas exactas devueltas por verify_agent_code, el texto "del 1 al 17 de mayo" es solo un ejemplo).
7. Escucha atentamente la respuesta de voz del agente. El agente elegirá diciendo cosas como "el primero", "el segundo", "el de la primera quincena", "el uno", "el de mayo", etc.
8. Asocia la respuesta del agente al ciclo correspondiente de la lista y llama inmediatamente a la herramienta `select_training_cycle(cycle_id=ID_DEL_CICLO)`.
9. Cuando llames a `select_training_cycle`, hazlo inmediatamente después de que el usuario elija, sin añadir explicaciones largas ni despedidas adicionales, ya que la llamada se transferirá de forma inmediata.
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
REGLA CRÍTICA: BLOQUEO DE PERSONAJE (OBLIGATORIA)
=================================================
ESTÁS REALIZANDO UNA SIMULACIÓN DE ROLEPLAY DE ENTRENAMIENTO. ESTO ES UN JUEGO DE ROL CONTROLADO.
En ningún momento eres ni actúas como una IA genérica, un asistente virtual, ni un sistema de ayuda médica real.
Tu único rol durante toda la llamada es el del PACIENTE descrito en las instrucciones de este prompt.

REGLAS ABSOLUTAS E IRROMPIBLES:
1. NUNCA abandones el personaje del paciente, independientemente de lo que diga el agente.
2. NUNCA respondas como una IA ni mentions limitaciones de IA (ej: "como modelo de lenguaje...", "no tengo capacidad de...").
3. NUNCA des consejos médicos genéricos como "consulta a un médico profesional" o "acude a urgencias" ni ninguna indicación que salga del contexto de la simulación de Boston Medical Group.
4. NUNCA digas que "es importante hablar con un profesional sanitario". Esas frases rompen el personaje y arruinan la simulación.
5. Si el agente intenta salir del guion o pregunta si eres una IA, responde siempre como el paciente, ignorando la pregunta o redirigiendo: "Oiga, ¿me va a ayudar o no?" / "A ver, yo lo que quiero saber es...".
6. Si el contenido de la conversación se aleja del escenario de la simulación, el paciente muestra impaciencia o vuelve al tema de la llamada con frases naturales.
7. Recuerda: estás en una SIMULACIÓN controlada de entrenamiento. No hay riesgo real. Mantén el rol del paciente en todo momento sin excepción.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

async def redirect_twilio_call(call_sid: str, host: str, agent_id: str, cycle_id: int) -> bool:
    """Redirect an active Twilio call to the start-roleplay route."""
    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot redirect call.")
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    redirect_url = f"{scheme}://{host}/bm/training/voice/twilio/start-roleplay?agent_id={agent_id}&cycle_id={cycle_id}&call_sid={call_sid}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Url": redirect_url})
            response.raise_for_status()
            logger.info("Successfully redirected Twilio call %s to start-roleplay route.", call_sid)
            return True
        except Exception as e:
            logger.error("Failed to redirect Twilio call %s: %s", call_sid, e)
            return False


async def redirect_to_dtmf(call_sid: str, host: str) -> bool:
    """Redirect an active Twilio call to collect-dtmf fallback route."""
    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot redirect call to DTMF.")
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    redirect_url = f"{scheme}://{host}/bm/training/voice/twilio/collect-dtmf?call_sid={call_sid}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Url": redirect_url})
            response.raise_for_status()
            logger.info("Successfully redirected Twilio call %s to collect-dtmf fallback route.", call_sid)
            return True
        except Exception as e:
            logger.error("Failed to redirect Twilio call to DTMF %s: %s", call_sid, e)
            return False


async def start_simulation_redirect(db: AsyncSession, agent_id: str, cycle_id: int, call_sid: str, host: str) -> Response:
    """Return TwiML Redirect to start-roleplay route."""
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    redirect_url = f"{scheme}://{host}/bm/training/voice/twilio/start-roleplay?agent_id={agent_id}&cycle_id={cycle_id}&call_sid={call_sid}"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Redirect>{redirect_url}</Redirect>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


def format_period_spanish(start: datetime, end: datetime) -> str:
    """Format datetime period into natural Spanish phrasing."""
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
        7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    if start.month == end.month:
        return f"del {start.day} al {end.day} de {meses[start.month]}"
    else:
        return f"del {start.day} de {meses[start.month]} al {end.day} de {meses[end.month]}"


async def get_active_cycles_for_agent(db: AsyncSession, hubspot_owner_id: str) -> List[TrainingAgentReport]:
    """Retrieve all cycles for an agent that have pending or in_progress simulations."""
    stmt_cycles = select(TrainingAgentReport).where(
        and_(
            TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
            TrainingAgentReport.status.in_(["pending", "running", "completed"])
        )
    ).order_by(desc(TrainingAgentReport.training_report_id))
    res_cycles = await db.execute(stmt_cycles)
    cycles = list(res_cycles.scalars().all())
    
    active_cycles = []
    for c in cycles:
        stmt_comps = select(TrainingCompletionStatus).where(
            and_(
                TrainingCompletionStatus.training_report_id == c.training_report_id,
                TrainingCompletionStatus.status.in_(["pending", "in_progress"])
            )
        )
        res_comps = await db.execute(stmt_comps)
        pending_comps = list(res_comps.scalars().all())
        if pending_comps:
            active_cycles.append(c)
            
    return active_cycles


def enforce_admin_role(user: User):
    """Enforce that the logged-in user is an administrator."""
    if user.role not in ["admin", "administrador"]:
        logger.warning("Access denied: User ID %s does not have administrator role.", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador para realizar esta operación."
        )


async def start_twilio_recording(call_sid: str, host: str) -> Optional[str]:
    """Start Twilio recording for call_sid and set the status callback."""
    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot record call.")
        return None
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json"
    auth = (account_sid, auth_token)
    
    # Secure callback url
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    callback_url = f"{scheme}://{host}/bm/training/voice/twilio/recording-completed"
    
    data = {
        "RecordingTrack": "both",
        "RecordingStatusCallback": callback_url,
        "RecordingStatusCallbackEvent": ["completed", "absent"]
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data=data)
            response.raise_for_status()
            res_json = response.json()
            rec_sid = res_json.get("sid")
            logger.info("Successfully started Twilio recording for call %s: %s", call_sid, rec_sid)
            return rec_sid
        except Exception as e:
            logger.error("Failed to start Twilio recording for call %s: %s", call_sid, e)
            return None


async def hangup_twilio_call(call_sid: str) -> bool:
    """Terminate the Twilio call after a brief delay to allow final audio to finish."""
    account_sid = settings.twilio_account_sid
    auth_token = settings.twilio_auth_token
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot hang up call.")
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    
    # Sleep 3 seconds as done in reference project to play final audio turn
    await asyncio.sleep(3)
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Status": "completed"})
            response.raise_for_status()
            logger.info("Successfully hung up Twilio call %s", call_sid)
            return True
        except Exception as e:
            logger.error("Failed to hang up Twilio call %s: %s", call_sid, e)
            return False


async def start_session_for_prompt(db: AsyncSession, hubspot_owner_id: str, report_id: int, prompt: TrainingSimulationPrompt, call_sid: str) -> int:
    """Create a new training call session and link/update related structures."""
    session = TrainingCallSession(
        call_sid=call_sid,
        agent_id=hubspot_owner_id,
        cycle_id=report_id,
        conversation_id=prompt.simulation_prompt_id,
        status="in_progress"
    )
    db.add(session)
    await db.flush()
    
    # Link to completion status
    stmt_comp = select(TrainingCompletionStatus).where(
        and_(
            TrainingCompletionStatus.training_report_id == report_id,
            TrainingCompletionStatus.simulation_prompt_id == prompt.simulation_prompt_id
        )
    )
    res_comp = await db.execute(stmt_comp)
    comp = res_comp.scalars().first()
    if comp:
        comp.status = "in_progress"
        comp.call_session_id = session.session_id
        comp.training_call_id = call_sid
        
    # Mark cycle report as running
    stmt_rep = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == report_id)
    res_rep = await db.execute(stmt_rep)
    report = res_rep.scalars().first()
    if report and report.status == "pending":
        report.status = "running"
        
    await db.commit()
    return session.session_id


async def start_cycle_roleplay(db: AsyncSession, cycle: TrainingAgentReport, hubspot_owner_id: str, call_sid: str, agent_name: str, request: Request) -> Response:
    """Resolve next pending simulation and construct TwiML response to initiate streaming."""
    stmt_prompts = select(TrainingSimulationPrompt).where(
        TrainingSimulationPrompt.training_report_id == cycle.training_report_id
    ).order_by(TrainingSimulationPrompt.prompt_number.asc())
    res_prompts = await db.execute(stmt_prompts)
    prompts = list(res_prompts.scalars().all())
    
    stmt_comps = select(TrainingCompletionStatus).where(
        TrainingCompletionStatus.training_report_id == cycle.training_report_id
    )
    res_comps = await db.execute(stmt_comps)
    comps = {c.simulation_prompt_id: c for c in res_comps.scalars().all()}
    
    pending_prompt = None
    for p in prompts:
        comp = comps.get(p.simulation_prompt_id)
        if comp and comp.status in ["pending", "in_progress"]:
            pending_prompt = p
            break
                
    if not pending_prompt:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Hola. Ya has completado todas las conversaciones de este ciclo de entrenamiento. Gracias por tu esfuerzo.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    # Start call session in DB
    session_id = await start_session_for_prompt(db, hubspot_owner_id, cycle.training_report_id, pending_prompt, call_sid)
    
    # Build Webhook ws_url
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto", "http")
    scheme = "wss" if proto == "https" or "localhost" not in host else "ws"
    
    ws_url = f"{scheme}://{host}/bm/training/voice/twilio/media-stream"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="{ws_url}">
                <Parameter name="session_id" value="{session_id}" />
            </Stream>
        </Connect>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


# ── Twilio IVR Webhooks ────────────────────────────────────────────────────────

@router.post("/voice/twilio/incoming-call")
async def incoming_call(request: Request):
    """Initial Twilio Webhook to answer calls and start identification stream by voice."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto", "http")
    scheme = "wss" if proto == "https" or "localhost" not in host else "ws"
    
    ws_url = f"{scheme}://{host}/bm/training/voice/twilio/media-stream"
    
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


@router.post("/voice/twilio/collect-dtmf")
async def collect_dtmf(request: Request):
    """Webhook to gather agent numeric code via DTMF gather."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    action_url = f"/bm/training/voice/twilio/verify-numeric-code"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="4" timeout="10" action="{action_url}">
            <Say language="es-ES">No he podido identificar tu código por voz. Por favor, introduce tu código numérico de empleado de cuatro dígitos en el teclado, seguido de la tecla almohadilla.</Say>
        </Gather>
        <Say language="es-ES">No he recibido ninguna entrada. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/twilio/verify-numeric-code")
async def verify_numeric_code(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Verify agent numeric training code and route to cycle selection or start simulation."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    call_sid = form_data.get("CallSid", "").strip()
    
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
            TrainingAgentSetting.is_enabled == True,
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
        
    agent_name = setting.agent_name
    hubspot_owner_id = setting.hubspot_owner_id
    
    # Query active reports (cycles)
    cycles = await get_active_cycles_for_agent(db, hubspot_owner_id)
    
    if not cycles:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Hola {agent_name}. Actualmente no tienes ningún ciclo de entrenamiento activo. Contacta con tu supervisor.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    if len(cycles) == 1:
        # Redirect to start simulation
        return await start_simulation_redirect(db, hubspot_owner_id, cycles[0].training_report_id, call_sid, host)
        
    # Multiple active cycles, prompt to choose one
    first_name = agent_name.split()[0] if agent_name else "Agente"
    say_text = f"Perfecto {first_name}, vamos a ver qué ciclos tienes pendientes. "
    if len(cycles) == 2:
        p1 = format_period_spanish(cycles[0].period_start, cycles[0].period_end)
        p2 = format_period_spanish(cycles[1].period_start, cycles[1].period_end)
        say_text += f"¿Quieres hacer el ciclo {p1} o el ciclo {p2}? Presiona 1 o 2 respectivamente."
    else:
        say_text += f"Tienes {len(cycles)} ciclos activos. "
        for i, c in enumerate(cycles[:3]):
            p = format_period_spanish(c.period_start, c.period_end)
            say_text += f"Para el ciclo {p}, presiona {i+1}. "
        
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="1" timeout="10" action="/bm/training/voice/twilio/select-cycle?agent_id={hubspot_owner_id}&amp;call_sid={call_sid}">
            <Say language="es-ES">{say_text}</Say>
        </Gather>
        <Say language="es-ES">No he recibido ninguna selección. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/twilio/select-cycle")
async def select_cycle(
    request: Request,
    agent_id: str = Query(...),
    call_sid: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """Handle DTMF input for cycle selection and redirect to start simulation."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    
    try:
        selection_idx = int(digits) - 1
    except ValueError:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Selección no válida. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    cycles = await get_active_cycles_for_agent(db, agent_id)
    
    if selection_idx < 0 or selection_idx >= len(cycles):
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Selección incorrecta. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    selected_cycle = cycles[selection_idx]
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    return await start_simulation_redirect(db, agent_id, selected_cycle.training_report_id, call_sid, host)


@router.post("/voice/twilio/start-roleplay")
async def start_roleplay(
    request: Request,
    agent_id: str = Query(...),
    cycle_id: int = Query(...),
    call_sid: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """Dynamically start roleplay stream for the resolved agent and cycle."""
    # Find settings for initials
    stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == agent_id)
    res_set = await db.execute(stmt_set)
    setting = res_set.scalars().first()
    agent_name = setting.agent_name if setting else "Agente"
    
    # Find cycle report
    stmt_cycle = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == cycle_id)
    res_cycle = await db.execute(stmt_cycle)
    cycle = res_cycle.scalars().first()
    
    if not cycle:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">No se encontró el ciclo de entrenamiento. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    return await start_cycle_roleplay(db, cycle, agent_id, call_sid, agent_name, request)


# ── Audio Transcoding Math Helpers ────────────────────────────────────────────

def decode_twilio_to_gemini(base64_payload: str, rate_state: Optional[tuple]) -> tuple[Optional[str], Optional[tuple]]:
    """Convert G.711 µ-law 8kHz Base64 (Twilio) to 16kHz linear PCM 16-bit Base64 (Gemini)."""
    try:
        mulaw_bytes = base64.b64decode(base64_payload)
        # 1. Decode µ-law to 16-bit linear PCM (still at 8kHz, 2 bytes/sample)
        pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
        # 2. Resample from 8kHz to 16kHz (width=2, channels=1)
        pcm_16k, new_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, rate_state)
        # 3. Encode to Base64 string
        payload = base64.b64encode(pcm_16k).decode("utf-8")
        return payload, new_state
    except Exception as e:
        logger.error("Error decoding Twilio audio to Gemini: %s", e)
        return None, rate_state


def encode_gemini_to_twilio(base64_payload: str, rate_state: Optional[tuple]) -> tuple[Optional[str], Optional[tuple]]:
    """Convert 24kHz linear PCM 16-bit Base64 (Gemini) to G.711 µ-law 8kHz Base64 (Twilio)."""
    try:
        pcm_24k = base64.b64decode(base64_payload)
        # 1. Resample from 24kHz to 8kHz (width=2, channels=1)
        pcm_8k, new_state = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, rate_state)
        # 2. Encode 16-bit linear PCM to G.711 µ-law
        mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)
        payload = base64.b64encode(mulaw_bytes).decode("utf-8")
        return payload, new_state
    except Exception as e:
        logger.error("Error encoding Gemini audio to Twilio: %s", e)
        return None, rate_state


# ── Dynamic WS Helper Functions ──────────────────────────────────────────────

async def duration_monitor_task(
    call_sid: str,
    stream_sid: str,
    gemini_ws: websockets.WebSocketClientProtocol,
    websocket: WebSocket,
    session_id: int
):
    """Monitor active simulation call duration and apply progressive timeout closures."""
    logger.info("Starting progressive timeout monitor for call: %s", call_sid)
    try:
        # 1. Minuto 8:00 (Aviso de Cierre)
        await asyncio.sleep(480)
        logger.info("8 minutes reached for call %s. Injecting close orienting system instruction.", call_sid)
        msg_8min = {
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": "[INSTRUCCIÓN DEL SISTEMA: El tiempo de llamada se agota (minuto 8). Como paciente, empieza a orientar la conversación hacia el cierre, cediendo ante la solución propuesta o pidiendo confirmación final.]"}]
                }],
                "turnComplete": True
            }
        }
        await gemini_ws.send(json.dumps(msg_8min))
        
        # 2. Minuto 9:30 (Cierre Amable Forzado)
        await asyncio.sleep(90)
        logger.info("9:30 minutes reached for call %s. Injecting polite forced closure.", call_sid)
        msg_930min = {
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": "[INSTRUCCIÓN DEL SISTEMA: El entrenamiento ha terminado. Sal del personaje de forma amigable y pronuncia exactamente la frase obligatoria: 'El entrenamiento ha terminado, ten un buen día y muchas gracias' e invoca inmediatamente la herramienta hangup_call.]"}]
                }],
                "turnComplete": True
            }
        }
        await gemini_ws.send(json.dumps(msg_930min))
        
        # 3. Minuto 10:00 (Colgado del Backend)
        await asyncio.sleep(30)
        logger.info("10 minutes reached for call %s. Forcing backend disconnect.", call_sid)
        await hangup_twilio_call(call_sid)
        
    except asyncio.CancelledError:
        logger.info("Progressive timeout monitor cancelled for call %s.", call_sid)
    except Exception as e:
        logger.error("Error in progressive timeout monitor for call %s: %s", call_sid, e)


async def handle_verify_agent_code(
    agent_code: str,
    call_sid: str,
    websocket: WebSocket,
    attempts: int
) -> dict:
    """Validate voice-identified agent code, route call to start-roleplay or DTMF fallback."""
    cleaned = agent_code.replace(" ", "").upper()
    host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
    
    engine = get_engine()
    async with AsyncSession(engine) as db:
        stmt = select(TrainingAgentSetting).where(
            and_(
                func.upper(TrainingAgentSetting.training_code) == cleaned,
                TrainingAgentSetting.is_enabled == True,
                TrainingAgentSetting.training_code_enabled == True
            )
        )
        res = await db.execute(stmt)
        setting = res.scalars().first()
        
        if not setting:
            attempts += 1
            if attempts >= 2:
                logger.warning("Agent voice identification failed twice. Redirecting to DTMF fallback. cleaned_code=%s", cleaned)
                await redirect_to_dtmf(call_sid, host)
                return {"attempts": attempts, "result": {"status": "fallback_dtmf"}, "redirected": True}
            else:
                logger.info("Agent voice identification failed once. attempts=%d, cleaned_code=%s", attempts, cleaned)
                return {"attempts": attempts, "result": {"status": "invalid", "attempts": attempts}, "redirected": False}
        
        # Verify active cycles
        hubspot_owner_id = setting.hubspot_owner_id
        active_cycles = await get_active_cycles_for_agent(db, hubspot_owner_id)
        
        if not active_cycles:
            logger.info("Agent %s identified, but has no active cycles with pending simulations.", setting.agent_name)
            return {"attempts": attempts, "result": {"status": "no_active_cycles", "agent_name": setting.agent_name}, "redirected": False}
            
        if len(active_cycles) == 1:
            logger.info("Agent %s identified with 1 active cycle. Redirecting to start-roleplay.", setting.agent_name)
            await redirect_twilio_call(call_sid, host, hubspot_owner_id, active_cycles[0].training_report_id)
            return {"attempts": attempts, "result": {"status": "redirecting"}, "redirected": True}
        else:
            logger.info("Agent %s identified with %d active cycles. Remaining in WebSocket to select cycle by voice.", setting.agent_name, len(active_cycles))
            cycles_data = []
            for idx, c in enumerate(active_cycles):
                p = format_period_spanish(c.period_start, c.period_end)
                cycles_data.append({
                    "cycle_id": c.training_report_id,
                    "index": idx + 1,
                    "period_text": p
                })
            return {
                "attempts": attempts,
                "agent_id": hubspot_owner_id,
                "result": {
                    "status": "multiple_cycles",
                    "agent_name": setting.agent_name.split()[0],
                    "cycles": cycles_data
                },
                "redirected": False
            }


async def handle_roleplay_hangup(
    session_id: int,
    call_sid: str,
    call_start_time: Optional[datetime],
    reason: str
):
    """Mark session as ended or failed depending on duration constraints."""
    engine = get_engine()
    async with AsyncSession(engine) as db:
        stmt = select(TrainingCallSession).where(TrainingCallSession.session_id == session_id)
        res = await db.execute(stmt)
        session = res.scalars().first()
        
        if session and session.status == "in_progress":
            duration_seconds = 0
            if call_start_time:
                duration_seconds = (datetime.now(timezone.utc) - call_start_time).total_seconds()
            
            logger.info("Training voice session ended. duration=%.2f seconds, reason=%s", duration_seconds, reason)
            
            # Short Call Rule: < 15 seconds is marked as failed, resetting progress status to pending
            if duration_seconds < 15 and reason != "exito_conversacional":
                logger.warning("Short call duration < 15 seconds. Setting status to failed. Resetting completion progress.")
                session.status = "failed"
                session.error_message = f"Llamada menor a 15 segundos: {int(duration_seconds)} segundos."
                session.ended_at = datetime.now(timezone.utc)
                
                stmt_comp = select(TrainingCompletionStatus).where(
                    and_(
                        TrainingCompletionStatus.training_report_id == session.cycle_id,
                        TrainingCompletionStatus.simulation_prompt_id == session.conversation_id
                    )
                )
                res_comp = await db.execute(stmt_comp)
                comp = res_comp.scalars().first()
                if comp:
                    comp.status = "pending"
                    comp.completed_at = None
                    comp.training_call_id = None
                    comp.call_session_id = None
            else:
                session.status = "ended"
                session.ended_at = datetime.now(timezone.utc)
                
            await db.commit()


# ── Select Cycle Menu Webhook ──────────────────────────────────────────────────

@router.post("/voice/twilio/select-cycle-menu")
async def select_cycle_menu(
    request: Request,
    agent_id: str = Query(...),
    call_sid: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """Webhook menu to handle cycle selection when agent has multiple active cycles."""
    cycles = await get_active_cycles_for_agent(db, agent_id)
    
    if not cycles:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">No se encontraron ciclos de entrenamiento activos. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")
        
    # Get agent first name
    stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == agent_id)
    res_set = await db.execute(stmt_set)
    setting = res_set.scalars().first()
    first_name = setting.agent_name.split()[0] if setting else "Agente"
    
    say_text = f"Perfecto {first_name}, vamos a ver qué ciclos tienes pendientes. "
    if len(cycles) == 2:
        p1 = format_period_spanish(cycles[0].period_start, cycles[0].period_end)
        p2 = format_period_spanish(cycles[1].period_start, cycles[1].period_end)
        say_text += f"¿Quieres hacer el ciclo {p1} o el ciclo {p2}? Presiona 1 o 2 respectivamente."
    else:
        say_text += f"Tienes {len(cycles)} ciclos activos. "
        for i, c in enumerate(cycles[:3]):
            p = format_period_spanish(c.period_start, c.period_end)
            say_text += f"Para el ciclo {p}, presiona {i+1}. "
        
    action_url = f"/bm/training/voice/twilio/select-cycle?agent_id={agent_id}&amp;call_sid={call_sid}"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="1" timeout="10" action="{action_url}">
            <Say language="es-ES">{say_text}</Say>
        </Gather>
        <Say language="es-ES">No he recibido ninguna selección. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


# ── Realtime WebSocket Media Stream Endpoint ──────────────────────────────────

@router.websocket("/voice/twilio/media-stream")
async def twilio_media_stream(
    websocket: WebSocket,
    flow: Optional[str] = Query(None),
    session_id: Optional[int] = Query(None)
):
    """FastAPI WebSocket media stream connecting Twilio calls to Gemini Live Multimodal API."""
    await websocket.accept()
    logger.info("Accepted Twilio WebSocket connection. Initial query params: flow=%s, session_id=%s", flow, session_id)
    
    # Initialize variables that can be overridden by customParameters
    start_event_data = None
    stream_sid = None
    call_sid = None
    call_start_time = None
    
    # If parameters not provided in query params, wait for start event from Twilio
    if flow is None and session_id is None:
        try:
            logger.info("No query params provided. Waiting for Twilio 'start' event to extract customParameters...")
            
            async def wait_for_start_event():
                nonlocal flow, session_id, start_event_data, stream_sid, call_sid, call_start_time
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
                        flow = custom_params.get("flow")
                        sess_val = custom_params.get("session_id")
                        if sess_val is not None:
                            try:
                                session_id = int(sess_val)
                            except ValueError:
                                logger.warning("Invalid session_id in customParameters: %s", sess_val)
                        logger.info("Extracted parameters from start event. flow=%s, session_id=%s, stream_sid=%s, call_sid=%s", flow, session_id, stream_sid, call_sid)
                        return
                    else:
                        logger.info("Ignoring pre-start Twilio event: %s", event)
            
            # Timeout of 10.0 seconds for the entire loop
            await asyncio.wait_for(wait_for_start_event(), timeout=10.0)
            
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for initial Twilio 'start' event.")
            await websocket.close()
            return
        except Exception as e:
            logger.error("Error receiving/parsing initial Twilio message: %s", e)
            await websocket.close()
            return

    gemini_api_key = settings.gemini_api_key
    gemini_model = settings.gemini_model or "models/gemini-3.1-flash-live-preview"
    if not gemini_api_key:
        logger.error("GEMINI_API_KEY is not configured in config/environment.")
        await websocket.close()
        return
        
    # Determine the system instructions and tools depending on the connection flow
    instruction = ""
    tools = []
    
    if flow == "identify":
        instruction = IDENTIFICATION_SYSTEM_INSTRUCTION + "\n" + SPANISH_VOICE_RULES
        tools = [
            {
                "functionDeclarations": [
                    {
                        "name": "verify_agent_code",
                        "description": "Valida el código alfanumérico pronunciado por el agente.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "agent_code": {
                                    "type": "STRING",
                                    "description": "Código alfanumérico pronunciado y normalizado (ej: LD23)."
                                }
                            },
                            "required": ["agent_code"]
                        }
                    },
                    {
                        "name": "select_training_cycle",
                        "description": "Permite seleccionar uno de los ciclos activos cuando el agente tiene múltiples opciones.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "cycle_id": {
                                    "type": "INTEGER",
                                    "description": "El ID del ciclo de entrenamiento seleccionado."
                                }
                            },
                            "required": ["cycle_id"]
                        }
                    }
                ]
            }
        ]
    elif session_id is not None:
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(TrainingCallSession).where(TrainingCallSession.session_id == session_id)
            res = await db.execute(stmt)
            sess = res.scalars().first()
            if not sess:
                logger.error("Session %s not found in DB.", session_id)
                await websocket.close()
                return
            stmt_pr = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.simulation_prompt_id == sess.conversation_id)
            res_pr = await db.execute(stmt_pr)
            prompt = res_pr.scalars().first()
            if not prompt:
                logger.error("Simulation prompt not found in DB.")
                await websocket.close()
                return
                
            # Count remaining simulations in this cycle (including current one which is in_progress)
            stmt_rem = select(func.count(TrainingCompletionStatus.completion_id)).where(
                and_(
                    TrainingCompletionStatus.training_report_id == sess.cycle_id,
                    TrainingCompletionStatus.status.in_(["pending", "in_progress"])
                )
            )
            res_rem = await db.execute(stmt_rem)
            remaining_count = res_rem.scalar() or 1
            
            # Fetch agent name to personalize
            stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == sess.agent_id)
            res_set = await db.execute(stmt_set)
            setting = res_set.scalars().first()
            agent_first_name = setting.agent_name.split()[0] if setting else "Agente"
            
            instruction = prompt.prompt_text + "\n" + SPANISH_VOICE_RULES
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
        logger.error("WebSocket connection requires either flow='identify' or a valid session_id.")
        await websocket.close()
        return
        
    gemini_url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={gemini_api_key}"
    
    try:
        async with websockets.connect(gemini_url) as gemini_ws:
            logger.info("Connected to Gemini Live WebSocket.")
            
            # 1. Send Setup Configuration
            setup_msg = {
                "setup": {
                    "model": gemini_model,
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Algieba"  # Requested voice, sounds natural & peninsular
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
                            "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                            "prefixPaddingMs": 120,
                            "silenceDurationMs": 130,
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
            
            # State variables for WS session scope
            gemini_ready = False
            attempts = 0
            recording_sid = None
            redirected = False
            identified_agent_id = None
            
            twilio_rate_state = None
            gemini_rate_state = None
            monitor_task = None
            
            # Start recording and duration monitor early if we already have session_id and call_sid from the early start event
            if session_id is not None and call_sid:
                logger.info("Triggering recording and duration monitor task early from pre-parsed start event.")
                host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                recording_sid = await start_twilio_recording(call_sid, host)
                monitor_task = asyncio.create_task(
                    duration_monitor_task(call_sid, stream_sid, gemini_ws, websocket, session_id)
                )
            
            # Concurrency loops
            async def twilio_to_gemini_loop():
                nonlocal stream_sid, call_sid, call_start_time, recording_sid, monitor_task, twilio_rate_state
                async for message in websocket.iter_text():
                    try:
                        data = json.loads(message)
                        event = data.get("event")
                        
                        if event == "start":
                            if not stream_sid:
                                start_data = data.get("start", {})
                                stream_sid = start_data.get("streamSid")
                                call_sid = start_data.get("callSid")
                                logger.info("Twilio stream start in loop. stream_sid=%s, call_sid=%s", stream_sid, call_sid)
                                call_start_time = datetime.now(timezone.utc)
                                
                                # In simulation, trigger recording and timeouts monitor
                                if session_id is not None and call_sid:
                                    host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                    recording_sid = await start_twilio_recording(call_sid, host)
                                    monitor_task = asyncio.create_task(
                                        duration_monitor_task(call_sid, stream_sid, gemini_ws, websocket, session_id)
                                    )
                            else:
                                logger.info("Twilio stream start event received in loop, but already initialized early.")
                        elif event == "media":
                            payload = data.get("media", {}).get("payload")
                            if payload and gemini_ready:
                                pcm_16k_base64, twilio_rate_state = decode_twilio_to_gemini(payload, twilio_rate_state)
                                if pcm_16k_base64:
                                    gemini_audio = {
                                        "realtimeInput": {
                                            "audio": {
                                                "mimeType": "audio/pcm;rate=16000",
                                                "data": pcm_16k_base64
                                            }
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(gemini_audio))
                        elif event == "stop":
                            logger.info("Twilio stream stopped. Stopping loops.")
                            break
                    except Exception as e_tw:
                        logger.error("Error in Twilio WS loop: %s", e_tw)
                        break
                        
            async def gemini_to_twilio_loop():
                nonlocal gemini_ready, attempts, redirected, gemini_rate_state, identified_agent_id
                async for raw_msg in gemini_ws:
                    try:
                        data = json.loads(raw_msg)
                        
                        if "setupComplete" in data:
                            logger.info("Gemini Live configuration established.")
                            gemini_ready = True
                            
                            # Trigger initial greeting
                            if flow == "identify":
                                greet_msg = {
                                    "clientContent": {
                                        "turns": [{
                                            "role": "user",
                                            "parts": [{"text": "Di exactamente: 'Hola, soy Luis, el asistente de entrenamiento de Boston Medical. Por favor, dime tu código de empleado.'"}]
                                        }],
                                        "turnComplete": True
                                    }
                                }
                                await gemini_ws.send(json.dumps(greet_msg))
                            else:
                                if remaining_count == 1:
                                    rem_text = "te queda solo este entrenamiento, así que vamos a ello."
                                else:
                                    rem_text = f"te quedan {remaining_count} entrenamientos, así que vamos a ello."
                                    
                                greet_msg = {
                                    "clientContent": {
                                        "turns": [{
                                            "role": "user",
                                            "parts": [{"text": f"Di exactamente: 'Perfecto {agent_first_name}, pues vamos con ese. {rem_text} Iniciamos la simulación número {prompt.prompt_number}. Prepárate.' y a continuación, sin pausar, inicia la conversación asumiendo tu personaje del roleplay."}]
                                        }],
                                        "turnComplete": True
                                    }
                                }
                                await gemini_ws.send(json.dumps(greet_msg))
                                
                        elif "toolCall" in data:
                            calls = data["toolCall"].get("functionCalls", [])
                            for call in calls:
                                call_id = call.get("id")
                                name = call.get("name")
                                args = call.get("args", {})
                                
                                logger.info("Gemini Live toolCall request: %s", name)
                                
                                if name == "verify_agent_code" and flow == "identify":
                                    agent_code = args.get("agent_code", "").strip()
                                    status_res = await handle_verify_agent_code(
                                        agent_code=agent_code,
                                        call_sid=call_sid,
                                        websocket=websocket,
                                        attempts=attempts
                                    )
                                    attempts = status_res.get("attempts", attempts)
                                    result_val = status_res.get("result", {})
                                    if "agent_id" in status_res:
                                        identified_agent_id = status_res["agent_id"]
                                    
                                    # Respond to Gemini
                                    resp_msg = {
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id": call_id,
                                                "name": name,
                                                "response": {"result": result_val}
                                            }]
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(resp_msg))
                                    
                                    if status_res.get("redirected"):
                                        redirected = True
                                        return
                                        
                                    if result_val.get("status") == "no_active_cycles":
                                        # Schedule an asynchronous hangup after 6 seconds to let Gemini say the phrase
                                        async def delayed_no_cycles_hangup():
                                            await asyncio.sleep(6)
                                            logger.info("No active cycles: hanging up Twilio call %s.", call_sid)
                                            await hangup_twilio_call(call_sid)
                                        asyncio.create_task(delayed_no_cycles_hangup())
                                        
                                elif name == "select_training_cycle" and flow == "identify":
                                    cycle_id = args.get("cycle_id")
                                    # Ensure we have the identified agent_id
                                    if not identified_agent_id and cycle_id:
                                        # Lookup from database
                                        engine = get_engine()
                                        async with AsyncSession(engine) as db:
                                            stmt_c = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == cycle_id)
                                            res_c = await db.execute(stmt_c)
                                            cycle_obj = res_c.scalars().first()
                                            if cycle_obj:
                                                identified_agent_id = cycle_obj.hubspot_owner_id
                                    
                                    if identified_agent_id and cycle_id:
                                        host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                        logger.info("Voice cycle selection received. Redirecting Twilio call %s to cycle %s for agent %s.", call_sid, cycle_id, identified_agent_id)
                                        await redirect_twilio_call(call_sid, host, identified_agent_id, cycle_id)
                                        
                                        resp_msg = {
                                            "toolResponse": {
                                                "functionResponses": [{
                                                    "id": call_id,
                                                    "name": name,
                                                    "response": {"result": {"status": "redirecting"}}
                                                }]
                                            }
                                        }
                                        await gemini_ws.send(json.dumps(resp_msg))
                                        redirected = True
                                        return
                                    else:
                                        logger.error("Failed to redirect voice cycle selection: agent_id=%s, cycle_id=%s", identified_agent_id, cycle_id)
                                        resp_msg = {
                                            "toolResponse": {
                                                "functionResponses": [{
                                                    "id": call_id,
                                                    "name": name,
                                                    "response": {"result": {"status": "error", "message": "Falta información del agente o del ciclo."}}
                                                }]
                                            }
                                        }
                                        await gemini_ws.send(json.dumps(resp_msg))
                                        
                                elif name == "hangup_call":
                                    reason = args.get("reason", "fin_de_conversacion")
                                    await handle_roleplay_hangup(
                                        session_id=session_id,
                                        call_sid=call_sid,
                                        call_start_time=call_start_time,
                                        reason=reason
                                    )
                                    
                                    # Respond to Gemini
                                    resp_msg = {
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id": call_id,
                                                "name": name,
                                                "response": {"result": {"ok": True}}
                                            }]
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(resp_msg))
                                    await hangup_twilio_call(call_sid)
                                    redirected = True
                                    return
                                    
                        elif "serverContent" in data:
                            content = data["serverContent"]
                            
                            # Barge-in handling
                            if content.get("interrupted") and stream_sid:
                                logger.info("User interrupted bot voice. Clearing Twilio audio buffer.")
                                clear_msg = {"event": "clear", "streamSid": stream_sid}
                                await websocket.send_text(json.dumps(clear_msg))
                                
                            model_turn = content.get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    inline_data = part.get("inlineData")
                                    if inline_data and inline_data.get("data"):
                                        base64_pcm24k = inline_data.get("data")
                                        base64_mulaw8k, gemini_rate_state = encode_gemini_to_twilio(
                                            base64_pcm24k, gemini_rate_state
                                        )
                                        if base64_mulaw8k and stream_sid:
                                            media_msg = {
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {
                                                    "payload": base64_mulaw8k
                                                }
                                            }
                                            await websocket.send_text(json.dumps(media_msg))
                    except Exception as e_gem:
                        logger.error("Error in Gemini WS loop: %s", e_gem)
                        break
                        
            # Execute concurrent loops
            tw_task = asyncio.create_task(twilio_to_gemini_loop())
            gem_task = asyncio.create_task(gemini_to_twilio_loop())
            
            done, pending = await asyncio.wait(
                [tw_task, gem_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
                
            # Cleanup duration monitor
            if monitor_task:
                monitor_task.cancel()
                
            # Handle phone hangup (unexpected websocket disconnect without hangup_call tool)
            if session_id is not None and not redirected:
                logger.info("WS disconnected unexpectedly. Triggering default hangup cleanup.")
                await handle_roleplay_hangup(
                    session_id=session_id,
                    call_sid=call_sid,
                    call_start_time=call_start_time,
                    reason="hangup"
                )
                
    except Exception as e_conn:
        logger.error("Connection error in media stream WS: %s", e_conn)
    finally:
        logger.info("Closing Twilio WebSocket.")
        try:
            await websocket.close()
        except Exception:
            pass


# ── Twilio Recording Completed Webhook ────────────────────────────────────────

@router.post("/voice/twilio/recording-completed")
async def recording_completed(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Receive completed recording callback from Twilio and trigger evaluation."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "").strip()
    recording_sid = form_data.get("RecordingSid", "").strip()
    recording_url = form_data.get("RecordingUrl", "").strip()
    recording_status = form_data.get("RecordingStatus", "").strip()
    
    logger.info(
        "Recording status callback: call_sid=%s, recording_sid=%s, url=%s, status=%s",
        call_sid, recording_sid, recording_url, recording_status
    )
    
    if recording_status != "completed":
        logger.warning("Recording not completed successfully. Status: %s", recording_status)
        return Response(content="ok", media_type="text/plain")
        
    # Find matching call session by call_sid
    stmt = select(TrainingCallSession).where(
        TrainingCallSession.call_sid == call_sid
    ).order_by(desc(TrainingCallSession.session_id))
    res = await db.execute(stmt)
    session = res.scalars().first()
        
    if not session:
        logger.error("No active/completed Call Session found for call_sid: %s", call_sid)
        return Response(content="ok", media_type="text/plain")
        
    # Update Session recording info
    session.recording_sid = recording_sid
    session.recording_url = recording_url
    session.recording_ready_at = datetime.now(timezone.utc)
    await db.commit()
    
    # Trigger asynchronous background evaluation task
    from fastapi.background import BackgroundTasks
    bg_tasks = BackgroundTasks()
    
    # We delay execution import to avoid circular dependencies
    from app.services.personalized_training_service import evaluate_training_session_task
    bg_tasks.add_task(evaluate_training_session_task, session.session_id)
    
    return Response(content="ok", media_type="text/plain", background=bg_tasks)


# ── Admin Training Evaluation Prompts API ──────────────────────────────────────

@router.get("/admin/evaluation-prompts", response_model=List[dict])
async def list_evaluation_prompts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all voice training evaluation prompts (Admin only)."""
    enforce_admin_role(current_user)
    
    # Fetch active/inactive training evaluation prompts, joining Service
    from app.models.services import Service
    stmt = select(TrainingEvaluationPrompt, Service.service_name).join(
        Service, TrainingEvaluationPrompt.service_id == Service.service_id
    ).order_by(Service.service_name.asc(), TrainingEvaluationPrompt.version.desc())
    res = await db.execute(stmt)
    
    results = []
    for row in res.all():
        prompt = row[0]
        srv_name = row[1]
        results.append({
            "id": prompt.id,
            "service_id": prompt.service_id,
            "service_name": srv_name,
            "prompt_text": prompt.prompt_text,
            "version": prompt.version,
            "is_active": prompt.is_active,
            "created_by": prompt.created_by,
            "created_at": prompt.created_at,
            "updated_at": prompt.updated_at
        })
    return results


@router.put("/admin/evaluation-prompts/{service_id}", response_model=dict)
async def update_evaluation_prompt(
    service_id: int,
    payload: dict, # expects {"prompt_text": "..."}
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """Create a new version of the training evaluation prompt for a service, deactivating old ones (Admin only)."""
    enforce_admin_role(current_user)
    
    prompt_text = payload.get("prompt_text")
    if not prompt_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El campo 'prompt_text' es requerido."
        )
        
    # Verify service exists
    from app.models.services import Service
    stmt_srv = select(Service).where(Service.service_id == service_id)
    res_srv = await db.execute(stmt_srv)
    srv = res_srv.scalars().first()
    if not srv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )
        
    # Find active prompt version to increment version number
    stmt_active = select(TrainingEvaluationPrompt).where(
        and_(
            TrainingEvaluationPrompt.service_id == service_id,
            TrainingEvaluationPrompt.is_active == True
        )
    )
    res_active = await db.execute(stmt_active)
    active_prompt = res_active.scalars().first()
    
    next_version = 1
    if active_prompt:
        next_version = active_prompt.version + 1
        # Deactivate current version
        active_prompt.is_active = False
        active_prompt.updated_at = datetime.now(timezone.utc)
        db.add(active_prompt)
        
    # Create new active prompt version
    new_prompt = TrainingEvaluationPrompt(
        service_id=service_id,
        prompt_text=prompt_text,
        version=next_version,
        is_active=True,
        created_by=current_user.email
    )
    db.add(new_prompt)
    await db.commit()
    await db.refresh(new_prompt)
    
    return {
        "id": new_prompt.id,
        "service_id": new_prompt.service_id,
        "service_name": srv.service_name,
        "prompt_text": new_prompt.prompt_text,
        "version": new_prompt.version,
        "is_active": new_prompt.is_active,
        "created_by": new_prompt.created_by,
        "created_at": new_prompt.created_at,
        "updated_at": new_prompt.updated_at
    }


@router.post("/admin/sessions/{session_id}/retry-evaluation")
async def retry_session_evaluation(
    session_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Admin endpoint: Re-trigger the background evaluation task for a training call session.
    Useful when a session has status 'failed' due to a transient error (e.g., JSON parse failure)
    but the recording URL is still accessible.
    Resets session status to 'in_progress' so the evaluator can process it again.
    """
    from app.models.personalized_training import TrainingCallSession, TrainingCompletionStatus
    from app.services.personalized_training_service import evaluate_training_session_task

    stmt = select(TrainingCallSession).where(TrainingCallSession.session_id == session_id)
    res = await db.execute(stmt)
    session = res.scalars().first()

    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")

    if not session.recording_url:
        raise HTTPException(
            status_code=400,
            detail="Session has no recording URL. Cannot re-evaluate without a recording."
        )

    # Reset session to allow re-evaluation
    session.status = "in_progress"
    session.error_message = None

    # Reset the associated completion status to pending if it exists
    stmt_comp = select(TrainingCompletionStatus).where(
        TrainingCompletionStatus.training_report_id == session.cycle_id,
        TrainingCompletionStatus.prompt_number == session.prompt_number,
    )
    res_comp = await db.execute(stmt_comp)
    comp = res_comp.scalars().first()
    if comp and comp.status != "completed":
        comp.status = "pending"
        comp.completed_at = None
        comp.evaluation_id = None

    await db.commit()

    background_tasks.add_task(evaluate_training_session_task, session_id)

    logger.info(
        "Admin re-triggered evaluation for session %d (agent %s, cycle %s).",
        session_id, session.agent_id, session.cycle_id
    )
    return {
        "message": f"Evaluation re-triggered for session {session_id}.",
        "session_id": session_id,
        "recording_url": session.recording_url,
    }
