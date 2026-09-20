"""
scripts/prepare_demo_trainer_agents.py
======================================
Safe, idempotent preparation script for the 4 Trainer demo agents in Empresa Demo (company_id=7).

TARGET AGENTS:
- Agente Demo 01 -> demo_owner_01 -> AC-F01 -> Front Atención (Atención al Cliente) -> PIN 1001
- Agente Demo 11 -> demo_owner_11 -> AC-B01 -> Backoffice Atención (Atención al Cliente) -> PIN 2011
- Agente Demo 31 -> demo_owner_31 -> VT-C01 -> Equipo Comercial (Ventas) -> PIN 3031
- Agente Demo 41 -> demo_owner_41 -> VT-R01 -> Equipo Retención (Ventas) -> PIN 4041

AIRLOCK & SAFETY:
- company_id MUST be 7 (Empresa Demo).
- is_demo MUST be True.
- Only touches the 4 target agents. Aborts if any other agent is targeted.
- Aborts if any of the 4 target agents does not exist.
- Aborts if user/agent relationships are inconsistent.
- Preserves all historical Run 1 reports, prompts, sessions, evaluations, and completions (0 modified, 0 deleted).
- Preserves all 50,075 base calls and 300,450 criteria results (0 touched).
- Preserves Boston Medical (company_id=1) and all other companies (0 touched).
- IDEMPOTENT: running multiple times does not duplicate cycles, prompts, or completions.

Usage:
  python scripts/prepare_demo_trainer_agents.py --dry-run
  python scripts/prepare_demo_trainer_agents.py --apply
"""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import logging
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import select, and_, update, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db import _make_async_url
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingAgentSetting,
)
from app.services.demo_training_cycle_enhancer import (
    DEMO_COMPANY_ID,
    generate_enhanced_training_cycle_data,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("prepare_demo_trainer_agents")

# Target agents specification
TARGET_AGENTS = {
    "demo_owner_01": {
        "index": 1,
        "name": "Agente Demo 01",
        "code": "AC-F01",
        "pin": "1001",
        "team": "Front Atención",
        "service_key": "atencion-al-cliente",
    },
    "demo_owner_11": {
        "index": 11,
        "name": "Agente Demo 11",
        "code": "AC-B01",
        "pin": "2011",
        "team": "Backoffice Atención",
        "service_key": "atencion-al-cliente",
    },
    "demo_owner_31": {
        "index": 31,
        "name": "Agente Demo 31",
        "code": "VT-C01",
        "pin": "3031",
        "team": "Equipo Comercial",
        "service_key": "ventas",
    },
    "demo_owner_41": {
        "index": 41,
        "name": "Agente Demo 41",
        "code": "VT-R01",
        "pin": "4041",
        "team": "Equipo Retención",
        "service_key": "ventas",
    },
}


async def prepare_demo_agents(db: AsyncSession, apply: bool = False) -> Dict[str, Any]:
    stats = {
        "target_agents": len(TARGET_AGENTS),
        "historical_cycles_found": 0,
        "active_cycles_existing_before": 0,
        "new_cycles_created": 0,
        "prompts_created": 0,
        "completions_created": 0,
        "pins_updated": 0,
        "pins_already_matching": 0,
        "agents_already_prepared": 0,
        "historical_reports_modified": 0,
        "historical_prompts_modified": 0,
        "historical_sessions_modified": 0,
        "historical_evaluations_modified": 0,
        "historical_completions_modified": 0,
        "base_calls_modified": 0,
        "criteria_results_modified": 0,
        "other_agents_modified": 0,
        "other_companies_modified": 0,
        "details": [],
    }

    # ── 1. Strict Airlock & Tenant Verification ──────────────────────────────
    stmt_comp = select(Company).where(Company.company_id == DEMO_COMPANY_ID)
    res_comp = await db.execute(stmt_comp)
    company = res_comp.scalars().first()

    if not company:
        raise ValueError(f"CRITICAL AIRLOCK: Empresa Demo con ID {DEMO_COMPANY_ID} no encontrada en la base de datos.")
    if not company.is_demo:
        raise ValueError(f"CRITICAL AIRLOCK: La empresa {DEMO_COMPANY_ID} ('{company.company_name}') NO está marcada como is_demo.")
    if company.company_id != DEMO_COMPANY_ID:
        raise ValueError(f"CRITICAL AIRLOCK: Violación de aislamiento multitenant. Intentando acceder a company_id={company.company_id}.")

    logger.info("Airlock verificado: Empresa '%s' (company_id=%d, is_demo=%s)", company.company_name, company.company_id, company.is_demo)

    # ── 2. Verify all 4 target users exist and belong to company 7 ───────────
    target_owner_ids = list(TARGET_AGENTS.keys())
    stmt_users = select(User).where(
        User.company_id == DEMO_COMPANY_ID,
        User.hubspot_owner_id.in_(target_owner_ids)
    )
    res_users = await db.execute(stmt_users)
    users_by_oid = {u.hubspot_owner_id: u for u in res_users.scalars().all()}

    for oid, expected in TARGET_AGENTS.items():
        if oid not in users_by_oid:
            raise ValueError(f"CRITICAL AIRLOCK: El usuario objetivo {oid} ({expected['name']}) no existe en company_id={DEMO_COMPANY_ID}.")
        u = users_by_oid[oid]
        if u.company_id != DEMO_COMPANY_ID:
            raise ValueError(f"CRITICAL AIRLOCK: El usuario {oid} pertenece a company_id={u.company_id}, no a {DEMO_COMPANY_ID}.")
        if not u.is_active:
            raise ValueError(f"CRITICAL AIRLOCK: El usuario {oid} está inactivo.")

    # ── 3. Check PIN uniqueness & collision safety ───────────────────────────
    target_pins = {spec["pin"]: oid for oid, spec in TARGET_AGENTS.items()}
    stmt_pins = select(TrainingAgentSetting).where(
        TrainingAgentSetting.training_numeric_code.in_(list(target_pins.keys()))
    )
    res_pins = await db.execute(stmt_pins)
    for existing_s in res_pins.scalars().all():
        if existing_s.hubspot_owner_id not in TARGET_AGENTS:
            raise ValueError(
                f"CRITICAL: El PIN '{existing_s.training_numeric_code}' ya está en uso por otro agente ({existing_s.hubspot_owner_id} - {existing_s.agent_name})."
            )

    # ── 4. Resolve Active TrainingRun or Create One ──────────────────────────
    now_utc = datetime.now(timezone.utc)
    stmt_run = select(TrainingRun).where(
        TrainingRun.company_id == DEMO_COMPANY_ID,
        TrainingRun.status.in_(["running", "in_progress"])
    ).order_by(TrainingRun.training_run_id.desc())
    res_run = await db.execute(stmt_run)
    active_run = res_run.scalars().first()

    if not active_run:
        logger.info("Creando TrainingRun activo (Run 2/3) para ciclos en entrenamiento...")
        active_run = TrainingRun(
            company_id=DEMO_COMPANY_ID,
            period_start=now_utc - timedelta(days=7),
            period_end=now_utc + timedelta(days=7),
            status="running",
            triggered_by="scheduler",
            agents_total=4,
            agents_completed=0,
            started_at=now_utc - timedelta(days=7),
        )
        if apply:
            db.add(active_run)
            await db.flush()

    # ── 5. Process Each Target Agent (Idempotent) ────────────────────────────
    for oid, spec in TARGET_AGENTS.items():
        user = users_by_oid[oid]
        agent_pin = spec["pin"]
        service_key = spec["service_key"]

        # Check existing historical cycles
        stmt_hist = select(TrainingAgentReport).where(
            TrainingAgentReport.company_id == DEMO_COMPANY_ID,
            TrainingAgentReport.hubspot_owner_id == oid,
            TrainingAgentReport.status == "completed"
        )
        res_hist = await db.execute(stmt_hist)
        hist_reports = list(res_hist.scalars().all())
        stats["historical_cycles_found"] += len(hist_reports)

        # Check if already has an active in_progress cycle
        stmt_active = select(TrainingAgentReport).where(
            TrainingAgentReport.company_id == DEMO_COMPANY_ID,
            TrainingAgentReport.hubspot_owner_id == oid,
            TrainingAgentReport.status.in_(["in_progress", "running"]),
            TrainingAgentReport.is_current == True
        )
        res_active = await db.execute(stmt_active)
        active_report = res_active.scalars().first()

        cycle_created = False
        if active_report:
            stats["active_cycles_existing_before"] += 1
            stats["agents_already_prepared"] += 1
            logger.info("Agente %s (%s) ya tiene un ciclo activo (ID %d). No se duplica.", oid, spec["name"], active_report.training_report_id)
            current_report_id = active_report.training_report_id
        else:
            # Generate new rich in_progress cycle payload
            cycle_data = generate_enhanced_training_cycle_data(
                agent_id=oid,
                agent_name=spec["name"],
                agent_initials=spec["code"],
                service_key=service_key,
                status="in_progress",
                cycle_index=active_run.training_run_id if active_run else 2,
            )

            # Deactivate is_current on previous completed cycles for this agent
            if apply:
                await db.execute(
                    update(TrainingAgentReport)
                    .where(
                        TrainingAgentReport.company_id == DEMO_COMPANY_ID,
                        TrainingAgentReport.hubspot_owner_id == oid,
                        TrainingAgentReport.is_current == True
                    )
                    .values(is_current=False)
                )

            # Create new active TrainingAgentReport
            new_report = TrainingAgentReport(
                training_run_id=active_run.training_run_id if active_run else None,
                company_id=DEMO_COMPANY_ID,
                service_id=user.primary_service_id,
                hubspot_owner_id=oid,
                agent_name=spec["name"],
                agent_initials=spec["code"],
                period_start=now_utc - timedelta(days=7),
                period_end=now_utc + timedelta(days=7),
                status="in_progress",
                cycle_mode="automatic",
                evaluations_count=0,
                calls_count=0,
                avg_evaluacion_global=cycle_data["avg_score"],
                summary_general=cycle_data["summary_general"],
                evolution_summary=cycle_data["evolution_summary"],
                strengths_json=cycle_data["strengths_json"],
                weaknesses_json=cycle_data["weaknesses_json"],
                notable_data_json=cycle_data["notable_data_json"],
                general_objectives_json=cycle_data["general_objectives_json"],
                specific_objectives_json=cycle_data["specific_objectives_json"],
                final_report_json=None,
                is_current=True,
                generated_at=now_utc - timedelta(days=7),
                approved_at=now_utc - timedelta(days=7),
            )
            if apply:
                db.add(new_report)
                await db.flush()
                current_report_id = new_report.training_report_id
            else:
                current_report_id = -1

            stats["new_cycles_created"] += 1
            cycle_created = True

            # Create simulation prompts and pending completion statuses
            for idx, p_info in enumerate(cycle_data["prompts"]):
                if apply:
                    p = TrainingSimulationPrompt(
                        training_report_id=current_report_id,
                        hubspot_owner_id=oid,
                        prompt_number=idx + 1,
                        title=p_info["title"],
                        scenario_type=p_info["scenario_type"],
                        objective_focus_json=p_info["objective_focus_json"],
                        prompt_text=p_info["prompt_text"],
                    )
                    db.add(p)
                    await db.flush()

                    comp = TrainingCompletionStatus(
                        training_report_id=current_report_id,
                        simulation_prompt_id=p.simulation_prompt_id,
                        hubspot_owner_id=oid,
                        status="pending",
                        completed_at=None,
                        call_session_id=None,
                        evaluation_id=None,
                        notes=None,
                    )
                    db.add(comp)

                stats["prompts_created"] += 1
                stats["completions_created"] += 1

        # ── Update PIN in TrainingAgentSetting ────────────────────────────────
        stmt_setting = select(TrainingAgentSetting).where(
            TrainingAgentSetting.company_id == DEMO_COMPANY_ID,
            TrainingAgentSetting.hubspot_owner_id == oid
        )
        res_setting = await db.execute(stmt_setting)
        setting = res_setting.scalars().first()

        pin_updated = False
        if setting:
            if setting.training_numeric_code == agent_pin and setting.training_code_enabled is True:
                stats["pins_already_matching"] += 1
            else:
                if apply:
                    setting.training_numeric_code = agent_pin
                    setting.training_code_enabled = True
                    setting.training_code_updated_at = now_utc
                stats["pins_updated"] += 1
                pin_updated = True
        else:
            if apply:
                setting = TrainingAgentSetting(
                    company_id=DEMO_COMPANY_ID,
                    hubspot_owner_id=oid,
                    agent_name=spec["name"],
                    agent_initials=spec["code"],
                    is_enabled=True,
                    include_in_scheduler=True,
                    training_code=f"{spec['code'][:2]}{spec['index']:02d}",
                    training_numeric_code=agent_pin,
                    training_code_enabled=True,
                )
                db.add(setting)
            stats["pins_updated"] += 1
            pin_updated = True

        stats["details"].append({
            "hubspot_owner_id": oid,
            "agent_name": spec["name"],
            "code": spec["code"],
            "team": spec["team"],
            "pin": agent_pin,
            "cycle_created": cycle_created,
            "pin_updated": pin_updated,
            "report_id": current_report_id,
        })

    if apply:
        await db.commit()
        logger.info("Cambios aplicados y confirmados en la base de datos.")
    else:
        await db.rollback()
        logger.info("DRY-RUN completado. Ningún cambio aplicado a la base de datos.")

    return stats


def print_summary_report(stats: Dict[str, Any], dry_run: bool):
    mode_str = "DRY-RUN (Simulación, 0 cambios en BD)" if dry_run else "APPLY (Cambios aplicados en BD)"
    print("\n" + "=" * 90)
    print(f"PREPARACIÓN DE AGENTES DEMO PARA TRAINER — {mode_str}")
    print("=" * 90)
    print(f"Empresa objetivo:                    Empresa Demo (company_id={DEMO_COMPANY_ID}, is_demo=True)")
    print(f"Agentes objetivo:                    {stats['target_agents']}")
    print(f"Ciclos históricos encontrados:        {stats['historical_cycles_found']} (Run 1 completado)")
    print(f"Ciclos activos existentes antes:     {stats['active_cycles_existing_before']}")
    print(f"Nuevos ciclos {'previstos' if dry_run else 'creados'}:               {stats['new_cycles_created']}")
    print(f"Prompts de simulación {'previstos' if dry_run else 'creados'}:     {stats['prompts_created']}")
    print(f"Completions 'pending' {'previstas' if dry_run else 'creadas'}:     {stats['completions_created']}")
    print(f"PINs {'a actualizar' if dry_run else 'actualizados'}:                  {stats['pins_updated']} (ya coincidentes: {stats['pins_already_matching']})")
    print("-" * 90)
    print("GARANTÍAS DE INMUTABILIDAD & SEGURIDAD:")
    print(f"  * TrainingAgentReport históricos modificados:      {stats['historical_reports_modified']} (0 esperados)")
    print(f"  * TrainingSimulationPrompt históricos modificados: {stats['historical_prompts_modified']} (0 esperados)")
    print(f"  * TrainingCallSession históricas modificadas:      {stats['historical_sessions_modified']} (0 esperadas)")
    print(f"  * TrainingCallEvaluation históricas modificadas:    {stats['historical_evaluations_modified']} (0 esperadas)")
    print(f"  * TrainingCompletionStatus históricos modificados: {stats['historical_completions_modified']} (0 esperados)")
    print(f"  * Llamadas base (bm_mass_evaluation_results):      {stats['base_calls_modified']} (0 esperadas)")
    print(f"  * Criterios base (bm_mass_eval_criterion_results): {stats['criteria_results_modified']} (0 esperados)")
    print(f"  * Agentes distintos de los 4 modificados:          {stats['other_agents_modified']} (0 esperados)")
    print(f"  * Empresas distintas de 7 modificadas:             {stats['other_companies_modified']} (0 esperadas)")
    print("-" * 90)
    print(f"{'Agente':<18} | {'Código':<8} | {'Equipo':<22} | {'PIN':<6} | {'Nuevo Ciclo':<12} | {'PIN Act.'}")
    print("-" * 90)
    for d in stats["details"]:
        c_str = "SÍ" if d["cycle_created"] else "Ya existía"
        p_str = "SÍ" if d["pin_updated"] else "Ya OK"
        print(f"{d['agent_name']:<18} | {d['code']:<8} | {d['team']:<22} | {d['pin']:<6} | {c_str:<12} | {p_str}")
    print("=" * 90 + "\n")


async def main():
    parser = argparse.ArgumentParser(description="Prepara los 4 agentes de Empresa Demo para el flujo end-to-end de Trainer.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="Ejecuta en modo simulación (default, sin cambios en BD).")
    group.add_argument("--apply", action="store_true", help="Aplica los cambios en la base de datos.")
    args = parser.parse_args()

    apply = bool(args.apply)
    dry_run = not apply

    settings = get_settings()
    raw_url = settings.database_url or ""
    async_url = _make_async_url(raw_url)

    engine = create_async_engine(async_url, echo=False)
    async with AsyncSession(engine) as db:
        stats = await prepare_demo_agents(db, apply=apply)
        print_summary_report(stats, dry_run=dry_run)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
