"""
Pydantic schemas for FinOps AI Usage Reporting (Phase 4).
Covers summary metrics, breakdown by dimensions (company, service, module, model),
and timeseries trends.
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class UsageSummaryResponse(BaseModel):
    """Overall FinOps summary metrics for the requested period and filters."""
    period: str
    date_from: datetime
    date_to: datetime
    currency: str = "USD"

    total_events: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    cached_tokens: int = 0
    thoughts_tokens: int = 0

    input_text_tokens: int = 0
    input_audio_tokens: int = 0
    output_text_tokens: int = 0
    output_audio_tokens: int = 0

    audio_seconds: float = 0.0
    audio_minutes: float = 0.0
    audio_related_cost_usd: float = 0.0
    cost_per_audio_minute_usd: Optional[float] = None
    cost_per_event_usd: Optional[float] = None
    avg_latency_ms: Optional[float] = None

    priced_events: int = 0
    partial_events: int = 0
    unpriced_events: int = 0
    cost_coverage_percentage: float = 100.0


class UsageBreakdownItem(BaseModel):
    """Base fields for dimensional usage breakdown items."""
    total_events: int = 0
    total_cost_usd: float = 0.0
    percentage_of_total_cost: float = 0.0

    total_tokens: int = 0
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    cached_tokens: int = 0
    thoughts_tokens: int = 0

    audio_seconds: float = 0.0
    audio_minutes: float = 0.0
    audio_related_cost_usd: float = 0.0
    cost_per_audio_minute_usd: Optional[float] = None
    cost_per_event_usd: Optional[float] = None
    avg_latency_ms: Optional[float] = None

    priced_events: int = 0
    partial_events: int = 0
    unpriced_events: int = 0


class CompanyUsageBreakdown(UsageBreakdownItem):
    company_id: Optional[int] = None
    company_name: Optional[str] = None


class CompanyUsageResponse(BaseModel):
    period: str
    date_from: datetime
    date_to: datetime
    currency: str = "USD"
    total_cost_usd: float = 0.0
    items: List[CompanyUsageBreakdown] = Field(default_factory=list)


class ServiceUsageBreakdown(UsageBreakdownItem):
    service_id: Optional[int] = None
    service_name: Optional[str] = None
    company_id: Optional[int] = None
    company_name: Optional[str] = None


class ServiceUsageResponse(BaseModel):
    period: str
    date_from: datetime
    date_to: datetime
    currency: str = "USD"
    total_cost_usd: float = 0.0
    items: List[ServiceUsageBreakdown] = Field(default_factory=list)


class ModuleUsageBreakdown(UsageBreakdownItem):
    module: str
    functional_area: str


class ModuleUsageResponse(BaseModel):
    period: str
    date_from: datetime
    date_to: datetime
    currency: str = "USD"
    total_cost_usd: float = 0.0
    items: List[ModuleUsageBreakdown] = Field(default_factory=list)


class ModelUsageBreakdown(UsageBreakdownItem):
    model: str
    provider: str = "gemini"


class ModelUsageResponse(BaseModel):
    period: str
    date_from: datetime
    date_to: datetime
    currency: str = "USD"
    total_cost_usd: float = 0.0
    items: List[ModelUsageBreakdown] = Field(default_factory=list)


class UsageTimeseriesPoint(BaseModel):
    timestamp: datetime
    bucket_label: str
    group_key: Optional[str] = None
    group_id: Optional[str] = None

    total_events: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    cached_tokens: int = 0
    thoughts_tokens: int = 0

    audio_seconds: float = 0.0
    audio_minutes: float = 0.0
    audio_related_cost_usd: float = 0.0
    cost_per_audio_minute_usd: Optional[float] = None
    cost_per_event_usd: Optional[float] = None


class UsageTimeseriesResponse(BaseModel):
    period: str
    granularity: str
    group_by: str
    date_from: datetime
    date_to: datetime
    currency: str = "USD"
    total_cost_usd: float = 0.0
    points: List[UsageTimeseriesPoint] = Field(default_factory=list)
