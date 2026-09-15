#!/usr/bin/env python
"""
scripts/seed_demo_data.py
==========================
Generates realistic synthetic data for the isolated Demo Company (Empresa Demo).

Populates:
1. Evaluation Structure:
   - 2 Prompts (Atención al Cliente, Ventas)
   - 2 Prompt Versions (v1.0)
   - 12 Prompt Criteria (6 per service)
   - 8 Typologies (4 per service)
2. Historical Mass Evaluations (Analytics & Dashboard):
   - 2 Mass Evaluation Jobs (1 per service)
   - 26 Mass Evaluation Runs (weekly over 90 days)
   - 3,600 MassEvaluationResult records with realistic distributions, timestamps,
     and full result_json / items_json payloads
   - 21,600 MassEvaluationCriterionResult records (6 criteria per call)
   - Realistic behavioral agent trends:
     * 15 Agents with Progressive Improvement (ascends from 5.8 to 8.6)
     * 24 Stable High-Performing Agents (7.8 to 8.8)
     * 12 Stable Medium Agents (6.2 to 7.2)
     * 6 Declining / Alert Agents (drops to 4.8, triggering alarms)
     * 3 New Agents (active only during the last 20 days)
   - Clear team performance differences:
     * Front Atención: High (~8.3)
     * Backoffice Atención: Medium-High (~7.4)
     * Equipo Comercial: Variable with dispersion (~7.6)
     * Equipo Retención: Tougher calls with friction (~6.3)
3. Improvement Cycles (Personalized Training):
   - 2 Training Runs (Run 1 completed 45d ago, Run 2 active 10d ago)
   - 32 TrainingAgentReport records (20 completed, 12 in progress)
   - TrainingSimulationPrompt & TrainingCompletionStatus records
4. Trainer Module:
   - 2 TrainerEvaluationConfig records (linked to service prompts)
   - 4 Published TrainerSimulation records with versions
   - 50 TrainerSession and TrainerEvaluation records with score & structured feedback

Usage:
  python scripts/seed_demo_data.py --help
  python scripts/seed_demo_data.py --dry-run
  python scripts/seed_demo_data.py --apply
  python scripts/seed_demo_data.py --apply --validate
  python scripts/seed_demo_data.py --apply --only analytics
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.typologies import Typology
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
)
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("seed_demo_data")

DEMO_COMPANY_KEY = "empresa-demo"
DAYS_HISTORY = 90
TARGET_EVALUATIONS = 3600


class DemoDataSeedError(Exception):
    """Base exception for demo data seeding errors."""
    pass


class DemoDataAlreadyExistsError(DemoDataSeedError):
    """Raised when evaluation data already exists for the company."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Criteria & Typology Configurations
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# 4 Evaluation Structures: Criteria & Typology Configurations
# ─────────────────────────────────────────────────────────────────────────────

# --- Servicio 1: Atención al Cliente ---

# Estructura 1.1: Calidad Atención General (Tipologías: consulta_general, soporte_tecnico)
CRITERIA_ATENCION_GENERAL = [
    {
        "criterion_key": "saludo_identificacion",
        "criterion_name": "Saludo e Identificación",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Saludo institucional formal y presentación clara con nombre y empresa.",
        "feedback_mid": "Saludo cordial aunque omite alguna fórmula protocolaria menor.",
        "feedback_low": "Entrada brusca sin identificación ni bienvenida formal.",
    },
    {
        "criterion_key": "escucha_activa",
        "criterion_name": "Escucha Activa y Comprensión",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.20"),
        "feedback_high": "Escucha atenta sin interrupciones, parafraseando la consulta del cliente.",
        "feedback_mid": "Escucha correcta aunque con alguna leve interrupción puntual.",
        "feedback_low": "Interrupciones frecuentes y desatención a los detalles expresados.",
    },
    {
        "criterion_key": "resolucion_consulta",
        "criterion_name": "Resolución de Consulta",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.25"),
        "feedback_high": "Explicación clara y certera de los pasos a seguir con solución definitiva.",
        "feedback_mid": "Resolución adecuada aunque requirió reiteraciones para quedar clara.",
        "feedback_low": "Información confusa o errónea que no resuelve la duda del usuario.",
    },
    {
        "criterion_key": "conocimiento_catalogo",
        "criterion_name": "Conocimiento del Catálogo",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.20"),
        "feedback_high": "Dominio exhaustivo de servicios, condiciones y procesos operativos.",
        "feedback_mid": "Conocimiento suficiente con pequeñas consultas a base de conocimiento.",
        "feedback_low": "Desconocimiento de procedimientos básicos y vacilaciones continuas.",
    },
    {
        "criterion_key": "cordialidad_empatia",
        "criterion_name": "Cordialidad y Empatía",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.15"),
        "feedback_high": "Tono cálido, paciente y empático adaptado al estado del interlocutor.",
        "feedback_mid": "Trato profesional y correcto aunque algo distante.",
        "feedback_low": "Tono frío, desinteresado o reactivo ante la insistencia.",
    },
    {
        "criterion_key": "despedida_protocolo",
        "criterion_name": "Cierre y Despedida",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Verificación de dudas adicionales y despedida formal impecable.",
        "feedback_mid": "Despedida correcta sin sondeo explícito de dudas adicionales.",
        "feedback_low": "Corte apresurado sin verificar si el cliente requería más ayuda.",
    },
]

# Estructura 1.2: Gestión Reclamaciones (Tipologías: reclamacion_incidencia, facturacion_cobros)
CRITERIA_GESTION_RECLAMACIONES = [
    {
        "criterion_key": "acogida_emocional",
        "criterion_name": "Acogida y Contención Emocional",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.15"),
        "feedback_high": "Validación inmediata del malestar del cliente con empatía genuina.",
        "feedback_mid": "Recepción formal del caso manteniendo una actitud neutral.",
        "feedback_low": "Minimización de la molestia o tono defensivo ante la queja.",
    },
    {
        "criterion_key": "analisis_conflicto",
        "criterion_name": "Análisis y Diagnóstico de la Queja",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.20"),
        "feedback_high": "Indagación profunda de la causa raíz con preguntas precisas y contraste de datos.",
        "feedback_mid": "Recogida de datos básica sin profundizar en el origen del fallo.",
        "feedback_low": "Registro superficial sin contrastar la información del expediente.",
    },
    {
        "criterion_key": "gestion_frustracion",
        "criterion_name": "Gestión de Frustración y Calma",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.25"),
        "feedback_high": "Manejo extraordinario de la tensión redirigiendo la conversación hacia soluciones.",
        "feedback_mid": "Mantiene la serenidad aunque cede a momentos de incomodidad evidente.",
        "feedback_low": "Se altera o entra en confrontación y discusión directa con el cliente.",
    },
    {
        "criterion_key": "propuesta_solucion",
        "criterion_name": "Propuesta Resolutiva o Compensación",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.20"),
        "feedback_high": "Alternativas justas y viables ajustadas a la política de reclamaciones.",
        "feedback_mid": "Ofrece solución estándar que requiere validación posterior dilatada.",
        "feedback_low": "Negativa rotunda sin alternativas viables ni margen de negociación.",
    },
    {
        "criterion_key": "compromiso_tiempos",
        "criterion_name": "Compromiso de Tiempos y Trazabilidad",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Plazos exactos de resolución con número de expediente comunicado.",
        "feedback_mid": "Tiempos estimados genéricos sin compromiso concreto de fecha.",
        "feedback_low": "Incertidumbre absoluta sobre plazos y próximos pasos de resolución.",
    },
    {
        "criterion_key": "cierre_reclamacion",
        "criterion_name": "Verificación de Conformidad Final",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Comprobación de que el cliente acepta el plan propuesto y queda conforme.",
        "feedback_mid": "Cierre formal de la queja con aceptación parcial o resignada.",
        "feedback_low": "Finalización abrupta dejando la reclamación en estado crítico.",
    },
]

TYPOLOGIES_ATENCION = [
    ("consulta_general", "Consulta General"),
    ("soporte_tecnico", "Soporte Técnico"),
    ("reclamacion_incidencia", "Reclamación o Incidencia"),
    ("facturacion_cobros", "Facturación y Cobros"),
]

# --- Servicio 2: Ventas ---

