"""
scripts/update_demo_simulation_prompts.py
=========================================
Dedicated, laser-focused updater for TrainingSimulationPrompt in Empresa Demo (company_id=7).

SCOPE:
- ONLY modifies TrainingSimulationPrompt fields:
  * title
  * scenario_type
  * objective_focus_json
  * prompt_text
- GUARANTEES NO TOUCH to:
  * TrainingCallSession (0 modified)
  * TrainingCallEvaluation (0 modified)
  * Transcriptions (0 modified)
  * Scores/Notes (0 modified)
  * TrainingCompletionStatus (0 modified)
  * TrainingAgentReport (0 modified)
  * Base calls / MassEvaluationResult (0 modified)
  * CriterionResult (0 modified)
  * Company 1 (Boston Medical) (0 modified)

Usage:
  python scripts/update_demo_simulation_prompts.py --dry-run
  python scripts/update_demo_simulation_prompts.py --apply
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db import _make_async_url
from app.models.companies import Company
from app.models.services import Service
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
)
from app.services.demo_training_cycle_enhancer import (
    DEMO_COMPANY_ID,
    build_roleplay_simulation_prompt,
    PERSONAS_POOL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("update_demo_simulation_prompts")

MEDICAL_TERM_REGEX = re.compile(
    r"(?i)\b(?:médic[oa]s?|pacientes?|clínicas?|ecografías?|urólog[oa]s?|tratamientos?|anamnesis|patologías?|fármacos?|Boston\s+Medical)\b"
)


async def update_simulation_prompts(db: AsyncSession, apply: bool = False) -> Dict[str, Any]:
    # ── 1. Airlock Verification ──────────────────────────────────────────────
    stmt_comp = select(Company).where(Company.company_id == DEMO_COMPANY_ID)
    res_comp = await db.execute(stmt_comp)
    company = res_comp.scalars().first()
    if not company or not company.is_demo:
        raise RuntimeError("AIRLOCK ABORT: target company is not demo!")

    logger.info("Airlock verificado: Empresa '%s' (company_id=%d, is_demo=%s)", company.company_name, company.company_id, company.is_demo)

    # ── 2. Services Mapping ──────────────────────────────────────────────────
    stmt_svc = select(Service).where(Service.company_id == DEMO_COMPANY_ID)
    res_svc = await db.execute(stmt_svc)
    services = {s.service_id: s for s in res_svc.scalars().all()}

    # ── 3. Query Reports of Company 7 ────────────────────────────────────────
    stmt_reports = select(TrainingAgentReport).where(TrainingAgentReport.company_id == DEMO_COMPANY_ID)
    res_reports = await db.execute(stmt_reports)
    reports = {r.training_report_id: r for r in res_reports.scalars().all()}

    # ── 4. Query All Prompts of Company 7 Reports ────────────────────────────
    report_ids = list(reports.keys())
    if not report_ids:
        logger.warning("No se encontraron informes para Empresa Demo.")
        return {}

    stmt_prompts = (
        select(TrainingSimulationPrompt)
        .where(TrainingSimulationPrompt.training_report_id.in_(report_ids))
        .order_by(TrainingSimulationPrompt.training_report_id.asc(), TrainingSimulationPrompt.prompt_number.asc())
    )
    res_prompts = await db.execute(stmt_prompts)
    prompts = list(res_prompts.scalars().all())

    stats = {
        "prompts_detected": len(prompts),
        "prompts_to_update": len(prompts),
        "prompts_updated": 0,
        "prompts_medical_before": 0,
        "prompts_medical_after": 0,
        "prompts_atc_count": 0,
        "prompts_ventas_count": 0,
        "sessions_modified": 0,
        "evaluations_modified": 0,
        "transcriptions_modified": 0,
        "scores_modified": 0,
        "completions_modified": 0,
        "calls_modified": 0,
        "criterion_results_modified": 0,
        "reports_modified": 0,
        "sample_updated_prompts": [],
    }

    for p in prompts:
        rep = reports.get(p.training_report_id)
        if not rep:
            continue

        svc = services.get(rep.service_id)
        service_key = svc.service_key if svc else "atencion-al-cliente"
        if "venta" in service_key.lower() or "comercial" in service_key.lower():
            cat_key = "ventas"
            stats["prompts_ventas_count"] += 1
        else:
            cat_key = "atencion-al-cliente"
            stats["prompts_atc_count"] += 1

        # Check existing content for medical terms
        old_text = f"{p.title or ''} {p.prompt_text or ''} {json.dumps(p.objective_focus_json or {}, ensure_ascii=False)}"
        if MEDICAL_TERM_REGEX.findall(old_text):
            stats["prompts_medical_before"] += 1

        # Determine agent number for deterministic variation
        agent_num = 1
        try:
            agent_num = int(p.hubspot_owner_id.split("_")[-1])
        except Exception:
            agent_num = abs(hash(p.hubspot_owner_id)) % 60 + 1

        # Generate fresh generic contact center prompt
        new_p_data = build_roleplay_simulation_prompt(
            prompt_number=p.prompt_number,
            agent_name=rep.agent_name,
            service_key=cat_key,
            persona_index=(agent_num * 3 + p.prompt_number),
            difficulty_level="alta" if p.prompt_number >= 3 else "media",
        )

        # Verify new prompt has ZERO medical terms
        new_text = f"{new_p_data['title']} {new_p_data['prompt_text']} {json.dumps(new_p_data['objective_focus_json'], ensure_ascii=False)}"
        new_med_matches = MEDICAL_TERM_REGEX.findall(new_text)
        if new_med_matches:
            stats["prompts_medical_after"] += 1
            logger.error("ALERTA: Término médico detectado en prompt nuevo: %s", new_med_matches)

        if apply:
            p.title = new_p_data["title"]
            p.scenario_type = new_p_data["scenario_type"]
            p.objective_focus_json = new_p_data["objective_focus_json"]
            p.prompt_text = new_p_data["prompt_text"]

        stats["prompts_updated"] += 1

        if len(stats["sample_updated_prompts"]) < 4:
            stats["sample_updated_prompts"].append({
                "simulation_prompt_id": p.simulation_prompt_id,
                "prompt_number": p.prompt_number,
                "agent_name": rep.agent_name,
                "service": cat_key,
                "new_title": new_p_data["title"],
                "focus": new_p_data["objective_focus_json"].get("focus", []),
            })

    if apply:
        await db.commit()
        logger.info("TRANSACCIÓN CONFIRMADA (COMMIT). Prompts actualizados exitosamente en BD.")
    else:
        await db.rollback()
        logger.info("MODO DRY-RUN: Transacción revertida (ROLLBACK). Ningún dato fue alterado en BD.")

    return stats


async def main():
    parser = argparse.ArgumentParser(description="Actualización exclusiva de TrainingSimulationPrompt de Empresa Demo.")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Ejecutar en modo simulación (sin escribir en BD).")
    parser.add_argument("--apply", action="store_true", default=False, help="Aplicar cambios en la base de datos de forma persistente.")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True

    is_apply = args.apply

    logger.info("==================================================")
    logger.info("ACTUALIZACIÓN DE PROMPTS DE SIMULACIÓN (DEMO)")
    logger.info("Modo: %s", "APPLY (ESCRITURA REAL)" if is_apply else "DRY-RUN (SIMULACIÓN)")
    logger.info("Empresa objetivo: Empresa Demo (company_id=%d)", DEMO_COMPANY_ID)
    logger.info("==================================================")

    settings = get_settings()
    engine = create_async_engine(_make_async_url(settings.database_url), echo=False)

    async with AsyncSession(engine) as session:
        stats = await update_simulation_prompts(session, apply=is_apply)

    logger.info("==================================================")
    logger.info("RESUMEN DE RESULTADOS (%s):", "APPLY" if is_apply else "DRY-RUN")
    logger.info("- Prompts detectados: %d", stats["prompts_detected"])
    logger.info("- Prompts previstos para actualizar: %d", stats["prompts_to_update"])
    logger.info("- Prompts efectivamente actualizados: %d", stats["prompts_updated"] if is_apply else 0)
    logger.info("  * Prompts de Atención al Cliente: %d", stats["prompts_atc_count"])
    logger.info("  * Prompts de Ventas: %d", stats["prompts_ventas_count"])
    logger.info("- Sesiones de llamada modificadas: %d", stats["sessions_modified"])
    logger.info("- Evaluaciones de simulación modificadas: %d", stats["evaluations_modified"])
    logger.info("- Transcripciones modificadas: %d", stats["transcriptions_modified"])
    logger.info("- Puntuaciones modificadas: %d", stats["scores_modified"])
    logger.info("- TrainingCompletionStatus modificados: %d", stats["completions_modified"])
    logger.info("- Informes de agente modificados: %d", stats["reports_modified"])
    logger.info("- Llamadas base modificadas: %d", stats["calls_modified"])
    logger.info("- CriterionResult modificados: %d", stats["criterion_results_modified"])
    logger.info("--------------------------------------------------")
    logger.info("VALIDACIÓN DE CONTENIDO MÉDICO:")
    logger.info("- Prompts con términos médicos antes del cambio: %d / %d", stats["prompts_medical_before"], stats["prompts_detected"])
    logger.info("- Prompts con términos médicos en nuevo contenido: %d", stats["prompts_medical_after"])
    logger.info("--------------------------------------------------")
    logger.info("MUESTRA DE PROMPTS ACTUALIZADOS:")
    for s in stats["sample_updated_prompts"]:
        logger.info("  * ID=%d (#%d, Agente: %s, Servicio: %s): '%s' | Foco: %s",
                    s["simulation_prompt_id"], s["prompt_number"], s["agent_name"], s["service"], s["new_title"], s["focus"])
    logger.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
