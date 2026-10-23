"""
Service for content-addressed deduplication of prompt snapshots (bm_prompt_snapshots).
Provides thread-safe, concurrency-safe calculation and retrieval.
"""
import hashlib
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models.mass_evaluations import MassEvaluationResult

logger = logging.getLogger(__name__)


def compute_prompt_hash(prompt_text: str | None) -> str:
    """
    Computes a deterministic SHA-256 hash in lowercase hex format for the given prompt text.
    Uses UTF-8 encoding.
    """
    if not prompt_text:
        return ""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


async def get_or_create_prompt_snapshot(
    db: AsyncSession,
    prompt_text: str | None
) -> int | None:
    """
    Finds or creates a deduplicated prompt snapshot by its deterministic SHA-256 hash.
    Safe against concurrent insertions via savepoints and IntegrityError handling.
    
    Returns the integer primary key (id) of bm_prompt_snapshots, or None if prompt_text is empty.
    """
    if not prompt_text or not prompt_text.strip():
        return None

    from app.models.mass_evaluations import PromptSnapshot

    h = compute_prompt_hash(prompt_text)

    # 1. Fast path: check if snapshot already exists
    stmt = select(PromptSnapshot.id, PromptSnapshot.prompt_text).where(PromptSnapshot.hash_sha256 == h)
    res = await db.execute(stmt)
    row = res.first()
    if row:
        existing_id, existing_text = row
        if existing_text == prompt_text:
            return existing_id
        # In the virtually impossible case of a hash collision:
        logger.warning(
            "SHA-256 collision or content mismatch detected for hash %s (existing id=%d)",
            h, existing_id
        )
        return existing_id

    # 2. Insertion path with savepoint to guarantee concurrency safety
    try:
        async with db.begin_nested():
            snapshot = PromptSnapshot(
                hash_sha256=h,
                prompt_text=prompt_text
            )
            db.add(snapshot)
            await db.flush()
            return snapshot.id
    except IntegrityError:
        # Concurrent worker inserted the same hash between SELECT and INSERT
        res = await db.execute(
            select(PromptSnapshot.id).where(PromptSnapshot.hash_sha256 == h)
        )
        existing_id = res.scalar()
        if existing_id:
            return existing_id
        raise


async def resolve_prompt_snapshot_text(
    db: AsyncSession,
    result: "MassEvaluationResult"
) -> str:
    """
    Resolves the exact prompt text for an evaluation result transparently:
    Priority 1: Snapshot relation if already loaded.
    Priority 2: Snapshot fetched by prompt_snapshot_id from bm_prompt_snapshots.
    Priority 3: Legacy prompt_snapshot column if present.
    Fallback: Empty string.
    """
    if not result:
        return ""

    # Priority 1: Check if relationship is already loaded in instance dict
    if "snapshot_rel" in result.__dict__ and result.snapshot_rel is not None:
        if result.snapshot_rel.prompt_text:
            return result.snapshot_rel.prompt_text

    # Priority 2: Fetch by prompt_snapshot_id if present
    if result.prompt_snapshot_id:
        from app.models.mass_evaluations import PromptSnapshot
        snap = await db.get(PromptSnapshot, result.prompt_snapshot_id)
        if snap and snap.prompt_text:
            return snap.prompt_text

    # Priority 3: Fallback to legacy column if populated
    if "_prompt_snapshot" in result.__dict__ and result._prompt_snapshot:
        return result._prompt_snapshot
    if hasattr(result, "prompt_snapshot") and result.prompt_snapshot:
        return result.prompt_snapshot

    return ""
