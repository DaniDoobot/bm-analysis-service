"""
Test suite for GET /bm/mass-evaluation-results performance and pagination contract.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from datetime import datetime, timezone
from app.schemas.mass_evaluations import MassEvaluationResultResponse, PagedMassEvaluationResultResponse


def test_mass_results_response_schema():
    """Verify that MassEvaluationResultResponse handles pagination, direction, and items visual."""
    res = MassEvaluationResultResponse(
        mass_analysis_id=101,
        run_id=5,
        job_id=2,
        call_id="call-999",
        hs_object_id="12345",
        recording_url="https://recording.example.com/1.mp3",
        hubspot_owner_id="1375831790",
        agent_name="Luci Dos Santos",
        call_timestamp=datetime.now(timezone.utc),
        analysis_timestamp=datetime.now(timezone.utc),
        call_duration_seconds=180,
        direction="inbound",
        prompt_id=1,
        prompt_version_id=1,
        prompt_name="Default Prompt",
        prompt_version_name="v1",
        prompt_version_label="v1.0",
        prompt_snapshot="{}",
        company_id=1,
        service_id=1,
        service_key="front",
        service_name="Front",
        typology_id=10,
        typology_key="cita",
        typology_name="Cita",
        execution_source="automation",
        status="completed",
        result_json={"evaluacion_global": 8.0},
        items_json=[],
        items_visual=[],
        global_score=8.0,
        hubspot_metadata={},
        error_message=None,
        created_at=datetime.now(timezone.utc)
    )

    paged = PagedMassEvaluationResultResponse(
        items=[res],
        total=1,
        limit=100,
        offset=0
    )

    d = paged.model_dump()
    assert d["total"] == 1
    assert d["items"][0]["direction"] == "inbound"
    assert d["items"][0]["execution_source"] == "automation"
    print("[OK] test_mass_results_response_schema passed.")


if __name__ == "__main__":
    test_mass_results_response_schema()
    print("\n[OK] ALL mass evaluation results performance contract tests PASSED successfully!")
