import asyncio
import json
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    print("Connecting to database...")
    async with engine.connect() as conn:
        # Check total calls
        res = await conn.execute(text("SELECT COUNT(*) FROM bm_mass_evaluation_results;"))
        print("Total mass evaluation results:", res.scalar())

        # Check total criteria rows
        res = await conn.execute(text("SELECT COUNT(*) FROM bm_mass_evaluation_criterion_results;"))
        print("Total mass evaluation criterion results:", res.scalar())

        # List distinct criterion keys
        res = await conn.execute(text("""
            SELECT criterion_key, COUNT(*) 
            FROM bm_mass_evaluation_criterion_results 
            GROUP BY criterion_key 
            ORDER BY count DESC;
        """))
        print("\nDistinct criterion keys:")
        for row in res.fetchall():
            print(f"  {row[0]}: {row[1]}")

        # Check a sample result_json structure
        res = await conn.execute(text("""
            SELECT result_json 
            FROM bm_mass_evaluation_results 
            WHERE result_json IS NOT NULL 
            LIMIT 1;
        """))
        row = res.fetchone()
        if row:
            print("\nSample result_json keys:")
            print(json.dumps(list(row[0].keys()), indent=2))
        else:
            print("\nNo result_json found.")

if __name__ == "__main__":
    asyncio.run(main())
