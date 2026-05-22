"""Pydantic schemas for service evolution."""
from pydantic import BaseModel
from typing import Any


class ServiceEvolutionFilters(BaseModel):
    service_id: int | None = None
    service_key: str | None = None
    service_name: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    granularity: str


class ServiceEvolutionSummary(BaseModel):
    total_calls: int
    avg_evaluacion_global: float | None = None
    avg_claridad: float | None = None
    avg_empatia: float | None = None
    avg_procedimiento: float | None = None
    cierre_cita_rate: float | None = None
    main_typology: str | None = None


class ServiceEvolutionSeriesItem(BaseModel):
    period: str
    service_id: int | None = None
    service_name: str | None = None
    total_calls: int
    avg_evaluacion_global: float | None = None
    avg_sentiment: float | None = None
    avg_empatia: float | None = None
    avg_simpatia: float | None = None
    avg_claridad: float | None = None
    avg_procedimiento: float | None = None
    avg_saludo_inicio: float | None = None
    avg_n3_preguntas: float | None = None
    avg_gestion_objeciones: float | None = None
    avg_propension: float | None = None
    cierre_cita_rate: float | None = None


class ServiceEvolutionTypologyItem(BaseModel):
    typology_key: str | None = None
    typology_name: str | None = None
    total_calls: int
    avg_evaluacion_global: float | None = None
    cierre_cita_rate: float | None = None


class ServiceEvolutionAgentItem(BaseModel):
    agent_owner_id: str | None = None
    agent_name: str | None = None
    total_calls: int
    avg_evaluacion_global: float | None = None
    avg_claridad: float | None = None
    cierre_cita_rate: float | None = None


class ServiceEvolutionCriteriaRankingItem(BaseModel):
    criterion_key: str
    criterion_name: str | None = None
    avg_value: float | None = None
    total_applicable: int


class ServiceEvolutionResponse(BaseModel):
    filters: ServiceEvolutionFilters
    summary: ServiceEvolutionSummary
    series: list[ServiceEvolutionSeriesItem]
    by_typology: list[ServiceEvolutionTypologyItem]
    by_agent: list[ServiceEvolutionAgentItem]
    criteria_ranking: list[ServiceEvolutionCriteriaRankingItem]


class ServiceListItem(BaseModel):
    service_id: int
    service_key: str
    service_name: str
    total_calls: int
    first_analysis_date: str | None = None
    last_analysis_date: str | None = None


class CriterionListItem(BaseModel):
    criterion_key: str
    criterion_name: str | None = None
    criterion_type: str | None = None
    total_applicable: int
