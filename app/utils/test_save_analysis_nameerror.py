"""
Test script to prevent NameError in save_analysis function.
Ensures that the individual analysis flow works end-to-end,
correctly importing and using SQLAlchemy's select() function.
"""
import asyncio
import logging
import sys
import os
import uuid
from sqlalchemy import select, delete

# Add parent directory to path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db import get_engine, SessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.analyses import Analysis, AnalysisCriterionResult, CallAnalysisCurrent
from app.services.analysis_persistence import save_analysis

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

async def test_save_analysis_no_nameerror():
    logger.info("=== Running Save Analysis NameError Prevention Test ===")
    
    async with SessionLocal() as db:
        # Fetch an existing active prompt to avoid duplicate criteria constraints
        stmt = select(Prompt).where(Prompt.is_active == True, Prompt.is_archived == False).limit(1)
        res = await db.execute(stmt)
        prompt = res.scalar()
        
        if not prompt:
            logger.info("No active prompt found, creating a temporary mock prompt...")
            unique_suffix = str(uuid.uuid4())[:8]
            prompt = Prompt(prompt_name=f"Mock Prompt {unique_suffix}", prompt_type="text")
            db.add(prompt)
            await db.flush()
            
            v = PromptVersion(prompt_id=prompt.prompt_id, prompt="Mock prompt content", is_current=True)
            db.add(v)
            await db.flush()
            
            c = PromptCriterion(
                prompt_id=prompt.prompt_id, 
                criterion_key="tipo_llamada", 
                criterion_type="text", 
                output_key="tipo_llamada",
                criterion_name="Tipo de llamada"
            )
            db.add(c)
            await db.flush()
            await db.commit()
            
            v_id = v.id
            prompt_id = prompt.prompt_id
        else:
            prompt_id = prompt.prompt_id
            v_stmt = select(PromptVersion).where(PromptVersion.prompt_id == prompt_id, PromptVersion.is_current == True).limit(1)
            v_res = await db.execute(v_stmt)
            version = v_res.scalar()
            if not version:
                v_stmt = select(PromptVersion).where(PromptVersion.prompt_id == prompt_id).limit(1)
                v_res = await db.execute(v_stmt)
                version = v_res.scalar()
            v_id = version.id if version else None

        logger.info("Using Prompt ID: %s, Version ID: %s", prompt_id, v_id)
        
        call_id = f"test_prevention_call_{uuid.uuid4().hex[:10]}"
        
        call_metadata = {
            "call_id": call_id,
            "hubspot_url": "https://app.hubspot.com/contacts/mock/call/mock",
            "call_direction": "INBOUND",
            "agente_telefonico": "Prevention Agent Test",
            "hubspot_owner_id": "99999"
        }
        
        prompt_metadata = {
            "prompt_id": prompt_id,
            "prompt_version_id": v_id
        }
        
        model_metadata = {
            "model_provider": "openai",
            "model_name": "gpt-4o"
        }
        
        result_json = {
            "tipo_llamada": "cita",
            "evaluacion_global": 9.0,
            "observaciones": "Llamada de prevención de NameError."
        }
        
        payload = {"prevention_test": True}
        
        analysis_id = None
        try:
            logger.info("Executing save_analysis...")
            analysis = await save_analysis(
                db=db,
                analysis_type="text",
                call_metadata=call_metadata,
                prompt_metadata=prompt_metadata,
                model_metadata=model_metadata,
                result_json=result_json,
                payload=payload
            )
            analysis_id = analysis.analysis_id
            logger.info("save_analysis executed successfully without NameError! Created ID: %s", analysis_id)
            
            # Assertions to verify correct persistence
            assert analysis_id is not None, "Analysis ID must not be None"
            
            # Check bm_analyses table
            res_a = await db.execute(select(Analysis).where(Analysis.analysis_id == analysis_id))
            saved_a = res_a.scalar()
            assert saved_a is not None, "Analysis row not found in bm_analyses"
            assert saved_a.call_id == call_id, "Call ID mismatch"
            assert saved_a.status == "completed", "Status must be completed"
            
            # Check bm_call_analysis_current table
            res_c = await db.execute(select(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == call_id))
            saved_c = res_c.scalar()
            assert saved_c is not None, "Current analysis row not found in bm_call_analysis_current"
            assert saved_c.latest_analysis_id == analysis_id, "Latest analysis ID mismatch"
            
            # Check bm_analysis_criterion_results table
            res_r = await db.execute(select(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == analysis_id))
            saved_r = res_r.scalars().all()
            assert len(saved_r) > 0, "No criterion results saved in bm_analysis_criterion_results"
            
            logger.info("All DB records successfully verified!")
            
        except NameError as ne:
            logger.error("NameError detected during save_analysis: %s", ne)
            raise ne
        except Exception as e:
            logger.error("Unexpected error during save_analysis validation: %s", e)
            raise e
        finally:
            logger.info("Cleaning up E2E temporary test records...")
            if analysis_id:
                await db.execute(delete(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == analysis_id))
            await db.execute(delete(CallAnalysisCurrent).where(CallAnalysisCurrent.call_id == call_id))
            if analysis_id:
                await db.execute(delete(Analysis).where(Analysis.analysis_id == analysis_id))
            await db.commit()
            logger.info("Cleanup completed successfully.")
            
    logger.info("=== Save Analysis NameError Prevention Test Passed! ===")

if __name__ == "__main__":
    asyncio.run(test_save_analysis_no_nameerror())
