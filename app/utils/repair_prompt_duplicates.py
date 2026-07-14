"""
Repair prompt duplicates script.
Usage:
  python app/utils/repair_prompt_duplicates.py --prompt-id 25 --dry-run
  python app/utils/repair_prompt_duplicates.py --prompt-id 25 --apply
"""
import argparse
import asyncio
import sys
import os
import re
from datetime import datetime, timezone
from sqlalchemy import select, update

# Add parent directory to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.db import AsyncSessionLocal
from app.models.prompts import Prompt, PromptVersion
from app.services.prompts_service import sync_prompt_text_with_active_criteria

def get_stats(text: str) -> dict:
    if not text:
        return {
            "length": 0,
            "definicion_count": 0,
            "prioridades_count": 0,
            "criterios_count": 0,
            "start_delim": 0,
            "end_delim": 0,
            "output_keys": 0
        }
    keys_found = re.findall(r'"output_key"\s*:\s*"([^"]+)"', text)
    keys_found_alt = re.findall(r'output_key\s*:\s*([a-zA-Z0-9_]+)', text)
    unique_keys = len(set(keys_found + keys_found_alt))
    return {
        "length": len(text),
        "definicion_count": text.count("### DEFINICIÓN DE TIPOS DE LLAMADA"),
        "prioridades_count": text.count("### PRIORIDADES EN CASO DE CONFLICTO"),
        "criterios_count": text.count("### CRITERIOS DE ANÁLISIS"),
        "start_delim": text.count("<!-- BM_CRITERIA_BLOCK_START -->"),
        "end_delim": text.count("<!-- BM_CRITERIA_BLOCK_END -->"),
        "output_keys": unique_keys
    }

