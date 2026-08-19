"""
Read-Only Production Validation for ERR-03 / ERR-04 (Item Score & Boolean Filters).
===================================================================================
Executes safe SELECT queries only against the production database:
1. Validates /bm/evaluation-items/filter-options for service Front.
2. Validates MassEvaluationService.count_results and list_results with:
   - No item_filters
   - Real numeric filter
   - Real boolean filter
   - Combination filter (numeric + boolean)
3. Validates dashboard_service.get_dashboard_summary with item_filters.
"""
import asyncio
import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import get_settings
from app.db import _make_async_url, AsyncSession

from app.core.tenant_context import TenantContext, InternalRole
from app.services.mass_evaluation_service import MassEvaluationService
from app.services.dashboard_service import get_dashboard_summary
from app.utils.item_score_filters import (
    get_evaluation_item_filter_options,
    parse_item_score_filters_detailed,
)


async def run_readonly_verification():
    settings = get_settings()
    raw_url = os.environ.get("DATABASE_URL") or settings.database_url
    if not raw_url:
        print("[ERROR] DATABASE_URL is not set.")
        return
    db_url = _make_async_url(raw_url)
    engine = create_async_engine(db_url, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print("=" * 80)
    print("READ-ONLY VERIFICATION AGAINST PRODUCTION FOR ERR-03 / ERR-04")
    print("=" * 80)

    async with async_session() as session:
        # 1. Resolve Front service_id (usually 1)
        from app.utils.service_resolvers import resolve_service_id
        front_service_id, front_service_key = await resolve_service_id(session, service_param="front")
        print(f"\n[1] Front Service Resolved: ID={front_service_id}, Key={front_service_key}")

        # 2. Test get_evaluation_item_filter_options
        print("\n[2] Testing get_evaluation_item_filter_options for Front:")
        options = await get_evaluation_item_filter_options(session, service_ids=[front_service_id] if front_service_id else None)
        print(f"  -> Total Criteria Found: {len(options)}")
        
        numeric_items = [o for o in options if o["type"] == "score"]
        boolean_items = [o for o in options if o["type"] == "boolean"]
        
        print(f"  -> Numeric Criteria count: {len(numeric_items)}")
        for n in numeric_items[:5]:
            print(f"     * key='{n['key']}', label='{n['label']}', min={n.get('min_score')}, max={n.get('max_score')}")

        print(f"  -> Boolean Criteria count: {len(boolean_items)}")
        for b in boolean_items[:5]:
            print(f"     * key='{b['key']}', label='{b['label']}', options={b.get('options')}")

        assert len(numeric_items) > 0, "Expected at least one numeric criterion"
        assert len(boolean_items) > 0, "Expected at least one boolean criterion"

        sample_num_key = numeric_items[0]["key"]
        sample_bool_key = boolean_items[0]["key"]

        # 3. Test MassEvaluationService.count_results and list_results
        print(f"\n[3] Testing MassEvaluationService call results for Front (service_id={front_service_id}):")

        # 3A. Without filters
        total_base = await MassEvaluationService.count_results(session, service_id=front_service_id)
        list_base = await MassEvaluationService.list_results(session, service_id=front_service_id, limit=5)
        print(f"  [3A] Base (No item_filters): Total={total_base}, Retrieved={len(list_base)} rows")

        # 3B. Numeric filter (e.g. key >= 7.0)
        num_filter_str = json.dumps([{"key": sample_num_key, "min": 7.0, "max": 10.0}])
        total_num = await MassEvaluationService.count_results(session, service_id=front_service_id, item_filters=num_filter_str)
        list_num = await MassEvaluationService.list_results(session, service_id=front_service_id, item_filters=num_filter_str, limit=5)
        print(f"  [3B] Numeric Filter ({sample_num_key} in [7.0, 10.0]): Total={total_num}, Retrieved={len(list_num)} rows")
        assert total_num <= total_base, f"Expected total_num ({total_num}) <= total_base ({total_base})"

        # 3C. Boolean filter (e.g. key == True)
        bool_filter_str = json.dumps([{"key": sample_bool_key, "value": True}])
        total_bool = await MassEvaluationService.count_results(session, service_id=front_service_id, item_filters=bool_filter_str)
        list_bool = await MassEvaluationService.list_results(session, service_id=front_service_id, item_filters=bool_filter_str, limit=5)
        print(f"  [3C] Boolean Filter ({sample_bool_key}=True): Total={total_bool}, Retrieved={len(list_bool)} rows")
        assert total_bool <= total_base, f"Expected total_bool ({total_bool}) <= total_base ({total_base})"

        # 3D. Combined filter (Numeric + Boolean)
        comb_num_key = "empatia" if any(o["key"] == "empatia" for o in numeric_items) else sample_num_key
        comb_bool_key = "precio_consulta" if any(o["key"] == "precio_consulta" for o in boolean_items) else sample_bool_key
        comb_filter_str = json.dumps([
            {"key": comb_num_key, "min": 7.0, "max": 10.0},
            {"key": comb_bool_key, "value": True}
        ])
        total_comb = await MassEvaluationService.count_results(session, service_id=front_service_id, item_filters=comb_filter_str)
        list_comb = await MassEvaluationService.list_results(session, service_id=front_service_id, item_filters=comb_filter_str, limit=5)
        print(f"  [3D] Combined Filter ({comb_num_key}>=7.0 AND {comb_bool_key}=True): Total={total_comb}, Retrieved={len(list_comb)} rows")

        # 4. Test get_dashboard_summary with item_filters
        print(f"\n[4] Testing get_dashboard_summary with item_filters on Front:")
        ctx = TenantContext(
            user_id=1,
            email="admin@test.com",
            raw_role="super_admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
            allowed_service_ids=[front_service_id] if front_service_id else None
        )
        dash_res = await get_dashboard_summary(
            session,
            service_id=front_service_id,
            period="30d",
            item_filters=comb_filter_str,
            context=ctx
        )
        total_summary_analyses = dash_res.get("summary", {}).get("total_analyses")
        avg_eval_kpi = dash_res.get("kpis", {}).get("avg_eval")
        norm_filters = dash_res.get("filters", {}).get("item_filters_normalized")
        print(f"  -> Dashboard Summary total_analyses: {total_summary_analyses}")
        print(f"  -> Dashboard Summary avg_eval KPI: {avg_eval_kpi}")
        print(f"  -> Dashboard Summary normalized filters: {norm_filters}")
        assert "summary" in dash_res, "Dashboard response must contain summary"
        assert "kpis" in dash_res, "Dashboard response must contain kpis"
        assert "filters" in dash_res, "Dashboard response must contain filters"

        print("\n" + "=" * 80)
        print("READ-ONLY PRODUCTION VERIFICATION PASSED SUCCESSFULLY")
        print("=" * 80)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_readonly_verification())
