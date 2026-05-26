"""Verify both Looker-ready views against production DB."""
import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:

        # First, apply the views directly (normally done on startup)
        with open("migrations/v001_looker_views.sql", "r", encoding="utf-8") as f:
            sql = f.read()
        # Split on semicolons and run each statement
        statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]
        for stmt in statements:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                print(f"  SKIP: {str(e)[:80]}")
        await conn.commit()
        print("Views applied.\n")

        # === A) vw_bm_mass_evaluation_criteria_flat ===
        print("=== A) vw_bm_mass_evaluation_criteria_flat ===")
        res = await conn.execute(text("SELECT COUNT(*) FROM vw_bm_mass_evaluation_criteria_flat"))
        total = res.scalar()
        print(f"  Total rows: {total}  (expected: 243 * ~44 criteria = ~10692)")

        res = await conn.execute(text("""
            SELECT criterion_key, criterion_name, criterion_type, COUNT(*)
            FROM vw_bm_mass_evaluation_criteria_flat
            GROUP BY criterion_key, criterion_name, criterion_type
            ORDER BY COUNT(*) DESC
            LIMIT 10
        """))
        rows = res.fetchall()
        print("  Top 10 criteria (criterion_key | criterion_name | criterion_type | count):")
        for r in rows:
            name_ok = "OK" if r[1] else "MISSING NAME"
            type_ok = "OK" if r[2] else "MISSING TYPE"
            print(f"    {r[0]:<45} | {str(r[1]):<35} [{name_ok}] | {str(r[2]):<12} [{type_ok}] | {r[3]}")

        # Check that criterion_name and criterion_type are populated
        res = await conn.execute(text("""
            SELECT COUNT(*) FROM vw_bm_mass_evaluation_criteria_flat
            WHERE criterion_name IS NULL OR criterion_type IS NULL
        """))
        nulls = res.scalar()
        print(f"\n  Rows with NULL criterion_name or criterion_type: {nulls} (expected: 0)")

        # === B) vw_bm_mass_evaluation_calls_summary ===
        print("\n=== B) vw_bm_mass_evaluation_calls_summary ===")
        res = await conn.execute(text("SELECT COUNT(*) FROM vw_bm_mass_evaluation_calls_summary"))
        total = res.scalar()
        print(f"  Total rows: {total}  (expected: 243 calls)")

        res = await conn.execute(text("""
            SELECT 
                COUNT(*) as total,
                COUNT(evaluacion_global) as has_eg,
                ROUND(AVG(evaluacion_global)::numeric, 4) as avg_eg,
                COUNT(cierre_cita) as has_cierre,
                COUNT(claridad) as has_claridad,
                COUNT(tipo_llamada) as has_tipo,
                COUNT(patologia) as has_patologia
            FROM vw_bm_mass_evaluation_calls_summary
        """))
        r = res.fetchone()
        print(f"  total={r[0]}, evaluacion_global={r[1]} rows ({r[2]}), cierre_cita={r[3]}, claridad={r[4]}, tipo_llamada={r[5]}, patologia={r[6]}")

        # Sample row
        res = await conn.execute(text("""
            SELECT conversation_id, agent_name, call_date, tipo_llamada, cierre_cita,
                   evaluacion_global, claridad, n3_preguntas, propension
            FROM vw_bm_mass_evaluation_calls_summary
            LIMIT 3
        """))
        rows = res.fetchall()
        print("\n  Sample rows:")
        for r in rows:
            print(f"    conv={r[0][:20]}.. agent={str(r[1])[:20]}.. date={r[2]} tipo={r[3]} cierre={r[4]} eg={r[5]} claridad={r[6]} n3={r[7]} prop={r[8]}")

asyncio.run(main())
