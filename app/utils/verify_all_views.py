"""Apply both Looker migrations and verify all views."""
import asyncio
from pathlib import Path
from sqlalchemy import text
from app.db import get_engine

async def apply_sql_file(conn, path: str):
    sql = Path(path).read_text(encoding="utf-8")
    # Split on semicolons, skip comment-only blocks
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt and not all(line.startswith("--") or not line.strip() for line in stmt.splitlines()):
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                print(f"  [WARN] {str(e)[:120]}")

async def main():
    engine = get_engine()
    async with engine.begin() as conn:
        print("Applying v001_looker_views.sql...")
        await apply_sql_file(conn, "migrations/v001_looker_views.sql")
        print("Applying v002_looker_individual_views.sql...")
        await apply_sql_file(conn, "migrations/v002_looker_individual_views.sql")
    print("All migrations applied.\n")

    async with engine.connect() as conn:
        views = [
            ("vw_bm_mass_evaluation_criteria_flat", "mass eval criteria flat"),
            ("vw_bm_mass_evaluation_calls_summary", "mass eval calls summary"),
            ("vw_bm_individual_analysis_criteria_flat", "individual criteria flat"),
            ("vw_bm_individual_analysis_summary", "individual summary"),
            ("vw_bm_all_analysis_criteria_flat", "unified flat"),
        ]
        for view, label in views:
            res = await conn.execute(text(f"SELECT COUNT(*) FROM {view}"))
            print(f"  {label}: {res.scalar()} rows  [{view}]")

        print()
        print("=== Individual criteria: key/name/type check ===")
        res = await conn.execute(text("""
            SELECT criterion_key, criterion_name, criterion_type, source_type, COUNT(*)
            FROM vw_bm_individual_analysis_criteria_flat
            GROUP BY criterion_key, criterion_name, criterion_type, source_type
            ORDER BY COUNT(*) DESC LIMIT 10
        """))
        for r in res.fetchall():
            name_ok = "OK" if r[1] else "MISSING"
            type_ok = "OK" if r[2] else "MISSING"
            print(f"  {str(r[0]):<35} | {str(r[1]):<30} [{name_ok}] | {str(r[2]):<12} [{type_ok}] | src={r[3]} | n={r[4]}")

        print()
        print("=== Individual summary: evaluacion_global + cierre_cita sample ===")
        res = await conn.execute(text("""
            SELECT analysis_id, conversation_id, call_date, evaluacion_global, cierre_cita, claridad, n3_preguntas
            FROM vw_bm_individual_analysis_summary LIMIT 5
        """))
        for r in res.fetchall():
            print(f"  id={r[0]}  call={str(r[1])[:18]}  date={r[2]}  eg={r[3]}  cierre={r[4]}  claridad={r[5]}  n3={r[6]}")

        print()
        print("=== Unified view: analysis_source breakdown ===")
        res = await conn.execute(text("""
            SELECT analysis_source, COUNT(*) as rows,
                   COUNT(DISTINCT criterion_key) as distinct_criteria
            FROM vw_bm_all_analysis_criteria_flat
            GROUP BY analysis_source ORDER BY rows DESC
        """))
        for r in res.fetchall():
            print(f"  source={r[0]:<20} rows={r[1]}  distinct_criteria={r[2]}")

asyncio.run(main())
