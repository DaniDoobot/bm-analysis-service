"""HubSpot owners mapping and resolution helper."""

OWNER_TO_NAME = {
    "1459417733": "Santiago Taboada Alvarez",
    "1375831790": "Luci Dos Santos Furtado",
    "1539993532": "Fernanda Rodrigues",
    "1375831787": "Roberto Galan Alvarez",
    "1375831791": "Eugenia Carreno",
    "33013277": "Bryan Herrera",
    "33013276": "Cristina Montenegro",
}

def resolve_owner_name(owner_id: str | int | None) -> str | None:
    if owner_id is None:
        return None
    return OWNER_TO_NAME.get(str(owner_id).strip())

def resolve_agent_display(agente_telefonico: str | None, hubspot_owner_id: str | int | None) -> str | None:
    # 1. Check hardcoded mapping
    resolved = resolve_owner_name(hubspot_owner_id)
    if resolved:
        return resolved

    # 2. Return agent name if it's not purely numeric
    if agente_telefonico:
        value = str(agente_telefonico).strip()
        if value and not value.isdigit():
            return value

    # 3. Fallback to owner ID
    if hubspot_owner_id:
        return str(hubspot_owner_id)

    # 4. Ultimate fallback
    return agente_telefonico
