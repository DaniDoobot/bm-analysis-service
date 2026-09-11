"""
Central service for provisioning and managing agent training credentials.

Credentials model:
- training_code: Alphanumeric code (format: <agent_initials><2 digits>, e.g. "VA12").
- training_numeric_code: 4-digit numeric PIN for telephony/DTMF/voice authentication.
- training_code_enabled: Controls whether code-based authentication is enabled/revoked.
- is_enabled: Controls automatic cycle generation in Training Hub scheduler (NEVER coupled to authentication).

Security note:
Plaintext codes are credentials. They must never be logged, printed in exceptions,
or included in structured logs.
"""
import logging
import secrets
from typing import List, Optional, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import InternalRole, normalize_role
from app.models.personalized_training import TrainingAgentSetting
from app.models.users import User

logger = logging.getLogger(__name__)


def edit_distance(s1: str, s2: str) -> int:
    """Calculate Levenshtein edit distance between two strings."""
    if len(s1) > len(s2):
        s1, s2 = s2, s1
    distances = range(len(s1) + 1)
    for i2, c2 in enumerate(s2):
        distances_ = [i2 + 1]
        for i1, c1 in enumerate(s1):
            if c1 == c2:
                distances_.append(distances[i1])
            else:
                distances_.append(1 + min((distances[i1], distances[i1 + 1], distances_[-1])))
        distances = distances_
    return distances[-1]


def is_trivial_pin(pin: str) -> bool:
    """
    Check if a 4-digit PIN is considered trivial and unsafe for telephone voice/DTMF auth.
    Rejects:
    - All identical digits (0000, 1111, 2222, ..., 9999)
    - Sequential ascending (0123, 1234, 2345, ..., 6789)
    - Sequential descending (9876, 8765, 7654, ..., 3210)
    """
    if len(pin) != 4 or not pin.isdigit():
        return True
    if len(set(pin)) == 1:
        return True
    if pin in "0123456789":
        return True
    if pin in "9876543210":
        return True
    return False


