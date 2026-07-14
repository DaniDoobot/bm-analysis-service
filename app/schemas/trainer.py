"""Pydantic schemas for the Trainer module."""
from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional
from pydantic import BaseModel, Field


# ── Evaluation Config Schemas ──────────────────────────────────────────────────

class TrainerEvaluationConfigCreate(BaseModel):
    name: str = Field(..., description="Nombre descriptivo de la configuración")
    service_id: int = Field(..., description="ID del servicio asociado")
    speech_structure_id: int = Field(..., description="ID del prompt/estructura base de Speech (bm_prompts)")
    extra_instructions: Optional[str] = Field(None, description="Instrucciones adicionales para la evaluación")
    is_active: bool = Field(True, description="Estado activo/desactivo")


class TrainerEvaluationConfigUpdate(BaseModel):
    name: Optional[str] = None
    extra_instructions: Optional[str] = None
    is_active: Optional[bool] = None


class TrainerEvaluationConfigResponse(BaseModel):
    config_id: int
    name: str
    service_id: int
    service_name: Optional[str] = None
    speech_structure_id: int
    speech_structure_name: Optional[str] = None
    speech_structure_type: Optional[str] = None
    speech_structure_description: Optional[str] = None
    extra_instructions: Optional[str] = None
    is_active: bool
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Simulation Schemas ──────────────────────────────────────────────────────────

class TrainerSimulationCreate(BaseModel):
    name: str = Field(..., description="Nombre de la simulación de roleplay")
    code: str = Field(..., description="Código único global para iniciarla por teléfono")
    service_id: int = Field(..., description="ID del servicio asociado")
    roleplay_prompt: str = Field(..., description="Instrucciones de roleplay/personaje para Gemini Live")
    evaluation_config_id: Optional[int] = Field(None, description="ID de la configuración de evaluación asociada")
    objective: Optional[str] = Field(None, description="Objetivo de la simulación")
    difficulty: Optional[str] = Field(None, description="Dificultad de la simulación (e.g. fácil, media, difícil)")


class TrainerSimulationUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    roleplay_prompt: Optional[str] = None
    evaluation_config_id: Optional[int] = None
    objective: Optional[str] = None
    difficulty: Optional[str] = None
    status: Optional[str] = None  # draft, published, archived


class TrainerSimulationResponse(BaseModel):
    simulation_id: int
    name: str
    code: str
    service_id: int
    evaluation_config_id: Optional[int] = None
    roleplay_prompt: str
    status: str
    objective: Optional[str] = None
    difficulty: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    published_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Session & Evaluation Schemas ───────────────────────────────────────────────

class TrainerEvaluationResponse(BaseModel):
    evaluation_id: int
    session_id: int
    evaluation_config_id: Optional[int] = None
    prompt_snapshot: str
    result_json: dict
    score: Optional[Decimal] = None
    summary: Optional[str] = None
    strengths: Optional[dict] = None
    improvement_points: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TrainerSessionResponse(BaseModel):
    session_id: int
    simulation_id: int
    simulation_version_id: Optional[int] = None
    agent_id: str
    agent_code: str
    service_id: int
    call_id: str
    external_call_sid: Optional[str] = None
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    duration_seconds: Optional[int] = None
    status: str
    evaluation_status: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # Denormalised fields for convenience (populated by service layer)
    agent_name: Optional[str] = None
    simulation_name: Optional[str] = None
    simulation_code: Optional[str] = None
    service_name: Optional[str] = None

    # New structured evaluation fields
    score: Optional[float] = None
    score_max: Optional[int] = None
    score_source: Optional[str] = None
    transcription: Optional[str] = None
    call_status: Optional[str] = None

    evaluation_summary: Optional[str] = None
    criteria_scores: Optional[List[dict]] = None
    extraction_values: Optional[dict] = None
    score_items: Optional[List[dict]] = None
    non_score_items: Optional[List[dict]] = None
    evaluation_json: Optional[dict] = None

    # Config & structure info
    evaluation_config_id: Optional[int] = None
    evaluation_config_name: Optional[str] = None
    speech_structure_id: Optional[int] = None
    speech_structure_name: Optional[str] = None

    # Nested relationships if queried in detail
    simulation: Optional[TrainerSimulationResponse] = None
    evaluation: Optional[TrainerEvaluationResponse] = None

    class Config:
        from_attributes = True



class TrainerSessionList(BaseModel):
    sessions: List[TrainerSessionResponse]
    total_count: int


# ── AI Prompts Assistant Schemas ───────────────────────────────────────────────

class AIPromptGenerateRequest(BaseModel):
    service_id: int
    ideas: str
    objective: str
    difficulty: Optional[str] = None
    tone: Optional[str] = None


class AIPromptImproveRequest(BaseModel):
    current_prompt: str
    requested_changes: str
    service_id: Optional[int] = None


class AvailableSpeechStructure(BaseModel):
    prompt_id: int
    prompt_name: str
    prompt_type: str
    description: Optional[str] = None
    is_active: bool
    is_archived: bool

    class Config:
        from_attributes = True


# ── Phone Webhooks / Integration Schemas ────────────────────────────────────────

class PhoneValidateAgentRequest(BaseModel):
    agent_code: str


class PhoneValidateSimulationRequest(BaseModel):
    simulation_code: str


class PhoneStartSessionRequest(BaseModel):
    agent_code: str
    simulation_code: str
    call_id: str


class PhoneCompleteSessionRequest(BaseModel):
    session_id: int
    transcript: str
    recording_url: str
    duration_seconds: int
