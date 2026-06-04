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
    TrainingSchedulerSetting,
)
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.models.users import User
from app.services.openai_service import complete_text
from app.utils.json_utils import safe_parse_json

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
    async def get_or_create_scheduler_settings(db: AsyncSession) -> TrainingSchedulerSetting:
        """Fetch the single row of scheduler settings, or create it if not present."""
        stmt = select(TrainingSchedulerSetting).limit(1)
        res = await db.execute(stmt)
        settings = res.scalars().first()
        
        if not settings:
            settings = TrainingSchedulerSetting(
                is_enabled=True,
                interval_days=14,
                lookback_days=14
            )
            db.add(settings)
            await db.commit()
            await db.refresh(settings)
            
        return settings

    @staticmethod
    async def update_scheduler_settings(
        db: AsyncSession,
        is_enabled: Optional[bool] = None,
        interval_days: Optional[int] = None,
        lookback_days: Optional[int] = None,
        updated_by_email: Optional[str] = None
    ) -> TrainingSchedulerSetting:
        """Update persistent scheduler settings and recompute next_run_at."""
        settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
        
        if is_enabled is not None:
            settings.is_enabled = is_enabled
        if interval_days is not None:
            settings.interval_days = interval_days
        if lookback_days is not None:
            settings.lookback_days = lookback_days
        if updated_by_email is not None:
            settings.updated_by_email = updated_by_email
            
        # Recompute next_run_at
        now = datetime.now(timezone.utc)
        ref = settings.last_run_at or now
        settings.next_run_at = ref + timedelta(days=settings.interval_days)
        
        await db.commit()
        await db.refresh(settings)
        return settings

    @staticmethod
    async def get_agent_overview(db: AsyncSession) -> List[dict]:
        """Returns the overview list of all agents for the admin tracking dashboard."""
        settings = await PersonalizedTrainingService.get_agent_settings(db)
        overview = []

        for s in settings:
            # Fetch all reports for this agent
            stmt_reps = select(TrainingAgentReport).where(
                TrainingAgentReport.hubspot_owner_id == s.hubspot_owner_id
            ).order_by(desc(TrainingAgentReport.training_report_id))
            res_reps = await db.execute(stmt_reps)
            all_agent_reps = list(res_reps.scalars().all())

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
            prev_count = len(all_agent_reps)

            # Compute cycle aggregates
            pending_cycles = 0
            pending_simulations = 0
            active_cycles = 0
            completed_cycles = 0
            
            for r in all_agent_reps:
                if r.status == "completed":
                    # Check how many simulations are completed for this report
                    stmt_sim_c = select(func.count(TrainingCompletionStatus.completion_id)).where(
                        and_(
                            TrainingCompletionStatus.training_report_id == r.training_report_id,
                            TrainingCompletionStatus.status == "completed"
                        )
                    )
                    res_sim_c = await db.execute(stmt_sim_c)
                    sim_comp_count = res_sim_c.scalar() or 0
                    
                    if sim_comp_count == 4:
                        completed_cycles += 1
                    else:
                        pending_cycles += 1
                        pending_simulations += (4 - sim_comp_count)
                        if sim_comp_count > 0:
                            active_cycles += 1

            # Latest cycle info
            latest_r = all_agent_reps[0] if all_agent_reps else None
            latest_cycle_status = "no_data"
            latest_cycle_progress_completed = 0
            latest_cycle_progress_total = 4
            latest_cycle_period_start = None
            latest_cycle_period_end = None
            latest_cycle_avg_score = None
            
            if latest_r:
                latest_cycle_period_start = latest_r.period_start
                latest_cycle_period_end = latest_r.period_end
                latest_cycle_avg_score = latest_r.avg_evaluacion_global
                
                if latest_r.status == "failed":
                    latest_cycle_status = "failed"
                elif latest_r.status == "skipped":
                    latest_cycle_status = "skipped"
                elif latest_r.status == "completed":
                    stmt_latest_comp = select(func.count(TrainingCompletionStatus.completion_id)).where(
                        and_(
                            TrainingCompletionStatus.training_report_id == latest_r.training_report_id,
                            TrainingCompletionStatus.status == "completed"
                        )
                    )
                    res_latest_comp = await db.execute(stmt_latest_comp)
                    latest_comp_count = res_latest_comp.scalar() or 0
                    
                    latest_cycle_progress_completed = latest_comp_count
                    if latest_comp_count == 0:
                        latest_cycle_status = "pending"
                    elif latest_comp_count == 4:
                        latest_cycle_status = "completed"
                    else:
                        latest_cycle_status = "running"

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
                "previous_reports_count": prev_count,
                "error_message": None,
                # New fields
                "pending_cycles_count": pending_cycles,
                "pending_simulations_count": pending_simulations,
                "active_cycles_count": active_cycles,
                "completed_cycles_count": completed_cycles,
                "latest_cycle_status": latest_cycle_status,
                "latest_cycle_progress_completed": latest_cycle_progress_completed,
                "latest_cycle_progress_total": latest_cycle_progress_total,
                "latest_cycle_period_start": latest_cycle_period_start,
                "latest_cycle_period_end": latest_cycle_period_end,
                "latest_cycle_avg_score": latest_cycle_avg_score
            }

            if report:
                item["current_report_id"] = report.training_report_id
                item["current_period_start"] = report.period_start
                item["current_period_end"] = report.period_end
                item["status"] = report.status
                item["evaluations_count"] = report.evaluations_count
                item["summary_general"] = report.summary_general[:100] + "..." if report.summary_general and len(report.summary_general) > 100 else report.summary_general
                item["last_generated_at"] = report.generated_at or report.created_at
                item["error_message"] = report.error_message

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
    def _map_setting_to_dict(s: TrainingAgentSetting) -> Optional[dict]:
        if not s:
            return None
        return {
            "setting_id": s.setting_id,
            "hubspot_owner_id": s.hubspot_owner_id,
            "agent_name": s.agent_name,
            "agent_initials": s.agent_initials,
            "is_enabled": s.is_enabled,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }

    @staticmethod
    def _map_prompt_to_dict(p: TrainingSimulationPrompt) -> Optional[dict]:
        if not p:
            return None
        
        focus_data = p.objective_focus_json
        focus_list = []
        linked_gen = []
        linked_spec = []
        obj_summary = None
        exp_behavior = None
        
        if isinstance(focus_data, dict):
            focus_list = focus_data.get("focus") or []
            linked_gen = focus_data.get("linked_general_objectives") or []
            linked_spec = focus_data.get("linked_specific_objectives") or []
            obj_summary = focus_data.get("objective_summary")
            exp_behavior = focus_data.get("expected_behavior")
        elif isinstance(focus_data, list):
            focus_list = focus_data
        elif focus_data is not None:
            focus_list = [str(focus_data)]
            
        if not obj_summary:
            obj_summary = f"Reforzar habilidades críticas asociadas a la simulación {p.prompt_number}."
        if not exp_behavior:
            exp_behavior = "Aplicar correctamente los criterios evaluativos del protocolo Boston Medical."
            
        return {
            "simulation_prompt_id": p.simulation_prompt_id,
            "training_report_id": p.training_report_id,
            "hubspot_owner_id": p.hubspot_owner_id,
            "prompt_number": p.prompt_number,
            "title": p.title or f"Simulación de entrenamiento {p.prompt_number}",
            "scenario_type": p.scenario_type or "roleplay",
            "objective_focus_json": [str(x) for x in focus_list],
            "linked_general_objectives": [str(x) for x in linked_gen],
            "linked_specific_objectives": [str(x) for x in linked_spec],
            "objective_summary": obj_summary,
            "expected_behavior": exp_behavior,
            "prompt_text": p.prompt_text or "",
            "created_at": p.created_at,
        }

    @staticmethod
    def _map_completion_to_dict(c: TrainingCompletionStatus) -> Optional[dict]:
        if not c:
            return None
        return {
            "completion_id": c.completion_id,
            "training_report_id": c.training_report_id,
            "simulation_prompt_id": c.simulation_prompt_id,
            "hubspot_owner_id": c.hubspot_owner_id,
            "status": c.status or "pending",
            "completed_at": c.completed_at,
            "training_call_id": c.training_call_id,
            "training_phone_number": c.training_phone_number,
            "notes": c.notes,
            "created_at": c.created_at,
        }

    @staticmethod
    def _map_report_to_dict(r: TrainingAgentReport, prompts: list = None, completions: list = None) -> Optional[dict]:
        if not r:
            return None
            
        avg_score = None
        if r.avg_evaluacion_global is not None:
            try:
                avg_score = Decimal(str(r.avg_evaluacion_global))
            except Exception:
                pass
                
        # Defensive JSONB lists normalizations
        strengths = r.strengths_json
        normalized_strengths = []
        if isinstance(strengths, list):
            for item in strengths:
                if isinstance(item, dict):
                    normalized_strengths.append({
                        "title": str(item.get("title") or item.get("titulo") or "Punto Fuerte"),
                        "description": str(item.get("description") or item.get("descripcion") or ""),
                        "evidence": str(item.get("evidence") or item.get("evidencia") or "")
                    })
                else:
                    normalized_strengths.append({
                        "title": "Punto Fuerte",
                        "description": str(item),
                        "evidence": ""
                    })
        
        weaknesses = r.weaknesses_json
        normalized_weaknesses = []
        if isinstance(weaknesses, list):
            for item in weaknesses:
                if isinstance(item, dict):
                    normalized_weaknesses.append({
                        "title": str(item.get("title") or item.get("titulo") or "Área de Mejora"),
                        "description": str(item.get("description") or item.get("descripcion") or ""),
                        "evidence": str(item.get("evidence") or item.get("evidencia") or "")
                    })
                else:
                    normalized_weaknesses.append({
                        "title": "Área de Mejora",
                        "description": str(item),
                        "evidence": ""
                    })

        notable = r.notable_data_json
        normalized_notable = []
        if isinstance(notable, list):
            for item in notable:
                if isinstance(item, dict):
                    normalized_notable.append({
                        "title": str(item.get("title") or item.get("titulo") or "Dato Notable"),
                        "description": str(item.get("description") or item.get("descripcion") or ""),
                        "metric_or_pattern": str(item.get("metric_or_pattern") or item.get("metrica") or item.get("patron") or "")
                    })
                else:
                    normalized_notable.append({
                        "title": "Dato Notable",
                        "description": str(item),
                        "metric_or_pattern": ""
                    })

        gen_objectives = r.general_objectives_json
        normalized_gen = []
        if isinstance(gen_objectives, list):
            for item in gen_objectives:
                if isinstance(item, dict):
                    indicators = item.get("success_indicators") or item.get("indicadores_exito") or []
                    if not isinstance(indicators, list):
                        indicators = [str(indicators)]
                    normalized_gen.append({
                        "title": str(item.get("title") or item.get("titulo") or "Objetivo General"),
                        "description": str(item.get("description") or item.get("descripcion") or ""),
                        "rationale": str(item.get("rationale") or item.get("justificacion") or ""),
                        "expected_behavior": str(item.get("expected_behavior") or item.get("comportamiento_esperado") or ""),
                        "success_indicators": [str(x) for x in indicators]
                    })

        spec_objectives = r.specific_objectives_json
        normalized_spec = []
        if isinstance(spec_objectives, list):
            for item in spec_objectives:
                if isinstance(item, dict):
                    criteria = item.get("related_criteria") or item.get("criterios_relacionados") or []
                    if not isinstance(criteria, list):
                        criteria = [str(criteria)]
                    indicators = item.get("success_indicators") or item.get("indicadores_exito") or []
                    if not isinstance(indicators, list):
                        indicators = [str(indicators)]
                    normalized_spec.append({
                        "title": str(item.get("title") or item.get("titulo") or "Objetivo Específico"),
                        "description": str(item.get("description") or item.get("descripcion") or ""),
                        "related_criteria": [str(x) for x in criteria],
                        "specific_behavior_to_improve": str(item.get("specific_behavior_to_improve") or item.get("comportamiento_especifico") or ""),
                        "success_indicators": [str(x) for x in indicators]
                    })
            
        # Calculate progress completion metrics
        progress_completed = 0
        progress_percentage = Decimal("0.0")
        if completions:
            completed_count = sum(1 for c in completions if (c.status == "completed" if hasattr(c, "status") else c.get("status") == "completed"))
            progress_completed = completed_count
            progress_percentage = Decimal(str(completed_count / 4.0 * 100.0)).quantize(Decimal("0.01"))

        mapped = {
            "training_report_id": r.training_report_id,
            "training_run_id": r.training_run_id,
            "hubspot_owner_id": r.hubspot_owner_id,
            "agent_name": r.agent_name,
            "agent_initials": r.agent_initials,
            "period_start": r.period_start,
            "period_end": r.period_end,
            "status": r.status or "pending",
            "skipped_reason": r.skipped_reason,
            "evaluations_count": r.evaluations_count or 0,
            "calls_count": r.calls_count or 0,
            "avg_evaluacion_global": avg_score,
            "summary_general": r.summary_general,
            "strengths_json": normalized_strengths,
            "weaknesses_json": normalized_weaknesses,
            "notable_data_json": normalized_notable,
            "evolution_summary": r.evolution_summary,
            "general_objectives_json": normalized_gen,
            "specific_objectives_json": normalized_spec,
            "is_current": r.is_current,
            "created_at": r.created_at,
            "generated_at": r.generated_at,
            "error_message": r.error_message,
            "progress_completed": progress_completed,
            "progress_total": 4,
            "progress_percentage": progress_percentage
        }
        
        if prompts is not None:
            mapped["prompts"] = [PersonalizedTrainingService._map_prompt_to_dict(p) for p in prompts]
        else:
            mapped["prompts"] = []
            
        if completions is not None:
            mapped["completion_statuses"] = [PersonalizedTrainingService._map_completion_to_dict(c) for c in completions]
        else:
            mapped["completion_statuses"] = []
            
        return mapped

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

            current_report_data = PersonalizedTrainingService._map_report_to_dict(report, prompts, completions)

        # Fetch historical reports (excluding current report or just listing all)
        stmt_hist = select(TrainingAgentReport).where(
            TrainingAgentReport.hubspot_owner_id == hubspot_owner_id
        ).order_by(desc(TrainingAgentReport.period_start))
        res_hist = await db.execute(stmt_hist)
        history = list(res_hist.scalars().all())

        mapped_history = []
        for h in history:
            stmt_comp_h = select(TrainingCompletionStatus).where(
                TrainingCompletionStatus.training_report_id == h.training_report_id
            ).order_by(TrainingCompletionStatus.completion_id.asc())
            res_comp_h = await db.execute(stmt_comp_h)
            completions_h = list(res_comp_h.scalars().all())
            
            mapped_h = PersonalizedTrainingService._map_report_to_dict(h, completions=completions_h)
            mapped_history.append(mapped_h)

        return {
            "agent_setting": PersonalizedTrainingService._map_setting_to_dict(setting),
            "current_report": current_report_data,
            "progress_completed": progress_completed,
            "progress_total": 4,
            "progress_percentage": progress_percentage,
            "history": mapped_history,
            "evolution_summary": report.evolution_summary if report else None
        }

    @staticmethod
    async def get_cycles_team_summary(db: AsyncSession) -> dict:
        """
        Computes team-wide training metrics for administrators.
        """
        # 1. Fetch all active/enabled agents
        settings_stmt = select(TrainingAgentSetting).where(TrainingAgentSetting.is_enabled == True)
        res_settings = await db.execute(settings_stmt)
        active_settings = list(res_settings.scalars().all())
        active_agents_count = len(active_settings)

        team_scores = []
        team_prev_scores = []
        team_close_rates = []
        
        total_pending_cycles = 0
        total_pending_simulations = 0
        
        agents_requiring_attention = 0
        agents_improving = 0
        agents_stagnant = 0
        agents_declining = 0
        
        priority_agents = []

        for s in active_settings:
            # Get all reports for this agent ordered by period start desc
            stmt_reps = select(TrainingAgentReport).where(
                TrainingAgentReport.hubspot_owner_id == s.hubspot_owner_id
            ).order_by(desc(TrainingAgentReport.period_start))
            res_reps = await db.execute(stmt_reps)
            reps = list(res_reps.scalars().all())
            
            # Find the latest and second latest completed reports
            completed_reps = [r for r in reps if r.status == "completed"]
            latest_r = completed_reps[0] if completed_reps else None
            prev_r = completed_reps[1] if len(completed_reps) > 1 else None
            
            # Get simulation progress for this agent
            # We count pending simulations across all reports for this agent
            agent_pending_cycles = 0
            agent_pending_simulations = 0
            
            for r in reps:
                if r.status == "completed":
                    stmt_sim_c = select(func.count(TrainingCompletionStatus.completion_id)).where(
                        and_(
                            TrainingCompletionStatus.training_report_id == r.training_report_id,
                            TrainingCompletionStatus.status == "completed"
                        )
                    )
                    res_sim_c = await db.execute(stmt_sim_c)
                    sim_comp_count = res_sim_c.scalar() or 0
                    if sim_comp_count < 4:
                        agent_pending_cycles += 1
                        agent_pending_simulations += (4 - sim_comp_count)

            total_pending_cycles += agent_pending_cycles
            total_pending_simulations += agent_pending_simulations
            
            score = None
            score_delta = None
            
            if latest_r:
                if latest_r.avg_evaluacion_global is not None:
                    score = float(latest_r.avg_evaluacion_global)
                    team_scores.append(score)
                    
                    if prev_r and prev_r.avg_evaluacion_global is not None:
                        prev_score = float(prev_r.avg_evaluacion_global)
                        team_prev_scores.append(prev_score)
                        score_delta = round(score - prev_score, 2)
                        
                # Compute close rate for latest report period
                stmt_close = select(
                    func.count(MassEvaluationCriterionResult.id)
                ).join(
                    MassEvaluationResult, MassEvaluationResult.mass_analysis_id == MassEvaluationCriterionResult.mass_analysis_id
                ).where(
                    MassEvaluationResult.hubspot_owner_id == s.hubspot_owner_id,
                    MassEvaluationResult.call_timestamp >= latest_r.period_start,
                    MassEvaluationResult.call_timestamp <= latest_r.period_end,
                    MassEvaluationResult.status == "completed",
                    MassEvaluationCriterionResult.criterion_key == "cierre_cita",
                    MassEvaluationCriterionResult.is_applicable == True,
                    MassEvaluationCriterionResult.boolean_value == True
                )
                res_close = await db.execute(stmt_close)
                close_count = res_close.scalar() or 0
                
                stmt_total = select(
                    func.count(MassEvaluationCriterionResult.id)
                ).join(
                    MassEvaluationResult, MassEvaluationResult.mass_analysis_id == MassEvaluationCriterionResult.mass_analysis_id
                ).where(
                    MassEvaluationResult.hubspot_owner_id == s.hubspot_owner_id,
                    MassEvaluationResult.call_timestamp >= latest_r.period_start,
                    MassEvaluationResult.call_timestamp <= latest_r.period_end,
                    MassEvaluationResult.status == "completed",
                    MassEvaluationCriterionResult.criterion_key == "cierre_cita",
                    MassEvaluationCriterionResult.is_applicable == True,
                    MassEvaluationCriterionResult.boolean_value.is_not(None)
                )
                res_total = await db.execute(stmt_total)
                total_count = res_total.scalar() or 0
                
                if total_count > 0:
                    agent_close_rate = close_count / total_count
                    team_close_rates.append(agent_close_rate)
                else:
                    agent_close_rate = None
            else:
                agent_close_rate = None

            # Categorize agent
            agent_status = "stagnant"
            reason = "Rendimiento estable"
            
            if score is not None:
                if score < 6.5 or agent_pending_cycles > 0:
                    agent_status = "requires_attention"
                    agents_requiring_attention += 1
                    if score < 6.5 and agent_pending_cycles > 0:
                        reason = "Score bajo y ciclos pendientes"
                    elif score < 6.5:
                        reason = "Score bajo en el último ciclo"
                    else:
                        reason = "Ciclos pendientes acumulados"
                elif score_delta is not None:
                    if score_delta > 0.1:
                        agent_status = "improving"
                        agents_improving += 1
                        reason = "Progreso positivo en puntuaciones"
                    elif score_delta < -0.1:
                        agent_status = "declining"
                        agents_declining += 1
                        reason = "Rendimiento en declive"
                    else:
                        agents_stagnant += 1
                else:
                    agents_stagnant += 1
            else:
                agents_stagnant += 1

            if agent_status == "requires_attention" or (score_delta is not None and score_delta < 0):
                priority_agents.append({
                    "hubspot_owner_id": s.hubspot_owner_id,
                    "agent_initials": s.agent_initials,
                    "agent_name": s.agent_name,
                    "score": round(score, 2) if score is not None else None,
                    "score_delta": score_delta,
                    "pending_cycles": agent_pending_cycles,
                    "pending_simulations": agent_pending_simulations,
                    "status": agent_status,
                    "reason": reason
                })

        # Calculate averages
        team_avg_score = round(sum(team_scores) / len(team_scores), 2) if team_scores else 0.0
        
        # Calculate delta of team average
        if team_scores and team_prev_scores:
            latest_avg = sum(team_scores) / len(team_scores)
            prev_avg = sum(team_prev_scores) / len(team_prev_scores)
            team_avg_score_delta = round(latest_avg - prev_avg, 2)
        else:
            team_avg_score_delta = 0.0
            
        avg_close_rate = round(sum(team_close_rates) / len(team_close_rates), 2) if team_close_rates else 0.0

        # Sort priority agents
        priority_agents.sort(key=lambda x: (0 if x["status"] == "requires_attention" else 1, x["score"] or 10.0))

        # 2. Get recurring patterns/weaknesses from the latest cycle period
        stmt_patterns = select(
            MassEvaluationCriterionResult.criterion_name,
            func.count(MassEvaluationCriterionResult.criterion_key)
        ).join(
            MassEvaluationResult, MassEvaluationResult.mass_analysis_id == MassEvaluationCriterionResult.mass_analysis_id
        ).where(
            MassEvaluationResult.status == "completed",
            MassEvaluationCriterionResult.is_applicable == True,
            or_(
                and_(MassEvaluationCriterionResult.numeric_value.is_not(None), MassEvaluationCriterionResult.numeric_value < 8.0),
                and_(MassEvaluationCriterionResult.boolean_value.is_not(None), MassEvaluationCriterionResult.boolean_value == False)
            )
        ).group_by(
            MassEvaluationCriterionResult.criterion_name
        ).order_by(
            desc(func.count(MassEvaluationCriterionResult.criterion_key))
        ).limit(3)

        res_patterns = await db.execute(stmt_patterns)
        pattern_rows = res_patterns.all()
        
        recurring_patterns = []
        for row in pattern_rows:
            label, count = row
            severity = "high" if count >= 10 else "medium"
            recurring_patterns.append({
                "label": label,
                "count": count,
                "total_agents": active_agents_count,
                "severity": severity
            })

        if not recurring_patterns:
            recurring_patterns = [
                {
                    "label": "Tres preguntas clave ausente o tardía",
                    "count": 0,
                    "total_agents": active_agents_count,
                    "severity": "medium"
                }
            ]

        # 3. Cycle evolution (grouped by training runs)
        stmt_runs = select(TrainingRun).where(
            TrainingRun.status == "completed"
        ).order_by(desc(TrainingRun.created_at)).limit(5)
        res_runs = await db.execute(stmt_runs)
        runs = list(res_runs.scalars().all())
        runs.reverse()  # Chronological order

        cycle_evolution = []
        for idx, run in enumerate(runs):
            stmt_run_reps = select(TrainingAgentReport).where(
                TrainingAgentReport.training_run_id == run.training_run_id
            )
            res_run_reps = await db.execute(stmt_run_reps)
            run_reps = list(res_run_reps.scalars().all())
            
            run_scores = [float(r.avg_evaluacion_global) for r in run_reps if r.avg_evaluacion_global is not None]
            run_avg_score = round(sum(run_scores) / len(run_scores), 2) if run_scores else 0.0
            
            stmt_run_close = select(
                func.count(MassEvaluationCriterionResult.id)
            ).join(
                MassEvaluationResult, MassEvaluationResult.mass_analysis_id == MassEvaluationCriterionResult.mass_analysis_id
            ).where(
                MassEvaluationResult.call_timestamp >= run.period_start,
                MassEvaluationResult.call_timestamp <= run.period_end,
                MassEvaluationResult.status == "completed",
                MassEvaluationCriterionResult.criterion_key == "cierre_cita",
                MassEvaluationCriterionResult.is_applicable == True,
                MassEvaluationCriterionResult.boolean_value == True
            )
            res_run_close = await db.execute(stmt_run_close)
            run_close_count = res_run_close.scalar() or 0
            
            stmt_run_total = select(
                func.count(MassEvaluationCriterionResult.id)
            ).join(
                MassEvaluationResult, MassEvaluationResult.mass_analysis_id == MassEvaluationCriterionResult.mass_analysis_id
            ).where(
                MassEvaluationResult.call_timestamp >= run.period_start,
                MassEvaluationResult.call_timestamp <= run.period_end,
                MassEvaluationResult.status == "completed",
                MassEvaluationCriterionResult.criterion_key == "cierre_cita",
                MassEvaluationCriterionResult.is_applicable == True,
                MassEvaluationCriterionResult.boolean_value.is_not(None)
            )
            res_run_total = await db.execute(stmt_run_total)
            run_total_count = res_run_total.scalar() or 0
            
            run_close_rate = round(run_close_count / run_total_count, 2) if run_total_count > 0 else 0.0

            completed_cycles = 0
            pending_simulations = 0
            
            for r in run_reps:
                stmt_sim_c = select(func.count(TrainingCompletionStatus.completion_id)).where(
                    and_(
                        TrainingCompletionStatus.training_report_id == r.training_report_id,
                        TrainingCompletionStatus.status == "completed"
                    )
                )
                res_sim_c = await db.execute(stmt_sim_c)
                sim_comp_count = res_sim_c.scalar() or 0
                if sim_comp_count == 4:
                    completed_cycles += 1
                else:
                    pending_simulations += (4 - sim_comp_count)

            cycle_evolution.append({
                "cycle_label": f"Ciclo {idx + 1}",
                "team_avg_score": run_avg_score,
                "close_rate": run_close_rate,
                "completed_cycles": completed_cycles,
                "pending_simulations": pending_simulations
            })

        if not cycle_evolution:
            cycle_evolution = [
                {
                    "cycle_label": "Ciclo 1",
                    "team_avg_score": team_avg_score,
                    "close_rate": avg_close_rate,
                    "completed_cycles": len([s for s in active_settings if total_pending_cycles == 0]),
                    "pending_simulations": total_pending_simulations
                }
            ]

        return {
            "active_agents": active_agents_count,
            "team_avg_score": team_avg_score,
            "team_avg_score_delta": team_avg_score_delta,
            "avg_close_rate": avg_close_rate,
            "agents_requiring_attention": agents_requiring_attention,
            "agents_improving": agents_improving,
            "agents_stagnant": agents_stagnant,
            "agents_declining": agents_declining,
            "pending_cycles": total_pending_cycles,
            "pending_simulations": total_pending_simulations,
            "priority_agents": priority_agents,
            "recurring_patterns": recurring_patterns,
            "cycle_evolution": cycle_evolution
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

        return PersonalizedTrainingService._map_report_to_dict(report, prompts, completions)

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
        from app.services.dashboard_service import extract_score_from_mass
        global_scores = []
        for r in results:
            score = extract_score_from_mass(r.result_json, r.items_json, "evaluacion_global")
            if score is not None:
                global_scores.append(score)

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
        logger.info("[training] start generate agent")
        
        # Fetch agent settings
        stmt_set = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == hubspot_owner_id)
        res_set = await db.execute(stmt_set)
        agent_setting = res_set.scalars().first()

        if not agent_setting:
            raise ValueError(f"No agent settings found for HubSpot Owner ID {hubspot_owner_id}")

        agent_name = agent_setting.agent_name
        agent_initials = agent_setting.agent_initials
        logger.info(f"[training] agent={agent_initials} owner_id={hubspot_owner_id}")
        logger.info(f"[training] period_start={period_start.isoformat()}")
        logger.info(f"[training] period_end={period_end.isoformat()}")

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
        existing_report_id = existing_report.training_report_id if existing_report else None

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
            logger.info("Phase: aggregate evaluations")
            aggregates = await PersonalizedTrainingService.aggregate_agent_evaluations(db, hubspot_owner_id, period_start, period_end)
            
            logger.info(f"[training] evaluations_count={aggregates['evaluations_count']}")
            logger.info(f"[training] calls_count={aggregates['calls_count']}")
            
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
            logger.info("[training] build_ai_payload")
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
                "- simulation_prompts: Una lista de EXACTAMENTE 4 prompts de voz interactivos para bots de roleplay de llamadas. Cada prompt de simulación debe ser un objeto conteniendo:\n"
                "    * prompt_number: número entero (1, 2, 3, 4)\n"
                "    * title: título de la simulación\n"
                "    * scenario_type: tipo de escenario (generalmente 'roleplay')\n"
                "    * prompt_text: el prompt de voz detallado del bot\n"
                "    * objective_focus: lista de enfoques específicos (e.g. ['explicacion_precio'])\n"
                "    * linked_general_objectives: lista de títulos de objetivos generales vinculados a esta simulación\n"
                "    * linked_specific_objectives: lista de títulos de objetivos específicos vinculados a esta simulación\n"
                "    * objective_summary: explicación breve del objetivo de la simulación\n"
                "    * expected_behavior: conducta esperada del agente en la simulación\n\n"
                "NO devuelvas texto introductorio, formateo Markdown complementario, explicaciones ni etiquetas, solo el JSON puro."
            )

            # Convert aggregates to clean text block safely
            c_averages_lines = []
            for key, data in aggregates["criteria_averages"].items():
                val = data.get("value") or 0.0
                c_averages_lines.append(f"- {data['name']} ({key}): {val:.2f} ({'Puntuación 1-10' if data['type'] == 'numeric' else 'Porcentaje de Cumplimiento %'})")
            c_averages_str = "\n".join(c_averages_lines)

            feedbacks_str = "\n".join(
                f"- Criterio '{f['criterion']}' {f['score']}: \"{f['feedback']}\""
                for f in aggregates["critical_feedbacks"]
            )

            tipologia_str = ", ".join(f"{k} ({v} llamadas)" for k, v in aggregates["tipologia_distribution"].items())

            avg_val_global = aggregates.get("avg_evaluacion_global")
            cierre_cita_rate = aggregates.get("cierre_cita_rate")

            avg_val_global_str = f"{avg_val_global:.2f}/10" if avg_val_global is not None else "No disponible"
            cierre_cita_rate_str = f"{cierre_cita_rate:.2f}%" if cierre_cita_rate is not None else "No disponible"

            user_prompt = (
                f"### DATOS DE EVALUACIONES MASIVAS DEL AGENTE\n"
                f"Agente: {agent_name} ({agent_initials})\n"
                f"Periodo Analizado: {period_start.strftime('%Y-%m-%d')} al {period_end.strftime('%Y-%m-%d')}\n"
                f"Total de llamadas evaluadas: {aggregates['evaluations_count']}\n"
                f"Llamadas únicas: {aggregates['calls_count']}\n"
                f"Evaluación Global Media del agente: {avg_val_global_str}\n"
                f"Tasa de Cierre de Cita: {cierre_cita_rate_str}\n"
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

            # 5. Call OpenAI and validate
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            ai_data = None
            ai_response_raw = ""
            retry_count = 0
            max_retries = 1

            while retry_count <= max_retries:
                logger.info("[training] call_openai_start")
                import time
                t_openai_start = time.perf_counter()
                ai_response_raw = await complete_text(
                    messages=messages,
                    temperature=0.3,
                    response_format="json_object"
                )
                duration_openai = time.perf_counter() - t_openai_start
                logger.info(f"[training] call_openai_done duration={duration_openai:.2f}s")
                logger.info(f"[training] raw_ai_response_length={len(ai_response_raw)}")

                logger.info("[training] parse_json_start")
                ai_data = safe_parse_json(ai_response_raw)

                if ai_data is None:
                    logger.info("[training] parse_json_failed")
                    logger.error("Failed to parse JSON. Raw response:\n%s", ai_response_raw)
                    if retry_count < max_retries:
                        retry_count += 1
                        logger.warning("Retrying OpenAI call because JSON parsing failed.")
                        messages.append({"role": "assistant", "content": ai_response_raw})
                        messages.append({"role": "user", "content": "Error: La respuesta no es un JSON válido. Devuelve estrictamente el objeto JSON sin envoltorios markdown."})
                        continue
                    else:
                        raise ValueError(f"AI response is not valid JSON. Response length: {len(ai_response_raw)}")

                logger.info("[training] parse_json_ok")
                logger.info("[training] validation_start")

                # Normalize keys defensively
                gen_objs = None
                for k in ["general_objectives", "objetivos_generales", "general_objectives_json", "objetivos", "general_goals"]:
                    if k in ai_data:
                        gen_objs = ai_data[k]
                        break
                if gen_objs is None:
                    gen_objs = []

                spec_objs = None
                for k in ["specific_objectives", "objetivos_especificos", "specific_objectives_json", "objetivos_especificos_json", "specific_goals"]:
                    if k in ai_data:
                        spec_objs = ai_data[k]
                        break
                if spec_objs is None:
                    spec_objs = []

                sim_prompts = None
                for k in ["simulation_prompts", "prompts_simulacion", "simulations", "roleplay_prompts", "prompts", "training_prompts"]:
                    if k in ai_data:
                        sim_prompts = ai_data[k]
                        break
                if sim_prompts is None:
                    sim_prompts = []

                # Defensive pad/pruning for general_objectives to ensure exactly 3 items
                normalized_gen = []
                if isinstance(gen_objs, list):
                    for item in gen_objs:
                        if isinstance(item, dict):
                            indicators = item.get("success_indicators") or item.get("indicadores_exito") or []
                            if not isinstance(indicators, list):
                                indicators = [str(indicators)]
                            normalized_gen.append({
                                "title": str(item.get("title") or item.get("titulo") or "Objetivo General"),
                                "description": str(item.get("description") or item.get("descripcion") or ""),
                                "rationale": str(item.get("rationale") or item.get("justificacion") or ""),
                                "expected_behavior": str(item.get("expected_behavior") or item.get("comportamiento_esperado") or ""),
                                "success_indicators": [str(x) for x in indicators]
                            })
                        else:
                            normalized_gen.append({
                                "title": "Objetivo General",
                                "description": str(item),
                                "rationale": "",
                                "expected_behavior": "",
                                "success_indicators": []
                            })
                while len(normalized_gen) < 3:
                    normalized_gen.append({
                        "title": f"Objetivo General de Refuerzo {len(normalized_gen) + 1}",
                        "description": "Reforzar el protocolo Boston Medical en el trato de pacientes.",
                        "rationale": "Mantener altos estándares de calidad clínica y comercial.",
                        "expected_behavior": "Seguir la estructura del protocolo en cada llamada.",
                        "success_indicators": ["Cumplimiento general de criterios"]
                    })
                gen_objs = normalized_gen[:3]

                # Defensive pad/pruning for specific_objectives to ensure exactly 3 items
                normalized_spec = []
                if isinstance(spec_objs, list):
                    for item in spec_objs:
                        if isinstance(item, dict):
                            criteria = item.get("related_criteria") or item.get("criterios_relacionados") or []
                            if not isinstance(criteria, list):
                                criteria = [str(criteria)]
                            indicators = item.get("success_indicators") or item.get("indicadores_exito") or []
                            if not isinstance(indicators, list):
                                indicators = [str(indicators)]
                            normalized_spec.append({
                                "title": str(item.get("title") or item.get("titulo") or "Objetivo Específico"),
                                "description": str(item.get("description") or item.get("descripcion") or ""),
                                "related_criteria": [str(x) for x in criteria],
                                "specific_behavior_to_improve": str(item.get("specific_behavior_to_improve") or item.get("comportamiento_especifico") or ""),
                                "success_indicators": [str(x) for x in indicators]
                            })
                        else:
                            normalized_spec.append({
                                "title": "Objetivo Específico",
                                "description": str(item),
                                "related_criteria": [],
                                "specific_behavior_to_improve": "",
                                "success_indicators": []
                            })
                while len(normalized_spec) < 3:
                    normalized_spec.append({
                        "title": f"Objetivo Específico de Apoyo {len(normalized_spec) + 1}",
                        "description": "Mejorar la adherencia a criterios específicos de llamada.",
                        "related_criteria": ["protocolo_general"],
                        "specific_behavior_to_improve": "Aplicar escucha activa y empatía.",
                        "success_indicators": ["Mejora de la puntuación en evaluaciones futuras"]
                    })
                spec_objs = normalized_spec[:3]

                summary_general = ai_data.get("summary_general") or ai_data.get("resumen_general") or ai_data.get("summary")
                strengths = ai_data.get("strengths") or ai_data.get("puntos_fuertes") or ai_data.get("fortalezas") or []
                weaknesses = ai_data.get("weaknesses") or ai_data.get("puntos_debiles") or ai_data.get("debilidades") or []
                notable_data = ai_data.get("notable_data") or ai_data.get("datos_notables") or ai_data.get("hallazgos") or []
                evolution_summary = ai_data.get("evolution_summary") or ai_data.get("resumen_evolucion") or ai_data.get("evolucion")

                logger.info(f"[training] general_objectives_count={len(gen_objs)}")
                logger.info(f"[training] specific_objectives_count={len(spec_objs)}")
                logger.info(f"[training] simulation_prompts_count={len(sim_prompts)}")

                validation_errors = []
                if not summary_general:
                    validation_errors.append("Falta el campo 'summary_general'.")

                if not isinstance(sim_prompts, list):
                    validation_errors.append(f"'simulation_prompts' debe ser una lista, se obtuvo {type(sim_prompts).__name__}")
                elif len(sim_prompts) != 4:
                    validation_errors.append(f"Se esperaban exactamente 4 prompts de simulación en 'simulation_prompts', se obtuvieron {len(sim_prompts)}")

                if validation_errors:
                    err_msg = " | ".join(validation_errors)
                    logger.warning("AI output validation failed: %s. Raw Response:\n%s", err_msg, ai_response_raw)
                    if retry_count < max_retries:
                        retry_count += 1
                        logger.warning("Retrying OpenAI call because validation failed.")
                        messages.append({"role": "assistant", "content": ai_response_raw})
                        messages.append({"role": "user", "content": f"Error de validación: {err_msg}. Corrige el formato para cumplir exactamente las cantidades (4 simulation_prompts)."})
                        continue
                    else:
                        raise ValueError(f"AI output validation failed: {err_msg}")
                else:
                    # Successfully parsed and validated!
                    ai_data["general_objectives"] = gen_objs
                    ai_data["specific_objectives"] = spec_objs
                    ai_data["simulation_prompts"] = sim_prompts
                    ai_data["summary_general"] = summary_general
                    ai_data["strengths"] = strengths
                    ai_data["weaknesses"] = weaknesses
                    ai_data["notable_data"] = notable_data
                    ai_data["evolution_summary"] = evolution_summary
                    break

            # Normalize strengths, weaknesses, notable data defensively
            normalized_strengths = []
            if isinstance(strengths, list):
                for item in strengths:
                    if isinstance(item, dict):
                        normalized_strengths.append({
                            "title": str(item.get("title") or item.get("titulo") or "Punto Fuerte"),
                            "description": str(item.get("description") or item.get("descripcion") or ""),
                            "evidence": str(item.get("evidence") or item.get("evidencia") or "")
                        })
                    else:
                        normalized_strengths.append({
                            "title": "Punto Fuerte",
                            "description": str(item),
                            "evidence": ""
                        })
            while len(normalized_strengths) < 3:
                normalized_strengths.append({
                    "title": f"Punto Fuerte {len(normalized_strengths) + 1}",
                    "description": "Reforzar el cumplimiento del protocolo de atención Boston Medical.",
                    "evidence": "N/A"
                })
            normalized_strengths = normalized_strengths[:3]

            normalized_weaknesses = []
            if isinstance(weaknesses, list):
                for item in weaknesses:
                    if isinstance(item, dict):
                        normalized_weaknesses.append({
                            "title": str(item.get("title") or item.get("titulo") or "Área de Mejora"),
                            "description": str(item.get("description") or item.get("descripcion") or ""),
                            "evidence": str(item.get("evidence") or item.get("evidencia") or "")
                        })
                    else:
                        normalized_weaknesses.append({
                            "title": "Área de Mejora",
                            "description": str(item),
                            "evidence": ""
                        })
            while len(normalized_weaknesses) < 3:
                normalized_weaknesses.append({
                    "title": f"Área de Mejora {len(normalized_weaknesses) + 1}",
                    "description": "Seguir las pautas de capacitación asignadas para el periodo.",
                    "evidence": "N/A"
                })
            normalized_weaknesses = normalized_weaknesses[:3]

            normalized_notable = []
            if isinstance(notable_data, list):
                for item in notable_data:
                    if isinstance(item, dict):
                        normalized_notable.append({
                            "title": str(item.get("title") or item.get("titulo") or "Dato Notable"),
                            "description": str(item.get("description") or item.get("descripcion") or ""),
                            "metric_or_pattern": str(item.get("metric_or_pattern") or item.get("metrica") or item.get("patron") or "N/A")
                        })
                    else:
                        normalized_notable.append({
                            "title": "Dato Notable",
                            "description": str(item),
                            "metric_or_pattern": "N/A"
                        })
            while len(normalized_notable) < 3:
                normalized_notable.append({
                    "title": f"Dato Notable {len(normalized_notable) + 1}",
                    "description": "Estabilidad general en la gestión de llamadas.",
                    "metric_or_pattern": "Estable"
                })
            normalized_notable = normalized_notable[:3]

            # 6. Save report
            logger.info("[training] save_report_start")
            new_report.status = "completed"
            new_report.evaluations_count = aggregates["evaluations_count"]
            new_report.calls_count = aggregates["calls_count"]
            new_report.avg_evaluacion_global = Decimal(str(avg_val_global)).quantize(Decimal("0.01")) if avg_val_global is not None else None
            new_report.summary_general = summary_general
            new_report.strengths_json = normalized_strengths
            new_report.weaknesses_json = normalized_weaknesses
            new_report.notable_data_json = normalized_notable
            new_report.evolution_summary = evolution_summary
            new_report.general_objectives_json = gen_objs
            new_report.specific_objectives_json = spec_objs
            new_report.generated_at = datetime.now(timezone.utc)
            new_report.error_message = None  # Clear any previous error

            # 7. Add simulation prompts & completion status records
            logger.info("[training] save_prompts_start")
            for idx, p in enumerate(sim_prompts):
                if not isinstance(p, dict):
                    logger.error("Simulation prompt item is not a dictionary: %s", p)
                    p = {
                        "prompt_text": str(p),
                        "title": f"Simulación de entrenamiento {idx + 1}",
                        "scenario_type": "roleplay"
                    }
                
                p_number = p.get("prompt_number") or p.get("numero") or (idx + 1)
                try:
                    p_number = int(p_number)
                except (ValueError, TypeError):
                    p_number = idx + 1
                    
                p_title = p.get("title") or p.get("titulo") or p.get("scenario") or f"Simulación de entrenamiento {idx + 1}"
                p_scenario = p.get("scenario_type") or p.get("scenario") or p.get("tipo_escenario") or "roleplay"
                p_text = p.get("prompt_text") or p.get("prompt") or p.get("text") or p.get("texto")
                if not p_text:
                    for k in ["prompt_text", "prompt", "text", "texto", "bot_prompt"]:
                        if p.get(k):
                            p_text = p.get(k)
                            break
                p_focus = p.get("objective_focus") or p.get("enfoque_objetivo") or p.get("objectives") or []
                if not isinstance(p_focus, list):
                    p_focus = [str(p_focus)]
                
                linked_gen = p.get("linked_general_objectives") or []
                if not isinstance(linked_gen, list):
                    linked_gen = [str(linked_gen)]
                    
                linked_spec = p.get("linked_specific_objectives") or []
                if not isinstance(linked_spec, list):
                    linked_spec = [str(linked_spec)]
                    
                obj_summary = p.get("objective_summary") or p.get("resumen_objetivo")
                exp_behavior = p.get("expected_behavior") or p.get("conducta_esperada")
                
                combined_focus = {
                    "focus": p_focus,
                    "linked_general_objectives": linked_gen,
                    "linked_specific_objectives": linked_spec,
                    "objective_summary": obj_summary,
                    "expected_behavior": exp_behavior
                }

                if not p_text:
                    raise ValueError(f"Falta el campo requerido 'prompt_text' para la simulación {idx + 1}")

                new_prompt = TrainingSimulationPrompt(
                    training_report_id=new_report.training_report_id,
                    hubspot_owner_id=hubspot_owner_id,
                    prompt_number=p_number,
                    title=str(p_title),
                    scenario_type=str(p_scenario),
                    objective_focus_json=combined_focus,
                    prompt_text=str(p_text)
                )
                db.add(new_prompt)
                await db.flush()

                # 8. Create completion statuses
                logger.info("[training] save_completion_status_start")
                comp_status = TrainingCompletionStatus(
                    training_report_id=new_report.training_report_id,
                    simulation_prompt_id=new_prompt.simulation_prompt_id,
                    hubspot_owner_id=hubspot_owner_id,
                    status="pending"
                )
                db.add(comp_status)

            # 9. Deactivate previous reports for this agent
            if existing_report_id:
                stmt_old = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == existing_report_id)
                res_old = await db.execute(stmt_old)
                old_rep = res_old.scalars().first()
                if old_rep:
                    old_rep.is_current = False
                    old_rep.superseded_by_report_id = new_report.training_report_id

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

            # 10. Commit
            logger.info("[training] commit_ok")
            await db.commit()
            logger.info("Report ID %d successfully completed for agent %s.", new_report.training_report_id, hubspot_owner_id)
            return new_report

        except Exception as ex:
            logger.info(f"[training] failed exception={str(ex)}")
            logger.exception("Failed to generate report for agent %s in period %s to %s.", hubspot_owner_id, period_start, period_end)
            
            # Rollback the transaction to discard incomplete database structures
            await db.rollback()
            
            # Start a clean transaction to insert a failed report with the traceback details
            try:
                failed_report = TrainingAgentReport(
                    training_run_id=run_id,
                    hubspot_owner_id=hubspot_owner_id,
                    agent_name=agent_name,
                    agent_initials=agent_initials,
                    period_start=period_start,
                    period_end=period_end,
                    status="failed",
                    is_current=True,
                    error_message=str(ex),
                    evaluations_count=aggregates.get("evaluations_count", 0) if 'aggregates' in locals() else 0,
                    calls_count=aggregates.get("calls_count", 0) if 'aggregates' in locals() else 0,
                )
                db.add(failed_report)
                await db.flush()
                
                # Deactivate previous reports even on failure so this failure is marked current
                if existing_report_id:
                    stmt_old = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == existing_report_id)
                    res_old = await db.execute(stmt_old)
                    old_rep = res_old.scalars().first()
                    if old_rep:
                        old_rep.is_current = False
                        old_rep.superseded_by_report_id = failed_report.training_report_id
                
                stmt_deact = select(TrainingAgentReport).where(
                    and_(
                        TrainingAgentReport.hubspot_owner_id == hubspot_owner_id,
                        TrainingAgentReport.training_report_id != failed_report.training_report_id,
                        TrainingAgentReport.is_current == True
                    )
                )
                res_deact = await db.execute(stmt_deact)
                deacts = res_deact.scalars().all()
                for d_rep in deacts:
                    d_rep.is_current = False
                    d_rep.superseded_by_report_id = failed_report.training_report_id
                
                await db.commit()
                return failed_report
            except Exception as e_fail_save:
                logger.exception("Critically failed to save failed report status for %s", hubspot_owner_id)
                await db.rollback()
            
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
            now = datetime.now(timezone.utc)
            period_end = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=timezone.utc) - timedelta(days=1)
        if not period_start:
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

        # Decouple settings from ORM to avoid expired attributes exceptions after transaction commits/rollbacks
        target_agents = [
            {
                "hubspot_owner_id": s.hubspot_owner_id,
                "agent_name": s.agent_name,
                "agent_initials": s.agent_initials
            }
            for s in active_settings
        ]

        # Create global Run record
        new_run = TrainingRun(
            period_start=period_start,
            period_end=period_end,
            status="running",
            triggered_by=triggered_by,
            created_by_email=created_by_email,
            started_at=datetime.now(timezone.utc),
            agents_total=len(target_agents)
        )
        db.add(new_run)
        await db.commit()
        await db.refresh(new_run)

        run_id = new_run.training_run_id
        completed = 0
        skipped = 0
        failed = 0
        failed_agents_errors = []

        for agent_info in target_agents:
            owner_id = agent_info["hubspot_owner_id"]
            initials = agent_info["agent_initials"]
            try:
                rep = await PersonalizedTrainingService.generate_report_for_agent(
                    db=db,
                    hubspot_owner_id=owner_id,
                    period_start=period_start,
                    period_end=period_end,
                    run_id=run_id,
                    force_regenerate=force_regenerate
                )
                
                # Fetch primitive values immediately so we don't cause lazy loading after commits/rollbacks
                rep_status = rep.status
                rep_error = rep.error_message
                
                if rep_status == "completed":
                    completed += 1
                elif rep_status == "skipped":
                    skipped += 1
                else:
                    failed += 1
                    if rep_error:
                        failed_agents_errors.append(f"{initials}: {rep_error}")
            except Exception as e_agent:
                logger.error("Failed agent %s in run ID %d: %s", owner_id, run_id, e_agent)
                failed += 1
                failed_agents_errors.append(f"{initials}: {str(e_agent)}")

        # Re-fetch new_run to avoid any greenlet/expired attribute issues after rollbacks
        stmt_run = select(TrainingRun).where(TrainingRun.training_run_id == run_id)
        res_run = await db.execute(stmt_run)
        new_run = res_run.scalar_one()

        # Finalize run
        new_run.status = "completed" if failed == 0 else ("partially_completed" if completed > 0 else "failed")
        new_run.agents_completed = completed
        new_run.agents_skipped = skipped
        new_run.agents_failed = failed
        new_run.finished_at = datetime.now(timezone.utc)
        if failed_agents_errors:
            new_run.error_message = "; ".join(failed_agents_errors)

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
        
        # 1. Global environment override check
        if not settings.enable_training_scheduler:
            logger.info("Training scheduler: Automatically disabled globally by environment variable ENABLE_TRAINING_SCHEDULER=false.")
            return {"triggered": False, "reason": "Scheduler deshabilitado por variable de entorno"}

        # 2. Database persistent settings check
        db_settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
        if not db_settings.is_enabled:
            logger.info("Training scheduler: Persistently disabled in database settings.")
            return {"triggered": False, "reason": "Scheduler deshabilitado en base de datos"}

        # 3. Fetch latest completed run
        stmt = select(TrainingRun).where(
            TrainingRun.status.in_(["completed", "partially_completed"])
        ).order_by(desc(TrainingRun.training_run_id)).limit(1)
        
        res = await db.execute(stmt)
        last_run = res.scalars().first()
        
        now = datetime.now(timezone.utc)
        due = False
        reason = ""
        
        # 4. Resolve due by next_run_at or interval days
        if db_settings.next_run_at:
            next_run = db_settings.next_run_at
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
                
            if now >= next_run:
                due = True
                reason = f"Current time {now.isoformat()} is at or past next scheduled run {next_run.isoformat()}."
            else:
                reason = f"Next scheduled run is at {next_run.isoformat()} (due in {(next_run - now).days} days)."
        else:
            # Fallback to last run + interval_days
            if not last_run:
                due = True
                reason = "No previous training runs exist and no schedule next_run_at is defined."
            else:
                ref_time = last_run.finished_at or last_run.created_at
                if ref_time.tzinfo is None:
                    ref_time = ref_time.replace(tzinfo=timezone.utc)
                    
                elapsed = now - ref_time
                limit = timedelta(days=db_settings.interval_days)
                
                if elapsed >= limit:
                    due = True
                    reason = f"Last run finished {elapsed.days} days ago (limit is {db_settings.interval_days} days)."
                else:
                    reason = f"Last run was {elapsed.days} days ago (limit is {db_settings.interval_days} days). Next run due in {db_settings.interval_days - elapsed.days} days."

        if due:
            logger.info("Training scheduler: A new personalized training run is DUE. Reason: %s", reason)
            
            # Set setting status to running before launching to prevent parallel triggers
            db_settings.last_status = "running"
            await db.commit()
            
            run = None
            try:
                run = await PersonalizedTrainingService.run_personalized_training_pass(
                    db=db,
                    triggered_by="scheduler"
                )
                
                # Fetch settings again to refresh session state safely
                db_settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
                db_settings.last_run_at = now
                db_settings.next_run_at = now + timedelta(days=db_settings.interval_days)
                db_settings.last_status = run.status
                await db.commit()
                return {"triggered": True, "run_id": run.training_run_id, "reason": reason}
            except Exception as e_run:
                logger.exception("Training scheduler: Run failed.")
                # Fetch settings again to refresh session state safely
                db_settings = await PersonalizedTrainingService.get_or_create_scheduler_settings(db)
                db_settings.last_run_at = now
                db_settings.next_run_at = now + timedelta(days=db_settings.interval_days)
                db_settings.last_status = "failed"
                await db.commit()
                raise e_run
        
        logger.info("Training scheduler: No training run due. Status: %s", reason)
        return {"triggered": False, "reason": reason}

