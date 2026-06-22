"""Pydantic schemas for Analytics v2."""
from pydantic import BaseModel, Field

class AnalyticsItem(BaseModel):
    key: str = Field(..., description="Unique key identifier for the metric")
    label: str = Field(..., description="User-friendly display name of the metric")
    type: str = Field(..., description="Metric representation type: 'score' or 'percentage'")
    order: int = Field(..., description="Display order sequence index")
    default_selected: bool = Field(..., description="Whether this metric should be selected by default in UI charts")


class AgentInfo(BaseModel):
    hubspot_owner_id: str = Field(..., description="HubSpot Owner ID of the agent as a string")
    agent_name: str = Field(..., description="Full display name of the agent")


class AgentComparisonRow(BaseModel):
    hubspot_owner_id: str = Field(..., description="HubSpot Owner ID of the agent")
    agent_name: str = Field(..., description="Full display name of the agent")
    item_key: str = Field(..., description="Key identifier for the compared metric")
    item_label: str = Field(..., description="Display name of the compared metric")
    metric_type: str = Field(..., description="Type of the metric: 'score' or 'percentage'")
    value: float | None = Field(..., description="Average value computed for this agent and metric, or null if no valid data exists")
    count: int = Field(..., description="Total count of non-null evaluations used to calculate this metric value")


class AgentComparisonResponse(BaseModel):
    agents: list[AgentInfo] = Field(..., description="List of all available agents matching filters")
    items: list[AnalyticsItem] = Field(..., description="List of all compared metrics catalogue")
    comparison: list[AgentComparisonRow] = Field(..., description="Agent-by-agent metric comparison breakdown rows")


class EvolutionPoint(BaseModel):
    date: str = Field(..., description="Bucket date string (YYYY-MM-DD, YYYY-MM-DD HH:00, or YYYY-MM-DD for week start)")
    value: float | None = Field(..., description="Average metric value inside this bucket, or null if no data exists")
    count: int = Field(..., description="Count of non-null evaluations in this bucket")


class ItemEvolutionSeries(BaseModel):
    item_key: str = Field(..., description="Key identifier for this metric")
    item_label: str = Field(..., description="Display name of this metric")
    metric_type: str = Field(..., description="Type of this metric: 'score' or 'percentage'")
    points: list[EvolutionPoint] = Field(..., description="Chronological timeline points for this metric series")
