"""
Diagnostic and backfill script for records with NULL company_id or service_id.
Runs in dry-run mode by default. Run with --commit to apply changes.
"""
import sys
import argparse
import asyncio
from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.db import get_engine
from app.models.analyses import Analysis, CallAnalysisCurrent
from app.models.mass_evaluations import MassEvaluationResult
from app.models.prompts import Prompt
from app.models.users import User
from app.models.services import Service

async def run_backfill(commit: bool):
    engine = get_engine()
    
    async with AsyncSession(engine) as db:
        print("Starting Diagnostic / Backfill dry-run...")
        print("Connection URL:", engine.url)
        
        # 1. Load prompts mapping for resolution
        print("\n1. Loading prompt metadata...")
        prompts_res = await db.execute(select(Prompt.prompt_id, Prompt.company_id, Prompt.service_id))
        prompts = {p[0]: (p[1], p[2]) for p in prompts_res.fetchall()}
        print(f"Loaded {len(prompts)} prompts from database.")
        
        # 2. Load agent/user mappings for resolution
        print("\n2. Loading user/agent metadata...")
        users_res = await db.execute(select(User.hubspot_owner_id, User.company_id).where(User.hubspot_owner_id.is_not(None)))
        agents = {u[0]: u[1] for u in users_res.fetchall()}
        print(f"Loaded {len(agents)} agents with hubspot_owner_id.")

        # Default fallback
        fallback_company_id = 1  # Boston Medical
        fallback_service_id = 1  # Front
        
        # Lists to keep track of updates
        analyses_to_update = []
        currents_to_update = []
        mass_to_update = []

        # --- A. Scan bm_analyses ---
        print("\nScanning bm_analyses for NULL company_id or service_id...")
        stmt_a = select(Analysis).where((Analysis.company_id.is_(None)) | (Analysis.service_id.is_(None)))
        res_a = await db.execute(stmt_a)
        null_a = res_a.scalars().all()
        print(f"Found {len(null_a)} rows with NULL company_id/service_id in bm_analyses.")
        
        for a in null_a:
            resolved_company = a.company_id
            resolved_service = a.service_id
            reason = []
            
            # Resolve via prompt
            if a.prompt_id and a.prompt_id in prompts:
                p_comp, p_svc = prompts[a.prompt_id]
                if resolved_company is None and p_comp is not None:
                    resolved_company = p_comp
                    reason.append(f"Prompt {a.prompt_id} company_id={p_comp}")
                if resolved_service is None and p_svc is not None:
                    resolved_service = p_svc
                    reason.append(f"Prompt {a.prompt_id} service_id={p_svc}")
            
            # Resolve via agent
            if resolved_company is None and a.hubspot_owner_id and a.hubspot_owner_id in agents:
                resolved_company = agents[a.hubspot_owner_id]
                reason.append(f"Agent {a.hubspot_owner_id} company_id={resolved_company}")
                
            # Resolve via fallback
            if resolved_company is None:
                resolved_company = fallback_company_id
                reason.append(f"Fallback company_id={fallback_company_id}")
            if resolved_service is None:
                resolved_service = fallback_service_id
                reason.append(f"Fallback service_id={fallback_service_id}")
                
            print(f"  - Analysis ID {a.analysis_id} (call_id: {a.call_id}): "
                  f"company_id {a.company_id} -> {resolved_company}, "
                  f"service_id {a.service_id} -> {resolved_service} "
                  f"| Reason: {', '.join(reason)}")
            analyses_to_update.append((a.analysis_id, resolved_company, resolved_service))

        # --- B. Scan bm_call_analysis_current ---
        print("\nScanning bm_call_analysis_current for NULL company_id or service_id...")
        stmt_c = select(CallAnalysisCurrent).where((CallAnalysisCurrent.company_id.is_(None)) | (CallAnalysisCurrent.service_id.is_(None)))
        res_c = await db.execute(stmt_c)
        null_c = res_c.scalars().all()
        print(f"Found {len(null_c)} rows with NULL company_id/service_id in bm_call_analysis_current.")
        
        for c in null_c:
            resolved_company = c.company_id
            resolved_service = c.service_id
            reason = []
            
            # Resolve via prompt
            if c.prompt_id and c.prompt_id in prompts:
                p_comp, p_svc = prompts[c.prompt_id]
                if resolved_company is None and p_comp is not None:
                    resolved_company = p_comp
                    reason.append(f"Prompt {c.prompt_id} company_id={p_comp}")
                if resolved_service is None and p_svc is not None:
                    resolved_service = p_svc
                    reason.append(f"Prompt {c.prompt_id} service_id={p_svc}")
            
            # Resolve via agent
            if resolved_company is None and c.hubspot_owner_id and c.hubspot_owner_id in agents:
                resolved_company = agents[c.hubspot_owner_id]
                reason.append(f"Agent {c.hubspot_owner_id} company_id={resolved_company}")
                
            # Resolve via fallback
            if resolved_company is None:
                resolved_company = fallback_company_id
                reason.append(f"Fallback company_id={fallback_company_id}")
            if resolved_service is None:
                resolved_service = fallback_service_id
                reason.append(f"Fallback service_id={fallback_service_id}")
                
            print(f"  - Call {c.call_id} (type: {c.analysis_type}): "
                  f"company_id {c.company_id} -> {resolved_company}, "
                  f"service_id {c.service_id} -> {resolved_service} "
                  f"| Reason: {', '.join(reason)}")
            currents_to_update.append((c.call_id, c.analysis_type, resolved_company, resolved_service))

        # --- C. Scan bm_mass_evaluation_results ---
        print("\nScanning bm_mass_evaluation_results for NULL company_id or service_id...")
        stmt_m = select(MassEvaluationResult).where((MassEvaluationResult.company_id.is_(None)) | (MassEvaluationResult.service_id.is_(None)))
        res_m = await db.execute(stmt_m)
        null_m = res_m.scalars().all()
        print(f"Found {len(null_m)} rows with NULL company_id/service_id in bm_mass_evaluation_results.")
        
        for m in null_m:
            resolved_company = m.company_id
            resolved_service = m.service_id
            reason = []
            
            # Resolve via prompt
            if m.prompt_id and m.prompt_id in prompts:
                p_comp, p_svc = prompts[m.prompt_id]
                if resolved_company is None and p_comp is not None:
                    resolved_company = p_comp
                    reason.append(f"Prompt {m.prompt_id} company_id={p_comp}")
                if resolved_service is None and p_svc is not None:
                    resolved_service = p_svc
                    reason.append(f"Prompt {m.prompt_id} service_id={p_svc}")
            
            # Resolve via agent
            if resolved_company is None and m.hubspot_owner_id and m.hubspot_owner_id in agents:
                resolved_company = agents[m.hubspot_owner_id]
                reason.append(f"Agent {m.hubspot_owner_id} company_id={resolved_company}")
                
            # Resolve via fallback
            if resolved_company is None:
                resolved_company = fallback_company_id
                reason.append(f"Fallback company_id={fallback_company_id}")
            if resolved_service is None:
                resolved_service = fallback_service_id
                reason.append(f"Fallback service_id={fallback_service_id}")
                
            print(f"  - Mass Analysis ID {m.mass_analysis_id} (call_id: {m.call_id}): "
                  f"company_id {m.company_id} -> {resolved_company}, "
                  f"service_id {m.service_id} -> {resolved_service} "
                  f"| Reason: {', '.join(reason)}")
            mass_to_update.append((m.mass_analysis_id, resolved_company, resolved_service))

        # --- EXECUTION ---
        if commit:
            print("\n=======================================================")
            print("COMMITTING CHANGES TO DATABASE...")
            
            # Update analyses
            updated_a_count = 0
            for a_id, comp_id, svc_id in analyses_to_update:
                await db.execute(
                    update(Analysis)
                    .where(Analysis.analysis_id == a_id)
                    .values(company_id=comp_id, service_id=svc_id)
                )
                updated_a_count += 1
                
            # Update currents
            updated_c_count = 0
            for call_id, analysis_type, comp_id, svc_id in currents_to_update:
                await db.execute(
                    update(CallAnalysisCurrent)
                    .where((CallAnalysisCurrent.call_id == call_id) & (CallAnalysisCurrent.analysis_type == analysis_type))
                    .values(company_id=comp_id, service_id=svc_id)
                )
                updated_c_count += 1
                
            # Update mass evaluation results
            updated_m_count = 0
            for m_id, comp_id, svc_id in mass_to_update:
                await db.execute(
                    update(MassEvaluationResult)
                    .where(MassEvaluationResult.mass_analysis_id == m_id)
                    .values(company_id=comp_id, service_id=svc_id)
                )
                updated_m_count += 1
                
            await db.commit()
            print(f"Commit successful! Updated:\n"
                  f"  - {updated_a_count} rows in bm_analyses\n"
                  f"  - {updated_c_count} rows in bm_call_analysis_current\n"
                  f"  - {updated_m_count} rows in bm_mass_evaluation_results")
        else:
            print("\n=======================================================")
            print("DRY-RUN MODE: No changes were written to the database.")
            print("To commit these changes, run the script with the --commit flag.")
            
    await engine.dispose()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill missing company_id and service_id in analysis tables.")
    parser.add_argument("--commit", action="store_true", help="Apply changes to the database.")
    args = parser.parse_args()
    
    asyncio.run(run_backfill(args.commit))
