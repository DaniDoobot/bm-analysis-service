"""HubSpot owners mapping and resolution helper."""
from typing import Any

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

def is_placeholder_agent_name(agent_name: str | None) -> bool:
    """Return True if agent_name is None, empty, or an 'Agente no identificado (...)' placeholder."""
    if not agent_name or not str(agent_name).strip():
        return True
    s = str(agent_name).strip()
    return s.startswith("Agente no identificado")

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


async def resolve_agent_name_canonical(
    db: Any,
    hubspot_owner_id: str | int | None,
    company_id: int | None = None,
    raw_agent: str | None = None,
    cache: dict[tuple[int | None, str], str] | None = None,
) -> str | None:
    """
    Resolves agent name following canonical priority:
    1. bm_users by hubspot_owner_id within company_id (active user).
    2. Legacy OWNER_TO_NAME fallback.
    3. raw_agent if non-numeric and not a placeholder.
    4. "Agente no identificado ({owner_id})".
    """
    clean_oid = str(hubspot_owner_id).strip() if hubspot_owner_id is not None else None

    if clean_oid:
        # Check cache if provided
        if cache is not None:
            if (company_id, clean_oid) in cache:
                return cache[(company_id, clean_oid)]
            if (None, clean_oid) in cache and company_id is None:
                return cache[(None, clean_oid)]

        # 1. Query bm_users
        from app.models.users import User
        from sqlalchemy import select, and_, or_

        stmt = select(User.name, User.username).where(
            User.hubspot_owner_id == clean_oid,
        )
        if company_id is not None:
            stmt = stmt.where(User.company_id == company_id)

        stmt = stmt.order_by(User.user_id.asc()).limit(1)
        res = await db.execute(stmt)
        user_row = res.first()
        if user_row:
            name, username = user_row
            disp = (name and name.strip()) or (username and username.strip())
            if disp:
                if cache is not None:
                    cache[(company_id, clean_oid)] = disp
                return disp

        # 2. Fallback to legacy OWNER_TO_NAME
        if clean_oid in OWNER_TO_NAME:
            legacy_name = OWNER_TO_NAME[clean_oid]
            if cache is not None:
                cache[(company_id, clean_oid)] = legacy_name
            return legacy_name

    # 3. raw_agent if valid text
    if raw_agent:
        s = str(raw_agent).strip()
        if s and not s.isdigit() and not s.startswith("Agente no identificado"):
            return s

    # 4. Fallback placeholder
    if clean_oid:
        return f"Agente no identificado ({clean_oid})"

    return raw_agent


async def batch_resolve_agent_names(
    db: Any,
    owner_company_pairs: set[tuple[int | None, str]] | list[tuple[int | None, str]],
) -> dict[tuple[int | None, str], str]:
    """
    Resolve agent names from bm_users in a single query for multiple (company_id, hubspot_owner_id) pairs.
    Prevents N+1 queries during listing.
    """
    if not owner_company_pairs:
        return {}

    unique_owners = {str(oid).strip() for cid, oid in owner_company_pairs if oid}
    unique_companies = {cid for cid, oid in owner_company_pairs if cid is not None}

    if not unique_owners:
        return {}

    from app.models.users import User
    from sqlalchemy import select, or_

    stmt = select(User.company_id, User.hubspot_owner_id, User.name, User.username).where(
        User.hubspot_owner_id.in_(unique_owners),
    )
    if unique_companies:
        stmt = stmt.where(or_(User.company_id.in_(unique_companies), User.company_id.is_(None)))

    res = await db.execute(stmt)
    rows = res.fetchall()

    resolved_map: dict[tuple[int | None, str], str] = {}
    for cid, oid, name, username in rows:
        clean_oid = str(oid).strip() if oid else ""
        disp_name = (name and name.strip()) or (username and username.strip())
        if disp_name:
            resolved_map[(cid, clean_oid)] = disp_name
            if cid is None:
                resolved_map[("*", clean_oid)] = disp_name

    # Fallback to OWNER_TO_NAME for any pair not resolved in bm_users
    for cid, oid in owner_company_pairs:
        clean_oid = str(oid).strip() if oid else ""
        if clean_oid and (cid, clean_oid) not in resolved_map and ("*", clean_oid) not in resolved_map:
            if clean_oid in OWNER_TO_NAME:
                resolved_map[(cid, clean_oid)] = OWNER_TO_NAME[clean_oid]

    return resolved_map
