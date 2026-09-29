"""FastAPI APIRouter for Trainer Tasks (assignments, task tracking, eligible agents, and agent views)."""
import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import InternalRole, normalize_role
from app.core.tenant_context import TenantContext
from app.dependencies import get_current_user, get_db, get_tenant_context
from app.models.users import User
from app.schemas.trainer_tasks import (
    AgentResultResponse,
    AgentTaskResponse,
    EligibleAgentResponse,
    PagedAgentResultsResponse,
    TaskCreate,
    TaskDetailResponse,
    TaskSummaryResponse,
    TaskUpdate,
)
from app.services.trainer_task_service import TrainerTaskService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bm/trainer", tags=["Trainer Tasks"])


def _enforce_task_management_role(context: TenantContext):
    """Ensure user has manager, admin or team leader role to manage tasks."""
    if context.normalized_role == InternalRole.AGENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador, responsable o coordinador de equipo para gestionar tareas.",
        )


# ── Admin & Manager Endpoints ───────────────────────────────────────────────

@router.get(
    "/tasks/eligible-agents",
    response_model=List[EligibleAgentResponse],
    summary="Obtener agentes elegibles para un servicio específico",
)
async def list_eligible_agents(
    service_id: int = Query(..., description="ID del servicio para el cual listar agentes"),
    context: Annotated[TenantContext, Depends(get_tenant_context)] = None,
    db: AsyncSession = Depends(get_db),
):
    """Devuelve la lista de agentes elegibles para un servicio respetando los scopes del usuario."""
    _enforce_task_management_role(context)
    agents = await TrainerTaskService.get_eligible_agents(db, service_id, context)
    return agents


@router.post(
    "/tasks",
    response_model=TaskDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una nueva tarea de entrenamiento y asignarla a agentes",
)
async def create_task(
    payload: TaskCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Crea una tarea con uno o más entrenamientos y la asigna a uno o más agentes del servicio."""
    _enforce_task_management_role(context)
    return await TrainerTaskService.create_task(db, payload, context, current_user)


@router.get(
    "/tasks",
    response_model=List[TaskSummaryResponse],
    summary="Listar tareas de entrenamiento con agregaciones de progreso",
)
async def list_tasks(
    service_id: Optional[int] = Query(None, description="Filtrar por service_id"),
    team_id: Optional[int] = Query(None, description="Filtrar por team_id"),
    agent_id: Optional[str] = Query(None, description="Filtrar por hubspot_owner_id del agente"),
    status: Optional[str] = Query(None, description="Filtrar por estado ('active', 'completed', 'cancelled')"),
    simulation_id: Optional[int] = Query(None, description="Filtrar por ID de simulación"),
    due_status: Optional[str] = Query(
        None, description="Filtrar por vencimiento ('with_deadline', 'without_deadline', 'overdue')"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    context: Annotated[TenantContext, Depends(get_tenant_context)] = None,
    db: AsyncSession = Depends(get_db),
):
    """Lista las tareas accesibles para el usuario con progreso consolidado."""
    _enforce_task_management_role(context)
    tasks, _ = await TrainerTaskService.list_tasks(
        db=db,
        context=context,
        service_id=service_id,
        team_id=team_id,
        agent_id=agent_id,
        status_filter=status,
        simulation_id=simulation_id,
        due_status=due_status,
        limit=limit,
        offset=offset,
    )
    return tasks


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    summary="Obtener detalle de una tarea con desglose por item y agente",
)
async def get_task_detail(
    task_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Devuelve el detalle completo de la tarea con items y progreso por agente asignado."""
    _enforce_task_management_role(context)
    return await TrainerTaskService.get_task_detail(db, task_id, context)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    summary="Actualizar metadatos de la tarea (título, descripción, fecha límite)",
)
async def patch_task(
    task_id: int,
    payload: TaskUpdate,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Actualiza metadatos no estructurales sin alterar asignaciones ni histórico."""
    _enforce_task_management_role(context)
    return await TrainerTaskService.patch_task(db, task_id, payload, context)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskDetailResponse,
    summary="Cancelar una tarea de entrenamiento",
)
async def cancel_task(
    task_id: int,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Cancela una tarea y sus asignaciones activas, preservando el histórico y las evaluaciones realizadas."""
    _enforce_task_management_role(context)
    return await TrainerTaskService.cancel_task(db, task_id, context)


# ── Agent Endpoints ("Mis entrenamientos" y "Mis resultados") ───────────────

@router.get(
    "/me/tasks",
    response_model=List[AgentTaskResponse],
    summary="Mis entrenamientos: tareas asignadas al agente autenticado",
)
async def get_my_tasks(
    current_user: Annotated[User, Depends(get_current_user)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
):
    """Devuelve las tareas asignadas exclusivamente al agente autenticado."""
    owner_id = current_user.hubspot_owner_id
    if not owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún identificador de agente (HubSpot Owner ID).",
        )
    company_id = context.company_id or current_user.company_id
    if not company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pudo resolver la empresa asociada a tu cuenta.",
        )

    return await TrainerTaskService.get_agent_tasks(db, owner_id, company_id)


@router.get(
    "/me/results",
    response_model=PagedAgentResultsResponse,
    summary="Mis resultados: histórico de simulaciones del agente con metadata de tarea",
)
async def get_my_results(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: Annotated[User, Depends(get_current_user)] = None,
    context: Annotated[TenantContext, Depends(get_tenant_context)] = None,
    db: AsyncSession = Depends(get_db),
):
    """Devuelve el histórico de sesiones evaluadas del agente, indicando si contaron para una tarea."""
    owner_id = current_user.hubspot_owner_id
    if not owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tu cuenta de usuario no está asociada a ningún identificador de agente (HubSpot Owner ID).",
        )
    company_id = context.company_id or current_user.company_id
    if not company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se pudo resolver la empresa asociada a tu cuenta.",
        )

    results, total_count = await TrainerTaskService.get_agent_results(
        db=db, hubspot_owner_id=owner_id, company_id=company_id, limit=limit, offset=offset
    )
    return {
        "results": results,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }
