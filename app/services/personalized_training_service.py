"""Service for personalized training report generation and management using Azure OpenAI."""
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional
from sqlalchemy import select, and_, or_, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
)
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.models.users import User
from app.services.openai_service import complete_text

logger = logging.getLogger(__name__)


class PersonalizedTrainingService:

    @staticmethod
    async def get_agent_settings(db: AsyncSession) -> List[TrainingAgentSetting]:
        """List all agent settings, ordered by agent name."""
        stmt = select(TrainingAgentSetting).order_by(TrainingAgentSetting.agent_name.asc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def update_agent_setting(
        db: AsyncSession, hubspot_owner_id: str, is_enabled: Optional[bool] = None, agent_name: Optional[str] = None, agent_initials: Optional[str] = None
    ) -> Optional[TrainingAgentSetting]:
        """Update an agent setting. Dynamically creates it if it doesn't exist yet."""
        stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == hubspot_owner_id)
        res = await db.execute(stmt)
        setting = res.scalars().first()

        if not setting:
            # If agent settings row doesn't exist, we resolve agent name dynamically or fallback
            if not agent_name or not agent_initials:
                raise ValueError("Agent settings not found. Name and initials are required to create a new setting.")
            setting = TrainingAgentSetting(
                hubspot_owner_id=hubspot_owner_id,
                agent_name=agent_name,
                agent_initials=agent_initials,
                is_enabled=is_enabled if is_enabled is not None else True
            )
            db.add(setting)
        else:
            if is_enabled is not None:
                setting.is_enabled = is_enabled
            if agent_name is not None:
                setting.agent_name = agent_name
            if agent_initials is not None:
                setting.agent_initials = agent_initials

        await db.commit()
        await db.refresh(setting)
        return setting

    @staticmethod
    async def get_agent_overview(db: AsyncSession) -> List[dict]:
        """Returns the overview list of all agents for the admin tracking dashboard."""
        settings = await PersonalizedTrainingService.get_agent_settings(db)
        overview = []

        for s in settings:
            # Fetch current (latest, is_current=True) report for the agent
            stmt_rep = select(TrainingAgentReport).where(
                and_(
                    TrainingAgentReport.hubspot_owner_id == s.hubspot_owner_id,
                    TrainingAgentReport.is_current == True
                )
            ).order_by(desc(TrainingAgentReport.training_report_id))
            res_rep = await db.execute(stmt_rep)
            report = res_rep.scalars().first()

            # Count previous reports
            stmt_prev_count = select(func.count(TrainingAgentReport.training_report_id)).where(
                TrainingAgentReport.hubspot_owner_id == s.hubspot_owner_id
            )
            res_prev_count = await db.execute(stmt_prev_count)
            prev_count = res_prev_count.scalar() or 0

            item = {
                "hubspot_owner_id": s.hubspot_owner_id,
                "agent_name": s.agent_name,
                "agent_initials": s.agent_initials,
                "is_enabled": s.is_enabled,
                "current_report_id": None,
                "current_period_start": None,
                "current_period_end": None,
                "status": "no_data",
                "evaluations_count": 0,
                "summary_general": None,
                "objectives_count": 0,
                "simulation_prompts_count": 0,
                "progress_completed": 0,
                "progress_total": 4,
                "progress_percentage": Decimal("0.0"),
                "last_generated_at": None,
                "previous_reports_count": prev_count
            }

            if report:
                item["current_report_id"] = report.training_report_id
                item["current_period_start"] = report.period_start
                item["current_period_end"] = report.period_end
                item["status"] = report.status
                item["evaluations_count"] = report.evaluations_count
                item["summary_general"] = report.summary_general[:100] + "..." if report.summary_general and len(report.summary_general) > 100 else report.summary_general
                item["last_generated_at"] = report.generated_at or report.created_at

                if report.status == "completed":
                    item["objectives_count"] = 6  # 3 general + 3 specific
                    item["simulation_prompts_count"] = 4
                    
                    # Fetch completion progress
                    stmt_comp = select(func.count(TrainingCompletionStatus.completion_id)).where(
                        and_(
                            TrainingCompletionStatus.training_report_id == report.training_report_id,
                            TrainingCompletionStatus.status == "completed"
                        )
                    )
                    res_comp = await db.execute(stmt_comp)
                    comp_count = res_comp.scalar() or 0
                    
                    item["progress_completed"] = comp_count
                    item["progress_percentage"] = Decimal(str(comp_count / 4.0 * 100.0)).quantize(Decimal("0.01"))

            overview.append(item)

        return overview

    @staticmethod
    async def get_agent_detail(db: AsyncSession, hubspot_owner_id: str) -> Optional[dict]:
        """Returns detailed personalized training information for a specific agent."""
        stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == hubspot_owner_id)
        res_set = await db.execute(stmt_set)
        setting = res_set.scalars().first()

        if not setting:
            return None

        # Fetch current report
        stmt_rep = select(TrainingAgentReport).where(
            and_(
                TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
                TrainingAgentReport.is_current == True
            )
        ).order_by(desc(TrainingAgentReport.training_report_id))
        res_rep = await db.execute(stmt_rep)
        report = res_rep.scalars().first()

        current_report_data = None
        progress_completed = 0
        progress_percentage = Decimal("0.0")

        if report:
            # Fetch prompts
            stmt_prompts = select(TrainingSimulationPrompt).where(
                TrainingSimulationPrompt.training_report_id == report.training_report_id
            ).order_by(TrainingSimulationPrompt.prompt_number.asc())
            res_prompts = await db.execute(stmt_prompts)
            prompts = list(res_prompts.scalars().all())

            # Fetch completion statuses
            stmt_comp = select(TrainingCompletionStatus).where(
                TrainingCompletionStatus.training_report_id == report.training_report_id
            ).order_by(TrainingCompletionStatus.completion_id.asc())
            res_comp = await db.execute(stmt_comp)
            completions = list(res_comp.scalars().all())

            completed_count = sum(1 for c in completions if c.status == "completed")
            progress_completed = completed_count
            progress_percentage = Decimal(str(completed_count / 4.0 * 100.0)).quantize(Decimal("0.01"))

            current_report_data = {
                **report.__dict__,
                "prompts": prompts,
                "completion_statuses": completions
            }

        # Fetch historical reports (excluding current report or just listing all)
        stmt_hist = select(TrainingAgentReport).where(
            TrainingAgentReport.hubspot_owner_id == hubspot_owner_id
        ).order_by(desc(TrainingAgentReport.period_start))
        res_hist = await db.execute(stmt_hist)
        history = list(res_hist.scalars().all())

        return {
            "agent_setting": setting,
            "current_report": current_report_data,
            "progress_completed": progress_completed,
            "progress_total": 4,
            "progress_percentage": progress_percentage,
            "history": history,
            "evolution_summary": report.evolution_summary if report else None
        }

    @staticmethod
    async def get_report_by_id(db: AsyncSession, report_id: int) -> Optional[dict]:
        """Returns details for a specific report ID."""
        stmt = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == report_id)
        res = await db.execute(stmt)
        report = res.scalars().first()

        if not report:
            return None

        # Fetch prompts
        stmt_prompts = select(TrainingSimulationPrompt).where(
            TrainingSimulationPrompt.training_report_id == report_id
        ).order_by(TrainingSimulationPrompt.prompt_number.asc())
        res_prompts = await db.execute(stmt_prompts)
        prompts = list(res_prompts.scalars().all())

        # Fetch completion statuses
        stmt_comp = select(TrainingCompletionStatus).where(
            TrainingCompletionStatus.training_report_id == report_id
        ).order_by(TrainingCompletionStatus.completion_id.asc())
        res_comp = await db.execute(stmt_comp)
        completions = list(res_comp.scalars().all())

        return {
            **report.__dict__,
            "prompts": prompts,
            "completion_statuses": completions
        }

    @staticmethod
    async def aggregate_agent_evaluations(db: AsyncSession, hubspot_owner_id: str, period_start: datetime, period_end: datetime) -> dict:
        """
        Gathers and aggregates mass evaluations for a specific agent and period.
        Filters strictly by call_timestamp.
        """
        # Fetch completed mass evaluation results
        stmt = select(MassEvaluationResult).where(
            and_(
                MassEvaluationResult.hubspot_owner_id == hubspot_owner_id,
                MassEvaluationResult.call_timestamp >= period_start,
                MassEvaluationResult.call_timestamp <= period_end,
                MassEvaluationResult.status == "completed"
            )
        )
        res = await db.execute(stmt)
        results = list(res.scalars().all())

        if not results:
            return {
                "evaluations_count": 0,
                "calls_count": 0,
                "avg_evaluacion_global": None,
                "criteria_averages": {},
                "tipologia_distribution": {},
                "critical_feedbacks": [],
                "cierre_cita_rate": None,
            }

        # Calculate basic counts
        evaluations_count = len(results)
        calls_count = len(set(r.call_id for r in results))

        # Calculate average global evaluation
        global_scores = []
        for r in results:
            if r.result_json and "evaluacion_global" in r.result_json:
                try:
                    score = float(r.result_json["evaluacion_global"])
                    global_scores.append(score)
                except (ValueError, TypeError):
                    pass

        avg_global = sum(global_scores) / len(global_scores) if global_scores else None

        # Gather typology distribution
        tipologias = {}
        for r in results:
            if r.typology_name:
                tipologias[r.typology_name] = tipologias.get(r.typology_name, 0) + 1
            elif r.result_json and "tipo_llamada" in r.result_json:
                tipo = r.result_json["tipo_llamada"]
                tipologias[tipo] = tipologias.get(tipo, 0) + 1

        # Fetch detailed criteria results to calculate averages and gather feedbacks
        analysis_ids = [r.mass_analysis_id for r in results]
        stmt_c = select(MassEvaluationCriterionResult).where(
            MassEvaluationCriterionResult.mass_analysis_id.in_(analysis_ids)
        )
        res_c = await db.execute(stmt_c)
        criterion_results = list(res_c.scalars().all())

        # Consolidate criteria scores
        criteria_raw = {}  # key -> list of numeric scores or boolean values
        feedbacks = []  # List of dicts with {"criterion": "...", "feedback": "...", "score": ...}
        
        cierre_cita_vals = []

        for cr in criterion_results:
            key = cr.criterion_key
            if not cr.is_applicable:
                continue

            if key not in criteria_raw:
                criteria_raw[key] = {
                    "name": cr.criterion_name or key,
                    "scores": [],
                    "booleans": []
                }

            if cr.numeric_value is not None:
                criteria_raw[key]["scores"].append(float(cr.numeric_value))
            elif cr.boolean_value is not None:
                criteria_raw[key]["booleans"].append(cr.boolean_value)

            if key == "cierre_cita" and cr.boolean_value is not None:
                cierre_cita_vals.append(cr.boolean_value)

            # Gather critical feedback (e.g. numeric score below 8.0, or boolean is False)
            if cr.feedback:
                is_critical = False
                score_str = ""
                if cr.numeric_value is not None:
                    score_val = float(cr.numeric_value)
                    score_str = f"({score_val}/10)"
                    if score_val < 8.0:
                        is_critical = True
                elif cr.boolean_value is not None:
                    score_str = "(Sí)" if cr.boolean_value else "(No)"
                    if not cr.boolean_value:
                        is_critical = True

                if is_critical:
                    feedbacks.append({
                        "criterion": cr.criterion_name or key,
                        "score": score_str,
                        "feedback": cr.feedback
                    })

        # Calculate averages for criteria
        criteria_averages = {}
        for key, raw in criteria_raw.items():
            if raw["scores"]:
                criteria_averages[key] = {
                    "name": raw["name"],
                    "value": sum(raw["scores"]) / len(raw["scores"]),
                    "type": "numeric"
                }
            elif raw["booleans"]:
                # Express as percentage of True values
                trues = sum(1 for b in raw["booleans"] if b)
                criteria_averages[key] = {
                    "name": raw["name"],
                    "value": (trues / len(raw["booleans"])) * 100.0,
                    "type": "boolean"
                }

        cierre_cita_rate = (sum(1 for v in cierre_cita_vals if v) / len(cierre_cita_vals)) * 100.0 if cierre_cita_vals else None

        # Cap critical feedbacks at 20 items to avoid token bloat
        feedbacks = feedbacks[:20]

        return {
            "evaluations_count": evaluations_count,
            "calls_count": calls_count,
            "avg_evaluacion_global": avg_global,
            "criteria_averages": criteria_averages,
            "tipologia_distribution": tipologias,
            "critical_feedbacks": feedbacks,
            "cierre_cita_rate": cierre_cita_rate,
        }

    @staticmethod
    async def generate_report_for_agent(
        db: AsyncSession,
        hubspot_owner_id: str,
        period_start: datetime,
        period_end: datetime,
        run_id: Optional[int] = None,
        force_regenerate: bool = False
    ) -> TrainingAgentReport:
        """
        Aggregates data, generates reports using AI, saves report, 6 objectives,
        and 4 simulation prompts to DB. Idempotent by period/agent.
        """
        # Fetch agent settings
        stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == hubspot_owner_id)
        res_set = await db.execute(stmt_set)
        agent_setting = res_set.scalars().first()

        if not agent_setting:
            raise ValueError(f"No agent settings found for HubSpot Owner ID {hubspot_owner_id}")

        agent_name = agent_setting.agent_name
        agent_initials = agent_setting.agent_initials

        # 1. Check for duplicates
        stmt_dup = select(TrainingAgentReport).where(
            and_(
                TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
                TrainingAgentReport.period_start == period_start,
                TrainingAgentReport.period_end == period_end,
                TrainingAgentReport.is_current == True
            )
        )
        res_dup = await db.execute(stmt_dup)
        existing_report = res_dup.scalars().first()

        if existing_report and not force_regenerate:
            logger.info("Report already exists for agent %s in period %s to %s. Returning existing.", hubspot_owner_id, period_start, period_end)
            return existing_report

        # Create base report in database as 'running'
        new_report = TrainingAgentReport(
            training_run_id=run_id,
            hubspot_owner_id=hubspot_owner_id,
            agent_name=agent_name,
            agent_initials=agent_initials,
            period_start=period_start,
            period_end=period_end,
            status="running",
            is_current=True
        )
        db.add(new_report)
        await db.flush()

        # If there's an existing current report, we mark it superseded later if we succeed.
        try:
            # 2. Aggregate evaluations
            aggregates = await PersonalizedTrainingService.aggregate_agent_evaluations(db, hubspot_owner_id, period_start, period_end)

            if aggregates["evaluations_count"] == 0:
                logger.info("No evaluations found for agent %s in period %s to %s. Skipping report.", hubspot_owner_id, period_start, period_end)
                new_report.status = "skipped"
                new_report.skipped_reason = "No hay evaluaciones masivas en el periodo."
                await db.commit()
                return new_report

            # 3. Retrieve historical report for context
            stmt_prev = select(TrainingAgentReport).where(
                and_(
                    TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
                    TrainingAgentReport.status == "completed",
                    TrainingAgentReport.period_start < period_start
                )
            ).order_by(desc(TrainingAgentReport.period_start))
            res_prev = await db.execute(stmt_prev)
            prev_report = res_prev.scalars().first()

            prev_context = "No hay informes de entrenamiento anteriores disponibles (Este es el primer informe del agente)."
            if prev_report:
                prev_context = (
                    f"Informe anterior del periodo {prev_report.period_start} al {prev_report.period_end}.\n"
                    f"Resumen General Anterior: {prev_report.summary_general}\n"
                    f"Objetivos Generales Anteriores: {json.dumps(prev_report.general_objectives_json, ensure_ascii=False)}\n"
                    f"Objetivos Específicos Anteriores: {json.dumps(prev_report.specific_objectives_json, ensure_ascii=False)}"
                )

            # 4. Construct AI prompt
            system_prompt = (
                "Eres un Director de Capacitación Comercial y Coach de Atención Clínica especializado en Boston Medical Group "
                "(salud sexual masculina). Tu labor es analizar las llamadas reales de los agentes de atención al paciente "
                "y generar planes de capacitación personalizados, objetivos medibles y simulaciones de roleplay altamente efectivas.\n\n"
                "INSTRUCCIÓN CLAVE:\n"
                "Debes devolver estrictamente un objeto JSON estructurado que contenga:\n"
                "- summary_general: Texto claro, consultivo y profesional en español.\n"
                "- strengths: Una lista de exactamente 3 puntos fuertes basados en evidencias reales del periodo.\n"
                "- weaknesses: Una lista de exactamente 3 puntos débiles accionables.\n"
                "- notable_data: Una lista de exactamente 3 hallazgos o datos notables del periodo.\n"
                "- evolution_summary: Análisis de la evolución vs el informe anterior si existe.\n"
                "- general_objectives: Una lista de EXACTAMENTE 3 objetivos generales de capacitación.\n"
                "- specific_objectives: Una lista de EXACTAMENTE 3 objetivos específicos asociados a criterios.\n"
                "- simulation_prompts: Una lista de EXACTAMENTE 4 prompts de voz interactivos para bots de roleplay de llamadas.\n\n"
                "NO devuelvas texto introductorio, formateo Markdown complementario, explicaciones ni etiquetas, solo el JSON puro."
            )

            # Convert aggregates to clean text block
            c_averages_str = "\n".join(
                f"- {data['name']} ({key}): {data['value']:.2f} ({'Puntuación 1-10' if data['type'] == 'numeric' else 'Porcentaje de Cumplimiento %'})"
                for key, data in aggregates["criteria_averages"].items()
            )

            feedbacks_str = "\n".join(
                f"- Criterio '{f['criterion']}' {f['score']}: \"{f['feedback']}\""
                for f in aggregates["critical_feedbacks"]
            )

            tipologia_str = ", ".join(f"{k} ({v} llamadas)" for k, v in aggregates["tipologia_distribution"].items())

            user_prompt = (
                f"### DATOS DE EVALUACIONES MASIVAS DEL AGENTE\n"
                f"Agente: {agent_name} ({agent_initials})\n"
                f"Periodo Analizado: {period_start.strftime('%Y-%m-%d')} al {period_end.strftime('%Y-%m-%d')}\n"
                f"Total de llamadas evaluadas: {aggregates['evaluations_count']}\n"
                f"Llamadas únicas: {aggregates['calls_count']}\n"
                f"Evaluación Global Media del agente: {aggregates['avg_evaluacion_global']:.2f}/10\n"
                f"Tasa de Cierre de Cita: {aggregates['cierre_cita_rate'] or 0.0:.2f}%\n"
                f"Tipologías de Llamadas:\n{tipologia_str or 'No disponible'}\n\n"
                f"### PROMEDIOS POR CRITERIO DE EVALUACIÓN:\n{c_averages_str}\n\n"
                f"### FEEDBACKS NEGATIVOS/ÁREAS DE MEJORA DETECTADAS EN LLAMADAS:\n{feedbacks_str or 'No hay feedbacks negativos registrados'}\n\n"
                f"### HISTÓRICO Y CONTEXTO PREVIO:\n{prev_context}\n\n"
                f"### REGLAS DE GENERACIÓN DE PROMPTS DE SIMULACIÓN DE ROLEPLAY:\n"
                f"Debes crear EXACTAMENTE 4 prompts interactivos de simulación de roleplay en español para entrenar los objetivos de mejora.\n"
                f"Cada uno de los 4 prompts de simulación de llamada debe ser un prompt completo para un BOT DE VOZ.\n"
                f"Sigue de forma rigurosa la estructura de Boston Medical en cada prompt:\n"
                f"1. IDENTIDAD DEL BOT: Eres un bot de voz interactivo de roleplay para Boston Medical, interpretas al paciente. Asigna un nombre al paciente (e.g. MIGUEL).\n"
                f"2. REGLAS CRÍTICAS: Mantener siempre el rol de paciente, consistencia de identidad (no cambiar nombre ni hechos), bloqueo de cambio de rol (nunca hablar como Boston Medical).\n"
                f"3. REGLAS DE VOZ: Tono real de paciente molesto o inseguro, respuestas breves (1-2 frases cortas), fillers permitidos, pronunciar emails arrobapunto.\n"
                f"4. CONTEXTO PARA EL AGENTE: Explicar qué caso debe gestionar el agente (e.g., precio de consulta, objeciones del tratamiento, acompañamiento de pareja, retraso de medicación).\n"
                f"5. LIMITACIONES OPERATIVAS: El agente no puede confirmar logística ni prometer reembolsos sin procedimiento.\n"
                f"6. INICIO DEL ROLEPLAY: Primera frase exacta del bot como paciente (e.g., 'Mira, llamo porque...').\n"
                f"7. ACTITUD EMOCIONAL Y DIFICULTAD: anger_level inicial del 1 al 6 (e.g., empieza en 4 o 5). Pautas de evolución permitida si el agente valida emocionalmente, no inventa plazos, y no insiste comercialmente.\n"
                f"8. OBJECIONES TÍPICAS Y CIERRE OBLIGATORIO: Frase de cierre natural según desempeño del agente, seguido EXACTAMENTE de la frase: 'La prueba ha terminado. Gracias por participar.'\n\n"
                f"Genera los 4 prompts específicos para entrenar las debilidades reales del agente ({agent_name}) mostradas en sus promedios de criterios y feedbacks."
            )

            # 5. Call OpenAI
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            logger.info("Calling Azure OpenAI complete_text to generate personalized training report for %s", hubspot_owner_id)
            ai_response_raw = await complete_text(
                messages=messages,
                temperature=0.3,
                response_format="json_object"
            )

            # 6. Parse response
            ai_data = json.loads(ai_response_raw)

            # Enforce exactly 3 general, 3 specific, 4 simulation prompts
            general_objectives = ai_data.get("general_objectives", [])[:3]
            specific_objectives = ai_data.get("specific_objectives", [])[:3]
            simulation_prompts = ai_data.get("simulation_prompts", [])[:4]

            # Write values to our SQLAlchemy model
            new_report.status = "completed"
            new_report.evaluations_count = aggregates["evaluations_count"]
            new_report.calls_count = aggregates["calls_count"]
            new_report.avg_evaluacion_global = Decimal(str(aggregates["avg_evaluacion_global"] or 0.0)).quantize(Decimal("0.01"))
            new_report.summary_general = ai_data.get("summary_general")
            new_report.strengths_json = ai_data.get("strengths")
            new_report.weaknesses_json = ai_data.get("weaknesses")
            new_report.notable_data_json = ai_data.get("notable_data")
            new_report.evolution_summary = ai_data.get("evolution_summary")
            new_report.general_objectives_json = general_objectives
            new_report.specific_objectives_json = specific_objectives
            new_report.generated_at = datetime.now(timezone.utc)

            # 7. Add simulation prompts & completion status records
            for idx, p in enumerate(simulation_prompts):
                new_prompt = TrainingSimulationPrompt(
                    training_report_id=new_report.training_report_id,
                    hubspot_owner_id=hubspot_owner_id,
                    prompt_number=p.get("prompt_number", idx + 1),
                    title=p.get("title", f"Escenario de Simulación {idx + 1}"),
                    scenario_type=p.get("scenario_type", "General"),
                    objective_focus_json=p.get("objective_focus", []),
                    prompt_text=p.get("prompt_text", "")
                )
                db.add(new_prompt)
                await db.flush()

                # Create pending completion status
                comp_status = TrainingCompletionStatus(
                    training_report_id=new_report.training_report_id,
                    simulation_prompt_id=new_prompt.simulation_prompt_id,
                    hubspot_owner_id=hubspot_owner_id,
                    status="pending"
                )
                db.add(comp_status)

            # 8. Deactivate previous reports for this agent
            if existing_report:
                existing_report.is_current = False
                existing_report.superseded_by_report_id = new_report.training_report_id

            stmt_deact = select(TrainingAgentReport).where(
                and_(
                    TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
                    TrainingAgentReport.training_report_id != new_report.training_report_id,
                    TrainingAgentReport.is_current == True
                )
            )
            res_deact = await db.execute(stmt_deact)
            deacts = res_deact.scalars().all()
            for d_rep in deacts:
                d_rep.is_current = False
                d_rep.superseded_by_report_id = new_report.training_report_id

            await db.commit()
            logger.info("Report ID %d successfully completed for agent %s.", new_report.training_report_id, hubspot_owner_id)
            return new_report

        except Exception as ex:
            logger.exception("Failed to generate report for agent %s in period %s to %s.", hubspot_owner_id, period_start, period_end)
            new_report.status = "failed"
            new_report.error_message = str(ex)
            await db.commit()
            raise ex

    @staticmethod
    async def run_personalized_training_pass(
        db: AsyncSession,
        hubspot_owner_ids: Optional[List[str]] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        triggered_by: str = "manual",
        created_by_email: Optional[str] = None,
        force_regenerate: bool = False
    ) -> TrainingRun:
        """
        Executes a global run for a group of agents.
        Calculates automatically the past 2 weeks if no dates are provided.
        """
        # Calculate dates if missing
        if not period_end:
            # End of yesterday
            now = datetime.now(timezone.utc)
            period_end = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=timezone.utc) - timedelta(days=1)
        if not period_start:
            # 14 days before period_end
            period_start = period_end - timedelta(days=14) + timedelta(seconds=1)

        # Select target agents
        if hubspot_owner_ids:
            stmt_set = select(TrainingAgentSetting).where(
                and_(
                    TrainingAgentSetting.hubspot_owner_id.in_(hubspot_owner_ids),
                    TrainingAgentSetting.is_enabled == True
                )
            )
        else:
            stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.is_enabled == True)

        res_set = await db.execute(stmt_set)
        active_settings = res_set.scalars().all()

        # Create global Run record
        new_run = TrainingRun(
            period_start=period_start,
            period_end=period_end,
            status="running",
            triggered_by=triggered_by,
            created_by_email=created_by_email,
            started_at=datetime.now(timezone.utc),
            agents_total=len(active_settings)
        )
        db.add(new_run)
        await db.commit()
        await db.refresh(new_run)

        completed = 0
        skipped = 0
        failed = 0

        for s in active_settings:
            try:
                rep = await PersonalizedTrainingService.generate_report_for_agent(
                    db=db,
                    hubspot_owner_id=s.hubspot_owner_id,
                    period_start=period_start,
                    period_end=period_end,
                    run_id=new_run.training_run_id,
                    force_regenerate=force_regenerate
                )
                if rep.status == "completed":
                    completed += 1
                elif rep.status == "skipped":
                    skipped += 1
                else:
                    failed += 1
            except Exception as e_agent:
                logger.error("Failed agent %s in run ID %d: %s", s.hubspot_owner_id, new_run.training_run_id, e_agent)
                failed += 1

        # Finalize run
        new_run.status = "completed" if failed == 0 else ("partially_completed" if completed > 0 else "failed")
        new_run.agents_completed = completed
        new_run.agents_skipped = skipped
        new_run.agents_failed = failed
        new_run.finished_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(new_run)
        return new_run

    @staticmethod
    async def run_due_training_jobs(db: AsyncSession) -> dict:
        """
        Checks if a new personalized training run is due based on settings.training_interval_days.
        Runs every hour, and triggers a new run if due.
        """
        from app.config import get_settings
        settings = get_settings()
        
        # 1. Fetch latest completed run
        stmt = select(TrainingRun).where(
            TrainingRun.status.in_(["completed", "partially_completed"])
        ).order_by(desc(TrainingRun.training_run_id)).limit(1)
        
        res = await db.execute(stmt)
        last_run = res.scalars().first()
        
        now = datetime.now(timezone.utc)
        due = False
        reason = ""
        
        if not last_run:
            due = True
            reason = "No previous training runs exist."
        else:
            ref_time = last_run.finished_at or last_run.created_at
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
                
            elapsed = now - ref_time
            limit = timedelta(days=settings.training_interval_days)
            
            if elapsed >= limit:
                due = True
                reason = f"Last run finished {elapsed.days} days ago (limit is {settings.training_interval_days} days)."
            else:
                reason = f"Last run was {elapsed.days} days ago (limit is {settings.training_interval_days} days). Next run due in {settings.training_interval_days - elapsed.days} days."

        if due:
            logger.info("Training scheduler: A new personalized training run is DUE. Reason: %s", reason)
            # Run the pass automatically
            run = await PersonalizedTrainingService.run_personalized_training_pass(
                db=db,
                triggered_by="scheduler"
            )
            return {"triggered": True, "run_id": run.training_run_id, "reason": reason}
        
        logger.info("Training scheduler: No training run due. Status: %s", reason)
        return {"triggered": False, "reason": reason}

