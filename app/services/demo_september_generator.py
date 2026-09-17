"""
app/services/demo_september_generator.py
========================================
Deterministic shift-based synthetic data generator for Empresa Demo (company_id=7),
focused strictly on SEPTEMBER 2026 (2026-09-01 to 2026-09-23 inclusive).

Key Design Principles:
1. Target Period: 2026-09-01 00:00:00 to 2026-09-23 23:59:59. Fixed dates, NO datetime.now().
2. Realistic Shifts: Deterministic schedule per agent (standard L-V, rotating days off,
   multi-day leaves/vacations, late hires starting mid-month).
3. Daily Volumes per Active Agent:
   - Front Atención: 55-65 calls/day (mean ~60)
   - Backoffice Atención: 45-55 calls/day (mean ~50)
   - Equipo Comercial: 50-60 calls/day (mean ~55)
   - Equipo Retención: 40-50 calls/day (mean ~45)
4. Natural Call Spread: Calls distributed across working hours with realistic intervals.
5. Strict Isolation: company_id is always 7.
6. Absolute Idempotency: Unique call_ids (demo_sep26_{code}_{YYYYMMDD}_{seq}). Existing
   records are safely skipped without duplication or overwriting previous demo data.
7. Batching Support:
   - pilot: first 3 working days (1-3 Sep), 15 agents (~2,000-3,000 calls)
   - scale: working days 1-9 (1-11 Sep), all agents (~20,000-24,000 calls)
   - final: working days 10-17 (14-23 Sep), all agents (~20,000-24,000 calls)
   - all: full month 1-23 Sep (~42,000-48,000 calls)
8. Dry-Run: Complete statistics without modifying database.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
import logging
import random
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.typologies import Typology
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)

logger = logging.getLogger("demo_september_generator")

# Target company constants
DEMO_COMPANY_KEY = "empresa-demo"
DEMO_COMPANY_ID = 7

# Calendar definition for September 2026
SEPTEMBER_2026_START = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
SEPTEMBER_2026_END = datetime(2026, 9, 23, 23, 59, 59, tzinfo=timezone.utc)

# 17 working days (Monday-Friday) between Sep 1 and Sep 23, 2026
WORKING_DAYS_SEP_2026: List[date] = [
    date(2026, 9, 1),   # Tue - W1
    date(2026, 9, 2),   # Wed - W1
    date(2026, 9, 3),   # Thu - W1
    date(2026, 9, 4),   # Fri - W1
    date(2026, 9, 7),   # Mon - W2
    date(2026, 9, 8),   # Tue - W2
    date(2026, 9, 9),   # Wed - W2
    date(2026, 9, 10),  # Thu - W2
    date(2026, 9, 11),  # Fri - W2
    date(2026, 9, 14),  # Mon - W3
    date(2026, 9, 15),  # Tue - W3
    date(2026, 9, 16),  # Wed - W3
    date(2026, 9, 17),  # Thu - W3
    date(2026, 9, 18),  # Fri - W3
    date(2026, 9, 21),  # Mon - W4
    date(2026, 9, 22),  # Tue - W4
    date(2026, 9, 23),  # Wed - W4
]

PILOT_AGENT_INDICES = [
    1, 2, 3, 4,       # Front Atención (4)
    11, 12, 13, 14, 15, # Backoffice (5)
    31, 32, 33,        # Comercial (3)
    41, 42, 43         # Retención (3)
]  # 15 agents total


@dataclass
class AgentShiftConfig:
    """Configuration for an agent's work pattern in September 2026."""
    agent_index: int
    agent_code: str
    team_name: str
    service_key: str
    active_days: Set[date]
    daily_min_calls: int
    daily_max_calls: int
    daily_mean_calls: float
    daily_std_calls: float
    shift_start_hour: int
    shift_duration_hours: float
    archetype: str
    team_offset: float


