"""
Validation for ERR-05: Dashboard 'Últimas conversaciones' (latest_analyses) filter consistency.
================================================================================================
Validates that get_dashboard_summary produces latest_analyses strictly from the filtered universe:
1. Without filters (30d base)
2. Service Front (service_id=1, service='front')
3. item_filters = Empatía 7-10
4. item_filters = Precio de consulta = True
5. Combined item_filters (Empatía 7-10 AND Precio de consulta = True)
6. Agent filter (hubspot_owner_id / agent_id)
7. Verification that NO conversation returned contradicts any active filter.
"""
import asyncio
import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import get_settings
from app.db import _make_async_url, AsyncSession
from app.core.tenant_context import TenantContext, InternalRole
from app.models.mass_evaluations import MassEvaluationResult
from app.services.dashboard_service import (
    get_dashboard_summary,
    extract_score_from_mass,
)
from app.utils.item_score_filters import extract_boolean_from_mass


async def run_err05_verification():
    settings = get_settings()
    raw_url = os.environ.get("DATABASE_URL") or settings.database_url
    if not raw_url:
        print("[ERROR] DATABASE_URL is not configured.")
        return
    db_url = _make_async_url(raw_url)
    engine = create_async_engine(db_url, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    ctx = TenantContext(
        user_id=1,
        email="admin@bostonmedical.es",
        raw_role="super_admin",
        normalized_role=InternalRole.SUPER_ADMIN,
        is_super_admin=True,
        allowed_company_ids=[1],
        allowed_service_ids=[1],
    )

    print("=" * 100)
    print("READ-ONLY VERIFICATION ERR-05: DASHBOARD 'ÚLTIMAS CONVERSACIONES' (latest_analyses)")
    print("=" * 100)

    async with async_session() as session:
        # Case 1: Without filters (default 30d window for Front)
        print("\n[Case 1] Dashboard without specific filters (Front service_id=1, period=30d):")
        res1 = await get_dashboard_summary(session, service_id=1, period="30d", context=ctx)
        latest1 = res1.get("latest_analyses", [])
        total1 = res1.get("summary", {}).get("total_analyses", 0)
        print(f"  -> Total analyses in summary: {total1}")
        print(f"  -> Latest analyses count: {len(latest1)} (max 8)")
        assert len(latest1) <= 8
        for i, item in enumerate(latest1[:3]):
            print(f"     ({i+1}) ID={item.get('analysis_id')}, Agent={item.get('agente_telefonico')}, Score={item.get('evaluacion_global')}, Date={item.get('call_timestamp')}")

        # Case 2: Service Front by name/slug ('front')
        print("\n[Case 2] Dashboard for Service Front using service_key='front':")
        res2 = await get_dashboard_summary(session, service_key="front", period="30d", context=ctx)
        latest2 = res2.get("latest_analyses", [])
        total2 = res2.get("summary", {}).get("total_analyses", 0)
        print(f"  -> Total analyses in summary: {total2}")
        print(f"  -> Latest analyses count: {len(latest2)}")
        assert total2 == total1

        # Case 3: item_filters: Empatía 7-10
        print("\n[Case 3] Dashboard with item_filters = Empatía [7.0, 10.0]:")
        filt_num = json.dumps([{"key": "empatia", "min": 7.0, "max": 10.0}])
        res3 = await get_dashboard_summary(session, service_id=1, period="30d", item_filters=filt_num, context=ctx)
        latest3 = res3.get("latest_analyses", [])
        total3 = res3.get("summary", {}).get("total_analyses", 0)
        print(f"  -> Total analyses in summary: {total3}")
        print(f"  -> Latest analyses count: {len(latest3)}")
        assert total3 <= total1
        for i, item in enumerate(latest3):
            mid = item.get("analysis_id")
            r_db = (await session.execute(select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == mid))).scalar_one_or_none()
            emp_val = extract_score_from_mass(r_db.result_json, r_db.items_json, "empatia") if r_db else None
            print(f"     ({i+1}) ID={mid}, Agent={item.get('agente_telefonico')}, Global={item.get('evaluacion_global')}, Empatia={emp_val}")
            assert emp_val is not None and 7.0 <= float(emp_val) <= 10.0, f"Error: ID={mid} Empatia={emp_val} not in [7, 10]"

        # Case 4: item_filters: Precio de consulta = True
        print("\n[Case 4] Dashboard with item_filters = Precio de consulta = True:")
        filt_bool = json.dumps([{"key": "precio_consulta", "value": True}])
        res4 = await get_dashboard_summary(session, service_id=1, period="30d", item_filters=filt_bool, context=ctx)
        latest4 = res4.get("latest_analyses", [])
        total4 = res4.get("summary", {}).get("total_analyses", 0)
        print(f"  -> Total analyses in summary: {total4}")
        print(f"  -> Latest analyses count: {len(latest4)}")
        assert total4 <= total1
        for i, item in enumerate(latest4):
            mid = item.get("analysis_id")
            r_db = (await session.execute(select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == mid))).scalar_one_or_none()
            pc_val = extract_boolean_from_mass(r_db.result_json, r_db.items_json, "precio_consulta") if r_db else None
            print(f"     ({i+1}) ID={mid}, Agent={item.get('agente_telefonico')}, Global={item.get('evaluacion_global')}, PrecioConsulta={pc_val}")
            assert pc_val is True, f"Error: ID={mid} PrecioConsulta={pc_val} is not True"

        # Case 5: Combined item_filters (Empatía 7-10 AND Precio de consulta = True)
        print("\n[Case 5] Dashboard with Combined Filters (Empatía [7, 10] AND Precio de consulta = True):")
        filt_comb = json.dumps([
            {"key": "empatia", "min": 7.0, "max": 10.0},
            {"key": "precio_consulta", "value": True}
        ])
        res5 = await get_dashboard_summary(session, service_id=1, period="30d", item_filters=filt_comb, context=ctx)
        latest5 = res5.get("latest_analyses", [])
        total5 = res5.get("summary", {}).get("total_analyses", 0)
        print(f"  -> Total analyses in summary: {total5}")
        print(f"  -> Latest analyses count: {len(latest5)}")
        assert total5 <= total3
        assert total5 <= total4
        for i, item in enumerate(latest5):
            mid = item.get("analysis_id")
            r_db = (await session.execute(select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == mid))).scalar_one_or_none()
            emp_val = extract_score_from_mass(r_db.result_json, r_db.items_json, "empatia") if r_db else None
            pc_val = extract_boolean_from_mass(r_db.result_json, r_db.items_json, "precio_consulta") if r_db else None
            print(f"     ({i+1}) ID={mid}, Agent={item.get('agente_telefonico')}, Global={item.get('evaluacion_global')}, Empatia={emp_val}, PrecioConsulta={pc_val}")
            assert emp_val is not None and 7.0 <= float(emp_val) <= 10.0, f"Error: ID={mid} Empatia={emp_val} not in [7, 10]"
            assert pc_val is True, f"Error: ID={mid} PrecioConsulta={pc_val} is not True"

        # Case 6: Dashboard with Agent filter (e.g. Eugenia Carreno)
        sample_agent_id = latest1[0].get("hubspot_owner_id") if latest1 else None
        if sample_agent_id:
            print(f"\n[Case 6] Dashboard with Agent filter (hubspot_owner_id='{sample_agent_id}'):")
            res6 = await get_dashboard_summary(session, service_id=1, period="30d", hubspot_owner_id=sample_agent_id, context=ctx)
            latest6 = res6.get("latest_analyses", [])
            total6 = res6.get("summary", {}).get("total_analyses", 0)
            print(f"  -> Total analyses for agent: {total6}")
            print(f"  -> Latest analyses count: {len(latest6)}")
            for i, item in enumerate(latest6):
                assert item.get("hubspot_owner_id") == sample_agent_id, f"Error: item owner {item.get('hubspot_owner_id')} != {sample_agent_id}"
                print(f"     ({i+1}) ID={item.get('analysis_id')}, Agent={item.get('agente_telefonico')}, OwnerID={item.get('hubspot_owner_id')}")

        print("\n" + "=" * 100)
        print("ALL 6 CASES PASSED - NO CONVERSATION CONTRADICTS ACTIVE FILTERS")
        print("=" * 100)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_err05_verification())
