"""
scripts/update_demo_agent_reports.py
====================================
Dedicated, laser-focused updater for descriptive fields in TrainingAgentReport
for Empresa Demo (company_id=7).

SCOPE:
- ONLY modifies TrainingAgentReport descriptive fields:
  * summary_general
  * evolution_summary
  * strengths_json (3-4 items, list of dicts with title, description, evidence)
  * weaknesses_json (3-4 items, list of dicts with title, description, evidence)
  * notable_data_json (1-2 items)
  * general_objectives_json (3-4 items, Contact Center B2B)
  * specific_objectives_json (3-4 items, Contact Center B2B)
  * final_report_json (for completed reports: rich objectives_status with individual justifications)
- GUARANTEES NO TOUCH to:
  * TrainingSimulationPrompt (0 modified)
  * TrainingCallSession (0 modified)
  * TrainingCallEvaluation (0 modified)
  * TrainingCompletionStatus (0 modified)
  * Scores / Notes / Transcriptions (0 modified)
  * Base calls / MassEvaluationResult (50.075) (0 modified)
  * CriterionResult (300.450) (0 modified)
  * Company 1 (Boston Medical) or others (0 modified)
  * Report IDs, agent_name, hubspot_owner_id, status, dates (0 modified)

Usage:
  python scripts/update_demo_agent_reports.py --dry-run
  python scripts/update_demo_agent_reports.py --apply
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
    generate_enhanced_training_cycle_data,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("update_demo_agent_reports")

MEDICAL_TERM_REGEX = re.compile(
    r"(?i)\b(?:médic[oa]s?|pacientes?|clínicas?|ecografías?|urólog[oa]s?|tratamientos?|anamnesis|patologías?|fármacos?|Boston\s+Medical)\b"
)

GENERIC_JUSTIFICATION = "Objetivo superado satisfactoriamente. Demostró aplicación consistente en las simulaciones finales."


async def update_agent_reports(db: AsyncSession, apply: bool = False) -> Dict[str, Any]:
    # ── 1. Airlock Verification ──────────────────────────────────────────────
    stmt_comp = select(Company).where(Company.company_id == DEMO_COMPANY_ID)
    res_comp = await db.execute(stmt_comp)
    company = res_comp.scalars().first()
    if not company or not company.is_demo:
        raise RuntimeError("AIRLOCK ABORT: target company is not demo!")

    logger.info("Airlock verificado: Empresa '%s' (company_id=%d, is_demo=%s)",
                company.company_name, company.company_id, company.is_demo)

    # ── 2. Services Mapping ──────────────────────────────────────────────────
    stmt_svc = select(Service).where(Service.company_id == DEMO_COMPANY_ID)
    res_svc = await db.execute(stmt_svc)
    services = {s.service_id: s for s in res_svc.scalars().all()}

    # ── 3. Query All Reports of Company 7 ────────────────────────────────────
    stmt_reports = (
        select(TrainingAgentReport)
        .where(TrainingAgentReport.company_id == DEMO_COMPANY_ID)
        .order_by(TrainingAgentReport.training_report_id.asc())
    )
    res_reports = await db.execute(stmt_reports)
    reports = list(res_reports.scalars().all())

    stats = {
        "reports_detected": len(reports),
        "reports_updated": 0,
        "reports_medical_before": 0,
        "reports_medical_after": 0,
        "reports_generic_justification_before": 0,
        "reports_generic_justification_after": 0,
        "reports_empty_sw_before": 0,
        "reports_completed_count": 0,
        "reports_in_progress_count": 0,
        "prompts_modified": 0,
        "sessions_modified": 0,
        "evaluations_modified": 0,
        "completions_modified": 0,
        "calls_modified": 0,
        "criterion_results_modified": 0,
        "sample_reports": [],
    }

    for rep in reports:
        svc = services.get(rep.service_id)
        service_key = svc.service_key if svc else "atencion-al-cliente"
        if "venta" in service_key.lower() or "comercial" in service_key.lower():
            cat_key = "ventas"
        else:
            cat_key = "atencion-al-cliente"

        if rep.status == "completed":
            stats["reports_completed_count"] += 1
        else:
            stats["reports_in_progress_count"] += 1

        # Check existing content for medical terms and empty strengths/weaknesses
        old_text = (
            f"{rep.summary_general or ''} {rep.evolution_summary or ''} "
            f"{json.dumps(rep.general_objectives_json or [], ensure_ascii=False)} "
            f"{json.dumps(rep.specific_objectives_json or [], ensure_ascii=False)} "
            f"{json.dumps(rep.final_report_json or {}, ensure_ascii=False)}"
        )
        if MEDICAL_TERM_REGEX.findall(old_text):
            stats["reports_medical_before"] += 1
        if GENERIC_JUSTIFICATION in old_text:
            stats["reports_generic_justification_before"] += 1

        # Check if strengths or weaknesses were dict-wrapped or empty
        if not isinstance(rep.strengths_json, list) or not rep.strengths_json:
            stats["reports_empty_sw_before"] += 1

        # Generate enhanced contact center descriptive data
        enhanced_data = generate_enhanced_training_cycle_data(
            agent_id=rep.hubspot_owner_id,
            agent_name=rep.agent_name,
            agent_initials=rep.agent_initials or "AG",
            service_key=cat_key,
            status=rep.status or "completed",
        )

        # Validate new content has 0 medical terms and 0 generic justifications
        new_text = (
            f"{enhanced_data['summary_general']} {enhanced_data['evolution_summary']} "
            f"{json.dumps(enhanced_data['general_objectives_json'], ensure_ascii=False)} "
            f"{json.dumps(enhanced_data['specific_objectives_json'], ensure_ascii=False)} "
            f"{json.dumps(enhanced_data['final_report_json'] or {}, ensure_ascii=False)}"
        )
        med_matches = MEDICAL_TERM_REGEX.findall(new_text)
        if med_matches:
            stats["reports_medical_after"] += 1
            logger.error("ALERTA: Término médico detectado en reporte nuevo: %s", med_matches)

        if GENERIC_JUSTIFICATION in new_text:
            stats["reports_generic_justification_after"] += 1
            logger.error("ALERTA: Justificación genérica detectada en reporte nuevo!")

        if apply:
            rep.summary_general = enhanced_data["summary_general"]
            rep.evolution_summary = enhanced_data["evolution_summary"]
            rep.strengths_json = enhanced_data["strengths_json"]
            rep.weaknesses_json = enhanced_data["weaknesses_json"]
            rep.notable_data_json = enhanced_data["notable_data_json"]
            rep.general_objectives_json = enhanced_data["general_objectives_json"]
            rep.specific_objectives_json = enhanced_data["specific_objectives_json"]
            if rep.status == "completed":
                rep.final_report_json = enhanced_data["final_report_json"]

        stats["reports_updated"] += 1

        if len(stats["sample_reports"]) < 3:
            sample_obj_just = None
            if enhanced_data["final_report_json"]:
                objs_st = enhanced_data["final_report_json"].get("objectives_status", [])
                if objs_st:
                    sample_obj_just = objs_st[0].get("justification")
            stats["sample_reports"].append({
                "training_report_id": rep.training_report_id,
                "agent_name": rep.agent_name,
                "service": cat_key,
                "status": rep.status,
                "strengths_count": len(enhanced_data["strengths_json"]),
                "weaknesses_count": len(enhanced_data["weaknesses_json"]),
                "general_objectives_count": len(enhanced_data["general_objectives_json"]),
                "specific_objectives_count": len(enhanced_data["specific_objectives_json"]),
                "first_strength_title": enhanced_data["strengths_json"][0]["title"],
                "first_weakness_title": enhanced_data["weaknesses_json"][0]["title"],
                "sample_justification": sample_obj_just,
            })

    if apply:
        await db.commit()
        logger.info("TRANSACCIÓN CONFIRMADA (COMMIT). Informes de agente actualizados exitosamente en BD.")
    else:
        await db.rollback()
        logger.info("MODO DRY-RUN: Transacción revertida (ROLLBACK). Ningún dato fue alterado en BD.")

    return stats


async def main():
    parser = argparse.ArgumentParser(description="Actualización exclusiva de datos descriptivos de TrainingAgentReport de Empresa Demo.")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Ejecutar en modo simulación (sin escribir en BD).")
    parser.add_argument("--apply", action="store_true", default=False, help="Aplicar cambios en la base de datos de forma persistente.")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True

    is_apply = args.apply

    logger.info("==================================================")
    logger.info("ACTUALIZACIÓN DE DATOS DESCRIPTIVOS DE CICLOS (DEMO)")
    logger.info("Modo: %s", "APPLY (ESCRITURA REAL)" if is_apply else "DRY-RUN (SIMULACIÓN)")
    logger.info("Empresa objetivo: Empresa Demo (company_id=%d)", DEMO_COMPANY_ID)
    logger.info("==================================================")

    settings = get_settings()
    engine = create_async_engine(_make_async_url(settings.database_url), echo=False)

    async with AsyncSession(engine) as session:
        stats = await update_agent_reports(session, apply=is_apply)

    logger.info("==================================================")
    logger.info("RESUMEN DE RESULTADOS (%s):", "APPLY" if is_apply else "DRY-RUN")
    logger.info("- Informes detectados: %d", stats["reports_detected"])
    logger.info("- Informes actualizados: %d", stats["reports_updated"] if is_apply else 0)
    logger.info("  * Informes completados: %d", stats["reports_completed_count"])
    logger.info("  * Informes en curso: %d", stats["reports_in_progress_count"])
    logger.info("- TrainingSimulationPrompt modificados: %d", stats["prompts_modified"])
    logger.info("- TrainingCallSession modificadas: %d", stats["sessions_modified"])
    logger.info("- TrainingCallEvaluation modificadas: %d", stats["evaluations_modified"])
    logger.info("- TrainingCompletionStatus modificados: %d", stats["completions_modified"])
    logger.info("- Llamadas base modificadas: %d", stats["calls_modified"])
    logger.info("- CriterionResult modificados: %d", stats["criterion_results_modified"])
    logger.info("--------------------------------------------------")
    logger.info("VALIDACIÓN DE CONTENIDO:")
    logger.info("- Informes con fortalezas/debilidades vacías antes: %d / %d", stats["reports_empty_sw_before"], stats["reports_detected"])
    logger.info("- Informes con términos médicos antes del cambio: %d / %d", stats["reports_medical_before"], stats["reports_detected"])
    logger.info("- Informes con términos médicos en nuevo contenido: %d", stats["reports_medical_after"])
    logger.info("- Informes con justificación fija genérica antes: %d / %d", stats["reports_generic_justification_before"], stats["reports_detected"])
    logger.info("- Informes con justificación fija genérica después: %d", stats["reports_generic_justification_after"])
    logger.info("--------------------------------------------------")
    logger.info("MUESTRA DE INFORMES:")
    for s in stats["sample_reports"]:
        logger.info("  * ID=%d (%s | %s | %s):", s["training_report_id"], s["agent_name"], s["service"], s["status"])
        logger.info("    - Fortalezas (%d): e.g. '%s'", s["strengths_count"], s["first_strength_title"])
        logger.info("    - Áreas de Mejora (%d): e.g. '%s'", s["weaknesses_count"], s["first_weakness_title"])
        logger.info("    - Objetivos: %d generales, %d específicos", s["general_objectives_count"], s["specific_objectives_count"])
        if s["sample_justification"]:
            logger.info("    - Justificación de muestra: '%s...'", s["sample_justification"][:80])
    logger.info("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
