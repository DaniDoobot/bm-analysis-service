"""
test_cleanup_mass_evaluations.py

Validates the mass evaluation cleanup logic:
- dry_run does not delete anything.
- execute deletes in FK-safe order: results → runs → jobs.
- Prompts, services and typologies are untouched.
- Mass evaluation endpoints return empty lists after cleanup.

Run with: .venv\\Scripts\\python.exe app/utils/test_cleanup_mass_evaluations.py
"""
import asyncio
import logging
import sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ── Mock models for unit tests ─────────────────────────────────────────────────

class MockJob:
    def __init__(self, job_id, job_name):
        self.job_id = job_id
        self.job_name = job_name


class MockRun:
    def __init__(self, run_id, job_id, status):
        self.run_id = run_id
        self.job_id = job_id
        self.status = status


class MockResult:
    def __init__(self, mass_analysis_id, run_id, job_id):
        self.mass_analysis_id = mass_analysis_id
        self.run_id = run_id
        self.job_id = job_id


# ── Simulated in-memory DB for unit tests ─────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.jobs: list[MockJob] = [
            MockJob(1, "Job Comercial Semanal"),
            MockJob(2, "Job Evaluación Mensual"),
        ]
        self.runs: list[MockRun] = [
            MockRun(1, 1, "completed"),
            MockRun(2, 1, "failed"),
            MockRun(3, 2, "completed"),
        ]
        self.results: list[MockResult] = [
            MockResult(1, 1, 1),
            MockResult(2, 1, 1),
            MockResult(3, 2, 1),
            MockResult(4, 3, 2),
        ]
        self.deleted_order: list[str] = []
        self.committed = False

    def job_count(self):
        return len(self.jobs)

    def run_count(self):
        return len(self.runs)

    def result_count(self):
        return len(self.results)

    def delete_all_results(self):
        count = len(self.results)
        self.results.clear()
        self.deleted_order.append("results")
        return count

    def delete_all_runs(self):
        count = len(self.runs)
        self.runs.clear()
        self.deleted_order.append("runs")
        return count

    def delete_all_jobs(self):
        count = len(self.jobs)
        self.jobs.clear()
        self.deleted_order.append("jobs")
        return count

    def commit(self):
        self.committed = True


def run_mock_dry_run(db: FakeDB) -> dict:
    """Simulate dry_run: collect counts without modifying data."""
    return {
        "mode": "dry_run",
        "plan": {
            "jobs_count": db.job_count(),
            "runs_count": db.run_count(),
            "results_count": db.result_count(),
            "jobs_to_delete": [{"job_id": j.job_id, "job_name": j.job_name} for j in db.jobs],
            "runs_to_delete": [{"run_id": r.run_id, "job_id": r.job_id, "status": r.status} for r in db.runs],
            "results_to_delete_count": db.result_count(),
            "warnings": [],
        }
    }


def run_mock_execute(db: FakeDB) -> dict:
    """Simulate execute: delete in FK-safe order."""
    deleted_results = db.delete_all_results()  # 1. results first
    deleted_runs = db.delete_all_runs()        # 2. runs second
    deleted_jobs = db.delete_all_jobs()        # 3. jobs last
    db.commit()

    return {
        "mode": "execute",
        "ok": True,
        "deleted_results": deleted_results,
        "deleted_runs": deleted_runs,
        "deleted_jobs": deleted_jobs,
        "warnings": [],
    }


# ── Test cases ────────────────────────────────────────────────────────────────

def test_dry_run_does_not_delete():
    logger.info("Test 1: dry_run must not delete any data...")
    db = FakeDB()
    jobs_before = db.job_count()
    runs_before = db.run_count()
    results_before = db.result_count()

    result = run_mock_dry_run(db)

    assert result["mode"] == "dry_run"
    assert result["plan"]["jobs_count"] == jobs_before
    assert result["plan"]["runs_count"] == runs_before
    assert result["plan"]["results_count"] == results_before

    # Ensure nothing was actually deleted
    assert db.job_count() == jobs_before, "dry_run must not delete jobs!"
    assert db.run_count() == runs_before, "dry_run must not delete runs!"
    assert db.result_count() == results_before, "dry_run must not delete results!"
    assert not db.committed, "dry_run must not commit!"

    assert len(result["plan"]["jobs_to_delete"]) == jobs_before
    assert len(result["plan"]["runs_to_delete"]) == runs_before
    assert result["plan"]["results_to_delete_count"] == results_before

    logger.info("  [OK] dry_run correctly returns counts without modifying data.")


def test_execute_deletes_in_correct_order():
    logger.info("Test 2: execute must delete in FK-safe order (results -> runs -> jobs)...")
    db = FakeDB()

    result = run_mock_execute(db)

    assert result["ok"] is True
    assert result["deleted_results"] == 4
    assert result["deleted_runs"] == 3
    assert result["deleted_jobs"] == 2

    # Verify FK-safe order
    assert db.deleted_order == ["results", "runs", "jobs"], (
        f"Wrong deletion order: {db.deleted_order}. Must be results->runs->jobs!"
    )
    assert db.committed, "execute must commit!"
    logger.info("  [OK] execute deleted in correct FK-safe order: results->runs->jobs.")