async def repair(prompt_id: int | None, prompt_name: str | None, prompt_type: str | None, dry_run: bool):
    async with AsyncSessionLocal() as db:
        # Find prompt
        prompt = None
        if prompt_id is not None:
            prompt = await db.get(Prompt, prompt_id)
        elif prompt_name is not None:
            stmt = select(Prompt).where(
                Prompt.prompt_name == prompt_name,
                Prompt.prompt_type == (prompt_type or "audio")
            )
            res = await db.execute(stmt)
            prompt = res.scalars().first()
            
        if not prompt:
            print("ERROR: Prompt not found with the specified parameters.")
            sys.exit(1)
            
        print(f"Target Prompt: ID {prompt.prompt_id} | Name: '{prompt.prompt_name}' | Type: '{prompt.prompt_type}'")
        
        # Get current version
        v_stmt = select(PromptVersion).where(
            PromptVersion.prompt_id == prompt.prompt_id,
            PromptVersion.is_current == True
        )
        v_res = await db.execute(v_stmt)
        current_version = v_res.scalars().first()
        if not current_version:
            print("ERROR: No current active PromptVersion found for this prompt.")
            sys.exit(1)
            
        orig_text = current_version.prompt or ""
        print(f"Current version label: {current_version.version_label} | ID: {current_version.id}")
        
        stats_before = get_stats(orig_text)
        
        # 1. Run sync_prompt_text_with_active_criteria to automatically run our sanitization and unified criteria sync!
        try:
            from app.services.prompts_service import sanitize_static_prompt_sections, sync_prompt_text_with_criteria_list
            from app.services.criteria_service import get_active_criteria
            from app.models.typologies import Typology
            
            # Fetch typologies
            from app.models.services import Service
            from app.models.prompts import BaseStructureTypology
            p = prompt
            service_id = p.service_id
            if not service_id:
                s_res = await db.execute(select(Service.service_id).where(Service.service_key == "front"))
                service_id = s_res.scalar()
                
            typologies = []
            if p and p.base_structure_id:
                t_res = await db.execute(
                    select(Typology)
                    .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
                    .where(
                        BaseStructureTypology.base_structure_id == p.base_structure_id,
                        Typology.is_active == True
                    )
                    .order_by(Typology.sort_order.asc())
                )
                typologies = t_res.scalars().all()
                
            if not typologies and service_id:
                t_res = await db.execute(
                    select(Typology)
                    .where(Typology.service_id == service_id, Typology.is_active == True)
                    .order_by(Typology.sort_order.asc())
                )
                typologies = t_res.scalars().all()
                
            active_criteria = await get_active_criteria(db, prompt.prompt_id)
            
            sanitized_prompt_text, stats = sanitize_static_prompt_sections(orig_text)
            sanitized_changed = (stats["removed_count"] > 0) or (sanitized_prompt_text != orig_text)
            
            new_text, changed = await sync_prompt_text_with_active_criteria(db, prompt.prompt_id, orig_text)
        except Exception as e:
            print(f"ERROR: Sync process failed: {e}")
            sys.exit(1)
            
        stats_after = get_stats(new_text)
        
        print("\n=== COMPARISON STATS ===")
        print(f"Metric                        | Before     | After")
        print(f"--------------------------------------------------")
        print(f"Length (chars)                | {stats_before['length']:<10} | {stats_after['length']}")
        print(f"### DEFINICIÓN DE TIPOS       | {stats_before['definicion_count']:<10} | {stats_after['definicion_count']}")
        print(f"### PRIORIDADES EN CONFLICTO  | {stats_before['prioridades_count']:<10} | {stats_after['prioridades_count']}")
        print(f"### CRITERIOS DE ANÁLISIS     | {stats_before['criterios_count']:<10} | {stats_after['criterios_count']}")
        print(f"Start delimiter count         | {stats_before['start_delim']:<10} | {stats_after['start_delim']}")
        print(f"End delimiter count           | {stats_before['end_delim']:<10} | {stats_after['end_delim']}")
        print(f"Output keys count             | {stats_before['output_keys']:<10} | {stats_after['output_keys']}")
        
        under_limit = stats_after['length'] <= 120000
        print(f"Under 120,000 limit?          | {'Yes' if under_limit else 'NO'}")
        
        if dry_run:
            print("\n*** DRY-RUN MODE: No changes were saved to the database. ***")
            return
            
        if not changed:
            print("\nPrompt is already fully clean. No repair needed.")
            return
            
        # --- Apply changes ---
        print("\nApplying changes to the database...")
        # 1. Mark current version as not current
        current_version.is_current = False
        db.add(current_version)
        
        # 2. Generate a new version label (increment timestamp or simple tag)
        now = datetime.now(timezone.utc)
        label = f"v{now.strftime('%Y%m%d-%H%M%S')}"
        
        # 3. Create a new PromptVersion
        new_v = PromptVersion(
            prompt_id=prompt.prompt_id,
            prompt=new_text,
            version_label=label,
            version_name=f"Reparación saneada - {now.strftime('%d/%m/%Y')}",
            updated_by="System Repair Script",
            updated_by_email="repair@doobot.ai",
            change_note="Reparación automática: Saneamiento de secciones estáticas duplicadas y reconstrucción delimitada de criterios.",
            is_current=True,
            created_at=now
        )
        db.add(new_v)
        
        # 4. Update the prompt updated_at
        prompt.updated_at = now
        db.add(prompt)
        
        # Commit transaction
        await db.commit()
        print(f"SUCCESS: Created new PromptVersion ID {new_v.id} marked as current with label '{label}'!")

def main():
    parser = argparse.ArgumentParser(description="Repair duplicated headers and sync criteria in prompt versions.")
    parser.add_argument("--prompt-id", type=int, help="Prompt database ID")
    parser.add_argument("--prompt-name", type=str, help="Prompt name")
    parser.add_argument("--type", type=str, default="audio", help="Prompt type (audio/text)")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Diagnose and preview changes without applying")
    group.add_argument("--apply", action="store_true", help="Apply changes and create a new sanitized version")
    
    args = parser.parse_args()
    
    if args.prompt_id is None and args.prompt_name is None:
        print("ERROR: Either --prompt-id or --prompt-name must be provided.")
        sys.exit(1)
        
    asyncio.run(repair(args.prompt_id, args.prompt_name, args.type, args.dry_run))

if __name__ == "__main__":
    main()
