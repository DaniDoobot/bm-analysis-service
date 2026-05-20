"""Pydantic schemas for mass evaluations."""
from datetime import datetime, time
from typing import Any
from pydantic import BaseModel


class MassEvaluationJobCreate(BaseModel):
    job_name: str
    description: str | None = None
    is_active: bool = True
    prompt_id: int
    prompt_version_id: int | None = None

    agent_owner_ids: list[str] | None = None
    agent_names: list[str] | None = None
    date_mode: str = "relative"  # relative / fixed_range / previous_day / previous_week / custom
    date_from: datetime | None = None
    date_to: datetime | None = None
    relative_days: int | None = None
    time_window_start: time | None = None
    time_window_end: time | None = None
    timezone: str = "Europe/Madrid"
    duration_min_seconds: int | None = None
    duration_max_seconds: int | None = None
    direction: str = "all"  # inbound / outbound / all
    only_with_recording: bool = True
    max_calls: int = 100

    schedule_enabled: bool = False
    schedule_type: str | None = None  # manual / daily / weekly / monthly / cron
    schedule_cron: str | None = None
    schedule_rrule: str | None = None
    schedule_day_of_week: int | None = None  # 0 Mon, 6 Sun
    schedule_day_of_month: int | None = None
    schedule_time: time | None = None

    created_by: str | None = None
    created_by_email: str | None = None


class MassEvaluationJobUpdate(BaseModel):
    job_name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    prompt_id: int | None = None
    prompt_version_id: int | None = None

    agent_owner_ids: list[str] | None = None
    agent_names: list[str] | None = None
    date_mode: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    relative_days: int | None = None
    time_window_start: time | None = None
    time_window_end: time | None = None
    timezone: str | None = None
    duration_min_seconds: int | None = None
    duration_max_seconds: int | None = None
    direction: str | None = None
    only_with_recording: bool | None = None
    max_calls: int | None = None

    schedule_enabled: bool | None = None
    schedule_type: str | None = None
    schedule_cron: str | None = None
    schedule_rrule: str | None = None
    schedule_day_of_week: int | None = None
    schedule_day_of_month: int | None = None
    schedule_time: time | None = None


class MassEvaluationJobResponse(BaseModel):
    job_id: int
    job_name: str
    description: str | None
    is_active: bool
    prompt_id: int
    prompt_version_id: int | None
    prompt_name: str | None
    prompt_version_name: str | None
    prompt_version_label: str | None

    agent_owner_ids: list[str] | None
    agent_names: list[str] | None
    date_mode: str
    date_from: datetime | None
    date_to: datetime | None
    relative_days: int | None
    time_window_start: time | None
    time_window_end: time | None
    timezone: str
    duration_min_seconds: int | None
    duration_max_seconds: int | None
    direction: str
    only_with_recording: bool
    max_calls: int

    schedule_enabled: bool
    schedule_type: str | None
    schedule_cron: str | None
    schedule_rrule: str | None
    schedule_day_of_week: int | None
    schedule_day_of_month: int | None
    schedule_time: time | None
    next_run_at: datetime | None
    last_run_at: datetime | None

    created_at: datetime
    updated_at: datetime
    created_by: str | None
    created_by_email: str | None

    class Config:
        from_attributes = True


class MassEvaluationRunResponse(BaseModel):
    run_id: int
    job_id: int
    trigger_type: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    calls_found: int
    calls_selected: int
    calls_analyzed: int
    calls_skipped: int
    calls_failed: int
    effective_filters: dict[str, Any] | None
    error_message: str | None
    run_summary: dict[str, Any] | None
    created_at: datetime

    class Config:
        from_attributes = True


class MassEvaluationResultResponse(BaseModel):
    mass_analysis_id: int
    run_id: int
    job_id: int
    call_id: str
    hs_object_id: str | None
    recording_url: str | None
    hubspot_owner_id: str | None
    agent_name: str | None
    call_timestamp: datetime | None
    analysis_timestamp: datetime
    call_duration_seconds: int | None
    direction: str | None
    prompt_id: int
    prompt_version_id: int | None
    prompt_name: str | None
    prompt_version_name: str | None
    prompt_version_label: str | None
    prompt_snapshot: str
    status: str
    result_json: dict[str, Any] | None
    items_json: Any
    hubspot_metadata: dict[str, Any] | None
    error_message: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class MassEvaluationJobManualRunRequest(BaseModel):
    trigger_type: str = "manual"
    override_date_from: datetime | None = None
    override_date_to: datetime | None = None
    dry_run: bool = False
