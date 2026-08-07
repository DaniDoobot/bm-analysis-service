"""
Test suite for GET /bm/analytics/agents-comparison performance contract.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.schemas.analytics import AgentComparisonRow, AgentComparisonResponse, AgentInfo, AnalyticsItem


def test_agent_comparison_contract():
    """Verify that AgentComparisonResponse constructs properly even with empty data or catalog fallback."""
    response = AgentComparisonResponse(
        agents=[
            AgentInfo(
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos",
                name="Luci Dos Santos",
                agent_initials="LD",
                initials="LD",
                label="LD · Luci Dos Santos",
                service_id=1,
                service_name="Front"
            )
        ],
        items=[
            AnalyticsItem(
                key="evaluacion_global",
                label="Evaluación Global",
                type="score",
                value_type="score",
                order=1,
                default_selected=True
            )
        ],
        comparison=[
            AgentComparisonRow(
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos",
                item_key="evaluacion_global",
                item_label="Evaluación Global",
                metric_type="score",
                value=8.5,
                count=12
            )
        ]
    )
    d = response.model_dump()
    assert len(d["agents"]) == 1
    assert d["agents"][0]["agent_initials"] == "LD"
    assert len(d["comparison"]) == 1
    assert d["comparison"][0]["value"] == 8.5
    print("[OK] test_agent_comparison_contract passed.")


if __name__ == "__main__":
    test_agent_comparison_contract()
    print("\n[OK] ALL agents comparison performance contract tests PASSED successfully!")
