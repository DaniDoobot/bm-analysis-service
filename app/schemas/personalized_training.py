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
    is_enabled: bool = True


class TrainingAgentSettingOut(TrainingAgentSettingBase):
    setting_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TrainingAgentSettingUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    agent_name: Optional[str] = None
    agent_initials: Optional[str] = None


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
    rationale: str
    expected_behavior: str
    success_indicators: List[str]


class SpecificObjectiveDetail(BaseModel):
    title: str
    description: str
    related_criteria: List[str]
    specific_behavior_to_improve: str
    success_indicators: List[str]


# ── Simulation Prompts & Completion Schemas ──────────────────────────────────

class SimulationPromptOut(BaseModel):
    simulation_prompt_id: int
    training_report_id: int
    hubspot_owner_id: str
    prompt_number: int
    title: str
    scenario_type: str
    objective_focus_json: List[str] = Field(default_factory=list)
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
    created_at: datetime

    class Config:
        from_attributes = True


# ── Reports Schemas ───────────────────────────────────────────────────────────

class TrainingAgentReportBase(BaseModel):
    training_report_id: int
    training_run_id: Optional[int] = None
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
    
    is_current: bool
    created_at: datetime
    generated_at: Optional[datetime] = None


class TrainingAgentReportOut(TrainingAgentReportBase):
    prompts: List[SimulationPromptOut] = []
    completion_statuses: List[CompletionStatusOut] = []

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


class AgentDetailResponse(BaseModel):
    agent_setting: TrainingAgentSettingOut
    current_report: Optional[TrainingAgentReportOut] = None
    progress_completed: int = 0
    progress_total: int = 4
    progress_percentage: Decimal = Decimal("0.0")
    history: List[TrainingAgentReportBase] = []
    evolution_summary: Optional[str] = None


# ── Manual Generation Request Schema ──────────────────────────────────────────

class ManualGeneratePayload(BaseModel):
    hubspot_owner_ids: Optional[List[str]] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    force_regenerate: bool = False
