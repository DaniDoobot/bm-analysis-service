"""HubSpot owners mapping and resolution helper."""

OWNER_TO_NAME = {
    "1459417733": "Santiago Taboada",
    "1375831790": "Luci Dos Santos Furtado",
    "1539993532": "Fernanda Rodrigues",
    "1375831787": "Roberto Galán",
    "1375831791": "Eugenia Carreno",
    "33013277": "Bryan Herrera",
    "33013276": "Cristina Montenegro",
}

AGENT_EMAIL_TO_OWNER_ID = {
    "santiago@bostonmedical.es": "1459417733",
    "santiago@bostonmedicalgroup.es": "1459417733",
    "santiago@bostonmedicalgroup.com": "1459417733",
    "santiago@gmail.com": "1459417733",
    "luci@bostonmedical.es": "1375831790",
    "luci@bostonmedicalgroup.es": "1375831790",
    "luci@bostonmedicalgroup.com": "1375831790",
    "luci@gmail.com": "1375831790",
    "fernanda@bostonmedical.es": "1539993532",
    "fernanda@bostonmedicalgroup.es": "1539993532",
    "fernanda@bostonmedicalgroup.com": "1539993532",
    "fernanda@gmail.com": "1539993532",
    "roberto@bostonmedical.es": "1375831787",
    "roberto@bostonmedicalgroup.es": "1375831787",
    "roberto@bostonmedicalgroup.com": "1375831787",
    "roberto@gmail.com": "1375831787",
    "eugenia@bostonmedical.es": "1375831791",
    "eugenia@bostonmedicalgroup.es": "1375831791",
    "eugenia@bostonmedicalgroup.com": "1375831791",
    "eugenia@gmail.com": "1375831791",
    "bryan@bostonmedical.es": "33013277",
    "bryan@bostonmedicalgroup.es": "33013277",
    "bryan@bostonmedicalgroup.com": "33013277",
    "bryan@gmail.com": "33013277",
    "cristina@bostonmedical.es": "33013276",
    "cristina@bostonmedicalgroup.es": "33013276",
    "cristina@bostonmedicalgroup.com": "33013276",
    "cristina@gmail.com": "33013276",
}

PREFIX_TO_OWNER_ID = {
    "santiago": "1459417733",
    "luci": "1375831790",
    "fernanda": "1539993532",
    "roberto": "1375831787",
    "eugenia": "1375831791",
    "bryan": "33013277",
    "cristina": "33013276",
}

def resolve_owner_id_by_email(email: str | None) -> str | None:
    if not email:
        return None
    cleaned = email.strip().lower()
    
    # 1. Try direct match
    if cleaned in AGENT_EMAIL_TO_OWNER_ID:
        return AGENT_EMAIL_TO_OWNER_ID[cleaned]
        
    # 2. Try prefix match if email contains '@'
    if "@" in cleaned:
        prefix = cleaned.split("@")[0]
        if prefix in PREFIX_TO_OWNER_ID:
            return PREFIX_TO_OWNER_ID[prefix]
            
    # 3. Try raw prefix match
    if cleaned in PREFIX_TO_OWNER_ID:
        return PREFIX_TO_OWNER_ID[cleaned]
        
    return None

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
        return f"Agente no identificado ({hubspot_owner_id})"

    # 4. Ultimate fallback
    return agente_telefonico
