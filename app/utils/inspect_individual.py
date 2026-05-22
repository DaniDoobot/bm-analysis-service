"""Inspect individual analysis tables to understand real data shape."""
import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:

        print("=== bm_analyses ===")
        res = await conn.execute(text("""
            SELECT COUNT(*), MIN(created_at), MAX(created_at),
                   COUNT(DISTINCT call_id), COUNT(DISTINCT analysis_type)
            FROM bm_analyses
        """))
        r = res.fetchone()
        print(f"  total={r[0]}  date_range={r[1]:%Y-%m-%d} to {r[2]:%Y-%m-%d}  unique_calls={r[3]}  types={r[4]}")

        res = await conn.execute(text("SELECT DISTINCT analysis_type FROM bm_analyses LIMIT 10"))
        print(f"  analysis_types: {[r[0] for r in res.fetchall()]}")

        res = await conn.execute(text("""
            SELECT COUNT(*) FROM bm_analyses WHERE status = 'completed'
        """))
        print(f"  completed: {res.scalar()}")

        print("\n=== bm_analysis_criterion_results ===")
        res = await conn.execute(text("SELECT COUNT(*) FROM bm_analysis_criterion_results"))
        print(f"  total rows: {res.scalar()}")
        res = await conn.execute(text("""
            SELECT criterion_key, criterion_name, criterion_type, COUNT(*)
            FROM bm_analysis_criterion_results
            GROUP BY criterion_key, criterion_name, criterion_type
            ORDER BY COUNT(*) DESC
            LIMIT 15
        """))
        rows = res.fetchall()
        print("  Top criteria (key | name | type | count):")
        for r in rows:
            print(f"    {str(r[0]):<40} | {str(r[1]):<35} | {str(r[2]):<12} | {r[3]}")

        # Check nulls
        res = await conn.execute(text("""
            SELECT COUNT(*) FROM bm_analysis_criterion_results
            WHERE criterion_name IS NULL OR criterion_type IS NULL
        """))
        print(f"  Rows missing criterion_name or criterion_type: {res.scalar()}")

        print("\n=== bm_analysis_results (legacy table) ===")
        res = await conn.execute(text("SELECT COUNT(*) FROM bm_analysis_results"))
        print(f"  total rows: {res.scalar()}")
        res = await conn.execute(text("""
            SELECT criterion_key, criterion_name, criterion_type, COUNT(*)
            FROM bm_analysis_results
            GROUP BY criterion_key, criterion_name, criterion_type
            ORDER BY COUNT(*) DESC
            LIMIT 10
        """))
        rows = res.fetchall()
        print("  Top criteria:")
        for r in rows:
            print(f"    {str(r[0]):<40} | {str(r[1]):<35} | {str(r[2]):<12} | {r[3]}")

        print("\n=== bm_analyses result JSON shape (first 3 completed) ===")
        res = await conn.execute(text("""
            SELECT analysis_id, call_id, analysis_type, analysis_type, evaluacion_global,
                   jsonb_typeof(result) as result_type,
                   (SELECT string_agg(k, ', ') FROM jsonb_object_keys(result) AS k) as result_keys
            FROM bm_analyses
            WHERE status = 'completed' AND result IS NOT NULL
            LIMIT 3
        """))
        for r in res.fetchall():
            print(f"  id={r[0]}  call={str(r[1])[:20]}  type={r[2]}  eg={r[4]}  result_keys={r[6]}")

        print("\n=== bm_analyses: service fields available? ===")
        res = await conn.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(hubspot_owner_id) as has_agent,
                   COUNT(tipo_llamada) as has_tipo
            FROM bm_analyses
        """))
        r = res.fetchone()
        print(f"  total={r[0]}  has_agent={r[1]}  has_tipo={r[2]}")

        print("\n=== Check if bm_analyses has service_id/service_key ===")
        res = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'bm_analyses'
            ORDER BY ordinal_position
        """))
        cols = [r[0] for r in res.fetchall()]
        print(f"  columns: {cols}")

asyncio.run(main())
