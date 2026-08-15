import asyncio
from zoneinfo import ZoneInfo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

MADRID_TZ = ZoneInfo("Europe/Madrid")


async def main():
    db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    engine = create_async_engine(db_url, echo=False)
    async with AsyncSession(engine) as db:
        # Check total MassEvaluationResult by day in Madrid time
        q_results = text("""
            SELECT date_trunc('day', call_timestamp AT TIME ZONE 'Europe/Madrid') as day,
                   count(*) as total_results,
                   count(case when call_duration_seconds >= 120 then 1 end) as dur_gte_120
            FROM bm_mass_evaluation_results
            GROUP BY day
            ORDER BY day DESC
            LIMIT 15;
        """)
        res = await db.execute(q_results)
        print("HISTÓRICO DE RESULTADOS EN bm_mass_evaluation_results POR DÍA (Madrid):")
        print(f"{'Dia (Madrid)':<25} | {'Total Resultados':<18} | {'Duracion >= 120s'}")
        print("-" * 65)
        for r in res.fetchall():
            print(f"{str(r[0]):<25} | {r[1]:<18} | {r[2]}")

        # Check total runs with calls_found > 0 in August 2026
        q_runs = text("""
            SELECT run_id, trigger_type, started_at AT TIME ZONE 'Europe/Madrid' as st_madrid, calls_found, calls_selected, calls_analyzed, effective_filters
            FROM bm_mass_evaluation_runs
            WHERE calls_found > 0
            ORDER BY run_id DESC
            LIMIT 10;
        """)
        res_r = await db.execute(q_runs)
        print("\nÚLTIMOS 10 MASS RUNS CON CALLS_FOUND > 0:")
        for r in res_r.fetchall():
            print(f"Run {r[0]} ({r[1]}) at {r[2]}: found={r[3]}, sel={r[4]}, analyzed={r[5]}")
            eff = r[6]
            if eff:
                print(f"   Filters: date_from={eff.get('date_from')}, date_to={eff.get('date_to')}, dur_min={eff.get('duration_min_seconds')}")

        # Check latest individual analyses in bm_analyses
        q_analyses = text("""
            SELECT date_trunc('day', call_timestamp AT TIME ZONE 'Europe/Madrid') as day,
                   count(*) as total_analyses
            FROM bm_analyses
            GROUP BY day
            ORDER BY day DESC
            LIMIT 15;
        """)
        res_a = await db.execute(q_analyses)
        print("\nHISTÓRICO EN bm_analyses POR DÍA (Madrid):")
        for r in res_a.fetchall():
            print(f"{str(r[0]):<25} | {r[1]}")


if __name__ == "__main__":
    asyncio.run(main())
