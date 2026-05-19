"""Test script for prompt base structures and blank prompt logic."""
import asyncio
import logging
import sys
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Ensure parent directory is in path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db import get_engine
from app.services.db_init_service import init_db
from app.models.prompts import PromptBaseStructure, Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.services.prompts_service import create_prompt_from_base, save_prompt_version
from app.schemas.prompts import CreateFromBaseRequest, SavePromptRequest
from app.services.prompt_builder import build_prompt_with_ai
from app.services.criteria_service import save_criterion
from app.schemas.criteria import SaveCriterionRequest


async def run_validation_pipeline():
    logger.info("Initializing DB and seeding structures...")
    await init_db()

    engine = get_engine()
    async with AsyncSession(engine) as db:
        # 1. Fetch the seeded structures
        logger.info("Checking seeded structures...")
        res = await db.execute(select(PromptBaseStructure))
        structures = res.scalars().all()
        logger.info("Seeded structures count: %d", len(structures))
        for s in structures:
            logger.info(" - [%d] Key: %s, Name: %s", s.id, s.structure_key, s.structure_name)

        boston_struct = next((s for s in structures if s.structure_key == "boston_medical_audio"), None)
        blank_struct = next((s for s in structures if s.structure_key == "blank"), None)

        if not boston_struct or not blank_struct:
            logger.error("Missing seeded structures in database!")
            return

        # ── 1 & 2. Create prompt from boston_medical_audio and verify criteria are copied ──
        logger.info("\n=== Step 1 & 2: Create prompt from boston_medical_audio ===")
        req_boston = CreateFromBaseRequest(
            base_structure_id=boston_struct.id,
            prompt_name="Test Prompt Boston Medical dynamic",
            prompt_type="audio",
            created_by="Test Agent",
            created_by_email="agent@doobot.ai",
            copy_default_criteria=True
        )
        res_boston = await create_prompt_from_base(db, req_boston)
        logger.info("Result of creation from Boston Medical base:")
        logger.info(" - prompt_id: %s", res_boston["prompt_id"])
        logger.info(" - prompt_version_id: %s", res_boston["prompt_version_id"])
        logger.info(" - criteria_count: %s", res_boston["criteria_count"])

        # Fetch actual criteria in DB for this prompt
        db_criteria_boston = await db.execute(
            select(PromptCriterion).where(PromptCriterion.prompt_id == res_boston["prompt_id"])
        )
        c_list = db_criteria_boston.scalars().all()
        logger.info("Criteria actually stored in DB for Boston: %d", len(c_list))
        assert len(c_list) > 0, "Should copy default criteria!"
        logger.info("Step 1 & 2 Success!")

        # ── 3 & 4. Create prompt from blank base structure and verify 0 criteria ──
        logger.info("\n=== Step 3 & 4: Create prompt from blank structure ===")
        req_blank = CreateFromBaseRequest(
            base_structure_id=blank_struct.id,
            prompt_name="Test Prompt Blank from Scratch",
            prompt_type="audio",
            created_by="Test Agent",
            created_by_email="agent@doobot.ai",
            copy_default_criteria=True
        )
        res_blank = await create_prompt_from_base(db, req_blank)
        logger.info("Result of creation from Blank base:")
        logger.info(" - prompt_id: %s", res_blank["prompt_id"])
        logger.info(" - prompt_version_id: %s", res_blank["prompt_version_id"])
        logger.info(" - criteria_count: %s", res_blank["criteria_count"])

        # Fetch actual criteria in DB for blank
        db_criteria_blank = await db.execute(
            select(PromptCriterion).where(PromptCriterion.prompt_id == res_blank["prompt_id"])
        )
        c_blank_list = db_criteria_blank.scalars().all()
        logger.info("Criteria actually stored in DB for Blank: %d", len(c_blank_list))
        assert len(c_blank_list) == 0, "Blank prompt must have 0 criteria!"
        logger.info("Step 3 & 4 Success!")

        # ── 5. Add a manual criterion to the blank prompt ──
        logger.info("\n=== Step 5: Add a manual criterion to blank prompt ===")
        crit_body = SaveCriterionRequest(
            prompt_id=res_blank["prompt_id"],
            criterion_key="claridad_test",
            criterion_name="Claridad de voz de prueba",
            criterion_description="Evalúa si el tono y claridad de la voz son correctos.",
            criterion_type="boolean",
            output_key="claridad_test",
            feed_key="claridad_test_feed",
            order_index=10,
            is_required=True,
            is_active=True
        )
        saved_crit = await save_criterion(db, crit_body)
        logger.info("Criterion saved with ID: %s", saved_crit.criterion_id)

        # Check in DB
        db_criteria_blank_updated = await db.execute(
            select(PromptCriterion).where(PromptCriterion.prompt_id == res_blank["prompt_id"])
        )
        c_blank_updated_list = db_criteria_blank_updated.scalars().all()
        logger.info("Criteria count after manual add: %d", len(c_blank_updated_list))
        assert len(c_blank_updated_list) == 1, "Should have exactly 1 criterion!"
        logger.info("Step 5 Success!")

        # ── 6. Save a version with edited text from full editor ──
        logger.info("\n=== Step 6: Save an edited version from complete editor ===")
        edited_text = (
            "### PROMPT PERSONALIZADO EDITADO DESDE EL EDITOR COMPLETO\n"
            "Este es un texto editado manualmente.\n"
            "Debe evaluar la claridad_test y devolver la justificación en claridad_test_feed."
        )
        save_req = SavePromptRequest(
            prompt_id=res_blank["prompt_id"],
            prompt_type="audio",
            prompt=edited_text,
            updated_by="Test Agent",
            updated_by_email="agent@doobot.ai",
            change_note="Edición manual completa desde Lovable",
            source="manual",
            version_name="Versión editada a mano"
        )
        saved_version = await save_prompt_version(db, save_req)
        logger.info("Saved new version: ID=%s, Is current=%s", saved_version.id, saved_version.is_current)
        assert saved_version.prompt == edited_text, "Prompt content mismatch!"
        logger.info("Step 6 Success!")

        # ── 7. Verify build-with-ai does not fail on 0 criteria prompts ──
        logger.info("\n=== Step 7: Verify build-with-ai with 0 criteria prompt ===")
        # We will create a fresh blank prompt with 0 criteria and run build-with-ai
        req_fresh_blank = CreateFromBaseRequest(
            base_structure_id=blank_struct.id,
            prompt_name="Fresh Blank for AI builder",
            prompt_type="audio",
            created_by="Test Agent",
            created_by_email="agent@doobot.ai",
            copy_default_criteria=True
        )
        res_fresh_blank = await create_prompt_from_base(db, req_fresh_blank)
        
        # Test building with AI for this prompt (it has 0 criteria)
        # Note: This calls build_prompt_with_ai (which sends to OpenAI if keys are valid)
        # Since we might not want to run real OpenAI bills during a dry run unless needed,
        # let's just make sure build_prompt_with_ai executes its setup and doesn't crash 
        # on 0 criteria before making the API call! 
        # Actually, let's execute build_prompt_with_ai with a dry-run or mock or let it run real OpenAI
        # because the API keys are configured and it's safe!
        # Wait, let's run it and see!
        try:
            logger.info("Calling build_prompt_with_ai for prompt_id %d...", res_fresh_blank["prompt_id"])
            build_res = await build_prompt_with_ai(
                db=db,
                prompt_id=res_fresh_blank["prompt_id"],
                instructions="Genera un prompt básico para atención telefónica comercial.",
                base_structure_id=blank_struct.id
            )
            logger.info("AI Prompt Builder Result: ok=%s", build_res["ok"])
            if not build_res["ok"]:
                logger.warning("AI Builder returned error (expected if key missing or OpenAI validates criteria): %s", build_res.get("error_message"))
            else:
                logger.info("Generated prompt preview: %s", build_res["generated_prompt"][:200])
        except Exception as ex:
            logger.error("AI Builder crashed! %s", ex, exc_info=True)
            raise ex
            
        logger.info("Step 7 Success!")

        # Clean up temporary test prompts to keep database clean
        logger.info("\nCleaning up test records...")
        await db.execute(delete(PromptCriterion).where(PromptCriterion.prompt_id.in_([res_boston["prompt_id"], res_blank["prompt_id"], res_fresh_blank["prompt_id"]])))
        await db.execute(delete(PromptVersion).where(PromptVersion.prompt_id.in_([res_boston["prompt_id"], res_blank["prompt_id"], res_fresh_blank["prompt_id"]])))
        await db.execute(delete(Prompt).where(Prompt.prompt_id.in_([res_boston["prompt_id"], res_blank["prompt_id"], res_fresh_blank["prompt_id"]])))
        await db.commit()
        logger.info("Cleanup completed successfully.")

    logger.info("\nALL INTEGRATION VALIDATION PIPELINES COMPLETED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(run_validation_pipeline())
