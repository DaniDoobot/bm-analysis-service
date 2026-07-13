"""FastAPI router for unified Twilio voice training hub (agent identification, select mode, DTMF fallbacks, and redirection)."""
import logging
import base64
import json
import httpx
import asyncio
import os
import websockets
from datetime import datetime, timezone
from typing import List, Optional, Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.dependencies import get_db
from app.models.personalized_training import TrainingAgentSetting
from app.models.trainer import TrainerSimulation
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.services.trainer_service import TrainerService
from app.routers.trainer_voice import redirect_trainer_call
from app.routers.training_voice import redirect_twilio_call, get_active_cycles_for_agent

try:
    import audioop
except ImportError:
    import audioop_lts as audioop

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/bm/training/hub", tags=["Training Hub Voice/IVR"])

# Enforce brand pronunciation and natural Spanish peninsular voice rules
SPANISH_VOICE_RULES = """
- Conversación telefónica natural y fluida.
- Respuestas cortas y directas. No des explicaciones largas.
- Responde siempre en español de España, con pronunciación peninsular.
- Evita seseo: pronuncia claramente "c" y "z" como español de España.
- Evita giros, entonación o dejes latinoamericanos.
- Voz adulta, estable y madura.
- Usa una entonación sobria, de locutor telefónico adulto.
- Evita cambios bruscos de tono dentro de una misma frase. No hagas quiebros de voz.
"""

HUB_SYSTEM_INSTRUCTION = f"""
Eres el Asistente virtual de entrenamiento de Dubot. Esta llamada tiene DOS fases.

=== FASE 1: IDENTIFICACIÓN Y SELECCIÓN DE MODO ===
1. Da la bienvenida de forma amable: "Hola, has llamado al asistente virtual de entrenamiento de Dubot. Identifícate con tu código de agente, por favor. Puedes decirlo por voz o introducirlo con el teclado."
2. Cuando pidas o recibas códigos de agente, pide que se digan dígito a dígito si es necesario.
3. Si el usuario da un número de 4 dígitos o una secuencia de cuatro dígitos hablados, llama INMEDIATAMENTE a `verify_agent_code(agent_code=codigo_normalizado)`. No rechaces un código sin llamar a la tool. No inventes resultado.
4. Si el backend devuelve status "invalid", indícalo educadamente y pídele que lo repita o lo marque.
5. Si el código es válido (status "valid"), di: "Estupendo, [Nombre]. ¿Quieres practicar en Trainer o avanzar con tus ciclos?"
6. Escucha la elección:
   - Trainer ("Trainer", "practicar", "simulación", "roleplay", "uno", "1"): llama a `switch_to_trainer_mode()`.
   - Ciclos ("ciclos", "mis ciclos", "continuar", "dos", "2"): llama a `select_cycles_mode()`.
7. Si no entiendes la elección: "No te he entendido bien. Puedes decir 'Trainer' para practicar una simulación o 'ciclos' para continuar con tus ciclos asignados."

=== FASE 2: CÓDIGO DE SIMULACIÓN TRAINER ===
Esta fase se activa cuando el backend confirma que el agente ha seleccionado Trainer.
Cuando estés en esta fase:
1. Tu única labor es obtener el código de la simulación que quiere practicar.
2. Cuando el usuario diga cualquier número, código o secuencia alfanumérica, llama INMEDIATAMENTE a `validate_trainer_simulation_code(code=codigo_normalizado)`. NO esperes, NO digas "lo compruebo" sin llamar a la tool en ese mismo momento. NO inventes el resultado.
3. Si el backend devuelve valid=false, di claramente: "No he encontrado ese código. Puedes repetirlo dígito a dígito o marcarlo con el teclado." y pídelo otra vez.
4. Si es válido, di "Perfecto, iniciamos la simulación." y quédate en silencio mientras se transfiere la llamada.
5. NO pidas código de agente en esta fase. El agente ya está identificado.
6. Si el usuario prefiere marcar el código en el teclado, espera a que lo introduzca con el teclado.

Reglas de pronunciación:
{SPANISH_VOICE_RULES}
"""

TRAINER_CODE_SYSTEM_INSTRUCTION = f"""
Eres el Asistente virtual de entrenamiento de Dubot.
El agente ya está identificado y ha seleccionado realizar una simulación en Trainer.
Tu única labor es obtener el código de simulación:
1. Di: "Perfecto. Para mayor precisión, te recomiendo marcar el código con el teclado de tu teléfono. Si prefieres decirlo por voz, hazlo dígito a dígito."
2. Cuando el usuario proporcione el código (por voz dígito a dígito o por teclado), normalízalo y llama INMEDIATAMENTE a `validate_trainer_simulation_code(code=codigo_normalizado)`. NO esperes ni digas "lo compruebo" sin llamar a la tool en ese mismo turno. NO inventes el resultado.
3. Si el backend devuelve valid=false, di exactamente: "No he encontrado ese código. Por favor, repítelo dígito a dígito o márcalo con el teclado." y espera nueva entrada.
4. Si es válido, di "Perfecto, iniciamos la simulación." y quédate en silencio.
5. NO pidas código de agente. El agente ya está identificado.

Reglas de pronunciación:
{SPANISH_VOICE_RULES}
"""

