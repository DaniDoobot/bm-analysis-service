"""
Trainer Chatbot Service — Unified Scoped RAG for voice training knowledge.
Supports both text and audio input through a single reasoning and retrieval pipeline.
"""
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import io
import json
import logging
import re
from typing import Any, List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import InternalRole, normalize_role
from app.core.tenant_context import TenantContext
from app.models.mass_evaluations import MassEvaluationResult
from app.models.personalized_training import TrainingAgentReport, TrainingKnowledgeDocument
from app.models.trainer import TrainerSession
from app.models.users import User
from app.routers.personalized_training import enforce_agent_or_admin_ownership
from app.services import openai_service
from app.services.dashboard_service import get_agent_evolution

logger = logging.getLogger(__name__)

# Constants
MAX_AUDIO_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".webm", ".ogg", ".aac", ".flac"}
MAX_HISTORY_MESSAGES = 8
MAX_KNOWLEDGE_DOCUMENTS = 12

SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

SPANISH_WORDS_NUM = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3,
    "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
    "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "dieciséis": 16, "veinte": 20,
    "veintiuno": 21, "veinticinco": 25, "treinta": 30,
}

AGENT_BLOCKING_MSG = (
    "Puedo analizar tus evaluaciones con detalle en periodos de hasta 15 días para poder profundizar bien en cada aspecto. "
    "Si quieres, indícame una quincena o un rango de fechas concreto (por ejemplo, del 1 al 15 del mes) y lo revisamos a fondo."
)

ADMIN_BLOCKING_MSG = (
    "Puedo analizar el desempeño del agente en detalle en periodos de hasta 15 días "
    "para ofrecer un desglose pedagógico preciso. Por favor, acota la consulta a un intervalo "
    "de máximo 15 días (por ejemplo, una quincena o dos semanas concretas)."
)

AGENT_CLARIFICATION_MSG = (
    "Por favor, indícame las fechas o el tramo concreto que deseas analizar "
    "(con un máximo de 15 días) para revisarlo en detalle."
)

ADMIN_CLARIFICATION_MSG = (
    "Por favor, indica las fechas o el tramo concreto que deseas analizar para el agente "
    "(con un intervalo máximo de 15 días)."
)


@dataclass
class DetailedPeriodResult:
    is_detailed_period_request: bool
    exceeds_limit: bool = False
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    period_days: Optional[int] = None
    raw_expression: Optional[str] = None
    blocking_message: Optional[str] = None
    clarification_needed: bool = False
    is_fortnight: bool = False


