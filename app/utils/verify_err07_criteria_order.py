"""
Validation for ERR-07: Criteria order in GET /bm/evaluation-items/filter-options matching Estructura Específica.
=================================================================================================================
Performs read-only validation against production database for Front (service_id=1) and Global query:
1. Top 15 criteria in active structure (bm_prompt_criteria) ordered by order_index ASC.
2. Top 15 criteria returned by get_evaluation_item_filter_options(service_ids=[1]).
3. Exact sequence match verification.
4. Deterministic multi-service ordering in global view (service_ids=None).
"""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import get_settings
from app.db import _make_async_url, AsyncSession
from app.models.prompts import Prompt
from app.models.criteria import PromptCriterion
from app.utils.item_score_filters import get_evaluation_item_filter_options


async def run_err07_verification():
    settings = get_settings()
    raw_url = os.environ.get("DATABASE_URL") or settings.database_url
    if not raw_url:
        print("[ERROR] DATABASE_URL is not configured.")
        return
    db_url = _make_async_url(raw_url)
    engine = create_async_engine(db_url, connect_args={"ssl": False}, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print("=" * 100)
    print("READ-ONLY VERIFICATION ERR-07: CRITERIA CATALOG ORDERING (Estructura Específica vs Filter-Options)")
    print("=" * 100)

    async with async_session() as session:
        # 1. Front (service_id=1) Active Structure
        prompt_res = await session.execute(
            select(Prompt.prompt_id, Prompt.prompt_name)
            .where(
                Prompt.service_id == 1,
                Prompt.prompt_type == "audio",
                Prompt.is_active == True,
                Prompt.is_archived == False,
                Prompt.deleted_at.is_(None)
            )
            .order_by(Prompt.prompt_id.desc())
            .limit(1)
        )
        front_prompt = prompt_res.first()
        assert front_prompt is not None, "Active prompt for Front not found"
        prompt_id, prompt_name = front_prompt

        crit_res = await session.execute(
            select(
                PromptCriterion.order_index,
                PromptCriterion.criterion_key,
                PromptCriterion.criterion_name,
                PromptCriterion.criterion_type,
                PromptCriterion.output_key
            )
            .where(
                PromptCriterion.prompt_id == prompt_id,
                PromptCriterion.is_active == True,
                PromptCriterion.deleted_at.is_(None),
                PromptCriterion.criterion_type.in_(["score_1_10", "score", "number", "boolean", "percentage"])
            )
            .order_by(
                PromptCriterion.order_index.asc().nullslast(),
                PromptCriterion.criterion_id.asc()
            )
        )
        structure_criteria = crit_res.all()

        print(f"\n[1] Front Active Structure: Prompt ID={prompt_id} ('{prompt_name}')")
        print(f"    Total Evaluative Criteria in Structure: {len(structure_criteria)}")
        print("\n--- TOP 15 CRITERIA IN ESTRUCTURA ESPECÍFICA (bm_prompt_criteria) ---")
        print(f"{'#':<3} | {'OrderIdx':<8} | {'Key':<32} | {'Name':<35} | {'Type':<12}")
        print("-" * 95)
        top_structure_keys = []
        for idx, c in enumerate(structure_criteria[:15]):
            ckey = c[1] or c[4]
            top_structure_keys.append(ckey)
            print(f"{idx+1:<3} | {str(c[0]):<8} | {ckey:<32} | {str(c[2]):<35} | {str(c[3]):<12}")

        # 2. Query get_evaluation_item_filter_options for service_ids=[1]
        catalog_options = await get_evaluation_item_filter_options(session, service_ids=[1])
        print(f"\n[2] Catalog Output from get_evaluation_item_filter_options(service_ids=[1])")
        print(f"    Total Options in Catalog: {len(catalog_options)}")
        print("\n--- TOP 15 CRITERIA RETURNED BY CATALOG ---")
        print(f"{'#':<3} | {'SortOrder':<9} | {'Key':<32} | {'Label':<35} | {'Type':<12}")
        print("-" * 95)
        top_catalog_keys = []
        for idx, o in enumerate(catalog_options[:15]):
            top_catalog_keys.append(o["key"])
            print(f"{idx+1:<3} | {str(o.get('sort_order')):<9} | {o.get('key'):<32} | {o.get('label'):<35} | {o.get('type'):<12}")

        # 3. Match Assertion
        print("\n[3] Order Sequence Verification:")
        for idx in range(15):
            s_key = top_structure_keys[idx]
            c_key = top_catalog_keys[idx]
            match = "MATCH" if s_key == c_key else "MISMATCH"
            print(f"    Position {idx+1:2d}: Structure='{s_key}' <--> Catalog='{c_key}' [{match}]")
            assert s_key == c_key, f"Mismatch at position {idx+1}: {s_key} != {c_key}"

        print("\n--> CONFIRMATION: Top 15 criteria match 100% in exact order.")

        # 4. Check Global view (service_ids=None)
        global_options = await get_evaluation_item_filter_options(session, service_ids=None)
        print(f"\n[4] Global Catalog Output (service_ids=None)")
        print(f"    Total Options: {len(global_options)}")
        print(f"    Top 5 Global Keys: {[o['key'] for o in global_options[:5]]}")
        # Verify deduplication
        all_keys = [o["key"] for o in global_options]
        assert len(all_keys) == len(set(all_keys)), "Global keys contain duplicates"
        print("    Deduplication verification: PASSED (0 duplicate keys)")

        # Verify contiguous sort_order
        for idx, o in enumerate(global_options):
            assert o["sort_order"] == idx + 1, f"Sort order mismatch at {idx}: {o['sort_order']}"
        print("    Contiguous sort_order verification: PASSED")

        print("\n" + "=" * 100)
        print("ALL VERIFICATIONS PASSED SUCCESSFULLY")
        print("=" * 100)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_err07_verification())
