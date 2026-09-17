"""
Centralized Agent Initials Resolution Utilities.
=================================================
Provides resolve_agent_initials and batch helper functions to resolve agent initials
consistently across all dashboard, analytics, and summary endpoints according to:
1. bm_users.agent_initials by hubspot_owner_id (+ company_id)
2. bm_users.agent_initials by normalized name match if unique
3. persisted initials in result if present
4. calculated fallback by name
"""
import re
import unicodedata
import logging
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def normalize_name_key(name: Optional[str]) -> str:
    """Normalize name string for matching: lowercase, strip accents & special chars, collapse spaces."""
    if not name:
        return ""
    s_lower = str(name).strip().lower()
    nfkd_form = unicodedata.normalize("NFKD", s_lower)
    s_no_accents = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    cleaned = re.sub(r"[^a-z0-9\s]+", "", s_no_accents)
    return " ".join(cleaned.split())


def get_fallback_initials(name: Optional[str]) -> str:
    """Calculate fallback initials from display name (e.g. 'Luci Dos Santos' -> 'LD', 'Luci' -> 'LU')."""
    if not name:
        return "??"
    parts = str(name).strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    elif len(parts) == 1:
        clean = parts[0]
        return clean[:2].upper() if len(clean) >= 2 else clean.upper()
    return "??"


def get_demo_agent_index(
    hubspot_owner_id: Optional[Any] = None,
    agent_name: Optional[str] = None,
) -> Optional[int]:
    """
    Extract 1-based index (1..60) for a demo agent from hubspot_owner_id (e.g. 'demo_owner_01')
    or agent_name (e.g. 'Agente Demo 01').
    """
    if hubspot_owner_id is not None:
        s_oid = str(hubspot_owner_id).strip()
        m_oid = re.search(r"demo_owner_(\d+)", s_oid, re.IGNORECASE)
        if m_oid:
            idx = int(m_oid.group(1))
            if 1 <= idx <= 60:
                return idx
    if agent_name:
        s_name = str(agent_name).strip()
        m_name = re.search(r"agente\s+demo\s+(\d+)", s_name, re.IGNORECASE)
        if m_name:
            idx = int(m_name.group(1))
            if 1 <= idx <= 60:
                return idx
    return None


def calculate_demo_agent_code(index: int) -> str:
    """
    Calculate the official unique visible agent_code for a demo agent index (1..60).
    - 01..10 -> AC-F01..AC-F10 (Atención al Cliente - Front Atención)
    - 11..30 -> AC-B01..AC-B20 (Atención al Cliente - Backoffice Atención)
    - 31..40 -> VT-C01..VT-C10 (Ventas - Equipo Comercial)
    - 41..60 -> VT-R01..VT-R20 (Ventas - Equipo Retención)
    """
    if 1 <= index <= 10:
        return f"AC-F{index:02d}"
    elif 11 <= index <= 30:
        return f"AC-B{(index - 10):02d}"
    elif 31 <= index <= 40:
        return f"VT-C{(index - 30):02d}"
    elif 41 <= index <= 60:
        return f"VT-R{(index - 40):02d}"
    return f"DM-{index:02d}"


