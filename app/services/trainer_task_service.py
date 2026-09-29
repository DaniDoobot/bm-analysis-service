"""Service module for Trainer Tasks business logic, FIFO attempt allocation, and progress aggregation."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException, status
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.roles import InternalRole
from app.core.tenant_context import TenantContext
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.trainer import (
    TrainerEvaluation,
    TrainerSession,
    TrainerSimulation,
    TrainerTask,
    TrainerTaskAssignee,
    TrainerTaskAttemptAllocation,
    TrainerTaskItem,
)
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.schemas.trainer_tasks import TaskCreate, TaskUpdate

logger = logging.getLogger(__name__)


class TrainerTaskService:

    @staticmethod
    async def get_eligible_agents(
        db: AsyncSession, service_id: int, context: TenantContext
    ) -> List[Dict[str, Any]]:
        """
        List all agents eligible for a given service within the actor's tenant and scope.
        Validates that the service belongs to the actor's company and allowed services.
        """
        stmt_svc = select(Service).where(Service.service_id == service_id)
        res_svc = await db.execute(stmt_svc)
        svc = res_svc.scalars().first()
        if not svc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Servicio con ID {service_id} no encontrado.",
            )

        if not context.is_super_admin:
            if svc.company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: El servicio pertenece a otra empresa.",
                )
            if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos sobre este servicio.",
                )

        company_id = svc.company_id

        # Collect candidate agent IDs from Users, UserServices, UserTeams, and TrainingAgentSettings
        agents_map: Dict[str, Dict[str, Any]] = {}

        # 1. Users belonging to company with primary_service_id == service_id
        stmt_users = (
            select(User)
            .options(selectinload(User.primary_team))
            .where(
                and_(
                    User.company_id == company_id,
                    User.is_active == True,
                    User.hubspot_owner_id.is_not(None),
                    or_(
                        User.primary_service_id == service_id,
                        User.user_id.in_(
                            select(UserServiceAssociation.user_id).where(
                                UserServiceAssociation.service_id == service_id
                            )
                        ),
                        User.user_id.in_(
                            select(UserTeamAssociation.user_id).join(
                                Team, Team.team_id == UserTeamAssociation.team_id
                            ).where(Team.service_id == service_id)
                        ),
                        User.user_id.in_(
                            select(AgentTeamAssociation.user_id).join(
                                Team, Team.team_id == AgentTeamAssociation.team_id
                            ).where(Team.service_id == service_id)
                        ),
                    ),
                )
            )
        )
        res_users = await db.execute(stmt_users)
        for u in res_users.scalars().all():
            owner_id = str(u.hubspot_owner_id).strip()
            if not owner_id:
                continue
            t_name = u.primary_team.team_name if u.primary_team else None
            t_id = u.primary_team_id
            full_name = getattr(u, "display_name", None) or getattr(u, "name", None) or u.username or owner_id
            agents_map[owner_id] = {
                "hubspot_owner_id": owner_id,
                "agent_name": full_name,
                "email": u.email,
                "team_id": t_id,
                "team_name": t_name,
                "service_id": service_id,
                "service_name": svc.service_name,
            }

        # 2. Enrich agent names from TrainingAgentSetting if available
        stmt_settings = select(TrainingAgentSetting).where(
            TrainingAgentSetting.company_id == company_id
        )
        res_settings = await db.execute(stmt_settings)
        for s in res_settings.scalars().all():
            owner_id = str(s.hubspot_owner_id).strip()
            if owner_id and owner_id in agents_map:
                if not agents_map[owner_id]["agent_name"] or agents_map[owner_id]["agent_name"] == owner_id:
                    agents_map[owner_id]["agent_name"] = s.agent_name

        # Filter by allowed_agent_ids if actor has restricted agent scope (e.g. team leader)
        if not context.is_super_admin and context.allowed_agent_ids is not None:
            allowed_set = set(context.allowed_agent_ids)
            agents_map = {k: v for k, v in agents_map.items() if k in allowed_set}

        return sorted(list(agents_map.values()), key=lambda x: x["agent_name"].lower())

    @staticmethod
    async def create_task(
        db: AsyncSession,
        payload: TaskCreate,
        context: TenantContext,
        current_user: Optional[User] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create a new Trainer Task with its items and assignees.
        Strictly enforces that all simulations belong to the same service and company,
        and that all assignees belong to that service and company.
        """
        if not payload.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La tarea debe contener al menos un entrenamiento (item).",
            )
        if not payload.agent_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La tarea debe estar asignada al menos a un agente.",
            )

        # 1. Fetch and validate all simulations
        sim_ids = [it.simulation_id for it in payload.items]
        stmt_sims = select(TrainerSimulation).where(TrainerSimulation.simulation_id.in_(sim_ids))
        res_sims = await db.execute(stmt_sims)
        sims = list(res_sims.scalars().all())

        if len(sims) != len(set(sim_ids)):
            found_ids = {s.simulation_id for s in sims}
            missing = set(sim_ids) - found_ids
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Los siguientes entrenamientos no existen: {sorted(list(missing))}",
            )

        # Ensure all simulations belong to the same company_id and service_id
        companies = {s.company_id for s in sims}
        services = {s.service_id for s in sims}

        if len(companies) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Todos los entrenamientos de una tarea deben pertenecer a la misma empresa.",
            )
        if len(services) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Todos los entrenamientos de una tarea deben pertenecer al mismo servicio.",
            )

        task_company_id = sims[0].company_id
        task_service_id = sims[0].service_id

        if task_company_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Los entrenamientos seleccionados no tienen una empresa asignada válida.",
            )

        # 2. Verify actor's scope on company and service
        if not context.is_super_admin:
            if task_company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: Los entrenamientos pertenecen a otra empresa.",
                )
            if context.allowed_service_ids is not None and task_service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos para gestionar tareas en este servicio.",
                )

        # 3. Check simulation status
        for s in sims:
            if s.status == "archived":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El entrenamiento '{s.name}' ({s.code}) está archivado y no puede asignarse.",
                )

        # 4. Validate all assignees belong to this company, this service, and the actor's scope
        eligible_agents = await TrainerTaskService.get_eligible_agents(
            db, task_service_id, context
        )
        eligible_map = {a["hubspot_owner_id"]: a for a in eligible_agents}

        clean_agent_ids = list(dict.fromkeys(str(a).strip() for a in payload.agent_ids if str(a).strip()))
        if not clean_agent_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes especificar al menos un agente válido.",
            )

        for aid in clean_agent_ids:
            if aid not in eligible_map:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"El agente '{aid}' no es elegible para esta tarea (no pertenece al servicio "
                        f"ID {task_service_id} de esta empresa o está fuera de tus permisos de supervisión)."
                    ),
                )

        # 5. Create TrainerTask
        task = TrainerTask(
            company_id=task_company_id,
            service_id=task_service_id,
            title=payload.title.strip(),
            description=payload.description.strip() if payload.description else None,
            due_at=payload.due_at,
            created_by_user_id=(current_user.user_id if current_user else None) or user_id or context.user_id,
            status="active",
        )
        db.add(task)
        await db.flush()

        # 6. Create Task Items
        seen_sims: Set[int] = set()
        for idx, item_in in enumerate(payload.items):
            if item_in.simulation_id in seen_sims:
                continue
            seen_sims.add(item_in.simulation_id)
            task_item = TrainerTaskItem(
                task_id=task.task_id,
                simulation_id=item_in.simulation_id,
                required_attempts=item_in.required_attempts,
                order_index=idx,
            )
            db.add(task_item)

        # 7. Create Task Assignees
        now_utc = datetime.now(timezone.utc)
        for aid in clean_agent_ids:
            assignee = TrainerTaskAssignee(
                task_id=task.task_id,
                company_id=task_company_id,
                hubspot_owner_id=aid,
                assigned_at=now_utc,
                status="pending",
            )
            db.add(assignee)

        await db.commit()
        await db.refresh(task)
        logger.info(
            "Created TrainerTask %d ('%s') with %d items and %d assignees.",
            task.task_id, task.title, len(seen_sims), len(clean_agent_ids)
        )
        return await TrainerTaskService.get_task_detail(db, task.task_id, context)

    @staticmethod
    async def list_tasks(
        db: AsyncSession,
        context: TenantContext,
        service_id: Optional[int] = None,
        team_id: Optional[int] = None,
        agent_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        status: Optional[str] = None,
        simulation_id: Optional[int] = None,
        due_status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        List Trainer tasks with aggregated progress, respecting multitenant security scoping.
        """
        filters = []

        # Multi-tenant scoping
        if not context.is_super_admin:
            filters.append(TrainerTask.company_id.in_(context.allowed_company_ids))
            if context.allowed_service_ids is not None:
                filters.append(TrainerTask.service_id.in_(context.allowed_service_ids))

        if service_id is not None:
            filters.append(TrainerTask.service_id == service_id)

        eff_status = status_filter or status
        if eff_status is not None:
            filters.append(TrainerTask.status == eff_status)

        now_utc = datetime.now(timezone.utc)
        if due_status == "with_deadline":
            filters.append(TrainerTask.due_at.is_not(None))
        elif due_status == "without_deadline":
            filters.append(TrainerTask.due_at.is_(None))
        elif due_status == "overdue":
            filters.append(
                and_(
                    TrainerTask.due_at.is_not(None),
                    TrainerTask.due_at < now_utc,
                    TrainerTask.status == "active",
                )
            )

        # Filters that require joining items
        if simulation_id is not None:
            filters.append(
                TrainerTask.task_id.in_(
                    select(TrainerTaskItem.task_id).where(
                        TrainerTaskItem.simulation_id == simulation_id
                    )
                )
            )

        # Filters that require joining assignees
        if agent_id is not None:
            filters.append(
                TrainerTask.task_id.in_(
                    select(TrainerTaskAssignee.task_id).where(
                        TrainerTaskAssignee.hubspot_owner_id == agent_id
                    )
                )
            )

        # Team leader restriction: only see tasks that have at least one assignee within allowed_agent_ids
        if not context.is_super_admin and context.allowed_agent_ids is not None:
            filters.append(
                TrainerTask.task_id.in_(
                    select(TrainerTaskAssignee.task_id).where(
                        TrainerTaskAssignee.hubspot_owner_id.in_(context.allowed_agent_ids)
                    )
                )
            )

        # Count total
        count_stmt = select(func.count(TrainerTask.task_id)).where(and_(*filters))
        total_res = await db.execute(count_stmt)
        total_count = total_res.scalar() or 0

        # Query tasks
        stmt = (
            select(TrainerTask)
            .options(
                selectinload(TrainerTask.service),
                selectinload(TrainerTask.created_by_user),
                selectinload(TrainerTask.items),
                selectinload(TrainerTask.assignees).selectinload(TrainerTaskAssignee.allocations),
            )
            .where(and_(*filters))
            .order_by(TrainerTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        res = await db.execute(stmt)
        tasks = list(res.scalars().all())

        results = []
        for t in tasks:
            # Aggregate assignee and attempt stats
            total_assignees = len(t.assignees)
            completed_assignees = sum(
                1 for a in t.assignees if a.status in ("completed", "completed_late")
            )

            # Sum total required attempts across all assignees
            per_agent_required = sum(item.required_attempts for item in t.items)
            progress_required_attempts = per_agent_required * total_assignees

            # Sum total completed attempts from all allocations
            progress_completed_attempts = 0
            for a in t.assignees:
                progress_completed_attempts += len(a.allocations)

            # Cap completed attempts for progress percentage calculation
            effective_completed = min(progress_completed_attempts, progress_required_attempts)
            pct = 0.0
            if progress_required_attempts > 0:
                pct = round((effective_completed / progress_required_attempts) * 100.0, 1)

            created_by_name = None
            if t.created_by_user:
                created_by_name = (
                    getattr(t.created_by_user, "display_name", None)
                    or getattr(t.created_by_user, "name", None)
                    or t.created_by_user.username
                )

            results.append({
                "task_id": t.task_id,
                "company_id": t.company_id,
                "service_id": t.service_id,
                "service_name": t.service.service_name if t.service else None,
                "title": t.title,
                "description": t.description,
                "created_by_user_id": t.created_by_user_id,
                "created_by_name": created_by_name,
                "due_at": t.due_at,
                "status": t.status,
                "effective_status": t.compute_effective_status(now_utc),
                "total_assignees": total_assignees,
                "completed_assignees": completed_assignees,
                "progress_completed_attempts": progress_completed_attempts,
                "progress_required_attempts": progress_required_attempts,
                "progress_percentage": pct,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            })

        return results, total_count

    @staticmethod
    async def get_task_detail(
        db: AsyncSession, task_id: int, context: TenantContext
    ) -> Dict[str, Any]:
        """
        Fetch full details of a Trainer task including per-item and per-assignee progress breakdown.
        """
        stmt = (
            select(TrainerTask)
            .options(
                selectinload(TrainerTask.service),
                selectinload(TrainerTask.created_by_user),
                selectinload(TrainerTask.items).selectinload(TrainerTaskItem.simulation),
                selectinload(TrainerTask.assignees).selectinload(TrainerTaskAssignee.allocations),
            )
            .where(TrainerTask.task_id == task_id)
        )
        res = await db.execute(stmt)
        task = res.scalars().first()
        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tarea con ID {task_id} no encontrada.",
            )

        # Security scope check
        if not context.is_super_admin:
            if task.company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: La tarea pertenece a otra empresa.",
                )
            if context.allowed_service_ids is not None and task.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos sobre este servicio.",
                )

        now_utc = datetime.now(timezone.utc)

        # Preload agent display names and teams
        agent_ids = [a.hubspot_owner_id for a in task.assignees]
        agent_names_map: Dict[str, Tuple[str, Optional[str]]] = {}
        if agent_ids:
            stmt_u = (
                select(User)
                .options(selectinload(User.primary_team))
                .where(
                    and_(
                        User.company_id == task.company_id,
                        User.hubspot_owner_id.in_(agent_ids),
                    )
                )
            )
            res_u = await db.execute(stmt_u)
            for u in res_u.scalars().all():
                fn = getattr(u, "display_name", None) or getattr(u, "name", None) or u.username or str(u.hubspot_owner_id)
                t_name = u.primary_team.team_name if u.primary_team else None
                agent_names_map[str(u.hubspot_owner_id)] = (fn, t_name)

            # Fallback for settings
            missing_owners = set(agent_ids) - set(agent_names_map.keys())
            if missing_owners:
                stmt_s = select(TrainingAgentSetting).where(
                    and_(
                        TrainingAgentSetting.company_id == task.company_id,
                        TrainingAgentSetting.hubspot_owner_id.in_(list(missing_owners)),
                    )
                )
                res_s = await db.execute(stmt_s)
                for s in res_s.scalars().all():
                    agent_names_map[str(s.hubspot_owner_id)] = (
                        s.agent_name or s.name or str(s.hubspot_owner_id),
                        s.team_name,
                    )

        # Format items
        formatted_items = []
        for it in sorted(task.items, key=lambda x: x.order_index):
            formatted_items.append({
                "task_item_id": it.task_item_id,
                "task_id": it.task_id,
                "simulation_id": it.simulation_id,
                "simulation_name": it.simulation.name if it.simulation else f"Simulación {it.simulation_id}",
                "simulation_code": it.simulation.code if it.simulation else "",
                "required_attempts": it.required_attempts,
                "order_index": it.order_index,
                "created_at": it.created_at,
            })

        # Format assignees
        formatted_assignees = []
        total_assignees = len(task.assignees)
        completed_assignees = 0
        total_req_all = sum(it["required_attempts"] for it in formatted_items) * total_assignees
        total_comp_all = 0

        for a in task.assignees:
            # If actor is team leader, skip assignees outside allowed_agent_ids
            if not context.is_super_admin and context.allowed_agent_ids is not None:
                if a.hubspot_owner_id not in context.allowed_agent_ids:
                    continue

            if a.status in ("completed", "completed_late"):
                completed_assignees += 1

            # Count allocations per task_item_id
            alloc_counts: Dict[int, int] = {}
            for alloc in a.allocations:
                alloc_counts[alloc.task_item_id] = alloc_counts.get(alloc.task_item_id, 0) + 1

            assignee_comp_attempts = len(a.allocations)
            total_comp_all += assignee_comp_attempts

            assignee_req_attempts = sum(it["required_attempts"] for it in formatted_items)
            pct_a = 0.0
            if assignee_req_attempts > 0:
                pct_a = round(min(assignee_comp_attempts / assignee_req_attempts * 100.0, 100.0), 1)

            # Per-item breakdown for this assignee
            a_items = []
            for it in formatted_items:
                c_count = alloc_counts.get(it["task_item_id"], 0)
                req_c = it["required_attempts"]
                it_pct = round(min(c_count / req_c * 100.0, 100.0), 1) if req_c > 0 else 0.0
                a_items.append({
                    "task_item_id": it["task_item_id"],
                    "simulation_id": it["simulation_id"],
                    "simulation_name": it["simulation_name"],
                    "simulation_code": it["simulation_code"],
                    "required_attempts": req_c,
                    "completed_attempts": c_count,
                    "progress_percentage": it_pct,
                    "is_completed": c_count >= req_c,
                })

            agent_display, team_display = agent_names_map.get(
                a.hubspot_owner_id, (a.hubspot_owner_id, None)
            )

            formatted_assignees.append({
                "task_assignee_id": a.task_assignee_id,
                "task_id": a.task_id,
                "company_id": a.company_id,
                "hubspot_owner_id": a.hubspot_owner_id,
                "agent_name": agent_display,
                "team_name": team_display,
                "assigned_at": a.assigned_at,
                "status": a.status,
                "effective_status": a.compute_effective_status(now_utc),
                "completed_at": a.completed_at,
                "total_completed_attempts": assignee_comp_attempts,
                "total_required_attempts": assignee_req_attempts,
                "progress_percentage": pct_a,
                "items": a_items,
                "items_progress": a_items,
            })

        global_pct = 0.0
        if total_req_all > 0:
            global_pct = round(min(total_comp_all / total_req_all * 100.0, 100.0), 1)

        created_by_name = None
        if task.created_by_user:
            created_by_name = (
                getattr(task.created_by_user, "display_name", None)
                or getattr(task.created_by_user, "name", None)
                or task.created_by_user.username
            )

        return {
            "task_id": task.task_id,
            "company_id": task.company_id,
            "service_id": task.service_id,
            "service_name": task.service.service_name if task.service else None,
            "title": task.title,
            "description": task.description,
            "created_by_user_id": task.created_by_user_id,
            "created_by_name": created_by_name,
            "due_at": task.due_at,
            "status": task.status,
            "effective_status": task.compute_effective_status(now_utc),
            "total_assignees": total_assignees,
            "completed_assignees": completed_assignees,
            "progress_completed_attempts": total_comp_all,
            "progress_required_attempts": total_req_all,
            "progress_percentage": global_pct,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "items": formatted_items,
            "assignees": formatted_assignees,
        }

    @staticmethod
    async def patch_task(
        db: AsyncSession, task_id: int, payload: TaskUpdate, context: TenantContext
    ) -> Dict[str, Any]:
        """
        Safely update non-structural task metadata (title, description, due_at).
        Does NOT alter simulation items or assignees to preserve historical audit integrity.
        """
        stmt = select(TrainerTask).where(TrainerTask.task_id == task_id)
        res = await db.execute(stmt)
        task = res.scalars().first()
        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tarea con ID {task_id} no encontrada.",
            )

        if not context.is_super_admin:
            if task.company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: La tarea pertenece a otra empresa.",
                )
            if context.allowed_service_ids is not None and task.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos sobre este servicio.",
                )

        if payload.title is not None:
            clean_title = payload.title.strip()
            if not clean_title:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El título no puede estar vacío.",
                )
            task.title = clean_title

        if payload.description is not None:
            task.description = payload.description.strip() if payload.description else None

        # Handle due_at: can be updated or reset to None
        update_dict = payload.model_dump(exclude_unset=True)
        if "due_at" in update_dict:
            task.due_at = payload.due_at

        task.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return await TrainerTaskService.get_task_detail(db, task_id, context)

    @staticmethod
    async def cancel_task(
        db: AsyncSession, task_id: int, context: TenantContext
    ) -> Dict[str, Any]:
        """
        Cancel a Trainer task and all its non-terminal assignees.
        Preserves all completed attempts and historical records without physical deletion.
        """
        stmt = (
            select(TrainerTask)
            .options(selectinload(TrainerTask.assignees))
            .where(TrainerTask.task_id == task_id)
        )
        res = await db.execute(stmt)
        task = res.scalars().first()
        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tarea con ID {task_id} no encontrada.",
            )

        if not context.is_super_admin:
            if task.company_id not in context.allowed_company_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: La tarea pertenece a otra empresa.",
                )
            if context.allowed_service_ids is not None and task.service_id not in context.allowed_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Acceso denegado: No tienes permisos sobre este servicio.",
                )

        task.status = "cancelled"
        now_utc = datetime.now(timezone.utc)
        task.updated_at = now_utc

        # Cancel non-completed assignees
        for a in task.assignees:
            if a.status in ("pending", "in_progress"):
                a.status = "cancelled"
                a.updated_at = now_utc

        await db.commit()
        logger.info("Cancelled TrainerTask %d.", task_id)
        return await TrainerTaskService.get_task_detail(db, task_id, context)

    @staticmethod
    async def get_agent_tasks(
        db: AsyncSession, hubspot_owner_id: str, company_id: int
    ) -> List[Dict[str, Any]]:
        """
        Fetch all assigned tasks for the authenticated agent ("Mis entrenamientos").
        Sorted cleanly: active/pending/overdue first, nearest due_at first, then completed/cancelled.
        """
        now_utc = datetime.now(timezone.utc)
        stmt = (
            select(TrainerTaskAssignee)
            .options(
                selectinload(TrainerTaskAssignee.task).selectinload(TrainerTask.service),
                selectinload(TrainerTaskAssignee.task)
                .selectinload(TrainerTask.items)
                .selectinload(TrainerTaskItem.simulation),
                selectinload(TrainerTaskAssignee.allocations),
            )
            .where(
                and_(
                    TrainerTaskAssignee.company_id == company_id,
                    TrainerTaskAssignee.hubspot_owner_id == hubspot_owner_id,
                )
            )
        )
        res = await db.execute(stmt)
        assignees = list(res.scalars().all())

        tasks_out = []
        for a in assignees:
            task = a.task
            if not task:
                continue

            # Count allocations per task_item_id
            alloc_counts: Dict[int, int] = {}
            for alloc in a.allocations:
                alloc_counts[alloc.task_item_id] = alloc_counts.get(alloc.task_item_id, 0) + 1

            total_comp = len(a.allocations)
            total_req = sum(it.required_attempts for it in task.items)
            pct = round(min(total_comp / total_req * 100.0, 100.0), 1) if total_req > 0 else 0.0

            items_out = []
            for it in sorted(task.items, key=lambda x: x.order_index):
                c_count = alloc_counts.get(it.task_item_id, 0)
                req_c = it.required_attempts
                it_pct = round(min(c_count / req_c * 100.0, 100.0), 1) if req_c > 0 else 0.0
                items_out.append({
                    "task_item_id": it.task_item_id,
                    "simulation_id": it.simulation_id,
                    "simulation_name": it.simulation.name if it.simulation else f"Simulación {it.simulation_id}",
                    "simulation_code": it.simulation.code if it.simulation else "",
                    "required_attempts": req_c,
                    "completed_attempts": c_count,
                    "progress_percentage": it_pct,
                    "is_completed": c_count >= req_c,
                })

            eff_status = a.compute_effective_status(now_utc)

            # Assign sorting rank
            # 1: overdue, in_progress, pending
            # 2: completed, completed_late
            # 3: cancelled
            if eff_status in ("overdue", "in_progress", "pending"):
                rank = 1
            elif eff_status in ("completed", "completed_late"):
                rank = 2
            else:
                rank = 3

            tasks_out.append({
                "task_id": task.task_id,
                "task_assignee_id": a.task_assignee_id,
                "title": task.title,
                "description": task.description,
                "service_id": task.service_id,
                "service_name": task.service.service_name if task.service else None,
                "assigned_at": a.assigned_at,
                "due_at": task.due_at,
                "status": a.status,
                "effective_status": eff_status,
                "completed_at": a.completed_at,
                "total_completed_attempts": total_comp,
                "total_required_attempts": total_req,
                "progress_percentage": pct,
                "items": items_out,
                "_sort_rank": rank,
                "_due_at_sort": task.due_at or datetime.max.replace(tzinfo=timezone.utc),
            })

        # Sort: rank ASC, due_at ASC, assigned_at DESC
        tasks_out.sort(key=lambda x: (x["_sort_rank"], x["_due_at_sort"], -x["assigned_at"].timestamp()))
        for t in tasks_out:
            t.pop("_sort_rank", None)
            t.pop("_due_at_sort", None)

        return tasks_out

    @staticmethod
    async def get_agent_results(
        db: AsyncSession, hubspot_owner_id: str, company_id: int, limit: int = 50, offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Fetch completed execution history for an agent ("Mis resultados de Trainer"),
        enriched with task metadata if an attempt was allocated to a task.
        """
        # Count total sessions for this agent
        count_stmt = select(func.count(TrainerSession.session_id)).where(
            and_(
                TrainerSession.agent_id == hubspot_owner_id,
                TrainerSession.company_id == company_id,
                TrainerSession.status == "completed",
            )
        )
        total_res = await db.execute(count_stmt)
        total_count = total_res.scalar() or 0

        # Query sessions with evaluation and optional allocation
        stmt = (
            select(
                TrainerSession,
                TrainerEvaluation,
                TrainerSimulation,
                TrainerTaskAttemptAllocation,
                TrainerTask,
            )
            .outerjoin(TrainerEvaluation, TrainerSession.session_id == TrainerEvaluation.session_id)
            .outerjoin(TrainerSimulation, TrainerSession.simulation_id == TrainerSimulation.simulation_id)
            .outerjoin(
                TrainerTaskAttemptAllocation,
                TrainerSession.session_id == TrainerTaskAttemptAllocation.session_id,
            )
            .outerjoin(
                TrainerTaskAssignee,
                TrainerTaskAttemptAllocation.task_assignee_id == TrainerTaskAssignee.task_assignee_id,
            )
            .outerjoin(TrainerTask, TrainerTaskAssignee.task_id == TrainerTask.task_id)
            .where(
                and_(
                    TrainerSession.agent_id == hubspot_owner_id,
                    TrainerSession.company_id == company_id,
                    TrainerSession.status == "completed",
                )
            )
            .order_by(TrainerSession.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        res = await db.execute(stmt)
        rows = res.all()

        results = []
        for sess, evaluation, simulation, allocation, task in rows:
            score_val = float(evaluation.score) if evaluation and evaluation.score is not None else None
            summary_val = evaluation.summary if evaluation else None
            sim_name = simulation.name if simulation else f"Simulación {sess.simulation_id}"
            sim_code = simulation.code if simulation else None

            results.append({
                "session_id": sess.session_id,
                "simulation_id": sess.simulation_id,
                "simulation_name": sim_name,
                "simulation_code": sim_code,
                "started_at": sess.started_at,
                "ended_at": sess.ended_at,
                "evaluation_status": sess.evaluation_status,
                "score": score_val,
                "summary": summary_val,
                "task_id": task.task_id if task else None,
                "task_title": task.title if task else None,
                "task_item_id": allocation.task_item_id if allocation else None,
                "counted_for_task": allocation is not None,
            })

        return results, total_count

    # ── FIFO Attempt Allocator ──────────────────────────────────────────────────

    @staticmethod
    async def allocate_completed_attempt(
        db: AsyncSession, session_id: int
    ) -> Optional[TrainerTaskAttemptAllocation]:
        """
        Central FIFO allocator for completed Trainer sessions.
        Idempotent, concurrency-safe with row-level locks (SELECT FOR UPDATE).
        """
        # 1. Check idempotency: if already allocated, return immediately
        stmt_check = select(TrainerTaskAttemptAllocation).where(
            TrainerTaskAttemptAllocation.session_id == session_id
        )
        res_check = await db.execute(stmt_check)
        existing = res_check.scalars().first()
        if existing:
            logger.info("Session %d is already allocated to task item %d. Skipping.", session_id, existing.task_item_id)
            return existing

        # 2. Fetch session and evaluation details
        stmt_sess = (
            select(TrainerSession)
            .options(selectinload(TrainerSession.evaluation))
            .where(TrainerSession.session_id == session_id)
        )
        res_sess = await db.execute(stmt_sess)
        sess = res_sess.scalars().first()
        if not sess:
            logger.warning("Session %d not found for task attempt allocation.", session_id)
            return None

        # Verify session is countable
        if sess.status != "completed":
            logger.info("Session %d status is '%s' (not 'completed'). Not allocating.", session_id, sess.status)
            return None

        if sess.evaluation_status not in ("evaluated", "completed_without_score"):
            logger.info("Session %d evaluation_status is '%s'. Not allocating.", session_id, sess.evaluation_status)
            return None

        if not sess.evaluation:
            logger.info("Session %d has no TrainerEvaluation. Not allocating.", session_id)
            return None

        completed_at = sess.ended_at or sess.updated_at or datetime.now(timezone.utc)

        # 3. Query candidate active assignees in FIFO order with row-level lock
        stmt_candidates = (
            select(TrainerTaskAssignee, TrainerTaskItem, TrainerTask)
            .join(TrainerTask, TrainerTask.task_id == TrainerTaskAssignee.task_id)
            .join(
                TrainerTaskItem,
                and_(
                    TrainerTaskItem.task_id == TrainerTask.task_id,
                    TrainerTaskItem.simulation_id == sess.simulation_id,
                ),
            )
            .where(
                and_(
                    TrainerTaskAssignee.company_id == sess.company_id,
                    TrainerTaskAssignee.hubspot_owner_id == sess.agent_id,
                    TrainerTask.service_id == sess.service_id,
                    TrainerTask.status == "active",
                    TrainerTaskAssignee.status.in_(["pending", "in_progress"]),
                    TrainerTaskAssignee.assigned_at <= completed_at,
                )
            )
            .order_by(
                TrainerTaskAssignee.assigned_at.asc(),
                TrainerTaskAssignee.task_assignee_id.asc(),
                TrainerTaskItem.task_item_id.asc(),
            )
            .with_for_update(of=TrainerTaskAssignee)
        )
        res_candidates = await db.execute(stmt_candidates)
        candidates = res_candidates.all()

        target_assignee: Optional[TrainerTaskAssignee] = None
        target_item: Optional[TrainerTaskItem] = None
        target_task: Optional[TrainerTask] = None

        for assignee, item, task in candidates:
            # Re-count allocations for this specific (assignee, item) inside the transaction
            stmt_cnt = select(func.count(TrainerTaskAttemptAllocation.allocation_id)).where(
                and_(
                    TrainerTaskAttemptAllocation.task_assignee_id == assignee.task_assignee_id,
                    TrainerTaskAttemptAllocation.task_item_id == item.task_item_id,
                )
            )
            rc = await db.execute(stmt_cnt)
            current_allocations = rc.scalar() or 0

            if current_allocations < item.required_attempts:
                target_assignee = assignee
                target_item = item
                target_task = task
                break

        if not target_assignee or not target_item or not target_task:
            logger.info(
                "No candidate task waiting for simulation %d for agent %s in service %d. No allocation created.",
                sess.simulation_id, sess.agent_id, sess.service_id
            )
            return None

        # 4. Insert allocation
        allocation = TrainerTaskAttemptAllocation(
            task_assignee_id=target_assignee.task_assignee_id,
            task_item_id=target_item.task_item_id,
            session_id=sess.session_id,
            allocated_at=datetime.now(timezone.utc),
        )
        db.add(allocation)
        await db.flush()

        # 5. Progress assignee status: pending -> in_progress
        if target_assignee.status == "pending":
            target_assignee.status = "in_progress"

        # 6. Check if ALL items of target_assignee are now completed
        stmt_all_items = select(TrainerTaskItem).where(
            TrainerTaskItem.task_id == target_task.task_id
        )
        res_all_items = await db.execute(stmt_all_items)
        all_items = res_all_items.scalars().all()

        all_completed = True
        for it in all_items:
            stmt_c = select(func.count(TrainerTaskAttemptAllocation.allocation_id)).where(
                and_(
                    TrainerTaskAttemptAllocation.task_assignee_id == target_assignee.task_assignee_id,
                    TrainerTaskAttemptAllocation.task_item_id == it.task_item_id,
                )
            )
            rc = await db.execute(stmt_c)
            it_count = rc.scalar() or 0
            if it_count < it.required_attempts:
                all_completed = False
                break

        if all_completed:
            is_late = False
            if target_task.due_at:
                due = target_task.due_at if target_task.due_at.tzinfo else target_task.due_at.replace(tzinfo=timezone.utc)
                comp = completed_at if completed_at.tzinfo else completed_at.replace(tzinfo=timezone.utc)
                if comp > due:
                    is_late = True
            target_assignee.status = "completed_late" if is_late else "completed"
            target_assignee.completed_at = completed_at
            logger.info(
                "Assignee %d (agent %s) completed all items for task %d (status: %s).",
                target_assignee.task_assignee_id, target_assignee.hubspot_owner_id,
                target_task.task_id, target_assignee.status
            )

        # 7. Check if ALL non-cancelled assignees of target_task are finished
        stmt_all_assignees = select(TrainerTaskAssignee).where(
            TrainerTaskAssignee.task_id == target_task.task_id
        )
        res_all_assignees = await db.execute(stmt_all_assignees)
        all_assignees = res_all_assignees.scalars().all()

        non_cancelled = [a for a in all_assignees if a.status != "cancelled"]
        if non_cancelled and all(a.status in ("completed", "completed_late") for a in non_cancelled):
            target_task.status = "completed"
            logger.info("TrainerTask %d marked as completed (all assignees finished).", target_task.task_id)

        target_assignee.updated_at = datetime.now(timezone.utc)
        target_task.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(allocation)
        return allocation
