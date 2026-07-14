"""
Unit tests for prompt static section sanitization and criteria sync idempotency.
Runs on a safe local SQLite database.
"""
import os
import sys

# Override DATABASE_URL to a safe local SQLite database before any imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///prompt_sanit_test.db"

# Ensure parent directory is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Compilation rule to translate PostgreSQL JSONB to SQLite JSON
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import BigInteger
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

import asyncio
import unittest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.services.db_init_service import init_db
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.typologies import Typology
from app.models.services import Service
from app.services.prompts_service import (
    sanitize_static_prompt_sections,
    sync_prompt_text_with_active_criteria,
    sync_prompt_text_with_criteria_list
)

class TestPromptSanitization(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        # Create all tables first
        from app.db import Base
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            
        # Initialize SQLite DB and seed typologies/services
        await init_db()
        self.db = AsyncSession(self.engine)
        
        # Ensure we have a service in SQLite
        s_res = await self.db.execute(select(Service).where(Service.service_key == "front"))
        self.service = s_res.scalar()
        if not self.service:
            self.service = Service(service_key="front", service_name="Front Desk")
            self.db.add(self.service)
            await self.db.flush()
            
        # Create a dummy prompt with manual ID
        self.prompt = Prompt(
            prompt_id=9999,
            prompt_name="Test Artificial Prompt",
            prompt_type="audio",
            is_active=True,
            service_id=self.service.service_id
        )
        self.db.add(self.prompt)
        await self.db.flush()
        
        # Add 3 active criteria for this prompt in the database
        self.criteria = [
            PromptCriterion(
                criterion_id=10001,
                prompt_id=9999,
                criterion_key="c1",
                criterion_name="Criterion One",
                criterion_description="Description for c1",
                criterion_type="score_1_10",
                output_key="c1_key",
                feed_key="c1_feed",
                is_active=True,
                is_required=True
            ),
            PromptCriterion(
                criterion_id=10002,
                prompt_id=9999,
                criterion_key="c2",
                criterion_name="Criterion Two",
                criterion_description="Description for c2",
                criterion_type="score_1_10",
                output_key="c2_key",
                feed_key="c2_feed",
                is_active=True,
                is_required=True
            ),
            PromptCriterion(
                criterion_id=10003,
                prompt_id=9999,
                criterion_key="c3",
                criterion_name="Criterion Three",
                criterion_description="Description for c3",
                criterion_type="score_1_10",
                output_key="c3_key",
                feed_key="c3_feed",
                is_active=True,
                is_required=True
            )
        ]
        for c in self.criteria:
            self.db.add(c)
        await self.db.flush()
        
        # Fetch seeded typologies for Front
        t_res = await self.db.execute(
            select(Typology)
            .where(Typology.service_id == self.service.service_id, Typology.is_active == True)
        )
        self.typologies = t_res.scalars().all()
        
        # Construct an artificial corrupted prompt text
        self.corrupted_text = (
            "System Prompt Intro.\n\n"
            "### DEFINICIÓN DE TIPOS DE LLAMADA\n"
            "Esta es la clasificación.\n\n"
            "### PRIORIDADES EN CASO DE CONFLICTO\n"
            "Orden de prioridad 1\n\n"
            "### PRIORIDADES EN CASO DE CONFLICTO\n"
            "Orden de prioridad 2 (Duplicate)\n\n"
            "### PRIORIDADES EN CASO DE CONFLICTO\n"
            "Orden de prioridad 3 (Duplicate)\n\n"
            "### PRIORIDADES EN CASO DE CONFLICTO\n"
            "Orden de prioridad 4 (Duplicate)\n\n"
            "### PRIORIDADES EN CASO DE CONFLICTO\n"
            "Orden de prioridad 5 (Duplicate)\n\n"
            "### CRITERIOS DE ANÁLISIS\n"
            "Listado de criterios sin delimitadores aquí:\n"
            "- Criterio viejo\n\n"
            "### FORMATO DE SALIDA JSON\n"
            "Old output format json block."
        )
        
    async def asyncTearDown(self):
        await self.db.close()
        # Clean up database files if necessary
        try:
            if os.path.exists("prompt_sanit_test.db"):
                os.remove("prompt_sanit_test.db")
        except Exception:
            pass

    def test_1_2_3_sanitize_static_sections_counts(self):
        # El saneador deja DEFINICIÓN, PRIORIDADES y CRITERIOS exactamente 1 vez
        sanitized, stats = sanitize_static_prompt_sections(self.corrupted_text)
        
        self.assertEqual(sanitized.count("### DEFINICIÓN DE TIPOS DE LLAMADA"), 1)
        self.assertEqual(sanitized.count("### PRIORIDADES EN CASO DE CONFLICTO"), 1)
        self.assertEqual(sanitized.count("### CRITERIOS DE ANÁLISIS"), 1)
        self.assertEqual(stats["removed_count"], 4)
        self.assertEqual(stats["details"].get("prioridades en caso de conflicto"), 4)

    def test_6_sanitize_is_idempotent(self):
        # Ejecutar saneamiento dos veces produce exactamente el mismo texto
        sanitized_1, _ = sanitize_static_prompt_sections(self.corrupted_text)
        sanitized_2, stats_2 = sanitize_static_prompt_sections(sanitized_1)
        
        self.assertEqual(sanitized_1, sanitized_2)
        self.assertEqual(stats_2["removed_count"], 0)

    async def test_4_5_7_8_9_sync_idempotency_and_limits(self):
        # Sincronizar el prompt corrupto con la base de datos
        # 1. First sync run
        new_text_1, changed_1 = await sync_prompt_text_with_active_criteria(
            self.db, self.prompt.prompt_id, self.corrupted_text
        )
        self.assertTrue(changed_1)
        
        # El prompt finalizado contiene exactamente un bloque delimitado
        self.assertEqual(new_text_1.count("<!-- BM_CRITERIA_BLOCK_START -->"), 1)
        self.assertEqual(new_text_1.count("<!-- BM_CRITERIA_BLOCK_END -->"), 1)
        
        # El prompt finalizado contiene exactamente 1 ocurrencia de las secciones
        self.assertEqual(new_text_1.count("### DEFINICIÓN DE TIPOS DE LLAMADA"), 1)
        self.assertEqual(new_text_1.count("### PRIORIDADES EN CASO DE CONFLICTO"), 1)
        self.assertEqual(new_text_1.count("### CRITERIOS DE ANÁLISIS"), 1)
        self.assertEqual(new_text_1.count("### FORMATO DE SALIDA JSON"), 1)
        
        # 2. Second sync run (idempotency check)
        new_text_2, changed_2 = await sync_prompt_text_with_active_criteria(
            self.db, self.prompt.prompt_id, new_text_1
        )
        
        # Ejecutar sync dos veces no aumenta la longitud y no genera cambios
        self.assertFalse(changed_2)
        self.assertEqual(len(new_text_1), len(new_text_2))
        
        # Ejecutar sync dos veces no cambia el número de output keys
        for key in ["c1_key", "c2_key", "c3_key"]:
            self.assertEqual(new_text_1.count(key), new_text_2.count(key))
            self.assertGreater(new_text_1.count(key), 0)
        
        # El prompt resultante queda por debajo de 120,000
        self.assertLess(len(new_text_2), 120000)

import re

if __name__ == "__main__":
    unittest.main()
