"""Service for generating, updating and retrieving deterministic Training Knowledge Documents."""
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional
from decimal import Decimal

from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCallEvaluation,
    TrainingCompletionStatus,
    TrainingKnowledgeDocument,
)
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, AgentTeamAssociation, UserTeamAssociation
from app.models.users import User

logger = logging.getLogger(__name__)


class TrainingKnowledgeService:
    """Service to produce, persist, and retrieve structured training knowledge documents."""

    @staticmethod
    async def _resolve_team_info(db: AsyncSession, hubspot_owner_id: str) -> tuple[Optional[int], Optional[str]]:
        """Resolves team_id and team_name for an agent if assigned to a team."""
        try:
            # 1. Check AgentTeamAssociation
            stmt1 = (
                select(Team.team_id, Team.team_name)
                .join(AgentTeamAssociation, AgentTeamAssociation.team_id == Team.team_id)
                .join(User, User.user_id == AgentTeamAssociation.user_id)
                .where(User.hubspot_owner_id == hubspot_owner_id)
            )
            res1 = await db.execute(stmt1)
            row = res1.first()
            if row:
                return row[0], row[1]

            # 2. Check UserTeamAssociation
            stmt2 = (
                select(Team.team_id, Team.team_name)
                .join(UserTeamAssociation, UserTeamAssociation.team_id == Team.team_id)
                .join(User, User.user_id == UserTeamAssociation.user_id)
                .where(User.hubspot_owner_id == hubspot_owner_id)
            )
            res2 = await db.execute(stmt2)
            row = res2.first()
            if row:
                return row[0], row[1]
        except Exception as e:
            logger.debug("Failed resolving team for owner %s: %s", hubspot_owner_id, e)

        return None, None

    @staticmethod
    async def _compute_cycle_number(db: AsyncSession, report: TrainingAgentReport) -> Optional[int]:
        """Computes sequential cycle number for the agent."""
        try:
            stmt = select(func.count(TrainingAgentReport.training_report_id)).where(
                TrainingAgentReport.hubspot_owner_id == report.hubspot_owner_id,
                TrainingAgentReport.period_start <= report.period_start,
            )
            res = await db.execute(stmt)
            count = res.scalar()
            return count if count and count > 0 else 1
        except Exception:
            return None

    @staticmethod
    def _extract_strengths_and_weaknesses(result_json: dict) -> tuple[List[str], List[str]]:
        """Extracts strengths and weaknesses from an evaluation result_json safely."""
        strengths = []
        weaknesses = []
        if not isinstance(result_json, dict):
            return strengths, weaknesses

        inner_res = result_json.get("result_json") if isinstance(result_json.get("result_json"), dict) else None
        search_dicts = []
        if inner_res:
            search_dicts.append(inner_res)
        search_dicts.append(result_json)

        for d in search_dicts:
            for key in ["objectives_met", "objetivos_cumplidos", "strengths", "fortalezas", "puntos_fuertes"]:
                if key in d and isinstance(d[key], list):
                    strengths = [str(x) for x in d[key] if x]
                    break
            if strengths:
                break

        for d in search_dicts:
            for key in ["areas_for_improvement", "objetivos_no_cumplidos", "weaknesses", "areas_mejora", "areas_de_mejora", "debilidades"]:
                if key in d and isinstance(d[key], list):
                    weaknesses = [str(x) for x in d[key] if x]
                    break
            if weaknesses:
                break

        # Fallback from boolean criteria if none extracted
        if not strengths and not weaknesses:
            target_dict = inner_res if inner_res else {
                k: v for k, v in result_json.items()
                if k not in ["score", "feedback", "transcription", "is_valid_roleplay", "criteria_evaluations"] and isinstance(v, bool)
            }
            for k, passed in target_dict.items():
                clean_name = k.replace("_", " ").strip().capitalize()
                if passed is True:
                    strengths.append(clean_name)
                elif passed is False:
                    weaknesses.append(clean_name)

        return strengths, weaknesses

    @staticmethod
    def _build_simulation_document_content(
        company_name: str,
        service_name: str,
        team_name: Optional[str],
        report: TrainingAgentReport,
        prompt: TrainingSimulationPrompt,
        evaluation: TrainingCallEvaluation,
        cycle_number: Optional[int],
    ) -> str:
        """Constructs human and LLM readable Markdown text for a simulation knowledge document."""
        lines = []
        lines.append("# DOCUMENTO DE CONOCIMIENTO: SIMULACIÓN")
        lines.append("")
        lines.append("## METADATOS Y CONTEXTO")
        lines.append(f"- **Tipo de Documento:** Simulación")
        lines.append(f"- **Empresa:** {company_name}")
        lines.append(f"- **Agente:** {report.agent_name} ({report.agent_initials}) [ID: {report.hubspot_owner_id}]")
        lines.append(f"- **Servicio:** {service_name}")
        if team_name:
            lines.append(f"- **Equipo:** {team_name}")
        cycle_num_str = f" (Ciclo #{cycle_number})" if cycle_number else ""
        period_start_str = report.period_start.strftime('%Y-%m-%d') if report.period_start else "N/A"
        period_end_str = report.period_end.strftime('%Y-%m-%d') if report.period_end else "N/A"
        lines.append(f"- **Ciclo:** ID {report.training_report_id}{cycle_num_str} (Periodo: {period_start_str} al {period_end_str})")
        lines.append(f"- **Simulación Nº:** {prompt.prompt_number}")
        lines.append(f"- **Título de la Simulación:** {prompt.title}")
        lines.append(f"- **Escenario:** {prompt.scenario_type}")
        lines.append("")

        lines.append("## OBJETIVOS DEL CICLO VINCULADOS")
        gen_objs = report.general_objectives_json or []
        lines.append("### Objetivos Generales:")
        if gen_objs:
            for obj in gen_objs:
                t = obj.get("title") or "Objetivo general"
                d = obj.get("description") or ""
                b = obj.get("base_score")
                base_info = f" (Base: {b})" if b is not None else ""
                lines.append(f"- {t}: {d}{base_info}")
        else:
            lines.append("- No especificados.")

        spec_objs = report.specific_objectives_json or []
        lines.append("### Objetivos Específicos:")
        if spec_objs:
            for obj in spec_objs:
                t = obj.get("title") or "Objetivo específico"
                d = obj.get("description") or ""
                b = obj.get("base_score")
                base_info = f" (Base: {b})" if b is not None else ""
                crit = obj.get("related_criteria") or []
                crit_info = f" [Criterios: {', '.join(crit)}]" if crit else ""
                lines.append(f"- {t}: {d}{base_info}{crit_info}")
        else:
            lines.append("- No especificados.")
        lines.append("")

        lines.append("## FOCO E INSTRUCCIONES DEL ROLEPLAY")
        if prompt.objective_focus_json:
            import json
            focus_str = json.dumps(prompt.objective_focus_json, ensure_ascii=False, indent=2) if isinstance(prompt.objective_focus_json, (dict, list)) else str(prompt.objective_focus_json)
            lines.append("### Foco del Objetivo:")
            lines.append(focus_str)
        lines.append("### Contexto e Instrucciones de la Simulación:")
        lines.append(prompt.prompt_text or "Sin instrucciones registradas.")
        lines.append("")

        lines.append("## RESULTADO DE LA EVALUACIÓN")
        score_val = f"{float(evaluation.score):.2f}" if evaluation.score is not None else "Sin puntuación"
        lines.append(f"- **Puntuación Global:** {score_val} / 10.0")
        lines.append("- **Feedback General:**")
        lines.append(evaluation.feedback or "Sin feedback general.")
        lines.append("")

        lines.append("## CRITERIOS EVALUADOS Y EVIDENCIA")
        result_json = evaluation.result_json if isinstance(evaluation.result_json, dict) else {}
        criteria_evals = result_json.get("criteria_evaluations")

        if isinstance(criteria_evals, list) and criteria_evals:
            for item in criteria_evals:
                if not isinstance(item, dict):
                    continue
                name = item.get("criterion_name") or item.get("criterion_key") or "Criterio"
                key = item.get("criterion_key") or ""
                key_info = f" ({key})" if key and key != name else ""
                lines.append(f"### Criterio: {name}{key_info}")
                sc = item.get("score")
                sc_str = f"{float(sc):.1f}" if sc is not None else "No evaluado"
                lines.append(f"- **Puntuación:** {sc_str} / 10.0")
                passed = item.get("passed")
                passed_str = "Superado" if passed is True else ("No superado" if passed is False else "No especificado")
                lines.append(f"- **Estado:** {passed_str}")
                if item.get("expected_behavior"):
                    lines.append(f"- **Comportamiento Esperado:** {item.get('expected_behavior')}")
                if item.get("observed_behavior"):
                    lines.append(f"- **Comportamiento Observado:** {item.get('observed_behavior')}")
                if item.get("evidence_quote"):
                    lines.append(f'- **Evidencia Textual:** "{item.get("evidence_quote")}"')
                turns = item.get("relevant_turns")
                if turns:
                    turns_str = ", ".join(str(t) for t in turns) if isinstance(turns, list) else str(turns)
                    lines.append(f"- **Turnos Relevantes:** {turns_str}")
                if item.get("reasoning"):
                    lines.append(f"- **Razonamiento:** {item.get('reasoning')}")
                if item.get("improvement_tip"):
                    lines.append(f"- **Recomendación / Mejora:** {item.get('improvement_tip')}")
                lines.append("")
        else:
            # Legacy criteria without structured evidence (zero fabrication)
            inner_crit = result_json.get("result_json") if isinstance(result_json.get("result_json"), dict) else None
            criteria_dict = inner_crit if inner_crit else {
                k: v for k, v in result_json.items()
                if k not in ["score", "feedback", "transcription", "is_valid_roleplay", "criteria_evaluations"] and isinstance(v, bool)
            }
            if criteria_dict:
                for k, v in criteria_dict.items():
                    c_name = k.replace("_", " ").strip().capitalize()
                    status_str = "Cumplido" if v is True else ("No cumplido" if v is False else str(v))
                    lines.append(f"- **{c_name}:** {status_str}")
                lines.append("")
            else:
                lines.append("- No se registraron criterios desglosados en esta evaluación.")
                lines.append("")

        strengths, weaknesses = TrainingKnowledgeService._extract_strengths_and_weaknesses(result_json)
        lines.append("## FORTALEZAS Y ÁREAS DE MEJORA")
        lines.append("### Fortalezas / Objetivos Cumplidos:")
        if strengths:
            for s in strengths:
                lines.append(f"- {s}")
        else:
            lines.append("- Sin fortalezas destacadas.")

        lines.append("### Áreas de Mejora / Debilidades:")
        if weaknesses:
            for w in weaknesses:
                lines.append(f"- {w}")
        else:
            lines.append("- Sin áreas de mejora identificadas.")
        lines.append("")

        lines.append("## TRANSCRIPCIÓN DE LA LLAMADA")
        lines.append(evaluation.transcription or "Sin transcripción registrada.")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _build_cycle_document_content(
        company_name: str,
        service_name: str,
        team_name: Optional[str],
        report: TrainingAgentReport,
        simulations_data: List[dict],
        cycle_number: Optional[int],
    ) -> str:
        """Constructs human and LLM readable Markdown text for a cycle summary knowledge document."""
        lines = []
        lines.append("# DOCUMENTO DE CONOCIMIENTO: RESUMEN DE CICLO")
        lines.append("")
        lines.append("## METADATOS Y CONTEXTO")
        lines.append(f"- **Tipo de Documento:** Resumen de Ciclo")
        lines.append(f"- **Empresa:** {company_name}")
        lines.append(f"- **Agente:** {report.agent_name} ({report.agent_initials}) [ID: {report.hubspot_owner_id}]")
        lines.append(f"- **Servicio:** {service_name}")
        if team_name:
            lines.append(f"- **Equipo:** {team_name}")
        cycle_num_str = f" (Ciclo #{cycle_number})" if cycle_number else ""
        period_start_str = report.period_start.strftime('%Y-%m-%d') if report.period_start else "N/A"
        period_end_str = report.period_end.strftime('%Y-%m-%d') if report.period_end else "N/A"
        lines.append(f"- **Ciclo:** ID {report.training_report_id}{cycle_num_str}")
        lines.append(f"- **Periodo Analizado:** {period_start_str} al {period_end_str}")
        avg_sc_str = f"{float(report.avg_evaluacion_global):.2f}" if report.avg_evaluacion_global is not None else "N/A"
        lines.append(f"- **Puntuación Media Global:** {avg_sc_str} / 10.0")
        lines.append(f"- **Estado del Ciclo:** {report.status}")
        lines.append("")

        # Objectives and Status
        lines.append("## OBJETIVOS DEL CICLO Y ESTADO FINAL")
        final_rep = report.final_report_json if isinstance(report.final_report_json, dict) else {}
        objs_status = final_rep.get("objectives_status") or []

        gen_objs = [o for o in objs_status if o.get("type") in ["general", "General"]] if objs_status else (report.general_objectives_json or [])
        spec_objs = [o for o in objs_status if o.get("type") in ["especifico", "specific", "Específico"]] if objs_status else (report.specific_objectives_json or [])

        lines.append("### Objetivos Generales:")
        if gen_objs:
            for o in gen_objs:
                t = o.get("title") or "Objetivo general"
                d = o.get("description") or ""
                b = o.get("base_score")
                s = o.get("score")
                st = o.get("status")
                delta = o.get("improvement_delta")
                just = o.get("justification")
                score_part = f" (Base: {b} -> Final: {s}, Delta: {delta:+.2f})" if b is not None and s is not None and delta is not None else ""
                st_part = f" -> [{st.upper()}]" if st else ""
                lines.append(f"- **{t}**: {d}{score_part}{st_part}")
                if just:
                    lines.append(f"  *Justificación:* {just}")
        else:
            lines.append("- No registrados.")

        lines.append("### Objetivos Específicos:")
        if spec_objs:
            for o in spec_objs:
                t = o.get("title") or "Objetivo específico"
                d = o.get("description") or ""
                b = o.get("base_score")
                s = o.get("score")
                st = o.get("status")
                delta = o.get("improvement_delta")
                just = o.get("justification")
                crit = o.get("related_criteria")
                score_part = f" (Base: {b} -> Final: {s}, Delta: {delta:+.2f})" if b is not None and s is not None and delta is not None else ""
                st_part = f" -> [{st.upper()}]" if st else ""
                crit_part = f" [Criterios: {', '.join(crit)}]" if crit else ""
                lines.append(f"- **{t}**: {d}{crit_part}{score_part}{st_part}")
                if just:
                    lines.append(f"  *Justificación:* {just}")
        else:
            lines.append("- No registrados.")
        lines.append("")

        # Completed Simulations
        lines.append("## SIMULACIONES REALIZADAS")
        if simulations_data:
            for sim in simulations_data:
                p_num = sim.get("prompt_number")
                title = sim.get("title") or "Simulación"
                scen = sim.get("scenario_type") or "Estándar"
                sc = sim.get("score")
                sc_str = f"{float(sc):.2f}" if sc is not None else "N/A"
                fb = sim.get("feedback") or "Sin feedback registrado."
                fb_snippet = fb[:300] + "..." if len(fb) > 300 else fb
                lines.append(f"- **Simulación {p_num}: {title}** ({scen}) — Puntuación: {sc_str} / 10.0")
                lines.append(f"  *Feedback:* {fb_snippet}")
        else:
            lines.append("- No constan simulaciones registradas para este ciclo.")
        lines.append("")

        # Strengths and Weaknesses
        lines.append("## SÍNTESIS DE DESEMPEÑO")
        consolidated_strengths = final_rep.get("strengths") or []
        consolidated_weaknesses = final_rep.get("weaknesses") or []

        # Fallback to report columns if final_rep doesn't have them
        if not consolidated_strengths and report.strengths_json:
            raw_s = report.strengths_json
            if isinstance(raw_s, list):
                consolidated_strengths = raw_s
            elif isinstance(raw_s, dict):
                consolidated_strengths = raw_s.get("strengths", [])

        if not consolidated_weaknesses and report.weaknesses_json:
            raw_w = report.weaknesses_json
            if isinstance(raw_w, list):
                consolidated_weaknesses = raw_w
            elif isinstance(raw_w, dict):
                consolidated_weaknesses = raw_w.get("weaknesses", [])

        lines.append("### Fortalezas Consolidadas:")
        if consolidated_strengths:
            for s in consolidated_strengths:
                lines.append(f"- {s}")
        else:
            lines.append("- Sin fortalezas consolidadas registradas.")

        lines.append("### Áreas de Mejora / Debilidades Persistentes:")
        if consolidated_weaknesses:
            for w in consolidated_weaknesses:
                lines.append(f"- {w}")
        else:
            lines.append("- Sin áreas de mejora consolidadas registradas.")
        lines.append("")

        # Evolution and Recommendations
        lines.append("## EVOLUCIÓN Y RECOMENDACIONES")
        evolution = (
            final_rep.get("summary_final")
            or report.evolution_summary
            or report.summary_general
            or "Sin resumen de evolución disponible."
        )
        recommendations = (
            final_rep.get("recommendations")
            or "Sin recomendaciones registradas para el próximo ciclo."
        )
        lines.append("### Resumen de Evolución:")
        lines.append(evolution)
        lines.append("")
        lines.append("### Recomendaciones para el Próximo Ciclo:")
        lines.append(recommendations)
        lines.append("")

        return "\n".join(lines)

    @classmethod
    async def generate_simulation_knowledge_document(
        cls, db: AsyncSession, evaluation_id: int
    ) -> Optional[TrainingKnowledgeDocument]:
        """
        Generates or updates a deterministic knowledge document for an individual simulation evaluation.
        Ensures strict idempotency: updates existing row if already present.
        """
        stmt_eval = select(TrainingCallEvaluation).where(TrainingCallEvaluation.evaluation_id == evaluation_id)
        res_eval = await db.execute(stmt_eval)
        evaluation = res_eval.scalars().first()
        if not evaluation:
            logger.warning("generate_simulation_knowledge_document: Evaluation ID %d not found.", evaluation_id)
            return None

        # Fetch cycle report
        stmt_report = select(TrainingAgentReport).where(
            TrainingAgentReport.training_report_id == evaluation.cycle_id
        )
        res_report = await db.execute(stmt_report)
        report = res_report.scalars().first()
        if not report:
            logger.warning("generate_simulation_knowledge_document: Cycle %d not found for eval %d.", evaluation.cycle_id, evaluation_id)
            return None

        # Fetch prompt
        stmt_prompt = select(TrainingSimulationPrompt).where(
            TrainingSimulationPrompt.simulation_prompt_id == evaluation.conversation_id
        )
        res_prompt = await db.execute(stmt_prompt)
        prompt = res_prompt.scalars().first()
        if not prompt:
            logger.warning("generate_simulation_knowledge_document: Prompt %d not found for eval %d.", evaluation.conversation_id, evaluation_id)
            return None

        # Fetch Company name
        company_name = f"Empresa ID {report.company_id}"
        if report.company_id:
            stmt_comp = select(Company.company_name).where(Company.company_id == report.company_id)
            res_comp = await db.execute(stmt_comp)
            c_name = res_comp.scalar()
            if c_name:
                company_name = c_name

        # Fetch Service name
        service_name = f"Servicio ID {report.service_id}"
        if report.service_id:
            stmt_serv = select(Service.service_name).where(Service.service_id == report.service_id)
            res_serv = await db.execute(stmt_serv)
            s_name = res_serv.scalar()
            if s_name:
                service_name = s_name

        # Resolve team
        team_id, team_name = await cls._resolve_team_info(db, report.hubspot_owner_id)
        cycle_number = await cls._compute_cycle_number(db, report)

        title = f"Simulación {prompt.prompt_number}: {prompt.title} - Agente {report.agent_name}"
        content = cls._build_simulation_document_content(
            company_name=company_name,
            service_name=service_name,
            team_name=team_name,
            report=report,
            prompt=prompt,
            evaluation=evaluation,
            cycle_number=cycle_number,
        )

        metadata_json = {
            "company_id": report.company_id,
            "company_name": company_name,
            "hubspot_owner_id": report.hubspot_owner_id,
            "agent_name": report.agent_name,
            "agent_initials": report.agent_initials,
            "service_id": report.service_id,
            "service_name": service_name,
            "team_id": team_id,
            "team_name": team_name,
            "cycle_id": report.training_report_id,
            "cycle_number": cycle_number,
            "simulation_id": prompt.simulation_prompt_id,
            "simulation_number": prompt.prompt_number,
            "evaluation_id": evaluation.evaluation_id,
            "document_type": "simulation",
            "score": float(evaluation.score) if evaluation.score is not None else None,
            "scenario_type": prompt.scenario_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Idempotent Upsert
        stmt_existing = select(TrainingKnowledgeDocument).where(
            and_(
                TrainingKnowledgeDocument.cycle_id == report.training_report_id,
                TrainingKnowledgeDocument.simulation_id == prompt.simulation_prompt_id,
                TrainingKnowledgeDocument.document_type == "simulation",
            )
        )
        res_existing = await db.execute(stmt_existing)
        existing_doc = res_existing.scalars().first()

        if existing_doc:
            existing_doc.company_id = report.company_id
            existing_doc.hubspot_owner_id = report.hubspot_owner_id
            existing_doc.service_id = report.service_id
            existing_doc.team_id = team_id
            existing_doc.evaluation_id = evaluation.evaluation_id
            existing_doc.title = title
            existing_doc.content = content
            existing_doc.metadata_json = metadata_json
            existing_doc.updated_at = datetime.now(timezone.utc)
            doc = existing_doc
            logger.info("Updated existing simulation knowledge document ID %d for eval %d.", doc.id, evaluation_id)
        else:
            doc = TrainingKnowledgeDocument(
                company_id=report.company_id,
                hubspot_owner_id=report.hubspot_owner_id,
                service_id=report.service_id,
                team_id=team_id,
                cycle_id=report.training_report_id,
                simulation_id=prompt.simulation_prompt_id,
                evaluation_id=evaluation.evaluation_id,
                document_type="simulation",
                title=title,
                content=content,
                metadata_json=metadata_json,
            )
            db.add(doc)
            logger.info("Created new simulation knowledge document for eval %d.", evaluation_id)

        await db.commit()
        await db.refresh(doc)
        return doc

    @classmethod
    async def generate_cycle_knowledge_documents(
        cls, db: AsyncSession, cycle_id: int
    ) -> List[TrainingKnowledgeDocument]:
        """
        Generates or updates knowledge documents for a completed cycle:
        1. Ensures simulation documents exist for each completed/evaluated simulation of the cycle.
        2. Generates the cycle summary document.
        Ensures strict idempotency: no duplicates are generated on repeated calls.
        """
        stmt_report = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == cycle_id)
        res_report = await db.execute(stmt_report)
        report = res_report.scalars().first()
        if not report:
            logger.warning("generate_cycle_knowledge_documents: Cycle ID %d not found.", cycle_id)
            return []

        # Check that cycle is completed and has not pending simulations
        stmt_comp = select(
            func.count(TrainingCompletionStatus.completion_id)
        ).where(TrainingCompletionStatus.training_report_id == cycle_id)
        res_comp = await db.execute(stmt_comp)
        total_count = res_comp.scalar() or 0

        stmt_done = select(
            func.count(TrainingCompletionStatus.completion_id)
        ).where(
            and_(
                TrainingCompletionStatus.training_report_id == cycle_id,
                TrainingCompletionStatus.status == "completed",
            )
        )
        res_done = await db.execute(stmt_done)
        done_count = res_done.scalar() or 0

        if total_count == 0 or done_count < total_count:
            logger.info("generate_cycle_knowledge_documents: Cycle %d is not yet complete (%d/%d). Skipping cycle document generation.", cycle_id, done_count, total_count)
            return []

        # 1. Generate/refresh simulation documents for all completed simulations
        stmt_statuses = (
            select(
                TrainingCompletionStatus.simulation_prompt_id,
                TrainingCompletionStatus.evaluation_id,
            )
            .where(
                and_(
                    TrainingCompletionStatus.training_report_id == cycle_id,
                    TrainingCompletionStatus.status == "completed",
                    TrainingCompletionStatus.evaluation_id != None,
                )
            )
        )
        res_statuses = await db.execute(stmt_statuses)
        status_tuples = list(res_statuses.all())

        generated_docs: List[TrainingKnowledgeDocument] = []
        simulations_data: List[dict] = []

        for sim_prompt_id, eval_id in status_tuples:
            if eval_id:
                sim_doc = await cls.generate_simulation_knowledge_document(db, eval_id)
                if sim_doc:
                    generated_docs.append(sim_doc)

                # Collect summary data for cycle document via scalar selects
                stmt_eval = select(TrainingCallEvaluation.score, TrainingCallEvaluation.feedback).where(
                    TrainingCallEvaluation.evaluation_id == eval_id
                )
                r_eval = await db.execute(stmt_eval)
                ev_row = r_eval.first()

                stmt_p = select(
                    TrainingSimulationPrompt.prompt_number,
                    TrainingSimulationPrompt.title,
                    TrainingSimulationPrompt.scenario_type,
                ).where(
                    TrainingSimulationPrompt.simulation_prompt_id == sim_prompt_id
                )
                r_p = await db.execute(stmt_p)
                p_row = r_p.first()

                if p_row and ev_row:
                    simulations_data.append({
                        "prompt_number": p_row[0],
                        "title": p_row[1],
                        "scenario_type": p_row[2],
                        "score": float(ev_row[0]) if ev_row[0] is not None else None,
                        "feedback": ev_row[1] or "",
                    })

        # Sort simulations by prompt number
        simulations_data.sort(key=lambda x: x.get("prompt_number") or 0)

        # 2. Reload fresh report to avoid expired object issues after simulation commits
        stmt_report_fresh = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == cycle_id)
        res_report_fresh = await db.execute(stmt_report_fresh)
        report = res_report_fresh.scalars().first()
        if not report:
            return generated_docs

        # Resolve Company, Service, Team, Cycle Number
        company_name = f"Empresa ID {report.company_id}"
        if report.company_id:
            stmt_comp_name = select(Company.company_name).where(Company.company_id == report.company_id)
            res_comp_name = await db.execute(stmt_comp_name)
            c_name = res_comp_name.scalar()
            if c_name:
                company_name = c_name

        service_name = f"Servicio ID {report.service_id}"
        if report.service_id:
            stmt_serv_name = select(Service.service_name).where(Service.service_id == report.service_id)
            res_serv_name = await db.execute(stmt_serv_name)
            s_name = res_serv_name.scalar()
            if s_name:
                service_name = s_name

        team_id, team_name = await cls._resolve_team_info(db, report.hubspot_owner_id)
        cycle_number = await cls._compute_cycle_number(db, report)

        cycle_title = f"Resumen de Ciclo {report.training_report_id} - Agente {report.agent_name}"
        cycle_content = cls._build_cycle_document_content(
            company_name=company_name,
            service_name=service_name,
            team_name=team_name,
            report=report,
            simulations_data=simulations_data,
            cycle_number=cycle_number,
        )

        cycle_metadata_json = {
            "company_id": report.company_id,
            "company_name": company_name,
            "hubspot_owner_id": report.hubspot_owner_id,
            "agent_name": report.agent_name,
            "agent_initials": report.agent_initials,
            "service_id": report.service_id,
            "service_name": service_name,
            "team_id": team_id,
            "team_name": team_name,
            "cycle_id": report.training_report_id,
            "cycle_number": cycle_number,
            "simulation_id": None,
            "simulation_number": None,
            "evaluation_id": None,
            "document_type": "cycle",
            "avg_score": float(report.avg_evaluacion_global) if report.avg_evaluacion_global is not None else None,
            "simulations_count": len(simulations_data),
            "status": report.status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Idempotent Upsert for Cycle Document
        stmt_existing_cycle = select(TrainingKnowledgeDocument).where(
            and_(
                TrainingKnowledgeDocument.cycle_id == report.training_report_id,
                TrainingKnowledgeDocument.document_type == "cycle",
                TrainingKnowledgeDocument.simulation_id == None,
            )
        )
        res_existing_cycle = await db.execute(stmt_existing_cycle)
        existing_cycle_doc = res_existing_cycle.scalars().first()

        if existing_cycle_doc:
            existing_cycle_doc.company_id = report.company_id
            existing_cycle_doc.hubspot_owner_id = report.hubspot_owner_id
            existing_cycle_doc.service_id = report.service_id
            existing_cycle_doc.team_id = team_id
            existing_cycle_doc.title = cycle_title
            existing_cycle_doc.content = cycle_content
            existing_cycle_doc.metadata_json = cycle_metadata_json
            existing_cycle_doc.updated_at = datetime.now(timezone.utc)
            cycle_doc = existing_cycle_doc
            logger.info("Updated existing cycle knowledge document ID %d for cycle %d.", cycle_doc.id, cycle_id)
        else:
            cycle_doc = TrainingKnowledgeDocument(
                company_id=report.company_id,
                hubspot_owner_id=report.hubspot_owner_id,
                service_id=report.service_id,
                team_id=team_id,
                cycle_id=report.training_report_id,
                simulation_id=None,
                evaluation_id=None,
                document_type="cycle",
                title=cycle_title,
                content=cycle_content,
                metadata_json=cycle_metadata_json,
            )
            db.add(cycle_doc)
            logger.info("Created new cycle knowledge document for cycle %d.", cycle_id)

        await db.commit()
        await db.refresh(cycle_doc)
        generated_docs.append(cycle_doc)

        return generated_docs

    @classmethod
    async def get_document_by_id(
        cls, db: AsyncSession, document_id: int
    ) -> Optional[TrainingKnowledgeDocument]:
        """Retrieves a single knowledge document by primary key."""
        stmt = select(TrainingKnowledgeDocument).where(TrainingKnowledgeDocument.id == document_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @classmethod
    async def list_documents(
        cls,
        db: AsyncSession,
        company_id: Optional[int] = None,
        hubspot_owner_id: Optional[str] = None,
        service_id: Optional[int] = None,
        team_id: Optional[int] = None,
        cycle_id: Optional[int] = None,
        simulation_id: Optional[int] = None,
        document_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[TrainingKnowledgeDocument]:
        """Lists knowledge documents applying filters with pagination."""
        stmt = select(TrainingKnowledgeDocument)
        filters = []
        if company_id is not None:
            filters.append(TrainingKnowledgeDocument.company_id == company_id)
        if hubspot_owner_id is not None:
            filters.append(TrainingKnowledgeDocument.hubspot_owner_id == hubspot_owner_id)
        if service_id is not None:
            filters.append(TrainingKnowledgeDocument.service_id == service_id)
        if team_id is not None:
            filters.append(TrainingKnowledgeDocument.team_id == team_id)
        if cycle_id is not None:
            filters.append(TrainingKnowledgeDocument.cycle_id == cycle_id)
        if simulation_id is not None:
            filters.append(TrainingKnowledgeDocument.simulation_id == simulation_id)
        if document_type is not None:
            filters.append(TrainingKnowledgeDocument.document_type == document_type)

        if filters:
            stmt = stmt.where(and_(*filters))

        stmt = stmt.order_by(desc(TrainingKnowledgeDocument.id)).offset(offset).limit(limit)
        res = await db.execute(stmt)
        return list(res.scalars().all())