class AgentCredentialsService:

    @staticmethod
    async def generate_unique_training_code(
        db: AsyncSession,
        initials: str,
        current_owner_id: str,
        max_attempts: int = 200,
    ) -> str:
        """
        Generate a unique alphanumeric training code in the format <INITIALS><2_DIGITS>.
        Enforces:
        - Valid alphanumeric format.
        - Global uniqueness across all TrainingAgentSetting.
        - Levenshtein edit distance > 1 against all existing training_code values.
        """
        clean_initials = initials.strip().upper()
        if not clean_initials or not clean_initials.isalnum():
            clean_initials = "AG"

        # Load all existing training codes
        stmt = select(TrainingAgentSetting.training_code).where(
            and_(
                TrainingAgentSetting.training_code.isnot(None),
                TrainingAgentSetting.hubspot_owner_id != current_owner_id,
            )
        )
        res = await db.execute(stmt)
        existing_codes = {c for c in res.scalars().all() if c}

        # Candidate generation: randomize 2-digit numbers
        all_nums = list(range(100))
        secrets.SystemRandom().shuffle(all_nums)

        for num in all_nums[:max_attempts]:
            candidate = f"{clean_initials}{num:02d}"
            if candidate in existing_codes:
                continue

            # Check Levenshtein distance <= 1 against existing codes
            too_close = False
            for existing in existing_codes:
                if edit_distance(candidate, existing) <= 1:
                    too_close = True
                    break
            if not too_close:
                return candidate

        raise RuntimeError(
            "No se pudo generar un training_code alfanumérico único para las iniciales especificadas."
        )

    @staticmethod
    async def generate_unique_training_numeric_code(
        db: AsyncSession,
        current_owner_id: str,
        max_attempts: int = 500,
    ) -> str:
        """
        Generate a unique 4-digit PIN for telephony and voice authentication.
        Enforces:
        - Cryptographically secure 4 digits.
        - Non-trivial pattern (no 0000, 1111, 1234, 4321, etc.).
        - Global uniqueness across all TrainingAgentSetting.
        - Levenshtein edit distance > 1 against all existing training_numeric_code values.
        """
        stmt = select(TrainingAgentSetting.training_numeric_code).where(
            and_(
                TrainingAgentSetting.training_numeric_code.isnot(None),
                TrainingAgentSetting.hubspot_owner_id != current_owner_id,
            )
        )
        res = await db.execute(stmt)
        existing_numeric_codes = {c for c in res.scalars().all() if c}

        for _ in range(max_attempts):
            val = secrets.randbelow(10000)
            candidate = f"{val:04d}"

            if is_trivial_pin(candidate):
                continue

            if candidate in existing_numeric_codes:
                continue

            # Check Levenshtein distance <= 1
            too_close = False
            for existing in existing_numeric_codes:
                if edit_distance(candidate, existing) <= 1:
                    too_close = True
                    break
            if not too_close:
                return candidate

        raise RuntimeError(
            "No se pudo generar un training_numeric_code único y no trivial tras múltiples intentos."
        )

    @staticmethod
    async def ensure_agent_training_credentials(
        db: AsyncSession,
        *,
        user: Optional[User] = None,
        user_id: Optional[int] = None,
        hubspot_owner_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_initials: Optional[str] = None,
        company_id: Optional[int] = None,
        commit: bool = True,
    ) -> Optional[TrainingAgentSetting]:
        """
        Canonical, idempotent service for provisioning agent training credentials.

        Rules:
        1. Only applies to users with normalized role == AGENT ('agente' / 'agent').
        2. Requires hubspot_owner_id and agent_initials to be present. If missing, does not create partial credentials.
        3. Preserves existing credentials (never overwrites non-null codes).
        4. Generates training_code only if missing.
        5. Generates training_numeric_code only if missing.
        6. Preserves training_code_enabled if setting already exists (defaults to True on new).
        7. NEVER modifies is_enabled (defaults to False on new).
        8. Handles concurrent races with retry on IntegrityError.
        9. Idempotent: running multiple times preserves existing credentials.
        """
        target_user = user
        if target_user is None and user_id is not None:
            res_u = await db.execute(select(User).where(User.user_id == user_id))
            target_user = res_u.scalars().first()

        if target_user is not None:
            norm_role = normalize_role(target_user.role)
            if norm_role != InternalRole.AGENT:
                logger.debug(
                    "ensure_agent_training_credentials skipped: user_id=%s has non-agent role=%s",
                    target_user.user_id,
                    target_user.role,
                )
                return None

            resolved_hs_id = (target_user.hubspot_owner_id or "").strip()
            resolved_initials = (target_user.agent_initials or "").strip()
            resolved_name = (
                target_user.name or target_user.username or f"Agente {resolved_hs_id}"
            ).strip()
            resolved_company_id = target_user.company_id
        else:
            resolved_hs_id = (hubspot_owner_id or "").strip()
            resolved_initials = (agent_initials or "").strip()
            resolved_name = (agent_name or f"Agente {resolved_hs_id}").strip()
            resolved_company_id = company_id

        # Require both hubspot_owner_id and agent_initials for full credentials
        if not resolved_hs_id or not resolved_initials:
            logger.info(
                "ensure_agent_training_credentials deferred: missing hubspot_owner_id or agent_initials (hs_id=%s, initials=%s)",
                bool(resolved_hs_id),
                bool(resolved_initials),
            )
            return None

        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with db.begin_nested():
                    # 1. Fetch or create TrainingAgentSetting
                    stmt = select(TrainingAgentSetting).where(
                        TrainingAgentSetting.hubspot_owner_id == resolved_hs_id
                    )
                    res = await db.execute(stmt)
                    setting = res.scalars().first()

                    is_new = False
                    if not setting:
                        setting = TrainingAgentSetting(
                            hubspot_owner_id=resolved_hs_id,
                            agent_name=resolved_name,
                            agent_initials=resolved_initials,
                            company_id=resolved_company_id,
                            is_enabled=False,  # Automatic cycle generation disabled by default
                            training_code_enabled=True,  # Telephony auth allowed by default
                            training_code=None,
                            training_numeric_code=None,
                        )
                        db.add(setting)
                        is_new = True
                    else:
                        # Update metadata if needed without touching credentials or cycle flags
                        if setting.agent_name != resolved_name and resolved_name:
                            setting.agent_name = resolved_name
                        if setting.agent_initials != resolved_initials and resolved_initials:
                            setting.agent_initials = resolved_initials
                        if resolved_company_id is not None and setting.company_id != resolved_company_id:
                            setting.company_id = resolved_company_id

                    # 2. Provision training_code if missing
                    code_updated = False
                    if not setting.training_code or not setting.training_code.strip():
                        new_code = await AgentCredentialsService.generate_unique_training_code(
                            db,
                            initials=resolved_initials,
                            current_owner_id=resolved_hs_id,
                        )
                        setting.training_code = new_code
                        code_updated = True

                    # 3. Provision training_numeric_code if missing
                    if not setting.training_numeric_code or not setting.training_numeric_code.strip():
                        new_numeric = (
                            await AgentCredentialsService.generate_unique_training_numeric_code(
                                db,
                                current_owner_id=resolved_hs_id,
                            )
                        )
                        setting.training_numeric_code = new_numeric
                        code_updated = True

                    # Flush inside the savepoint to immediately verify uniqueness constraints
                    await db.flush()

                if commit:
                    await db.commit()
                    await db.refresh(setting)

                logger.info(
                    "ensure_agent_training_credentials: owner_id=%s is_new=%s code_provisioned=%s",
                    resolved_hs_id,
                    is_new,
                    code_updated,
                )
                return setting

            except IntegrityError as ie:
                # begin_nested() context manager has automatically rolled back to the savepoint!
                # Outer transaction (User, team/service associations) is completely preserved.
                err_str = (str(ie.orig) if hasattr(ie, "orig") else str(ie)).lower()
                if (
                    "training_code" in err_str
                    or "training_numeric_code" in err_str
                    or "unique" in err_str
                ):
                    logger.warning(
                        "Integrity collision during agent credentials provisioning for owner_id=%s (attempt %d/%d). Retrying...",
                        resolved_hs_id,
                        attempt + 1,
                        max_retries,
                    )
                    if attempt == max_retries - 1:
                        raise
                    continue
                else:
                    # Unrelated integrity error, re-raise
                    raise

        return None

    @staticmethod
    async def get_agent_credentials_for_user(
        db: AsyncSession,
        user: User,
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """
        Retrieve canonical credentials for an authenticated user.
        Returns: (training_code, training_numeric_code, enabled)
        enabled is True ONLY when training_code_enabled is True.
        """
        if not user.hubspot_owner_id:
            return None, None, False

        stmt = select(TrainingAgentSetting).where(
            TrainingAgentSetting.hubspot_owner_id == user.hubspot_owner_id
        )
        res = await db.execute(stmt)
        setting = res.scalar_one_or_none()

        if not setting:
            return None, None, False

        raw_code = setting.training_code
        training_code = raw_code.strip() if raw_code and raw_code.strip() else None

        raw_num = setting.training_numeric_code
        training_numeric_code = raw_num.strip() if raw_num and raw_num.strip() else None

        # Enabled flag strictly reflects training_code_enabled
        enabled = bool(setting.training_code_enabled)

        return training_code, training_numeric_code, enabled
