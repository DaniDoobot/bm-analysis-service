"""E2E verification tests for Service Evolution dashboard endpoints."""
import asyncio
import os
import sys
from decimal import Decimal
import httpx

# Add app to path
sys.path.insert(0, os.path.abspath("."))

from app.db import get_engine
from app.main import app
from app.services.db_init_service import init_db
from sqlalchemy import text


async def test_pipeline():
    print("=== STARTING SERVICE EVOLUTION VERIFICATION PIPELINE ===")
    
    # 1. Force database schema creation/upgrade
    print("Step 1: Running db_init_service to ensure everything is prepared...")
    await init_db()
    
    engine = get_engine()
    
    # 2. Seed temporary mock data for E2E tests with explicit fixed timestamps
    print("\nStep 2: Seeding mock mass evaluation data...")
    async with engine.begin() as conn:
        # Delete any leftover test data first
        await conn.execute(text("DELETE FROM bm_mass_evaluation_criterion_results WHERE mass_analysis_id IN (88801, 88802);"))
        await conn.execute(text("DELETE FROM bm_mass_evaluation_results WHERE mass_analysis_id IN (88801, 88802);"))
        await conn.execute(text("DELETE FROM bm_mass_evaluation_runs WHERE run_id = 77;"))
        await conn.execute(text("DELETE FROM bm_mass_evaluation_jobs WHERE job_id = 77;"))
        
        # Insert Front service if it somehow doesn't exist
        s_res = await conn.execute(text("SELECT service_id FROM bm_services WHERE service_key = 'front';"))
        s_row = s_res.fetchone()
        front_id = s_row[0] if s_row else 1
        
        # Seed parent Job and Run records to satisfy foreign keys
        await conn.execute(text("""
            INSERT INTO bm_mass_evaluation_jobs (job_id, job_name, prompt_id, date_mode)
            VALUES (77, 'E2E Test Job', 1, 'relative');
        """))
        
        await conn.execute(text("""
            INSERT INTO bm_mass_evaluation_runs (run_id, job_id, trigger_type, status)
            VALUES (77, 77, 'manual', 'completed');
        """))
        
        # Insert mass evaluation results with all required columns (including prompt_id and prompt_snapshot)
        # Use fictitious agent info: hubspot_owner_id = '999999999'
        # Seed record 88802 exactly at 2026-05-22 10:00:00+00
        await conn.execute(text(f"""
            INSERT INTO bm_mass_evaluation_results (
                mass_analysis_id, job_id, run_id, call_id, status, 
                service_id, service_name, service_key, 
                typology_id, typology_key, typology_name, agent_name, hubspot_owner_id, 
                prompt_id, prompt_snapshot, created_at
            ) VALUES 
            (
                88801, 77, 77, 'call_e2e_1', 'completed', 
                :front_id, 'Front', 'front',
                1, 'cita', 'Cita', 'Agente Ficticio E2E', '999999999',
                1, '{{}}', '2026-05-21 14:30:00+00'
            ),
            (
                88802, 77, 77, 'call_e2e_2', 'completed', 
                :front_id, 'Front', 'front',
                1, 'cita', 'Cita', 'Agente Ficticio E2E', '999999999',
                1, '{{}}', '2026-05-22 10:00:00+00'
            );
        """), {"front_id": front_id})
        
        # Insert criteria results for 88801 (including evaluacion_global)
        criteria_1 = [
            ("evaluacion_global", "Evaluacion Global", "score_1_10", Decimal("8.00"), True, False),
            ("claridad", "Claridad", "score_1_10", Decimal("8.00"), True, False),
            ("empatia", "Empatia", "score_1_10", Decimal("7.00"), True, False),
            ("procedimiento", "Procedimiento", "score_1_10", Decimal("9.00"), True, False),
            ("sentiment", "Sentiment", "score_1_10", Decimal("8.00"), True, False),
            ("cierre_cita", "Cierre de Cita", "boolean", None, True, False, True),
        ]
        for c_key, c_name, c_type, num, is_app, not_app, *bool_val in criteria_1:
            bv = bool_val[0] if bool_val else None
            await conn.execute(text("""
                INSERT INTO bm_mass_evaluation_criterion_results (
                    mass_analysis_id, run_id, job_id, call_id, criterion_key, criterion_name, criterion_type,
                    numeric_value, boolean_value, is_applicable, not_applicable, 
                    service_id, service_name, service_key, typology_id, typology_key, typology_name,
                    created_at
                ) VALUES (
                    88801, 77, 77, 'call_e2e_1', :c_key, :c_name, :c_type,
                    :num, :bv, :is_app, :not_app,
                    :front_id, 'Front', 'front', 1, 'cita', 'Cita',
                    '2026-05-21 14:30:00+00'
                );
            """), {"c_key": c_key, "c_name": c_name, "c_type": c_type, "num": num, "bv": bv, "is_app": is_app, "not_app": not_app, "front_id": front_id})

        # Insert criteria results for 88802 (including evaluacion_global)
        criteria_2 = [
            ("evaluacion_global", "Evaluacion Global", "score_1_10", Decimal("7.00"), True, False),
            ("claridad", "Claridad", "score_1_10", Decimal("7.00"), True, False),
            ("empatia", "Empatia", "score_1_10", Decimal("8.00"), True, False),
            ("procedimiento", "Procedimiento", "score_1_10", Decimal("6.00"), True, False),
            ("sentiment", "Sentiment", "score_1_10", Decimal("7.00"), True, False),
            ("cierre_cita", "Cierre de Cita", "boolean", None, True, False, False),
        ]
        for c_key, c_name, c_type, num, is_app, not_app, *bool_val in criteria_2:
            bv = bool_val[0] if bool_val else None
            await conn.execute(text("""
                INSERT INTO bm_mass_evaluation_criterion_results (
                    mass_analysis_id, run_id, job_id, call_id, criterion_key, criterion_name, criterion_type,
                    numeric_value, boolean_value, is_applicable, not_applicable, 
                    service_id, service_name, service_key, typology_id, typology_key, typology_name,
                    created_at
                ) VALUES (
                    88802, 77, 77, 'call_e2e_2', :c_key, :c_name, :c_type,
                    :num, :bv, :is_app, :not_app,
                    :front_id, 'Front', 'front', 1, 'cita', 'Cita',
                    '2026-05-22 10:00:00+00'
                );
            """), {"c_key": c_key, "c_name": c_name, "c_type": c_type, "num": num, "bv": bv, "is_app": is_app, "not_app": not_app, "front_id": front_id})

    print("Mock data seeded successfully.")

    # 3. HTTP Tests using httpx.AsyncClient with ASGITransport
    print("\nStep 3: Executing API tests with httpx.AsyncClient...")
    from httpx import ASGITransport
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        
        # Validation 1: GET /bm/service-evolution/services
        print("Testing GET /bm/service-evolution/services with date filters...")
        res_s = await client.get("/bm/service-evolution/services?date_from=2026-05-03&date_to=2026-05-22")
        assert res_s.status_code == 200, f"Failed services list: {res_s.status_code}"
        services_list = res_s.json()
        print(f"Services List: {services_list}")
        front_service = next((s for s in services_list if s["service_key"] == "front"), None)
        assert front_service is not None, "Front service not found in list"
        print("Validation 1 Passed!")

        # Validation 2: GET /bm/service-evolution/criteria
        print("Testing GET /bm/service-evolution/criteria with date filters...")
        res_c = await client.get(f"/bm/service-evolution/criteria?service_id={front_id}&date_from=2026-05-03&date_to=2026-05-22")
        assert res_c.status_code == 200, f"Failed criteria list: {res_c.status_code}"
        criteria_list = res_c.json()
        print(f"Criteria List (first 3): {criteria_list[:3]}")
        clarity_criterion = next((c for c in criteria_list if c["criterion_key"] == "claridad"), None)
        assert clarity_criterion is not None, "claridad criterion not found in list"
        print("Criteria Validation Passed!")

        # Validation 3: GET /bm/service-evolution?service_key=front (isolated by agent_owner_id and date boundaries)
        # This confirms that date_to=2026-05-22 correctly INCLUDES the call from 2026-05-22 10:00:00!
        print("Testing GET /bm/service-evolution?service_key=front with inclusive date_to=2026-05-22...")
        res_e = await client.get("/bm/service-evolution?service_key=front&granularity=day&agent_owner_id=999999999&date_from=2026-05-03&date_to=2026-05-22")
        assert res_e.status_code == 200, f"Failed evolution query: {res_e.status_code}"
        evo_data = res_e.json()
        
        # Assert filters
        assert evo_data["filters"]["service_key"] == "front"
        assert evo_data["filters"]["granularity"] == "day"
        assert evo_data["filters"]["date_from"] == "2026-05-03"
        assert evo_data["filters"]["date_to"] == "2026-05-22"
        
        # Assert summary - Must find BOTH calls (the one on 21st and the one on 22nd at 10:00:00)
        summary = evo_data["summary"]
        print(f"Summary: {summary}")
        assert summary["total_calls"] == 2, f"Expected exactly 2 calls, got {summary['total_calls']}! Bug with inclusive date_to is still present!"
        assert summary["avg_evaluacion_global"] == 7.5, f"Expected avg global 7.5, got {summary['avg_evaluacion_global']}"
        assert summary["avg_claridad"] == 7.5, f"Expected avg claridad 7.5, got {summary['avg_claridad']}"
        assert summary["avg_empatia"] == 7.5, f"Expected avg empatia 7.5, got {summary['avg_empatia']}"
        assert summary["avg_procedimiento"] == 7.5, f"Expected avg procedimiento 7.5, got {summary['avg_procedimiento']}"
        assert summary["cierre_cita_rate"] == 0.5, f"Expected cierre_cita_rate 0.5, got {summary['cierre_cita_rate']}"
        assert summary["main_typology"] == "Cita"
        print("Validation 3 (Inclusive date_to Summary Metrics) Passed!")

        # Assert series
        series = evo_data["series"]
        print(f"Series: {series}")
        assert len(series) == 2, f"Expected exactly 2 periods in series, got {len(series)}"
        print("Validation 3 (Series Metrics) Passed!")

        # Assert typology breakdown
        by_typo = evo_data["by_typology"]
        print(f"By Typology: {by_typo}")
        cita_typo = next((t for t in by_typo if t["typology_key"] == "cita"), None)
        assert cita_typo is not None, "cita typology not found in breakdown"
        assert cita_typo["total_calls"] == 2
        print("Validation 3 (Typology Breakdown) Passed!")

        # Assert agent breakdown
        by_agent = evo_data["by_agent"]
        print(f"By Agent: {by_agent}")
        luci_agent = next((a for a in by_agent if a["agent_owner_id"] == "999999999"), None)
        assert luci_agent is not None, "Luci agent not found in agent breakdown"
        assert luci_agent["agent_name"] == "Agente Ficticio E2E"
        assert luci_agent["total_calls"] == 2
        print("Validation 3 (Agent Breakdown) Passed!")

    # 4. Clean up mock data
    print("\nStep 4: Cleaning up mock mass evaluation data...")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM bm_mass_evaluation_criterion_results WHERE mass_analysis_id IN (88801, 88802);"))
        await conn.execute(text("DELETE FROM bm_mass_evaluation_results WHERE mass_analysis_id IN (88801, 88802);"))
        await conn.execute(text("DELETE FROM bm_mass_evaluation_runs WHERE run_id = 77;"))
        await conn.execute(text("DELETE FROM bm_mass_evaluation_jobs WHERE job_id = 77;"))
    print("Cleanup complete.")

    print("\n=== ALL E2E SERVICE EVOLUTION DATE BOUNDARY TESTS PASSED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(test_pipeline())
