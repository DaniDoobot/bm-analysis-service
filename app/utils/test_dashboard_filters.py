"""
Async test script to validate the dashboard filters, multi-service support,
custom date range parsing and priority, dynamic typologies catalog,
objections, and agent evolution directly via AsyncSession.
"""
import sys
import os
import json
import logging
import asyncio
from datetime import datetime, timezone

# Add parent directory to path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

from app.db import SessionLocal
from app.services.dashboard_service import (
    get_dashboard_summary,
    get_agents_list,
    get_agent_evolution,
    get_objections_breakdown
)

def print_section(title: str):
    logger.info("=" * 60)
    logger.info(f" TEST CASE: {title}")
    logger.info("=" * 60)

async def test_case_runner():
    async with SessionLocal() as db:
        # 1. Dashboard summary with default (all services, default period)
        print_section("1. Dashboard summary (Default all services, 30d)")
        data = await get_dashboard_summary(db, analysis_type="audio", period="30d")
        logger.info(f"KPIs returned: {json.dumps(data.get('kpis'), indent=2)}")
        logger.info(f"Number of latest analyses: {len(data.get('latest_analyses', []))}")
        logger.info(f"Type distribution length: {len(data.get('type_distribution', []))}")
        if data.get('type_distribution'):
            logger.info(f"Sample type distribution item: {json.dumps(data['type_distribution'][0], indent=2)}")
        
        # 2. Dashboard summary filtered by service_key=front
        print_section("2. Dashboard summary filtered by service_key=front")
        data_front = await get_dashboard_summary(db, analysis_type="audio", period="30d", service_key="front")
        logger.info(f"KPIs (Front): {json.dumps(data_front.get('kpis'), indent=2)}")
        
        # 3. Dashboard summary filtered by service_key=experiencia_paciente
        print_section("3. Dashboard summary filtered by service_key=experiencia_paciente")
        data_exp = await get_dashboard_summary(db, analysis_type="audio", period="30d", service_key="experiencia_paciente")
        logger.info(f"KPIs (Experiencia de Paciente): {json.dumps(data_exp.get('kpis'), indent=2)}")
        logger.info(f"Type distribution for Experiencia de Paciente:")
        for t in data_exp.get('type_distribution', []):
            logger.info(f"  - Typology: {t.get('typology_name')} ({t.get('typology_key')}), Calls: {t.get('total_calls')}, Pct: {t.get('percentage')}%")
            
        # 4. Dashboard summary with custom date range
        print_section("4. Dashboard summary custom range (front, 2026-05-01 to 2026-05-26)")
        data_range = await get_dashboard_summary(db, 
            analysis_type="audio", 
            service_key="front",
            date_from="2026-05-01",
            date_to="2026-05-26"
        )
        logger.info(f"KPIs (Custom Range Front): {json.dumps(data_range.get('kpis'), indent=2)}")
        logger.info(f"Timeline interval: {len(data_range.get('calls_evolution', []))} buckets")
        
        # 5. Objections breakdown with custom date range
        print_section("5. Objections breakdown custom range (front, 2026-05-01 to 2026-05-26)")
        data_objs = await get_objections_breakdown(db, 
            analysis_type="audio",
            service_key="front",
            date_from="2026-05-01",
            date_to="2026-05-26"
        )
        logger.info(f"Total objection calls: {data_objs.get('total_objection_calls')}")
        logger.info(f"Total objection items: {data_objs.get('total_objection_items')}")
        logger.info(f"Top objections length: {len(data_objs.get('top_objections', []))}")
        logger.info(f"Agent groupings length: {len(data_objs.get('by_agent', []))}")
        
        # 6. Agents filtered by service
        print_section("6. Agents by service_key=front")
        agents_front = await get_agents_list(db, service_key="front")
        logger.info(f"Total agents returned (Front): {len(agents_front)}")
        for a in agents_front[:3]:
            logger.info(f"  - Agent: {a.get('agent_name')} (ID: {a.get('hubspot_owner_id')}), Analyses: {a.get('total_analyses')}")
            
        print_section("6b. Agents by service_key=experiencia_paciente")
        agents_exp = await get_agents_list(db, service_key="experiencia_paciente")
        logger.info(f"Total agents returned (Experiencia Paciente): {len(agents_exp)}")
        for a in agents_exp[:3]:
            logger.info(f"  - Agent: {a.get('agent_name')} (ID: {a.get('hubspot_owner_id')}), Analyses: {a.get('total_analyses')}")

        # 7. Agent evolution hourly (period=24h)
        print_section("7. Agent evolution (owner=1539993532, period=24h, service=front)")
        evo_24h = await get_agent_evolution(db, 
            hubspot_owner_id="1539993532",
            analysis_type="audio",
            period="24h",
            service_key="front",
            bucket_param="day"
        )
        logger.info(f"Agent: {evo_24h.get('agent')}")
        logger.info(f"Period: {evo_24h.get('period')}")
        logger.info(f"Summary metrics: {json.dumps(evo_24h.get('summary'), indent=2)}")
        logger.info(f"Timeline points: {len(evo_24h.get('timeline', []))}")
        if evo_24h.get('timeline'):
            logger.info(f"Sample timeline point: {json.dumps(evo_24h['timeline'][0], indent=2)}")

        # 8. Agent evolution custom range
        print_section("8. Agent evolution custom range (owner=1539993532, service=front, 2026-05-01 to 2026-05-26)")
        evo_range = await get_agent_evolution(db, 
            hubspot_owner_id="1539993532",
            analysis_type="audio",
            service_key="front",
            date_from="2026-05-01",
            date_to="2026-05-26",
            bucket_param="day"
        )
        logger.info(f"Agent: {evo_range.get('agent')}")
        logger.info(f"Timeline points: {len(evo_range.get('timeline', []))}")
        
        # 9. Mass evaluation results filtered by execution_source
        print_section("9. Mass evaluation results filtered by execution_source")
        from app.services.mass_evaluation_service import MassEvaluationService
        
        # Test default (all)
        results_all = await MassEvaluationService.list_results(db, limit=5)
        logger.info(f"Total results (all): {len(results_all)}")
        
        # Test on_demand
        results_on_demand = await MassEvaluationService.list_results(db, execution_source="on_demand", limit=5)
        logger.info(f"Total results (on_demand): {len(results_on_demand)}")
        for r in results_on_demand:
            logger.info(f"  - Call: {r.call_id}, Source: {r.execution_source}")
            assert r.execution_source == "on_demand", f"Expected 'on_demand', got '{r.execution_source}'"

        # Test automation
        results_auto = await MassEvaluationService.list_results(db, execution_source="automation", limit=5)
        logger.info(f"Total results (automation): {len(results_auto)}")
        for r in results_auto:
            logger.info(f"  - Call: {r.call_id}, Source: {r.execution_source}")
            assert r.execution_source == "automation", f"Expected 'automation', got '{r.execution_source}'"
            
        logger.info("\n=== ALL TEST CASES COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(test_case_runner())
