import sys
import os
import asyncio
from unittest.mock import patch, AsyncMock
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import SessionLocal
from app.services.criteria_service import generate_criterion_description_ai

class DummyBody:
    def __init__(self, name, item_type, out_key, feed_key, current_desc, instr, typology_keys=None):
        self.criterion_name = name
        self.criterion_type = item_type
        self.output_key = out_key
        self.feed_key = feed_key
        self.current_description = current_desc
        self.instruction = instr
        self.typology_keys = typology_keys or []

async def test_sanitization_and_validations():
    print("=== TESTING AI DESCRIPTION SANITIZATION & VALIDATIONS ===")
    
    # Test case 1: score_1_10 with JSON leak and missing rubric details
    body = DummyBody(
        name="Prueba Cercanía",
        item_type="score_1_10",
        out_key="prueba_cercania",
        feed_key="prueba_cercania_feed",
        current_desc="Evalúa cercanía.",
        instr="Quiero que evalúe si el agente es cercano."
    )
    
    # Mock response containing a JSON block and output_key labels, and missing medium/low score details
    mock_llm_response = """
    Aquí tienes la descripción:
    Evalúa la cercanía del agente con el paciente.
    {
      "output_key": "prueba_cercania",
      "feed_key": "prueba_cercania_feed"
    }
    Puntúa alto (9-10) si habla amablemente.
    No se detalla nada más.
    """
    
    with patch("app.services.openai_service.complete_text", return_value=mock_llm_response):
        async with SessionLocal() as db:
            res = await generate_criterion_description_ai(db, 371, body)
            print("Response:", res)
            
            assert res["ok"] == True
            # Verify JSON block was removed
            assert "{" not in res["description"]
            assert "}" not in res["description"]
            # Verify technical labels were removed
            assert "output_key:" not in res["description"]
            assert "feed_key:" not in res["description"]
            
            # Verify warnings about missing rubrics are present
            warnings = res["warnings"]
            print("Warnings generated:", warnings)
            assert any("puntuación media" in w for w in warnings)
            assert any("puntuación baja" in w for w in warnings)
            assert any("justificar" in w for w in warnings)
            
    print("Sanitization and validation tests passed successfully!\n")

async def test_boolean_sanitization():
    print("=== TESTING BOOLEAN SANITIZATION ===")
    
    body = DummyBody(
        name="Verifica Cita",
        item_type="boolean",
        out_key="verifica_cita",
        feed_key=None,
        current_desc="Evalúa cita.",
        instr="Quiero ver si verifica la cita."
    )
    
    mock_llm_response = """
    Determina si el agente verifica los detalles de la cita.
    Responde con Si si lo hace, con No si no lo hace, o null si no es aplicable.
    """
    
    with patch("app.services.openai_service.complete_text", return_value=mock_llm_response):
        async with SessionLocal() as db:
            res = await generate_criterion_description_ai(db, 999, body)
            print("Response:", res)
            assert res["ok"] == True
            assert len(res["warnings"]) == 0
            assert "Si" in res["description"]
            
    print("Boolean sanitization tests passed successfully!\n")

async def test_endpoint_no_id():
    print("=== TESTING ENDPOINT WITHOUT CRITERION_ID ===")
    from fastapi.testclient import TestClient
    from app.main import app
    
    client = TestClient(app)
    payload = {
        "instruction": "Quiero que sea muy cercano y empático.",
        "current_description": "",
        "criterion_name": "Prueba Cercanía",
        "criterion_type": "score_1_10",
        "output_key": "prueba_cercania",
        "feed_key": "prueba_cercania_feed",
        "service_id": 1,
        "typology_keys": ["cita", "confirmacion"]
    }
    
    mock_llm_response = """
    Evalúa la empatía.
    Puntúa alto (9-10) si es muy cercano.
    Puntúa medio (5-8) si es formal.
    Puntúa bajo (0-4) si es borde.
    Justifica en prueba_cercania_feed.
    """
    
    with patch("app.services.openai_service.complete_text", return_value=mock_llm_response):
        # Test 1: POST /bm/prompt-criteria/ai-description
        response = client.post("/bm/prompt-criteria/ai-description", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] == True
        assert "empatía" in data["description"]
        assert len(data["warnings"]) == 0
        print("Sub-test /bm/prompt-criteria/ai-description passed!")
        
        # Test 2: POST /bm/criteria/ai-description (alias)
        response_alias = client.post("/bm/criteria/ai-description", json=payload)
        assert response_alias.status_code == 200
        data_alias = response_alias.json()
        assert data_alias["ok"] == True
        assert "empatía" in data_alias["description"]
        assert len(data_alias["warnings"]) == 0
        print("Sub-test /bm/criteria/ai-description alias passed!")
        
    print("Endpoint without criterion_id tests passed successfully!\n")

async def main():
    await test_sanitization_and_validations()
    await test_boolean_sanitization()
    await test_endpoint_no_id()

if __name__ == "__main__":
    asyncio.run(main())

