"""Pydantic schemas for Trainer Tasks request and response models."""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskItemCreate(BaseModel):
    simulation_id: int
    required_attempts: int = Field(..., ge=1, description="Número de ejecuciones requeridas (>= 1)")


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="Título de la tarea")
    description: Optional[str] = Field(None, description="Descripción opcional de la tarea")
    due_at: Optional[datetime] = Field(None, description="Fecha límite opcional. Por defecto null (Sin fecha límite)")
    items: List[TaskItemCreate] = Field(..., min_length=1, description="Lista de entrenamientos a incluir")
    agent_ids: List[str] = Field(..., min_length=1, description="Lista de hubspot_owner_id de los agentes asignados")

    @field_validator("items")
    @classmethod
    def validate_unique_simulations(cls, v: List[TaskItemCreate]) -> List[TaskItemCreate]:
        sim_ids = [it.simulation_id for it in v]
        if len(sim_ids) != len(set(sim_ids)):
            raise ValueError("No se pueden repetir entrenamientos dentro de la misma tarea.")
        return v

    @field_validator("agent_ids")
    @classmethod
    def validate_unique_agents(cls, v: List[str]) -> List[str]:
        cleaned = [a.strip() for a in v if a and a.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("No se pueden repetir agentes dentro de la misma tarea.")
        return cleaned


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    due_at: Optional[datetime] = None


class TaskItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_item_id: int
    task_id: int
    simulation_id: int
    simulation_name: str
    simulation_code: str
    required_attempts: int
    order_index: int = 0
    created_at: datetime


class TaskAssigneeItemProgress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_item_id: int
    simulation_id: int
    simulation_name: str
    simulation_code: str
    required_attempts: int
    completed_attempts: int = 0
    progress_percentage: float = 0.0
    is_completed: bool = False


class TaskAssigneeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_assignee_id: int
    task_id: int
    company_id: int
    hubspot_owner_id: str
    agent_name: Optional[str] = None
    team_name: Optional[str] = None
    assigned_at: datetime
    status: str  # pending, in_progress, completed, completed_late, cancelled
    effective_status: str  # derived: includes 'overdue'
    completed_at: Optional[datetime] = None
    total_completed_attempts: int = 0
    total_required_attempts: int = 0
    progress_percentage: float = 0.0
    items: List[TaskAssigneeItemProgress] = []


class TaskSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: int
    company_id: int
    service_id: int
    service_name: Optional[str] = None
    title: str
    description: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_name: Optional[str] = None
    due_at: Optional[datetime] = None
    status: str  # active, completed, cancelled
    effective_status: str  # derived: includes 'overdue'
    total_assignees: int = 0
    completed_assignees: int = 0
    progress_completed_attempts: int = 0
    progress_required_attempts: int = 0
    progress_percentage: float = 0.0
    created_at: datetime
    updated_at: datetime


class TaskDetailResponse(TaskSummaryResponse):
    items: List[TaskItemResponse] = []
    assignees: List[TaskAssigneeResponse] = []


class EligibleAgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hubspot_owner_id: str
    agent_name: str
    email: Optional[str] = None
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    service_id: int
    service_name: Optional[str] = None


class AgentTaskItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_item_id: int
    simulation_id: int
    simulation_name: str
    simulation_code: str
    required_attempts: int
    completed_attempts: int = 0
    progress_percentage: float = 0.0
    is_completed: bool = False


class AgentTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: int
    task_assignee_id: int
    title: str
    description: Optional[str] = None
    service_id: int
    service_name: Optional[str] = None
    assigned_at: datetime
    due_at: Optional[datetime] = None
    status: str
    effective_status: str
    completed_at: Optional[datetime] = None
    total_completed_attempts: int = 0
    total_required_attempts: int = 0
    progress_percentage: float = 0.0
    items: List[AgentTaskItemResponse] = []


class AgentResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: int
    simulation_id: int
    simulation_name: str
    simulation_code: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    evaluation_status: str
    score: Optional[float] = None
    summary: Optional[str] = None
    task_id: Optional[int] = None
    task_title: Optional[str] = None
    task_item_id: Optional[int] = None
    counted_for_task: bool = False


class PagedAgentResultsResponse(BaseModel):
    results: List[AgentResultResponse] = []
    total_count: int = 0
    limit: int = 50
    offset: int = 0
