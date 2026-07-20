"""Pydantic schemas for personalized agent training endpoints."""
from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional
from pydantic import BaseModel, Field


# ── Agent Settings Schemas ───────────────────────────────────────────────────

class TrainingAgentSettingBase(BaseModel):
    hubspot_owner_id: str
    agent_name: str
    agent_initials: str
    company_id: Optional[int] = None
    is_enabled: bool = True
    training_code: Optional[str] = None
    training_numeric_code: Optional[str] = None
    training_code_enabled: bool = True


class TrainingAgentSettingOut(TrainingAgentSettingBase):
    setting_id: int
    training_code_updated_at: datetime
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TrainingAgentSettingUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    agent_name: Optional[str] = None
    agent_initials: Optional[str] = None
    training_code: Optional[str] = None
    training_numeric_code: Optional[str] = None
    training_code_enabled: Optional[bool] = None


# ── Objectives & Prompts sub-schemas ──────────────────────────────────────────

class StrengthWeaknessDetail(BaseModel):
    title: str
    description: str
    evidence: str


class NotableDataDetail(BaseModel):
    title: str
    description: str
    metric_or_pattern: str


class GeneralObjectiveDetail(BaseModel):
    title: str
    description: str
    rationale: Optional[str] = ""
    expected_behavior: Optional[str] = ""
    success_indicators: Optional[List[str]] = Field(default_factory=list)
    status: Optional[str] = None
    is_evaluated: Optional[bool] = False
    score: Optional[float] = None
    base_score: Optional[float] = None
    improvement_delta: Optional[float] = None
    justification: Optional[str] = None
    evaluated_at: Optional[datetime] = None


class SpecificObjectiveDetail(BaseModel):
    title: str
    description: str
    related_criteria: Optional[List[str]] = Field(default_factory=list)
    specific_behavior_to_improve: Optional[str] = ""
    success_indicators: Optional[List[str]] = Field(default_factory=list)
    status: Optional[str] = None
    is_evaluated: Optional[bool] = False
    score: Optional[float] = None
    base_score: Optional[float] = None
    improvement_delta: Optional[float] = None
    justification: Optional[str] = None
    evaluated_at: Optional[datetime] = None


# ── Simulation Prompts & Completion Schemas ──────────────────────────────────

class SimulationPromptOut(BaseModel):
    simulation_prompt_id: int
    training_report_id: int
    hubspot_owner_id: str
    prompt_number: int
    title: str
    scenario_type: str
    objective_focus_json: Optional[Any] = Field(default=None)
    linked_general_objectives: Optional[List[str]] = Field(default_factory=list)
    linked_specific_objectives: Optional[List[str]] = Field(default_factory=list)
    objective_summary: Optional[str] = None
    expected_behavior: Optional[str] = None
    prompt_text: str
    created_at: datetime

    class Config:
        from_attributes = True


class CompletionStatusOut(BaseModel):
    completion_id: int
    training_report_id: int
    simulation_prompt_id: int
    hubspot_owner_id: str
    status: str
    completed_at: Optional[datetime] = None
    training_call_id: Optional[str] = None
    training_phone_number: Optional[str] = None
    notes: Optional[str] = None
    score: Optional[float] = None
    prompt_number: Optional[int] = None
    feedback: Optional[str] = None
    criteria: Optional[dict[str, Any]] = None
    transcription_turns: Optional[List[dict[str, Any]]] = None
    evaluation_id: Optional[int] = None
    call_session_id: Optional[int] = None
    title: Optional[str] = None
    strengths: Optional[List[str]] = None
    weaknesses: Optional[List[str]] = None
    result_json: Optional[dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── Reports Schemas ───────────────────────────────────────────────────────────

class TrainingAgentReportBase(BaseModel):
    training_report_id: int
    training_run_id: Optional[int] = None
    company_id: Optional[int] = None
    service_id: Optional[int] = None
    hubspot_owner_id: str
    agent_name: str
    agent_initials: str
    period_start: datetime
    period_end: datetime
    status: str
    skipped_reason: Optional[str] = None
    evaluations_count: int
    calls_count: int
    avg_evaluacion_global: Optional[Decimal] = None
    
    summary_general: Optional[str] = None
    strengths_json: Optional[List[StrengthWeaknessDetail]] = None
    weaknesses_json: Optional[List[StrengthWeaknessDetail]] = None
    notable_data_json: Optional[List[NotableDataDetail]] = None
    evolution_summary: Optional[str] = None
    general_objectives_json: Optional[List[GeneralObjectiveDetail]] = None
    specific_objectives_json: Optional[List[SpecificObjectiveDetail]] = None
    final_report_json: Optional[dict[str, Any]] = None
    
    is_current: bool
    created_at: datetime
    generated_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    approved_by_user_id: Optional[int] = None
    error_message: Optional[str] = None

    # Cycle progress fields
    progress_completed: int = 0
    progress_total: int = 4
    progress_percentage: Decimal = Decimal("0.0")

    class Config:
        from_attributes = True


class TrainingAgentReportOut(TrainingAgentReportBase):
    prompts: List[SimulationPromptOut] = []
    completion_statuses: List[CompletionStatusOut] = []
    simulations: List[CompletionStatusOut] = []

    class Config:
        from_attributes = True


# ── Runs Schemas ──────────────────────────────────────────────────────────────

class TrainingRunResponse(BaseModel):
    training_run_id: int
    period_start: datetime
    period_end: datetime
    status: str
    triggered_by: str
    created_by_email: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    agents_total: int
    agents_completed: int
    agents_skipped: int
    agents_failed: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Dashboard & Overview Schemas (for Lovable UI) ─────────────────────────────

class AgentOverviewItem(BaseModel):
    hubspot_owner_id: str
    agent_initials: str
    agent_name: str
    is_enabled: bool
    current_report_id: Optional[int] = None
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    status: Optional[str] = "no_data"
    evaluations_count: int = 0
    summary_general: Optional[str] = None
    objectives_count: int = 0
    simulation_prompts_count: int = 0
    progress_completed: int = 0
    progress_total: int = 4
    progress_percentage: Decimal = Decimal("0.0")
    last_generated_at: Optional[datetime] = None
    previous_reports_count: int = 0
    error_message: Optional[str] = None

    # Advanced cycle metrics
    pending_cycles: int = 0
    pending_simulations: int = 0
    completed_cycles: int = 0
    pending_cycles_count: int = 0
    pending_simulations_count: int = 0
    active_cycles_count: int = 0
    completed_cycles_count: int = 0
    latest_cycle_status: Optional[str] = None
    latest_cycle_progress_completed: int = 0
    latest_cycle_progress_total: int = 4
    latest_cycle_period_start: Optional[datetime] = None
    latest_cycle_period_end: Optional[datetime] = None
    latest_cycle_avg_score: Optional[Decimal] = None


class AgentDetailResponse(BaseModel):
    agent_setting: TrainingAgentSettingOut
    current_report: Optional[TrainingAgentReportOut] = None
    progress_completed: int = 0
    progress_total: int = 4
    progress_percentage: Decimal = Decimal("0.0")
    history: List[TrainingAgentReportOut] = []
    evolution_summary: Optional[str] = None

    class Config:
        from_attributes = True



# ── Manual Generation Request Schema ──────────────────────────────────────────

class ManualGeneratePayload(BaseModel):
    hubspot_owner_ids: Optional[List[str]] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    force_regenerate: bool = False


# ── Scheduler Settings Schemas ────────────────────────────────────────────────

class TrainingSchedulerSettingOut(BaseModel):
    is_enabled: bool
    interval_days: int
    lookback_days: int
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    updated_at: datetime
    runtime_enabled: bool = True
    reason: Optional[str] = None

    class Config:
        from_attributes = True


class TrainingSchedulerSettingPatch(BaseModel):
    is_enabled: Optional[bool] = None
    interval_days: Optional[int] = None
    lookback_days: Optional[int] = None


# ── Team Summary Schemas ──────────────────────────────────────────────────────

class PriorityAgentItem(BaseModel):
    hubspot_owner_id: str
    agent_initials: str
    agent_name: str
    score: Optional[float] = None
    score_delta: Optional[float] = None
    pending_cycles: int
    pending_simulations: int
    status: str
    reason: str


class RecurringPatternItem(BaseModel):
    label: str
    category: str
    affected_agents: int
    affected_cycles: int
    occurrences: int
    avg_score: float
    severity: str
    reason: str
    source: str
    examples: List[str]
    
    # Backwards compatibility fields
    count: int
    total_agents: int


class CycleEvolutionItem(BaseModel):
    cycle_label: str
    team_avg_score: float
    close_rate: float
    completed_cycles: int
    pending_simulations: int


class CyclesTeamSummaryResponse(BaseModel):
    # active_agents kept for backwards compatibility; same as monitored_agents
    active_agents: int
    # monitored_agents: all agents that have valid (non-archived) cycles, regardless of is_enabled
    monitored_agents: int
    # generation_enabled_agents: agents with is_enabled=True (scheduled to receive new cycles)
    generation_enabled_agents: int
    total_cycles: int
    completed_cycles_total: int
    running_cycles_total: int
    team_avg_score: float
    team_avg_score_delta: float
    avg_close_rate: float
    agents_requiring_attention: int
    agents_improving: int
    agents_stagnant: int
    agents_declining: int
    pending_cycles: int
    pending_simulations: int
    pending_approval_cycles: int = 0
    priority_agents: List[PriorityAgentItem]
    recurring_patterns: List[RecurringPatternItem]
    cycle_evolution: List[CycleEvolutionItem]


# ── Approval Flow Schemas ─────────────────────────────────────────────────────

class UpdateCycleObjectivesPayload(BaseModel):
    """Payload para editar objetivos de un ciclo en estado pending_approval."""
    general_objectives_json: Optional[List[dict]] = Field(
        default=None,
        description="Lista de objetivos generales actualizados. Si es None, no se modifica."
    )
    specific_objectives_json: Optional[List[dict]] = Field(
        default=None,
        description="Lista de objetivos específicos actualizados. Si es None, no se modifica."
    )


class ApproveCycleResponse(BaseModel):
    """Respuesta al aprobar un ciclo de entrenamiento."""
    training_report_id: int
    status: str
    approved_at: datetime
    approved_by_user_id: int
    prompts_generated: int
    message: str


class ManualCycleCreateRequest(BaseModel):
    """Payload para la creación de un ciclo de entrenamiento manual."""
    hubspot_owner_ids: List[str] = Field(..., description="Lista de IDs de agentes para los que crear el ciclo.")
    title: Optional[str] = Field(default="Ciclo manual", description="Título/resumen del ciclo manual.")
    service_id: Optional[int] = Field(default=None, description="ID del servicio asociado opcional.")

    # Preferred fields (new)
    general_objectives: Optional[List[str]] = Field(
        default=None,
        description="Objetivos generales del ciclo manual."
    )
    specific_objectives: Optional[List[str]] = Field(
        default=None,
        description="Objetivos específicos del ciclo manual."
    )

    # Legacy field — if general_objectives/specific_objectives are absent, treat as specific_objectives
    objectives: Optional[List[str]] = Field(
        default=None,
        description="[Legacy] Lista de objetivos; se tratan como objetivos específicos si no llegan general_objectives/specific_objectives."
    )
