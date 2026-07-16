from enum import Enum

class InternalRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    COMPANY_ADMIN = "company_admin"
    SERVICE_MANAGER = "service_manager"
    TEAM_COORDINATOR = "team_coordinator"
    AGENT = "agent"

ROLE_MAPPINGS = {
    "admin": InternalRole.SUPER_ADMIN,
    "administrador": InternalRole.SUPER_ADMIN,
    "superadmin": InternalRole.SUPER_ADMIN,
    "super_admin": InternalRole.SUPER_ADMIN,
    
    "company_admin": InternalRole.COMPANY_ADMIN,
    "admin_empresa": InternalRole.COMPANY_ADMIN,
    "administrador_empresa": InternalRole.COMPANY_ADMIN,
    
    "service_manager": InternalRole.SERVICE_MANAGER,
    "responsable_servicio": InternalRole.SERVICE_MANAGER,
    
    "team_coordinator": InternalRole.TEAM_COORDINATOR,
    "coordinador_equipo": InternalRole.TEAM_COORDINATOR,
    
    "agent": InternalRole.AGENT,
    "agente": InternalRole.AGENT,
    "user": InternalRole.AGENT,
    "usuario": InternalRole.AGENT,
}

def normalize_role(raw_role: str) -> InternalRole:
    if not raw_role:
        return InternalRole.AGENT
    val = raw_role.strip().lower()
    return ROLE_MAPPINGS.get(val, InternalRole.AGENT)