def resolve_demo_agent_code(
    hubspot_owner_id: Optional[Any] = None,
    agent_name: Optional[str] = None,
    persisted_initials: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve unique agent_code for demo agents:
    1. If persisted_initials matches pattern 'AC-F..', 'AC-B..', 'VT-C..', 'VT-R..', return it.
    2. Extract index (1..60) from hubspot_owner_id or agent_name and compute code.
    """
    if persisted_initials:
        clean_init = str(persisted_initials).strip().upper()
        if re.match(r"^(AC-[FB]|VT-[CR])\d{2}$", clean_init):
            return clean_init

    idx = get_demo_agent_index(hubspot_owner_id=hubspot_owner_id, agent_name=agent_name)
    if idx is not None:
        return calculate_demo_agent_code(idx)

    return None


def is_demo_agent_code(code: Optional[Any]) -> bool:
    """Check if a given code matches the demo pattern e.g. 'AC-F01', 'AC-B02', 'VT-C03', 'VT-R04'."""
    if not code:
        return False
    return bool(re.match(r"^(AC-[FB]|VT-[CR])\d{2}$", str(code).strip().upper()))


def resolve_agent_code(
    hubspot_owner_id: Optional[Any] = None,
    agent_name: Optional[str] = None,
    company_id: Optional[int] = None,
    is_demo: bool = False,
    persisted_initials: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve agent_code:
    - For demo company (company_id == 6, is_demo=True, or demo_owner_*/Agente Demo*):
      Returns structured code e.g. 'AC-F01'.
    - For real companies (e.g. Boston Medical, company_id=1):
      Fallback to agent_name if no explicit code exists.
    """
    is_demo_agent = (
        is_demo
        or (company_id is not None and company_id in (6, 7))
        or (hubspot_owner_id and "demo_owner_" in str(hubspot_owner_id).lower())
        or (agent_name and "agente demo" in str(agent_name).lower())
        or (persisted_initials and re.match(r"^(AC-[FB]|VT-[CR])\d{2}$", str(persisted_initials).strip().upper()))
    )

    if is_demo_agent:
        demo_code = resolve_demo_agent_code(
            hubspot_owner_id=hubspot_owner_id,
            agent_name=agent_name,
            persisted_initials=persisted_initials,
        )
        if demo_code:
            return demo_code

    # Real company: fallback to agent_name or str(hubspot_owner_id)
    if agent_name and str(agent_name).strip():
        return str(agent_name).strip()
    if hubspot_owner_id is not None:
        return str(hubspot_owner_id).strip()
    return None


async def build_user_initials_maps(
    db: AsyncSession, company_id: Optional[int] = None
) -> Tuple[Dict[str, str], Dict[str, str], List[Dict[str, Any]]]:
    """
    Fetch relevant users from bm_users and build lookup maps:
    - by_owner: owner_id_str -> agent_initials
    - by_name: normalized_name -> agent_initials
    - users_list: list of user dicts for partial name matching
    """
    from app.models.users import User

    stmt = select(User)
    if company_id is not None:
        stmt = stmt.where(or_(User.company_id == company_id, User.company_id.is_(None)))

    res = await db.execute(stmt)
    users = res.scalars().all()

    by_owner: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    users_list: List[Dict[str, Any]] = []

    for u in users:
        # Check if user has explicit initials or demo code
        init = (u.agent_initials or "").strip()
        demo_c = resolve_demo_agent_code(
            hubspot_owner_id=u.hubspot_owner_id,
            agent_name=u.name,
            persisted_initials=init,
        )
        if demo_c:
            init = demo_c

        u_dict = {
            "user_id": u.user_id,
            "company_id": u.company_id,
            "primary_service_id": u.primary_service_id,
            "primary_team_id": u.primary_team_id,
            "hubspot_owner_id": u.hubspot_owner_id,
            "name": u.name,
            "username": u.username,
            "email": u.email,
            "agent_initials": init,
            "norm_name": normalize_name_key(u.name),
            "norm_username": normalize_name_key(u.username),
            "norm_email_prefix": normalize_name_key(u.email.split("@")[0]) if u.email else "",
        }
        users_list.append(u_dict)

        if init:
            if u.hubspot_owner_id:
                by_owner[str(u.hubspot_owner_id).strip()] = init
            if u_dict["norm_name"]:
                by_name[u_dict["norm_name"]] = init
            if u_dict["norm_username"]:
                by_name[u_dict["norm_username"]] = init
            if u_dict["norm_email_prefix"]:
                by_name[u_dict["norm_email_prefix"]] = init

    return by_owner, by_name, users_list


def resolve_agent_initials(
    hubspot_owner_id: Optional[Any] = None,
    agent_name: Optional[str] = None,
    company_id: Optional[int] = None,
    by_owner: Optional[Dict[str, str]] = None,
    by_name: Optional[Dict[str, str]] = None,
    users_list: Optional[List[Dict[str, Any]]] = None,
    persisted_initials: Optional[str] = None,
) -> str:
    """
    Resolve agent initials prioritizing:
    0. demo agent unique code (AC-F01, etc.) if it's a demo agent
    1. bm_users.agent_initials by hubspot_owner_id
    2. bm_users.agent_initials by normalized name match if unique
    3. persisted initials in result if present
    4. fallback calculated by name
    """
    # 0. Demo agent code priority
    demo_code = resolve_demo_agent_code(
        hubspot_owner_id=hubspot_owner_id,
        agent_name=agent_name,
        persisted_initials=persisted_initials,
    )
    if demo_code:
        return demo_code

    # 1. By hubspot_owner_id
    if hubspot_owner_id is not None:
        oid_str = str(hubspot_owner_id).strip()
        if by_owner and oid_str in by_owner:
            return by_owner[oid_str]

    # 2. By normalized name match
    if agent_name:
        norm_target = normalize_name_key(agent_name)
        if norm_target and by_name:
            # Exact normalized name match
            if norm_target in by_name:
                return by_name[norm_target]

            # Token / partial match against users_list
            if users_list:
                target_tokens = [t for t in norm_target.split() if len(t) >= 2]
                first_token = target_tokens[0] if target_tokens else ""
                matched_initials: Set[str] = set()

                for u in users_list:
                    u_init = u.get("agent_initials")
                    if not u_init:
                        continue

                    u_norm = u.get("norm_name") or ""
                    u_tokens = set(u_norm.split())

                    # Substring match (e.g. "luci dos santos" in "luci dos santos furtado")
                    if u_norm and (norm_target in u_norm or u_norm in norm_target):
                        matched_initials.add(u_init)
                    elif target_tokens and u_tokens:
                        common = set(target_tokens) & u_tokens
                        # Match if first name matches or subsets match
                        if (first_token and first_token in u_tokens) or set(target_tokens).issubset(u_tokens) or u_tokens.issubset(set(target_tokens)):
                            matched_initials.add(u_init)

                if len(matched_initials) == 1:
                    return next(iter(matched_initials))

    # 3. Persisted initials
    if persisted_initials and str(persisted_initials).strip():
        return str(persisted_initials).strip().upper()

    # 4. Fallback calculated by name
    return get_fallback_initials(agent_name)


async def resolve_agent_initials_async(
    db: AsyncSession,
    hubspot_owner_id: Optional[Any] = None,
    agent_name: Optional[str] = None,
    company_id: Optional[int] = None,
    persisted_initials: Optional[str] = None,
) -> str:
    """Async single-record wrapper for resolve_agent_initials."""
    by_owner, by_name, users_list = await build_user_initials_maps(db, company_id=company_id)
    return resolve_agent_initials(
        hubspot_owner_id=hubspot_owner_id,
        agent_name=agent_name,
        company_id=company_id,
        by_owner=by_owner,
        by_name=by_name,
        users_list=users_list,
        persisted_initials=persisted_initials,
    )


async def resolve_agent_identifiers_to_owner_ids(
    db: AsyncSession,
    identifiers: Optional[List[Any]],
    company_id: Optional[int] = None,
) -> List[str]:
    """
    Given a list of agent identifiers which may contain numeric user_ids (int or numeric str)
    or hubspot_owner_ids (str), resolves any user_ids to their canonical hubspot_owner_ids.
    Returns a deduplicated list of hubspot_owner_id strings.
    """
    if not identifiers:
        return []

    from app.models.users import User

    resolved_owner_ids: List[str] = []
    numeric_ids_to_lookup: List[int] = []

    for raw in identifiers:
        if raw is None:
            continue
        s_val = str(raw).strip()
        if not s_val:
            continue
        if s_val.isdigit():
            numeric_ids_to_lookup.append(int(s_val))
        else:
            resolved_owner_ids.append(s_val)

    if numeric_ids_to_lookup:
        stmt = select(User.user_id, User.hubspot_owner_id).where(
            or_(
                User.user_id.in_(numeric_ids_to_lookup),
                User.hubspot_owner_id.in_([str(n) for n in numeric_ids_to_lookup]),
            )
        )
        if company_id is not None:
            stmt = stmt.where(or_(User.company_id == company_id, User.company_id.is_(None)))
        res = await db.execute(stmt)
        found_rows = res.all()
        found_map: Dict[Any, str] = {}
        for uid, h_oid in found_rows:
            if h_oid:
                clean_h = str(h_oid).strip()
                found_map[uid] = clean_h
                found_map[str(uid)] = clean_h
                found_map[clean_h] = clean_h

        for num_id in numeric_ids_to_lookup:
            if num_id in found_map:
                resolved_owner_ids.append(found_map[num_id])
            elif str(num_id) in found_map:
                resolved_owner_ids.append(found_map[str(num_id)])
            else:
                resolved_owner_ids.append(str(num_id))

    # Deduplicate preserving order
    seen = set()
    deduped = []
    for oid in resolved_owner_ids:
        if oid not in seen:
            seen.add(oid)
            deduped.append(oid)
    return deduped

