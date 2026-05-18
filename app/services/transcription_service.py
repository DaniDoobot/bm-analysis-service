"""
Transcription service — orchestrates audio download + Whisper transcription.
"""
import logging

from app.services.hubspot_service import HubSpotService
from app.services.twilio_service import TwilioService
from app.services import openai_service

logger = logging.getLogger(__name__)


async def transcribe_call(call_id: str) -> dict:
    """
    Full pipeline: HubSpot call → recording URL → download audio → transcribe.

    Returns:
        {
            "text": "...",
            "model": "whisper-1",
            "provider": "openai",
            "call_metadata": {...},
        }
    """
    hubspot = HubSpotService()
    twilio = TwilioService()

    call_meta = await hubspot.get_call(call_id)
    recording_url = call_meta.get("recording_url")

    if not recording_url:
        raise ValueError(f"No recording URL found for call_id={call_id}")

    # Enrich agent name if possible
    owner_id = call_meta.get("hubspot_owner_id")
    if owner_id:
        name = await hubspot.get_owner_name(owner_id)
        if name:
            call_meta["agente_telefonico"] = name

    audio_bytes = await twilio.download_audio(recording_url)
    transcription_result = await openai_service.transcribe_audio(audio_bytes, filename="call.mp3")

    return {
        **transcription_result,
        "call_metadata": call_meta,
    }
