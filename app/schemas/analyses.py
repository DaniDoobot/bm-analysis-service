"""Pydantic schemas for analyses."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AnalysisListItem(BaseModel):
    """Row from bm_call_analysis_current for listing."""
    model_config = ConfigDict(from_attributes=True)

    call_id: str
    analysis_type: str
    analysis_id: int | None = None
    latest_analysis_id: int | None = None
    hubspot_url: str | None = None
    call_direction: str | None = None
    call_timestamp: datetime | str | None = None
    source: str | None = None
    fecha_eval: Any | None = None
    updated_at: datetime | str | None = None
    agente_telefonico: str | None = None
    hubspot_owner_id: str | None = None
    prompt_id: int | None = None
    prompt_version_id: int | None = None
    status: str | None = None
    tipo_llamada: str | None = None
    evaluacion_global: Any | None = None
    result: Any | None = None
    payload: Any | None = None


class AnalysisDetail(BaseModel):
    """Full row from bm_analyses."""
    model_config = ConfigDict(from_attributes=True)

    analysis_id: int
    analysis_type: str | None = None
    call_id: str | None = None
    hubspot_url: str | None = None
    call_direction: str | None = None
    call_timestamp: datetime | str | None = None
    source: str | None = None
    run_ts: datetime | str | None = None
    fecha_eval: Any | None = None
    agente_telefonico: str | None = None
    hubspot_owner_id: str | None = None
    prompt_id: int | None = None
    prompt_version_id: int | None = None
    transcription: str | None = None
    transcription_provider: str | None = None
    transcription_model: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    status: str | None = None
    tipo_llamada: str | None = None
    evaluacion_global: Any | None = None
    result: Any | None = None
    payload: Any | None = None
    error_message: str | None = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


class AnalysisResultOut(BaseModel):
    """Row from bm_analysis_results."""
    model_config = ConfigDict(from_attributes=True)

    result_id: int
    analysis_id: int | None = None
    criterion_id: int | None = None
    criterion_key: str | None = None
    criterion_name: str | None = None
    criterion_type: str | None = None
    value_number: float | int | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    value_category: str | None = None
    feed: str | None = None
    description: str | None = None
    raw_value: Any | None = None
    created_at: datetime | str | None = None


class AnalysisDetailResponse(BaseModel):
    ok: bool = True
    analysis: AnalysisDetail
    summary: dict[str, Any] = {}
    results: list[AnalysisResultOut] = []
    grouped: dict[str, list[AnalysisResultOut]] = {}


# ── Analysis Request Schemas ──────────────────────────────────────────────────

class AnalyzeAudioRequest(BaseModel):
    call_id: str
    prompt_id: int | None = None
    prompt_version_id: int | None = None
    analysis_type: str = "audio"
    metadata: dict[str, Any] | None = None
    recording_url: str | None = None
    audio_url: str | None = None
    force: bool = False


class TranscribeRequest(BaseModel):
    call_id: str


class AnalyzeTranscriptionRequest(BaseModel):
    call_id: str
    transcription: str
    analysis_type: str = "text"
    prompt_id: int | None = None
    prompt_version_id: int | None = None
    metadata: dict[str, Any] | None = None

