"""Quick validation of both bug fixes against production DB (using call_timestamp for date filter)."""
import asyncio
from sqlalchemy import text
from app.db import get_engine

async def main():
    engine = get_engine()
    async with engine.connect() as conn:
        print("=== BUG #1 VALIDATION: avg_evaluacion_global from result_json ===")
        # Use call_timestamp for date filtering (actual call date vs analysis creation date)
        res = await conn.execute(text("""
            SELECT 
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                AVG((r.result_json->>'evaluacion_global')::numeric) AS avg_evaluacion_global,
                AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate,
                AVG(CASE WHEN c.criterion_key = 'claridad' AND c.is_applicable = true THEN c.numeric_value END) AS avg_claridad
            FROM bm_mass_evaluation_results r
            LEFT JOIN bm_mass_evaluation_criterion_results c ON r.mass_analysis_id = c.mass_analysis_id
            WHERE r.status = 'completed'
              AND r.result_json IS NOT NULL
              AND r.service_key = 'front'
              AND r.call_timestamp >= '2026-05-03'::timestamptz
              AND r.call_timestamp <= '2026-05-22 23:59:59'::timestamptz
        """))
        row = res.fetchone()
        total = row[0]
        eg = round(float(row[1]),4) if row[1] else None
        cc = round(float(row[2]),4) if row[2] else None
        cl = round(float(row[3]),4) if row[3] else None

        print(f"  total_calls: {total}  (expected: 243) {'OK' if total == 243 else 'WRONG'}")
        print(f"  avg_evaluacion_global: {eg}  (expected: ~6.8, NOT null) {'OK' if eg else 'WRONG'}")
        print(f"  cierre_cita_rate: {cc}  (expected: 0.0-1.0) {'OK' if cc and 0 <= cc <= 1 else 'WRONG'}")
        print(f"  avg_claridad: {cl}")

        print("\n=== BUG #2 VALIDATION: Weekly series — cierre_cita_rate must be 0-1 ===")
        res2 = await conn.execute(text("""
            SELECT 
                date_trunc('week', r.call_timestamp)::date AS period,
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                AVG((r.result_json->>'evaluacion_global')::numeric) AS avg_evaluacion_global,
                AVG(CASE WHEN c.criterion_key = 'saludo_inicio' AND c.is_applicable = true THEN c.numeric_value END) AS avg_saludo_inicio,
                AVG(CASE WHEN c.criterion_key = 'n3_preguntas' AND c.is_applicable = true THEN c.numeric_value END) AS avg_n3_preguntas,
                AVG(CASE WHEN c.criterion_key = 'gestion_objeciones' AND c.is_applicable = true THEN c.numeric_value END) AS avg_gestion_objeciones,
                AVG(CASE WHEN c.criterion_key = 'propension' AND c.is_applicable = true THEN c.numeric_value END) AS avg_propension,
                AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate
            FROM bm_mass_evaluation_results r
            LEFT JOIN bm_mass_evaluation_criterion_results c ON r.mass_analysis_id = c.mass_analysis_id
            WHERE r.status = 'completed'
              AND r.result_json IS NOT NULL
              AND r.service_key = 'front'
              AND r.call_timestamp >= '2026-05-03'::timestamptz
              AND r.call_timestamp <= '2026-05-22 23:59:59'::timestamptz
            GROUP BY period
            ORDER BY period ASC
        """))
        rows = res2.fetchall()
        week_total = 0
        all_ok = True
        for row in rows:
            period = row[0]
            calls = row[1]
            week_total += calls
            eg = round(float(row[2]),3) if row[2] else None
            saludo = round(float(row[3]),3) if row[3] else None
            n3 = round(float(row[4]),3) if row[4] else None
            gob = round(float(row[5]),3) if row[5] else None
            prop = round(float(row[6]),3) if row[6] else None
            cc = round(float(row[7]),4) if row[7] else None

            cc_ok = cc is not None and 0 <= cc <= 1
            eg_ok = eg is not None
            if not cc_ok or not eg_ok:
                all_ok = False
            print(f"  {period}: calls={calls}  eg={eg} {'OK' if eg_ok else 'FAIL'}  saludo={saludo}  n3={n3}  gob={gob}  prop={prop}  cc_rate={cc} {'OK' if cc_ok else 'FAIL'}")
        
        print(f"\n  TOTAL calls summed: {week_total}  (expected: 243) {'OK' if week_total == 243 else 'WRONG'}")
        print(f"  All fields valid: {'YES' if all_ok else 'NO'}")

asyncio.run(main())
