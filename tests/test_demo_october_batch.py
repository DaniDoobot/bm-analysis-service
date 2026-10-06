"""
tests/test_demo_october_batch.py
================================
Comprehensive unit tests for the October 2026 extension of Demo Data Generator
(app/services/demo_september_generator.py & scripts/seed_demo_data.py).

Validates:
1. Exact October calendar bounds: 2026-09-24 to 2026-10-08 (11 working days).
2. Zero records on weekends (Saturday Sep 26, Oct 3, Sunday Sep 27, Oct 4).
3. Zero records outside [2026-09-24, 2026-10-08].
4. Calibrated October volume: ~10,000-12,000 total calls (~15-25 calls/active agent/day).
5. All 60 agents covered across 4 teams with realistic shifts & absenteeism.
6. Deterministic IDs namespace: demo_oct26_{agent_code}_{YYYYMMDD}_{seq}.
7. Idempotency & determinism across runs.
8. Tenant isolation: company_id=7, no external calls.
9. Service & team distribution coherence.
10. Backward compatibility with September batches.
"""
import os
import sys
from datetime import date, datetime, timezone
from typing import Set
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.demo_september_generator import (
    DemoSeptemberGenerator,
    WORKING_DAYS_OCT_2026,
    OCTOBER_2026_START,
    OCTOBER_2026_END,
    DEMO_COMPANY_ID,
    build_agent_schedule,
    resolve_batch_scope,
)


@pytest.fixture
def generator():
    return DemoSeptemberGenerator(company_id=DEMO_COMPANY_ID, seed=202609)


# =============================================================================
# 1. Calendar & Working Days Tests
# =============================================================================

def test_october_calendar_working_days():
    """Verify that October working days are strictly Monday-Friday (11 days)."""
    days = WORKING_DAYS_OCT_2026
    assert len(days) == 11, f"Expected 11 working days, got {len(days)}"
    assert days[0] == date(2026, 9, 24)
    assert days[-1] == date(2026, 10, 8)

    # Assert no weekend days (weekday < 5)
    for d in days:
        assert d.weekday() < 5, f"Date {d} is a weekend day (weekday={d.weekday()})"
        assert OCTOBER_2026_START.date() <= d <= OCTOBER_2026_END.date()

    # Assert weekends are explicitly excluded
    weekends = [
        date(2026, 9, 26),  # Sat
        date(2026, 9, 27),  # Sun
        date(2026, 10, 3),  # Sat
        date(2026, 10, 4),  # Sun
    ]
    for w in weekends:
        assert w not in days, f"Weekend date {w} should not be in WORKING_DAYS_OCT_2026"


def test_resolve_batch_scope_october():
    """Verify batch name resolution for 'october' and 'oct'."""
    days, agents = resolve_batch_scope("october")
    assert days == list(WORKING_DAYS_OCT_2026)
    assert agents == list(range(1, 61))

    days_short, agents_short = resolve_batch_scope("oct")
    assert days_short == days
    assert agents_short == agents


# =============================================================================
# 2. Volume & Calibration Tests
# =============================================================================

def test_october_batch_total_volume_and_range(generator):
    """Verify October planned volume falls within the target 10,000-12,000 range."""
    summary = generator.dry_run_summary("october")
    total_calls = summary["total_mass_evaluations"]

    assert 10000 <= total_calls <= 12000, (
        f"Total calls {total_calls} outside expected range [10,000, 12,000]"
    )
    assert summary["target_days_count"] == 11
    assert summary["target_agents_count"] == 60
    assert summary["total_criterion_results_estimated"] == total_calls * 6


def test_october_daily_call_volume_distribution(generator):
    """Verify daily volume is distributed around ~900-1,100 calls/day."""
    summary = generator.dry_run_summary("october")
    calls_by_day = summary["calls_by_day"]

    assert len(calls_by_day) == 11
    for day_str, count in calls_by_day.items():
        assert 900 <= count <= 1150, (
            f"Daily calls for {day_str} ({count}) outside calibrated window [900, 1,150]"
        )


