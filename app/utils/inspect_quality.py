"""Inspect data quality issues for Looker view fixes."""
import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:
        print("=== Percentage raw values in mass criterion results ===")
        res = await conn.execute(text("""
            SELECT criterion_key, criterion_type, value_raw, numeric_value, percentage_value
            FROM bm_mass_evaluation_criterion_results
            WHERE criterion_key IN ('hablando_agente','hablando_paciente')
            LIMIT 10
        """))
        for r in res.fetchall():
            print(f"  key={r[0]}  type={r[1]}  raw={r[2]}  num={r[3]}  pct={r[4]}")

        print("\n=== Duplicate names per criterion_key (mass) ===")
        res = await conn.execute(text("""
            SELECT criterion_key, array_agg(DISTINCT criterion_name ORDER BY criterion_name) as names
            FROM bm_mass_evaluation_criterion_results
            GROUP BY criterion_key
            HAVING COUNT(DISTINCT criterion_name) > 1
            ORDER BY criterion_key
        """))
        for r in res.fetchall():
            print(f"  {r[0]}: {r[1]}")

        print("\n=== saludo_inicio name variants across tables ===")
        res = await conn.execute(text("""
            SELECT 'mass' as src, criterion_key, criterion_name, COUNT(*)
            FROM bm_mass_evaluation_criterion_results
            WHERE criterion_key = 'saludo_inicio'
            GROUP BY criterion_key, criterion_name
            UNION ALL
            SELECT 'individual' as src, criterion_key, criterion_name, COUNT(*)
            FROM bm_analysis_results
            WHERE criterion_key = 'saludo_inicio'
            GROUP BY criterion_key, criterion_name
        """))
        for r in res.fetchall():
            print(f"  {r[0]}: key={r[1]}  name={r[2]}  n={r[3]}")

        print("\n=== trato_ustad / trato_usted variants ===")
        res = await conn.execute(text("""
            SELECT 'mass' as src, criterion_key, criterion_name, COUNT(*)
            FROM bm_mass_evaluation_criterion_results
            WHERE criterion_key LIKE 'trato%'
            GROUP BY criterion_key, criterion_name
            UNION ALL
            SELECT 'individual' as src, criterion_key, criterion_name, COUNT(*)
            FROM bm_analysis_results
            WHERE criterion_key LIKE 'trato%'
            GROUP BY criterion_key, criterion_name
        """))
        for r in res.fetchall():
            print(f"  {r[0]}: key={r[1]}  name={r[2]}  n={r[3]}")

        print("\n=== explicaciones_medicas name variants ===")
        res = await conn.execute(text("""
            SELECT 'mass' as src, criterion_key, criterion_name, COUNT(*)
            FROM bm_mass_evaluation_criterion_results
            WHERE criterion_key = 'explicaciones_medicas'
            GROUP BY criterion_key, criterion_name
            UNION ALL
            SELECT 'individual' as src, criterion_key, criterion_name, COUNT(*)
            FROM bm_analysis_results
            WHERE criterion_key = 'explicaciones_medicas'
            GROUP BY criterion_key, criterion_name
        """))
        for r in res.fetchall():
            print(f"  {r[0]}: key={r[1]}  name='{r[2]}'  n={r[3]}")

        print("\n=== All distinct criterion_key/name pairs across both tables ===")
        res = await conn.execute(text("""
            SELECT criterion_key, COUNT(DISTINCT criterion_name) as name_variants,
                   array_agg(DISTINCT criterion_name ORDER BY criterion_name) as names
            FROM (
                SELECT criterion_key, criterion_name FROM bm_mass_evaluation_criterion_results
                UNION ALL
                SELECT criterion_key, criterion_name FROM bm_analysis_results
            ) t
            GROUP BY criterion_key
            HAVING COUNT(DISTINCT criterion_name) > 1
            ORDER BY criterion_key
        """))
        for r in res.fetchall():
            print(f"  {r[0]} ({r[1]} variants): {r[2]}")

asyncio.run(main())
