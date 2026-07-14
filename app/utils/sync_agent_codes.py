"""
sync_agent_codes.py
===================
Idempotent upsert script that synchronizes training_code and training_numeric_code
for all known Training Hub agents.

Runs safely on a live production database:
  - Never deletes rows.
  - Never creates duplicate rows.
  - Only updates the code fields; leaves all other data untouched.
  - Can be re-run unlimited times (idempotent).

Usage:
  python app/utils/sync_agent_codes.py [--dry-run]

  --dry-run   Show what would change without writing to the database.
"""
import asyncio
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import AsyncSession

# ── Agent code map ────────────────────────────────────────────────────────────
# Format: (hubspot_owner_id, agent_name, agent_initials, training_code, training_numeric_code)
# training_code    = alphanumeric short code (e.g. "CM77")
# training_numeric_code = 4-digit voice code (e.g. "7777")
AGENT_CODE_MAP = [
    {
        "hubspot_owner_id": "33013276",
        "agent_name": "Cristina Montenegro",
        "agent_initials": "CM",
        "training_code": "CM77",
        "training_numeric_code": "7777",
    },
    {
        "hubspot_owner_id": "33013277",
        "agent_name": "Bryan Herrera",
        "agent_initials": "BH",
        "training_code": "BH55",
        "training_numeric_code": "5555",
    },
    {
        "hubspot_owner_id": "1375831791",
        "agent_name": "Eugenia Carreño",
        "agent_initials": "EC",
        "training_code": "EC88",
        "training_numeric_code": "8808",
    },
    {
        "hubspot_owner_id": "1539993532",
        "agent_name": "Fernanda Rodrigues",
        "agent_initials": "FR",
        "training_code": "FR45",
        "training_numeric_code": "4545",
    },
    {
        "hubspot_owner_id": "1375831790",
        "agent_name": "Luci Dos Santos Furtado",
        "agent_initials": "LD",
        "training_code": "LD23",
        "training_numeric_code": "2323",
    },
    {
        "hubspot_owner_id": "1459417733",
        "agent_name": "Santiago Taboada",
        "agent_initials": "ST",
        "training_code": "ST99",
        "training_numeric_code": "9909",
    },
    # Roberto Galán: voice hub code TBD — included without numeric code for now
    {
        "hubspot_owner_id": "1375831787",
        "agent_name": "Roberto Galán",
        "agent_initials": "RG",
        "training_code": None,
        "training_numeric_code": None,
    },
]


async def run(dry_run: bool = False) -> None:
    from app.db import get_engine
    from app.models.personalized_training import TrainingAgentSetting

    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # ── 1. Load all agents currently in the table ──────────────────────────
        res = await db.execute(select(TrainingAgentSetting))
        all_settings = {s.hubspot_owner_id: s for s in res.scalars().all()}

        print("\n" + "=" * 65)
        print("SYNC AGENT CODES — Training Hub Voice")
        print("=" * 65)
        print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'APPLY (writing to DB)'}\n")
        print(f"{'ID':>6}  {'Initials':8}  {'Name':<28}  {'Num Code':10}  {'Alpha Code':10}  {'Action'}")
        print("-" * 85)

        created = 0
        updated = 0
        skipped = 0

        for agent in AGENT_CODE_MAP:
            oid = agent["hubspot_owner_id"]
            setting = all_settings.get(oid)
            num_code = agent["training_numeric_code"]
            alpha_code = agent["training_code"]

            if setting is None:
                # Agent does not exist yet — create it
                action = "CREATE"
                print(f"{'–':>6}  {agent['agent_initials']:8}  {agent['agent_name']:<28}  {str(num_code or '–'):10}  {str(alpha_code or '–'):10}  {action}")
                if not dry_run:
                    new_s = TrainingAgentSetting(
                        hubspot_owner_id=oid,
                        agent_name=agent["agent_name"],
                        agent_initials=agent["agent_initials"],
                        training_code=alpha_code,
                        training_numeric_code=num_code,
                        is_enabled=True,
                        training_code_enabled=True if (alpha_code or num_code) else False,
                    )
                    db.add(new_s)
                created += 1
            else:
                # Check if codes need updating
                needs_update = (
                    setting.training_numeric_code != num_code or
                    setting.training_code != alpha_code
                )
                if needs_update:
                    action = (
                        f"UPDATE  {setting.training_numeric_code or '–'} → {num_code or '–'}"
                    )
                    print(f"{setting.setting_id:>6}  {setting.agent_initials:8}  {setting.agent_name:<28}  {str(num_code or '–'):10}  {str(alpha_code or '–'):10}  {action}")
                    if not dry_run:
                        setting.training_numeric_code = num_code
                        setting.training_code = alpha_code
                        if alpha_code or num_code:
                            setting.training_code_enabled = True
                    updated += 1
                else:
                    action = "OK (no change)"
                    print(f"{setting.setting_id:>6}  {setting.agent_initials:8}  {setting.agent_name:<28}  {str(num_code or '–'):10}  {str(alpha_code or '–'):10}  {action}")
                    skipped += 1

        print("-" * 85)
        print(f"\nSummary: {created} created | {updated} updated | {skipped} unchanged\n")

        if not dry_run and (created > 0 or updated > 0):
            await db.commit()
            print("✔ Changes committed to database.\n")

        # ── 2. Print final state ────────────────────────────────────────────────
        print("\n" + "=" * 65)
        print("FINAL STATE — Training Agent Code Map")
        print("=" * 65)
        print(f"{'ID':>6}  {'Init':5}  {'Name':<28}  {'NumCode':10}  {'AlphaCode':10}  {'Enabled':8}  {'CodeEnabled'}")
        print("-" * 85)
        res2 = await db.execute(select(TrainingAgentSetting).order_by(TrainingAgentSetting.agent_initials))
        for s in res2.scalars().all():
            enabled_flag = "✔" if s.is_enabled else "✗"
            code_flag = "✔" if s.training_code_enabled else "✗"
            print(
                f"{s.setting_id:>6}  {s.agent_initials:5}  {s.agent_name:<28}  "
                f"{str(s.training_numeric_code or '–'):10}  {str(s.training_code or '–'):10}  "
                f"{enabled_flag:8}  {code_flag}"
            )
        print()

        # ── 3. Detect duplicate numeric codes (sanity check) ───────────────────
        all_num_codes = [
            s.training_numeric_code
            for s in (await db.execute(select(TrainingAgentSetting).where(
                TrainingAgentSetting.training_numeric_code.isnot(None),
                TrainingAgentSetting.is_enabled == True,
            ))).scalars().all()
        ]
        seen = set()
        dups = set()
        for c in all_num_codes:
            if c in seen:
                dups.add(c)
            seen.add(c)
        if dups:
            print(f"⚠ WARNING: Duplicate numeric codes detected among enabled agents: {dups}")
        else:
            print("✔ No duplicate numeric codes detected among enabled agents.")
        print()


def main():
    parser = argparse.ArgumentParser(description="Sync training agent short codes.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
