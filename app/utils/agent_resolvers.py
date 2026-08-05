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
        init = (u.agent_initials or "").strip().upper()
        if u.hubspot_owner_id and init:
            by_owner[str(u.hubspot_owner_id).strip()] = init

        u_dict = {
            "user_id": u.user_id,
            "hubspot_owner_id": str(u.hubspot_owner_id).strip() if u.hubspot_owner_id else None,
            "name": u.name,
            "username": u.username,
            "email": u.email,
            "agent_initials": init if init else None,
            "norm_name": normalize_name_key(u.name),
            "norm_username": normalize_name_key(u.username),
            "norm_email_prefix": normalize_name_key(u.email.split("@")[0]) if u.email and "@" in u.email else "",
        }
        users_list.append(u_dict)

        if init:
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
    1. bm_users.agent_initials by hubspot_owner_id
    2. bm_users.agent_initials by normalized name match if unique
    3. persisted initials in result if present
    4. fallback calculated by name
    """
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
