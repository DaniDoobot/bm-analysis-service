import asyncio
import os
import sys
import json
from datetime import datetime, timezone
from sqlalchemy import select, and_, delete
from unittest.mock import AsyncMock, patch, MagicMock

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from app.main import app
from app.db import get_engine
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession
)
from app.routers.training_voice import handle_verify_agent_code

async def setup_test_data(db: AsyncSession):
    # 1. Ensure Agent Setting exists with code 'TS99'
    stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "test_owner_ts99")
    res_set = await db.execute(stmt_set)
    setting = res_set.scalar()
    
    if not setting:
        setting = TrainingAgentSetting(
            hubspot_owner_id="test_owner_ts99",
            agent_name="Fernanda Test",
            agent_initials="FT",
            is_enabled=True,
            training_code="TS99",
            training_numeric_code="9999",
            training_code_enabled=True
        )
        db.add(setting)
    else:
        setting.training_code = "TS99"
        setting.training_numeric_code = "9999"
        setting.training_code_enabled = True
        setting.is_enabled = True
    
    await db.flush()
    
    # Clean up any old cycles for test_owner_ts99
    stmt_cycles = select(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id == "test_owner_ts99")
    res_cycles = await db.execute(stmt_cycles)
    for c in res_cycles.scalars().all():
        await db.execute(delete(TrainingCompletionStatus).where(TrainingCompletionStatus.training_report_id == c.training_report_id))
        await db.execute(delete(TrainingSimulationPrompt).where(TrainingSimulationPrompt.training_report_id == c.training_report_id))
        await db.execute(delete(TrainingCallSession).where(TrainingCallSession.cycle_id == c.training_report_id))
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.training_report_id == c.training_report_id))
    
    await db.flush()

    # 2. Create Cycle 1 (May 1 to May 17)
    report1 = TrainingAgentReport(
        hubspot_owner_id="test_owner_ts99",
        agent_name="Fernanda Test",
        agent_initials="FT",
        period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 17, tzinfo=timezone.utc),
        status="running",
        is_current=True,
        evaluations_count=4,
        calls_count=4,
        avg_evaluacion_global=7.0,
        general_objectives_json=[],
        specific_objectives_json=[]
    )
    db.add(report1)
    await db.flush()
    
    pr1 = TrainingSimulationPrompt(
        training_report_id=report1.training_report_id,
        hubspot_owner_id="test_owner_ts99",
        prompt_number=1,
        title="Roleplay 1 Cycle 1",
        scenario_type="roleplay",
        prompt_text="Scenario 1"
    )
    db.add(pr1)
    await db.flush()
    
    comp1 = TrainingCompletionStatus(
        training_report_id=report1.training_report_id,
        simulation_prompt_id=pr1.simulation_prompt_id,
        hubspot_owner_id="test_owner_ts99",
        status="pending"
    )
    db.add(comp1)

    report1_id = report1.training_report_id
    
    # 3. Create Cycle 2 (May 18 to May 31)
    report2 = TrainingAgentReport(
        hubspot_owner_id="test_owner_ts99",
        agent_name="Fernanda Test",
        agent_initials="FT",
        period_start=datetime(2026, 5, 18, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 31, tzinfo=timezone.utc),
        status="running",
        is_current=True,
        evaluations_count=4,
        calls_count=4,
        avg_evaluacion_global=7.5,
        general_objectives_json=[],
        specific_objectives_json=[]
    )
    db.add(report2)
    await db.flush()
    report2_id = report2.training_report_id
    
    pr2 = TrainingSimulationPrompt(
        training_report_id=report2_id,
        hubspot_owner_id="test_owner_ts99",
        prompt_number=1,
        title="Roleplay 1 Cycle 2",
        scenario_type="roleplay",
        prompt_text="Scenario 2"
    )
    db.add(pr2)
    await db.flush()
    
    comp2 = TrainingCompletionStatus(
        training_report_id=report2_id,
        simulation_prompt_id=pr2.simulation_prompt_id,
        hubspot_owner_id="test_owner_ts99",
        status="pending"
    )
    db.add(comp2)
    
    await db.commit()
    print("Test data setup complete.")
    return report1_id, report2_id

async def cleanup_test_data(db: AsyncSession):
    stmt_cycles = select(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id == "test_owner_ts99")
    res_cycles = await db.execute(stmt_cycles)
    for c in res_cycles.scalars().all():
        await db.execute(delete(TrainingCompletionStatus).where(TrainingCompletionStatus.training_report_id == c.training_report_id))
        await db.execute(delete(TrainingSimulationPrompt).where(TrainingSimulationPrompt.training_report_id == c.training_report_id))
        await db.execute(delete(TrainingCallSession).where(TrainingCallSession.cycle_id == c.training_report_id))
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.training_report_id == c.training_report_id))
    
    await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == "test_owner_ts99"))
    await db.commit()
    print("Test data cleaned up successfully.")

