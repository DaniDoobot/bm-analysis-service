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
    "companyadmin": InternalRole.COMPANY_ADMIN,
    "company_administrator": InternalRole.COMPANY_ADMIN,
    "admin_empresa": InternalRole.COMPANY_ADMIN,
    "admin_de_empresa": InternalRole.COMPANY_ADMIN,
    "administrador_empresa": InternalRole.COMPANY_ADMIN,
    "administrador_de_empresa": InternalRole.COMPANY_ADMIN,
    "administrador_de_la_empresa": InternalRole.COMPANY_ADMIN,
    
    "service_manager": InternalRole.SERVICE_MANAGER,
    "responsable_servicio": InternalRole.SERVICE_MANAGER,
    "responsable_de_servicio": InternalRole.SERVICE_MANAGER,
    "gestor_servicio": InternalRole.SERVICE_MANAGER,
    "gestor_de_servicio": InternalRole.SERVICE_MANAGER,
    
    "team_coordinator": InternalRole.TEAM_COORDINATOR,
    "coordinador_equipo": InternalRole.TEAM_COORDINATOR,
    "coordinador_de_equipo": InternalRole.TEAM_COORDINATOR,
    
    "agent": InternalRole.AGENT,
    "agente": InternalRole.AGENT,
    "user": InternalRole.AGENT,
    "usuario": InternalRole.AGENT,
}

def normalize_role(raw_role: str) -> InternalRole:
    if not raw_role:
        return InternalRole.AGENT
    val = raw_role.strip().lower()
    val_clean = val.replace(" ", "_").replace("-", "_")
    return (
        ROLE_MAPPINGS.get(val) or
        ROLE_MAPPINGS.get(val_clean) or
        InternalRole.AGENT
    )

DISALLOWED_CREATION_ROLES = {"user", "usuario", "usuario_operativo", "usuario operativo", "generic_user"}

def is_disallowed_creation_role(raw_role: str | None) -> bool:
    if not raw_role:
        return False
    val = raw_role.strip().lower()
    return val in DISALLOWED_CREATION_ROLES
