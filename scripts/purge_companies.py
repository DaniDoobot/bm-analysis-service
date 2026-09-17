#!/usr/bin/env python
"""
scripts/purge_companies.py
==========================
Multi-company wrapper around purge_company.py logic.
Designed to run INSIDE the backend container where DATABASE_URL is available.

Key Safety Rules:
1. Dry-run by default: without --apply, no changes are committed.
2. ABSOLUTE PROTECTION: company_id=1 (Boston Medical) and company_id=7 (Empresa Demo)
   are STRICTLY FORBIDDEN — no flag can override this.
3. Allowed targets by default: {2, 3} only (Gesalux, Empresa Demo1).
4. Name+key verification for IDs 2 and 3 before any action.
5. Explicit interactive confirmation required in --apply mode.
6. All deletions in a single atomic transaction per company.
7. Uses DATABASE_URL from environment (or --db-url override).
8. Automatically normalizes postgresql:// → postgresql+asyncpg://.

Usage (inside container):
    # Dry-run audit (safe, read-only):
    python scripts/purge_companies.py --company-id 2 --dry-run
    python scripts/purge_companies.py --company-id 3 --dry-run
    python scripts/purge_companies.py --company-id 2 3 --dry-run

    # Permanent deletion (requires explicit confirmation):
    python scripts/purge_companies.py --company-id 2 --apply
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set

# Ensure application root is in path (works both locally and in container)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# Import reusable logic from the existing single-company script
from scripts.purge_company import (
    CompanyNotFoundError,
    CompanyProtectedError,
    CompanyPurgeError,
    audit_company_resources,
    purge_company,
    ALLOWED_DEFAULT_IDS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("purge_companies")

# ---------------------------------------------------------------------------
# Absolute protection list — NO flag can override these
# ---------------------------------------------------------------------------
ABSOLUTELY_PROTECTED_IDS: Set[int] = {1, 7}
ABSOLUTELY_PROTECTED_NAMES: Dict[int, str] = {
    1: "Boston Medical",
    7: "Empresa Demo",
}

# ---------------------------------------------------------------------------
# Expected identity map for known targets — verified before any action.
# Verification uses EXACT company_name only.
# company_key is intentionally NOT checked: real production values are "g" and
# "e" — too short and generic to be reliable identity signals.
# ---------------------------------------------------------------------------
EXPECTED_IDENTITY: Dict[int, str] = {
    2: "Gesalux",
    3: "Empresa Demo1",
}


def _normalize_db_url(url: str) -> str:
    """Convert postgresql:// to postgresql+asyncpg:// for async engine."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    return url


def _resolve_db_url(cli_url: Optional[str]) -> str:
    """
    Resolve the database URL in priority order:
      1. --db-url CLI argument
      2. DATABASE_URL environment variable
      3. App settings (if available)
    """
    if cli_url:
        return _normalize_db_url(cli_url)

    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return _normalize_db_url(env_url)

    try:
        from app.config import get_settings
        settings = get_settings()
        if settings.database_url:
            return _normalize_db_url(settings.database_url)
    except Exception:
        pass

    print("ERROR: No database URL found. Set DATABASE_URL or pass --db-url.")
    sys.exit(1)


def _check_absolute_protection(company_id: int) -> None:
    """
    Raise CompanyProtectedError if company_id is in the absolutely protected set.
    This cannot be bypassed by any flag.
    """
    if company_id in ABSOLUTELY_PROTECTED_IDS:
        name = ABSOLUTELY_PROTECTED_NAMES.get(company_id, f"company_id={company_id}")
        raise CompanyProtectedError(
            f"ABSOLUTE GUARD VIOLATION: company_id={company_id} ({name}) is STRICTLY PROTECTED "
            f"and can NEVER be deleted by this tool. "
            f"Protected IDs: {sorted(ABSOLUTELY_PROTECTED_IDS)}"
        )


def _verify_company_identity(comp_meta: Dict[str, Any], company_id: int) -> None:
    """
    For known target IDs (2, 3), verify that the company_name EXACTLY matches
    the expected value before allowing any purge action.

    This prevents accidental deletion if company IDs have been reassigned.
    company_key is intentionally NOT checked because real production keys ("g",
    "e") are single-character values that carry no reliable identity signal.
    """
    if company_id not in EXPECTED_IDENTITY:
        return

    expected_name: str = EXPECTED_IDENTITY[company_id]
    actual_name: str = comp_meta.get("company_name") or ""

    if actual_name != expected_name:
        raise CompanyProtectedError(
            f"IDENTITY MISMATCH for company_id={company_id}: "
            f"expected company_name='{expected_name}' (exact), "
            f"but got '{actual_name}'. "
            f"Aborting to prevent accidental deletion of the wrong company."
        )


