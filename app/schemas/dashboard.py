"""Pydantic schemas for the agent comparison dashboard."""
from pydantic import BaseModel
from typing import Any

class ComparisonFilters(BaseModel):
    hubspot_owner_ids: list[str] | None = None
    service_id: int | None = None
    service_key: str | None = None
    typology_key: str | None = None
    period: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    bucket: str | None = None


class AgentBestAvg(BaseModel):
    hubspot_owner_id: str | None = None
    agent_initials: str | None = None
    avg_evaluacion_global: float | None = None


class AgentBestImprovement(BaseModel):
    hubspot_owner_id: str | None = None
    agent_initials: str | None = None
    delta_avg_evaluacion_global: float | None = None


class AgentHighestVolume(BaseModel):
    hubspot_owner_id: str | None = None
    agent_initials: str | None = None
    total_calls: int | None = None


class ComparisonSummary(BaseModel):
    agents_count: int
    total_calls: int
    best_agent_by_avg: AgentBestAvg | dict
    best_agent_by_improvement: AgentBestImprovement | dict
    highest_volume_agent: AgentHighestVolume | dict


class AvailableMetric(BaseModel):
    metric_key: str
    label: str
    type: str  # fixed | criterion
    value_type: str  # score | count | percentage | boolean
    criterion_key: str | None = None
    output_key: str | None = None


class AgentDeltaMetrics(BaseModel):
    avg_evaluacion_global: float | None = None
    total_calls: int | None = None
    cierre_cita_rate: float | None = None


class AgentComparisonMetrics(BaseModel):
    hubspot_owner_id: str
    agent_initials: str
    agent_name: str
    total_calls: int
    completed_calls: int
    avg_evaluacion_global: float | None = None
    avg_claridad: float | None = None
    avg_empatia: float | None = None
    avg_procedimiento: float | None = None
    cierre_cita_rate: float | None = None
    main_typology: str | None = None
    delta_vs_previous_period: AgentDeltaMetrics
    
    # New dynamic comparison fields
    selected_metric_key: str
    selected_metric_label: str
    selected_metric_avg: float | None = None
    selected_metric_count: int
    selected_metric_delta_vs_previous_period: float


class SeriesPoint(BaseModel):
    bucket: str
    total_calls: int
    avg_evaluacion_global: float | None = None
    avg_empatia: float | None = None
    avg_claridad: float | None = None
    avg_procedimiento: float | None = None
    cierre_cita_rate: float | None = None
    
    # New dynamic series point fields
    selected_metric_key: str | None = None
    selected_metric_label: str | None = None
    selected_metric_value: float | None = None


class AgentSeries(BaseModel):
    hubspot_owner_id: str
    agent_initials: str
    points: list[SeriesPoint]


class TypologyInfo(BaseModel):
    typology_key: str
    typology_name: str
    total_calls: int
    percentage: float


class AgentTypologyDistribution(BaseModel):
    hubspot_owner_id: str
    agent_initials: str
    typologies: list[TypologyInfo]


class CriterionInfo(BaseModel):
    criterion_key: str
    criterion_name: str
    avg_score: float | None = None
    count: int


class AgentCriteriaSummary(BaseModel):
    hubspot_owner_id: str
    agent_initials: str
    criteria: list[CriterionInfo]


class AgentComparisonResponse(BaseModel):
    filters: ComparisonFilters
    summary: ComparisonSummary
    agents: list[AgentComparisonMetrics]
    series: list[AgentSeries]
    typology_distribution_by_agent: list[AgentTypologyDistribution]
    criteria_summary_by_agent: list[AgentCriteriaSummary]
    available_metrics: list[AvailableMetric] = []
