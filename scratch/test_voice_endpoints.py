import asyncio
import os
import sys
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from app.main import app

def test_twilio_incoming_call():
    client = TestClient(app)
    # 1. Test incoming-call TwiML
    response = client.post("/bm/training/voice/twilio/incoming-call")
    assert response.status_code == 200
    content = response.text
    print("=== incoming-call Response ===")
    print(content)
    # Check that it uses Stream and Parameter
    assert "<Connect>" in content
    assert "<Stream" in content
    assert '<Parameter name="flow" value="identify"' in content
    # Check that flow=identify is NOT a query parameter on Stream url
    assert "?flow=identify" not in content
    print("[OK] incoming-call TwiML verified.")

# To test WebSocket, we need to mock the Gemini connection.
# The WebSocket handler calls:
# async with websockets.connect(gemini_url) as gemini_ws:
# We patch websockets.connect to return a mock gemini_ws.

async def test_websocket_media_stream_identify():
    client = TestClient(app)
    
    # Create mock gemini_ws
    mock_gemini_ws = AsyncMock()
    mock_gemini_ws.send = AsyncMock()
    # Mocking async iterator for gemini_ws
    # When iterating over gemini_ws, it receives messages from Gemini.
    # We can simulate sending a setupComplete message first
    mock_gemini_ws.__aiter__.return_value = ["{\"setupComplete\": {}}"]
    
    from unittest.mock import MagicMock
    
    with patch("websockets.connect") as mock_connect:
        # We need to mock the async context manager returned by websockets.connect
        mock_context_manager = MagicMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_gemini_ws)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)
        mock_connect.return_value = mock_context_manager
        
        # Connect to media-stream WebSocket using TestClient
        # We don't pass flow/session_id as query parameters.
        with client.websocket_connect("/bm/training/voice/twilio/media-stream") as ws:
            # Send the initial Twilio "connected" event
            connected_event = {
                "event": "connected",
                "protocol": "Call",
                "version": "1.0.0"
            }
            ws.send_text(json.dumps(connected_event))
            
            # Send an arbitrary pre-start event to make sure the server ignores it
            dummy_event = {
                "event": "dummy",
                "some": "value"
            }
            ws.send_text(json.dumps(dummy_event))

            # Send the Twilio "start" event JSON containing the customParameters
            start_event = {
                "event": "start",
                "sequenceNumber": "1",
                "start": {
                    "accountSid": "ACmock",
                    "streamSid": "MZmock",
                    "callSid": "CAmock",
                    "customParameters": {
                        "flow": "identify"
                    }
                }
            }
            ws.send_text(json.dumps(start_event))
            
            # Since the mock gemini_ws will yield "setupComplete" on iteration, 
            # the backend will process it and send the initial greeting turn to Gemini
            # Let's wait a bit to let async tasks execute
            await asyncio.sleep(0.5)
            
            # Verify websockets.connect was called with gemini URL
            mock_connect.assert_called_once()
            args, kwargs = mock_connect.call_args
            assert "generativelanguage.googleapis.com" in args[0]
            
            # Verify the Setup Configuration was sent to Gemini
            # Locate all calls to mock_gemini_ws.send and check if setup is present
            called_setup = False
            for call in mock_gemini_ws.send.call_args_list:
                msg = json.loads(call[0][0])
                if "setup" in msg:
                    called_setup = True
                    setup = msg["setup"]
                    assert "Identificación" in setup["systemInstruction"]["parts"][0]["text"]
                    assert setup["tools"][0]["functionDeclarations"][0]["name"] == "verify_agent_code"
            
            assert called_setup, "Gemini setup message was not sent"
            print("[OK] WebSocket media-stream identify verified.")

async def run_all():
    print("=== RUNNING TWILIO VOICE INTEGRATION ENDPOINT TESTS ===")
    test_twilio_incoming_call()
    await test_websocket_media_stream_identify()
    print("=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    asyncio.run(run_all())
