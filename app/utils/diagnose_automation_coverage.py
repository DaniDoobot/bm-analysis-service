"""
Safe read-only diagnostic script to audit automation coverage, window gaps, duration filters,
HubSpot source call availability, and result persistence.

Run with:
$env:PYTHONPATH="c:/Users/Dani/proyectos/bm-analysis-service"; ./.venv/Scripts/python.exe app/utils/diagnose_automation_coverage.py
"""
import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select, func, text, desc, or_, case
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.mass_evaluations import (
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.models.services import Service
from app.models.prompts import Prompt
from app.models.users import User
from app.services.hubspot_service import HubSpotService


async def run_diagnostics(automation_id_filter: int | None = None, days_back: int = 7):
    try:
        from app.db import get_engine
        engine = get_engine()
    except Exception:
        db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///temp_diag.db")
        engine = create_async_engine(db_url)

    async with AsyncSession(engine) as db:
        print("=================================================================")
        print("A. IDENTIFICAR AUTOMATIZACIONES ACTIVAS Y CONFIGURACIÓN REAL")
        print("=================================================================")
        
        stmt_auto = select(MassAnalysisAutomation)
        res_auto = await db.execute(stmt_auto)
        automations = list(res_auto.scalars().all())

        print(f"Total automizaciones en DB: {len(automations)}\n")
        for a in automations:
            svc_name = None
            if a.service_id:
                res_s = await db.execute(select(Service).where(Service.service_id == a.service_id))
                s_obj = res_s.scalars().first()
                svc_name = f"{s_obj.service_name} ({s_obj.service_key})" if s_obj else str(a.service_id)

            p_name = None
            if a.prompt_id:
                res_p = await db.execute(select(Prompt).where(Prompt.prompt_id == a.prompt_id))
                p_obj = res_p.scalars().first()
                p_name = f"{p_obj.prompt_name} (id={p_obj.prompt_id})" if p_obj else str(a.prompt_id)

            print(f"--- Automation ID: {a.automation_id} | Name: '{a.name}' ---")
            print(f"  - Active: {a.is_active}")
            print(f"  - Service: {svc_name} (service_id={a.service_id})")
            print(f"  - Prompt: {p_name} (prompt_id={a.prompt_id}, version_id={a.prompt_version_id})")
            print(f"  - Interval Minutes: {a.interval_minutes}")
            print(f"  - Lookback Minutes: {a.lookback_minutes}")
            print(f"  - Delay Minutes: {a.delay_minutes}")
            print(f"  - Min Duration Seconds: {a.min_duration_seconds} (mins={a.min_duration_seconds/60.0 if a.min_duration_seconds else 0:.2f})")
            print(f"  - Direction Filter: '{a.direction_filter}'")
            print(f"  - Agent Owner IDs: {a.agent_owner_ids}")
            print(f"  - Linked Job ID: {a.job_id}")
            print(f"  - Created At: {a.created_at}")
            print(f"  - Last Run At: {a.last_run_at}\n")

        print("=================================================================")
        print("B. AUDITAR RUNS DE AUTOMATIZACIÓN Y RUNS MASIVOS VINCULADOS")
        print("=================================================================")
        
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days_back)

        for a in automations:
            print(f"\n================ Automation ID {a.automation_id}: '{a.name}' ================")
            stmt_runs = (
                select(MassAnalysisAutomationRun)
                .where(
                    MassAnalysisAutomationRun.automation_id == a.automation_id,
                    MassAnalysisAutomationRun.started_at >= cutoff
                )
                .order_by(MassAnalysisAutomationRun.started_at.asc())
            )
            res_runs = await db.execute(stmt_runs)
            auto_runs = list(res_runs.scalars().all())

            print(f"Total runs en los últimos {days_back} días: {len(auto_runs)}")

            for ar in auto_runs:
                # Linked mass run
                mr = None
                if ar.run_id:
                    res_mr = await db.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == ar.run_id))
                    mr = res_mr.scalars().first()

                mr_info = f"status={mr.status}, found={mr.calls_found}, selected={mr.calls_selected}, analyzed={mr.calls_analyzed}, skipped={mr.calls_skipped}, failed={mr.calls_failed}" if mr else "None"

                print(f"  AutoRun #{ar.automation_run_id} | Status: {ar.status} | Job #{ar.job_id} | MassRun #{ar.run_id}")
                print(f"    - Started: {ar.started_at} | Finished: {ar.finished_at}")
                print(f"    - Window: {ar.window_from} ---> {ar.window_to}")
                print(f"    - AutoRun counts: found={ar.calls_found}, selected={ar.calls_selected}, skipped={ar.calls_skipped}")
                print(f"    - Linked MassRun: {mr_info}")
                if ar.error_message:
                    print(f"    - Error: {ar.error_message}")

            print("\n=================================================================")
            print(f"C. GAPS Y SOLAPAMIENTOS ENTRE VENTANAS (Automation #{a.automation_id})")
            print("=================================================================")
            
            if len(auto_runs) < 2:
                print("Insuficientes runs para calcular gaps.")
            else:
                total_gap_seconds = 0
                gap_count = 0
                overlap_count = 0

                for i in range(1, len(auto_runs)):
                    prev = auto_runs[i - 1]
                    curr = auto_runs[i]

                    if prev.window_to and curr.window_from:
                        diff_sec = (curr.window_from - prev.window_to).total_seconds()
                        diff_min = diff_sec / 60.0

                        if diff_sec > 5: # Gap > 5 seconds
                            gap_count += 1
                            total_gap_seconds += diff_sec
                            print(f"  [GAP DETECTADO] Entre Run #{prev.automation_run_id} y #{curr.automation_run_id}:")
                            print(f"    Prev Window To: {prev.window_to}")
                            print(f"    Next Window From: {curr.window_from}")
                            print(f"    GAP: {diff_min:.2f} minutos ({diff_sec:.0f} segundos)\n")
                        elif diff_sec < -5: # Overlap > 5 seconds
                            overlap_count += 1
                            print(f"  [OVERLAP] Entre Run #{prev.automation_run_id} y #{curr.automation_run_id}: {abs(diff_min):.2f} mins de solapamiento")

                print(f"Resumen Gaps Automation #{a.automation_id}: Total Gaps={gap_count}, Tiempo total perdido en gaps={total_gap_seconds/60.0:.2f} minutos")

        print("\n=================================================================")
        print("D & E. COMPARAR HUBSPOT VS EVALUACIONES EN DB & DEDUPLICACIÓN")
        print("=================================================================")

        # Query bm_mass_evaluation_results stats for the period
        res_db_evals = await db.execute(
            select(
                MassEvaluationResult.service_id,
                MassEvaluationResult.service_key,
                MassEvaluationResult.execution_source,
                MassEvaluationResult.status,
                func.count().label("cnt")
            )
            .where(MassEvaluationResult.created_at >= cutoff)
            .group_by(
                MassEvaluationResult.service_id,
                MassEvaluationResult.service_key,
                MassEvaluationResult.execution_source,
                MassEvaluationResult.status
            )
        )
        print("Conteos de bm_mass_evaluation_results por servicio, origen y estatus (últimos %d días):" % days_back)
        for r in res_db_evals.fetchall():
            print(f"  Service: {r[0]} ({r[1]}) | Source: {r[2]} | Status: {r[3]} | Count: {r[4]}")

        # Duration breakdown in bm_mass_evaluation_results
        print("\nDesglose de duraciones en bm_mass_evaluation_results (últimos %d días):" % days_back)
        res_dur = await db.execute(
            select(
                case(
                    (MassEvaluationResult.call_duration_seconds < 30, "0-30s"),
                    (MassEvaluationResult.call_duration_seconds.between(30, 60), "31-60s"),
                    (MassEvaluationResult.call_duration_seconds.between(61, 120), "61-120s"),
                    (MassEvaluationResult.call_duration_seconds > 120, ">120s (>2 min)"),
                    else_="Desconocida/Null"
                ).label("dur_bucket"),
                func.count().label("cnt")
            )
            .where(MassEvaluationResult.created_at >= cutoff)
            .group_by("dur_bucket")
        )
        for r in res_dur.fetchall():
            print(f"  Duration Range '{r[0]}': {r[1]} evaluaciones")

        # Agents breakdown in bm_mass_evaluation_results
        print("\nDesglose por agente en bm_mass_evaluation_results (últimos %d días):" % days_back)
        res_ag = await db.execute(
            select(
                MassEvaluationResult.hubspot_owner_id,
                MassEvaluationResult.agent_name,
                func.count().label("cnt")
            )
            .where(MassEvaluationResult.created_at >= cutoff)
            .group_by(MassEvaluationResult.hubspot_owner_id, MassEvaluationResult.agent_name)
        )
        for r in res_ag.fetchall():
            print(f"  Owner #{r[0]} ({r[1]}): {r[2]} evaluaciones")

        # Check HubSpot search if token available
        hs_service = HubSpotService()
        if hs_service.token:
            print("\n-----------------------------------------------------------------")
            print("CONSULTANDO HUBSPOT API PARA COMPARACIÓN DE FUENTE DIRECTA")
            print("-----------------------------------------------------------------")
            for a in automations:
                # Query HubSpot for full candidate calls in last 48h
                date_to_hs = now
                date_from_hs = now - timedelta(days=2)

                filters_hs = {
                    "date_from": date_from_hs,
                    "date_to": date_to_hs,
                    "agent_owner_ids": a.agent_owner_ids,
                    "duration_min_seconds": a.min_duration_seconds,
                    "direction": a.direction_filter,
                    "only_with_recording": True,
                    "max_calls": 500
                }

                try:
                    hs_calls = await hs_service.search_calls_for_mass_evaluation(filters_hs)
                    print(f"\nHubSpot API Search para Automation #{a.automation_id} ('{a.name}') en últimas 48h:")
                    print(f"  - Filtros aplicados: date_from={date_from_hs.isoformat()}, min_duration={a.min_duration_seconds}s, direction={a.direction_filter}, agents={a.agent_owner_ids}")
                    print(f"  - Total llamadas devueltas por HubSpot: {len(hs_calls)}")

                    # Group hs_calls by duration
                    dur_0_30 = sum(1 for c in hs_calls if c.get("call_duration_seconds") and c["call_duration_seconds"] < 30)
                    dur_31_60 = sum(1 for c in hs_calls if c.get("call_duration_seconds") and 30 <= c["call_duration_seconds"] <= 60)
                    dur_61_120 = sum(1 for c in hs_calls if c.get("call_duration_seconds") and 61 <= c["call_duration_seconds"] <= 120)
                    dur_gt_120 = sum(1 for c in hs_calls if c.get("call_duration_seconds") and c["call_duration_seconds"] > 120)
                    dur_null = sum(1 for c in hs_calls if not c.get("call_duration_seconds"))

                    print(f"  - Desglose duraciones HubSpot:")
                    print(f"      0-30s: {dur_0_30}")
                    print(f"      31-60s: {dur_31_60}")
                    print(f"      61-120s: {dur_61_120}")
                    print(f"      >120s (>2m): {dur_gt_120}")
                    print(f"      Null/sin duración: {dur_null}")

                    # Check how many of these hs_calls exist in bm_mass_evaluation_results
                    hs_call_ids = [c["call_id"] for c in hs_calls]
                    if hs_call_ids:
                        res_eval_hs = await db.execute(
                            select(MassEvaluationResult.call_id, MassEvaluationResult.status, MassEvaluationResult.service_id)
                            .where(MassEvaluationResult.call_id.in_(hs_call_ids))
                        )
                        eval_map = {row[0]: (row[1], row[2]) for row in res_eval_hs.fetchall()}

                        evaluated_cnt = len(eval_map)
                        not_evaluated_cnt = len(hs_call_ids) - evaluated_cnt
                        print(f"  - De las {len(hs_call_ids)} llamadas encontradas en HubSpot:")
                        print(f"      Evaluadas en DB: {evaluated_cnt}")
                        print(f"      NO Evaluadas en DB: {not_evaluated_cnt}")

                        if not_evaluated_cnt > 0:
                            print("\n    Ejemplos de llamadas de HubSpot NO evaluadas:")
                            un_cnt = 0
                            for c in hs_calls:
                                cid = c["call_id"]
                                if cid not in eval_map:
                                    un_cnt += 1
                                    print(f"      [{un_cnt}] call_id={cid} | owner={c.get('hubspot_owner_id')} | duration={c.get('call_duration_seconds')}s | timestamp={c.get('call_timestamp')} | recording={bool(c.get('recording_url'))}")
                                    if un_cnt >= 10:
                                        break
                except Exception as e_hs_search:
                    print(f"Error consultando HubSpot API: {e_hs_search}")
        else:
            print("HUBSPOT_ACCESS_TOKEN no configurado; no se pudo consultar HubSpot API directamente.")

        print("\n=================================================================")
        print("H. RESULTADOS VISIBLES VS PERSISTIDOS")
        print("=================================================================")
        
        res_total_db = await db.execute(select(func.count(MassEvaluationResult.mass_analysis_id)))
        total_in_db = res_total_db.scalar()
        print(f"Total filas en bm_mass_evaluation_results (historico completo DB): {total_in_db}")

        res_completed_db = await db.execute(select(func.count(MassEvaluationResult.mass_analysis_id)).where(MassEvaluationResult.status == 'completed'))
        completed_in_db = res_completed_db.scalar()
        print(f"Total 'completed' en bm_mass_evaluation_results: {completed_in_db}")


if __name__ == "__main__":
    asyncio.run(run_diagnostics())
