"""Diagnose date filter — check actual column ranges."""
import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(text("""
            SELECT 
                MIN(created_at) as min_created,
                MAX(created_at) as max_created,
                MIN(call_timestamp) as min_call,
                MAX(call_timestamp) as max_call,
                COUNT(*) as total
            FROM bm_mass_evaluation_results
            WHERE service_key = 'front' AND status = 'completed';
        """))
        row = res.fetchone()
        print(f"created_at range:   {row[0]}  to  {row[1]}")
        print(f"call_timestamp range: {row[2]}  to  {row[3]}")
        print(f"Total completed front rows: {row[4]}")

asyncio.run(main())