# ── Gemini Live shared voice config (mirrors training_voice.py exactly) ────────

def build_gemini_live_session_config(gemini_model: str, system_instruction: str, tools: list) -> dict:
    """Build Gemini Live session setup dict identical to training_voice.py."""
    return {
        "model": gemini_model,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": "Algieba"  # Same voice as training_voice.py
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
            "parts": [{"text": system_instruction.strip()}]
        },
        "tools": tools
    }


def normalize_agent_code(raw_text: str) -> Optional[str]:
    """Helper to convert spoken Spanish digits/words into a clean 4-digit code if possible."""
    if not raw_text:
        return None
    
    # Convert to lowercase and replace accents/punctuation
    text = raw_text.lower()
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        ",": " ", ".": " ", "-": " ", "_": " ", "/": " "
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
        
    word_to_digits = {
        # Units
        "cero": ["0"],
        "uno": ["1"],
        "dos": ["2"],
        "tres": ["3"],
        "cuatro": ["4"],
        "cinco": ["5"],
        "seis": ["6"],
        "siete": ["7"],
        "ocho": ["8"],
        "nueve": ["9"],
        
        # Teens / Tens
        "diez": ["1", "0"],
        "once": ["1", "1"],
        "doce": ["1", "2"],
        "trece": ["1", "3"],
        "catorce": ["1", "4"],
        "quince": ["1", "5"],
        "dieciseis": ["1", "6"],
        "diecisiete": ["1", "7"],
        "dieciocho": ["1", "8"],
        "diecinueve": ["1", "9"],
        "veinte": ["2", "0"],
        "veintiuno": ["2", "1"],
        "veintidos": ["2", "2"],
        "veintitres": ["2", "3"],
        "veinticuatro": ["2", "4"],
        "veinticinco": ["2", "5"],
        "veintiseis": ["2", "6"],
        "veintisiete": ["2", "7"],
        "veintiocho": ["2", "8"],
        "veintinueve": ["2", "9"],
        
        # Tens (prefixes)
        "treinta": ["3"],
        "cuarenta": ["4"],
        "cincuenta": ["5"],
        "sesenta": ["6"],
        "setenta": ["7"],
        "ochenta": ["8"],
        "noventa": ["9"],
        
        # Hundreds (prefixes)
        "cien": ["1"],
        "ciento": ["1"],
        "doscientos": ["2"],
        "trescientos": ["3"],
        "cuatrocientos": ["4"],
        "quinientos": ["5"],
        "seiscientos": ["6"],
        "setecientos": ["7"],
        "ochocientos": ["8"],
        "novecientos": ["9"]
    }
    
    words = text.split()
    digits = []
    
    for word in words:
        word = word.strip()
        if not word or word == "y" or word == "mil":
            continue
        
        # If word is entirely composed of digits
        if word.isdigit():
            digits.extend(list(word))
        elif word in word_to_digits:
            digits.extend(word_to_digits[word])
            
    # Join all found digits
    res = "".join(digits)
    
    # If the result has exactly 4 digits, return it
    if len(res) == 4:
        return res
    
    # If there's a 4-digit contiguous block
    import re
    match = re.search(r"\d{4}", res)
    if match:
        return match.group(0)
        
    return None


async def redirect_call(call_sid: str, redirect_url: str) -> bool:
    """Helper to update a Twilio call and redirect it to a new TwiML URL."""
    account_sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
    
    logger.info("Redirect call credentials check: account_sid is %s, auth_token is %s",
                "configured" if account_sid else "NOT configured",
                "configured" if auth_token else "NOT configured")
                
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not configured. Cannot redirect call.")
        return False
    
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    auth = (account_sid, auth_token)
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, auth=auth, data={"Url": redirect_url})
            if response.status_code in (200, 201):
                logger.info("Successfully redirected Twilio call %s to %s", call_sid, redirect_url)
                return True
            else:
                logger.error("Failed to redirect Twilio call %s: status=%d response=%s", call_sid, response.status_code, response.text)
                return False
        except Exception as e:
            logger.error("Failed to redirect Twilio call %s: %s", call_sid, e)
            return False


@router.post("/incoming-call")
async def incoming_call(request: Request):
    """Unified phone assistant entry point."""
    gemini_api_key = getattr(settings, "gemini_live_api_key", None) or getattr(settings, "gemini_api_key", None)
    if not gemini_api_key:
        logger.error("GEMINI_API_KEY / gemini_live_api_key is not configured. Answering call with config error TwiML.")
        twiml_error = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Lo siento, la clave de API de Gemini no está configurada. Por favor, contacta con soporte. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml_error, media_type="application/xml")

    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto", "http")
    scheme = "wss" if proto == "https" or "localhost" not in host else "ws"
    
    ws_url = f"{scheme}://{host}/bm/training/hub/media-stream?flow=hub"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="{ws_url}">
                <Parameter name="flow" value="hub" />
            </Stream>
        </Connect>
    </Response>
    """
    logger.info("Incoming call received at voice training hub.")
    return Response(content=twiml, media_type="application/xml")


@router.post("/collect-agent-dtmf")
async def collect_agent_dtmf(request: Request, call_sid: str = Query(...)):
    """DTMF fallback to gather agent numeric code."""
    action_url = f"/bm/training/hub/verify-agent-dtmf?call_sid={call_sid}"
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


@router.post("/verify-agent-dtmf")
async def verify_agent_dtmf(request: Request, call_sid: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Verify agent numeric code gathered via DTMF."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    
    stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.training_numeric_code == digits)
    res = await db.execute(stmt)
    setting = res.scalars().first()
    
    if setting and setting.is_enabled:
        logger.info("Agent identified via DTMF: agent_id=%s, code=%s", setting.hubspot_owner_id, digits)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
        action_url = f"/bm/training/hub/select-mode-menu?agent_id={setting.hubspot_owner_id}&amp;call_sid={call_sid}"
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect>{action_url}</Redirect>
        </Response>
        """, media_type="application/xml")
    else:
        logger.warning("Invalid agent code entered via DTMF: %s", digits)
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Código de agente incorrecto. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")


