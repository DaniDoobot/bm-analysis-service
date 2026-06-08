import sys
import os
import asyncio
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select, delete
from app.db import SessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.typologies import Typology
from app.schemas.criteria import SaveCriterionRequest
from app.services.criteria_service import save_criterion, toggle_criterion, delete_criterion, update_criterion_typologies
from app.services.transcription_analysis_service import analyze_transcription_pipeline
from app.services.prompts_service import get_active_prompt, _get_current_version
from unittest.mock import patch

# Configure logging to see the warning output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_regression")

async def run_regression_tests():
    async with SessionLocal() as db:
        print("\n=== STARTING CRITERION MUTATION REGRESSION TESTS ===")
        
        # 0. Setup and check prompt 23
        prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == 23))
        prompt = prompt_res.scalars().first()
        if not prompt:
            print("Prompt 23 not found in DB! Skipping regression.")
            return

        # Pre-cleanup leftover test criteria
        print("Cleaning up any leftover test criteria from previous runs...")
        cleanup_stmt = select(PromptCriterion).where(
            PromptCriterion.prompt_id == 23,
            PromptCriterion.criterion_key == "test_nuevo_criterio"
        )
        cleanup_res = await db.execute(cleanup_stmt)
        for c in cleanup_res.scalars().all():
            print(f"Deleting leftover test criterion ID {c.criterion_id}...")
            await db.execute(delete(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id == c.criterion_id))
            await db.delete(c)
        await db.commit()

        # 1. Create a new criterion
        print("\n--- Step 1: Creating new criterion 'test_nuevo_criterio' ---")
        save_req = SaveCriterionRequest(
            prompt_id=23,
            criterion_key="test_nuevo_criterio",
            criterion_name="Test Nuevo Criterio",
            criterion_description="Este es un criterio de prueba automatizado.",
            criterion_type="score_1_10",
            output_key="test_nuevo_criterio",
            feed_key="test_nuevo_criterio_feed",
            order_index=150,
            is_required=False,
            is_active=True
        )
        
        # Save criterion via service (this will auto-sync)
        created_crit = await save_criterion(db, save_req)
        print(f"Created Criterion ID: {created_crit.criterion_id} | Key: {created_crit.criterion_key}")
        
        # Associate it with 'cita' typology using the service
        t_res = await db.execute(select(Typology).where(Typology.service_id == prompt.service_id, Typology.typology_key == "cita"))
        cita_typology = t_res.scalars().first()
        if cita_typology:
            print(f"Associating with typology 'cita' (ID: {cita_typology.typology_id}) using service")
            await update_criterion_typologies(db, created_crit.criterion_id, [cita_typology.typology_id])
        else:
            print("Typology 'cita' not found, skipping association.")

        # Refresh prompt text
        version_curr = await _get_current_version(db, 23)
        prompt_text = version_curr.prompt or ""
        
        # Assertions
        assert "test_nuevo_criterio" in prompt_text, "Criterio description block missing in prompt text!"
        
        # Locate format section
        import re
        header_pattern = re.compile(
            r"^(?:###?\s+)?(?:FORMATO\s+DE\s+(?:RESPUESTA|SALIDA(?:\s+JSON)?))\b",
            re.IGNORECASE | re.MULTILINE
        )
        matches = list(header_pattern.finditer(prompt_text))
        format_section = prompt_text[matches[-1].start():] if matches else ""
        
        assert "test_nuevo_criterio" in format_section, "Criterio output_key missing in JSON format block!"
        assert "test_nuevo_criterio_feed" in format_section, "Criterio feed_key missing in JSON format block!"
        print("Step 1 PASSED: Criterion is in DB, prompt text, and JSON format block.")

        # 2. Mock evaluation (Missing Key)
        print("\n--- Step 2: Running mock transcription analysis (omitted key) ---")
        mock_response = '{"tipo_llamada": "cita", "empatia": 8}'
        with patch("app.services.openai_service.complete_text", return_value=mock_response):
            # We expect the Defensive Keys Guard to log structured warnings for test_nuevo_criterio
            # and inject it as None.
            result = await analyze_transcription_pipeline(
                db=db,
                call_id="call_omitted_test_123",
                transcription="Hola, buenos días. Quería cita.",
                prompt_id=23
            )
            assert result["ok"] == True
            res_keys = result["result"]
            assert "test_nuevo_criterio" in res_keys
            assert res_keys["test_nuevo_criterio"] is None
            assert "test_nuevo_criterio_feed" in res_keys
            assert res_keys["test_nuevo_criterio_feed"] is None
            print("Step 2 PASSED: Missing keys successfully handled by Defensive Guard and logged.")

        # 3. Mock evaluation (Present Key)
        print("\n--- Step 3: Running mock transcription analysis (present key) ---")
        mock_response_full = '{"tipo_llamada": "cita", "empatia": 8, "test_nuevo_criterio": 7, "test_nuevo_criterio_feed": "Explicación adecuada."}'
        with patch("app.services.openai_service.complete_text", return_value=mock_response_full):
            result = await analyze_transcription_pipeline(
                db=db,
                call_id="call_present_test_123",
                transcription="Hola, buenos días. Quería cita.",
                prompt_id=23
            )
            assert result["ok"] == True
            res_keys = result["result"]
            assert res_keys["test_nuevo_criterio"] == 7
            assert res_keys["test_nuevo_criterio_feed"] == "Explicación adecuada."
            print("Step 3 PASSED: Present keys successfully saved and mapped.")

        try:
            await toggle_criterion(db, created_crit.criterion_id, is_active=False)
            await db.commit()
        except Exception as ex:
            if hasattr(ex, "val_result"):
                print(f"Validation error occurred: {ex.val_result}")
            raise


        # Verify deactivated criterion is removed from active prompt and JSON output format
        version_curr = await _get_current_version(db, 23)
        prompt_text_deact = version_curr.prompt or ""
        
        matches_deact = list(header_pattern.finditer(prompt_text_deact))
        format_section_deact = prompt_text_deact[matches_deact[-1].start():] if matches_deact else ""
        
        assert "test_nuevo_criterio" not in format_section_deact, "Criterio output_key still present in JSON format block after deactivation!"
        # Check that it doesn't appear as a definition header
        assert "- [score_1_10] Test Nuevo Criterio" not in prompt_text_deact, "Criterio definition header still present in prompt text after deactivation!"
        print("Step 4 PASSED: Deactivated criterion successfully removed from prompt text and JSON format block.")

        try:
            await toggle_criterion(db, created_crit.criterion_id, is_active=True)
            await db.commit()
        except Exception as ex:
            if hasattr(ex, "val_result"):
                print(f"Validation error occurred: {ex.val_result}")
            raise


        # Verify reactivated criterion is back
        version_curr = await _get_current_version(db, 23)
        prompt_text_react = version_curr.prompt or ""
        
        matches_react = list(header_pattern.finditer(prompt_text_react))
        format_section_react = prompt_text_react[matches_react[-1].start():] if matches_react else ""
        
        assert "test_nuevo_criterio" in format_section_react, "Criterio output_key missing in JSON format block after reactivation!"
        assert "- [score_1_10] Test Nuevo Criterio" in prompt_text_react, "Criterio definition header missing in prompt text after reactivation!"
        print("Step 5 PASSED: Reactivated criterion successfully restored to prompt text and JSON format block.")

        # Clean up: Hard delete the test criterion so DB remains clean
        print("\n--- Cleaning up: deleting test criterion ---")
        await delete_criterion(db, created_crit.criterion_id)
        await db.commit()
        print("Cleanup completed.")
        
        print("\n=== ALL CRITERION MUTATION REGRESSION TESTS PASSED SUCCESSFULLY! ===")

if __name__ == "__main__":
    asyncio.run(run_regression_tests())