def build_agent_schedule(agent_index: int) -> AgentShiftConfig:
    """
    Deterministically assign shift pattern, working days, and call targets
    for each of the 60 Demo agents.
    """
    # 1. Determine Team, Service, Base Call Targets and Shift Hours
    if 1 <= agent_index <= 10:
        team_name = "Front Atención"
        service_key = "atencion-al-cliente"
        agent_code = f"AC-F{agent_index:02d}"
        min_c, max_c, mean_c, std_c = 55, 65, 60.0, 2.5
        team_offset = 0.35
        shift_start = 8  # 08:00 to 16:30
    elif 11 <= agent_index <= 30:
        team_name = "Backoffice Atención"
        service_key = "atencion-al-cliente"
        agent_code = f"AC-B{agent_index - 10:02d}"
        min_c, max_c, mean_c, std_c = 45, 55, 50.0, 2.5
        team_offset = 0.05
        shift_start = 9  # 09:00 to 17:30
    elif 31 <= agent_index <= 40:
        team_name = "Equipo Comercial"
        service_key = "ventas"
        agent_code = f"VT-C{agent_index - 30:02d}"
        min_c, max_c, mean_c, std_c = 50, 60, 55.0, 2.5
        team_offset = 0.15
        shift_start = 9  # 09:00 to 17:30
    else:
        team_name = "Equipo Retención"
        service_key = "ventas"
        agent_code = f"VT-R{agent_index - 40:02d}"
        min_c, max_c, mean_c, std_c = 40, 50, 45.0, 2.5
        team_offset = -0.40
        shift_start = 10  # 10:00 to 18:30

    # 2. Assign Archetypes for scoring realism
    if agent_index in (1, 2, 11, 12, 13, 31, 32, 41, 42, 43):
        archetype = "top_performer"
    elif agent_index in (3, 4, 5, 6, 7, 14, 15, 16, 17, 18, 19, 20, 33, 34, 35, 36, 44, 45, 46, 47, 48, 49, 50, 51):
        archetype = "stable_performer"
    elif agent_index in (8, 9, 21, 22, 23, 24, 25, 37, 38, 52, 53, 54, 55, 56):
        archetype = "improving"
    elif agent_index in (26, 27, 28, 39, 40, 57, 58, 59):
        archetype = "struggling"
    else:  # 10, 29, 30, 60
        archetype = "new_hire"

    # 3. Determine Working Days Pattern
    all_days = set(WORKING_DAYS_SEP_2026)

    # Pattern A: Vacation / Multi-day leave in Week 1 & 2 (Sep 1 to Sep 11 off)
    if agent_index in (9, 26):
        # Starts working on Sep 14 (Week 3 & 4 only)
        active = {d for d in all_days if d >= date(2026, 9, 14)}

    # Pattern B: Medical leave / Leave in Week 3 & 4 (Sep 14 to Sep 23 off)
    elif agent_index in (27, 57):
        # Works only Sep 1 to Sep 11 (Week 1 & 2 only)
        active = {d for d in all_days if d <= date(2026, 9, 11)}

    # Pattern C: One full week leave (Week 2, Sep 7 to Sep 11 off)
    elif agent_index in (39, 58):
        active = {d for d in all_days if not (date(2026, 9, 7) <= d <= date(2026, 9, 11))}

    # Pattern D: Late New Hire (starts Monday Sep 14, zero calls before)
    elif agent_index in (10, 29, 60):
        active = {d for d in all_days if d >= date(2026, 9, 14)}

    # Pattern E: Rotating weekly day off (e.g. 4-day workweek)
    elif agent_index == 8:
        # Off every Wednesday (Sep 2, 9, 16, 23)
        active = {d for d in all_days if d.weekday() != 2}
    elif agent_index == 24:
        # Off every Monday (Sep 7, 14, 21)
        active = {d for d in all_days if d.weekday() != 0}
    elif agent_index in (25, 38):
        # Off every Friday (Sep 4, 11, 18)
        active = {d for d in all_days if d.weekday() != 4}
    elif agent_index == 54:
        # Off every Tuesday (Sep 1, 8, 15, 22)
        active = {d for d in all_days if d.weekday() != 1}
    elif agent_index == 55:
        # Off every Thursday (Sep 3, 10, 17)
        active = {d for d in all_days if d.weekday() != 3}
    elif agent_index == 56:
        # Off alternating Fridays (Sep 4, 18)
        active = {d for d in all_days if d not in (date(2026, 9, 4), date(2026, 9, 18))}

    # Pattern F: Standard Full Monday-to-Friday (all 17 working days)
    else:
        active = set(all_days)

    return AgentShiftConfig(
        agent_index=agent_index,
        agent_code=agent_code,
        team_name=team_name,
        service_key=service_key,
        active_days=active,
        daily_min_calls=min_c,
        daily_max_calls=max_c,
        daily_mean_calls=mean_c,
        daily_std_calls=std_c,
        shift_start_hour=shift_start,
        shift_duration_hours=8.5,
        archetype=archetype,
        team_offset=team_offset,
    )


