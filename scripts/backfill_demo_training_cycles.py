"""
scripts/backfill_demo_training_cycles.py
========================================
Safe, idempotent backfill script to upgrade and enrich existing training cycles,
objectives, roleplay simulation prompts, call sessions, and evaluations for
Empresa Demo (company_id=7).

SAFETY & AIRLOCK:
- Strictly scoped to company_id=7 (Empresa Demo).
- Confirms is_demo is True.
- NEVER modifies bm_mass_evaluation_results or bm_mass_evaluation_criterion_results (the 50,075 calls remain intact).
- NEVER modifies company_id=1 (Boston Medical) or any other company.
- Supports --dry-run (default) and --apply.

Usage:
  python scripts/backfill_demo_training_cycles.py --dry-run
  python scripts/backfill_demo_training_cycles.py --apply
"""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import select, and_, func
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
    TrainingEvaluationPrompt,
)
from app.services.demo_training_cycle_enhancer import (
    DEMO_COMPANY_ID,
    generate_enhanced_training_cycle_data,
    generate_simulation_evaluation_data,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_demo_training_cycles")


async def backfill_training_cycles(db: AsyncSession, apply: bool = False) -> Dict[str, Any]:
    # ── 1. Tenant Verification & Safety Airlock ──────────────────────────────
    stmt_comp = select(Company).where(Company.company_id == DEMO_COMPANY_ID)
    res_comp = await db.execute(stmt_comp)
    company = res_comp.scalars().first()

    if not company:
        raise ValueError(f"Empresa Demo con ID {DEMO_COMPANY_ID} no encontrada en la base de datos.")
    if not company.is_demo:
        raise ValueError(f"CRITICAL: La empresa {DEMO_COMPANY_ID} ('{company.company_name}') NO está marcada como is_demo.")
    if company.company_id != DEMO_COMPANY_ID:
        raise ValueError(f"CRITICAL: Violación de aislamiento. Intentando modificar company_id={company.company_id}.")

    logger.info("Airlock verificado: Empresa '%s' (company_id=%d, is_demo=%s)", company.company_name, company.company_id, company.is_demo)

    # ── 2. Resolve Services & Default Evaluation Prompts ─────────────────────
    stmt_svc = select(Service).where(Service.company_id == DEMO_COMPANY_ID)
    res_svc = await db.execute(stmt_svc)
    services = {s.service_id: s for s in res_svc.scalars().all()}
    services_by_key = {s.service_key: s for s in services.values()}

    eval_prompts: Dict[int, TrainingEvaluationPrompt] = {}
    for svc_id, s_obj in services.items():
        ep_res = await db.execute(
            select(TrainingEvaluationPrompt).where(
                TrainingEvaluationPrompt.service_id == svc_id,
                TrainingEvaluationPrompt.is_active == True,
            )
        )
        ep = ep_res.scalars().first()
        if not ep:
            logger.info("Creando TrainingEvaluationPrompt por defecto para servicio '%s' (ID %d)...", s_obj.service_name, svc_id)
            if apply:
                ep = TrainingEvaluationPrompt(
                    service_id=svc_id,
                    company_id=DEMO_COMPANY_ID,
                    prompt_text="Evalúa la simulación médica según los criterios del protocolo Boston Medical.",
                    version=1,
                    is_active=True,
                    created_by="backfill_system",
                )
                db.add(ep)
                await db.flush()
        if ep:
            eval_prompts[svc_id] = ep

    default_ep_id = list(eval_prompts.values())[0].id if eval_prompts else 1

    # ── 3. Query All Existing Reports for Company 7 ──────────────────────────
    stmt_reports = (
        select(TrainingAgentReport)
        .where(TrainingAgentReport.company_id == DEMO_COMPANY_ID)
        .order_by(TrainingAgentReport.training_report_id.asc())
    )
    res_reports = await db.execute(stmt_reports)
    reports = list(res_reports.scalars().all())

    logger.info("Encontrados %d informes de entrenamiento para Empresa Demo (company_id=%d)", len(reports), DEMO_COMPANY_ID)

    stats = {
        "reports_audited": len(reports),
        "reports_updated": 0,
        "completed_reports_enriched": 0,
        "active_reports_enriched": 0,
        "prompts_updated": 0,
        "prompts_created": 0,
        "sessions_created": 0,
        "evaluations_created": 0,
        "completions_linked": 0,
        "strengths_upgraded": 0,
        "weaknesses_upgraded": 0,
        "general_objectives_upgraded": 0,
        "specific_objectives_created": 0,
        "final_reports_enriched": 0,
    }

    # ── 4. Process Each Report ───────────────────────────────────────────────
    for r in reports:
        # Determine service key
        svc_obj = services.get(r.service_id)
        service_key = svc_obj.service_key if svc_obj else "atencion-al-cliente"

        # Generate rich data payload
        cycle_data = generate_enhanced_training_cycle_data(
            agent_id=r.hubspot_owner_id,
            agent_name=r.agent_name,
            agent_initials=r.agent_initials or "AD",
            service_key=service_key,
            status=r.status or "in_progress",
            cycle_index=r.training_run_id or 1
        )

        # Update Report Fields
        r.summary_general = cycle_data["summary_general"]
        r.evolution_summary = cycle_data["evolution_summary"]
        r.strengths_json = cycle_data["strengths_json"]
        r.weaknesses_json = cycle_data["weaknesses_json"]
        r.notable_data_json = cycle_data["notable_data_json"]
        r.general_objectives_json = cycle_data["general_objectives_json"]
        r.specific_objectives_json = cycle_data["specific_objectives_json"]

        if r.status == "completed":
            r.final_report_json = cycle_data["final_report_json"]
            stats["final_reports_enriched"] += 1
            stats["completed_reports_enriched"] += 1
        else:
            stats["active_reports_enriched"] += 1

        if r.avg_evaluacion_global is None:
            r.avg_evaluacion_global = cycle_data["avg_score"]

        stats["reports_updated"] += 1
        stats["strengths_upgraded"] += 1
        stats["weaknesses_upgraded"] += 1
        stats["general_objectives_upgraded"] += 1
        stats["specific_objectives_created"] += 1

        # ── 5. Process Simulation Prompts ────────────────────────────────────
        stmt_p = (
            select(TrainingSimulationPrompt)
            .where(TrainingSimulationPrompt.training_report_id == r.training_report_id)
            .order_by(TrainingSimulationPrompt.prompt_number.asc())
        )
        res_p = await db.execute(stmt_p)
        existing_prompts = list(res_p.scalars().all())

        enhanced_prompts = cycle_data["prompts"]

        # Upgrade existing prompts
        for idx, p in enumerate(existing_prompts):
            p_data = enhanced_prompts[idx % len(enhanced_prompts)]
            p.title = p_data["title"]
            p.scenario_type = p_data["scenario_type"]
            p.objective_focus_json = p_data["objective_focus_json"]
            p.prompt_text = p_data["prompt_text"]
            stats["prompts_updated"] += 1

        # Add missing prompts if report has fewer prompts than desired
        prompts_to_add = len(enhanced_prompts) - len(existing_prompts)
        if prompts_to_add > 0:
            for idx in range(len(existing_prompts), len(enhanced_prompts)):
                p_data = enhanced_prompts[idx]
                new_p = TrainingSimulationPrompt(
                    training_report_id=r.training_report_id,
                    hubspot_owner_id=r.hubspot_owner_id,
                    prompt_number=idx + 1,
                    title=p_data["title"],
                    scenario_type=p_data["scenario_type"],
                    objective_focus_json=p_data["objective_focus_json"],
                    prompt_text=p_data["prompt_text"],
                )
                if apply:
                    db.add(new_p)
                    await db.flush()
                existing_prompts.append(new_p)
                stats["prompts_created"] += 1

        # ── 6. Process Completion Statuses, Sessions & Evaluations ───────────
        ep_obj = eval_prompts.get(r.service_id)
        ep_id = ep_obj.id if ep_obj else default_ep_id

        for idx, p in enumerate(existing_prompts):
            # Fetch or create completion status
            stmt_c = select(TrainingCompletionStatus).where(
                and_(
                    TrainingCompletionStatus.training_report_id == r.training_report_id,
                    TrainingCompletionStatus.simulation_prompt_id == p.simulation_prompt_id,
                )
            )
            res_c = await db.execute(stmt_c)
            comp = res_c.scalars().first()

            should_be_completed = (r.status == "completed") or (r.status == "in_progress" and idx == 0)

            if not comp:
                comp = TrainingCompletionStatus(
                    training_report_id=r.training_report_id,
                    simulation_prompt_id=p.simulation_prompt_id,
                    hubspot_owner_id=r.hubspot_owner_id,
                    status="completed" if should_be_completed else "pending",
                )
                if apply:
                    db.add(comp)
                    await db.flush()

            if should_be_completed:
                comp.status = "completed"
                comp_completed_at = r.period_end or datetime.now(timezone.utc)
                comp.completed_at = comp_completed_at

                # Check if session exists
                stmt_s = select(TrainingCallSession).where(
                    and_(
                        TrainingCallSession.cycle_id == r.training_report_id,
                        TrainingCallSession.conversation_id == p.simulation_prompt_id,
                    )
                )
                res_s = await db.execute(stmt_s)
                sess = res_s.scalars().first()

                if not sess and apply:
                    sess = TrainingCallSession(
                        call_sid=f"CA_demo_bf_{r.training_report_id}_{p.simulation_prompt_id}_{r.hubspot_owner_id}",
                        recording_url=f"https://storage.doobot.ai/demo/recordings/{r.hubspot_owner_id}_{p.simulation_prompt_id}.mp3",
                        agent_id=r.hubspot_owner_id,
                        cycle_id=r.training_report_id,
                        conversation_id=p.simulation_prompt_id,
                        status="completed",
                        started_at=comp_completed_at - timedelta(minutes=7),
                        ended_at=comp_completed_at - timedelta(minutes=1),
                        recording_ready_at=comp_completed_at - timedelta(seconds=45),
                        evaluation_completed_at=comp_completed_at,
                    )
                    db.add(sess)
                    await db.flush()
                stats["sessions_created"] += 1

                # Check if evaluation exists
                eval_obj = None
                if sess:
                    stmt_ev = select(TrainingCallEvaluation).where(
                        and_(
                            TrainingCallEvaluation.cycle_id == r.training_report_id,
                            TrainingCallEvaluation.conversation_id == p.simulation_prompt_id,
                        )
                    )
                    res_ev = await db.execute(stmt_ev)
                    eval_obj = res_ev.scalars().first()

                if not eval_obj and apply and sess:
                    eval_res = generate_simulation_evaluation_data(
                        prompt_title=p.title,
                        prompt_number=p.prompt_number,
                        agent_name=r.agent_name,
                        service_key=service_key,
                        agent_score_tier=cycle_data["tier"]
                    )
                    eval_obj = TrainingCallEvaluation(
                        session_id=sess.session_id,
                        cycle_id=r.training_report_id,
                        conversation_id=p.simulation_prompt_id,
                        agent_id=r.hubspot_owner_id,
                        prompt_version_id=ep_id,
                        transcription=eval_res["transcription"],
                        result_json=eval_res["result_json"],
                        score=eval_res["score"],
                        feedback=eval_res["feedback"],
                        created_at=comp_completed_at,
                    )
                    db.add(eval_obj)
                    await db.flush()
                stats["evaluations_created"] += 1

                # Link IDs in completion
                if sess:
                    comp.call_session_id = sess.session_id
                if eval_obj:
                    comp.evaluation_id = eval_obj.evaluation_id
                    comp.notes = f"Simulación completada y evaluada con nota {eval_obj.score}/10."
                stats["completions_linked"] += 1
            else:
                # Pending simulation
                comp.status = "pending"
                comp.call_session_id = None
                comp.evaluation_id = None
                comp.completed_at = None

    if apply:
        await db.commit()
        logger.info("TRANSACCIÓN CONFIRMADA (COMMIT). Backfill aplicado exitosamente.")
    else:
        await db.rollback()
        logger.info("MODO DRY-RUN: Transacción revertida (ROLLBACK). Ningún dato fue alterado.")

    return stats


async def main():
    parser = argparse.ArgumentParser(description="Backfill y enriquecimiento de ciclos de entrenamiento de Empresa Demo.")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Ejecutar en modo simulación (sin escribir en BD).")
    parser.add_argument("--apply", action="store_true", default=False, help="Aplicar cambios en la base de datos de forma persistente.")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True

    is_apply = args.apply

    logger.info("==================================================")
    logger.info("INICIANDO BACKFILL DE CICLOS DE ENTRENAMIENTO")
    logger.info("Modo: %s", "APPLY (ESCRITURA REAL)" if is_apply else "DRY-RUN (SIMULACIÓN)")
    logger.info("Empresa objetivo: Empresa Demo (company_id=%d)", DEMO_COMPANY_ID)
    logger.info("==================================================")

    settings = get_settings()
    engine = create_async_engine(_make_async_url(settings.database_url), echo=False)

    async with AsyncSession(engine) as session:
        stats = await backfill_training_cycles(session, apply=is_apply)

    logger.info("==================================================")
    logger.info("RESUMEN DE RESULTADOS (%s):", "APPLY" if is_apply else "DRY-RUN")
    logger.info("- Informes auditados: %d", stats["reports_audited"])
    logger.info("- Informes actualizados: %d", stats["reports_updated"])
    logger.info("  * Ciclos completados enriquecidos: %d", stats["completed_reports_enriched"])
    logger.info("  * Ciclos activos enriquecidos: %d", stats["active_reports_enriched"])
    logger.info("- Fortalezas actualizadas (formato lista): %d", stats["strengths_upgraded"])
    logger.info("- Áreas de mejora actualizadas (formato lista): %d", stats["weaknesses_upgraded"])
    logger.info("- Objetivos generales actualizados (>=3): %d", stats["general_objectives_upgraded"])
    logger.info("- Objetivos específicos creados (>=3): %d", stats["specific_objectives_created"])
    logger.info("- Informes finales enriquecidos (con objectives_status): %d", stats["final_reports_enriched"])
    logger.info("- Prompts de simulación actualizados (>600 caracteres): %d", stats["prompts_updated"])
    logger.info("- Prompts de simulación creados: %d", stats["prompts_created"])
    logger.info("- Sesiones de llamada creadas: %d", stats["sessions_created"])
    logger.info("- Evaluaciones simuladas creadas: %d", stats["evaluations_created"])
    logger.info("- Completions vinculados con evaluation_id: %d", stats["completions_linked"])
    logger.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
