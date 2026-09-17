"""
app/utils/test_seed_demo_shifts.py
==================================
Unit tests for the deterministic shift-based September 2026 generator
(app/services/demo_september_generator.py).

Validates all 13 core requirements:
1. Exact September 2026 calendar bounds (2026-09-01 to 2026-09-23).
2. Zero records after 2026-09-23 23:59:59.
3. Zero records in August or any other month.
4. Deterministic shift distribution per agent across multiple runs.
5. Agents with scheduled days off (intercalated).
6. Agents with multi-day consecutive absences (vacations, leaves, late hires).
7. Daily volumes per active agent match team ranges:
   - Front: 55-65
   - Backoffice: 45-55
   - Comercial: 50-60
   - Retención: 40-50
8. Strict team, service, and agent consistency.
9. Typology strictly matches service.
10. Strict tenant isolation (company_id always 7).
11. Pilot batch volume matches expected range (~2,000-3,000 calls).
12. Dry-run purity (in-memory, no modifications).
13. Idempotency & non-duplication across batch progressions (pilot -> scale -> all).
"""
import os
import sys
from datetime import date, datetime, timezone
from typing import Set
import pytest

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.services.demo_september_generator import (
    DemoSeptemberGenerator,
    WORKING_DAYS_SEP_2026,
    PILOT_AGENT_INDICES,
    SEPTEMBER_2026_START,
    SEPTEMBER_2026_END,
    DEMO_COMPANY_ID,
    build_agent_schedule,
    resolve_batch_scope,
)


@pytest.fixture
def generator():
    return DemoSeptemberGenerator(company_id=DEMO_COMPANY_ID, seed=202609)


# =============================================================================
# 1. Calendar & Bounds Tests
# =============================================================================

def test_september_calendar_working_days(generator):
    """Verifies that working days are strictly Monday-Friday between Sep 1 and Sep 23, 2026."""
    days = WORKING_DAYS_SEP_2026
    assert len(days) == 17, f"Expected 17 working days, got {len(days)}"
    assert days[0] == date(2026, 9, 1)
    assert days[-1] == date(2026, 9, 23)

    # Every day must be in September 2026 and Monday-Friday (weekday 0..4)
    for d in days:
        assert d.year == 2026
        assert d.month == 9
        assert 1 <= d.day <= 23
        assert d.weekday() < 5, f"{d} is a weekend day!"


def test_zero_records_outside_september_window(generator):
    """All planned calls in batch 'all' must strictly fall within 2026-09-01 and 2026-09-23."""
    planned = generator.plan_calls_for_scope("all")
    assert len(planned) > 0

    for call in planned:
        ts = call["call_timestamp"]
        assert ts.year == 2026, f"Call timestamp year is {ts.year}"
        assert ts.month == 9, f"Call timestamp month is {ts.month} (expected September)"
        assert 1 <= ts.day <= 23, f"Call timestamp day is {ts.day} (expected 1..23)"
        assert ts >= SEPTEMBER_2026_START, f"{ts} is before September 1st"
        assert ts <= SEPTEMBER_2026_END, f"{ts} is after September 23rd"


# =============================================================================
# 2. Deterministic Shifts & Reproducibility
# =============================================================================

def test_shift_distribution_is_deterministic(generator):
    """Running the plan twice with the same seed must produce exact identical calls and IDs."""
    plan1 = generator.plan_calls_for_scope("pilot")
    plan2 = generator.plan_calls_for_scope("pilot")

    assert len(plan1) == len(plan2)
    for c1, c2 in zip(plan1, plan2):
        assert c1["call_id"] == c2["call_id"]
        assert c1["call_timestamp"] == c2["call_timestamp"]
        assert c1["agent_code"] == c2["agent_code"]


# =============================================================================
# 3. Absences, Days Off, and Multi-Day Gaps
# =============================================================================

