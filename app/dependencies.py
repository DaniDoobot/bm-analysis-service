"""
FastAPI dependency injectors.
"""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session and close it after the request."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from app.models.users import User
from app.utils.security import decode_access_token

security_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_bearer),
    db: AsyncSession = Depends(get_db)
) -> User:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Se requiere autenticación Bearer token."
        )
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload or "user_id" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado."
        )
    
    stmt = select(User).where(User.user_id == payload["user_id"])
    result = await db.execute(stmt)
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado."
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario inactivo."
        )
    if "email" in payload and payload["email"].strip().lower() != user.email.lower():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La sesión ha expirado porque se ha cambiado el correo electrónico."
        )
    return user


async def require_admin(
    current_user: User = Depends(get_current_user)
) -> User:
    """Require authenticated user with role 'administrador' or 'admin'."""
    if current_user.role not in {"administrador", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol administrador para esta operación."
        )
    return current_user


# ── Multi-tenant Context and Permission Dependencies ──────────────────────────
from fastapi import Header
from typing import Optional
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole

async def get_tenant_context(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    x_company_override: Optional[int] = Header(None, alias="X-Company-ID")
) -> TenantContext:
    """FastAPI dependency to build and inject the UserAccessContext / TenantContext."""
    return await TenantContext.build(current_user, db, company_override=x_company_override)


async def require_super_admin(
    context: TenantContext = Depends(get_tenant_context)
) -> TenantContext:
    """Dependency ensuring the user is a superadmin."""
    if not context.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Super Administrador."
        )
    return context


async def require_company_admin_or_super_admin(
    context: TenantContext = Depends(get_tenant_context)
) -> TenantContext:
    """Dependency ensuring the user is a company admin or superadmin."""
    if not context.is_super_admin and context.normalized_role != InternalRole.COMPANY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: Se requiere rol Administrador de Empresa o Super Administrador."
        )
    return context


class RequireCompanyAccess:
    """Dependency parameter to validate access to a specific company_id path parameter."""
    def __init__(self, check_write: bool = False):
        self.check_write = check_write

    async def __call__(
        self,
        company_id: int,
        context: TenantContext = Depends(get_tenant_context)
    ) -> TenantContext:
        if context.is_super_admin:
            return context
        if company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a esta empresa."
            )
        if self.check_write and context.normalized_role not in (InternalRole.COMPANY_ADMIN,):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: Se requiere rol Administrador de Empresa para escribir."
            )
        return context


class RequireServiceAccess:
    """Dependency parameter to validate access to a specific service_id path parameter."""
    def __init__(self, check_write: bool = False):
        self.check_write = check_write

    async def __call__(
        self,
        service_id: int,
        context: TenantContext = Depends(get_tenant_context)
    ) -> TenantContext:
        if context.is_super_admin:
            return context
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a este servicio."
            )
        if self.check_write and context.normalized_role not in (InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos de escritura para este servicio."
            )
        return context


class RequireTeamAccess:
    """Dependency parameter to validate access to a specific team_id path parameter."""
    def __init__(self, check_write: bool = False):
        self.check_write = check_write

    async def __call__(
        self,
        team_id: int,
        context: TenantContext = Depends(get_tenant_context)
    ) -> TenantContext:
        if context.is_super_admin:
            return context
        if context.allowed_team_ids is not None and team_id not in context.allowed_team_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos para acceder a este equipo."
            )
        if self.check_write and context.normalized_role not in (InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso denegado: No tienes permisos de escritura para este equipo."
            )
        return context

