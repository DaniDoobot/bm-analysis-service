"""
Idempotent backfill script to align company_id for Base Structures and Prompts / Specific Structures.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine
from app.models.services import Service
from app.models.prompts import Prompt, PromptBaseStructure


async def main(apply: bool = False):
    engine = get_engine()
    db_url = str(engine.url)
    print(f"Target DB: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")

    async with AsyncSession(engine) as db:
        # 1. Base Structures
        stmt_base = select(PromptBaseStructure, Service).join(
            Service, PromptBaseStructure.service_id == Service.service_id
        ).where(
            (PromptBaseStructure.company_id != Service.company_id) |
            (PromptBaseStructure.company_id == None)
        )
        base_res = await db.execute(stmt_base)
        base_mismatches = base_res.all()

        print(f"\nFound {len(base_mismatches)} Base Structures with missing/mismatching company_id:")
        for base, svc in base_mismatches:
            print(f"  - Base Structure {base.id} ({base.structure_name}): company_id={base.company_id} -> target={svc.company_id}")
            if apply:
                base.company_id = svc.company_id

        # 2. Prompts / Specific Structures
        stmt_prompt = select(Prompt, Service).join(
            Service, Prompt.service_id == Service.service_id
        ).where(
            (Prompt.company_id != Service.company_id) |
            (Prompt.company_id == None)
        )
        prompt_res = await db.execute(stmt_prompt)
        prompt_mismatches = prompt_res.all()

        print(f"\nFound {len(prompt_mismatches)} Prompts with missing/mismatching company_id:")
        for prompt, svc in prompt_mismatches:
            print(f"  - Prompt {prompt.prompt_id} ({prompt.prompt_name}): company_id={prompt.company_id} -> target={svc.company_id}")
            if apply:
                prompt.company_id = svc.company_id

        if apply:
            if len(base_mismatches) > 0 or len(prompt_mismatches) > 0:
                await db.commit()
                print("\n[OK] Changes committed successfully.")
            else:
                print("\n[OK] No changes needed.")
        else:
            print("\n[DRY-RUN] No changes committed. Run with '--apply' to apply updates.")


if __name__ == "__main__":
    apply = len(sys.argv) > 1 and sys.argv[1] == "--apply"
    asyncio.run(main(apply=apply))