class TrainerChatbotService:
    """Service orchestrating input parsing, scoping, knowledge retrieval, and grounded RAG responses."""

    @staticmethod
    async def validate_and_extract_input(
        message: Optional[str] = None,
        audio_file: Optional[Any] = None,
    ) -> Tuple[str, str]:
        """
        Validates input presence and exclusivity (either text OR audio, exactly one).
        If audio is provided, transcribes it into normalized query text.

        Returns:
            Tuple[str, str]: (query_text, input_type) where input_type is 'text' or 'audio'.
        """
        has_message = bool(message and str(message).strip())
        has_audio = (
            audio_file is not None
            and (isinstance(audio_file, (UploadFile, StarletteUploadFile)) or hasattr(audio_file, "file"))
        )

        if not has_message and not has_audio:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debe proporcionar un mensaje de texto o un archivo de audio.",
            )

        if has_message and has_audio:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debe enviar únicamente texto o audio, no ambos simultáneamente.",
            )

        if has_message:
            query_text = str(message).strip()
            return query_text, "text"

        # Audio branch
        filename = getattr(audio_file, "filename", None) or "audio.mp3"
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Formato de audio no permitido ('{ext}'). Formatos soportados: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}.",
            )

        try:
            audio_bytes = await audio_file.read()
        except Exception as read_err:
            logger.exception("Error leyendo bytes del archivo de audio: %s", read_err)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo leer el archivo de audio subido.",
            )
        finally:
            try:
                await audio_file.close()
            except Exception:
                pass

        if not audio_bytes or len(audio_bytes) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo de audio está vacío.",
            )

        if len(audio_bytes) > MAX_AUDIO_SIZE_BYTES:
            max_mb = MAX_AUDIO_SIZE_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El archivo de audio supera el tamaño máximo permitido ({max_mb} MB).",
            )

        try:
            transcription_res = await openai_service.transcribe_audio(audio_bytes, filename=filename)
        except Exception as e:
            logger.exception("Error durante la transcripción de audio en Trainer Chatbot: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Fallo en el servicio de transcripción de audio: {str(e)}",
            )

        raw_text = (
            transcription_res.get("text", "")
            if isinstance(transcription_res, dict)
            else str(transcription_res or "")
        )
        query_text = raw_text.strip()
        if not query_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se pudo transcribir ningún texto comprensible a partir del audio proporcionado.",
            )

        return query_text, "audio"

    @staticmethod
    def resolve_target_agent(
        current_user: User,
        requested_agent_id: Optional[str],
        context: TenantContext,
    ) -> str:
        """
        Determines the target hubspot_owner_id with strict tenant security:
        - AGENT: ALWAYS forced to current_user.hubspot_owner_id, ignoring any frontend requested_agent_id.
        - SUPERVISOR / COORDINATOR / ADMIN: validated against context scope.
        """
        norm_role = normalize_role(current_user.role)

        if norm_role == InternalRole.AGENT:
            if not current_user.hubspot_owner_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tu cuenta de usuario no está asociada a ningún HubSpot Owner ID.",
                )
            if requested_agent_id and requested_agent_id.strip() and requested_agent_id.strip() != current_user.hubspot_owner_id:
                logger.warning(
                    "Security notice: Agent user %s (%s) passed agent_id '%s' differing from authenticated identity '%s'. Overriding with authenticated identity.",
                    current_user.user_id,
                    current_user.email,
                    requested_agent_id.strip(),
                    current_user.hubspot_owner_id,
                )
            return current_user.hubspot_owner_id

        # Supervisors, coordinators, and admins
        if requested_agent_id and requested_agent_id.strip():
            target_agent = requested_agent_id.strip()
            # Reuse existing ownership validator
            enforce_agent_or_admin_ownership(current_user, target_agent, context)

            if context.allowed_agent_ids is not None and target_agent not in context.allowed_agent_ids:
                logger.warning(
                    "Security violation: user %s (%s) tried to query agent %s outside allowed_agent_ids %s",
                    current_user.user_id,
                    current_user.email,
                    target_agent,
                    context.allowed_agent_ids,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tienes permisos para consultar la formación de este agente.",
                )
            return target_agent

        # Fallback if no agent_id requested: check if user has own hubspot_owner_id
        if current_user.hubspot_owner_id:
            return current_user.hubspot_owner_id

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes especificar el 'agent_id' sobre el que deseas realizar la consulta.",
        )

    @staticmethod
    async def fetch_knowledge_documents(
        db: AsyncSession,
        context: TenantContext,
        target_agent_id: str,
        cycle_id: Optional[int] = None,
    ) -> List[TrainingKnowledgeDocument]:
        """
        Retrieves deterministic TrainingKnowledgeDocument records scoped by:
        - company_id (mandatory tenant isolation)
        - hubspot_owner_id == target_agent_id
        - cycle_id (optional filter)
        Deterministic SQL query on structured documents.
        """
        stmt = select(TrainingKnowledgeDocument).where(
            TrainingKnowledgeDocument.hubspot_owner_id == target_agent_id
        )

        # Multi-tenant scoping
        if context.company_id is not None:
            stmt = stmt.where(TrainingKnowledgeDocument.company_id == context.company_id)
        elif not context.is_super_admin and context.allowed_company_ids:
            stmt = stmt.where(TrainingKnowledgeDocument.company_id.in_(context.allowed_company_ids))

        if cycle_id is not None:
            stmt = stmt.where(TrainingKnowledgeDocument.cycle_id == cycle_id)
            stmt = stmt.order_by(
                TrainingKnowledgeDocument.document_type.desc(),  # 'simulation' before 'cycle'
                TrainingKnowledgeDocument.created_at.desc(),
                TrainingKnowledgeDocument.id.desc(),
            ).limit(20)
        else:
            stmt = stmt.order_by(
                TrainingKnowledgeDocument.created_at.desc(),
                TrainingKnowledgeDocument.id.desc(),
            ).limit(MAX_KNOWLEDGE_DOCUMENTS)

        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    def get_storage_key(
        company_id: Optional[Any],
        user_identifier: Optional[Any],
        key_type: str = "text",
        target_agent_id: Optional[Any] = None,
    ) -> str:
        """
        Generates the standard isolated storage key for client persistence.
        Guarantees that history is strictly partitioned by (company_id, user/agent_identity).
        If a supervisor is viewing a specific target agent, includes target_agent_id in key.
        """
        clean_company = str(company_id) if company_id is not None else "unknown_company"
        clean_user = str(user_identifier) if user_identifier is not None else "unknown_user"
        if target_agent_id is not None and str(target_agent_id).strip() and str(target_agent_id).strip() != clean_user:
            clean_target = str(target_agent_id).strip()
            return f"trainer_chat_{clean_company}_{clean_user}_target_{clean_target}_{key_type}"
        return f"trainer_chat_{clean_company}_{clean_user}_{key_type}"

    @staticmethod
    def sanitize_history(conversation_history_raw: Any) -> List[dict[str, str]]:
        """
        Parses and validates conversation history, limiting it to MAX_HISTORY_MESSAGES
        to prevent unbounded prompt growth.
        """
        if not conversation_history_raw:
            return []

        parsed = conversation_history_raw
        if isinstance(conversation_history_raw, str):
            try:
                parsed = json.loads(conversation_history_raw)
            except Exception:
                logger.debug("Failed parsing conversation_history as JSON; treating as empty.")
                return []

        if not isinstance(parsed, list):
            return []

        cleaned: List[dict[str, str]] = []
        for msg in parsed:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role in ["user", "assistant"] and isinstance(content, str) and content.strip():
                # Bound maximum individual message length to 2000 chars defensively
                cleaned.append({
                    "role": role,
                    "content": content.strip()[:2000],
                })

        return cleaned[-MAX_HISTORY_MESSAGES:]

    @classmethod
    async def build_agent_historical_profile(
        cls,
        db: AsyncSession,
        context: TenantContext,
        target_agent_id: str,
    ) -> str:
        """
        Builds a compact, structured historical profile covering the agent's entire available
        history: real calls evaluations, criteria trends, past training cycles with objectives
        (achieved/unachieved), and roleplay simulations feedback.
        """
        evolution_all: Optional[dict[str, Any]] = None
        evolution_recent: Optional[dict[str, Any]] = None
        try:
            evolution_all = await get_agent_evolution(
                db=db,
                hubspot_owner_id=target_agent_id,
                period="all",
                company_id=context.company_id,
                context=context,
            )
        except Exception as e:
            logger.warning("Error fetching complete agent evolution for %s: %s", target_agent_id, e)

        try:
            evolution_recent = await get_agent_evolution(
                db=db,
                hubspot_owner_id=target_agent_id,
                period="30d",
                company_id=context.company_id,
                context=context,
            )
        except Exception as e:
            logger.warning("Error fetching recent agent evolution for %s: %s", target_agent_id, e)

        reports: list[TrainingAgentReport] = []
        try:
            stmt_rep = select(TrainingAgentReport).where(
                TrainingAgentReport.hubspot_owner_id == target_agent_id
            )
            if context.company_id is not None:
                stmt_rep = stmt_rep.where(TrainingAgentReport.company_id == context.company_id)
            elif not context.is_super_admin and context.allowed_company_ids:
                stmt_rep = stmt_rep.where(TrainingAgentReport.company_id.in_(context.allowed_company_ids))
            stmt_rep = stmt_rep.order_by(TrainingAgentReport.created_at.asc())
            res_rep = await db.execute(stmt_rep)
            reports = list(res_rep.scalars().all())
        except Exception as e:
            logger.warning("Error fetching training reports for %s: %s", target_agent_id, e)

        sessions: list[TrainerSession] = []
        try:
            stmt_sess = select(TrainerSession).options(
                selectinload(TrainerSession.evaluation)
            ).where(
                TrainerSession.agent_id == target_agent_id,
            )
            if context.company_id is not None:
                stmt_sess = stmt_sess.where(TrainerSession.company_id == context.company_id)
            elif not context.is_super_admin and context.allowed_company_ids:
                stmt_sess = stmt_sess.where(TrainerSession.company_id.in_(context.allowed_company_ids))
            stmt_sess = stmt_sess.order_by(TrainerSession.created_at.asc())
            res_sess = await db.execute(stmt_sess)
            sessions = list(res_sess.scalars().all())
        except Exception as e:
            logger.warning("Error fetching trainer sessions for %s: %s", target_agent_id, e)

        summary_all = evolution_all.get("summary") if evolution_all and isinstance(evolution_all.get("summary"), dict) else {}
        trend_all = evolution_all.get("trend") if evolution_all and isinstance(evolution_all.get("trend"), dict) else {}
        total_calls = int(summary_all.get("total_analyses", 0))

        if total_calls == 0 and not reports and not sessions:
            return (
                "--- PERFIL HISTÓRICO Y EVOLUCIÓN COMPLETA DEL AGENTE ---\n"
                "No existen evaluaciones históricas de llamadas reales, ciclos formativos ni simulaciones registradas para este agente en la empresa.\n"
                "--------------------------------------------------------"
            )

        sections = ["--- PERFIL HISTÓRICO Y EVOLUCIÓN COMPLETA DEL AGENTE ---"]

        # 1. Resumen general de llamadas reales
        if total_calls > 0:
            avg_all = summary_all.get("avg_evaluacion_global")
            delta_val = trend_all.get("evaluacion_global_delta_first_last", 0.0) or 0.0
            delta_pct = trend_all.get("evaluacion_global_delta_pct", 0.0) or 0.0
            direction = trend_all.get("evaluacion_global_direction", "stable")
            interpretation = trend_all.get("interpretation", "")

            summary_recent = evolution_recent.get("summary") if evolution_recent and isinstance(evolution_recent.get("summary"), dict) else {}
            recent_calls = int(summary_recent.get("total_analyses", 0))
            recent_avg = summary_recent.get("avg_evaluacion_global")

            sections.append("#### 1. Resumen de Evaluaciones en Llamadas Reales")
            sections.append(f"- Total histórico de llamadas evaluadas: {total_calls} llamadas (todo el histórico disponible sin restricción de 90 días).")
            avg_all_str = f"{avg_all:.2f}/10" if avg_all is not None else "Sin nota"
            sections.append(f"- Puntuación media histórica global: {avg_all_str}.")

            if recent_calls > 0 and recent_avg is not None:
                sections.append(f"- Puntuación media en los últimos 30 días: {recent_avg:.2f}/10 ({recent_calls} llamadas recientes).")

            sign = "+" if delta_val > 0 else ""
            sections.append(f"- Tendencia global: {direction} ({sign}{delta_val:.1f} puntos / {sign}{delta_pct:.1f}%). {interpretation}")

            # 2. Fortalezas consistentes
            strengths = evolution_all.get("strengths", [])
            if strengths:
                sections.append("\n#### 2. Fortalezas Recurrentes y Consistentes (Criterios con mejor desempeño)")
                for s in strengths[:4]:
                    crit_name = s.get("criterion_name") or s.get("criterion_key", "")
                    sc = s.get("score")
                    cnt = s.get("count", 0)
                    sc_str = f"{sc:.1f}/10" if sc is not None else "N/A"
                    sections.append(f"- [Fortaleza] {crit_name}: nota media {sc_str} ({cnt} llamadas evaluadas).")

            # 3. Áreas de mejora recurrentes
            weaknesses = evolution_all.get("weaknesses", [])
            if weaknesses:
                sections.append("\n#### 3. Errores y Criterios Recurrentes con Mayor Dificultad (Foco de mejora)")
                for w in weaknesses[:4]:
                    crit_name = w.get("criterion_name") or w.get("criterion_key", "")
                    sc = w.get("score")
                    cnt = w.get("count", 0)
                    sc_str = f"{sc:.1f}/10" if sc is not None else "N/A"
                    sections.append(f"- [Área de mejora recurrente] {crit_name}: nota media {sc_str} ({cnt} llamadas evaluadas).")

            # 4. Evolución por criterios
            criteria_ev = evolution_all.get("criteria_evolution", [])
            if criteria_ev:
                sections.append("\n#### 4. Evolución Temporal por Criterios (Trayectoria completa)")
                for c in criteria_ev:
                    crit_name = c.get("criterion_name") or c.get("criterion_key", "")
                    f_avg = c.get("first_avg")
                    l_avg = c.get("last_avg")
                    cd = c.get("delta", 0.0)
                    cd_sign = "+" if cd > 0 else ""
                    c_dir = c.get("direction", "stable")
                    dir_label = "mejora" if c_dir == "up" else ("empeoramiento" if c_dir == "down" else "estable")
                    if f_avg is not None and l_avg is not None:
                        sections.append(
                            f"- {crit_name}: inicial {f_avg:.1f} -> reciente {l_avg:.1f} ({cd_sign}{cd:.1f}, {dir_label})."
                        )
        else:
            sections.append("#### 1. Evaluaciones de Llamadas Reales")
            sections.append("- No constan llamadas reales auditadas en el sistema para este agente.")

        # 5. Ciclos de formación y objetivos (Histórico Completo)
        if reports:
            total_cycles = len(reports)
            completed_cycles = sum(1 for r in reports if r.status == "completed")
            active_cycles = sum(1 for r in reports if r.status in ("in_progress", "pending_approval", "pending"))

            # Chronological order for trajectory
            reports_chrono = sorted(
                reports,
                key=lambda r: (r.period_start or r.created_at or datetime.min.replace(tzinfo=timezone.utc))
            )

            cycle_scores = [float(r.avg_evaluacion_global) for r in reports_chrono if r.avg_evaluacion_global is not None]
            evolution_str = ""
            if len(cycle_scores) >= 2:
                first_s = cycle_scores[0]
                last_s = cycle_scores[-1]
                delta_s = round(last_s - first_s, 2)
                d_sign = "+" if delta_s > 0 else ""
                s_dir = "mejora" if delta_s > 0.2 else ("caída" if delta_s < -0.2 else "estable")
                evolution_str = f"- Evolución media en ciclos: de {first_s:.1f}/10 (primeros ciclos) a {last_s:.1f}/10 (últimos ciclos) ({d_sign}{delta_s:.1f}, {s_dir})."
            elif cycle_scores:
                evolution_str = f"- Nota media global en ciclos formativos: {sum(cycle_scores)/len(cycle_scores):.1f}/10."

            superados_count = 0
            no_superados_count = 0
            failed_objs_freq: dict[str, int] = {}
            passed_objs_freq: dict[str, int] = {}
            accumulated_recommendations: list[str] = []

            for r in reports_chrono:
                final_json = r.final_report_json if isinstance(r.final_report_json, dict) else {}
                objs_status = final_json.get("objectives_status")

                if objs_status and isinstance(objs_status, list):
                    for obj in objs_status:
                        if not isinstance(obj, dict):
                            continue
                        o_title = (obj.get("title") or "Objetivo").strip()
                        o_status = (obj.get("status") or "").upper()
                        if "SUPERADO" in o_status and "NO" not in o_status:
                            superados_count += 1
                            passed_objs_freq[o_title] = passed_objs_freq.get(o_title, 0) + 1
                        elif "NO SUPERADO" in o_status or "FALLIDO" in o_status:
                            no_superados_count += 1
                            failed_objs_freq[o_title] = failed_objs_freq.get(o_title, 0) + 1
                else:
                    gen_objs = r.general_objectives_json if isinstance(r.general_objectives_json, list) else []
                    spec_objs = r.specific_objectives_json if isinstance(r.specific_objectives_json, list) else []
                    for g in gen_objs:
                        if isinstance(g, dict) and g.get("title"):
                            t = g.get("title").strip()
                            passed_objs_freq[t] = passed_objs_freq.get(t, 0) + 1
                    for s in spec_objs:
                        if isinstance(s, dict) and s.get("title"):
                            t = s.get("title").strip()
                            passed_objs_freq[t] = passed_objs_freq.get(t, 0) + 1

                recs = final_json.get("recommendations") or final_json.get("conclusions")
                if recs:
                    if isinstance(recs, list):
                        accumulated_recommendations.extend([str(item).strip() for item in recs if str(item).strip()])
                    elif isinstance(recs, str) and recs.strip():
                        accumulated_recommendations.append(recs.strip()[:180])
                elif r.evolution_summary:
                    accumulated_recommendations.append(r.evolution_summary.strip()[:180])

            sections.append(f"\n#### 5. Histórico de Ciclos de Formación ({total_cycles} ciclos registrados)")
            sections.append(f"- Resumen de ciclos: {total_cycles} ciclos analizados ({completed_cycles} completados, {active_cycles} en curso/pendientes).")
            if evolution_str:
                sections.append(evolution_str)
            sections.append(f"- Balance global de objetivos: {superados_count} superados acumulados | {no_superados_count} no superados acumulados.")

            if failed_objs_freq:
                sorted_failed = sorted(failed_objs_freq.items(), key=lambda x: x[1], reverse=True)
                sections.append("- Objetivos no superados y áreas recurrentes de dificultad en ciclos:")
                for o_name, count in sorted_failed[:4]:
                    repeat_note = f" (no superado en {count} ciclos)" if count > 1 else ""
                    sections.append(f"  * [NO SUPERADO]{repeat_note} {o_name}")

            if passed_objs_freq:
                sorted_passed = sorted(passed_objs_freq.items(), key=lambda x: x[1], reverse=True)
                sections.append("- Objetivos consolidados y superados con consistencia:")
                for o_name, count in sorted_passed[:3]:
                    repeat_note = f" (superado en {count} ciclos)" if count > 1 else ""
                    sections.append(f"  * [SUPERADO]{repeat_note} {o_name}")

            if accumulated_recommendations:
                rec_freq: dict[str, int] = {}
                for rec in accumulated_recommendations:
                    rec_freq[rec] = rec_freq.get(rec, 0) + 1
                sorted_recs = sorted(rec_freq.keys(), key=lambda k: (rec_freq[k], accumulated_recommendations.index(k)), reverse=True)
                sections.append("\n* Recomendaciones pedagógicas de ciclos anteriores:")
                for rec in sorted_recs[:4]:
                    sections.append(f"  - {rec}")

            # Detalle específico de los ciclos más recientes (máx 3) para contexto detallado sin sobrecargar tokens
            recent_reports = sorted(
                reports,
                key=lambda r: (r.period_end or r.created_at or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True
            )[:3]
            sections.append("\n* Detalle de los ciclos formativos más recientes:")
            for idx, r in enumerate(recent_reports, 1):
                p_start = r.period_start.strftime("%d/%m/%Y") if r.period_start else "Inicio N/A"
                p_end = r.period_end.strftime("%d/%m/%Y") if r.period_end else "Fin N/A"
                r_score = f"{r.avg_evaluacion_global:.1f}/10" if r.avg_evaluacion_global is not None else "N/A"
                sections.append(f"  - Ciclo #{idx} (Periodo {p_start} a {p_end}, Estado: {r.status}, Nota media: {r_score}):")
                final_json = r.final_report_json if isinstance(r.final_report_json, dict) else {}
                objs_status = final_json.get("objectives_status") or []
                for obj in objs_status[:3]:
                    if isinstance(obj, dict):
                        o_title = obj.get("title", "Objetivo")
                        o_st = obj.get("status", "EVALUADO")
                        o_sc = obj.get("score")
                        o_bs = obj.get("base_score")
                        o_just = obj.get("justification", "")
                        sc_info = f" (nota {o_sc:.1f} vs base {o_bs:.1f})" if (o_sc is not None and o_bs is not None) else ""
                        j_compact = f" — {o_just[:100]}..." if len(o_just) > 100 else (f" — {o_just}" if o_just else "")
                        sections.append(f"    * [{o_st}] {o_title}{sc_info}{j_compact}")

        # 6. Histórico de Simulaciones de Roleplay (Histórico Completo)
        if sessions:
            all_sessions = sessions
            completed_sessions = [s for s in all_sessions if s.status == "completed"]
            evaluated_sessions = [
                s for s in completed_sessions
                if s.evaluation and s.evaluation.score is not None
            ]
            total_simulations = len(all_sessions)
            completed_simulations = len(completed_sessions)
            evaluated_simulations = len(evaluated_sessions)
            incomplete_simulations = total_simulations - completed_simulations

            scores: list[float] = []
            improvement_freq: dict[str, int] = {}
            strengths_freq: dict[str, int] = {}
            recent_feedback: list[str] = []

            # RENDIMIENTO: Las sesiones incompletas NO deben participar en métricas de rendimiento
            # (medias de puntuación, evolución, fortalezas, debilidades, patrones cualitativos ni feedback).
            # Solo iteramos sobre completed_sessions.
            completed_chrono = sorted(
                completed_sessions,
                key=lambda s: s.created_at or datetime.min.replace(tzinfo=timezone.utc)
            )

            for s in completed_chrono:
                ev = s.evaluation
                if not ev:
                    continue
                if ev.score is not None:
                    try:
                        scores.append(float(ev.score))
                    except (ValueError, TypeError):
                        pass

                imp = ev.improvement_points
                if isinstance(imp, list):
                    for p in imp:
                        if isinstance(p, str) and p.strip():
                            clean_p = p.strip()
                            improvement_freq[clean_p] = improvement_freq.get(clean_p, 0) + 1
                elif isinstance(imp, dict):
                    for k, v in imp.items():
                        if isinstance(v, str) and v.strip():
                            clean_p = f"{k}: {v.strip()}"
                            improvement_freq[clean_p] = improvement_freq.get(clean_p, 0) + 1

                st = ev.strengths
                if isinstance(st, list):
                    for p in st:
                        if isinstance(p, str) and p.strip():
                            clean_s = p.strip()
                            strengths_freq[clean_s] = strengths_freq.get(clean_s, 0) + 1
                elif isinstance(st, dict):
                    for k, v in st.items():
                        if isinstance(v, str) and v.strip():
                            clean_s = f"{k}: {v.strip()}"
                            strengths_freq[clean_s] = strengths_freq.get(clean_s, 0) + 1

                if ev.summary and ev.summary.strip():
                    recent_feedback.append(ev.summary.strip())

            sections.append(f"\n#### 6. Histórico de Simulaciones de Roleplay ({completed_simulations} completadas)")
            sections.append(
                "- Desglose de simulaciones:\n"
                f"  * Simulaciones registradas/iniciadas: {total_simulations}\n"
                f"  * Simulaciones completadas: {completed_simulations}\n"
                f"  * Simulaciones evaluadas con nota: {evaluated_simulations}\n"
                f"  * Simulaciones interrumpidas/no finalizadas: {incomplete_simulations}"
            )
            if scores:
                avg_sim = sum(scores) / len(scores)
                sim_eval_str = f"- Puntuación media en simulaciones: {avg_sim:.1f}/10 (basada en {len(scores)} simulaciones evaluadas)."
                if len(scores) >= 4:
                    mid = len(scores) // 2
                    first_avg = sum(scores[:mid]) / mid
                    second_avg = sum(scores[mid:]) / (len(scores) - mid)
                    sim_delta = round(second_avg - first_avg, 2)
                    sim_sign = "+" if sim_delta > 0 else ""
                    sim_dir = "mejora" if sim_delta > 0.2 else ("caída" if sim_delta < -0.2 else "estable")
                    sim_eval_str += f" Evolución: {first_avg:.1f} (inicial) -> {second_avg:.1f} (reciente) ({sim_sign}{sim_delta:.1f}, {sim_dir})."
                sections.append(sim_eval_str)

            if strengths_freq:
                sorted_str = sorted(strengths_freq.items(), key=lambda x: x[1], reverse=True)
                sections.append("- Fortalezas recurrentes demostradas en simulaciones:")
                for st_name, count in sorted_str[:3]:
                    repeat_note = f" (demostrado en {count} simulaciones)" if count > 1 else ""
                    sections.append(f"  * {st_name}{repeat_note}")

            if improvement_freq:
                sorted_imp = sorted(improvement_freq.items(), key=lambda x: x[1], reverse=True)
                sections.append("- Puntos de mejora señalados en simulaciones:")
                for imp_name, count in sorted_imp[:4]:
                    repeat_note = f" (señalado en {count} simulaciones)" if count > 1 else ""
                    sections.append(f"  * {imp_name}{repeat_note}")

            if recent_feedback:
                sections.append("- Feedback recibido en simulaciones recientes:")
                for fb in recent_feedback[-2:]:
                    sections.append(f"  * {fb[:150]}")

        sections.append("--------------------------------------------------------")
        return "\n".join(sections)

    @classmethod
    def detect_detailed_period_request(
        cls,
        query_text: str,
        is_admin: bool = False,
        reference_date: Optional[datetime] = None,
    ) -> DetailedPeriodResult:
        """
        Deterministically detects whether the user query is asking for a detailed analysis
        of evaluations / calls / simulations within an explicit temporal period.
        Enforces a hard limit of 15 natural days.
        Does NOT block general questions without explicit temporal periods
        (e.g., '¿Cómo he evolucionado?', '¿En qué suelo fallar?').
        """
        if not query_text or not str(query_text).strip():
            return DetailedPeriodResult(is_detailed_period_request=False)

        ref_dt = reference_date or datetime.now(timezone.utc)
        q = query_text.strip().lower()

        # Helper to parse integers or Spanish number words
        def _parse_num(val_str: Optional[str]) -> Optional[int]:
            if not val_str:
                return None
            clean_str = val_str.strip().lower()
            if clean_str.isdigit():
                return int(clean_str)
            return SPANISH_WORDS_NUM.get(clean_str)

        # 1. Explicit ranges: "del X al Y de <mes>" or "entre el X y el Y de <mes>"
        # Pattern A: same month: "del 1 al 15 de junio [de 2026]"
        m_range = re.search(
            r"\b(?:del?|desde\s+el)\s+(\d{1,2})\s+(?:al?|hasta\s+el)\s+(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+(?:de\s+|del\s+)?(\d{4}))?\b",
            q,
        )
        if not m_range:
            m_range = re.search(
                r"\bentre\s+el\s+(\d{1,2})\s+y\s+el\s+(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+(?:de\s+|del\s+)?(\d{4}))?\b",
                q,
            )

        if m_range:
            d1 = int(m_range.group(1))
            d2 = int(m_range.group(2))
            month_name = m_range.group(3).lower()
            year_str = m_range.group(4)
            month_num = SPANISH_MONTHS.get(month_name)
            if month_num:
                year = int(year_str) if year_str else ref_dt.year
                _, last_day = calendar.monthrange(year, month_num)
                d1 = max(1, min(d1, last_day))
                d2 = max(1, min(d2, last_day))
                if d1 > d2:
                    d1, d2 = d2, d1
                start_dt = datetime(year, month_num, d1, 0, 0, 0, tzinfo=timezone.utc)
                end_dt = datetime(year, month_num, d2, 23, 59, 59, tzinfo=timezone.utc)
                span_days = (end_dt.date() - start_dt.date()).days + 1
                exceeds = span_days > 15
                return DetailedPeriodResult(
                    is_detailed_period_request=True,
                    exceeds_limit=exceeds,
                    start_date=start_dt,
                    end_date=end_dt,
                    period_days=span_days,
                    raw_expression=m_range.group(0),
                    blocking_message=(ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG) if exceeds else None,
                )

        # Pattern B: cross months: "del 25 de mayo al 5 de junio"
        m_cross = re.search(
            r"\b(?:del?|desde\s+el)\s+(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:al?|hasta\s+el)\s+(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+(?:de\s+|del\s+)?(\d{4}))?\b",
            q,
        )
        if not m_cross:
            m_cross = re.search(
                r"\bentre\s+el\s+(\d{1,2})\s+de\s+([a-záéíóú]+)\s+y\s+el\s+(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+(?:de\s+|del\s+)?(\d{4}))?\b",
                q,
            )
        if m_cross:
            d1 = int(m_cross.group(1))
            m1_name = m_cross.group(2).lower()
            d2 = int(m_cross.group(3))
            m2_name = m_cross.group(4).lower()
            year_str = m_cross.group(5)
            m1_num = SPANISH_MONTHS.get(m1_name)
            m2_num = SPANISH_MONTHS.get(m2_name)
            if m1_num and m2_num:
                year = int(year_str) if year_str else ref_dt.year
                start_dt = datetime(year, m1_num, d1, 0, 0, 0, tzinfo=timezone.utc)
                end_dt = datetime(year, m2_num, d2, 23, 59, 59, tzinfo=timezone.utc)
                if start_dt > end_dt:
                    start_dt, end_dt = end_dt, start_dt
                span_days = (end_dt.date() - start_dt.date()).days + 1
                exceeds = span_days > 15
                return DetailedPeriodResult(
                    is_detailed_period_request=True,
                    exceeds_limit=exceeds,
                    start_date=start_dt,
                    end_date=end_dt,
                    period_days=span_days,
                    raw_expression=m_cross.group(0),
                    blocking_message=(ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG) if exceeds else None,
                )

        # 2. Quincenas: "primera / segunda quincena de <mes>"
        m_quincena = re.search(
            r"\b(?:la\s+)?(primera|segunda|1[aª]|2[aª])\s+quincena\s+(?:de\s+)?([a-záéíóú]+)(?:\s+(?:de\s+|del\s+)?(\d{4}))?\b",
            q,
        )
        if m_quincena:
            q_type = m_quincena.group(1).lower()
            month_name = m_quincena.group(2).lower()
            year_str = m_quincena.group(3)
            month_num = SPANISH_MONTHS.get(month_name)
            if month_num:
                year = int(year_str) if year_str else ref_dt.year
                _, last_day = calendar.monthrange(year, month_num)
                if q_type in ("primera", "1a", "1ª"):
                    start_dt = datetime(year, month_num, 1, 0, 0, 0, tzinfo=timezone.utc)
                    end_dt = datetime(year, month_num, 15, 23, 59, 59, tzinfo=timezone.utc)
                    span_days = 15
                else:
                    start_dt = datetime(year, month_num, 16, 0, 0, 0, tzinfo=timezone.utc)
                    end_dt = datetime(year, month_num, last_day, 23, 59, 59, tzinfo=timezone.utc)
                    span_days = (end_dt.date() - start_dt.date()).days + 1

                is_fortnight = True
                exceeds = span_days > 15 and not is_fortnight
                return DetailedPeriodResult(
                    is_detailed_period_request=True,
                    exceeds_limit=exceeds,
                    start_date=start_dt,
                    end_date=end_dt,
                    period_days=span_days,
                    raw_expression=m_quincena.group(0),
                    blocking_message=(ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG) if exceeds else None,
                    is_fortnight=is_fortnight,
                )

        # 3. Relative expressions: "últimos/as N días"
        m_days = re.search(
            r"\b(?:[uú]ltimos?|anteriores?|pasados?)\s+(\d+|[a-záéíóú]+)\s+d[ií]as\b",
            q,
        )
        if m_days:
            n_days = _parse_num(m_days.group(1))
            if n_days is not None:
                end_dt = ref_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
                start_dt = (ref_dt - timedelta(days=n_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
                exceeds = n_days > 15
                return DetailedPeriodResult(
                    is_detailed_period_request=True,
                    exceeds_limit=exceeds,
                    start_date=start_dt,
                    end_date=end_dt,
                    period_days=n_days,
                    raw_expression=m_days.group(0),
                    blocking_message=(ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG) if exceeds else None,
                )

        # 4. Relative expressions: "últimas/os N semanas" / "última semana" / "esta semana" / "la semana pasada"
        m_weeks = re.search(
            r"\b(?:[uú]ltimas?|anteriores?|pasadas?)\s+(\d+|[a-záéíóú]+)?\s*semanas?\b",
            q,
        )
        if not m_weeks and re.search(r"\b(?:esta\s+semana|(?:la\s+)?semana\s+pasada)\b", q):
            m_weeks = re.search(r"\b(?:esta\s+semana|(?:la\s+)?semana\s+pasada)\b", q)
            n_weeks = 1
        else:
            num_word = m_weeks.group(1) if m_weeks else None
            n_weeks = _parse_num(num_word) if num_word else 1
            if n_weeks is None:
                n_weeks = 1

        if m_weeks:
            n_days = n_weeks * 7
            end_dt = ref_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            start_dt = (ref_dt - timedelta(days=n_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            exceeds = n_days > 15
            return DetailedPeriodResult(
                is_detailed_period_request=True,
                exceeds_limit=exceeds,
                start_date=start_dt,
                end_date=end_dt,
                period_days=n_days,
                raw_expression=m_weeks.group(0),
                blocking_message=(ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG) if exceeds else None,
            )

        # 5. Relative expressions: "último/s N meses" / "último mes" / "este mes" / "el mes pasado"
        m_months = re.search(
            r"\b(?:[uú]ltimos?|anteriores?|pasados?)\s+(\d+|[a-záéíóú]+)?\s*mes(?:es)?\b",
            q,
        )
        if not m_months and re.search(r"\b(?:este\s+mes|(?:el\s+)?mes\s+pasado)\b", q):
            m_months = re.search(r"\b(?:este\s+mes|(?:el\s+)?mes\s+pasado)\b", q)
            n_months = 1
        else:
            num_word = m_months.group(1) if m_months else None
            n_months = _parse_num(num_word) if num_word else 1
            if n_months is None:
                n_months = 1

        if m_months:
            n_days = n_months * 30
            exceeds = n_days > 15
            return DetailedPeriodResult(
                is_detailed_period_request=True,
                exceeds_limit=exceeds,
                period_days=n_days,
                raw_expression=m_months.group(0),
                blocking_message=(ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG) if exceeds else None,
            )

        # 6. Multiple months: e.g. "junio y julio", "mayo a julio"
        month_names_pattern = "|".join(SPANISH_MONTHS.keys())
        m_multi_month = re.search(
            rf"\b({month_names_pattern})\s+(?:y|e|a|hasta)\s+({month_names_pattern})\b",
            q,
        )
        if m_multi_month:
            return DetailedPeriodResult(
                is_detailed_period_request=True,
                exceeds_limit=True,
                period_days=60,
                raw_expression=m_multi_month.group(0),
                blocking_message=ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG,
            )

        # 7. Full months: "todo junio", "en junio", "durante junio", "analízame junio", "analiza a este agente durante junio"
        m_month_full = re.search(
            rf"\b(?:todo\s+el\s+mes\s+de\s+|todo\s+|durante\s+|en\s+|el\s+mes\s+de\s+)?({month_names_pattern})\b",
            q,
        )
        if m_month_full:
            matched_month = m_month_full.group(1)
            has_temporal_intent = (
                f"todo {matched_month}" in q
                or f"todo el mes de {matched_month}" in q
                or f"durante {matched_month}" in q
                or f"en {matched_month}" in q
                or f"el mes de {matched_month}" in q
                or any(w in q for w in ["analiza", "analízame", "analizame", "revisa", "revísame", "revisame", "llamadas de", "evaluaciones de"])
            )
            if has_temporal_intent:
                m_num = SPANISH_MONTHS[matched_month]
                year = ref_dt.year
                _, last_day = calendar.monthrange(year, m_num)
                return DetailedPeriodResult(
                    is_detailed_period_request=True,
                    exceeds_limit=True,
                    period_days=last_day,
                    raw_expression=matched_month,
                    blocking_message=ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG,
                )

        # 8. Broad history analysis request: "todo mi histórico", "todas mis evaluaciones"
        all_history_match = re.search(
            r"\b(?:todo\s+(?:el\s+|mi\s+)?hist[oó]rico|todas\s+(?:las|mis)?\s*(?:evaluaciones|llamadas|simulaciones))\b",
            q,
        )
        if all_history_match and any(w in q for w in ["análisis", "analisis", "analiza", "analízame", "analizame", "revisa", "revísame", "revisame", "detalle", "detallado", "detallada"]):
            return DetailedPeriodResult(
                is_detailed_period_request=True,
                exceeds_limit=True,
                period_days=999,
                raw_expression=all_history_match.group(0),
                blocking_message=ADMIN_BLOCKING_MSG if is_admin else AGENT_BLOCKING_MSG,
            )

        # 9. Ambiguous temporal expression without resolvable dates:
        if any(p in q for p in ["ese periodo", "aquel periodo", "esas fechas", "aquellas fechas", "dicho periodo"]):
            return DetailedPeriodResult(
                is_detailed_period_request=True,
                exceeds_limit=True,
                clarification_needed=True,
                raw_expression=q,
                blocking_message=ADMIN_CLARIFICATION_MSG if is_admin else AGENT_CLARIFICATION_MSG,
            )

        # 10. General questions (no explicit temporal period):
        # "¿cómo he evolucionado?", "¿en qué suelo fallar?", "¿cuáles son mis puntos fuertes?"
        return DetailedPeriodResult(is_detailed_period_request=False)

    @classmethod
    async def build_detailed_period_summary(
        cls,
        db: AsyncSession,
        context: TenantContext,
        target_agent_id: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        raw_expression: Optional[str] = None,
    ) -> str:
        """
        Builds a compact, strictly aggregated synthesis of the agent's performance
        for a specific requested temporal period (bounded to max 15 days).
        Ensures prompt size does NOT grow linearly with the number of evaluations.
        """
        start_str = start_date.strftime("%d/%m/%Y") if start_date else "Inicio"
        end_str = end_date.strftime("%d/%m/%Y") if end_date else "Fin"
        date_from_iso = start_date.strftime("%Y-%m-%d") if start_date else None
        date_to_iso = end_date.strftime("%Y-%m-%d") if end_date else None

        period_evolution: Optional[dict[str, Any]] = None
        try:
            period_evolution = await get_agent_evolution(
                db=db,
                hubspot_owner_id=target_agent_id,
                date_from=date_from_iso,
                date_to=date_to_iso,
                company_id=context.company_id,
                context=context,
            )
        except Exception as e:
            logger.warning("Error fetching period evolution for %s [%s to %s]: %s", target_agent_id, date_from_iso, date_to_iso, e)

        summary = period_evolution.get("summary", {}) if period_evolution else {}
        total_calls = int(summary.get("total_analyses", 0))
        avg_score = summary.get("avg_evaluacion_global")
        trend = period_evolution.get("trend", {}) if period_evolution else {}
        strengths = period_evolution.get("strengths", []) if period_evolution else []
        weaknesses = period_evolution.get("weaknesses", []) if period_evolution else []

        simulations_in_period: list[TrainerSession] = []
        try:
            stmt_sim = select(TrainerSession).options(selectinload(TrainerSession.evaluation)).where(
                TrainerSession.agent_id == target_agent_id,
                TrainerSession.status == "completed",
            )
            if context.company_id is not None:
                stmt_sim = stmt_sim.where(TrainerSession.company_id == context.company_id)
            elif not context.is_super_admin and context.allowed_company_ids:
                stmt_sim = stmt_sim.where(TrainerSession.company_id.in_(context.allowed_company_ids))
            if start_date:
                stmt_sim = stmt_sim.where(TrainerSession.created_at >= start_date)
            if end_date:
                stmt_sim = stmt_sim.where(TrainerSession.created_at <= end_date)
            stmt_sim = stmt_sim.order_by(TrainerSession.created_at.desc())
            res_sim = await db.execute(stmt_sim)
            simulations_in_period = list(res_sim.scalars().all())
        except Exception as e:
            logger.warning("Error fetching period simulations for %s: %s", target_agent_id, e)

        sample_calls: list[MassEvaluationResult] = []
        if total_calls > 0:
            try:
                stmt_sample = select(MassEvaluationResult).where(
                    MassEvaluationResult.hubspot_owner_id == target_agent_id,
                    MassEvaluationResult.status == "completed",
                )
                if context.company_id is not None:
                    stmt_sample = stmt_sample.where(MassEvaluationResult.company_id == context.company_id)
                elif not context.is_super_admin and context.allowed_company_ids:
                    stmt_sample = stmt_sample.where(MassEvaluationResult.company_id.in_(context.allowed_company_ids))
                if start_date:
                    stmt_sample = stmt_sample.where(MassEvaluationResult.call_timestamp >= start_date)
                if end_date:
                    stmt_sample = stmt_sample.where(MassEvaluationResult.call_timestamp <= end_date)
                stmt_sample = stmt_sample.order_by(MassEvaluationResult.call_timestamp.desc()).limit(3)
                res_samp = await db.execute(stmt_sample)
                sample_calls = list(res_samp.scalars().all())
            except Exception as e:
                logger.warning("Error fetching sample calls for %s: %s", target_agent_id, e)

        lines = [
            f"--- ANÁLISIS DETALLADO DEL PERIODO SOLICITADO ({start_str} al {end_str}) ---",
            f"- Tramo temporal analizado: del {start_str} al {end_str}.",
        ]

        if total_calls == 0 and not simulations_in_period:
            lines.append(
                "- No constan evaluaciones de llamadas reales ni simulaciones registradas para este agente dentro de este intervalo."
            )
        else:
            if total_calls > 0:
                lines.append(f"- Evaluaciones de llamadas en el periodo: {total_calls} llamadas evaluadas.")
                score_str = f"{avg_score:.2f}/10" if avg_score is not None else "Sin nota"
                lines.append(f"- Puntuación media en el periodo: {score_str}.")
                direction = trend.get("evaluacion_global_direction", "stable")
                delta_val = trend.get("evaluacion_global_delta_first_last", 0.0) or 0.0
                sign = "+" if delta_val > 0 else ""
                lines.append(f"- Tendencia dentro del tramo: {direction} ({sign}{delta_val:.1f} puntos).")

                if strengths:
                    lines.append("- Criterios con mejor desempeño en este tramo:")
                    for st in strengths[:3]:
                        c_name = st.get("criterion_name") or st.get("criterion_key", "")
                        c_sc = st.get("score")
                        sc_fmt = f"{c_sc:.1f}/10" if c_sc is not None else "N/A"
                        lines.append(f"  * [Mejor resultado] {c_name}: nota media {sc_fmt}")

                if weaknesses:
                    lines.append("- Criterios con mayor margen de mejora en este tramo:")
                    for wk in weaknesses[:3]:
                        c_name = wk.get("criterion_name") or wk.get("criterion_key", "")
                        c_sc = wk.get("score")
                        sc_fmt = f"{c_sc:.1f}/10" if c_sc is not None else "N/A"
                        lines.append(f"  * [Área a reforzar] {c_name}: nota media {sc_fmt}")

                if sample_calls:
                    lines.append("- Ejemplos destacados de llamadas del periodo (máximo 3):")
                    for idx, c in enumerate(sample_calls, 1):
                        c_date = c.call_timestamp.strftime("%d/%m/%Y") if c.call_timestamp else "Fecha N/A"
                        c_sc = f"{c.evaluacion_global:.1f}/10" if c.evaluacion_global is not None else "N/A"
                        c_sum = ""
                        if isinstance(c.result_json, dict) and c.result_json.get("resumen"):
                            c_sum = str(c.result_json.get("resumen")).strip()
                        c_brief = f" — {c_sum[:100]}..." if len(c_sum) > 100 else (f" — {c_sum}" if c_sum else "")
                        lines.append(f"  * Llamada #{idx} ({c_date}, nota {c_sc}){c_brief}")

            if simulations_in_period:
                sim_scores = [float(s.evaluation.score) for s in simulations_in_period if s.evaluation and s.evaluation.score is not None]
                lines.append(f"- Simulaciones completadas en el periodo: {len(simulations_in_period)} simulaciones.")
                if sim_scores:
                    lines.append(f"- Media en simulaciones del periodo: {sum(sim_scores)/len(sim_scores):.1f}/10.")

        lines.append("----------------------------------------------------------------------------")
        return "\n".join(lines)

    @staticmethod
    def build_grounding_prompt(
        documents: List[TrainingKnowledgeDocument],
        target_agent_id: str,
        historical_profile: Optional[str] = None,
        detailed_period_summary: Optional[str] = None,
        is_admin: bool = False,
    ) -> str:
        """
        Builds the strict Grounding System Prompt incorporating available training knowledge documents,
        the full historical profile of the agent across calls, cycles, and simulations, and behavioral boundaries.
        Enforces factual fidelity, natural pedagogical tone (agent coaching vs admin supervision),
        strict functional scope, and zero technical ID/table leakage.
        """
        lines = []

        if is_admin:
            lines.extend([
                "Eres el Asistente y Tutor de Formación de Speech BM, enfocado en la supervisión pedagógica y análisis del desarrollo de los agentes.",
                "Tu interlocutor es un supervisor o administrador del centro de formación.",
                "Tu rol es proporcionarle un análisis formativo objetivo, constructivo y de alto valor sobre la trayectoria y desempeño del agente.",
                "DIRÍGETE AL USUARIO EN TERCERA PERSONA ('este agente', 'se observa en sus evaluaciones', 'sus fortalezas consolidadas', 'el siguiente foco formativo'). Evita juicios absolutos o sentenciosos.",
            ])
        else:
            lines.extend([
                "Eres el Tutor y Mentor de Formación de Speech BM.",
                "Tu interlocutor es el propio agente de contact center. Tu misión es acompañarle, guiarle y ayudarle en su desarrollo profesional.",
                "DIRÍGETE AL AGENTE DE FORMA CERCANA, NATURAL, PEDAGÓGICA Y PROFESIONAL EN SEGUNDA PERSONA ('tú', 'en tus llamadas', 'has mejorado en...', 'te convendría practicar...').",
                "Actúa como un formador o coach experto, constructivo, motivador y orientado a la mejora continua, NUNCA como un evaluador punitivo ni como un sistema robótico.",
            ])

        lines.extend([
            f"[Referencia interna de contexto para grounding: {target_agent_id}]",
            "NOTA DE CONFIDENCIALIDAD: La referencia anterior es exclusivamente para tu contexto interno. NUNCA menciones identificadores técnicos al usuario.",
            "",
            "REGLAS OBLIGATORIAS DE TONO Y COMPORTAMIENTO PEDAGÓGICO:",
            "1. Tono humano y natural: Habla como un formador o tutor humano experto, no como un software o sistema informático.",
            "   - Sé breve y directo cuando la consulta sea sencilla o puntual.",
            "   - Sé explicativo, estructurado y formativo cuando se requiera comprender un patrón de error o una técnica de mejora.",
            "2. Prohibición estricta de lenguaje robótico, burocrático o defensivo:",
            "   - NUNCA uses fórmulas mecánicas como:",
            "     * 'Según los datos suministrados por el sistema...'",
            "     * 'El sistema ha determinado...'",
            "     * 'El identificador del agente...'",
            "     * 'No tengo permisos para...'",
            "     * 'La tabla X indica...'",
            "     * 'La consulta SQL...'",
            "     * 'El backend...'",
            "   - Usa siempre lenguaje natural de formación y coaching:",
            "     * 'En tus últimas evaluaciones...' (o 'En las evaluaciones del agente...')",
            "     * 'Hay un patrón que se repite...'",
            "     * 'Aquí has mejorado...' (o 'Se observa un avance en...')",
            "     * 'Te convendría trabajar...' (o 'Un posible foco de mejora sería...')",
            "     * 'En las simulaciones se observa...'",
            "",
            "3. LÍMITES ESTRICTOS DE INFORMACIÓN TÉCNICA (CERO FUGAS):",
            "   - NUNCA reveles ni menciones al usuario identificadores técnicos de base de datos (company_id, user_id, agent_id, hubspot_owner_id, service_id, cycle_id, simulation_id, ni IDs numéricos como ID 573, ID #12, etc.).",
            "   - NUNCA menciones nombres de tablas (bm_users, trainer_sessions, etc.), nombres de modelos de datos, nombres de funciones, endpoints (/bm/...), roles técnicos internos (SUPER_ADMIN, COMPANY_ADMIN, etc.), trazas ni detalles de arquitectura.",
            "   - NUNCA uses nombres de campos técnicos de base de datos (evaluacion_global, result_json, etc.); refiérete a ellos de forma natural ('evaluación global', 'resultados', 'criterios').",
            "",
            "4. PREGUNTAS SOBRE DATOS O FUNCIONAMIENTO INTERNO:",
            "   - Si el usuario pregunta de dónde salen los datos, qué tabla se usa, qué endpoint se consulta, qué ID tiene, qué permisos tiene o cómo se sabe su empresa:",
            "     * NO expliques implementación ni des identificadores técnicos.",
            "     * Responde de forma funcional y natural:",
            "       - Para agente: 'Uso la información de formación y evaluaciones disponible para tu perfil.'",
            "       - Para admin: 'Uso la información de formación y evaluaciones registrada para el perfil del agente.'",
            "",
            "5. ÁMBITO FUNCIONAL EXCLUSIVO Y REDIRECCIÓN:",
            "   - Tu labor se limita EXCLUSIVAMENTE a la formación del agente: evolución, fortalezas, debilidades, feedback, criterios, llamadas evaluadas, simulaciones, ciclos formativos, objetivos y técnicas de atención/venta/servicio.",
            "   - Si el usuario pregunta sobre temas ajenos a la formación (deportes, Champions League, fútbol, programación o código Python, cultura general, capitales, etc.):",
            "     * NO respondas conocimiento general ni código.",
            "     * Redirige amablemente a tu función formativa:",
            "       - Para agente: 'Puedo ayudarte con tu formación, evaluaciones y evolución como agente. Si quieres, podemos revisar tus puntos fuertes, áreas de mejora o simulaciones.'",
            "       - Para admin: 'Puedo ayudarte con la supervisión formativa, evaluaciones y evolución pedagógica de los agentes. Si lo deseas, podemos analizar sus fortalezas, áreas de mejora o histórico de simulaciones.'",
            "",
            "6. MANEJO DE INCERTIDUMBRE Y RIGOR FACTUAL:",
            "   - Si no constan datos suficientes sobre un criterio, fecha o aspecto consultado:",
            "     * NUNCA inventes notas, hechos, llamadas ni simulaciones inexistentes.",
            "     * Responde con honestidad y prudencia:",
            "       'No tengo suficiente histórico para valorar todavía ese punto.' o",
            "       'En las evaluaciones disponibles no aparece evidencia suficiente para sacar una conclusión.'",
            "   - Distingue con rigor entre un hecho constatado (lo que ocurrió en una llamada), un patrón recurrente y una recomendación pedagógica. Nunca presentes una deducción como un hecho objetivo.",
            "",
            "7. ESTRUCTURA DE RESPUESTAS ÚTILES:",
            "   - Cuando analices un error o punto de mejora, estructura de forma pedagógica y práctica:",
            "     * Qué se observa (patrón o evidencia concreta de la interacción).",
            "     * Ejemplo o situación donde ocurrió.",
            "     * Qué hacer para mejorarlo (técnica práctica aplicable en el puesto).",
            "   - No uses una plantilla rígida obligatoria; adapta la respuesta a la pregunta.",
            "",
            "8. REFERENCIA A SIMULACIONES Y DOCUMENTOS:",
            "   - Refiérete a los documentos y simulaciones por su temática o título natural (ej. 'la simulación de objeciones de facturación', 'el ciclo de atención'), NUNCA usando IDs numéricos ni referencias a bases de datos.",
            "",
            "9. DISTINCIÓN Y CONTEO DE SIMULACIONES:",
            "   - Distingue con precisión entre simulaciones registradas (total iniciado), completadas, y evaluadas con nota:",
            "     * Si el usuario pregunta de forma general '¿cuántas simulaciones tengo?' o '¿cuántas simulaciones he hecho?', responde con el total de simulaciones registradas o iniciadas, aclarando cuántas se completaron o si alguna quedó sin finalizar si aporta claridad (ej. 'Tienes 4 simulaciones registradas. De ellas, 3 se completaron y 1 quedó sin finalizar').",
            "     * Si pregunta específicamente '¿cuántas he completado?' o '¿cuántas simulaciones completadas tengo?', responde con el número de completadas.",
            "     * Si pregunta '¿cuántas tienen evaluación?' o '¿cuántas tienen nota?', responde con el número de evaluadas con nota.",
            "   - NUNCA inventes notas o puntuaciones de simulaciones no evaluadas, y NUNCA uses simulaciones incompletas o interrumpidas para calcular medias, fortalezas o debilidades.",
            "",
        ])

        if detailed_period_summary and detailed_period_summary.strip():
            lines.append(detailed_period_summary.strip())
            lines.append("")
            lines.extend([
                "INSTRUCCIÓN PARA CONSULTAS POR PERIODO TEMPORAL ESPECÍFICO:",
                "- El usuario ha solicitado un análisis enfocado en un periodo temporal concreto (acotado a un máximo de 15 días).",
                "- Utiliza como referencia principal los datos de la sección 'ANÁLISIS DETALLADO DEL PERIODO SOLICITADO' para responder sobre lo ocurrido en esas fechas.",
                "- Si en ese periodo no constan evaluaciones o no hay datos suficientes, indícaselo con naturalidad y honestidad pedagógica.",
                "- Puedes contrastar con el 'PERFIL HISTÓRICO' para dar contexto o perspectiva global si aporta valor formativo, pero prioriza el tramo temporal solicitado.",
                "",
            ])

        if historical_profile and historical_profile.strip():
            lines.append(historical_profile.strip())
            lines.append("")

        if not documents:
            lines.append("--- BASE DE CONOCIMIENTO ---")
            lines.append(
                "ADVERTENCIA FORMATIVA: No existen documentos de conocimiento ni evaluaciones registradas para este agente "
                "en el contexto o ciclo especificado. Informa al usuario con amabilidad de que no hay datos disponibles sin inventar información."
            )
            lines.append("----------------------------")
        else:
            lines.append(f"--- BASE DE CONOCIMIENTO DISPONIBLE ({len(documents)} DOCUMENTOS) ---")
            for doc in documents:
                lines.append(f"### [DOCUMENTO: {doc.title}]")
                lines.append(f"- Tipo de documento: {doc.document_type}")
                lines.append("Contenido formativo:")
                lines.append(doc.content)
                lines.append("---")
            lines.append("FIN DE LA BASE DE CONOCIMIENTO.")

        return "\n".join(lines)

    @classmethod
    def sanitize_response_text(
        cls,
        text: str,
        is_admin: bool = False,
        query_text: Optional[str] = None,
    ) -> str:
        """
        Ensures zero leakage of technical identifiers (company_id, agent_id, user_id, internal IDs),
        table names, endpoints, internal roles, and enforces natural redirection for out-of-scope queries
        and functional responses for system inquiries.
        """
        if not text:
            return ""

        q_lower = (query_text or "").lower().strip()

        # 1. Out-of-scope redirection check
        out_of_scope_triggers = [
            "champions", "champions league", "quién ganó", "quien gano", "partido de fútbol", "partido de futbol",
            "código python", "codigo python", "escríbeme código", "escribeme codigo", "código en python", "codigo en python",
            "script python", "script en python", "crea un script", "programa en python",
            "capital de japón", "capital de japon", "capital de francia", "capital de italia", "capital de españa",
            "cómo funciona la base de datos", "como funciona la base de datos", "dime cómo funciona la base de datos",
            "muéstrame todos los usuarios", "muestrame todos los usuarios",
            "datos internos del sistema",
        ]
        if any(trigger in q_lower for trigger in out_of_scope_triggers):
            if "puedo ayudarte con tu formación" not in text.lower() and "puedo ayudarte con la supervisión" not in text.lower():
                if is_admin:
                    return "Puedo ayudarte con la supervisión formativa, evaluaciones y evolución pedagógica de los agentes. Si lo deseas, podemos analizar sus fortalezas, áreas de mejora o histórico de simulaciones."
                return "Puedo ayudarte con tu formación, evaluaciones y evolución como agente. Si quieres, podemos revisar tus puntos fuertes, áreas de mejora o simulaciones."

        # 2. Internal system query check (IDs, tables, endpoints, permissions)
        internal_query_triggers = [
            "de dónde sacas estos datos", "de donde sacas estos datos",
            "qué tabla estás usando", "que tabla estas usando", "qué tabla usas", "que tabla usas", "qué tabla", "que tabla",
            "qué endpoint", "que endpoint",
            "qué id tengo", "que id tengo", "dime mi id", "cuál es mi id", "cual es mi id", "dime el id", "cuál es el id",
            "hubspot_owner_id",
            "qué permisos tengo", "que permisos tengo",
            "cómo sabes mi empresa", "como sabes mi empresa",
            "cómo funciona internamente el sistema", "como funciona internamente el sistema",
            "enséñame cómo funciona internamente", "enseñame como funciona internamente",
        ]
        if any(trigger in q_lower for trigger in internal_query_triggers):
            if is_admin:
                return "Uso la información de formación y evaluaciones registrada para el perfil del agente."
            return "Uso la información de formación y evaluaciones disponible para tu perfil."

        # 3. Clean up any leaked technical ID patterns
        # e.g., "(ID 573)" -> "", "ID #573" -> "", "ID: 573" -> ""
        sanitized = re.sub(r"\s*\(?\b(?:ID|id)\s*[:#]?\s*\d+\)?", "", text)

        # e.g., company_id=1, agent_id='agent_01', hubspot_owner_id: demo_owner_01
        sanitized = re.sub(
            r"\b(?:el\s+)?(?:agent_id|hubspot_owner_id)\s*(?:[:=]|es)?\s*['\"]?[a-zA-Z0-9_-]+['\"]?",
            "el agente" if is_admin else "tu perfil",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"\b(?:company_id|user_id|service_id|cycle_id|simulation_id)\s*(?:[:=]|es)?\s*['\"]?[a-zA-Z0-9_-]+['\"]?",
            "",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"\b(?:company_id|user_id|agent_id|hubspot_owner_id|service_id|cycle_id|simulation_id)\b",
            "perfil",
            sanitized,
            flags=re.IGNORECASE,
        )

        # 4. Clean up any leaked database table names
        sanitized = re.sub(
            r"\b(?:bm_users|bm_services|bm_typologies|trainer_sessions|training_agent_reports|trainer_evaluations|mass_evaluation_results|training_knowledge_documents|prompt_base_structures|bm_companies)\b",
            "el registro de formación",
            sanitized,
            flags=re.IGNORECASE,
        )

        # 5. Clean up any leaked endpoints
        sanitized = re.sub(
            r"(?:POST|GET|PUT|DELETE|PATCH)?\s*\/bm\/[a-zA-Z0-9_\/-]+",
            "el entorno de formación",
            sanitized,
        )

        # 6. Clean up internal roles
        sanitized = re.sub(
            r"\b(?:SUPER_ADMIN|COMPANY_ADMIN|TEAM_COORDINATOR|InternalRole)\b",
            "administrador",
            sanitized,
        )

        # 7. Clean up robotic boilerplate phrases
        sanitized = re.sub(
            r"[Ss]egún los datos suministrados por el sistema,?\s*",
            "Según las evaluaciones registradas, " if is_admin else "Según tus evaluaciones registradas, ",
            sanitized,
        )
        sanitized = re.sub(
            r"[Ee]l sistema ha determinado que\s*",
            "Se observa que ",
            sanitized,
        )
        sanitized = re.sub(
            r"[Ee]l identificador del agente\s*",
            "El perfil del agente ",
            sanitized,
        )
        sanitized = re.sub(
            r"[Nn]o tengo permisos para\s*",
            "No dispongo de información sobre ",
            sanitized,
        )
        sanitized = re.sub(
            r"\b[Ee]l backend\b\s*",
            "el servicio de formación ",
            sanitized,
        )

        # Normalize any resulting double spaces or empty formatting
        sanitized = re.sub(r"  +", " ", sanitized)
        sanitized = re.sub(r"\( *\)", "", sanitized)

        return sanitized.strip()

    @classmethod
    async def process_chat(
        cls,
        db: AsyncSession,
        current_user: User,
        context: TenantContext,
        message: Optional[str] = None,
        audio_file: Optional[UploadFile] = None,
        agent_id: Optional[str] = None,
        cycle_id: Optional[int] = None,
        conversation_history: Any = None,
    ) -> dict[str, Any]:
        """
        Executes the full unified Chatbot pipeline:
        1. Input validation & audio transcription (converges to query_text).
        2. Strict tenant security & target agent resolution.
        3. Scoped knowledge document retrieval.
        4. Scoped full historical profile generation (all-time calls, cycles, simulations).
        5. Grounded system prompt & conversation history assembly.
        6. AI Provider text completion.
        7. Response post-processing & sanitization.
        8. Structured response payload.
        """
        # 1. Validate & extract query text (audio or text)
        query_text, input_type = await cls.validate_and_extract_input(
            message=message,
            audio_file=audio_file,
        )

        # 2. Resolve target agent with strict tenant isolation
        target_agent_id = cls.resolve_target_agent(
            current_user=current_user,
            requested_agent_id=agent_id,
            context=context,
        )

        # Determine whether current_user is acting in an administrative role
        norm_role = normalize_role(current_user.role)
        is_admin = norm_role in (
            InternalRole.SUPER_ADMIN,
            InternalRole.COMPANY_ADMIN,
            InternalRole.SERVICE_MANAGER,
            InternalRole.TEAM_COORDINATOR,
        )

        # 3. Detect temporal period request and enforce 15-day limit
        period_result = cls.detect_detailed_period_request(
            query_text=query_text,
            is_admin=is_admin,
        )

        # If user requests a detailed analysis over a period > 15 days, block mass evaluation:
        if period_result.is_detailed_period_request and period_result.exceeds_limit:
            return {
                "response": period_result.blocking_message,
                "user_query": query_text,
                "input_type": input_type,
                "sources": [],
                "agent_id": target_agent_id,
                "company_id": context.company_id,
            }

        # 4. Fetch knowledge documents
        documents = await cls.fetch_knowledge_documents(
            db=db,
            context=context,
            target_agent_id=target_agent_id,
            cycle_id=cycle_id,
        )

        # 5. Build complete historical profile for target agent
        historical_profile = await cls.build_agent_historical_profile(
            db=db,
            context=context,
            target_agent_id=target_agent_id,
        )

        # 6. If within allowed 15-day period, build aggregated period summary
        detailed_period_summary = None
        if period_result.is_detailed_period_request and not period_result.exceeds_limit:
            detailed_period_summary = await cls.build_detailed_period_summary(
                db=db,
                context=context,
                target_agent_id=target_agent_id,
                start_date=period_result.start_date,
                end_date=period_result.end_date,
                raw_expression=period_result.raw_expression,
            )

        # 7. Build prompt and prepare message list
        system_instruction = cls.build_grounding_prompt(
            documents=documents,
            target_agent_id=target_agent_id,
            historical_profile=historical_profile,
            detailed_period_summary=detailed_period_summary,
            is_admin=is_admin,
        )

        history = cls.sanitize_history(conversation_history)

        messages = [{"role": "system", "content": system_instruction}]
        messages.extend(history)
        messages.append({"role": "user", "content": query_text})

        # 6. Execute LLM completion via existing abstraction
        try:
            response_text = await openai_service.complete_text(
                messages=messages,
                temperature=0.2,
                response_format=None,
            )
        except Exception as llm_err:
            logger.exception("Error invocando complete_text en Trainer Chatbot: %s", llm_err)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Error en el modelo de lenguaje del chatbot: {str(llm_err)}",
            )

        # 7. Post-process & sanitize response (zero leaks of technical IDs/tables, natural redirection)
        sanitized_response = cls.sanitize_response_text(
            text=response_text or "",
            is_admin=is_admin,
            query_text=query_text,
        )

        # 8. Format sources
        sources = [
            {
                "document_id": doc.id,
                "title": doc.title,
                "document_type": doc.document_type,
                "cycle_id": doc.cycle_id,
            }
            for doc in documents
        ]

        return {
            "response": sanitized_response,
            "user_query": query_text,
            "input_type": input_type,
            "sources": sources,
            "agent_id": target_agent_id,
            "company_id": context.company_id,
        }
