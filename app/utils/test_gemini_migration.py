"""Integration test suite for Gemini Flash migration."""
import asyncio
import os
import sys
from datetime import datetime, timezone

# Add workspace directory to path
sys.path.append(".")

from app.config import get_settings
from app.services.ai_provider import get_ai_provider, GeminiProvider, AzureOpenAIProvider

def make_dummy_wav() -> bytes:
    import struct
    num_samples = 4000  # 0.5s silent
    data_bytes = b'\x00\x00' * num_samples
    byte_rate = 8000 * 2
    block_align = 2
    
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + len(data_bytes),
        b'WAVE',
        b'fmt ',
        16,
        1,  # PCM
        1,  # Mono
        8000,  # sample rate
        byte_rate,
        block_align,
        16,  # bits per sample
        b'data',
        len(data_bytes)
    )
    return header + data_bytes

async def test_gemini_workflow():
    settings = get_settings()
    print("=== INICIANDO PRUEBAS DE MIGRACION A GEMINI FLASH ===")
    
    # 1. Verify Provider Resolution
    print("\nTest 1: Verifying active provider resolution...")
    provider = get_ai_provider()
    assert isinstance(provider, GeminiProvider), f"Expected GeminiProvider, got {type(provider)}"
    print("[OK] get_ai_provider() successfully resolved to GeminiProvider.")

    # 2. Test Text Completion (complete_text)
    print("\nTest 2: Testing text completion (complete_text)...")
    messages = [
        {"role": "system", "content": "Eres un asistente servicial que responde de forma extremadamente concisa."},
        {"role": "user", "content": "Dis hola y el nombre de un color primario."}
    ]
    resp = await provider.complete_text(
        messages=messages,
        temperature=0.1,
        response_format=None
    )
    print(f"Response: {resp}")
    assert len(resp) > 0, "Response is empty"
    assert "hola" in resp.lower(), "Expected response to contain 'hola'"
    print("[OK] complete_text returned a valid response.")

    # 3. Test Text Completion with Structured JSON
    print("\nTest 3: Testing structured JSON completion (complete_text)...")
    messages_json = [
        {"role": "system", "content": "Devuelve un objeto JSON con dos campos: 'nombre' (un color) y 'es_primario' (boolean)."},
        {"role": "user", "content": "Dame la respuesta para el color azul."}
    ]
    resp_json = await provider.complete_text(
        messages=messages_json,
        temperature=0.1,
        response_format="json_object"
    )
    print(f"JSON Response: {resp_json}")
    import json
    parsed = json.loads(resp_json)
    assert isinstance(parsed, dict)
    assert "nombre" in parsed
    assert parsed.get("es_primario") is True
    print("[OK] complete_text successfully generated structured JSON.")

    # 4. Test Multimodal Audio Bytes (analyze_audio_bytes)
    print("\nTest 4: Testing multimodal audio analysis (analyze_audio_bytes)...")
    audio_bytes = make_dummy_wav()
    prompt = (
        "Analiza este audio silencioso. Devuelve un objeto JSON con los siguientes campos:\n"
        "1. 'tipo_llamada': 'cita'\n"
        "2. 'evaluacion_global': 10.0\n"
        "3. 'transcription': 'Silencio.'"
    )
    analysis_resp = await provider.analyze_audio_bytes(
        audio_bytes=audio_bytes,
        prompt_text=prompt,
        audio_format="wav"
    )
    print(f"Analysis Response: {analysis_resp}")
    parsed_analysis = json.loads(analysis_resp)
    assert isinstance(parsed_analysis, dict)
    assert parsed_analysis.get("tipo_llamada") == "cita"
    assert float(parsed_analysis.get("evaluacion_global")) == 10.0
    print("[OK] analyze_audio_bytes successfully analyzed audio and returned correct JSON keys.")

    # 5. Test Audio Transcription (transcribe_audio)
    print("\nTest 5: Testing audio transcription (transcribe_audio)...")
    trans_resp = await provider.transcribe_audio(
        audio_bytes=audio_bytes,
        filename="call.wav"
    )
    print(f"Transcription Response: {trans_resp}")
    assert isinstance(trans_resp, dict)
    assert "text" in trans_resp
    assert trans_resp.get("provider") == "gemini"
    print("[OK] transcribe_audio successfully processed audio transcription.")

    # 6. Test Error Handling (Missing API Key)
    print("\nTest 6: Testing error handling for missing API key...")
    original_key = settings.gemini_api_key
    try:
        settings.gemini_api_key = ""
        try:
            get_ai_provider()
            failed_to_raise = True
        except ValueError as ve:
            print(f"Expected resolution error caught: {ve}")
            failed_to_raise = False
        assert not failed_to_raise, "Expected ValueError when GEMINI_API_KEY is missing"
    finally:
        settings.gemini_api_key = original_key
    print("[OK] Provider error handling validated.")

    print("\n=== TODAS LAS PRUEBAS DE MIGRACION HAN PASADO CON EXITO ===")

if __name__ == "__main__":
    asyncio.run(test_gemini_workflow())