# Estructura 2.1: Venta Consultiva (Tipologías: captacion_nuevo, upselling_cross)
CRITERIA_VENTA_CONSULTIVA = [
    {
        "criterion_key": "sondeo_necesidades",
        "criterion_name": "Sondeo y Detección de Necesidades",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.20"),
        "feedback_high": "Preguntas abiertas estratégicas que identifican prioridades y presupuesto.",
        "feedback_mid": "Preguntas genéricas de calificación comercial básica.",
        "feedback_low": "Lanzamiento inmediato del producto sin indagar el perfil del cliente.",
    },
    {
        "criterion_key": "argumentacion_valor",
        "criterion_name": "Argumentación de la Propuesta de Valor",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.25"),
        "feedback_high": "Presentación adaptada conectando ventajas clave con los dolores expresados.",
        "feedback_mid": "Exposición estándar del catálogo sin personalización marcada.",
        "feedback_low": "Lectura plana del guión sin destacar valor diferencial frente a alternativas.",
    },
    {
        "criterion_key": "manejo_objeciones",
        "criterion_name": "Tratamiento de Objeciones Comerciales",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.20"),
        "feedback_high": "Aislamiento y refutación solvente de dudas de precio y condiciones.",
        "feedback_mid": "Respuesta tímida a objeciones que no disipa todas las dudas del cliente.",
        "feedback_low": "Bloqueo ante objeciones o concesión prematura de descuentos sin contrapartida.",
    },
    {
        "criterion_key": "tecnicas_cierre",
        "criterion_name": "Técnicas de Cierre y Compromiso",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.15"),
        "feedback_high": "Propuesta de cierre directo o por alternativa en el momento oportuno.",
        "feedback_mid": "Tentativa tibia de cierre dejando la decisión abierta al cliente.",
        "feedback_low": "Omisión del cierre, terminando la llamada sin solicitar compromiso.",
    },
    {
        "criterion_key": "transparencia_condiciones",
        "criterion_name": "Transparencia en Condiciones y Contratación",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Explicación transparente y precisa de tarifas, permanencias y garantías.",
        "feedback_mid": "Detalle correcto de precios omitiendo detalles de letra pequeña.",
        "feedback_low": "Falta de claridad u ocultación de cláusulas clave de contratación.",
    },
    {
        "criterion_key": "energia_comercial",
        "criterion_name": "Energía Comercial y Actitud Proactiva",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Dinamismo, seguridad y entusiasmo que generan confianza inmediata.",
        "feedback_mid": "Tono comercial neutro y profesional sin especial chispa.",
        "feedback_low": "Tono apagado, dubitativo o monótono que desmotiva el interés.",
    },
]

# Estructura 2.2: Retención/Renovación (Tipologías: renovacion, retencion_baja)
CRITERIA_RETENCION_RENOVACION = [
    {
        "criterion_key": "diagnostico_motivo",
        "criterion_name": "Diagnóstico del Motivo de Cancelación",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.25"),
        "feedback_high": "Identificación precisa de la causa raíz de insatisfacción o desinterés.",
        "feedback_mid": "Registro del motivo de baja sin profundizar en los detonantes.",
        "feedback_low": "Asunción apresurada del motivo sin indagar la causa real.",
    },
    {
        "criterion_key": "valoracion_historial",
        "criterion_name": "Puesta en Valor del Historial",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.15"),
        "feedback_high": "Reconocimiento expreso de la fidelidad del cliente y valor de la relación.",
        "feedback_mid": "Mención protocolaria a la antigüedad del cliente en la empresa.",
        "feedback_low": "Trato frío como si fuera un usuario desconocido sin historial.",
    },
    {
        "criterion_key": "negociacion_oferta",
        "criterion_name": "Negociación y Oferta de Fidelización",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.25"),
        "feedback_high": "Paquete de retención escalonado ajustado con precisión al coste-beneficio.",
        "feedback_mid": "Oferta de descuento genérica sin contraprestaciones asociadas.",
        "feedback_low": "Falta de propuesta de retención o entrega de la máxima rebaja sin negociar.",
    },
    {
        "criterion_key": "refuerzo_confianza",
        "criterion_name": "Refuerzo de Confianza en la Compañía",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.15"),
        "feedback_high": "Reafirmación de compromisos de servicio y mejoras operativas inminentes.",
        "feedback_mid": "Argumentos razonables sobre las ventajas de mantenerse en el servicio.",
        "feedback_low": "Incapacidad de responder al desprestigio o decepción con el servicio.",
    },
    {
        "criterion_key": "agilidad_tramitacion",
        "criterion_name": "Agilidad y Facilidad en la Gestión",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Trámite inmediato sin fricción, confirmando condiciones por escrito.",
        "feedback_mid": "Gestión adecuada con leve retraso en la activación del cambio.",
        "feedback_low": "Trabas burocráticas innecesarias que exasperan al cliente.",
    },
    {
        "criterion_key": "cierre_fidelizador",
        "criterion_name": "Cierre y Despedida de Fidelización",
        "criterion_type": "score_1_10",
        "weight": Decimal("0.10"),
        "feedback_high": "Agradecimiento sincero por la continuidad con mensaje reforzador.",
        "feedback_mid": "Despedida formal confirmando la permanencia o baja sin más.",
        "feedback_low": "Despedida tensa o displicente ante una baja no recuperada.",
    },
]

