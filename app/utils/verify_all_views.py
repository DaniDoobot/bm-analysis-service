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
                import traceback
                print(f"  [ERROR executing statement]:")
                print(stmt[:300] + "...")
                traceback.print_exc()

async def main():
    engine = get_engine()
    async with engine.begin() as conn:
        print("Applying v001_looker_views.sql...")
        await apply_sql_file(conn, "migrations/v001_looker_views.sql")
        print("Applying v002_looker_individual_views.sql...")
        await apply_sql_file(conn, "migrations/v002_looker_individual_views.sql")
        print("Applying v003_looker_wide_views.sql...")
        await apply_sql_file(conn, "migrations/v003_looker_wide_views.sql")
    print("All migrations applied.\n")

    async with engine.connect() as conn:
        views = [
            ("vw_bm_mass_evaluation_criteria_flat", "mass eval criteria flat"),
            ("vw_bm_mass_evaluation_calls_summary", "mass eval calls summary"),
            ("vw_bm_individual_analysis_criteria_flat", "individual criteria flat"),
            ("vw_bm_individual_analysis_summary", "individual summary"),
            ("vw_bm_all_analysis_criteria_flat", "unified flat"),
            ("vw_bm_mass_evaluation_report_wide", "mass eval report wide"),
            ("vw_bm_individual_analysis_report_wide", "individual report wide"),
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

        print()
        print("=== Wide views: Comparative distinct count validation ===")
        
        # 1. Mass comparison
        summary_mass_res = await conn.execute(text("SELECT COUNT(*) FROM vw_bm_mass_evaluation_calls_summary"))
        summary_mass = summary_mass_res.scalar()
        wide_mass_res = await conn.execute(text("SELECT COUNT(*) FROM vw_bm_mass_evaluation_report_wide"))
        wide_mass = wide_mass_res.scalar()
        print(f"  Mass: Summary Rows = {summary_mass} | Report Wide Rows = {wide_mass} -> {'OK' if summary_mass == wide_mass else 'FAIL'}")

        # 2. Individual comparison
        summary_indiv_res = await conn.execute(text("SELECT COUNT(*) FROM vw_bm_individual_analysis_summary"))
        summary_indiv = summary_indiv_res.scalar()
        wide_indiv_res = await conn.execute(text("SELECT COUNT(*) FROM vw_bm_individual_analysis_report_wide"))
        wide_indiv = wide_indiv_res.scalar()
        print(f"  Individual: Summary Rows = {summary_indiv} | Report Wide Rows = {wide_indiv} -> {'OK' if summary_indiv == wide_indiv else 'FAIL'}")

        print()
        print("=== Wide views: Sample pivoted columns check ===")
        # 3. Mass sample columns
        mass_sample = await conn.execute(text("""
            SELECT mass_evaluation_result_id, service_name, agent_name, evaluacion_global, tipo_llamada, cierre_cita,
                   key_item_1, valor_item_1, key_item_2, valor_item_2, key_item_3, valor_item_3,
                   key_item_4, valor_item_4, key_item_5, valor_item_5
            FROM vw_bm_mass_evaluation_report_wide
            WHERE key_item_1 IS NOT NULL
            LIMIT 1
        """))
        r_mass = mass_sample.fetchone()
        if r_mass:
            print(f"  Mass wide sample: ID={r_mass[0]}  service={r_mass[1]}  agent={r_mass[2]}  global={r_mass[3]}  type={r_mass[4]}  cierre={r_mass[5]}")
            print(f"    Item 1: {r_mass[6]} = {r_mass[7]}")
            print(f"    Item 2: {r_mass[8]} = {r_mass[9]}")
            print(f"    Item 3: {r_mass[10]} = {r_mass[11]}")
            print(f"    Item 4: {r_mass[12]} = {r_mass[13]}")
            print(f"    Item 5: {r_mass[14]} = {r_mass[15]}")
        else:
            print("  No completed mass evaluations available for sample.")

        print()
        # 4. Individual sample columns
        indiv_sample = await conn.execute(text("""
            SELECT analysis_id, source_type, agent_name, evaluacion_global, tipo_llamada,
                   key_item_1, valor_item_1, key_item_2, valor_item_2, key_item_3, valor_item_3,
                   key_item_4, valor_item_4, key_item_5, valor_item_5
            FROM vw_bm_individual_analysis_report_wide
            WHERE key_item_1 IS NOT NULL
            LIMIT 1
        """))
        r_indiv = indiv_sample.fetchone()
        if r_indiv:
            print(f"  Individual wide sample: ID={r_indiv[0]}  src_type={r_indiv[1]}  agent={r_indiv[2]}  global={r_indiv[3]}  type={r_indiv[4]}")
            print(f"    Item 1: {r_indiv[5]} = {r_indiv[6]}")
            print(f"    Item 2: {r_indiv[7]} = {r_indiv[8]}")
            print(f"    Item 3: {r_indiv[9]} = {r_indiv[10]}")
            print(f"    Item 4: {r_indiv[11]} = {r_indiv[12]}")
            print(f"    Item 5: {r_indiv[13]} = {r_indiv[14]}")
        else:
            print("  No completed individual analyses available for sample.")
        print()
asyncio.run(main())
