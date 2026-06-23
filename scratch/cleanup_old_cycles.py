import sys
import os
import argparse
import asyncio
from sqlalchemy import select, delete, text
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import SessionLocal
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingCallSession,
    TrainingCallEvaluation,
)

async def audit_and_cleanup(execute: bool, confirm: bool):
    print("=== INICIANDO AUDITORÍA Y LIMPIEZA DE CICLOS ===")
    print(f"Modo: {'EJECUCIÓN' if execute else 'SIMULACIÓN (DRY RUN)'}")
    
    async with SessionLocal() as db:
        # 1. Fetch all agent reports
        stmt_reports = select(TrainingAgentReport).order_by(TrainingAgentReport.created_at.desc())
        res_reports = await db.execute(stmt_reports)
        reports = list(res_reports.scalars().all())
        
        # 2. Identify the target report to preserve
        # Fernanda Rodrigues, period 01/06/2026 - 23/06/2026
        target_report = None
        to_delete_reports = []
        
        for r in reports:
            # Check period start and end (ignoring exact time of day if needed, but checking 2026-06-01 to 2026-06-23)
            p_start_str = r.period_start.strftime("%Y-%m-%d")
            p_end_str = r.period_end.strftime("%Y-%m-%d")
            
            is_target = (
                r.agent_name == "Fernanda Rodrigues"
                and p_start_str == "2026-06-01"
                and p_end_str == "2026-06-23"
            )
            
            if is_target:
                target_report = r
            else:
                to_delete_reports.append(r)
                
        # Print summary
        if target_report:
            print(f"\n[OK] Encontrado ciclo a PRESERVAR:")
            print(f"  ID: {target_report.training_report_id} | Agente: {target_report.agent_name} | Periodo: {target_report.period_start.strftime('%Y-%m-%d')} a {target_report.period_end.strftime('%Y-%m-%d')} | Status: {target_report.status}")
        else:
            print("\n[WARNING] ¡No se encontró el ciclo de Fernanda Rodrigues (01/06/2026 a 23/06/2026) en la base de datos!")
            
        print(f"\nCiclos antiguos/otros detectados para ELIMINAR: {len(to_delete_reports)}")
        for r in to_delete_reports:
            print(f"  ID: {r.training_report_id} | Agente: {r.agent_name} | Periodo: {r.period_start.strftime('%Y-%m-%d')} a {r.period_end.strftime('%Y-%m-%d')} | Status: {r.status} | Creado: {r.created_at}")

        if not to_delete_reports:
            print("\nNo hay ciclos antiguos que requieran eliminación. El sistema está limpio.")
            return

        # 3. Collect all related entities for to-delete reports
        to_delete_report_ids = [r.training_report_id for r in to_delete_reports]
        
        # Evaluations
        stmt_evals = select(TrainingCallEvaluation).where(TrainingCallEvaluation.cycle_id.in_(to_delete_report_ids))
        res_evals = await db.execute(stmt_evals)
        evals_to_delete = list(res_evals.scalars().all())
        
        # Sessions
        stmt_sess = select(TrainingCallSession).where(TrainingCallSession.cycle_id.in_(to_delete_report_ids))
        res_sess = await db.execute(stmt_sess)
        sess_to_delete = list(res_sess.scalars().all())
        
        # Completion Statuses
        stmt_comps = select(TrainingCompletionStatus).where(TrainingCompletionStatus.training_report_id.in_(to_delete_report_ids))
        res_comps = await db.execute(stmt_comps)
        comps_to_delete = list(res_comps.scalars().all())
        
        # Simulation Prompts
        stmt_prompts = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.training_report_id.in_(to_delete_report_ids))
        res_prompts = await db.execute(stmt_prompts)
        prompts_to_delete = list(res_prompts.scalars().all())
        
        # Identify runs that will become empty or are associated with deleted reports
        stmt_runs = select(TrainingRun)
        res_runs = await db.execute(stmt_runs)
        all_runs = list(res_runs.scalars().all())
        
        runs_to_delete = []
        for run in all_runs:
            stmt_run_reps = select(TrainingAgentReport.training_report_id).where(TrainingAgentReport.training_run_id == run.training_run_id)
            res_run_reps = await db.execute(stmt_run_reps)
            run_rep_ids = [row[0] for row in res_run_reps.fetchall()]
            
            if all(rep_id in to_delete_report_ids for rep_id in run_rep_ids):
                runs_to_delete.append(run)

        # Print statistics
        print("\n=== RESUMEN DE ELEMENTOS A BORRAR ===")
        print(f"  Informes (reports): {len(to_delete_report_ids)}")
        print(f"  Evaluaciones (evaluations): {len(evals_to_delete)}")
        print(f"  Sesiones (sessions): {len(sess_to_delete)}")
        print(f"  Estados de simulación (completion statuses): {len(comps_to_delete)}")
        print(f"  Prompts de simulación (simulation prompts): {len(prompts_to_delete)}")
        print(f"  Runs de entrenamiento (training runs): {len(runs_to_delete)}")
        print("=====================================")

        if not execute:
            print("\n[DRY RUN] No se realizó ninguna modificación. Para ejecutar, usa la opción '--execute'.")
            return
            
        # Ask for confirmation if not --confirm
        if not confirm:
            print("\n¡ALERTA! Estás a punto de borrar registros de la base de datos de forma permanente.")
            user_input = input("¿Estás seguro de continuar? Escribe 'SÍ' o 'YES' para confirmar: ")
            if user_input.strip().upper() not in ["SÍ", "SI", "YES"]:
                print("Operación cancelada por el usuario.")
                return

        # 4. Perform the cleanup transactionally
        print("\n[EXECUTE] Iniciando eliminación segura de datos...")
        try:
            # Deleting evaluations first
            if evals_to_delete:
                eval_ids = [e.evaluation_id for e in evals_to_delete]
                await db.execute(delete(TrainingCallEvaluation).where(TrainingCallEvaluation.evaluation_id.in_(eval_ids)))
                print(f"  - Eliminadas {len(evals_to_delete)} evaluaciones.")
                
            # Deleting sessions
            if sess_to_delete:
                sess_ids = [s.session_id for s in sess_to_delete]
                await db.execute(delete(TrainingCallSession).where(TrainingCallSession.session_id.in_(sess_ids)))
                print(f"  - Eliminadas {len(sess_to_delete)} sesiones.")
                
            # Deleting completion status
            if comps_to_delete:
                comp_ids = [c.completion_id for c in comps_to_delete]
                await db.execute(delete(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id.in_(comp_ids)))
                print(f"  - Eliminados {len(comps_to_delete)} estados de simulación.")
                
            # Deleting prompts
            if prompts_to_delete:
                prompt_ids = [p.simulation_prompt_id for p in prompts_to_delete]
                await db.execute(delete(TrainingSimulationPrompt).where(TrainingSimulationPrompt.simulation_prompt_id.in_(prompt_ids)))
                print(f"  - Eliminados {len(prompts_to_delete)} prompts de simulación.")
                
            # Deleting reports
            await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.training_report_id.in_(to_delete_report_ids)))
            print(f"  - Eliminados {len(to_delete_report_ids)} informes de agente.")
            
            # Deleting runs
            if runs_to_delete:
                run_ids = [run.training_run_id for run in runs_to_delete]
                await db.execute(delete(TrainingRun).where(TrainingRun.training_run_id.in_(run_ids)))
                print(f"  - Eliminadas {len(runs_to_delete)} runs vacías.")
                
            await db.commit()
            print("[SUCCESS] Limpieza transaccional finalizada correctamente.")
            
        except Exception as e:
            await db.rollback()
            print(f"[ERROR] Ocurrió un error inesperado durante la transacción. Rollback ejecutado. Detalles: {e}")
            raise e

        # 5. Post-verification
        print("\n=== VERIFICACIÓN POSTERIOR ===")
        rep_count = (await db.execute(select(text("COUNT(*)")).select_from(TrainingAgentReport))).scalar()
        comp_count = (await db.execute(select(text("COUNT(*)")).select_from(TrainingCompletionStatus))).scalar()
        prompt_count = (await db.execute(select(text("COUNT(*)")).select_from(TrainingSimulationPrompt))).scalar()
        run_count = (await db.execute(select(text("COUNT(*)")).select_from(TrainingRun))).scalar()
        
        print(f"  Informes restantes: {rep_count}")
        print(f"  Simulaciones (completions) restantes: {comp_count}")
        print(f"  Prompts restantes: {prompt_count}")
        print(f"  Runs restantes: {run_count}")
        
        if target_report:
            verify_stmt = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == target_report.training_report_id)
            verify_res = await db.execute(verify_stmt)
            verify_rep = verify_res.scalars().first()
            if verify_rep:
                print(f"[OK] El ciclo de Fernanda Rodrigues (ID {target_report.training_report_id}) permanece intacto.")
            else:
                print("[CRITICAL] ¡Error de verificación! El ciclo de Fernanda Rodrigues ha sido eliminado.")
        else:
            print("[INFO] No había ciclo de Fernanda para validar inicialmente.")
        print("==============================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Purge old personalized training cycles safely.")
    parser.add_argument("--execute", action="store_true", help="Perform the actual database deletion.")
    parser.add_argument("--confirm", action="store_true", help="Bypass interactive confirmation prompt.")
    args = parser.parse_args()
    
    asyncio.run(audit_and_cleanup(execute=args.execute, confirm=args.confirm))