TYPOLOGIES_VENTAS = [
    ("captacion_nuevo", "Captación Nuevo Cliente"),
    ("upselling_cross", "Venta Cruzada / Up-Selling"),
    ("renovacion", "Renovación de Contrato"),
    ("retencion_baja", "Retención por Intención de Baja"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Seeding Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

async def get_target_company(db: AsyncSession, company_id: Optional[int] = None) -> Company:
    """Retrieve and validate the target demo company."""
    if company_id:
        stmt = select(Company).where(Company.company_id == company_id)
    else:
        stmt = select(Company).where(Company.company_key == DEMO_COMPANY_KEY)

    res = await db.execute(stmt)
    comp = res.scalars().first()
    if not comp:
        raise DemoDataSeedError(
            f"Target company (id={company_id}, key='{DEMO_COMPANY_KEY}') not found. "
            "Please run scripts/seed_demo_company.py first."
        )

    if comp.company_id == 1:
        raise DemoDataSeedError("CRITICAL SAFETY BLOCK: Cannot seed demo data into company_id=1.")

    if not comp.is_demo:
        raise DemoDataSeedError(
            f"Company '{comp.company_name}' (id={comp.company_id}) does not have is_demo=True. Aborting."
        )

    return comp


async def ensure_evaluation_structures(
    db: AsyncSession, company: Company
) -> Dict[str, Any]:
    """Create or load the 4 evaluation structures across the 2 services."""
    cid = company.company_id

    # Fetch services
    svc_res = await db.execute(select(Service).where(Service.company_id == cid))
    services = {s.service_key: s for s in svc_res.scalars().all()}
    if "atencion-al-cliente" not in services or "ventas" not in services:
        raise DemoDataSeedError("Expected services 'atencion-al-cliente' and 'ventas' not found for company.")

    svc_at = services["atencion-al-cliente"]
    svc_vn = services["ventas"]

    # 1. Prompts
    async def _get_or_create_prompt(svc: Service, name: str, desc: str) -> Prompt:
        p_res = await db.execute(
            select(Prompt).where(
                Prompt.company_id == cid,
                Prompt.service_id == svc.service_id,
                Prompt.prompt_name == name,
            )
        )
        p = p_res.scalars().first()
        if not p:
            p = Prompt(
                company_id=cid,
                service_id=svc.service_id,
                prompt_name=name,
                prompt_type="audio",
                description=desc,
                is_active=True,
            )
            db.add(p)
            await db.flush()
        return p

    # 2. Versions
    async def _get_or_create_version(prompt: Prompt) -> PromptVersion:
        v_res = await db.execute(
            select(PromptVersion).where(PromptVersion.prompt_id == prompt.prompt_id)
        )
        v = v_res.scalars().first()
        if not v:
            v = PromptVersion(
                prompt_id=prompt.prompt_id,
                prompt=f"System metaprompt para la evaluación automatizada de llamadas en {prompt.prompt_name}.",
                version_label="v1.0",
                version_name="Versión Inicial Demo",
            )
            db.add(v)
            await db.flush()
        return v

    # 3. Criteria
    async def _ensure_criteria(prompt: Prompt, criteria_defs: List[Dict[str, Any]]) -> Dict[str, PromptCriterion]:
        c_res = await db.execute(
            select(PromptCriterion).where(PromptCriterion.prompt_id == prompt.prompt_id)
        )
        existing = {c.criterion_key: c for c in c_res.scalars().all()}
        for idx, cdef in enumerate(criteria_defs):
            ck = cdef["criterion_key"]
            if ck not in existing:
                c = PromptCriterion(
                    prompt_id=prompt.prompt_id,
                    criterion_key=ck,
                    criterion_name=cdef["criterion_name"],
                    criterion_type=cdef.get("criterion_type", "score_1_10"),
                    order_index=(idx + 1) * 10,
                    is_active=True,
                )
                db.add(c)
                existing[ck] = c
        await db.flush()
        return existing

    # 4. Typologies
    async def _ensure_typologies(svc: Service, typo_defs: List[Tuple[str, str]]) -> Dict[str, Typology]:
        t_res = await db.execute(
            select(Typology).where(Typology.company_id == cid, Typology.service_id == svc.service_id)
        )
        existing = {t.typology_key: t for t in t_res.scalars().all()}
        for tkey, tname in typo_defs:
            if tkey not in existing:
                t = Typology(
                    company_id=cid,
                    service_id=svc.service_id,
                    typology_key=tkey,
                    typology_name=tname,
                    is_active=True,
                )
                db.add(t)
                existing[tkey] = t
        await db.flush()
        return existing

    typo_map_at = await _ensure_typologies(svc_at, TYPOLOGIES_ATENCION)
    typo_map_vn = await _ensure_typologies(svc_vn, TYPOLOGIES_VENTAS)

    # 1.1 Atención General
    p_at_gen = await _get_or_create_prompt(svc_at, "Calidad Atención General", "Evaluación de calidad y resolución en atención al cliente.")
    v_at_gen = await _get_or_create_version(p_at_gen)
    c_at_gen = await _ensure_criteria(p_at_gen, CRITERIA_ATENCION_GENERAL)
    struct_at_gen = {
        "service": svc_at,
        "prompt": p_at_gen,
        "version": v_at_gen,
        "criteria": c_at_gen,
        "criteria_defs": CRITERIA_ATENCION_GENERAL,
    }

    # 1.2 Gestión Reclamaciones
    p_at_rec = await _get_or_create_prompt(svc_at, "Gestión Reclamaciones", "Evaluación de tratamiento de reclamaciones, conflictos y cobros.")
    v_at_rec = await _get_or_create_version(p_at_rec)
    c_at_rec = await _ensure_criteria(p_at_rec, CRITERIA_GESTION_RECLAMACIONES)
    struct_at_rec = {
        "service": svc_at,
        "prompt": p_at_rec,
        "version": v_at_rec,
        "criteria": c_at_rec,
        "criteria_defs": CRITERIA_GESTION_RECLAMACIONES,
    }

    # 2.1 Venta Consultiva
    p_vn_con = await _get_or_create_prompt(svc_vn, "Venta Consultiva", "Evaluación de técnicas de venta consultiva, captación y valor.")
    v_vn_con = await _get_or_create_version(p_vn_con)
    c_vn_con = await _ensure_criteria(p_vn_con, CRITERIA_VENTA_CONSULTIVA)
    struct_vn_con = {
        "service": svc_vn,
        "prompt": p_vn_con,
        "version": v_vn_con,
        "criteria": c_vn_con,
        "criteria_defs": CRITERIA_VENTA_CONSULTIVA,
    }

    # 2.2 Retención / Renovación
    p_vn_ret = await _get_or_create_prompt(svc_vn, "Retención/Renovación", "Evaluación de salvamento de clientes, renovaciones y retención de bajas.")
    v_vn_ret = await _get_or_create_version(p_vn_ret)
    c_vn_ret = await _ensure_criteria(p_vn_ret, CRITERIA_RETENCION_RENOVACION)
    struct_vn_ret = {
        "service": svc_vn,
        "prompt": p_vn_ret,
        "version": v_vn_ret,
        "criteria": c_vn_ret,
        "criteria_defs": CRITERIA_RETENCION_RENOVACION,
    }

    by_typology = {
        "consulta_general": struct_at_gen,
        "soporte_tecnico": struct_at_gen,
        "reclamacion_incidencia": struct_at_rec,
        "facturacion_cobros": struct_at_rec,
        "captacion_nuevo": struct_vn_con,
        "upselling_cross": struct_vn_con,
        "renovacion": struct_vn_ret,
        "retencion_baja": struct_vn_ret,
    }

    return {
        "structures": {
            "atencion_general": struct_at_gen,
            "gestion_reclamaciones": struct_at_rec,
            "venta_consultiva": struct_vn_con,
            "retencion_renovacion": struct_vn_ret,
        },
        "by_typology": by_typology,
        "services": {
            "atencion": svc_at,
            "ventas": svc_vn,
        },
        "typologies": {
            **typo_map_at,
            **typo_map_vn,
        },
    }


def build_agent_profile(agent_index: int, team_name: str) -> Dict[str, Any]:
    """
    Assign performance archetype, strengths, weaknesses, and temporal parameters.
    
    Archetypes:
    - top_performer (10 agents): High performance (8.5 - 9.3), stable or slight positive drift.
    - stable_performer (24 agents): Constant reliable performance (7.2 - 7.8).
    - improving (14 agents): Strong learning curve, ascends from ~5.5 to ~8.6.
    - struggling (8 agents): Declining performance in recent weeks (drops from ~7.1 to ~4.5), alarms.
    - new_hire (4 agents): Active only in the last ~22 days, fewer calls (18 calls).
    """
    is_ventas = agent_index > 30

    if "Front" in team_name:
        team_offset = 0.25
    elif "Backoffice" in team_name:
        team_offset = 0.0
    elif "Comercial" in team_name:
        team_offset = 0.15
    else:  # Retención
        team_offset = -0.35

    # Top Performers (10 agents): 1, 2, 11, 12, 13, 31, 32, 41, 42, 43
    if agent_index in (1, 2, 11, 12, 13, 31, 32, 41, 42, 43):
        archetype = "top_performer"
        call_count = 63
        if not is_ventas:
            strengths = ["escucha_activa", "resolucion_consulta", "conocimiento_catalogo"]
            weaknesses = ["despedida_protocolo"]
        else:
            strengths = ["sondeo_necesidades", "argumentacion_valor", "negociacion_oferta"]
            weaknesses = ["transparencia_condiciones"]

    # Stable Performers (24 agents):
    # Front: 3, 4, 5, 6, 7 (5)
    # Backoffice: 14, 15, 16, 17, 18, 19, 20 (7)
    # Comercial: 33, 34, 35, 36 (4)
    # Retención: 44, 45, 46, 47, 48, 49, 50, 51 (8)
    elif agent_index in (3, 4, 5, 6, 7, 14, 15, 16, 17, 18, 19, 20, 33, 34, 35, 36, 44, 45, 46, 47, 48, 49, 50, 51):
        archetype = "stable_performer"
        call_count = 63
        if not is_ventas:
            strengths = ["cordialidad_empatia"]
            weaknesses = ["gestion_frustracion"]
        else:
            strengths = ["energia_comercial"]
            weaknesses = ["manejo_objeciones"]

    # Improving Agents (14 agents):
    # Front: 8, 9 (2)
    # Backoffice: 21, 22, 23, 24, 25 (5)
    # Comercial: 37, 38 (2)
    # Retención: 52, 53, 54, 55, 56 (5)
    elif agent_index in (8, 9, 21, 22, 23, 24, 25, 37, 38, 52, 53, 54, 55, 56):
        archetype = "improving"
        call_count = 63
        if not is_ventas:
            strengths = ["resolucion_consulta", "analisis_conflicto"]
            weaknesses = ["conocimiento_catalogo", "compromiso_tiempos"]
        else:
            strengths = ["argumentacion_valor", "refuerzo_confianza"]
            weaknesses = ["tecnicas_cierre", "diagnostico_motivo"]

    # Struggling Agents (8 agents):
    # Backoffice: 26, 27, 28 (3)
    # Comercial: 39, 40 (2)
    # Retención: 57, 58, 59 (3)
    elif agent_index in (26, 27, 28, 39, 40, 57, 58, 59):
        archetype = "struggling"
        call_count = 63
        if not is_ventas:
            strengths = ["saludo_identificacion"]
            weaknesses = ["gestion_frustracion", "propuesta_solucion"]
        else:
            strengths = ["energia_comercial"]
            weaknesses = ["manejo_objeciones", "negociacion_oferta"]

    # New Hires (4 agents): 10, 29, 30, 60
    else:
        archetype = "new_hire"
        call_count = 18
        if not is_ventas:
            strengths = ["saludo_identificacion", "cordialidad_empatia"]
            weaknesses = ["conocimiento_catalogo", "gestion_frustracion"]
        else:
            strengths = ["energia_comercial"]
            weaknesses = ["tecnicas_cierre", "manejo_objeciones"]

    return {
        "index": agent_index,
        "team_name": team_name,
        "team_offset": team_offset,
        "archetype": archetype,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "call_count": call_count,
        "min_day_offset": 68 if archetype == "new_hire" else 0,
        "is_ventas": is_ventas,
    }


def compute_call_evaluation(
    profile: Dict[str, Any],
    day_offset: int,
    chosen_typo: Typology,
    struct: Dict[str, Any],
    rng: random.Random,
    total_days: int = 90,
) -> Dict[str, Any]:
    """
    Calculate derived, realistic call metrics combining agent baseline, difficulty, sentiment, and criteria weights.
    """
    tkey = chosen_typo.typology_key
    crit_defs = struct["criteria_defs"]

    # 1. Call Context
    # Dificultad
    if tkey in ("reclamacion_incidencia", "retencion_baja"):
        diff = rng.choices(["baja", "media", "alta", "critica"], weights=[10, 35, 40, 15])[0]
    elif tkey in ("facturacion_cobros", "upselling_cross"):
        diff = rng.choices(["baja", "media", "alta", "critica"], weights=[20, 55, 20, 5])[0]
    else:
        diff = rng.choices(["baja", "media", "alta", "critica"], weights=[40, 50, 8, 2])[0]

    # Sentimiento
    if diff == "critica":
        sentiment = "negativo"
    elif diff == "alta":
        sentiment = rng.choices(["positivo", "neutro", "negativo"], weights=[5, 30, 65])[0]
    elif diff == "baja":
        sentiment = rng.choices(["positivo", "neutro", "negativo"], weights=[65, 30, 5])[0]
    else:
        sentiment = rng.choices(["positivo", "neutro", "negativo"], weights=[30, 55, 15])[0]

    # Duración
    base_durations = {
        "consulta_general": 160,
        "soporte_tecnico": 320,
        "reclamacion_incidencia": 440,
        "facturacion_cobros": 300,
        "captacion_nuevo": 380,
        "upselling_cross": 240,
        "renovacion": 330,
        "retencion_baja": 480,
    }
    diff_multipliers = {"baja": 0.85, "media": 1.0, "alta": 1.35, "critica": 1.70}
    raw_dur = base_durations.get(tkey, 250) * diff_multipliers[diff] + rng.gauss(0, 30)
    duracion = max(75, min(750, int(raw_dur)))

    # 3% non-evaluable calls
    if rng.random() < 0.03:
        reason = rng.choice([
            "Audio mudo durante más del 80% de la grabación",
            "Llamada cortada en los primeros 10 segundos",
            "Ruido severo de línea que imposibilita la comprensión",
            "Interferencia técnica de red en el canal de audio",
        ])
        items_json_list = []
        result_json_dict = {
            "evaluacion_global": 0.0,
            "alarma": False,
            "tipo_llamada": chosen_typo.typology_name,
            "dificultad": diff,
            "sentimiento": sentiment,
            "resultado": "no_evaluable",
            "duracion_segundos": duracion,
            "resumen": f"Llamada no evaluable debido a: {reason}.",
        }
        for cdef in crit_defs:
            ck = cdef["criterion_key"]
            result_json_dict[ck] = {"score": 0.0, "feedback": f"No evaluable: {reason}."}
            items_json_list.append({
                "criterion_key": ck,
                "output_key": ck,
                "name": cdef["criterion_name"],
                "type": "score_1_10",
                "value": 0.0,
                "weight": float(cdef["weight"]),
                "feedback": f"No evaluable: {reason}.",
                "not_applicable": True,
            })
        items_json_list.append({
            "criterion_key": "alarma",
            "output_key": "alarma",
            "name": "Alarma de Calidad",
            "type": "boolean",
            "value": False,
            "boolean_value": False,
            "feedback": "Llamada descartada para evaluación de calidad.",
            "not_applicable": True,
        })
        return {
            "is_evaluable": False,
            "non_evaluable_reason": reason,
            "dificultad": diff,
            "sentimiento": sentiment,
            "duracion_segundos": duracion,
            "evaluacion_global": Decimal("0.00"),
            "alarma": False,
            "items_json": items_json_list,
            "result_json": result_json_dict,
            "criteria_scores": {cdef["criterion_key"]: Decimal("0.00") for cdef in crit_defs},
            "resultado_llamada": "no_evaluable",
            "resumen": f"Llamada descartada: {reason}.",
        }

    # 2. Agent Temporal Baseline
    progress = day_offset / max(1.0, float(total_days - 1))
    arch = profile["archetype"]
    t_offset = profile["team_offset"]

    if arch == "top_performer":
        base_skill = 8.85 + 0.25 * progress + t_offset
    elif arch == "stable_performer":
        base_skill = 7.45 + t_offset
    elif arch == "improving":
        base_skill = 5.50 + 3.10 * progress + t_offset
    elif arch == "struggling":
        if progress < 0.55:
            base_skill = 7.10 + t_offset
        else:
            base_skill = 7.10 - 2.90 * ((progress - 0.55) / 0.45) + t_offset
    else:  # new_hire
        nh_p = max(0.0, min(1.0, (day_offset - 68) / 21.0))
        base_skill = 5.85 + 1.25 * nh_p + t_offset

    # 3. Call Condition Modifiers
    diff_mods = {"baja": 0.35, "media": 0.0, "alta": -0.55, "critica": -1.20}
    sent_mods = {"positivo": 0.25, "neutro": 0.0, "negativo": -0.45}
    call_noise = rng.gauss(0, 0.32)

    mod_context = diff_mods[diff] + sent_mods[sentiment] + call_noise

    # 4. Criteria Evaluation
    crit_scores: Dict[str, float] = {}
    items_json_list = []
    result_json_dict = {
        "tipo_llamada": chosen_typo.typology_name,
        "dificultad": diff,
        "sentimiento": sentiment,
        "duracion_segundos": duracion,
    }
    weighted_sum = 0.0

    for cdef in crit_defs:
        ck = cdef["criterion_key"]
        weight = float(cdef["weight"])

        # Affinity (strengths & weaknesses)
        affinity = 0.0
        if ck in profile["strengths"]:
            affinity += 0.95
        elif ck in profile["weaknesses"]:
            punish_mult = 1.40 if diff in ("alta", "critica") else 1.0
            affinity -= 1.80 * punish_mult

        jitter = rng.gauss(0, 0.22)
        raw_s = base_skill + mod_context + affinity + jitter
        score = max(1.0, min(10.0, round(raw_s, 1)))
        crit_scores[ck] = score
        weighted_sum += score * weight

        if score >= 8.0:
            feed = cdef["feedback_high"]
        elif score <= 4.5:
            feed = cdef["feedback_low"]
        else:
            feed = cdef["feedback_mid"]

        result_json_dict[ck] = {
            "score": score,
            "weight": weight,
            "feedback": feed,
        }
        items_json_list.append({
            "criterion_key": ck,
            "output_key": ck,
            "name": cdef["criterion_name"],
            "type": "score_1_10",
            "value": score,
            "weight": weight,
            "feedback": feed,
            "not_applicable": False,
        })

    eval_global = round(weighted_sum, 2)
    clamped_global = Decimal(f"{max(1.0, min(10.0, eval_global)):.2f}")

    # 5. Alarms
    has_alarm = (
        float(clamped_global) < 5.0
        or any(crit_scores[cdef["criterion_key"]] <= 3.5 for cdef in crit_defs if float(cdef["weight"]) >= 0.20)
        or (diff == "critica" and float(clamped_global) < 5.4)
    )

    items_json_list.append({
        "criterion_key": "alarma",
        "output_key": "alarma",
        "name": "Alarma de Calidad",
        "type": "boolean",
        "value": has_alarm,
        "boolean_value": has_alarm,
        "feedback": "Desviación crítica de calidad detectada en la llamada." if has_alarm else "Sin anomalías críticas.",
        "not_applicable": False,
    })

    # 6. Call Outcome
    if float(clamped_global) >= 7.5:
        if profile["is_ventas"]:
            outcome = "venta_cerrada_exitosa" if tkey == "captacion_nuevo" else "acuerdo_fidelizado"
        else:
            outcome = "incidencia_resuelta_definitiva" if tkey != "reclamacion_incidencia" else "acuerdo_reclamacion_satisfactorio"
    elif float(clamped_global) >= 5.0:
        if profile["is_ventas"]:
            outcome = "seguimiento_comercial_agendado" if tkey == "captacion_nuevo" else "propuesta_en_estudio"
        else:
            outcome = "gestion_derivada_segundo_nivel" if tkey != "reclamacion_incidencia" else "reclamacion_en_tramite"
    else:
        if profile["is_ventas"]:
            outcome = "venta_rechazada" if tkey == "captacion_nuevo" else "baja_tramitada"
        else:
            outcome = "incidencia_no_resuelta" if tkey != "reclamacion_incidencia" else "reclamacion_escalada_defensor"

    resumen = (
        f"Llamada de {chosen_typo.typology_name} con dificultad {diff} y cliente con tono {sentiment}. "
        f"Resultado: {outcome.replace('_', ' ')}. Puntuación global: {clamped_global}."
    )

    result_json_dict["evaluacion_global"] = float(clamped_global)
    result_json_dict["alarma"] = has_alarm
    result_json_dict["resultado"] = outcome
    result_json_dict["resumen"] = resumen

    return {
        "is_evaluable": True,
        "non_evaluable_reason": None,
        "dificultad": diff,
        "sentimiento": sentiment,
        "duracion_segundos": duracion,
        "evaluacion_global": clamped_global,
        "alarma": has_alarm,
        "items_json": items_json_list,
        "result_json": result_json_dict,
        "criteria_scores": {ck: Decimal(f"{v:.2f}") for ck, v in crit_scores.items()},
        "resultado_llamada": outcome,
        "resumen": resumen,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Core Seeding Modules
# ─────────────────────────────────────────────────────────────────────────────

async def seed_analytics_data(db: AsyncSession, company: Company) -> Dict[str, Any]:
    """Generate 3,600 MassEvaluationResults and 21,600 CriteriaResults over 90 days."""
    cid = company.company_id

    # Check for existing evaluation results
    existing_cnt_res = await db.execute(
        select(func.count(MassEvaluationResult.mass_analysis_id)).where(
            MassEvaluationResult.company_id == cid
        )
    )
    existing_cnt = existing_cnt_res.scalar() or 0
    if existing_cnt > 0:
        raise DemoDataAlreadyExistsError(
            f"Company '{company.company_name}' already has {existing_cnt} MassEvaluationResult rows. "
            "Aborting to avoid duplicated evaluations. Run scripts/purge_company.py first if you wish to reset."
        )

    # 1. Structures
    struct_reg = await ensure_evaluation_structures(db, company)
    svc_at = struct_reg["services"]["atencion"]
    svc_vn = struct_reg["services"]["ventas"]
    by_typo = struct_reg["by_typology"]
    all_typos = struct_reg["typologies"]

    # 2. Fetch Agents & Teams
    agent_res = await db.execute(
        select(User).where(User.company_id == cid, User.role == "agent").order_by(User.user_id.asc())
    )
    agents = list(agent_res.scalars().all())
    if len(agents) != 60:
        raise DemoDataSeedError(f"Expected 60 agents in demo company, found {len(agents)}.")

    teams_res = await db.execute(select(Team).where(Team.company_id == cid))
    teams_map = {t.team_id: t for t in teams_res.scalars().all()}

    # 3. Create Jobs (1 per service)
    async def _get_or_create_job(svc: Service, prompt: Prompt, job_name: str) -> MassEvaluationJob:
        j_res = await db.execute(
            select(MassEvaluationJob).where(MassEvaluationJob.company_id == cid, MassEvaluationJob.service_id == svc.service_id)
        )
        job = j_res.scalars().first()
        if not job:
            job = MassEvaluationJob(
                company_id=cid,
                service_id=svc.service_id,
                prompt_id=prompt.prompt_id,
                job_name=job_name,
                job_mode="standard",
                selection_mode="filter",
                is_active=True,
            )
            db.add(job)
            await db.flush()
        return job

    prompt_job_at = struct_reg["structures"]["atencion_general"]["prompt"]
    prompt_job_vn = struct_reg["structures"]["venta_consultiva"]["prompt"]
    job_at = await _get_or_create_job(svc_at, prompt_job_at, "Auditoría Histórica - Atención al Cliente")
    job_vn = await _get_or_create_job(svc_vn, prompt_job_vn, "Auditoría Histórica - Ventas y Retención")

    # 4. Create 26 Weekly Runs (13 weeks x 2 services)
    now_utc = datetime.now(timezone.utc)
    start_date = now_utc - timedelta(days=DAYS_HISTORY)

    runs_at: List[Tuple[datetime, datetime, int]] = []
    runs_vn: List[Tuple[datetime, datetime, int]] = []

    for week in range(13):
        w_start = start_date + timedelta(days=week * 7)
        w_end = min(now_utc, w_start + timedelta(days=6, hours=23, minutes=59))

        run_obj_at = MassEvaluationRun(
            job_id=job_at.job_id,
            company_id=cid,
            service_id=svc_at.service_id,
            trigger_type="scheduled",
            status="completed",
            started_at=w_start,
            finished_at=w_end,
            calls_found=0,
            calls_analyzed=0,
        )
        run_obj_vn = MassEvaluationRun(
            job_id=job_vn.job_id,
            company_id=cid,
            service_id=svc_vn.service_id,
            trigger_type="scheduled",
            status="completed",
            started_at=w_start,
            finished_at=w_end,
            calls_found=0,
            calls_analyzed=0,
        )
        db.add_all([run_obj_at, run_obj_vn])
        await db.flush()
        runs_at.append((w_start, w_end, run_obj_at.run_id))
        runs_vn.append((w_start, w_end, run_obj_vn.run_id))

    def _match_run(call_dt: datetime, is_ventas: bool) -> int:
        run_list = runs_vn if is_ventas else runs_at
        for s, e, rid in run_list:
            if s <= call_dt <= e:
                return rid
        return run_list[-1][2]

    # 5. Build Agent Profiles
    profiles = {}
    for idx, agent in enumerate(agents, start=1):
        team = teams_map.get(agent.primary_team_id)
        team_name = team.team_name if team else "Equipo Demo"
        profiles[agent.hubspot_owner_id] = build_agent_profile(idx, team_name)

    # 6. Generate Exactly 3,600 Calls
    rng = random.Random(42)  # Deterministic seed for reproducible demo realism
    call_records: List[Dict[str, Any]] = []
    call_seq = 1

    # Typologies by team
    front_typos = [all_typos["consulta_general"], all_typos["soporte_tecnico"], all_typos["reclamacion_incidencia"]]
    front_weights = [55, 35, 10]

    back_typos = [all_typos["reclamacion_incidencia"], all_typos["facturacion_cobros"], all_typos["soporte_tecnico"], all_typos["consulta_general"]]
    back_weights = [45, 35, 12, 8]

    comercial_typos = [all_typos["captacion_nuevo"], all_typos["upselling_cross"], all_typos["renovacion"]]
    comercial_weights = [60, 30, 10]

    retencion_typos = [all_typos["retencion_baja"], all_typos["renovacion"], all_typos["upselling_cross"], all_typos["captacion_nuevo"]]
    retencion_weights = [50, 35, 10, 5]

    for agent in agents:
        owner_id = agent.hubspot_owner_id
        prof = profiles[owner_id]
        is_ventas = prof["is_ventas"]
        target_calls = prof["call_count"]
        min_day_offset = prof["min_day_offset"]

        for _ in range(target_calls):
            day_offset = rng.randint(min_day_offset, DAYS_HISTORY - 1)
            call_date = start_date + timedelta(days=day_offset)

            # Office hours (07:00 - 17:30 UTC ~ 09:00 - 19:30 Europe/Madrid)
            hour = rng.randint(7, 17)
            minute = rng.randint(0, 59)
            second = rng.randint(0, 59)
            call_ts = call_date.replace(hour=hour, minute=minute, second=second)

            # Select typology based on team
            if "Front" in prof["team_name"]:
                chosen_typo = rng.choices(front_typos, weights=front_weights)[0]
            elif "Backoffice" in prof["team_name"]:
                chosen_typo = rng.choices(back_typos, weights=back_weights)[0]
            elif "Comercial" in prof["team_name"]:
                chosen_typo = rng.choices(comercial_typos, weights=comercial_weights)[0]
            else:  # Retención
                chosen_typo = rng.choices(retencion_typos, weights=retencion_weights)[0]

            # Select evaluation structure mapped to this typology
            struct = by_typo[chosen_typo.typology_key]

            # Compute evaluation
            eval_data = compute_call_evaluation(prof, day_offset, chosen_typo, struct, rng, DAYS_HISTORY)

            # Directions
            if is_ventas:
                direction = "outbound" if rng.random() < 0.78 else "inbound"
            else:
                direction = "inbound" if rng.random() < 0.85 else "outbound"

            call_id = f"demo_call_{call_ts.strftime('%Y%m%d')}_{call_seq:04d}"
            call_seq += 1
            run_id = _match_run(call_ts, is_ventas)

            call_records.append({
                "run_id": run_id,
                "job_id": job_vn.job_id if is_ventas else job_at.job_id,
                "company_id": cid,
                "service_id": struct["service"].service_id,
                "service_key": struct["service"].service_key,
                "service_name": struct["service"].service_name,
                "execution_source": "on_demand",
                "call_id": call_id,
                "hs_object_id": f"hs_{call_id}",
                "recording_url": f"https://demo.doobot.ai/recordings/{call_id}.mp3",
                "hubspot_owner_id": owner_id,
                "agent_name": agent.name,
                "call_timestamp": call_ts,
                "analysis_timestamp": call_ts,
                "created_at": call_ts,  # Critical: Must match call_timestamp for date filters
                "call_duration_seconds": eval_data["duracion_segundos"],
                "direction": direction,
                "prompt_id": struct["prompt"].prompt_id,
                "prompt_version_id": struct["version"].id,
                "prompt_name": struct["prompt"].prompt_name,
                "prompt_version_name": struct["version"].version_name,
                "prompt_version_label": struct["version"].version_label,
                "prompt_snapshot": struct["version"].prompt,
                "typology_id": chosen_typo.typology_id,
                "typology_key": chosen_typo.typology_key,
                "typology_name": chosen_typo.typology_name,
                "status": "completed",
                "is_evaluable": eval_data["is_evaluable"],
                "non_evaluable_reason": eval_data["non_evaluable_reason"],
                "result_json": eval_data["result_json"],
                "items_json": eval_data["items_json"],
                "evaluacion_global": eval_data["evaluacion_global"],
                "_struct": struct,
                "_eval_data": eval_data,
            })

    # Pad or trim to exactly TARGET_EVALUATIONS if needed
    if len(call_records) > TARGET_EVALUATIONS:
        call_records = call_records[:TARGET_EVALUATIONS]

    logger.info("Generated %d synthetic call records in memory. Inserting in batches...", len(call_records))

    # 7. Bulk insert MassEvaluationResult in batches of 600
    batch_size = 600
    mass_analysis_id_map: Dict[str, int] = {}

    for i in range(0, len(call_records), batch_size):
        chunk = call_records[i : i + batch_size]
        insert_chunk = [{k: v for k, v in row.items() if not k.startswith("_")} for row in chunk]

        stmt = (
            insert(MassEvaluationResult)
            .values(insert_chunk)
            .returning(MassEvaluationResult.mass_analysis_id, MassEvaluationResult.call_id)
        )
        res = await db.execute(stmt)
        for ma_id, cid_str in res.all():
            mass_analysis_id_map[cid_str] = ma_id

    logger.info("Successfully inserted %d MassEvaluationResult rows.", len(mass_analysis_id_map))

    # 8. Build & bulk insert MassEvaluationCriterionResult (6 criteria x 3,600 calls = 21,600 rows)
    criteria_records: List[Dict[str, Any]] = []
    for call_data in call_records:
        cid_str = call_data["call_id"]
        ma_id = mass_analysis_id_map[cid_str]
        struct = call_data["_struct"]
        eval_data = call_data["_eval_data"]
        crit_defs = struct["criteria_defs"]
        is_eval = call_data["is_evaluable"]

        for cdef in crit_defs:
            ckey = cdef["criterion_key"]
            crit_obj = struct["criteria"][ckey]
            score_data = eval_data["result_json"].get(ckey, {})
            c_score = Decimal(str(score_data.get("score", 0.0)))
            c_feed = score_data.get("feedback", "")

            criteria_records.append({
                "mass_analysis_id": ma_id,
                "run_id": call_data["run_id"],
                "job_id": call_data["job_id"],
                "prompt_id": struct["prompt"].prompt_id,
                "prompt_version_id": struct["version"].id,
                "criterion_id": crit_obj.criterion_id,
                "criterion_key": ckey,
                "criterion_name": cdef["criterion_name"],
                "criterion_type": "score",
                "call_id": cid_str,
                "numeric_value": c_score if is_eval else Decimal("0.0"),
                "percentage_value": c_score * Decimal("10.0") if is_eval else Decimal("0.0"),
                "boolean_value": c_score >= Decimal("5.0") if is_eval else False,
                "feedback": c_feed,
                "feed_key": ckey,
                "is_applicable": is_eval,
                "not_applicable": not is_eval,
                "service_id": call_data["service_id"],
                "service_key": call_data["service_key"],
                "service_name": call_data["service_name"],
                "typology_id": call_data["typology_id"],
                "typology_key": call_data["typology_key"],
                "typology_name": call_data["typology_name"],
            })

    # Bulk insert criteria in batches of 1,200
    crit_batch_size = 1200
    for i in range(0, len(criteria_records), crit_batch_size):
        chunk = criteria_records[i : i + crit_batch_size]
        stmt = insert(MassEvaluationCriterionResult).values(chunk)
        await db.execute(stmt)

    logger.info("Successfully inserted %d MassEvaluationCriterionResult rows.", len(criteria_records))

    return {
        "calls_count": len(call_records),
        "criteria_count": len(criteria_records),
        "jobs_count": 2,
        "runs_count": len(runs_at) + len(runs_vn),
    }


async def seed_training_cycles(db: AsyncSession, company: Company) -> Dict[str, Any]:
    """Generate 2 Training Runs and 32 TrainingAgentReports (20 completed, 12 in progress)."""
    cid = company.company_id

    # Check for existing reports
    rep_cnt_res = await db.execute(
        select(func.count(TrainingAgentReport.training_report_id)).where(
            TrainingAgentReport.company_id == cid
        )
    )
    if (rep_cnt_res.scalar() or 0) > 0:
        logger.info("Training cycles already exist for company_id=%s, skipping cycles seed.", cid)
        return {"reports_count": 0}

    svc_res = await db.execute(select(Service).where(Service.company_id == cid))
    services = {s.service_key: s for s in svc_res.scalars().all()}
    svc_at = services.get("atencion-al-cliente")
    svc_vn = services.get("ventas")

    now_utc = datetime.now(timezone.utc)

    # 1. Create 2 Runs
    # Run 1: Completed 45 days ago
    run1 = TrainingRun(
        company_id=cid,
        service_id=svc_at.service_id if svc_at else None,
        period_start=now_utc - timedelta(days=60),
        period_end=now_utc - timedelta(days=45),
        status="completed",
        triggered_by="scheduler",
        agents_total=20,
        agents_completed=20,
        started_at=now_utc - timedelta(days=60),
        finished_at=now_utc - timedelta(days=45),
    )
    # Run 2: Active / Running 10 days ago
    run2 = TrainingRun(
        company_id=cid,
        service_id=svc_vn.service_id if svc_vn else None,
        period_start=now_utc - timedelta(days=20),
        period_end=now_utc - timedelta(days=5),
        status="running",
        triggered_by="scheduler",
        agents_total=12,
        agents_completed=0,
        started_at=now_utc - timedelta(days=20),
    )
    db.add_all([run1, run2])
    await db.flush()

    # Fetch agents
    agent_res = await db.execute(
        select(User).where(User.company_id == cid, User.role == "agent").order_by(User.user_id.asc())
    )
    agents = list(agent_res.scalars().all())

    # Assign 20 agents from improving/stable to Run 1 (completed)
    # and the 12 struggling/new-hire agents to Run 2 (active cycle)
    struggling_or_new = [
        a for a in agents
        if int(a.hubspot_owner_id.split("_")[-1]) in (10, 26, 27, 28, 29, 30, 39, 40, 57, 58, 59, 60)
    ]
    other_agents = [a for a in agents if a not in struggling_or_new]
    agents_run1 = other_agents[:20]
    agents_run2 = struggling_or_new

    reports_created = []

    # Run 1 reports (completed)
    for agent in agents_run1:
        report = TrainingAgentReport(
            training_run_id=run1.training_run_id,
            company_id=cid,
            service_id=agent.primary_service_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            agent_name=agent.name,
            agent_initials=agent.agent_initials or "AD",
            period_start=run1.period_start,
            period_end=run1.period_end,
            status="completed",
            cycle_mode="automatic",
            evaluations_count=12,
            calls_count=12,
            avg_evaluacion_global=Decimal("8.15"),
            summary_general=(
                f"Informe de consolidación de ciclo para {agent.name}. "
                "Evolución positiva con consolidación en protocolos de atención y escucha activa."
            ),
            strengths_json={
                "fortalezas": [
                    "Excelente empatía y validación del problema del usuario.",
                    "Disminución drástica de interrupciones en llamada.",
                ]
            },
            weaknesses_json={
                "puntos_mejora": [
                    "Afianzar las tentativas de cierre temprano.",
                    "Sintetizar la explicación de condiciones contractuales.",
                ]
            },
            general_objectives_json={
                "objetivos": [
                    {"titulo": "Perfeccionamiento del cierre", "meta": "Superar 8.0 en cierre de llamada"}
                ]
            },
            final_report_json={
                "conclusiones": "Objetivos alcanzados con éxito. Se recomienda mantener el protocolo establecido."
            },
            is_current=True,
            generated_at=run1.period_end,
            approved_at=run1.period_end,
        )
        db.add(report)
        await db.flush()

        # Simulation Prompts
        p1 = TrainingSimulationPrompt(
            training_report_id=report.training_report_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            prompt_number=1,
            title="Manejo de Reclamación con Cliente Insatisfecho",
            scenario_type="roleplay",
            prompt_text="Simula una llamada con un cliente que ha recibido una factura con cargo duplicado.",
        )
        p2 = TrainingSimulationPrompt(
            training_report_id=report.training_report_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            prompt_number=2,
            title="Resolución Técnica y Despedida Cordial",
            scenario_type="roleplay",
            prompt_text="Simula el cierre de una incidencia técnica asegurando la satisfacción del cliente.",
        )
        db.add_all([p1, p2])
        await db.flush()

        c1 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=p1.simulation_prompt_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            status="completed",
            completed_at=run1.period_end,
            notes="Completado con buena soltura.",
        )
        c2 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=p2.simulation_prompt_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            status="completed",
            completed_at=run1.period_end,
            notes="Completado satisfactoriamente.",
        )
        db.add_all([c1, c2])
        reports_created.append(report)

    # Run 2 reports (active / in_progress)
    for agent in agents_run2:
        report = TrainingAgentReport(
            training_run_id=run2.training_run_id,
            company_id=cid,
            service_id=agent.primary_service_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            agent_name=agent.name,
            agent_initials=agent.agent_initials or "AD",
            period_start=run2.period_start,
            period_end=run2.period_end,
            status="in_progress",
            cycle_mode="automatic",
            evaluations_count=8,
            calls_count=8,
            avg_evaluacion_global=Decimal("7.40"),
            summary_general=(
                f"Ciclo activo de mejora para {agent.name}. "
                "Enfocado en superación de objeciones comerciales y agilidad en consulta."
            ),
            strengths_json={
                "fortalezas": [
                    "Claridad en la exposición de tarifas y promociones.",
                ]
            },
            weaknesses_json={
                "puntos_mejora": [
                    "Aumentar el número de preguntas de sondeo.",
                    "Manejo de objeción sobre precio de la competencia.",
                ]
            },
            general_objectives_json={
                "objetivos": [
                    {"titulo": "Superación de objeciones", "meta": "Alcanzar 7.5 en argumentación"}
                ]
            },
            is_current=True,
            generated_at=run2.period_start + timedelta(days=2),
        )
        db.add(report)
        await db.flush()

        p1 = TrainingSimulationPrompt(
            training_report_id=report.training_report_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            prompt_number=1,
            title="Superación de Objeción de Precio en Venta B2B",
            scenario_type="roleplay",
            prompt_text="El cliente afirma que el precio del competidor es un 15% más barato.",
        )
        db.add(p1)
        await db.flush()

        c1 = TrainingCompletionStatus(
            training_report_id=report.training_report_id,
            simulation_prompt_id=p1.simulation_prompt_id,
            hubspot_owner_id=agent.hubspot_owner_id,
            status="pending",
        )
        db.add(c1)
        reports_created.append(report)

    logger.info("Successfully seeded %d TrainingAgentReports across 2 runs.", len(reports_created))
    return {
        "training_runs_count": 2,
        "reports_count": len(reports_created),
        "completed_reports": len(agents_run1),
        "active_reports": len(agents_run2),
    }


async def seed_trainer_data(db: AsyncSession, company: Company) -> Dict[str, Any]:
    """Generate 2 TrainerEvaluationConfigs, 4 Simulations, 50 Sessions & Evaluations."""
    cid = company.company_id

    # Check for existing sessions
    sess_cnt_res = await db.execute(
        select(func.count(TrainerSession.session_id)).where(TrainerSession.company_id == cid)
    )
    if (sess_cnt_res.scalar() or 0) > 0:
        logger.info("Trainer data already exists for company_id=%s, skipping trainer seed.", cid)
        return {"sessions_count": 0}

    struct_reg = await ensure_evaluation_structures(db, company)
    svc_at = struct_reg["services"]["atencion"]
    svc_vn = struct_reg["services"]["ventas"]
    prompt_at = struct_reg["structures"]["atencion_general"]["prompt"]
    prompt_vn = struct_reg["structures"]["venta_consultiva"]["prompt"]

    # 1. Configs
    cfg_at = TrainerEvaluationConfig(
        company_id=cid,
        service_id=svc_at.service_id,
        speech_structure_id=prompt_at.prompt_id,
        name="Configuración Evaluación Trainer - Atención",
        is_active=True,
    )
    cfg_vn = TrainerEvaluationConfig(
        company_id=cid,
        service_id=svc_vn.service_id,
        speech_structure_id=prompt_vn.prompt_id,
        name="Configuración Evaluación Trainer - Ventas",
        is_active=True,
    )
    db.add_all([cfg_at, cfg_vn])
    await db.flush()

    # 2. Simulations
    sims_def = [
        (svc_at.service_id, cfg_at.config_id, "Gestión de Reclamación de Facturación Compleja", "SIM-DEMO-AT01", "Simula un cliente que ha recibido una factura con cobros no reconocidos y exige devolución inmediata."),
        (svc_at.service_id, cfg_at.config_id, "Protocolo de Desescalada con Cliente Furioso", "SIM-DEMO-AT02", "Simula un cliente muy molesto que eleva el tono de voz; el objetivo es desescalar sin perder la compostura."),
        (svc_vn.service_id, cfg_vn.config_id, "Prospección Comercial y Detección de Interés", "SIM-DEMO-VN01", "Simula una llamada saliente para captar a un cliente potencial y despertar interés en los nuevos planes."),
        (svc_vn.service_id, cfg_vn.config_id, "Superación de Objeción de Precio y Cierre", "SIM-DEMO-VN02", "Simula una negociación donde el cliente presiona por descuentos adicionales antes de firmar."),
    ]

    sim_objects = []
    for s_id, c_id, name, code, prompt_txt in sims_def:
        sim = TrainerSimulation(
            company_id=cid,
            service_id=s_id,
            evaluation_config_id=c_id,
            name=name,
            code=code,
            status="published",
            roleplay_prompt=prompt_txt,
        )
        db.add(sim)
        await db.flush()

        ver = TrainerSimulationVersion(
            simulation_id=sim.simulation_id,
            version_number=1,
            roleplay_prompt_snapshot=prompt_txt,
            evaluation_config_snapshot={"config_id": c_id, "name": name},
            service_id=s_id,
            evaluation_config_id=c_id,
        )
        db.add(ver)
        await db.flush()
        sim_objects.append((sim, ver))

    # 3. Create 50 Sessions and Evaluations
    agent_res = await db.execute(
        select(User).where(User.company_id == cid, User.role == "agent").limit(25)
    )
    agents_pool = list(agent_res.scalars().all())

    now_utc = datetime.now(timezone.utc)
    sessions_created = 0

    for i in range(1, 51):
        agent = agents_pool[i % len(agents_pool)]
        sim, ver = sim_objects[i % len(sim_objects)]
        session_date = now_utc - timedelta(days=random.randint(1, 30), hours=random.randint(1, 8))
        duration = random.randint(110, 340)

        session = TrainerSession(
            simulation_id=sim.simulation_id,
            simulation_version_id=ver.version_id,
            company_id=cid,
            service_id=sim.service_id,
            agent_id=agent.hubspot_owner_id,
            agent_code=agent.agent_initials or f"AG{i}",
            call_id=f"demo_trainer_session_{i:03d}",
            status="completed",
            evaluation_status="evaluated",
            duration_seconds=duration,
            transcript=(
                "Agente: Bienvenido al servicio de atención, ¿en qué puedo ayudarle?\n"
                "Cliente: Tengo una incidencia con mi última factura.\n"
                "Agente: Comprendo perfectamente, voy a revisar su expediente de inmediato..."
            ),
            started_at=session_date,
            ended_at=session_date + timedelta(seconds=duration),
        )
        db.add(session)
        await db.flush()

        score_val = round(random.uniform(6.5, 9.5), 2)
        evaluation = TrainerEvaluation(
            session_id=session.session_id,
            evaluation_config_id=sim.evaluation_config_id,
            prompt_snapshot=sim.roleplay_prompt,
            score=Decimal(str(score_val)),
            summary=f"Simulación '{sim.name}' completada con puntuación de {score_val}/10.",
            strengths={
                "items": [
                    "Excelente control del tono y escucha paciente.",
                    "Respuesta clara y estructurada a la inquietud planteada.",
                ]
            },
            improvement_points={
                "items": [
                    "Asegurar la confirmación expresa del cliente al finalizar la explicación.",
                ]
            },
            result_json={
                "empatia": round(score_val, 1),
                "claridad": round(min(10.0, score_val + 0.2), 1),
                "resolucion": round(max(5.0, score_val - 0.3), 1),
            },
        )
        db.add(evaluation)
        sessions_created += 1

    logger.info("Successfully seeded %d Trainer sessions and evaluations.", sessions_created)
    return {
        "configs_count": len(sims_def),
        "simulations_count": len(sim_objects),
        "sessions_count": sessions_created,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Master Seeder & CLI
# ─────────────────────────────────────────────────────────────────────────────

async def seed_demo_data(
    db: AsyncSession,
    company_id: Optional[int] = None,
    only: Optional[str] = None,
    validate: bool = False,
) -> Dict[str, Any]:
    """
    Main orchestration function for synthetic demo data generation.
    Must be executed inside an active transaction.
    """
    company = await get_target_company(db, company_id)
    cid = company.company_id
    logger.info("Seeding demo data for company: '%s' (id=%s)...", company.company_name, cid)

    results: Dict[str, Any] = {"company": {"id": cid, "name": company.company_name}}

    if only in (None, "analytics", "all"):
        results["analytics"] = await seed_analytics_data(db, company)

    if only in (None, "training", "all"):
        results["training"] = await seed_training_cycles(db, company)

    if only in (None, "trainer", "all"):
        results["trainer"] = await seed_trainer_data(db, company)

    if validate:
        # Run integrity assertions
        cnt_res = await db.execute(
            select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                MassEvaluationResult.company_id == cid
            )
        )
        total_evals = cnt_res.scalar() or 0
        if total_evals != TARGET_EVALUATIONS and only in (None, "analytics", "all"):
            raise DemoDataSeedError(f"Validation failed: expected {TARGET_EVALUATIONS} evaluations, got {total_evals}.")

        cnt_crit = await db.execute(
            select(func.count(MassEvaluationCriterionResult.id)).where(
                MassEvaluationCriterionResult.mass_analysis_id.in_(
                    select(MassEvaluationResult.mass_analysis_id).where(MassEvaluationResult.company_id == cid)
                )
            )
        )
        total_crit = cnt_crit.scalar() or 0
        expected_crit = TARGET_EVALUATIONS * 6
        if total_crit != expected_crit and only in (None, "analytics", "all"):
            raise DemoDataSeedError(f"Validation failed: expected {expected_crit} criteria, got {total_crit}.")

        results["validation"] = "passed"
        logger.info("Integrity assertions passed: %d evaluations, %d criteria.", total_evals, total_crit)

    return results


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate realistic synthetic demo data for Empresa Demo."
    )
    parser.add_argument("--company-id", type=int, default=None, help="Target demo company ID (defaults to finding 'empresa-demo')")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Roll back transaction without committing changes")
    parser.add_argument("--apply", action="store_true", default=False, help="Commit generated data to database")
    parser.add_argument("--only", choices=["analytics", "training", "trainer", "all"], default="all", help="Subset of demo data to seed")
    parser.add_argument("--validate", action="store_true", default=False, help="Run validation assertions after seeding")
    parser.add_argument("--db-url", type=str, default=None, help="Optional custom database URL")
    return parser.parse_args()


