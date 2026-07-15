import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db import AsyncSessionLocal
from sqlalchemy import select
from app.models.trainer import TrainerSession, TrainerEvaluation
from app.services.trainer_service import TrainerService
from decimal import Decimal

async def repair():
    async with AsyncSessionLocal() as db:
        # Find all sessions with evaluation_status = 'completed_without_score'
        stmt = select(TrainerSession).where(TrainerSession.evaluation_status == 'completed_without_score')
        res = await db.execute(stmt)
        sessions = res.scalars().all()
        
        print(f"Found {len(sessions)} sessions with status 'completed_without_score'")
        repaired_count = 0
        
        for sess in sessions:
            # Get the evaluation
            stmt_eval = select(TrainerEvaluation).where(TrainerEvaluation.session_id == sess.session_id)
            res_eval = await db.execute(stmt_eval)
            evaluation = res_eval.scalars().first()
            if not evaluation:
                print(f"Session {sess.session_id} has no evaluation record. Skipping.")
                continue
                
            # Try to map details using TrainerService to see if a score can be derived
            sess.evaluation = evaluation
            await TrainerService._map_session_evaluation_details(db, sess)
            
            derived_score = sess.__dict__.get("score")
            if derived_score is not None:
                # Update TrainerEvaluation.score
                evaluation.score = Decimal(str(round(derived_score, 2)))
                # Update TrainerSession.evaluation_status
                sess.evaluation_status = "evaluated"
                
                # Check summary fallback
                if not evaluation.summary:
                    result_json = evaluation.result_json or {}
                    feedback_parts = [
                        str(v).strip()
                        for k, v in result_json.items()
                        if (k.startswith("feedback_") or k.endswith("_feedback") or k.endswith("_fb"))
                        and isinstance(v, str) and v.strip()
                    ]
                    if feedback_parts:
                        evaluation.summary = " ".join(feedback_parts[:3])
                        print(f"Generated fallback summary for session {sess.session_id}")

                print(f"Repaired Session {sess.session_id}: evaluation_status -> 'evaluated', score -> {evaluation.score}")
                repaired_count += 1
                
        if repaired_count > 0:
            await db.commit()
            print(f"Successfully committed {repaired_count} repaired sessions.")
        else:
            print("No sessions needed repair.")

if __name__ == "__main__":
    asyncio.run(repair())