async def run_tests():
    print("=== TEST: SELECCION DE CICLOS POR VOZ ===")
    
    engine = get_engine()
    async with AsyncSession(engine) as db:
        c1_id, c2_id = await setup_test_data(db)
    await engine.dispose()
    
    try:
        # 1. Test handle_verify_agent_code output directly
        print("\n--- Probando handle_verify_agent_code directamente ---")
        mock_websocket = MagicMock()
        mock_websocket.headers = {"host": "localhost:8000"}
        
        status_res = await handle_verify_agent_code(
            agent_code="TS99",
            call_sid="CAmock123",
            websocket=mock_websocket,
            attempts=0
        )
        
        print("Resultado de handle_verify_agent_code:", json.dumps(status_res, indent=2))
        assert status_res["result"]["status"] == "multiple_cycles"
        assert status_res["result"]["agent_name"] == "Fernanda"
        assert len(status_res["result"]["cycles"]) == 2
        assert status_res["agent_id"] == "test_owner_ts99"
        
        cycles_list = status_res["result"]["cycles"]
        assert "18" in cycles_list[0]["period_text"]
        assert "1" in cycles_list[1]["period_text"]
        print("[OK] handle_verify_agent_code retorno exitoso y formateado.")

        # 2. Test WebSocket media-stream integration
        print("\n--- Probando WebSocket media-stream de selección ---")
        client = TestClient(app)
        
        # We need to mock the Gemini connection
        mock_gemini_ws = AsyncMock()
        mock_gemini_ws.send = AsyncMock()
        
        # When iterating over gemini_ws, we will yield:
        # - setupComplete
        # - verify_agent_code toolCall
        # - select_training_cycle toolCall
        # - followed by sleep
        async def mock_gemini_messages(*args, **kwargs):
            yield '{"setupComplete": {}}'
            await asyncio.sleep(0.1)
            
            # Send verify_agent_code toolCall from Gemini
            verify_call = {
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_v1",
                        "name": "verify_agent_code",
                        "args": {"agent_code": "TS99"}
                    }]
                }
            }
            yield json.dumps(verify_call)
            await asyncio.sleep(0.2)
            
            # Send select_training_cycle toolCall from Gemini
            select_call = {
                "toolCall": {
                    "functionCalls": [{
                        "id": "call_s1",
                        "name": "select_training_cycle",
                        "args": {"cycle_id": c1_id}
                    }]
                }
            }
            yield json.dumps(select_call)
            await asyncio.sleep(3600)
            
        mock_gemini_ws.__aiter__ = mock_gemini_messages
        
        # Mock websockets.connect context manager
        mock_context_manager = MagicMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_gemini_ws)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)
        
        mock_verify_res = {
            "attempts": 0,
            "agent_id": "test_owner_ts99",
            "result": {
                "status": "multiple_cycles",
                "agent_name": "Fernanda",
                "cycles": [
                    {"cycle_id": c1_id, "index": 1, "period_text": "del 1 al 17 de mayo"},
                    {"cycle_id": c2_id, "index": 2, "period_text": "del 18 al 31 de mayo"}
                ]
            },
            "redirected": False
        }
        
        # Mock redirect_twilio_call to not perform actual network requests to Twilio
        with patch("websockets.connect", return_value=mock_context_manager) as mock_connect, \
             patch("app.routers.training_voice.handle_verify_agent_code", new_callable=AsyncMock, return_value=mock_verify_res) as mock_verify, \
             patch("app.routers.training_voice.redirect_twilio_call", new_callable=AsyncMock) as mock_redirect:
             
            with client.websocket_connect("/bm/training/voice/twilio/media-stream") as ws:
                # Send the Twilio "connected" event
                ws.send_text(json.dumps({"event": "connected"}))
                
                # Send the Twilio "start" event with flow = identify
                start_event = {
                    "event": "start",
                    "sequenceNumber": "1",
                    "start": {
                        "accountSid": "ACmock",
                        "streamSid": "MZmock",
                        "callSid": "CAmock123",
                        "customParameters": {
                            "flow": "identify"
                        }
                    }
                }
                ws.send_text(json.dumps(start_event))
                
                # Wait for the async loops to process our mocked messages
                await asyncio.sleep(1.0)
                
                # Verify that websockets.connect was called
                mock_connect.assert_called_once()
                
                # Verify setup configuration was sent, including our tools
                setup_called = False
                verify_tool_response_sent = False
                select_tool_response_sent = False
                
                for call in mock_gemini_ws.send.call_args_list:
                    msg = json.loads(call[0][0])
                    if "setup" in msg:
                        setup_called = True
                        tools = msg["setup"]["tools"][0]["functionDeclarations"]
                        tool_names = [t["name"] for t in tools]
                        assert "verify_agent_code" in tool_names
                        assert "select_training_cycle" in tool_names
                        print("[OK] Herramientas verify_agent_code y select_training_cycle registradas en Gemini.")
                    elif "toolResponse" in msg:
                        responses = msg["toolResponse"]["functionResponses"]
                        for r in responses:
                            if r["name"] == "verify_agent_code":
                                verify_tool_response_sent = True
                                assert r["response"]["result"]["status"] == "multiple_cycles"
                                print("[OK] Respuesta del tool verify_agent_code contiene 'multiple_cycles'.")
                            elif r["name"] == "select_training_cycle":
                                select_tool_response_sent = True
                                assert r["response"]["result"]["status"] == "redirecting"
                                print("[OK] Respuesta del tool select_training_cycle contiene 'redirecting'.")
                                
                assert setup_called
                assert verify_tool_response_sent
                assert select_tool_response_sent
                
                # Verify redirect_twilio_call was invoked with correct arguments
                mock_redirect.assert_called_once_with("CAmock123", "testserver", "test_owner_ts99", c1_id)
                print("[OK] Redirección física de llamada Twilio gatillada correctamente.")

    finally:
        engine = get_engine()
        async with AsyncSession(engine) as db:
            await cleanup_test_data(db)
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_tests())
