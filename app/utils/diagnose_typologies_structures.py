"""
Diagnostic script for Typologies, Base Structures, and Specific Structures / Prompts.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.typologies import Typology
from app.models.prompts import Prompt, PromptBaseStructure, BaseStructureTypology


async def main():
    engine = get_engine()
    db_url = str(engine.url)
    print(f"Target DB: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    async with AsyncSession(engine) as db:
        print("\n--- 1. Typologies Verification ---")
        
        # Tipologías sin servicio
        typos_no_svc = (await db.execute(
            select(Typology).where(Typology.service_id == None)
        )).scalars().all()
        print(f"Typologies without service_id: {len(typos_no_svc)}")

        # Tipologías en servicios inexistentes
        typos_bad_svc = (await db.execute(
            select(Typology).outerjoin(Service, Typology.service_id == Service.service_id)
            .where(Service.service_id == None)
        )).scalars().all()
        print(f"Typologies in non-existent services: {len(typos_bad_svc)}")

        # Relación de empresa en tipologías (company_id vs service.company_id)
        typos_mismatch_company = (await db.execute(
            select(Typology, Service)
            .join(Service, Typology.service_id == Service.service_id)
            .where((Typology.company_id != Service.company_id) | (Typology.company_id == None))
        )).all()
        print(f"Typologies with company_id missing or mismatching service.company_id: {len(typos_mismatch_company)}")
        for t, s in typos_mismatch_company:
            print(f"  - Typology ID {t.typology_id} ({t.typology_name}): typo.company_id={t.company_id}, service.company_id={s.company_id}")

        print("\n--- 2. Base Structures Verification ---")

        # Estructuras base sin service_id
        base_no_svc = (await db.execute(
            select(PromptBaseStructure).where(PromptBaseStructure.service_id == None)
        )).scalars().all()
        print(f"Base Structures without service_id: {len(base_no_svc)}")

        # Estructuras base con company_id missing o mismatching service.company_id
        base_mismatch_company = (await db.execute(
            select(PromptBaseStructure, Service)
            .join(Service, PromptBaseStructure.service_id == Service.service_id)
            .where((PromptBaseStructure.company_id != Service.company_id) | (PromptBaseStructure.company_id == None))
        )).all()
        print(f"Base structures with company_id missing or mismatching service.company_id: {len(base_mismatch_company)}")
        for b, s in base_mismatch_company:
            print(f"  - Base Structure ID {b.id} ({b.structure_name}): base.company_id={b.company_id}, service.company_id={s.company_id}")

        print("\n--- 3. Base Structure <=> Typology Mappings ---")

        # Mappings donde la tipología pertenece a un servicio/empresa distinto que la estructura base
        bad_mappings = (await db.execute(
            select(BaseStructureTypology, PromptBaseStructure, Typology)
            .join(PromptBaseStructure, BaseStructureTypology.base_structure_id == PromptBaseStructure.id)
            .join(Typology, BaseStructureTypology.typology_id == Typology.typology_id)
            .where(PromptBaseStructure.service_id != Typology.service_id)
        )).all()
        print(f"Mappings where base structure and typology belong to different services: {len(bad_mappings)}")
        for m, b, t in bad_mappings:
            print(f"  - Mapping ID {m.id}: Base structure ID {b.id} (svc {b.service_id}) <-> Typology ID {t.typology_id} (svc {t.service_id})")

        print("\n--- 4. Specific Structures / Prompts Verification ---")

        # Prompts sin service_id
        prompts_no_svc = (await db.execute(
            select(Prompt).where(Prompt.service_id == None)
        )).scalars().all()
        print(f"Prompts without service_id: {len(prompts_no_svc)}")

        # Prompts con company_id missing o mismatching
        prompts_mismatch_company = (await db.execute(
            select(Prompt, Service)
            .join(Service, Prompt.service_id == Service.service_id)
            .where((Prompt.company_id != Service.company_id) | (Prompt.company_id == None))
        )).all()
        print(f"Prompts with company_id missing or mismatching service.company_id: {len(prompts_mismatch_company)}")
        for p, s in prompts_mismatch_company:
            print(f"  - Prompt ID {p.prompt_id} ({p.prompt_name}): prompt.company_id={p.company_id}, service.company_id={s.company_id}")

        # Prompts activos duplicados por servicio/tipo (audio o text)
        dup_active_prompts = (await db.execute(
            select(Prompt.service_id, Prompt.prompt_type, func.count(Prompt.prompt_id))
            .where(Prompt.is_active == True, Prompt.is_archived == False, Prompt.deleted_at == None)
            .group_by(Prompt.service_id, Prompt.prompt_type)
            .having(func.count(Prompt.prompt_id) > 1)
        )).all()
        print(f"Services with duplicate active prompts of same type: {len(dup_active_prompts)}")
        for svc_id, p_type, count in dup_active_prompts:
            print(f"  - Service {svc_id}, Type '{p_type}': {count} active prompts")


if __name__ == "__main__":
    asyncio.run(main())
