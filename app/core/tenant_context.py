from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companies import Company
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.services import Service
from app.core.roles import InternalRole, normalize_role

class TenantContext(BaseModel):
    user_id: int
    user_email: Optional[str] = None
    raw_role: str
    normalized_role: InternalRole
    is_super_admin: bool
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    primary_service_id: Optional[int] = None
    primary_service_name: Optional[str] = None
    allowed_company_ids: List[int] = []
    allowed_service_ids: Optional[List[int]] = None  # None = sin restricción
    allowed_services: Optional[List[dict]] = None
    allowed_team_ids: Optional[List[int]] = None     # None = sin restricción
    allowed_agent_ids: Optional[List[str]] = None    # None = sin restricción (hubspot_owner_ids)

    class Config:
        from_attributes = True

    @classmethod
    async def build(cls, user: User, db: AsyncSession, company_override: Optional[int] = None) -> "TenantContext":
        norm_role = normalize_role(user.role)
        is_super = norm_role == InternalRole.SUPER_ADMIN

        # 1. Resolver company_id y company_name del usuario
        company_id = user.company_id
        company_name = None

        if is_super and company_override is not None:
            # Superadmin puede simular cualquier empresa
            company_id = company_override

        if company_id is not None:
            company_stmt = select(Company.company_name).where(Company.company_id == company_id)
            company_res = await db.execute(company_stmt)
            company_name = company_res.scalar()

        # 2. Resolver primary_service_id y primary_service_name
        primary_service_id = user.primary_service_id
        primary_service_name = None
        if primary_service_id is not None:
            p_stmt = select(Service.service_name).where(Service.service_id == primary_service_id)
            p_res = await db.execute(p_stmt)
            primary_service_name = p_res.scalar()

        # 3. Cargar allowed_company_ids
        allowed_company_ids: List[int] = []
        if is_super:
            all_comp_stmt = select(Company.company_id)
            all_comp_res = await db.execute(all_comp_stmt)
            allowed_company_ids = list(all_comp_res.scalars().all())
        else:
            if user.company_id is not None:
                allowed_company_ids = [user.company_id]

        # 4. Inicializar permisos por rol
        allowed_services: Optional[List[int]] = None
        allowed_teams: Optional[List[int]] = None
        allowed_agents: Optional[List[str]] = None

        if is_super:
            # Superadmin no tiene restricciones
            return cls(
                user_id=user.user_id,
                user_email=user.email,
                raw_role=user.role,
                normalized_role=norm_role,
                is_super_admin=True,
                company_id=company_id,
                company_name=company_name,
                primary_service_id=primary_service_id,
                primary_service_name=primary_service_name,
                allowed_company_ids=allowed_company_ids,
                allowed_service_ids=None,
                allowed_services=None,
                allowed_team_ids=None,
                allowed_agent_ids=None
            )

        if company_id is None:
            # Un usuario no-superadmin sin empresa asignada no tiene accesos
            return cls(
                user_id=user.user_id,
                user_email=user.email,
                raw_role=user.role,
                normalized_role=norm_role,
                is_super_admin=False,
                company_id=None,
                company_name=None,
                primary_service_id=primary_service_id,
                primary_service_name=primary_service_name,
                allowed_company_ids=[],
                allowed_service_ids=[],
                allowed_services=[],
                allowed_team_ids=[],
                allowed_agent_ids=[]
            )

        if norm_role == InternalRole.COMPANY_ADMIN:
            # Admin de empresa ve toda su empresa, sin restricciones internas
            pass

        elif norm_role == InternalRole.SERVICE_MANAGER:
            # Cargar servicios asignados
            svc_stmt = select(UserServiceAssociation.service_id).where(UserServiceAssociation.user_id == user.user_id)
            svc_res = await db.execute(svc_stmt)
            allowed_services = list(svc_res.scalars().all())
            if user.primary_service_id is not None and user.primary_service_id not in allowed_services:
                allowed_services.append(user.primary_service_id)

            # Fallback para service_manager sin servicios: usar el primer servicio activo de su empresa
            if not allowed_services and company_id is not None:
                fb_stmt = select(Service.service_id, Service.service_name).where(
                    Service.company_id == company_id,
                    Service.is_active == True
                ).order_by(Service.service_id.asc()).limit(1)
                fb_res = await db.execute(fb_stmt)
                fb_row = fb_res.first()
                if fb_row:
                    allowed_services = [fb_row.service_id]
                    if primary_service_id is None:
                        primary_service_id = fb_row.service_id
                        primary_service_name = fb_row.service_name

            # Cargar equipos asociados a esos servicios en su empresa
            if allowed_services:
                teams_stmt = select(Team.team_id).where(Team.service_id.in_(allowed_services) & (Team.company_id == company_id))
                teams_res = await db.execute(teams_stmt)
                allowed_teams = list(teams_res.scalars().all())
            else:
                allowed_teams = []

            # Service manager accede a todos los datos de sus servicios permitidos sin filtrar por agentes (hubspot_owner_id)
            allowed_agents = None

        elif norm_role == InternalRole.TEAM_COORDINATOR:
            # Cargar equipos asignados
            teams_stmt = select(UserTeamAssociation.team_id).where(UserTeamAssociation.user_id == user.user_id)
            teams_res = await db.execute(teams_stmt)
            allowed_teams = list(teams_res.scalars().all())

            # Cargar servicios de esos equipos
            svc_stmt = select(Team.service_id).where(Team.team_id.in_(allowed_teams))
            svc_res = await db.execute(svc_stmt)
            allowed_services = list(set(svc_res.scalars().all()))

            # Cargar servicios asignados directamente
            direct_svc_stmt = select(UserServiceAssociation.service_id).where(UserServiceAssociation.user_id == user.user_id)
            direct_svc_res = await db.execute(direct_svc_stmt)
            allowed_services = list(set(allowed_services + list(direct_svc_res.scalars().all())))
            if user.primary_service_id is not None and user.primary_service_id not in allowed_services:
                allowed_services.append(user.primary_service_id)

            # Fallback para team_coordinator sin servicios: usar el primer servicio activo de su empresa
            if not allowed_services and company_id is not None:
                fb_stmt = select(Service.service_id, Service.service_name).where(
                    Service.company_id == company_id,
                    Service.is_active == True
                ).order_by(Service.service_id.asc()).limit(1)
                fb_res = await db.execute(fb_stmt)
                fb_row = fb_res.first()
                if fb_row:
                    allowed_services = [fb_row.service_id]
                    if primary_service_id is None:
                        primary_service_id = fb_row.service_id
                        primary_service_name = fb_row.service_name

            # Cargar agentes de esos equipos
            agents_stmt = select(User.hubspot_owner_id).join(AgentTeamAssociation).where(AgentTeamAssociation.team_id.in_(allowed_teams))
            agents_res = await db.execute(agents_stmt)
            allowed_agents = [uid for uid in agents_res.scalars().all() if uid]
            if user.hubspot_owner_id:
                allowed_agents.append(user.hubspot_owner_id)
            allowed_agents = list(set(allowed_agents))

        elif norm_role == InternalRole.AGENT:
            # Cargar sus equipos
            teams_stmt = select(AgentTeamAssociation.team_id).where(AgentTeamAssociation.user_id == user.user_id)
            teams_res = await db.execute(teams_stmt)
            allowed_teams = list(teams_res.scalars().all())

            # Cargar servicios de sus equipos
            svc_stmt = select(Team.service_id).where(Team.team_id.in_(allowed_teams))
            svc_res = await db.execute(svc_stmt)
            allowed_services = list(set(svc_res.scalars().all()))

            # Solo accede a su propio hubspot_owner_id
            allowed_agents = [user.hubspot_owner_id] if user.hubspot_owner_id else []

        # Construir objetos dict para allowed_services si es una lista
        allowed_services_dicts: Optional[List[dict]] = None
        if allowed_services is not None:
            if allowed_services:
                svc_objs_stmt = select(Service.service_id, Service.service_name).where(Service.service_id.in_(allowed_services))
                svc_objs_res = await db.execute(svc_objs_stmt)
                allowed_services_dicts = [
                    {"service_id": row.service_id, "service_name": row.service_name}
                    for row in svc_objs_res.all()
                ]
            else:
                allowed_services_dicts = []

        return cls(
            user_id=user.user_id,
            user_email=user.email,
            raw_role=user.role,
            normalized_role=norm_role,
            is_super_admin=False,
            company_id=company_id,
            company_name=company_name,
            primary_service_id=primary_service_id,
            primary_service_name=primary_service_name,
            allowed_company_ids=allowed_company_ids,
            allowed_service_ids=allowed_services,
            allowed_services=allowed_services_dicts,
            allowed_team_ids=allowed_teams,
            allowed_agent_ids=allowed_agents
        )