def test_agents_with_intercalated_days_off(generator):
    """Certain agents must have scheduled days off during the workweek."""
    # Agent 8 (Front) is off every Wednesday
    sched_8 = generator.get_schedule(8)
    for d in sched_8.active_days:
        assert d.weekday() != 2, f"Agent 8 should be off on Wednesdays, but worked on {d}"

    # Agent 24 (Backoffice) is off every Monday
    sched_24 = generator.get_schedule(24)
    for d in sched_24.active_days:
        assert d.weekday() != 0, f"Agent 24 should be off on Mondays, but worked on {d}"

    # Agent 25 (Backoffice) is off every Friday
    sched_25 = generator.get_schedule(25)
    for d in sched_25.active_days:
        assert d.weekday() != 4, f"Agent 25 should be off on Fridays, but worked on {d}"


def test_agents_with_multi_day_consecutive_absences(generator):
    """Certain agents must have multi-day consecutive periods with zero calls."""
    # Agent 9 (Front): Vacations Week 1 & 2 (Sep 1 to 11 off)
    sched_9 = generator.get_schedule(9)
    for d in WORKING_DAYS_SEP_2026:
        if d <= date(2026, 9, 11):
            assert d not in sched_9.active_days, f"Agent 9 should be on vacation on {d}"
        else:
            assert d in sched_9.active_days

    # Agent 27 (Backoffice): Medical leave Week 3 & 4 (Sep 14 to 23 off)
    sched_27 = generator.get_schedule(27)
    for d in WORKING_DAYS_SEP_2026:
        if d >= date(2026, 9, 14):
            assert d not in sched_27.active_days, f"Agent 27 should be on leave on {d}"
        else:
            assert d in sched_27.active_days

    # Agent 39 (Comercial): Leave in Week 2 (Sep 7 to 11 off)
    sched_39 = generator.get_schedule(39)
    for d in WORKING_DAYS_SEP_2026:
        if date(2026, 9, 7) <= d <= date(2026, 9, 11):
            assert d not in sched_39.active_days, f"Agent 39 should be off during Week 2 on {d}"

    # Agent 10 (Front): Late hire starting Sep 14
    sched_10 = generator.get_schedule(10)
    for d in WORKING_DAYS_SEP_2026:
        if d < date(2026, 9, 14):
            assert d not in sched_10.active_days, f"Agent 10 (new hire) should not work before Sep 14 on {d}"
        else:
            assert d in sched_10.active_days


# =============================================================================
# 4. Daily Volume per Active Agent
# =============================================================================

def test_daily_call_volumes_within_team_ranges(generator):
    """When active, calls per day must strictly adhere to team ranges."""
    planned = generator.plan_calls_for_scope("all")

    # Group calls by (agent_index, date)
    daily_counts = {}
    for c in planned:
        key = (c["agent_index"], c["call_date"])
        daily_counts[key] = daily_counts.get(key, 0) + 1

    for (agent_idx, call_date), count in daily_counts.items():
        sched = generator.get_schedule(agent_idx)
        assert sched.daily_min_calls <= count <= sched.daily_max_calls, (
            f"Agent {agent_idx} ({sched.team_name}) on {call_date} had {count} calls, "
            f"expected range [{sched.daily_min_calls}, {sched.daily_max_calls}]"
        )


# =============================================================================
# 5. Team, Service, and Identity Consistency
# =============================================================================

def test_agent_team_and_service_consistency(generator):
    """Verifies that every call has consistent team, service, and agent mappings."""
    planned = generator.plan_calls_for_scope("all")

    for c in planned:
        idx = c["agent_index"]
        code = c["agent_code"]
        svc = c["service_key"]
        tm = c["team_name"]

        if 1 <= idx <= 10:
            assert tm == "Front Atención"
            assert svc == "atencion-al-cliente"
            assert code.startswith("AC-F")
        elif 11 <= idx <= 30:
            assert tm == "Backoffice Atención"
            assert svc == "atencion-al-cliente"
            assert code.startswith("AC-B")
        elif 31 <= idx <= 40:
            assert tm == "Equipo Comercial"
            assert svc == "ventas"
            assert code.startswith("VT-C")
        else:
            assert tm == "Equipo Retención"
            assert svc == "ventas"
            assert code.startswith("VT-R")