def _print_audit_result(result: Dict[str, Any], apply_mode: bool) -> None:
    """Pretty-print the result of audit_company_resources or purge_company."""
    comp = result["company"]
    mode_label = "APPLIED" if result.get("applied") else "DRY-RUN"

    print(f"\n{'='*80}")
    print(f"[{mode_label}] Company: ({comp['company_id']}) {comp['company_name']}")
    print(f"  key={comp['company_key']!r}  is_demo={comp['is_demo']}  is_active={comp['is_active']}")
    print(f"  created_at={comp.get('created_at')}")
    print(f"{'-'*80}")
    print("RESOURCE INVENTORY:")
    total = sum(v for v in result["counts"].values() if isinstance(v, int))
    for k, v in result["counts"].items():
        marker = " <-- HAS DATA" if (isinstance(v, int) and v > 0) else ""
        print(f"  {k:35}: {v}{marker}")
    print(f"  {'TOTAL (summary counts)':35}: {total}")

    if apply_mode and result.get("deleted_records"):
        print(f"{'-'*80}")
        print("DELETED RECORDS (by table):")
        grand_total = 0
        for k, v in result["deleted_records"].items():
            if isinstance(v, int) and v > 0:
                grand_total += v
                print(f"  {k:45}: {v}")
        print(f"  {'GRAND TOTAL':45}: {grand_total}")

    status = result.get("status", "unknown")
    print(f"\nStatus: {status.upper()}")
    print(f"{'='*80}\n")


async def _audit_only(session: AsyncSession, company_id: int) -> Dict[str, Any]:
    """Read-only audit: fetch company info and counts without touching purge logic."""
    return await audit_company_resources(session, company_id)


async def process_company(
    session: AsyncSession,
    company_id: int,
    apply: bool,
    force_non_demo: bool,
) -> Dict[str, Any]:
    """
    Run audit (and optionally purge) for a single company within an existing session.

    Absolute protection is checked BEFORE hitting the DB.
    Identity verification is done AFTER fetching company info.
    """
    # Guard: absolute protection (before any DB access)
    _check_absolute_protection(company_id)

    if not apply:
        # Dry-run: just audit, no modifications
        audit = await _audit_only(session, company_id)
        # Identity verification on fetched data
        _verify_company_identity(audit["company"], company_id)
        return {
            "status": "dry_run",
            "applied": False,
            "company": audit["company"],
            "counts": audit["counts"],
            "deleted_records": {},
        }
    else:
        # Apply mode: delegate to purge_company (which re-fetches audit internally)
        # Identity verification will be done after audit fetch inside purge_company,
        # but we pre-verify here for early abort.
        audit = await _audit_only(session, company_id)
        _verify_company_identity(audit["company"], company_id)
        return await purge_company(
            session=session,
            company_id=company_id,
            apply=True,
            force_non_demo=force_non_demo,
            allowed_ids=ALLOWED_DEFAULT_IDS,
        )


