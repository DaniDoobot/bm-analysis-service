"""Pydantic schemas for mass evaluations."""
from datetime import datetime, time
from typing import Any
from pydantic import BaseModel, model_validator


class MassEvaluationJobCreate(BaseModel):
    job_name: str
    description: str | None = None
    is_active: bool = True
    prompt_id: int
    prompt_version_id: int | None = None

    # Validation override flags
    allow_inactive_prompt: bool = False
    test_mode: bool = False

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
    max_calls: int = 10

    schedule_enabled: bool = False
    schedule_type: str | None = None  # manual / daily / weekly / monthly / cron
    schedule_cron: str | None = None
    schedule_rrule: str | None = None
    schedule_day_of_week: int | None = None  # 0 Mon, 6 Sun
    schedule_day_of_month: int | None = None
    schedule_time: time | None = None

    created_by: str | None = None
    created_by_email: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_inputs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        def get_matching_value(keys):
            # First pass: prioritize any non-None, non-empty values
            for k in keys:
                if k in data and data[k] is not None and data[k] != "":
                    return data[k], True
            # Second pass: if all matches are None/empty, return the first present one
            for k in keys:
                if k in data:
                    return data[k], True
            return None, False

        # Normalize date_from
        val, found = get_matching_value(["date_from", "search_date_from", "fecha_desde"])
        if found:
            if val == "":
                val = None
            data["date_from"] = val

        # Normalize date_to
        val, found = get_matching_value(["date_to", "search_date_to", "fecha_hasta"])
        if found:
            if val == "":
                val = None
            data["date_to"] = val

        # Normalize time_window_start
        val, found = get_matching_value(["time_window_start", "time_from", "search_time_from", "hour_from", "hora_desde"])
        if found:
            if val == "":
                val = None
            data["time_window_start"] = val

        # Normalize time_window_end
        val, found = get_matching_value(["time_window_end", "time_to", "search_time_to", "hour_to", "hora_hasta"])
        if found:
            if val == "":
                val = None
            data["time_window_end"] = val

        return data


class MassEvaluationJobUpdate(BaseModel):
    job_name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    prompt_id: int | None = None
    prompt_version_id: int | None = None

    # Validation override flags
    allow_inactive_prompt: bool | None = None
    test_mode: bool | None = None

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

    @model_validator(mode="before")
    @classmethod
    def normalize_inputs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        def get_matching_value(keys):
            # First pass: prioritize any non-None, non-empty values
            for k in keys:
                if k in data and data[k] is not None and data[k] != "":
                    return data[k], True
            # Second pass: if all matches are None/empty, return the first present one
            for k in keys:
                if k in data:
                    return data[k], True
            return None, False

        # Normalize date_from
        val, found = get_matching_value(["date_from", "search_date_from", "fecha_desde"])
        if found:
            if val == "":
                val = None
            data["date_from"] = val

        # Normalize date_to
        val, found = get_matching_value(["date_to", "search_date_to", "fecha_hasta"])
        if found:
            if val == "":
                val = None
            data["date_to"] = val

        # Normalize time_window_start
        val, found = get_matching_value(["time_window_start", "time_from", "search_time_from", "hour_from", "hora_desde"])
        if found:
            if val == "":
                val = None
            data["time_window_start"] = val

        # Normalize time_window_end
        val, found = get_matching_value(["time_window_end", "time_to", "search_time_to", "hour_to", "hora_hasta"])
        if found:
            if val == "":
                val = None
            data["time_window_end"] = val

        return data


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
    time_window_start: Any = None
    time_window_end: Any = None
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

    # HTML Input Compatible String format fields / Aliases:
    search_date_from: str | None = None
    search_date_to: str | None = None
    search_time_from: str | None = None
    search_time_to: str | None = None

    date_from_str: str | None = None
    date_to_str: str | None = None
    time_from: str | None = None
    time_to: str | None = None
    
    fecha_desde: str | None = None
    fecha_hasta: str | None = None
    hora_desde: str | None = None
    hora_hasta: str | None = None
    
    hour_from: str | None = None
    hour_to: str | None = None
    fecha_hasta: str | None = None
    hora_desde: str | None = None
    hora_hasta: str | None = None

    @model_validator(mode="after")
    def populate_aliases(self) -> 'MassEvaluationJobResponse':
        d_from = self.date_from
        d_to = self.date_to
        t_start = self.time_window_start
        t_end = self.time_window_end

        # Convert timezone-aware datetimes to the job's designated timezone to avoid date shifts
        import zoneinfo
        tz = None
        try:
            tz = zoneinfo.ZoneInfo(self.timezone or "Europe/Madrid")
        except Exception:
            # Fall back to local system timezone (astimezone(None)) on Windows without tzdata
            tz = None

        if tz is not None:
            d_from_tz = d_from.astimezone(tz) if d_from and d_from.tzinfo else d_from
            d_to_tz = d_to.astimezone(tz) if d_to and d_to.tzinfo else d_to
        else:
            d_from_tz = d_from.astimezone(None) if d_from and d_from.tzinfo else d_from
            d_to_tz = d_to.astimezone(None) if d_to and d_to.tzinfo else d_to

        # format date to YYYY-MM-DD
        d_from_str = d_from_tz.strftime("%Y-%m-%d") if d_from_tz else None
        d_to_str = d_to_tz.strftime("%Y-%m-%d") if d_to_tz else None
        
        # format time to HH:MM (safeguarding string/time types)
        from datetime import time as dt_time
        
        t_start_str = None
        if isinstance(t_start, dt_time):
            t_start_str = t_start.strftime("%H:%M")
        elif isinstance(t_start, str) and t_start:
            t_start_str = t_start[:5]
            
        t_end_str = None
        if isinstance(t_end, dt_time):
            t_end_str = t_end.strftime("%H:%M")
        elif isinstance(t_end, str) and t_end:
            t_end_str = t_end[:5]

        self.time_window_start = t_start_str
        self.time_window_end = t_end_str

        self.search_date_from = d_from_str
        self.search_date_to = d_to_str
        self.search_time_from = t_start_str
        self.search_time_to = t_end_str

        self.date_from_str = d_from_str
        self.date_to_str = d_to_str
        self.time_from = t_start_str
        self.time_to = t_end_str

        self.fecha_desde = d_from_str
        self.fecha_hasta = d_to_str
        self.hora_desde = t_start_str
        self.hora_hasta = t_end_str

        self.hour_from = t_start_str
        self.hour_to = t_end_str

        return self

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
    
    # Service and typology snapshot
    service_id: int | None = None
    service_key: str | None = None
    service_name: str | None = None
    typology_id: int | None = None
    typology_key: str | None = None
    typology_name: str | None = None

    status: str
    result_json: dict[str, Any] | None
    items_json: Any
    items_visual: list[dict[str, Any]] | None = None
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


class MassEvaluationRunLaunchResponse(BaseModel):
    message: str = "Run started"
    polling_url: str
    run: MassEvaluationRunResponse


class MassCriterionTypologyBackfillRequest(BaseModel):
    mode: str  # "dry_run" | "execute"
    performed_by_email: str
