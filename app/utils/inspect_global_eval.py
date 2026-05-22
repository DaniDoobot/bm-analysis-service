import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:
        # Check how many results have evaluacion_global in result_json
        res = await conn.execute(text("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN (result_json->>'evaluacion_global') IS NOT NULL THEN 1 END) as has_global,
                AVG(CAST(result_json->>'evaluacion_global' AS double precision)) as avg_global,
                MIN(CAST(result_json->>'evaluacion_global' AS double precision)) as min_global,
                MAX(CAST(result_json->>'evaluacion_global' AS double precision)) as max_global
            FROM bm_mass_evaluation_results
            WHERE result_json IS NOT NULL;
        """))
        row = res.fetchone()
        print("Stats of evaluacion_global in result_json:")
        print(f"  Total rows with result_json: {row[0]}")
        print(f"  Rows with evaluacion_global: {row[1]}")
        print(f"  Average global: {row[2]}")
        print(f"  Min global: {row[3]}")
        print(f"  Max global: {row[4]}")

        # Check for service 'front' specifically
        res_front = await conn.execute(text("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN (result_json->>'evaluacion_global') IS NOT NULL THEN 1 END) as has_global,
                AVG(CAST(result_json->>'evaluacion_global' AS double precision)) as avg_global
            FROM bm_mass_evaluation_results
            WHERE service_key = 'front' AND result_json IS NOT NULL;
        """))
        row_front = res_front.fetchone()
        print("\nStats for service 'front' specifically:")
        print(f"  Total front rows: {row_front[0]}")
        print(f"  Front rows with evaluacion_global: {row_front[1]}")
        print(f"  Average global: {row_front[2]}")

if __name__ == "__main__":
    asyncio.run(main())
