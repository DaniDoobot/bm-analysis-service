import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:
        print("=== day granularity: call_timestamp grouping ===")
        res = await conn.execute(text("""
            SELECT 
                r.call_timestamp::date AS period,
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls
            FROM bm_mass_evaluation_results r
            WHERE r.status = 'completed'
              AND r.service_key = 'front'
              AND r.call_timestamp >= '2026-04-22'::timestamptz
              AND r.call_timestamp <= '2026-05-29 23:59:59'::timestamptz
            GROUP BY r.call_timestamp::date
            ORDER BY period
        """))
        rows = res.fetchall()
        print(f"Total periods: {len(rows)}")
        for r in rows:
            print(f"  period={r[0]}  calls={r[1]}")

        print()
        print("=== week granularity ===")
        res2 = await conn.execute(text("""
            SELECT 
                date_trunc('week', r.call_timestamp)::date AS period,
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls
            FROM bm_mass_evaluation_results r
            WHERE r.status = 'completed'
              AND r.service_key = 'front'
              AND r.call_timestamp >= '2026-04-22'::timestamptz
              AND r.call_timestamp <= '2026-05-29 23:59:59'::timestamptz
            GROUP BY date_trunc('week', r.call_timestamp)::date
            ORDER BY period
        """))
        rows2 = res2.fetchall()
        print(f"Total weekly periods: {len(rows2)}")
        for r in rows2:
            print(f"  period={r[0]}  calls={r[1]}")

asyncio.run(main())