@router.post("/select-mode-menu")
async def select_mode_menu(request: Request, agent_id: str = Query(...), call_sid: str = Query(...)):
    """Keypad menu fallback to select between Trainer and Cycles."""
    action_url = f"/bm/training/hub/verify-mode-dtmf?agent_id={agent_id}&amp;call_sid={call_sid}"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="1" timeout="10" action="{action_url}">
            <Say language="es-ES">Por favor, selecciona una opción usando tu teclado. Pulsa 1 para Trainer o pulsa 2 para continuar con tus ciclos asignados.</Say>
        </Gather>
        <Say language="es-ES">No he recibido ninguna selección. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/verify-mode-dtmf")
async def verify_mode_dtmf(request: Request, agent_id: str = Query(...), call_sid: str = Query(...)):
    """Handle DTMF input for mode selection."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    
    if digits == "1":
        logger.info("Agent selected Trainer via DTMF: agent_id=%s", agent_id)
        action_url = f"/bm/training/hub/trainer-init?agent_id={agent_id}&amp;call_sid={call_sid}"
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect>{action_url}</Redirect>
        </Response>
        """, media_type="application/xml")
    elif digits == "2":
        logger.info("Agent selected Cycles via DTMF: agent_id=%s", agent_id)
        action_url = f"/bm/training/hub/cycles-init?agent_id={agent_id}&amp;call_sid={call_sid}"
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect>{action_url}</Redirect>
        </Response>
        """, media_type="application/xml")
    else:
        logger.warning("Invalid mode digit entered via DTMF: %s", digits)
        action_url = f"/bm/training/hub/select-mode-menu?agent_id={agent_id}&amp;call_sid={call_sid}"
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Opción no válida.</Say>
            <Redirect>{action_url}</Redirect>
        </Response>
        """, media_type="application/xml")


@router.post("/trainer-init")
async def trainer_init(request: Request, agent_id: str = Query(...), call_sid: str = Query(...)):
    """Redirect to Trainer code entry stream."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto", "http")
    scheme = "wss" if proto == "https" or "localhost" not in host else "ws"
    
    ws_url = f"{scheme}://{host}/bm/training/hub/media-stream?flow=trainer_code&amp;agent_id={agent_id}"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="{ws_url}">
                <Parameter name="flow" value="trainer_code" />
                <Parameter name="agent_id" value="{agent_id}" />
            </Stream>
        </Connect>
    </Response>
    """
    logger.info("Redirection to Trainer code entry stream for agent_id=%s", agent_id)
    return Response(content=twiml, media_type="application/xml")


@router.post("/collect-simulation-dtmf")
async def collect_simulation_dtmf(request: Request, agent_id: str = Query(...), call_sid: str = Query(...)):
    """DTMF fallback to gather simulation code."""
    action_url = f"/bm/training/hub/verify-simulation-dtmf?agent_id={agent_id}&amp;call_sid={call_sid}"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Gather numDigits="6" timeout="10" action="{action_url}">
            <Say language="es-ES">No he podido entender el código de la simulación. Por favor, introduce el código numérico de la simulación usando el teclado de tu teléfono, terminado en almohadilla.</Say>
        </Gather>
        <Say language="es-ES">No he recibido ningún código. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.post("/verify-simulation-dtmf")
