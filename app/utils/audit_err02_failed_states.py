"""
ERR-02 Comprehensive Audit Script for Failed States in Production (Read-Only).
Analyzes:
1. bm_mass_evaluation_runs
2. bm_mass_analysis_automation_runs
3. bm_mass_evaluation_results
4. bm_analyses (individual analyses)
Identifies real failures vs false failures, error message coverage, provider root causes, and desynchronization.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

MADRID_TZ = ZoneInfo("Europe/Madrid")

def format_dt(dt):
    if dt is None:
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S")

async def run_audit():
    db_url = "postgresql+asyncpg://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"
    engine = create_async_engine(db_url, echo=False)
    now_utc = datetime.now(timezone.utc)
    utc_7d = now_utc - timedelta(days=7)
    utc_30d = now_utc - timedelta(days=30)

    async with AsyncSession(engine) as db:
        print("=" * 130)
        print(f"AUDITORIA ERR-02: ESTADOS FAILED EN PRODUCCION - {format_dt(now_utc)} (Madrid)")
        print("=" * 130)

        # =====================================================================
        # 1. AUDITORIA: bm_mass_evaluation_runs
        # =====================================================================
        print("\n" + "#" * 130)
        print("1. TABLA: bm_mass_evaluation_runs")
        print("#" * 130)

        # Status distribution all time, 30d, 7d
        for label, cutoff in [("Todo el Histórico", None), ("Últimos 30 días", utc_30d), ("Últimos 7 días", utc_7d)]:
            where_clause = "WHERE created_at >= :cutoff" if cutoff else ""
            params = {"cutoff": cutoff} if cutoff else {}
            q_status = text(f"""
                SELECT status, count(*) as cnt
                FROM bm_mass_evaluation_runs
                {where_clause}
                GROUP BY status
                ORDER BY cnt DESC;
            """)
            res = (await db.execute(q_status, params)).fetchall()
            total = sum(r[1] for r in res)
            print(f"\n--- Distribución de Status ({label}) | Total runs: {total} ---")
            for r in res:
                pct = (r[1] / total * 100) if total > 0 else 0
                print(f"  {r[0]:<25}: {r[1]:>5} runs ({pct:>5.1f}%)")

        # Breakdown of failed runs by error_message (Top 20)
        q_err_mass_runs = text("""
            SELECT 
                COALESCE(NULLIF(TRIM(error_message), ''), '<SIN MENSAJE / NULL>') as err_msg,
                count(*) as cnt,
                min(run_id) as sample_min_id,
                max(run_id) as sample_max_id,
                min(created_at) as first_seen,
                max(created_at) as last_seen
            FROM bm_mass_evaluation_runs
            WHERE status = 'failed'
            GROUP BY err_msg
            ORDER BY cnt DESC
            LIMIT 25;
        """)
        res_err_runs = (await db.execute(q_err_mass_runs)).fetchall()
        print("\n--- Causas de MassEvaluationRun FAILED (Histórico completo) ---")
        print(f"{'Cant':<5} | {'MinID':<6} | {'MaxID':<6} | {'Primera vez':<20} | {'Última vez':<20} | {'Mensaje de Error'}")
        print("-" * 130)
        for r in res_err_runs:
            print(f"{r[1]:<5} | {r[2]:<6} | {r[3]:<6} | {format_dt(r[4]):<20} | {format_dt(r[5]):<20} | {r[0][:60]}")

        # Breakdown by execution_source / trigger_type for failed runs
        q_src_failed = text("""
            SELECT 
                COALESCE(execution_source, 'None') as src,
                COALESCE(trigger_type, 'None') as trig,
                count(*) as cnt
            FROM bm_mass_evaluation_runs
            WHERE status = 'failed'
            GROUP BY src, trig
            ORDER BY cnt DESC;
        """)
        res_src = (await db.execute(q_src_failed)).fetchall()
        print("\n--- MassEvaluationRun FAILED por Origen / Trigger ---")
        for r in res_src:
            print(f"  Source: {r[0]:<15} | Trigger: {r[1]:<15} | Failed runs: {r[2]}")

        # Detection of FALSE FAILED in mass_evaluation_runs:
        # Runs marked failed that actually have completed results in bm_mass_evaluation_results
        q_false_failed_mass = text("""
            SELECT 
                r.run_id, r.job_id, r.status, r.calls_found, r.calls_selected, r.calls_analyzed, 
                r.error_message, r.created_at,
                count(res.mass_analysis_id) as total_results_in_db,
                count(CASE WHEN res.status = 'completed' THEN 1 END) as completed_results_in_db
            FROM bm_mass_evaluation_runs r
            JOIN bm_mass_evaluation_results res ON res.run_id = r.run_id
            WHERE r.status = 'failed'
            GROUP BY r.run_id, r.job_id, r.status, r.calls_found, r.calls_selected, r.calls_analyzed, r.error_message, r.created_at
            ORDER BY r.run_id DESC;
        """)
        false_failed_mass_runs = (await db.execute(q_false_failed_mass)).fetchall()
        print(f"\n--- FALSOS FAILED en MassEvaluationRuns (status=failed pero tienen resultados en DB): {len(false_failed_mass_runs)} runs ---")
        for r in false_failed_mass_runs:
            print(f"  Run #{r[0]} (Job #{r[1]}): {r[7]} completed res in DB / {r[8]} total res | error: {r[6]} | created: {format_dt(r[7])}")

        # Check for stuck/running runs (> 2 hours old)
        q_stuck_runs = text("""
            SELECT run_id, job_id, status, started_at, heartbeat_at, created_at
            FROM bm_mass_evaluation_runs
            WHERE status IN ('running', 'pending', 'cancelling')
            ORDER BY run_id DESC;
        """)
        stuck_runs = (await db.execute(q_stuck_runs)).fetchall()
        print(f"\n--- MassEvaluationRuns actualmente en 'running', 'pending' o 'cancelling': {len(stuck_runs)} runs ---")
        for r in stuck_runs:
            print(f"  Run #{r[0]} (Job #{r[1]}): status={r[2]}, started={format_dt(r[3])}, heartbeat={format_dt(r[4])}, created={format_dt(r[5])}")


        # =====================================================================
        # 2. AUDITORIA: bm_mass_analysis_automation_runs
        # =====================================================================
        print("\n" + "#" * 130)
        print("2. TABLA: bm_mass_analysis_automation_runs")
        print("#" * 130)

        for label, cutoff in [("Todo el Histórico", None), ("Últimos 30 días", utc_30d), ("Últimos 7 días", utc_7d)]:
            where_clause = "WHERE started_at >= :cutoff" if cutoff else ""
            params = {"cutoff": cutoff} if cutoff else {}
            q_auto_status = text(f"""
                SELECT status, count(*) as cnt
                FROM bm_mass_analysis_automation_runs
                {where_clause}
                GROUP BY status
                ORDER BY cnt DESC;
            """)
            res_auto = (await db.execute(q_auto_status, params)).fetchall()
            total_auto = sum(r[1] for r in res_auto)
            print(f"\n--- Distribución de Status en AutomationRuns ({label}) | Total: {total_auto} ---")
            for r in res_auto:
                pct = (r[1] / total_auto * 100) if total_auto > 0 else 0
                print(f"  {r[0]:<25}: {r[1]:>5} runs ({pct:>5.1f}%)")

        # Top error messages in automation runs
        q_err_auto = text("""
            SELECT 
                COALESCE(NULLIF(TRIM(error_message), ''), '<SIN MENSAJE / NULL>') as err_msg,
                count(*) as cnt,
                min(automation_run_id) as sample_min_id,
                max(automation_run_id) as sample_max_id,
                min(started_at) as first_seen,
                max(started_at) as last_seen
            FROM bm_mass_analysis_automation_runs
            WHERE status = 'failed'
            GROUP BY err_msg
            ORDER BY cnt DESC
            LIMIT 25;
        """)
        res_err_auto = (await db.execute(q_err_auto)).fetchall()
        print("\n--- Causas de AutomationRuns FAILED (Histórico completo) ---")
        print(f"{'Cant':<5} | {'MinID':<6} | {'MaxID':<6} | {'Primera vez':<20} | {'Última vez':<20} | {'Mensaje de Error'}")
        print("-" * 130)
        for r in res_err_auto:
            print(f"{r[1]:<5} | {r[2]:<6} | {r[3]:<6} | {format_dt(r[4]):<20} | {format_dt(r[5]):<20} | {r[0][:60]}")

        # Desynchronization: AutomationRun failed but MassEvaluationRun completed
        q_desync = text("""
            SELECT 
                a.automation_run_id, a.automation_id, a.run_id, a.status as auto_status, 
                m.status as mass_status, a.error_message as auto_err, m.error_message as mass_err,
                a.started_at
            FROM bm_mass_analysis_automation_runs a
            JOIN bm_mass_evaluation_runs m ON m.run_id = a.run_id
            WHERE a.status = 'failed' AND m.status = 'completed'
            ORDER BY a.automation_run_id DESC;
        """)
        desync_runs = (await db.execute(q_desync)).fetchall()
        print(f"\n--- DESINCRONIZACIONES: AutoRun FAILED pero MassRun COMPLETED: {len(desync_runs)} runs ---")
        for r in desync_runs:
            print(f"  AutoRun #{r[0]} (MassRun #{r[2]}): AutoStatus={r[3]}, MassStatus={r[4]} | AutoErr={r[5]} | Date: {format_dt(r[7])}")

        # Reverse Desync: AutoRun completed but MassRun failed
        q_rev_desync = text("""
            SELECT 
                a.automation_run_id, a.automation_id, a.run_id, a.status as auto_status, 
                m.status as mass_status, a.error_message as auto_err, m.error_message as mass_err,
                a.started_at
            FROM bm_mass_analysis_automation_runs a
            JOIN bm_mass_evaluation_runs m ON m.run_id = a.run_id
            WHERE a.status = 'completed' AND m.status = 'failed'
            ORDER BY a.automation_run_id DESC;
        """)
        rev_desync_runs = (await db.execute(q_rev_desync)).fetchall()
        print(f"\n--- DESINCRONIZACIONES: AutoRun COMPLETED pero MassRun FAILED: {len(rev_desync_runs)} runs ---")
        for r in rev_desync_runs:
            print(f"  AutoRun #{r[0]} (MassRun #{r[2]}): AutoStatus={r[3]}, MassStatus={r[4]} | MassErr={r[6]} | Date: {format_dt(r[7])}")


        # =====================================================================
        # 3. AUDITORIA: bm_mass_evaluation_results
        # =====================================================================
        print("\n" + "#" * 130)
        print("3. TABLA: bm_mass_evaluation_results")
        print("#" * 130)

        for label, cutoff in [("Todo el Histórico", None), ("Últimos 30 días", utc_30d), ("Últimos 7 días", utc_7d)]:
            where_clause = "WHERE created_at >= :cutoff" if cutoff else ""
            params = {"cutoff": cutoff} if cutoff else {}
            q_res_status = text(f"""
                SELECT status, count(*) as cnt
                FROM bm_mass_evaluation_results
                {where_clause}
                GROUP BY status
                ORDER BY cnt DESC;
            """)
            res_rows = (await db.execute(q_res_status, params)).fetchall()
            total_res = sum(r[1] for r in res_rows)
            print(f"\n--- Distribución de Status en Resultados ({label}) | Total: {total_res} ---")
            for r in res_rows:
                pct = (r[1] / total_res * 100) if total_res > 0 else 0
                print(f"  {r[0]:<25}: {r[1]:>5} resultados ({pct:>5.1f}%)")

        # Top error messages in bm_mass_evaluation_results
        q_err_results = text("""
            SELECT 
                COALESCE(NULLIF(TRIM(error_message), ''), '<SIN MENSAJE / NULL>') as err_msg,
                count(*) as cnt,
                min(mass_analysis_id) as sample_min_id,
                max(mass_analysis_id) as sample_max_id,
                min(created_at) as first_seen,
                max(created_at) as last_seen
            FROM bm_mass_evaluation_results
            WHERE status IN ('failed', 'error')
            GROUP BY err_msg
            ORDER BY cnt DESC
            LIMIT 30;
        """)
        res_err_results = (await db.execute(q_err_results)).fetchall()
        print("\n--- Causas de Resultados FAILED / ERROR (Histórico completo) ---")
        print(f"{'Cant':<5} | {'MinID':<6} | {'MaxID':<6} | {'Primera vez':<20} | {'Última vez':<20} | {'Mensaje de Error'}")
        print("-" * 130)
        for r in res_err_results:
            print(f"{r[1]:<5} | {r[2]:<6} | {r[3]:<6} | {format_dt(r[4]):<20} | {format_dt(r[5]):<20} | {r[0][:60]}")

        # Check results with status=failed but score is NOT NULL (contradiction)
        q_contra = text("""
            SELECT count(*) 
            FROM bm_mass_evaluation_results
            WHERE status IN ('failed', 'error') AND evaluacion_global IS NOT NULL;
        """)
        contra_cnt = (await db.execute(q_contra)).scalar()
        print(f"\n--- Resultados marcados FAILED pero con puntuacion asignada (evaluacion_global != NULL): {contra_cnt} ---")

        # Check results with status=failed for which a newer completed result exists for the same call_id
        q_resolved_fails = text("""
            SELECT count(DISTINCT f.call_id)
            FROM bm_mass_evaluation_results f
            JOIN bm_mass_evaluation_results c ON c.call_id = f.call_id AND c.status = 'completed' AND c.mass_analysis_id > f.mass_analysis_id
            WHERE f.status IN ('failed', 'error');
        """)
        resolved_fails_cnt = (await db.execute(q_resolved_fails)).scalar()
        print(f"--- Llamadas únicas que fallaron en algún momento pero luego fueron COMPLETADAS en un run posterior: {resolved_fails_cnt} ---")

        # Total distinct call_ids with ONLY failed status
        q_permanent_fails = text("""
            SELECT count(DISTINCT call_id)
            FROM bm_mass_evaluation_results
            WHERE status IN ('failed', 'error')
            AND call_id NOT IN (
                SELECT call_id FROM bm_mass_evaluation_results WHERE status = 'completed'
            );
        """)
        permanent_fails_cnt = (await db.execute(q_permanent_fails)).scalar()
        print(f"--- Llamadas únicas que PERMANECEN fallidas (sin ningún resultado completed): {permanent_fails_cnt} ---")

        # Sample of permanent failed call_ids
        q_sample_perm = text("""
            SELECT mass_analysis_id, call_id, error_message, created_at
            FROM bm_mass_evaluation_results
            WHERE status IN ('failed', 'error')
            AND call_id NOT IN (
                SELECT call_id FROM bm_mass_evaluation_results WHERE status = 'completed'
            )
            ORDER BY mass_analysis_id DESC
            LIMIT 15;
        """)
        sample_perm = (await db.execute(q_sample_perm)).fetchall()
        print("\n--- Muestra de las últimas llamadas con fallo permanente: ---")
        for sp in sample_perm:
            print(f"  Result #{sp[0]}: call_id={sp[1]} | error={sp[2]} | date={format_dt(sp[3])}")

        # =====================================================================
        # 4. AUDITORIA: bm_analyses (análisis individuales si existen)
        # =====================================================================
        print("\n" + "#" * 130)
        print("4. TABLA: bm_analyses (Análisis individuales)")
        print("#" * 130)
        try:
            q_ind_status = text("""
                SELECT status, count(*) as cnt
                FROM bm_analyses
                GROUP BY status
                ORDER BY cnt DESC;
            """)
            res_ind = (await db.execute(q_ind_status)).fetchall()
            total_ind = sum(r[1] for r in res_ind)
            print(f"Distribución de Status en bm_analyses | Total: {total_ind}")
            for r in res_ind:
                pct = (r[1] / total_ind * 100) if total_ind > 0 else 0
                print(f"  {r[0]:<25}: {r[1]:>5} ({pct:>5.1f}%)")
        except Exception as e_ind:
            print(f"Error consultando bm_analyses: {e_ind}")

        print("\n" + "=" * 130)
        print("FIN DE AUDITORIA ERR-02")
        print("=" * 130)

if __name__ == "__main__":
    asyncio.run(run_audit())
