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
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath("."))

MEDICAL_TERM_REGEX = re.compile(
    r"(?i)\b(?:médic[oa]s?|pacientes?|clínicas?|ecografías?|urólog[oa]s?|tratamientos?|anamnesis|patologías?|fármacos?|Boston\s+Medical)\b"
)

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
                    prompt_text="Evalúa la simulación de contact center según los criterios de calidad y atención al cliente.",
                    version=1,
                    is_active=True,
                    created_by="backfill_system",
                )
                db.add(ep)
                await db.flush()
        if ep:
            eval_prompts[svc_id] = ep

    default_ep_id = list(eval_prompts.values())[0].id if eval_prompts else 1

    # ── 3. Query All Agents & Existing Reports for Company 7 ─────────────────
    stmt_agents = (
        select(User)
        .where(User.company_id == DEMO_COMPANY_ID, User.role == "agent")
        .order_by(User.user_id.asc())
    )
    res_agents = await db.execute(stmt_agents)
    all_agents = list(res_agents.scalars().all())

    stmt_reports = (
        select(TrainingAgentReport)
        .where(TrainingAgentReport.company_id == DEMO_COMPANY_ID)
        .order_by(TrainingAgentReport.training_report_id.asc())
    )
    res_reports = await db.execute(stmt_reports)
    reports = list(res_reports.scalars().all())

    existing_agent_ids = {r.hubspot_owner_id for r in reports}
    missing_agents = [a for a in all_agents if a.hubspot_owner_id not in existing_agent_ids]

    logger.info(
        "Empresa Demo: %d agentes totales, %d con informe existente, %d sin ciclo formativo.",
        len(all_agents), len(existing_agent_ids), len(missing_agents)
    )

    stats = {
        "total_agents": len(all_agents),
        "existing_agents_with_reports": len(existing_agent_ids),
        "missing_agents_without_reports": len(missing_agents),
        "reports_audited": len(reports),
        "reports_updated": 0,
        "completed_reports_enriched": 0,
        "active_reports_enriched": 0,
        "new_reports_created": 0,
        "new_prompts_created": 0,
        "new_sessions_created": 0,
        "new_evaluations_created": 0,
        "new_completions_created": 0,
        "strengths_upgraded": 0,
        "weaknesses_upgraded": 0,
        "general_objectives_upgraded": 0,
        "specific_objectives_created": 0,
        "final_reports_enriched": 0,
        "prompts_audited": 0,
        "prompts_updated": 0,
        "prompts_created": 0,
        "prompts_with_medical_refs": 0,
        "prompts_medical_details": [],
        "sessions_created": 0,
        "sessions_modified": 0,
        "evaluations_created": 0,
        "evaluations_modified": 0,
        "completions_modified": 0,
        "base_calls_modified": 0,
        "criterion_results_modified": 0,
        "reports_with_medical_refs_before": 0,
        "reports_medical_details_before": [],
    }

    # ── 4. Process Each Report ───────────────────────────────────────────────
    for r in reports:
        # Check medical references in report before update
        rep_text = (
            f"{r.summary_general or ''} {r.evolution_summary or ''} "
            f"{json.dumps(r.strengths_json or {}, ensure_ascii=False)} "
            f"{json.dumps(r.weaknesses_json or {}, ensure_ascii=False)} "
            f"{json.dumps(r.general_objectives_json or {}, ensure_ascii=False)} "
            f"{json.dumps(r.specific_objectives_json or {}, ensure_ascii=False)} "
            f"{json.dumps(r.final_report_json or {}, ensure_ascii=False)}"
        )
        found_med_rep = MEDICAL_TERM_REGEX.findall(rep_text)
        if found_med_rep:
            stats["reports_with_medical_refs_before"] += 1
            stats["reports_medical_details_before"].append({
                "training_report_id": r.training_report_id,
                "agent_id": r.hubspot_owner_id,
                "agent_name": r.agent_name,
                "matched_terms": sorted(list(set(found_med_rep))),
            })

        # Determine service key
        svc_obj = services.get(r.service_id)
        service_key = svc_obj.service_key if svc_obj else "atencion-al-cliente"

        # Generate rich generic data payload
        cycle_data = generate_enhanced_training_cycle_data(
            agent_id=r.hubspot_owner_id,
            agent_name=r.agent_name,
            agent_initials=r.agent_initials or "AD",
            service_key=service_key,
            status=r.status or "in_progress",
            cycle_index=r.training_run_id or 1
        )

        # Update Report Fields (EXCLUSIVELY descriptive fields of TrainingAgentReport)
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

        # ── 5. Audit Simulation Prompts (READ-ONLY: DO NOT MODIFY) ───────────
        stmt_p = (
            select(TrainingSimulationPrompt)
            .where(TrainingSimulationPrompt.training_report_id == r.training_report_id)
            .order_by(TrainingSimulationPrompt.prompt_number.asc())
        )
        res_p = await db.execute(stmt_p)
        existing_prompts = list(res_p.scalars().all())

        for p in existing_prompts:
            stats["prompts_audited"] += 1
            p_text = f"{p.title or ''} {p.prompt_text or ''} {json.dumps(p.objective_focus_json or {}, ensure_ascii=False)}"
            found_med_prompt = MEDICAL_TERM_REGEX.findall(p_text)
            if found_med_prompt:
                stats["prompts_with_medical_refs"] += 1
                stats["prompts_medical_details"].append({
                    "simulation_prompt_id": p.simulation_prompt_id,
                    "prompt_number": p.prompt_number,
                    "training_report_id": r.training_report_id,
                    "agent_id": r.hubspot_owner_id,
                    "agent_name": r.agent_name,
                    "title": p.title,
                    "matched_terms": sorted(list(set(found_med_prompt))),
                })

    # ── 7. Generate Completed Training Cycles for Uncovered Agents ───────────
    if missing_agents:
        logger.info(
            "Iniciando generación de %d ciclos formativos históricos para agentes sin registro previo...",
            len(missing_agents)
        )

        # Retrieve or create historical Run 1
        stmt_run1 = (
            select(TrainingRun)
            .where(
                TrainingRun.company_id == DEMO_COMPANY_ID,
                TrainingRun.status == "completed"
            )
            .order_by(TrainingRun.training_run_id.asc())
        )
        res_run1 = await db.execute(stmt_run1)
        run1 = res_run1.scalars().first()

        if not run1:
            run1_id = next((r.training_run_id for r in reports if r.status == "completed"), None)
            if run1_id:
                run1 = await db.get(TrainingRun, run1_id)

        if not run1:
            logger.info("Creando TrainingRun histórico completado para Empresa Demo...")
            now_utc = datetime.now(timezone.utc)
            run1 = TrainingRun(
                company_id=DEMO_COMPANY_ID,
                service_id=list(services.keys())[0] if services else None,
                period_start=now_utc - timedelta(days=60),
                period_end=now_utc - timedelta(days=45),
                status="completed",
                triggered_by="scheduler",
                agents_total=len([r for r in reports if r.status == "completed"]) + len(missing_agents),
                agents_completed=len([r for r in reports if r.status == "completed"]) + len(missing_agents),
                started_at=now_utc - timedelta(days=60),
                finished_at=now_utc - timedelta(days=45),
            )
            if apply:
                db.add(run1)
                await db.flush()

        if run1 and apply:
            total_completed_agents = len([r for r in reports if r.status == "completed"]) + len(missing_agents)
            run1.agents_total = total_completed_agents
            run1.agents_completed = total_completed_agents

        p_start = run1.period_start if run1 else (datetime.now(timezone.utc) - timedelta(days=60))
        p_end = run1.period_end if run1 else (datetime.now(timezone.utc) - timedelta(days=45))

        for agent in missing_agents:
            svc_obj = services.get(agent.primary_service_id)
            service_key = svc_obj.service_key if svc_obj else "atencion-al-cliente"

            cycle_data = generate_enhanced_training_cycle_data(
                agent_id=agent.hubspot_owner_id,
                agent_name=agent.name,
                agent_initials=agent.agent_initials or "AD",
                service_key=service_key,
                status="completed",
                cycle_index=run1.training_run_id if run1 else 1,
            )

            new_rep = TrainingAgentReport(
                training_run_id=run1.training_run_id if run1 else 1,
                company_id=DEMO_COMPANY_ID,
                service_id=agent.primary_service_id or (list(services.keys())[0] if services else None),
                hubspot_owner_id=agent.hubspot_owner_id,
                agent_name=agent.name,
                agent_initials=agent.agent_initials or "AD",
                period_start=p_start,
                period_end=p_end,
                status="completed",
                cycle_mode="automatic",
                evaluations_count=12,
                calls_count=12,
                avg_evaluacion_global=cycle_data["avg_score"],
                summary_general=cycle_data["summary_general"],
                evolution_summary=cycle_data["evolution_summary"],
                strengths_json=cycle_data["strengths_json"],
                weaknesses_json=cycle_data["weaknesses_json"],
                notable_data_json=cycle_data["notable_data_json"],
                general_objectives_json=cycle_data["general_objectives_json"],
                specific_objectives_json=cycle_data["specific_objectives_json"],
                final_report_json=cycle_data["final_report_json"],
                is_current=True,
                generated_at=p_end,
                approved_at=p_end,
            )
            if apply:
                db.add(new_rep)
                await db.flush()

            stats["new_reports_created"] += 1
            stats["strengths_upgraded"] += 1
            stats["weaknesses_upgraded"] += 1
            stats["general_objectives_upgraded"] += 1
            stats["specific_objectives_created"] += 1
            stats["final_reports_enriched"] += 1

            ep_obj = eval_prompts.get(agent.primary_service_id)
            ep_id = ep_obj.id if ep_obj else default_ep_id

            for idx, p_info in enumerate(cycle_data["prompts"]):
                new_p = TrainingSimulationPrompt(
                    training_report_id=new_rep.training_report_id if apply else (1000 + stats["new_reports_created"]),
                    hubspot_owner_id=agent.hubspot_owner_id,
                    prompt_number=idx + 1,
                    title=p_info["title"],
                    scenario_type=p_info["scenario_type"],
                    objective_focus_json=p_info["objective_focus_json"],
                    prompt_text=p_info["prompt_text"],
                )
                if apply:
                    db.add(new_p)
                    await db.flush()
                stats["new_prompts_created"] += 1

                comp_time = p_end - timedelta(days=2, hours=idx)
                sess = None
                if apply:
                    sess = TrainingCallSession(
                        call_sid=f"CA_demo_bf_{new_rep.training_report_id}_{new_p.simulation_prompt_id}_{agent.hubspot_owner_id}",
                        recording_url=f"https://storage.doobot.ai/demo/recordings/{agent.hubspot_owner_id}_{new_p.simulation_prompt_id}.mp3",
                        agent_id=agent.hubspot_owner_id,
                        cycle_id=new_rep.training_report_id,
                        conversation_id=new_p.simulation_prompt_id,
                        status="completed",
                        started_at=comp_time - timedelta(minutes=7),
                        ended_at=comp_time - timedelta(minutes=1),
                        recording_ready_at=comp_time - timedelta(seconds=45),
                        evaluation_completed_at=comp_time,
                    )
                    db.add(sess)
                    await db.flush()
                stats["new_sessions_created"] += 1

                eval_res = generate_simulation_evaluation_data(
                    prompt_title=p_info["title"],
                    prompt_number=idx + 1,
                    agent_name=agent.name,
                    service_key=service_key,
                    agent_score_tier=cycle_data["tier"],
                    agent_id=agent.hubspot_owner_id,
                )
                ev = None
                if apply and sess:
                    ev = TrainingCallEvaluation(
                        session_id=sess.session_id,
                        cycle_id=new_rep.training_report_id,
                        conversation_id=new_p.simulation_prompt_id,
                        agent_id=agent.hubspot_owner_id,
                        prompt_version_id=ep_id,
                        transcription=eval_res["transcription"],
                        result_json=eval_res["result_json"],
                        score=eval_res["score"],
                        feedback=eval_res["feedback"],
                        created_at=comp_time,
                    )
                    db.add(ev)
                    await db.flush()
                stats["new_evaluations_created"] += 1

                if apply:
                    comp = TrainingCompletionStatus(
                        training_report_id=new_rep.training_report_id,
                        simulation_prompt_id=new_p.simulation_prompt_id,
                        hubspot_owner_id=agent.hubspot_owner_id,
                        status="completed",
                        completed_at=comp_time,
                        call_session_id=sess.session_id if sess else None,
                        evaluation_id=ev.evaluation_id if ev else None,
                        notes=f"Simulación evaluada con nota {eval_res['score']}/10.",
                    )
                    db.add(comp)
                stats["new_completions_created"] += 1

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
    logger.info("- Agentes totales: %d", stats["total_agents"])
    logger.info("- Agentes con ciclos existentes: %d", stats["existing_agents_with_reports"])
    logger.info("- Agentes sin ciclos: %d", stats["missing_agents_without_reports"])
    logger.info("- Informes existentes auditados: %d", stats["reports_audited"])
    logger.info("- Informes existentes que requieren revisión/enriquecimiento: %d", stats["reports_updated"])
    logger.info("  * Ciclos completados a enriquecer: %d", stats["completed_reports_enriched"])
    logger.info("  * Ciclos activos a enriquecer: %d", stats["active_reports_enriched"])
    logger.info("- Fortalezas a actualizar: %d", stats["strengths_upgraded"])
    logger.info("- Áreas de mejora a actualizar: %d", stats["weaknesses_upgraded"])
    logger.info("- Objetivos generales a actualizar: %d", stats["general_objectives_upgraded"])
    logger.info("- Objetivos específicos a actualizar: %d", stats["specific_objectives_created"])
    logger.info("- Informes finales/objectives_status a enriquecer: %d", stats["final_reports_enriched"])
    logger.info("- TrainingSimulationPrompt modificados: %d", stats["prompts_updated"])
    logger.info("- TrainingCallSession modificadas: %d", stats["sessions_modified"])
    logger.info("- TrainingCallEvaluation modificadas: %d", stats["evaluations_modified"])
    logger.info("- TrainingCompletionStatus modificados: %d", stats["completions_modified"])
    logger.info("- Llamadas base modificadas: %d", stats["base_calls_modified"])
    logger.info("- CriterionResult modificados: %d", stats["criterion_results_modified"])
    logger.info("--------------------------------------------------")
    logger.info("COMPROBACIÓN ESPECÍFICA DE CONTENIDO MÉDICO:")
    logger.info("A) Informes con referencias médicas antes del cambio: %d / %d", stats["reports_with_medical_refs_before"], stats["reports_audited"])
    logger.info("B) TrainingSimulationPrompt auditados: %d", stats["prompts_audited"])
    logger.info("   Prompts con referencias médicas detectadas (SOLO INFORME, NO MODIFICADOS): %d", stats["prompts_with_medical_refs"])
    for p_detail in stats["prompts_medical_details"]:
        logger.info(
            "   * Prompt ID=%s (#%s, Report ID=%s, Agente=%s): '%s' -> Términos: %s",
            p_detail["simulation_prompt_id"],
            p_detail["prompt_number"],
            p_detail["training_report_id"],
            p_detail["agent_name"],
            p_detail["title"],
            ", ".join(p_detail["matched_terms"])
        )
    logger.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