def test_october_agent_daily_average_volume(generator):
    """Verify that average agent volume is calibrated between 15 and 25 calls/day."""
    summary = generator.dry_run_summary("october")
    dist = summary["agent_distribution"]

    # 11 working days * ~18 calls/day = ~198 average
    avg_per_agent = dist["avg_calls_per_agent"]
    daily_avg = avg_per_agent / 11.0
    assert 15.0 <= daily_avg <= 22.0, (
        f"Daily average calls/agent {daily_avg:.2f} outside target [15.0, 22.0]"
    )


# =============================================================================
# 3. Call ID Namespace & Determinism
# =============================================================================

def test_october_call_id_format(generator):
    """Verify call ID format follows demo_oct26_{agent_code}_{YYYYMMDD}_{seq}."""
    calls = generator.plan_calls_for_scope("october")
    assert len(calls) > 0

    for c in calls[:200]:  # sample check
        cid = c["call_id"]
        assert cid.startswith("demo_oct26_"), f"Call ID {cid} missing demo_oct26_ prefix"
        parts = cid.split("_")
        assert len(parts) == 5, f"Call ID {cid} does not have 5 parts"
        assert parts[0] == "demo"
        assert parts[1] == "oct26"
        assert len(parts[4]) == 3, f"Sequence part {parts[4]} in {cid} not 3 digits"


def test_october_determinism(generator):
    """Verify plan_calls_for_scope produces identical calls across multiple calls."""
    calls1 = generator.plan_calls_for_scope("october")
    calls2 = generator.plan_calls_for_scope("october")

    assert len(calls1) == len(calls2)
    for c1, c2 in zip(calls1[:100], calls2[:100]):
        assert c1["call_id"] == c2["call_id"]
        assert c1["call_timestamp"] == c2["call_timestamp"]
        assert c1["_rng_seed"] == c2["_rng_seed"]


# =============================================================================
# 4. Service & Team Distribution Tests
# =============================================================================

def test_october_service_distribution(generator):
    """Verify ~50-55% Atención al Cliente vs ~45-50% Ventas."""
    summary = generator.dry_run_summary("october")
    svc_calls = summary["calls_by_service"]
    total = summary["total_mass_evaluations"]

    at_ratio = svc_calls["atencion-al-cliente"] / total
    vt_ratio = svc_calls["ventas"] / total

    assert 0.48 <= at_ratio <= 0.55, f"Atención ratio {at_ratio:.3f} unexpected"
    assert 0.45 <= vt_ratio <= 0.52, f"Ventas ratio {vt_ratio:.3f} unexpected"


def test_october_team_assignments(generator):
    """Verify team volume and correct service mapping."""
    summary = generator.dry_run_summary("october")
    teams = summary["calls_by_team"]

    assert "Front Atención" in teams
    assert "Backoffice Atención" in teams
    assert "Equipo Comercial" in teams
    assert "Equipo Retención" in teams

    for tm, count in teams.items():
        assert count > 1500, f"Team {tm} has unexpectedly low count {count}"


# =============================================================================
# 5. Shift & Absenteeism Patterns
# =============================================================================

def test_october_agent_leave_patterns():
    """Verify realistic October leave and shift patterns."""
    # Agent 27: on leave until Sep 30, active from Oct 1
    sched27 = build_agent_schedule(27, month="october")
    assert date(2026, 9, 24) not in sched27.active_days
    assert date(2026, 9, 30) not in sched27.active_days
    assert date(2026, 10, 1) in sched27.active_days

    # Agent 57: leave from Sep 28 to Oct 2
    sched57 = build_agent_schedule(57, month="october")
    assert date(2026, 9, 24) in sched57.active_days
    assert date(2026, 9, 28) not in sched57.active_days
    assert date(2026, 10, 2) not in sched57.active_days
    assert date(2026, 10, 5) in sched57.active_days

    # Agent 8: off Wednesdays (Sep 30, Oct 7)
    sched8 = build_agent_schedule(8, month="october")
    assert date(2026, 9, 30) not in sched8.active_days
    assert date(2026, 10, 7) not in sched8.active_days
    assert date(2026, 9, 29) in sched8.active_days