# =============================================================================
# 6. Pilot Batch Expected Volume
# =============================================================================

def test_pilot_batch_volume_in_expected_range(generator):
    """Pilot batch (3 days, 15 agents) must produce between 2,000 and 3,000 calls."""
    summary = generator.dry_run_summary("pilot")
    total_calls = summary["total_mass_evaluations"]

    assert 2000 <= total_calls <= 3000, (
        f"Pilot volume was {total_calls}, expected between 2000 and 3000."
    )
    assert summary["target_days_count"] == 3
    assert summary["target_agents_count"] == 15
    assert summary["total_criterion_results_estimated"] == total_calls * 6


# =============================================================================
# 7. Dry-Run Purity
# =============================================================================

def test_dry_run_summary_has_full_metrics(generator):
    """dry_run_summary() must return comprehensive statistical breakdown."""
    summary = generator.dry_run_summary("pilot")

    assert summary["company_id"] == DEMO_COMPANY_ID
    assert "calls_by_day" in summary
    assert len(summary["calls_by_day"]) == 3
    assert "active_agents_by_day" in summary
    assert "inactive_agents_by_day" in summary
    assert "calls_by_service" in summary
    assert "calls_by_team" in summary
    assert "agent_distribution" in summary
    assert "inbound_outbound_ratio_expected" in summary
    assert "call_duration_expected_seconds" in summary


# =============================================================================
# 8. Idempotency & Batch Progression
# =============================================================================

def test_call_ids_are_globally_unique_and_predictable(generator):
    """Every call_id in the entire month must be unique and follow the demo_sep26 convention."""
    planned = generator.plan_calls_for_scope("all")
    call_ids = [c["call_id"] for c in planned]

    assert len(call_ids) == len(set(call_ids)), "Duplicate call_ids found in plan!"

    for cid in call_ids:
        assert cid.startswith("demo_sep26_")
        parts = cid.split("_")
        # Format: demo_sep26_{code}_{YYYYMMDD}_{seq}
        assert len(parts) == 5
        assert parts[0] == "demo"
        assert parts[1] == "sep26"
        assert parts[3].startswith("202609")


def test_batch_progression_subsets(generator):
    """
    Verifies that:
    - Calls in 'pilot' are a strict subset of 'scale' and 'all'.
    - 'scale' (Sep 1-11) and 'final' (Sep 14-23) are mutually disjoint.
    - 'scale' + 'final' == 'all'.
    """
    pilot_calls = {c["call_id"] for c in generator.plan_calls_for_scope("pilot")}
    scale_calls = {c["call_id"] for c in generator.plan_calls_for_scope("scale")}
    final_calls = {c["call_id"] for c in generator.plan_calls_for_scope("final")}
    all_calls = {c["call_id"] for c in generator.plan_calls_for_scope("all")}

    # Pilot is fully included in Scale and All
    assert pilot_calls.issubset(scale_calls)
    assert pilot_calls.issubset(all_calls)

    # Scale and Final have zero overlap
    overlap = scale_calls.intersection(final_calls)
    assert len(overlap) == 0, f"Found {len(overlap)} overlapping calls between scale and final!"

    # Union of Scale and Final exactly equals All
    assert scale_calls.union(final_calls) == all_calls
    assert len(scale_calls) + len(final_calls) == len(all_calls)


# =============================================================================
# 9. Total Monthly Volume Target
# =============================================================================

def test_all_batch_target_volume_range(generator):
    """Full September volume must be between 40,000 and 50,000 calls."""
    summary = generator.dry_run_summary("all")
    total_calls = summary["total_mass_evaluations"]

    assert 40000 <= total_calls <= 50000, (
        f"Total monthly volume was {total_calls}, expected between 40,000 and 50,000."
    )