async def async_main():
    args = parse_args()
    apply_mode = bool(args.apply)

    settings = get_settings()
    db_url = args.db_url or os.environ.get("DATABASE_URL") or settings.database_url
    if not db_url:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    engine = create_async_engine(db_url, echo=False)

    print("=" * 80)
    print(f"DEMO DATA SEED TOOL - Mode: {'APPLY (COMMITTING DATA)' if apply_mode else 'DRY RUN (ROLLBACK)'}")
    print(f"Scope: {args.only}")
    print("=" * 80)

    try:
        async with AsyncSession(engine) as session:
            async with session.begin():
                summary = await seed_demo_data(
                    db=session,
                    company_id=args.company_id,
                    only=args.only,
                    validate=args.validate,
                )
                if not apply_mode:
                    await session.rollback()
                    print("\n[DRY-RUN] Data generation logic completed successfully. All changes rolled back.")
                else:
                    print("\n[APPLY] Demo data successfully committed to database!")

        print("-" * 80)
        comp = summary["company"]
        print(f"Company: {comp['name']} (id={comp['id']})")
        if "analytics" in summary:
            print("Analytics:")
            print(f"  - Evaluations: {summary['analytics']['calls_count']}")
            print(f"  - Criteria rows: {summary['analytics']['criteria_count']}")
            print(f"  - Jobs: {summary['analytics']['jobs_count']} | Runs: {summary['analytics']['runs_count']}")
        if "training" in summary:
            print("Improvement Cycles:")
            print(f"  - Reports: {summary['training']['reports_count']} (Completed: {summary['training'].get('completed_reports')}, Active: {summary['training'].get('active_reports')})")
        if "trainer" in summary:
            print("Trainer Module:")
            print(f"  - Simulations: {summary['trainer']['simulations_count']}")
            print(f"  - Sessions & Evals: {summary['trainer']['sessions_count']}")
        if summary.get("validation"):
            print(f"Validation: {summary['validation']}")
        print("=" * 80)

    except DemoDataAlreadyExistsError as e:
        print(f"\nABORTED: {e}")
        sys.exit(2)
    except Exception as e:
        logger.exception("Demo data seeding failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(async_main())
