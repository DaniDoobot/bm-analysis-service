import base64
import os
import sys

# Add current directory to path for imports
sys.path.insert(0, os.path.abspath("."))

from app.routers.training_voice import decode_twilio_to_gemini, encode_gemini_to_twilio

def main():
    print("=== TEST: TRANSCODIFICACION DE AUDIO ===")
    
    # 1. Test decode_twilio_to_gemini (8kHz mulaw -> 16kHz pcm)
    # Generate 160 bytes of dummy µ-law audio (representing 20ms of audio at 8kHz)
    dummy_mulaw = b"\xff" * 160
    mulaw_b64 = base64.b64encode(dummy_mulaw).decode("utf-8")
    
    pcm_b64, state = decode_twilio_to_gemini(mulaw_b64, None)
    
    assert pcm_b64 is not None
    pcm_bytes = base64.b64decode(pcm_b64)
    print(f"Decodificación exitosa: mulaw bytes={len(dummy_mulaw)} -> PCM bytes={len(pcm_bytes)} (esperado cerca de 640)")
    assert abs(len(pcm_bytes) - 640) <= 8
    
    # 2. Test encode_gemini_to_twilio (24kHz pcm -> 8kHz mulaw)
    dummy_pcm24k = b"\x00\x00" * 480
    pcm24k_b64 = base64.b64encode(dummy_pcm24k).decode("utf-8")
    
    mulaw8k_b64, state2 = encode_gemini_to_twilio(pcm24k_b64, None)
    
    assert mulaw8k_b64 is not None
    mulaw8k_bytes = base64.b64decode(mulaw8k_b64)
    print(f"Codificación exitosa: PCM24k bytes={len(dummy_pcm24k)} -> mulaw8k bytes={len(mulaw8k_bytes)} (esperado cerca de 160)")
    assert abs(len(mulaw8k_bytes) - 160) <= 4
    
    print("[OK] Pruebas de transcodificacion de audio superadas con exito.")

if __name__ == "__main__":
    main()