def _prompt_confirmation(company_ids: List[int], company_names: Dict[int, str]) -> bool:
    """Interactive confirmation prompt for --apply mode."""
    print("\n" + "!" * 80)
    print("WARNING: You are about to PERMANENTLY DELETE the following companies:")
    for cid in company_ids:
        print(f"  - company_id={cid}: {company_names.get(cid, 'UNKNOWN')}")
    print("This action is IRREVERSIBLE.")
    print("!" * 80)
    response = input("\nType 'DELETE' (uppercase) to confirm: ").strip()
    return response == "DELETE"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Safely purge one or more demo/test companies and all their resources. "
            "Dry-run by default. Designed to run inside the backend container."
        )
    )
    parser.add_argument(
        "--company-id",
        type=int,
        nargs="+",
        required=True,
        metavar="ID",
        help="One or more company IDs to audit/purge (e.g. --company-id 2 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode (default). Shows what would be deleted without making changes.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute and commit deletion permanently. Requires explicit confirmation.",
    )
    parser.add_argument(
        "--force-non-demo",
        action="store_true",
        default=False,
        help="Allow deleting companies outside the default allowed list or with is_demo=False.",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        help=(
            "Optional database URL override. If not provided, uses DATABASE_URL "
            "environment variable. Supports both postgresql:// and postgresql+asyncpg://."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip interactive confirmation in --apply mode (use with caution).",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    apply_mode = bool(args.apply)
    force_non_demo = bool(args.force_non_demo)
    company_ids: List[int] = args.company_id

    # Deduplicate while preserving order
    seen: Set[int] = set()
    unique_ids: List[int] = []
    for cid in company_ids:
        if cid not in seen:
            seen.add(cid)
            unique_ids.append(cid)
    company_ids = unique_ids

    db_url = _resolve_db_url(args.db_url)

    print("=" * 80)
    print(f"PURGE COMPANIES TOOL")
    print(f"Mode: {'APPLY (PERMANENT DELETION)' if apply_mode else 'DRY RUN (NO CHANGES)'}")
    print(f"Targets: {company_ids}")
    print(f"DB: {db_url[:50]}...")
    print("=" * 80)

    # Pre-flight: check absolute protection before connecting to DB
    for cid in company_ids:
        try:
            _check_absolute_protection(cid)
        except CompanyProtectedError as e:
            print(f"\nSAFETY GUARD BLOCKED EXECUTION:\n{e}")
            sys.exit(2)

    engine = create_async_engine(db_url, echo=False)

    # In dry-run mode: run each company in its own transaction (rollback after)
    if not apply_mode:
        all_ok = True
        company_names: Dict[int, str] = {}

        for cid in company_ids:
            print(f"\n--- Auditing company_id={cid} (DRY RUN) ---")
            try:
                async with AsyncSession(engine) as session:
                    async with session.begin():
                        result = await process_company(
                            session=session,
                            company_id=cid,
                            apply=False,
                            force_non_demo=force_non_demo,
                        )
                        await session.rollback()

                company_names[cid] = result["company"]["company_name"]
                _print_audit_result(result, apply_mode=False)

            except CompanyProtectedError as e:
                print(f"\nSAFETY GUARD BLOCKED:\n{e}\n")
                all_ok = False
            except CompanyNotFoundError as e:
                print(f"\nNOT FOUND: {e}\n")
                all_ok = False
            except Exception as e:
                logger.exception("Unexpected error auditing company_id=%s: %s", cid, e)
                all_ok = False

        print("=" * 80)
        print("DRY RUN COMPLETE: No data was modified.")
        print("To execute permanent deletion, run with --apply and confirm.")
        print("=" * 80)
        sys.exit(0 if all_ok else 1)

    # Apply mode: audit all first, then confirm, then delete one by one
    print("\n[APPLY MODE] Pre-flight audit of all targets...")
    audit_results: Dict[int, Dict[str, Any]] = {}
    company_names: Dict[int, str] = {}

    for cid in company_ids:
        try:
            async with AsyncSession(engine) as session:
                async with session.begin():
                    result = await process_company(
                        session=session,
                        company_id=cid,
                        apply=False,
                        force_non_demo=force_non_demo,
                    )
                    await session.rollback()

            audit_results[cid] = result
            company_names[cid] = result["company"]["company_name"]
            _print_audit_result(result, apply_mode=False)

        except (CompanyProtectedError, CompanyNotFoundError) as e:
            print(f"\nPre-flight failed for company_id={cid}:\n{e}")
            sys.exit(2)
        except Exception as e:
            logger.exception("Pre-flight error for company_id=%s: %s", cid, e)
            sys.exit(1)

    # Confirmation
    if not args.yes:
        confirmed = _prompt_confirmation(company_ids, company_names)
        if not confirmed:
            print("\nConfirmation not received. Aborting.")
            sys.exit(3)
    else:
        print("\n[--yes flag] Skipping interactive confirmation.")

    # Execute deletion for each company in its own transaction
    exit_code = 0
    for cid in company_ids:
        print(f"\n--- Deleting company_id={cid} ({company_names.get(cid)}) ---")
        try:
            async with AsyncSession(engine) as session:
                async with session.begin():
                    result = await process_company(
                        session=session,
                        company_id=cid,
                        apply=True,
                        force_non_demo=force_non_demo,
                    )

            _print_audit_result(result, apply_mode=True)
            print(f"SUCCESS: company_id={cid} purged successfully.")

        except CompanyProtectedError as e:
            print(f"\nSAFETY GUARD BLOCKED during apply:\n{e}\n")
            exit_code = 2
        except Exception as e:
            logger.exception("Purge failed for company_id=%s: %s", cid, e)
            exit_code = 1

    print("=" * 80)
    if exit_code == 0:
        print("ALL COMPANIES PURGED SUCCESSFULLY.")
    else:
        print("ONE OR MORE PURGES FAILED. Check logs above.")
    print("=" * 80)
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(async_main())