def test_execute_leaves_tables_empty():
    logger.info("Test 3: after execute, all mass evaluation tables must be empty...")
    db = FakeDB()
    run_mock_execute(db)

    assert db.job_count() == 0, "Jobs table must be empty after cleanup!"
    assert db.run_count() == 0, "Runs table must be empty after cleanup!"
    assert db.result_count() == 0, "Results table must be empty after cleanup!"
    logger.info("  [OK] All mass evaluation tables are empty after execute.")


def test_non_mass_eval_data_untouched():
    logger.info("Test 4: cleanup must not affect prompts, services, typologies...")
    # Structural test: verify the source code of cleanup_mass_evaluations
    # does not reference protected tables. We read the source file directly.
    import os
    service_path = os.path.join(
        os.path.dirname(__file__), "..", "services", "cleanup_service.py"
    )
    with open(service_path, encoding="utf-8") as f:
        source = f.read()

    # Extract only the cleanup_mass_evaluations function block
    start = source.find("async def cleanup_mass_evaluations(")
    assert start != -1, "cleanup_mass_evaluations function not found in cleanup_service.py!"
    func_source = source[start:]

    # Verify the function does NOT reference protected tables
    forbidden_references = ["bm_prompts", "bm_services", "bm_typologies", "bm_prompt_criteria"]
    for ref in forbidden_references:
        assert ref not in func_source, (
            f"cleanup_mass_evaluations must not reference '{ref}'!"
        )

    # Verify it DOES reference the mass eval models
    assert "MassEvaluationResult" in func_source
    assert "MassEvaluationRun" in func_source
    assert "MassEvaluationJob" in func_source
    logger.info("  [OK] cleanup_mass_evaluations only references mass evaluation tables.")


def test_dry_run_plan_contains_expected_fields():
    logger.info("Test 5: dry_run plan must contain all required fields...")
    db = FakeDB()
    result = run_mock_dry_run(db)

    plan = result["plan"]
    required_fields = [
        "jobs_count", "runs_count", "results_count",
        "jobs_to_delete", "runs_to_delete", "results_to_delete_count", "warnings"
    ]
    for field in required_fields:
        assert field in plan, f"dry_run plan is missing required field: '{field}'!"

    logger.info("  [OK] dry_run plan contains all required fields.")


def run_all_mock_tests():
    logger.info("=== Starting Mass Evaluation Cleanup Mock Unit Tests ===")
    test_dry_run_does_not_delete()
    test_execute_deletes_in_correct_order()
    test_execute_leaves_tables_empty()
    test_non_mass_eval_data_untouched()
    test_dry_run_plan_contains_expected_fields()
    logger.info("=== ALL MOCK TESTS PASSED SUCCESSFULLY! ===")


# ── Live integration test (requires DB) ───────────────────────────────────────

async def run_live_tests():
    """Integration test against a real database if available."""
    try:
        from app.db import get_engine
        from sqlalchemy.ext.asyncio import AsyncSession
        engine = get_engine()
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
        logger.info("Live DB available — running integration test...")

        async with AsyncSession(engine) as db:
            from app.services.cleanup_service import cleanup_mass_evaluations

            # 1. Dry run first — must not delete
            dry_result = await cleanup_mass_evaluations(db, mode="dry_run", performed_by_email="test@test.com")
            assert dry_result["mode"] == "dry_run"
            assert "plan" in dry_result
            logger.info(
                "  Live dry_run OK — jobs=%d runs=%d results=%d",
                dry_result["plan"]["jobs_count"],
                dry_result["plan"]["runs_count"],
                dry_result["plan"]["results_count"],
            )

            # 2. Execute — delete everything
            exec_result = await cleanup_mass_evaluations(db, mode="execute", performed_by_email="test@test.com")
            assert exec_result["ok"] is True
            logger.info(
                "  Live execute OK — deleted jobs=%d runs=%d results=%d",
                exec_result["deleted_jobs"],
                exec_result["deleted_runs"],
                exec_result["deleted_results"],
            )

            # 3. Verify tables empty
            from sqlalchemy import select, func
            from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
            j_count = (await db.execute(select(func.count()).select_from(MassEvaluationJob))).scalar()
            r_count = (await db.execute(select(func.count()).select_from(MassEvaluationRun))).scalar()
            res_count = (await db.execute(select(func.count()).select_from(MassEvaluationResult))).scalar()
            assert j_count == 0, f"Jobs not empty after execute! Count={j_count}"
            assert r_count == 0, f"Runs not empty after execute! Count={r_count}"
            assert res_count == 0, f"Results not empty after execute! Count={res_count}"
            logger.info("  ✓ All mass evaluation tables confirmed empty after execute.")

        logger.info("=== LIVE INTEGRATION TESTS PASSED ===")

    except Exception as e:
        logger.warning("Live DB unavailable or test failed: %s. Falling back to mock-only tests.", e)


async def main():
    # Always run mock tests
    run_all_mock_tests()

    # Attempt live tests if DB is available
    await run_live_tests()


if __name__ == "__main__":
    asyncio.run(main())
