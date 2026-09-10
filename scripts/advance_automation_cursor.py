#!/usr/bin/env python
"""
scripts/advance_automation_cursor.py

Administrative utility to safely and auditably advance an automation's cursor.
Inserts an auditable marker run with status='completed_empty' without modifying
any historical run records.

Usage:
    python scripts/advance_automation_cursor.py --automation-id 9
    python scripts/advance_automation_cursor.py --automation-id 9 --target-cursor 2026-09-10T10:30:00Z --reason "EXPAC controlled reactivation"
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.services.mass_evaluation_service import MassEvaluationService
from sqlalchemy.ext.asyncio import AsyncSession


async def run_advance(automation_id: int, target_cursor_str: str | None, reason: str, allow_backward: bool):
    target_cursor = None
    if target_cursor_str:
        clean_str = target_cursor_str.strip().replace("Z", "+00:00")
        target_cursor = datetime.fromisoformat(clean_str)
        if target_cursor.tzinfo is None:
            target_cursor = target_cursor.replace(tzinfo=timezone.utc)

    engine = get_engine()
    async with AsyncSession(engine) as session:
        try:
            res = await MassEvaluationService.advance_automation_cursor(
                db=session,
                automation_id=automation_id,
                target_cursor=target_cursor,
                reason=reason,
                allow_backward=allow_backward,
            )
            print("==================================================")
            print("   AUTOMATION CURSOR ADVANCE SUCCESSFUL")
            print("==================================================")
            print(f"Automation ID:             {res['automation_id']}")
            print(f"Previous Watermark:        {res['previous_watermark']}")
            print(f"New Watermark:             {res['new_watermark']}")
            print(f"Marker Run ID:             {res['marker_automation_run_id']}")
            print(f"Next Expected Window From: {res['next_expected_window_from']}")
            print(f"Next Expected Window To:   {res['next_expected_window_to']}")
            print(f"Message:                   {res['message']}")
            print("==================================================")
        except Exception as e:
            print("ERROR advancing cursor:", e)
            sys.exit(1)
        finally:
            await session.close()
    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Advance automation watermark safely.")
    parser.add_argument("--automation-id", type=int, required=True, help="Automation ID to advance (e.g. 9)")
    parser.add_argument("--target-cursor", type=str, default=None, help="Target UTC ISO timestamp (e.g. 2026-09-10T10:30:00Z). If omitted, dynamic calculation is used.")
    parser.add_argument("--reason", type=str, required=True, help="Reason for audit log (minimum 5 characters).")
    parser.add_argument("--allow-backward", action="store_true", help="Allow moving cursor backwards (dangerous).")

    args = parser.parse_args()
    asyncio.run(run_advance(args.automation_id, args.target_cursor, args.reason, args.allow_backward))


if __name__ == "__main__":
    main()