async def verify_simulation_dtmf(request: Request, agent_id: str = Query(...), call_sid: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Verify simulation code entered via DTMF."""
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    
    sim = await TrainerService.validate_simulation_code(db, digits)
    if sim:
        logger.info("Simulation validated via DTMF: sim_id=%s, code=%s", sim.simulation_id, digits)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
        await redirect_trainer_call(call_sid, host, agent_id, sim.simulation_id)
        return Response(content="""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Código verificado. Iniciando simulación.</Say>
        </Response>
        """, media_type="application/xml")
    else:
        logger.warning("Invalid simulation code entered via DTMF: %s", digits)
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Código de simulación incorrecto. La llamada finalizará.</Say>
            <Hangup/>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")


@router.post("/cycles-init")
async def cycles_init(request: Request, agent_id: str = Query(...), call_sid: str = Query(...),
                      agent_name: str = Query(default=""), db: AsyncSession = Depends(get_db)):
    """Redirect to agent active cycles flow."""
    active_cycles = await get_active_cycles_for_agent(db, agent_id)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    
    if not active_cycles:
        logger.info("Cycles selected but agent has 0 active cycles: agent_id=%s", agent_id)
        action_url = f"/bm/training/hub/no-active-cycles"
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect>{action_url}</Redirect>
        </Response>
        """, media_type="application/xml")
        
    if len(active_cycles) == 1:
        logger.info("Redirecting agent %s to single active cycle %s", agent_id, active_cycles[0].training_report_id)
        await redirect_twilio_call(call_sid, host, agent_id, active_cycles[0].training_report_id)
        return Response(content="""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say language="es-ES">Código verificado. Iniciando entrenamiento.</Say>
        </Response>
        """, media_type="application/xml")
    else:
        logger.info("Redirecting agent %s to cycle selection menu (multiple active cycles)", agent_id)
        action_url = f"/bm/training/voice/twilio/select-cycle-menu?agent_id={agent_id}&amp;call_sid={call_sid}"
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect>{action_url}</Redirect>
        </Response>
        """, media_type="application/xml")


@router.post("/no-active-cycles")
async def no_active_cycles(request: Request):
    """Play no active cycles message and hang up."""
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Say language="es-ES">He visto que no tienes ningún entrenamiento en proceso en este momento. Por favor, consulta con tu supervisor. La llamada finalizará.</Say>
        <Hangup/>
    </Response>
    """
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("Accepted training hub media stream connection.")
    
    params = dict(websocket.query_params)
    flow = params.get("flow", "hub")
    agent_id = params.get("agent_id")
    
    agent_name = "Agente"
    agent_initials = ""
    if agent_id:
        async with AsyncSessionLocal() as db:
            stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == agent_id)
            res = await db.execute(stmt)
            setting = res.scalars().first()
            if setting:
                agent_name = setting.agent_name
                agent_initials = setting.agent_initials
    
    gemini_api_key = getattr(settings, "gemini_live_api_key", None) or getattr(settings, "gemini_api_key", None)
    if not gemini_api_key:
        logger.error("GEMINI_API_KEY / gemini_live_api_key is not configured in WebSocket.")
        await websocket.close()
        return

    # ── Tool declarations ──────────────────────────────────────────────────────
    # IMPORTANT: HUB_TOOLS must include ALL tools that may be used throughout
    # the session, because Gemini Live tools cannot be changed mid-session.
    # This includes validate_trainer_simulation_code for Phase 2 (trainer code).
    HUB_TOOLS = [{
        "functionDeclarations": [
            {
                "name": "verify_agent_code",
                "description": "Verifica el código de empleado del agente (ej: LD23, FR45, CM21, EC7). Úsala cuando el agente dé su código de identificación.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "agent_code": {
                            "type": "STRING",
                            "description": "Código de agente hablado o marcado"
                        }
                    },
                    "required": ["agent_code"]
                }
            },
            {
                "name": "switch_to_trainer_mode",
                "description": "El agente ha seleccionado practicar con Trainer. Cambia el modo a Trainer dentro de la misma llamada. Llama a esta función cuando el agente diga 'Trainer', 'uno' o '1'.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "select_cycles_mode",
                "description": "El agente ha seleccionado avanzar con sus ciclos de entrenamiento asignados. Llama cuando diga 'ciclos', 'dos' o '2'.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "validate_trainer_simulation_code",
                "description": "Valida el código de la simulación de Trainer que el agente quiere iniciar. Llama a esta función INMEDIATAMENTE cuando el agente proporcione cualquier código numérico o alfanumérico de simulación en la fase Trainer (ej: '323334', 'SIM101', 'VENTAS2', 'tres dos tres tres tres cuatro'). Normaliza el código antes de enviarlo.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "code": {
                            "type": "STRING",
                            "description": "Código de simulación normalizado (solo dígitos o alfanumérico en mayúsculas)"
                        }
                    },
                    "required": ["code"]
                }
            }
        ]
    }]

    TRAINER_CODE_TOOLS = [{
        "functionDeclarations": [
            {
                "name": "validate_trainer_simulation_code",
                "description": "Valida el código de la simulación de Trainer que el agente quiere iniciar. Llama INMEDIATAMENTE cuando el agente proporcione cualquier código de simulación (ej: SIM101, 323334, VENTAS2). Normaliza el código antes de enviarlo.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "code": {
                            "type": "STRING",
                            "description": "Código de simulación normalizado"
                        }
                    },
                    "required": ["code"]
                }
            }
        ]
    }]

    # Choose instructions and tools based on flow
    if flow == "hub":
        system_instruction = HUB_SYSTEM_INSTRUCTION
        tools_decl = HUB_TOOLS
    elif flow == "trainer_code":
        first_name = agent_name.split()[0] if agent_name else "Agente"
        system_instruction = f"""
Eres el Asistente virtual de entrenamiento de Dubot.
El agente ya está identificado como {agent_name} y ha seleccionado practicar en Trainer.
Tu única labor es obtener el código de simulación:
1. Di: "Perfecto, {first_name}. Dime el código de la simulación que quieres realizar. También puedes marcarlo con el teclado."
2. Cuando el usuario diga cualquier número, código o secuencia alfanumérica, llama INMEDIATAMENTE a `validate_trainer_simulation_code(code=codigo_normalizado)`. NO esperes ni digas "lo compruebo" sin llamar a la tool en ese mismo turno. NO inventes el resultado.
3. Si el backend devuelve valid=false, di "Ese código no es válido. Por favor, dímelo otra vez." y vuelve al punto 1.
4. Si es válido, di "Perfecto, iniciamos la simulación." y quédate en silencio.
5. NO pidas código de agente. El agente ya está identificado.

Reglas de pronunciación:
{SPANISH_VOICE_RULES}
"""
        tools_decl = TRAINER_CODE_TOOLS
    else:
        logger.error("Invalid flow parameter: %s", flow)
        await websocket.close()
        return

    # Connect to Gemini Live API
    gemini_model = getattr(settings, "gemini_live_model", None) or getattr(settings, "gemini_model", None) or "models/gemini-2.0-flash-exp"
    if gemini_model and not gemini_model.startswith("models/"):
        gemini_model = f"models/{gemini_model}"

    uri = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={gemini_api_key}"
    
    session_config = build_gemini_live_session_config(gemini_model, system_instruction, tools_decl)

    try:
        async with websockets.connect(uri) as gemini_ws:
            # Send session configuration
            await gemini_ws.send(json.dumps({"setup": session_config}))
            setup_resp = await gemini_ws.recv()
            logger.info("Established Gemini Live training hub session.")
            
            # Trigger initial greeting automatically
            logger.info("Training Hub initial greeting requested.")
            greet_text = ""
            if flow == "hub":
                greet_text = "Di exactamente: 'Hola, has llamado al asistente virtual de entrenamiento de Dubot. Identifícate con tu código de agente, por favor. Puedes decirlo por voz o introducirlo con el teclado.' y quédate en silencio."
            elif flow == "trainer_code":
                first_name = agent_name.split()[0] if agent_name else "Agente"
                greet_text = f"Di exactamente: 'Perfecto, {first_name}. Pasamos a Trainer de Dubot. Dime el código de la simulación que quieres practicar. También puedes marcarlo con el teclado.' y quédate en silencio."
                
            if greet_text:
                greet_msg = {
                    "clientContent": {
                        "turns": [{
                            "role": "user",
                            "parts": [{"text": greet_text}]
                        }],
                        "turnComplete": True
                    }
                }
                await gemini_ws.send(json.dumps(greet_msg))
            
            call_sid = None
            stream_sid = None
            attempts = 0
            redirected = False
            identified_agent_id = agent_id
            identified_agent_name = agent_name
            dtmf_buffer = ""
            current_state = "awaiting_agent_code" if flow == "hub" else "trainer_code"
            
            async def play_redirection_error():
                logger.error("Twilio redirection failed. Instructing Gemini to play config error and closing.")
                err_msg = {
                    "clientContent": {
                        "turns": [{
                            "role": "user",
                            "parts": [{"text": "Di exactamente: 'Lo siento, hay un problema de configuración de telefonía. Por favor, ponte en contacto con soporte. La llamada finalizará.' y quédate en silencio."}]
                        }],
                        "turnComplete": True
                    }
                }
                await gemini_ws.send(json.dumps(err_msg))
                await asyncio.sleep(4.0)
                await websocket.close()

            async def switch_to_trainer_in_stream(first_name_arg: str):
                """Switch current WebSocket state to trainer_code without redirecting Twilio."""
                nonlocal current_state, dtmf_buffer, attempts
                logger.info("Training Hub trainer mode selected.")
                logger.info("Training Hub state changed: awaiting_mode -> trainer_code")
                current_state = "trainer_code"
                dtmf_buffer = ""
                attempts = 0
                logger.info("Trainer code prompt requested.")
                trainer_prompt_msg = {
                    "clientContent": {
                        "turns": [{
                            "role": "user",
                            "parts": [{"text": f"Di exactamente: 'Perfecto, {first_name_arg}. Pasamos a Trainer de Dubot. Dime el código de la simulación que quieres practicar. También puedes marcarlo con el teclado.' y quédate en silencio."}]
                        }],
                        "turnComplete": True
                    }
                }
                await gemini_ws.send(json.dumps(trainer_prompt_msg))

            async def validate_and_redirect_simulation(sim_code: str):
                """Validate simulation code and redirect to Trainer roleplay. Used by DTMF path."""
                nonlocal redirected, attempts, call_sid
                proto_http = websocket.headers.get("x-forwarded-proto", "http")
                scheme_http = "https" if proto_http == "https" or "localhost" not in (websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost") else "http"
                host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                
                logger.info("Trainer simulation code validation started: code=%s", sim_code)
                async with AsyncSessionLocal() as sub_db:
                    sim = await TrainerService.validate_simulation_code(sub_db, sim_code)
                    if sim and identified_agent_id:
                        logger.info("Trainer simulation validation success: simulation_id=%s, code=%s, name=%s", sim.simulation_id, sim_code, getattr(sim, 'name', 'unknown'))
                        logger.info("Redirecting call to trainer roleplay: agent_id=%s, simulation_id=%s, call_sid=%s", identified_agent_id, sim.simulation_id, call_sid)
                        ok = await redirect_trainer_call(call_sid, host, identified_agent_id, sim.simulation_id)
                        if ok:
                            logger.info("Trainer roleplay redirect success: call_sid=%s", call_sid)
                            redirected = True
                        else:
                            logger.error("Trainer roleplay redirect failed: reason=redirect_trainer_call_returned_false")
                            await play_redirection_error()
                            redirected = True
                    else:
                        logger.warning("Trainer simulation validation failed: code=%s, reason=not_found", sim_code)
                        attempts += 1
                        if attempts >= 3:
                            logger.info("Redirecting to DTMF collect-simulation-dtmf after %d failed attempts.", attempts)
                            await redirect_call(call_sid, f"{scheme_http}://{host}/bm/training/hub/collect-simulation-dtmf?agent_id={identified_agent_id}&call_sid={call_sid}")
                            redirected = True
                        else:
                            err_msg = {
                                "clientContent": {
                                    "turns": [{
                                        "role": "user",
                                        "parts": [{"text": "Di exactamente: 'Código de simulación incorrecto. Por favor, dímelo otra vez o márcalo con el teclado.' y quédate en silencio."}]
                                    }],
                                    "turnComplete": True
                                }
                            }
                            await gemini_ws.send(json.dumps(err_msg))
            
            async def receive_from_twilio():
                nonlocal call_sid, stream_sid, redirected, dtmf_buffer, current_state, identified_agent_id, identified_agent_name, attempts
                try:
                    async for message in websocket.iter_text():
                        if redirected:
                            break
                        data = json.loads(message)
                        event = data.get("event")
                        
                        if event == "start":
                            stream_sid = data["start"]["streamSid"]
                            call_sid = data["start"]["callSid"]
                            logger.info("Twilio stream start: stream_sid=%s, call_sid=%s", stream_sid, call_sid)
                            # Also read customParameters if Twilio delivers them via start event
                            custom_params = data["start"].get("customParameters", {})
                            if custom_params.get("flow") == "trainer_code" and not identified_agent_id:
                                cp_agent_id = custom_params.get("agent_id")
                                if cp_agent_id:
                                    identified_agent_id = cp_agent_id
                                    async with AsyncSessionLocal() as sub_db:
                                        stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == cp_agent_id)
                                        res = await sub_db.execute(stmt)
                                        setting = res.scalars().first()
                                        if setting:
                                            identified_agent_name = setting.agent_name
                        elif event == "media":
                            chunk_b64 = data["media"]["payload"]
                            raw_ulaw = base64.b64decode(chunk_b64)
                            # Convert 8kHz u-law (Twilio) to 16kHz linear PCM (Gemini)
                            raw_pcm = audioop.ulaw2lin(raw_ulaw, 2)
                            raw_pcm_16k, _ = audioop.ratecv(raw_pcm, 2, 1, 8000, 16000, None)
                            
                            if not getattr(websocket, "logged_audio_payload_info", False):
                                setattr(websocket, "logged_audio_payload_info", True)
                                logger.info("Sending realtime audio to Gemini using v1beta audio payload | mime=audio/pcm;rate=16000 | stream_sid=%s | call_sid=%s", stream_sid, call_sid)
                                
                            gemini_msg = {
                                "realtimeInput": {
                                    "audio": {
                                        "mimeType": "audio/pcm;rate=16000",
                                        "data": base64.b64encode(raw_pcm_16k).decode("utf-8")
                                    }
                                }
                            }
                            await gemini_ws.send(json.dumps(gemini_msg))
                        elif event == "dtmf":
                            digit = data["dtmf"]["digit"]
                            logger.info("Training Hub DTMF received: digit=%s", digit)
                            
                            proto_http = websocket.headers.get("x-forwarded-proto", "http")
                            scheme_http = "https" if proto_http == "https" or "localhost" not in (websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost") else "http"
                            host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                            
                            if current_state == "awaiting_agent_code":
                                dtmf_buffer += digit
                                logger.info("Training Hub DTMF buffer updated: state=awaiting_agent_code, length=%d", len(dtmf_buffer))
                                if len(dtmf_buffer) == 4:
                                    logger.info("Training Hub DTMF agent code completed: code=%s", dtmf_buffer)
                                    async with AsyncSessionLocal() as sub_db:
                                        agent = await TrainerService.validate_agent_code(sub_db, dtmf_buffer)
                                        if agent:
                                            identified_agent_id = agent["agent_id"]
                                            identified_agent_name = agent["agent_name"]
                                            first_name = agent["agent_name"].split()[0]
                                            logger.info("Training Hub agent validation success via DTMF: agent_id=%s, name=%s", agent["agent_id"], agent["agent_name"])
                                            current_state = "awaiting_mode"
                                            dtmf_buffer = ""
                                            
                                            # Instruct Gemini to speak the success prompt
                                            greet_msg = {
                                                "clientContent": {
                                                    "turns": [{
                                                        "role": "user",
                                                        "parts": [{"text": f"Di exactamente: 'Estupendo, {first_name}. ¿Quieres practicar en Trainer o avanzar con tus ciclos?'"}]
                                                    }],
                                                    "turnComplete": True
                                                }
                                            }
                                            await gemini_ws.send(json.dumps(greet_msg))
                                        else:
                                            logger.warning("Training Hub agent validation failed via DTMF: code=%s", dtmf_buffer)
                                            dtmf_buffer = ""
                                            attempts += 1
                                            if attempts >= 2:
                                                logger.info("Redirecting to DTMF collect-agent-dtmf after %d failed attempts.", attempts)
                                                await redirect_call(call_sid, f"{scheme_http}://{host}/bm/training/hub/collect-agent-dtmf?call_sid={call_sid}")
                                                redirected = True
                                                break
                                            else:
                                                err_msg = {
                                                    "clientContent": {
                                                        "turns": [{
                                                            "role": "user",
                                                            "parts": [{"text": "Di exactamente: 'Código de agente incorrecto. Por favor, dilo de nuevo o márcalo con el teclado.' y quédate en silencio."}]
                                                        }],
                                                        "turnComplete": True
                                                    }
                                                }
                                                await gemini_ws.send(json.dumps(err_msg))
                                                
                            elif current_state == "awaiting_mode":
                                if digit == "1":
                                    # ── TRAINER: Stay in same WebSocket, change state ──────────────────
                                    first_name = identified_agent_name.split()[0] if identified_agent_name else "Agente"
                                    await switch_to_trainer_in_stream(first_name)
                                    # Do NOT redirect, do NOT break — continue the same stream
                                elif digit == "2":
                                    logger.info("Training Hub DTMF mode selected: cycles")
                                    ok = await redirect_call(call_sid, f"{scheme_http}://{host}/bm/training/hub/cycles-init?agent_id={identified_agent_id}&call_sid={call_sid}")
                                    if not ok:
                                        await play_redirection_error()
                                    redirected = True
                                    break
                                    
                            elif current_state == "trainer_code":
                                if digit == "#":
                                    sim_code = dtmf_buffer
                                    dtmf_buffer = ""
                                    await validate_and_redirect_simulation(sim_code)
                                else:
                                    dtmf_buffer += digit
                                    logger.info("Training Hub DTMF buffer updated: state=trainer_code, length=%d", len(dtmf_buffer))
                                    if len(dtmf_buffer) == 6:
                                        sim_code = dtmf_buffer
                                        dtmf_buffer = ""
                                        await validate_and_redirect_simulation(sim_code)
                        elif event == "stop":
                            logger.info("Twilio stream stop.")
                            break
                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected.")
                except Exception as e:
                    logger.error("Error in receive_from_twilio: %s", e)

            async def send_to_twilio():
                nonlocal call_sid, stream_sid, attempts, redirected, identified_agent_id, identified_agent_name, current_state
                proto_http = websocket.headers.get("x-forwarded-proto", "http")
                scheme_http = "https" if proto_http == "https" or "localhost" not in (websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost") else "http"
                logged_initial_greeting_sent = False
                logged_trainer_code_prompt_sent = False
                
                try:
                    async for response_str in gemini_ws:
                        if redirected:
                            break
                        data = json.loads(response_str)
                        
                        # Forward audio from Gemini to Twilio
                        if "serverContent" in data:
                            if not logged_initial_greeting_sent and flow == "hub":
                                logged_initial_greeting_sent = True
                                logger.info("Training Hub initial greeting sent by Gemini.")
                            elif not logged_trainer_code_prompt_sent and (flow == "trainer_code" or current_state == "trainer_code"):
                                logged_trainer_code_prompt_sent = True
                                logger.info("Trainer code prompt sent by Gemini.")
                            parts = data["serverContent"].get("modelTurn", {}).get("parts", [])
                            for part in parts:
                                mime = part.get("inlineData", {}).get("mimeType", "")
                                if "audio/pcm" in mime:
                                    raw_pcm_24k = base64.b64decode(part["inlineData"]["data"])
                                    # Convert 24kHz linear PCM (Gemini) to 8kHz u-law (Twilio)
                                    raw_pcm_8k, _ = audioop.ratecv(raw_pcm_24k, 2, 1, 24000, 8000, None)
                                    raw_ulaw = audioop.lin2ulaw(raw_pcm_8k, 2)
                                    
                                    twilio_msg = {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {
                                            "payload": base64.b64encode(raw_ulaw).decode("utf-8")
                                        }
                                    }
                                    await websocket.send_text(json.dumps(twilio_msg))
                                    
                        elif "toolCall" in data:
                            calls = data["toolCall"].get("functionCalls", [])
                            for call in calls:
                                fid = call.get("id")
                                name = call.get("name")
                                args = call.get("args", {})
                                
                                logger.info("Gemini Live toolCall request: %s", name)
                                
                                if name == "verify_agent_code" and current_state == "awaiting_agent_code":
                                    raw_code = args.get("agent_code", "").strip()
                                    normalized = normalize_agent_code(raw_code) or raw_code
                                    logger.info("Training Hub agent code received: raw=%r, normalized=%r", raw_code, normalized)
                                    
                                    async with AsyncSessionLocal() as sub_db:
                                        agent = await TrainerService.validate_agent_code(sub_db, normalized)
                                        if agent:
                                            identified_agent_id = agent["agent_id"]
                                            identified_agent_name = agent["agent_name"]
                                            current_state = "awaiting_mode"
                                            logger.info("Training Hub agent validation success: agent_id=%s, initials=%s, name=%s", 
                                                        agent["agent_id"], agent["agent_initials"], agent["agent_name"])
                                            result_val = {"status": "valid", "agent_name": agent["agent_name"]}
                                        else:
                                            logger.warning("Training Hub agent validation failed: normalized=%r, reason=\"not_found\"", normalized)
                                            attempts += 1
                                            if attempts >= 2:
                                                logger.info("Redirecting to DTMF after %d failed voice validation attempts. call_sid=%s", attempts, call_sid)
                                                host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                                await redirect_call(call_sid, f"{scheme_http}://{host}/bm/training/hub/collect-agent-dtmf?call_sid={call_sid}")
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
                                    
                                elif name == "switch_to_trainer_mode" and current_state == "awaiting_mode":
                                    # ── Trainer selected by voice: stay in same WebSocket ──────────────
                                    first_name = identified_agent_name.split()[0] if identified_agent_name else "Agente"
                                    
                                    # Acknowledge the tool call first
                                    resp_msg = {
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id": fid,
                                                "name": name,
                                                "response": {"result": {"status": "ok"}}
                                            }]
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(resp_msg))
                                    
                                    # Then switch state and send prompt
                                    await switch_to_trainer_in_stream(first_name)

                                elif name == "select_cycles_mode" and current_state == "awaiting_mode":
                                    if identified_agent_id:
                                        host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                        logger.info("Redirecting call to cycles-init: agent_id=%s", identified_agent_id)
                                        ok = await redirect_call(call_sid, f"{scheme_http}://{host}/bm/training/hub/cycles-init?agent_id={identified_agent_id}&call_sid={call_sid}")
                                        
                                        if not ok:
                                            logger.error("Twilio redirection failed. Instructing Gemini to play config error and closing.")
                                            err_msg = {
                                                "clientContent": {
                                                    "turns": [{
                                                        "role": "user",
                                                        "parts": [{"text": "Di exactamente: 'Lo siento, hay un problema de configuración de telefonía. Por favor, ponte en contacto con soporte. La llamada finalizará.' y quédate en silencio."}]
                                                    }],
                                                    "turnComplete": True
                                                }
                                            }
                                            await gemini_ws.send(json.dumps(err_msg))
                                            await asyncio.sleep(4.0)
                                            await websocket.close()
                                            return
                                            
                                        redirected = True
                                        return
                                    else:
                                        resp_msg = {
                                            "toolResponse": {
                                                "functionResponses": [{
                                                    "id": fid,
                                                    "name": name,
                                                    "response": {"result": {"status": "invalid"}}
                                                }]
                                            }
                                        }
                                        await gemini_ws.send(json.dumps(resp_msg))

                                elif name == "validate_trainer_simulation_code" and current_state == "trainer_code":
                                    # ── Simulation code validation (voice path) ───────────────────────
                                    sim_code = args.get("code", "").strip()
                                    logger.info("Trainer simulation code received: raw=%r, normalized=%r", sim_code, sim_code)
                                    logger.info("Trainer simulation code validation started: code=%s", sim_code)
                                    async with AsyncSessionLocal() as sub_db:
                                        sim = await TrainerService.validate_simulation_code(sub_db, sim_code)
                                        if sim and identified_agent_id:
                                            host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                            logger.info("Trainer simulation validation success: simulation_id=%s, code=%s, name=%s",
                                                        sim.simulation_id, sim_code, getattr(sim, "name", "unknown"))
                                            logger.info("Redirecting call to trainer roleplay: agent_id=%s, simulation_id=%s, call_sid=%s",
                                                        identified_agent_id, sim.simulation_id, call_sid)
                                            ok = await redirect_trainer_call(call_sid, host, identified_agent_id, sim.simulation_id)
                                            if ok:
                                                logger.info("Trainer roleplay redirect success: call_sid=%s", call_sid)
                                                redirected = True
                                                return
                                            else:
                                                logger.error("Trainer roleplay redirect failed: reason=redirect_trainer_call_returned_false")
                                                err_msg = {
                                                    "clientContent": {
                                                        "turns": [{
                                                            "role": "user",
                                                            "parts": [{"text": "Di exactamente: 'Lo siento, hay un problema de configuración de telefonía. Por favor, ponte en contacto con soporte. La llamada finalizará.' y quédate en silencio."}]
                                                        }],
                                                        "turnComplete": True
                                                    }
                                                }
                                                await gemini_ws.send(json.dumps(err_msg))
                                                await asyncio.sleep(4.0)
                                                await websocket.close()
                                                return
                                        else:
                                            logger.warning("Trainer simulation validation failed: code=%s, reason=not_found", sim_code)
                                            attempts += 1
                                            if attempts >= 3:
                                                host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host") or "localhost"
                                                await redirect_call(call_sid, f"{scheme_http}://{host}/bm/training/hub/collect-simulation-dtmf?agent_id={identified_agent_id}&call_sid={call_sid}")
                                                redirected = True
                                                return
                                            result_val = {"valid": False, "attempts": attempts}
                                            
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
                except Exception as e:
                    logger.error("Error in send_to_twilio: %s", e)

            # Run Twilio receive and send loops concurrently
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            
    except Exception as e:
        logger.error("Exception in training hub WebSocket: %s", e)
    finally:
        logger.info("Closing training hub WebSocket.")
        try:
            await websocket.close()
        except Exception:
            pass