def resolve_batch_scope(
    batch_name: str,
) -> Tuple[List[date], List[int]]:
    """
    Returns (target_days, target_agent_indices) for a given batch name.
    """
    batch = batch_name.lower().strip()
    if batch == "pilot":
        # First 3 working days (Sep 1, 2, 3), 15 agents
        return (
            [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)],
            list(PILOT_AGENT_INDICES),
        )
    elif batch == "scale":
        # First 2 weeks (Sep 1 to Sep 11, 9 working days), all 60 agents
        return (
            [d for d in WORKING_DAYS_SEP_2026 if d <= date(2026, 9, 11)],
            list(range(1, 61)),
        )
    elif batch == "final":
        # Weeks 3 & 4 (Sep 14 to Sep 23, 8 working days), all 60 agents
        return (
            [d for d in WORKING_DAYS_SEP_2026 if d >= date(2026, 9, 14)],
            list(range(1, 61)),
        )
    elif batch == "all":
        # Full month (all 17 working days), all 60 agents
        return (
            list(WORKING_DAYS_SEP_2026),
            list(range(1, 61)),
        )
    else:
        raise ValueError(
            f"Unknown batch '{batch_name}'. Supported: 'pilot', 'scale', 'final', 'all'."
        )


class DemoSeptemberGenerator:
    """
    Orchestrates the generation of realistic September 2026 call records.
    """

    def __init__(
        self,
        company_id: int = DEMO_COMPANY_ID,
        seed: int = 202609,
        chunk_size: int = 600,
    ):
        self.company_id = company_id
        self.seed = seed
        self.chunk_size = chunk_size
        self._schedules: Dict[int, AgentShiftConfig] = {
            i: build_agent_schedule(i) for i in range(1, 61)
        }

    def get_schedule(self, agent_index: int) -> AgentShiftConfig:
        return self._schedules[agent_index]

    def plan_calls_for_scope(
        self,
        batch_name: str,
    ) -> List[Dict[str, Any]]:
        """
        Pure in-memory plan of all calls that should be generated for the batch.
        Deterministic, no database dependency.
        """
        target_days, target_agent_indices = resolve_batch_scope(batch_name)
        planned_calls: List[Dict[str, Any]] = []

        for agent_idx in sorted(target_agent_indices):
            config = self._schedules[agent_idx]
            for day in sorted(target_days):
                if day not in config.active_days:
                    continue  # Agent not working on this day

                # Deterministic seed per agent + day for reproducible variability
                day_seed = (
                    self.seed
                    + (agent_idx * 1000)
                    + (day.year * 10000)
                    + (day.month * 100)
                    + day.day
                )
                rng = random.Random(day_seed)

                # Daily call count variation
                raw_count = int(rng.gauss(config.daily_mean_calls, config.daily_std_calls))
                call_count = max(config.daily_min_calls, min(config.daily_max_calls, raw_count))

                # Distribute timestamps across shift
                shift_start_dt = datetime(
                    day.year,
                    day.month,
                    day.day,
                    config.shift_start_hour,
                    0,
                    0,
                    tzinfo=timezone.utc,
                )
                # Available window: shift_duration_hours minus lunch break (~35m)
                total_window_seconds = int((config.shift_duration_hours - 0.6) * 3600)
                interval_avg = total_window_seconds / max(1, call_count)

                current_sec_offset = rng.randint(60, 300)
                for seq in range(1, call_count + 1):
                    # Progress with natural jitter
                    jitter = rng.randint(-45, 65)
                    step = max(60, int(interval_avg + jitter))
                    current_sec_offset += step

                    # Add lunch pause after ~4 hours
                    if 14000 <= current_sec_offset <= 16500:
                        current_sec_offset += rng.randint(1800, 2700)

                    call_ts = shift_start_dt + timedelta(seconds=current_sec_offset)
                    call_id = f"demo_sep26_{config.agent_code}_{day.strftime('%Y%m%d')}_{seq:03d}"

                    planned_calls.append({
                        "call_id": call_id,
                        "agent_index": agent_idx,
                        "agent_code": config.agent_code,
                        "team_name": config.team_name,
                        "service_key": config.service_key,
                        "archetype": config.archetype,
                        "team_offset": config.team_offset,
                        "call_date": day,
                        "call_timestamp": call_ts,
                        "seq": seq,
                        "_rng_seed": day_seed + seq,
                    })

        # Sort chronologically
        planned_calls.sort(key=lambda c: c["call_timestamp"])
        return planned_calls

    def dry_run_summary(
        self,
        batch_name: str,
    ) -> Dict[str, Any]:
        """
        Generate detailed statistical summary of the batch plan without touching the DB.
        """
        target_days, target_agent_indices = resolve_batch_scope(batch_name)
        planned = self.plan_calls_for_scope(batch_name)

        calls_by_day: Dict[str, int] = {}
        active_agents_by_day: Dict[str, Set[int]] = {}
        calls_by_service: Dict[str, int] = {}
        calls_by_team: Dict[str, int] = {}
        calls_by_agent: Dict[str, int] = {}

        for d in target_days:
            d_str = d.strftime("%Y-%m-%d")
            calls_by_day[d_str] = 0
            active_agents_by_day[d_str] = set()

        for c in planned:
            d_str = c["call_date"].strftime("%Y-%m-%d")
            calls_by_day[d_str] = calls_by_day.get(d_str, 0) + 1
            active_agents_by_day[d_str].add(c["agent_index"])

            svc = c["service_key"]
            calls_by_service[svc] = calls_by_service.get(svc, 0) + 1

            tm = c["team_name"]
            calls_by_team[tm] = calls_by_team.get(tm, 0) + 1

            ag = c["agent_code"]
            calls_by_agent[ag] = calls_by_agent.get(ag, 0) + 1

        agent_call_counts = list(calls_by_agent.values())
        min_ag_calls = min(agent_call_counts) if agent_call_counts else 0
        max_ag_calls = max(agent_call_counts) if agent_call_counts else 0
        avg_ag_calls = (
            round(sum(agent_call_counts) / len(agent_call_counts), 1)
            if agent_call_counts
            else 0
        )

        daily_active_counts = {
            d: len(active_agents_by_day[d]) for d in calls_by_day
        }
        daily_inactive_counts = {
            d: len(target_agent_indices) - len(active_agents_by_day[d])
            for d in calls_by_day
        }

        min_date = planned[0]["call_timestamp"] if planned else None
        max_date = planned[-1]["call_timestamp"] if planned else None

        return {
            "batch": batch_name,
            "company_id": self.company_id,
            "target_days_count": len(target_days),
            "target_agents_count": len(target_agent_indices),
            "total_mass_evaluations": len(planned),
            "total_criterion_results_estimated": len(planned) * 6,
            "date_range": {
                "start": str(min_date) if min_date else None,
                "end": str(max_date) if max_date else None,
            },
            "calls_by_day": calls_by_day,
            "active_agents_by_day": daily_active_counts,
            "inactive_agents_by_day": daily_inactive_counts,
            "calls_by_service": calls_by_service,
            "calls_by_team": calls_by_team,
            "agent_distribution": {
                "min_calls_per_agent": min_ag_calls,
                "max_calls_per_agent": max_ag_calls,
                "avg_calls_per_agent": avg_ag_calls,
            },
            "inbound_outbound_ratio_expected": {
                "atencion_al_cliente": "85% inbound / 15% outbound",
                "ventas": "78% outbound / 22% inbound",
            },
            "call_duration_expected_seconds": {
                "Front Atención": "160-320s",
                "Backoffice Atención": "300-440s",
                "Equipo Comercial": "240-380s",
                "Equipo Retención": "330-480s",
            },
        }

    async def execute_batch(
        self,
        db: AsyncSession,
        batch_name: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes generation for the specified batch.
        If dry_run=True, returns summary without modifying the database.
        If dry_run=False, inserts in chunks with full idempotency check.
        """
        # 1. Validate target company is Empresa Demo
        comp_res = await db.execute(
            select(Company).where(Company.company_id == self.company_id)
        )
        company = comp_res.scalars().first()
        if not company or not company.is_demo:
            raise ValueError(
                f"Safety Guard: company_id={self.company_id} is not a valid demo company."
            )

        # 2. Plan in-memory
        summary = self.dry_run_summary(batch_name)
        if dry_run:
            logger.info(
                "[DRY-RUN] Planned %d calls for batch '%s'. No changes written.",
                summary["total_mass_evaluations"],
                batch_name,
            )
            return {"status": "dry_run", **summary}

        # 3. Load DB references for demo company
        planned_calls = self.plan_calls_for_scope(batch_name)
        if not planned_calls:
            return {"status": "success", "inserted_calls": 0, "skipped_calls": 0, **summary}

        # Check existing call_ids for idempotency
        all_call_ids = [c["call_id"] for c in planned_calls]
        existing_res = await db.execute(
            select(MassEvaluationResult.call_id).where(
                MassEvaluationResult.company_id == self.company_id,
                MassEvaluationResult.call_id.in_(all_call_ids),
            )
        )
        existing_set: Set[str] = set(existing_res.scalars().all())
        calls_to_insert = [c for c in planned_calls if c["call_id"] not in existing_set]

        skipped_count = len(planned_calls) - len(calls_to_insert)
        if skipped_count > 0:
            logger.info(
                "[IDEMPOTENCY] %d of %d calls already exist in DB. Skipping.",
                skipped_count,
                len(planned_calls),
            )

        if not calls_to_insert:
            logger.info("[IDEMPOTENCY] All calls in batch '%s' already exist. Nothing to insert.", batch_name)
            return {
                "status": "already_up_to_date",
                "inserted_calls": 0,
                "skipped_calls": skipped_count,
                **summary,
            }

        # Load services, typologies, prompts, criteria and jobs
        from scripts.seed_demo_data import (
            ensure_evaluation_structures,
            compute_call_evaluation,
            TARGET_EVALUATIONS,
        )

        struct_reg = await ensure_evaluation_structures(db, company)
        svc_at = struct_reg["services"]["atencion"]
        svc_vn = struct_reg["services"]["ventas"]
        by_typo = struct_reg.get("by_typology") or struct_reg["by_typo"]

        # Typology lookups: support both direct dict and nested structure mapping
        all_typos = struct_reg.get("typologies") or {
            tkey: st["typology"] for tkey, st in by_typo.items()
        }

        front_typos = [all_typos["consulta_general"], all_typos["soporte_tecnico"], all_typos["reclamacion_incidencia"]]
        front_weights = [55, 35, 10]

        back_typos = [all_typos["reclamacion_incidencia"], all_typos["facturacion_cobros"], all_typos["soporte_tecnico"], all_typos["consulta_general"]]
        back_weights = [45, 35, 12, 8]

        comercial_typos = [all_typos["captacion_nuevo"], all_typos["upselling_cross"], all_typos["renovacion"]]
        comercial_weights = [60, 30, 10]

        retencion_typos = [all_typos["retencion_baja"], all_typos["renovacion"], all_typos["upselling_cross"], all_typos["captacion_nuevo"]]
        retencion_weights = [50, 35, 10, 5]

        # Ensure Jobs exist for both services
        async def _ensure_job(svc: Service, prompt_obj: Any, name: str) -> MassEvaluationJob:
            j_res = await db.execute(
                select(MassEvaluationJob).where(
                    MassEvaluationJob.company_id == self.company_id,
                    MassEvaluationJob.service_id == svc.service_id,
                )
            )
            job = j_res.scalars().first()
            if not job:
                admin_user = struct_reg.get("admin_user")
                c_name = admin_user.name if admin_user else "Admin Empresa Demo"
                c_email = admin_user.email if admin_user else "admin@demo.doobot.ai"
                job = MassEvaluationJob(
                    company_id=self.company_id,
                    service_id=svc.service_id,
                    prompt_id=prompt_obj.prompt_id,
                    job_name=name,
                    job_mode="standard",
                    selection_mode="filter",
                    is_active=True,
                    created_by=c_name,
                    created_by_email=c_email,
                )
                db.add(job)
                await db.flush()
            return job

        prompt_at = struct_reg["structures"]["atencion_general"]["prompt"]
        prompt_vn = struct_reg["structures"]["venta_consultiva"]["prompt"]
        job_at = await _ensure_job(svc_at, prompt_at, "Auditoría Histórica - Atención al Cliente")
        job_vn = await _ensure_job(svc_vn, prompt_vn, "Auditoría Histórica - Ventas y Retención")

        # Ensure active Runs for September exist
        async def _ensure_run(job: MassEvaluationJob, svc: Service) -> MassEvaluationRun:
            r_res = await db.execute(
                select(MassEvaluationRun).where(
                    MassEvaluationRun.company_id == self.company_id,
                    MassEvaluationRun.service_id == svc.service_id,
                ).order_by(MassEvaluationRun.run_id.desc())
            )
            run_obj = r_res.scalars().first()
            if not run_obj:
                run_obj = MassEvaluationRun(
                    job_id=job.job_id,
                    company_id=self.company_id,
                    service_id=svc.service_id,
                    trigger_type="manual",
                    status="completed",
                    started_at=datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 9, 24, 1, 0, 0, tzinfo=timezone.utc),
                    calls_found=0,
                    calls_selected=0,
                    calls_analyzed=0,
                )
                db.add(run_obj)
                await db.flush()
            return run_obj

        run_at = await _ensure_run(job_at, svc_at)
        run_vn = await _ensure_run(job_vn, svc_vn)

        # Agents lookup for user_id / hubspot_owner_id / name
        users_res = await db.execute(
            select(User).where(User.company_id == self.company_id, User.role == "agent")
        )
        agents_by_code = {u.agent_initials: u for u in users_res.scalars().all()}

        # Build payload rows
        full_records: List[Dict[str, Any]] = []
        for p in calls_to_insert:
            rng = random.Random(p["_rng_seed"])
            agent_user = agents_by_code.get(p["agent_code"])
            agent_name = agent_user.name if agent_user else f"Agente {p['agent_code']}"
            owner_id = agent_user.hubspot_owner_id if agent_user else f"demo_owner_{p['agent_index']:02d}"

            is_ventas = (p["service_key"] == "ventas")
            target_job = job_vn if is_ventas else job_at
            target_run = run_vn if is_ventas else run_at

            # Select typology based on team
            tm = p["team_name"]
            if "Front" in tm:
                chosen_typo = rng.choices(front_typos, weights=front_weights)[0]
            elif "Backoffice" in tm:
                chosen_typo = rng.choices(back_typos, weights=back_weights)[0]
            elif "Comercial" in tm:
                chosen_typo = rng.choices(comercial_typos, weights=comercial_weights)[0]
            else:
                chosen_typo = rng.choices(retencion_typos, weights=retencion_weights)[0]

            struct = by_typo[chosen_typo.typology_key]

            # Compute realistic score & criteria
            from scripts.seed_demo_data import build_agent_profile
            prof = build_agent_profile(p["agent_index"], tm)
            eval_data = compute_call_evaluation(
                prof,
                day_offset=(p["call_date"] - date(2026, 9, 1)).days,
                chosen_typo=chosen_typo,
                struct=struct,
                rng=rng,
                total_days=23,
            )

            # Direction
            if is_ventas:
                direction = "outbound" if rng.random() < 0.78 else "inbound"
            else:
                direction = "inbound" if rng.random() < 0.85 else "outbound"

            call_ts = p["call_timestamp"]
            cid_str = p["call_id"]

            full_records.append({
                "run_id": target_run.run_id if target_run else None,
                "job_id": target_job.job_id if target_job else None,
                "company_id": self.company_id,
                "service_id": struct["service"].service_id,
                "service_key": struct["service"].service_key,
                "service_name": struct["service"].service_name,
                "execution_source": "on_demand",
                "call_id": cid_str,
                "hs_object_id": f"hs_{cid_str}",
                "recording_url": f"https://demo.doobot.ai/recordings/{cid_str}.mp3",
                "hubspot_owner_id": owner_id,
                "agent_name": agent_name,
                "call_timestamp": call_ts,
                "analysis_timestamp": call_ts + timedelta(seconds=eval_data["duracion_segundos"] + 45),
                "created_at": call_ts,  # Must strictly match call_timestamp for date filters
                "call_duration_seconds": eval_data["duracion_segundos"],
                "direction": direction,
                "prompt_id": struct["prompt"].prompt_id,
                "prompt_version_id": struct["version"].id,
                "prompt_name": struct["prompt"].prompt_name,
                "prompt_version_name": struct["version"].version_name,
                "prompt_version_label": struct["version"].version_label,
                "prompt_snapshot": struct["version"].prompt,
                "typology_id": chosen_typo.typology_id,
                "typology_key": chosen_typo.typology_key,
                "typology_name": chosen_typo.typology_name,
                "status": "completed",
                "is_evaluable": eval_data["is_evaluable"],
                "non_evaluable_reason": eval_data["non_evaluable_reason"],
                "result_json": eval_data["result_json"],
                "items_json": eval_data["items_json"],
                "evaluacion_global": eval_data["evaluacion_global"],
                "_struct": struct,
                "_eval_data": eval_data,
            })

        # Bulk insert MassEvaluationResult in chunks
        inserted_id_map: Dict[str, int] = {}
        for i in range(0, len(full_records), self.chunk_size):
            chunk = full_records[i : i + self.chunk_size]
            insert_chunk = [
                {k: v for k, v in row.items() if not k.startswith("_")}
                for row in chunk
            ]
            stmt = (
                insert(MassEvaluationResult)
                .values(insert_chunk)
                .returning(
                    MassEvaluationResult.mass_analysis_id,
                    MassEvaluationResult.call_id,
                )
            )
            res = await db.execute(stmt)
            for ma_id, cid_s in res.all():
                inserted_id_map[cid_s] = ma_id

        # Bulk insert MassEvaluationCriterionResult (6 per call)
        crit_rows: List[Dict[str, Any]] = []
        for call_item in full_records:
            cid_s = call_item["call_id"]
            ma_id = inserted_id_map.get(cid_s)
            if not ma_id:
                continue

            struct = call_item["_struct"]
            eval_d = call_item["_eval_data"]
            crit_defs = struct["criteria_defs"]
            is_eval = call_item["is_evaluable"]

            for cdef in crit_defs:
                ckey = cdef["criterion_key"]
                crit_obj = struct["criteria"][ckey]
                score_data = eval_d["result_json"].get(ckey, {})
                c_score = Decimal(str(score_data.get("score", 0.0)))
                c_feed = score_data.get("feedback", "")

                crit_rows.append({
                    "mass_analysis_id": ma_id,
                    "run_id": call_item["run_id"],
                    "job_id": call_item["job_id"],
                    "prompt_id": struct["prompt"].prompt_id,
                    "prompt_version_id": struct["version"].id,
                    "criterion_id": crit_obj.criterion_id,
                    "criterion_key": ckey,
                    "criterion_name": cdef["criterion_name"],
                    "criterion_type": "score",
                    "call_id": cid_s,
                    "numeric_value": c_score if is_eval else Decimal("0.0"),
                    "percentage_value": c_score * Decimal("10.0") if is_eval else Decimal("0.0"),
                    "boolean_value": c_score >= Decimal("5.0") if is_eval else False,
                    "feedback": c_feed,
                    "feed_key": crit_obj.feed_key or f"{ckey}_feedback",
                    "is_applicable": is_eval,
                })

        for i in range(0, len(crit_rows), self.chunk_size):
            crit_chunk = crit_rows[i : i + self.chunk_size]
            await db.execute(insert(MassEvaluationCriterionResult).values(crit_chunk))

        logger.info(
            "[APPLY] Batch '%s' applied: %d MassEvaluationResult rows and %d CriterionResult rows inserted.",
            batch_name,
            len(inserted_id_map),
            len(crit_rows),
        )

        return {
            "status": "applied",
            "inserted_calls": len(inserted_id_map),
            "inserted_criteria": len(crit_rows),
            "skipped_calls": skipped_count,
            **summary,
        }
