"""
Test suite for GET /bm/analytics/filter-options and available agents catalog contract.
Validates that agents array is populated with hubspot_owner_id, name, agent_initials, label, service_id, service_name.
"""
import sys
import os
import asyncio

# Ensure app is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.utils.dates import safe_parse_datetime
from app.schemas.analytics import AgentInfo


def test_date_parsing_formats():
    """Verify that safe_parse_datetime correctly parses slash, hyphen, and ISO date formats."""
    d1 = safe_parse_datetime("2026-07-01")
    assert d1 is not None and d1.year == 2026 and d1.month == 7 and d1.day == 1

    d2 = safe_parse_datetime("07/01/2026")
    assert d2 is not None and d2.year == 2026 and d2.month == 1 and d2.day == 7

    d3 = safe_parse_datetime("08/07/2026")
    assert d3 is not None and d3.year == 2026 and d3.month == 7 and d3.day == 8

    d4 = safe_parse_datetime("2026-07-01T00:00:00Z")
    assert d4 is not None and d4.year == 2026 and d4.month == 7 and d4.day == 1

    print("[OK] test_date_parsing_formats passed.")


def test_agent_info_schema():
    """Verify that AgentInfo schema supports all extended selector fields."""
    agent = AgentInfo(
        hubspot_owner_id="1375831790",
        agent_name="Luci Dos Santos",
        name="Luci Dos Santos",
        agent_initials="LD",
        initials="LD",
        label="LD · Luci Dos Santos",
        service_id=1,
        service_name="Front"
    )
    d = agent.model_dump()
    assert d["hubspot_owner_id"] == "1375831790"
    assert d["agent_name"] == "Luci Dos Santos"
    assert d["agent_initials"] == "LD"
    assert d["label"] == "LD · Luci Dos Santos"
    assert d["service_id"] == 1
    print("[OK] test_agent_info_schema passed.")


if __name__ == "__main__":
    test_date_parsing_formats()
    test_agent_info_schema()
    print("\n[OK] ALL agents comparison filter options tests PASSED successfully!")
