"""Service class for managing Trainer simulations, versions, evaluations, and phone sessions."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional, Set, Dict
from sqlalchemy import select, and_, or_, desc, func, delete, Text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport
from app.models.users import User
from app.models.teams import UserServiceAssociation
from app.schemas.trainer import (
    TrainerEvaluationConfigCreate,
    TrainerEvaluationConfigUpdate,
    TrainerSimulationCreate,
    TrainerSimulationUpdate,
    AIPromptGenerateRequest,
    AIPromptImproveRequest,
)
from app.services import openai_service
from app.utils.json_utils import safe_parse_json

logger = logging.getLogger(__name__)


class TrainerService:

    # ── Evaluation Configs ────────────────────────────────────────────────────────

    @staticmethod
    async def create_evaluation_config(
        db: AsyncSession, payload: TrainerEvaluationConfigCreate, created_by: Optional[str] = None
    ) -> TrainerEvaluationConfig:
        # Validate that structure exists and belongs to the same service
        stmt = select(Prompt).where(Prompt.prompt_id == payload.speech_structure_id)
        res = await db.execute(stmt)
        prompt = res.scalars().first()
        if not prompt:
            raise ValueError(f"La estructura base de Speech ID {payload.speech_structure_id} no existe.")
        if prompt.service_id != payload.service_id:
            raise ValueError("La estructura base seleccionada no pertenece al mismo servicio.")

        # Resolve company_id from service
        from app.models.services import Service
        stmt_svc = select(Service.company_id).where(Service.service_id == payload.service_id)
        res_svc = await db.execute(stmt_svc)
        company_id = res_svc.scalar()

        config = TrainerEvaluationConfig(
            name=payload.name,
            service_id=payload.service_id,
            company_id=company_id,
            speech_structure_id=payload.speech_structure_id,
            extra_instructions=payload.extra_instructions,
            is_active=payload.is_active,
            created_by=created_by,
        )
        db.add(config)
        await db.commit()
        stmt_reload = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config.config_id)
        res_reload = await db.execute(stmt_reload)
        return res_reload.scalars().first()

    @staticmethod
    async def update_evaluation_config(
        db: AsyncSession, config_id: int, payload: TrainerEvaluationConfigUpdate
    ) -> Optional[TrainerEvaluationConfig]:
        stmt = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config_id)
        res = await db.execute(stmt)
        config = res.scalars().first()
        if not config:
            return None

        if payload.name is not None:
            config.name = payload.name
        if payload.extra_instructions is not None:
            config.extra_instructions = payload.extra_instructions
        if payload.is_active is not None:
            config.is_active = payload.is_active

        config.updated_at = datetime.now(timezone.utc)
        await db.commit()
        stmt_reload = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config.config_id)
        res_reload = await db.execute(stmt_reload)
        return res_reload.scalars().first()

    @staticmethod
    async def get_evaluation_config(db: AsyncSession, config_id: int) -> Optional[TrainerEvaluationConfig]:
        stmt = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def list_evaluation_configs(
        db: AsyncSession,
        service_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        company_ids: Optional[List[int]] = None,
        allowed_service_ids: Optional[List[int]] = None,
    ) -> List[TrainerEvaluationConfig]:
        stmt = select(TrainerEvaluationConfig)
        filters = []
        if service_id is not None:
            filters.append(TrainerEvaluationConfig.service_id == service_id)
        if allowed_service_ids is not None:
            filters.append(TrainerEvaluationConfig.service_id.in_(allowed_service_ids))
        if company_ids is not None:
            filters.append(TrainerEvaluationConfig.company_id.in_(company_ids))
        if is_active is not None:
            filters.append(TrainerEvaluationConfig.is_active == is_active)
        if filters:
            stmt = stmt.where(and_(*filters))
        stmt = stmt.order_by(desc(TrainerEvaluationConfig.created_at))
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_available_structures(
        db: AsyncSession,
        service_id: int,
        include_inactive: bool = False,
        include_archived: bool = False,
    ) -> List[Prompt]:
        # Validate that service exists
        stmt_srv = select(Service).where(Service.service_id == service_id)
        res_srv = await db.execute(stmt_srv)
        if not res_srv.scalars().first():
            raise ValueError(f"El servicio con ID {service_id} no existe.")

        filters = [
            Prompt.service_id == service_id,
            Prompt.deleted_at == None,
        ]

        if not include_inactive:
            filters.append(Prompt.is_active == True)

        if not include_archived:
            filters.append(Prompt.is_archived == False)

        stmt = select(Prompt).where(and_(*filters)).order_by(Prompt.prompt_name.asc())
        res = await db.execute(stmt)
        return list(res.scalars().all())


    # ── Demo Company Simulation Code Helpers ─────────────────────────────────────

    @staticmethod
    def normalize_simulation_code(code: Optional[str]) -> str:
        """Strip whitespace, hyphens, underscores and convert to uppercase."""
        if not code:
            return ""
        return code.strip().upper().replace(" ", "").replace("-", "").replace("_", "")

    @staticmethod
    def is_valid_demo_simulation_code(code: str) -> bool:
        """
        Convention for Empresa Demo simulations:
        - 6 to 8 characters
        - Alphanumeric uppercase [A-Z0-9]
        - No spaces, hyphens, or underscores
        """
        return bool(re.match(r"^[A-Z0-9]{6,8}$", code))

    @staticmethod
    async def is_demo_company(db: AsyncSession, company_id: Optional[int]) -> bool:
        """Check if company_id belongs to Empresa Demo (is_demo=True or company_key='empresa-demo')."""
        if not company_id:
            return False
        from app.models.companies import Company
        stmt = select(Company).where(Company.company_id == company_id)
        res = await db.execute(stmt)
        comp = res.scalars().first()
        if not comp:
            return False
        return bool(comp.is_demo or comp.company_key == "empresa-demo")

    @staticmethod
    async def generate_demo_simulation_code(db: AsyncSession, service_id: int) -> str:
        """
        Generate a compliant 6-character code (e.g. ATEN01, VENT01) for Empresa Demo
        based on the service name mnemonic and sequential counter.
        """
        from app.models.services import Service
        stmt_svc = select(Service).where(Service.service_id == service_id)
        res_svc = await db.execute(stmt_svc)
        svc = res_svc.scalars().first()
        svc_name = (svc.service_name if svc else "").upper()

        if "VENT" in svc_name:
            prefix = "VENT"
        elif "ATEN" in svc_name or "CLIENT" in svc_name:
            prefix = "ATEN"
        elif "RECL" in svc_name:
            prefix = "RECL"
        elif "SOPO" in svc_name:
            prefix = "SOPO"
        else:
            clean_name = re.sub(r"[^A-Z0-9]", "", svc_name)
            prefix = clean_name[:4] if len(clean_name) >= 4 else clean_name.ljust(4, "X")

        stmt_sims = select(TrainerSimulation.code).where(
            TrainerSimulation.code.like(f"{prefix}%")
        )
        res_sims = await db.execute(stmt_sims)
        existing_codes = set(res_sims.scalars().all())

        for idx in range(1, 100):
            candidate = f"{prefix}{idx:02d}"
            if candidate not in existing_codes:
                return candidate

        for idx in range(100, 1000):
            candidate = f"{prefix[:5]}{idx:03d}"
            if candidate not in existing_codes:
                return candidate

        raise RuntimeError("No se pudo generar un código único para la simulación de Empresa Demo.")

    # ── Simulations ───────────────────────────────────────────────────────────────

    @staticmethod
    async def create_simulation(
        db: AsyncSession, payload: TrainerSimulationCreate, created_by: Optional[str] = None
    ) -> TrainerSimulation:
        if payload.evaluation_config_id:
            # Validate config
            cfg = await TrainerService.get_evaluation_config(db, payload.evaluation_config_id)
            if not cfg:
                raise ValueError("La configuración de evaluación seleccionada no existe.")
            if cfg.service_id != payload.service_id:
                raise ValueError("La configuración de evaluación seleccionada no pertenece al mismo servicio.")

        # Resolve company_id from service
        from app.models.services import Service
        stmt_svc = select(Service.company_id).where(Service.service_id == payload.service_id)
        res_svc = await db.execute(stmt_svc)
        company_id = res_svc.scalar()

        is_demo = await TrainerService.is_demo_company(db, company_id)
        code_input = payload.code.strip() if payload.code else ""
        if is_demo:
            norm_code = TrainerService.normalize_simulation_code(code_input)
            if not norm_code or norm_code.startswith("SIMDEMO") or norm_code.startswith("SIM"):
                norm_code = await TrainerService.generate_demo_simulation_code(db, payload.service_id)
            elif not TrainerService.is_valid_demo_simulation_code(norm_code):
                raise ValueError(
                    f"El código de simulación '{payload.code}' no es válido para la Empresa Demo. "
                    "Debe tener entre 6 y 8 caracteres alfanuméricos sin espacios ni guiones (ej. ATEN01, VENT01)."
                )
            code_final = norm_code
        else:
            code_final = code_input

        # Check code uniqueness
        stmt_check = select(TrainerSimulation).where(TrainerSimulation.code == code_final)
        res_check = await db.execute(stmt_check)
        if res_check.scalars().first():
            raise ValueError(f"El código de simulación '{code_final}' ya existe de manera global.")

        sim = TrainerSimulation(
            name=payload.name,
            code=code_final,
            service_id=payload.service_id,
            company_id=company_id,
            evaluation_config_id=payload.evaluation_config_id,
            roleplay_prompt=payload.roleplay_prompt,
            objective=payload.objective,
            difficulty=payload.difficulty,
            status="draft",
            created_by=created_by,
        )
        db.add(sim)
        await db.commit()
        await db.refresh(sim)
        return sim

    @staticmethod
    async def update_simulation(
        db: AsyncSession, simulation_id: int, payload: TrainerSimulationUpdate, updated_by: Optional[str] = None
    ) -> Optional[TrainerSimulation]:
        stmt = select(TrainerSimulation).where(TrainerSimulation.simulation_id == simulation_id)
        res = await db.execute(stmt)
        sim = res.scalars().first()
        if not sim:
            return None

        # If code changed, check uniqueness
        if payload.code is not None and payload.code.strip() != sim.code:
            is_demo = await TrainerService.is_demo_company(db, sim.company_id)
            if is_demo:
                code_clean = TrainerService.normalize_simulation_code(payload.code)
                if not TrainerService.is_valid_demo_simulation_code(code_clean):
                    raise ValueError(
                        f"El código de simulación '{payload.code}' no es válido para la Empresa Demo. "
                        "Debe tener entre 6 y 8 caracteres alfanuméricos sin espacios ni guiones (ej. ATEN01, VENT01)."
                    )
            else:
                code_clean = payload.code.strip()

            if code_clean != sim.code:
                stmt_check = select(TrainerSimulation).where(TrainerSimulation.code == code_clean)
                res_check = await db.execute(stmt_check)
                if res_check.scalars().first():
                    raise ValueError(f"El código de simulación '{payload.code}' ya existe de manera global.")
                sim.code = code_clean

        if payload.evaluation_config_id is not None:
            if payload.evaluation_config_id:
                cfg = await TrainerService.get_evaluation_config(db, payload.evaluation_config_id)
                if not cfg:
                    raise ValueError("La configuración de evaluación seleccionada no existe.")
                if cfg.service_id != (payload.service_id or sim.service_id):
                    raise ValueError("La configuración de evaluación seleccionada no pertenece al mismo servicio.")
            sim.evaluation_config_id = payload.evaluation_config_id

        # Check if version needs to be updated (do this BEFORE applying updates to sim)
        prompt_changed = False
        config_changed = False
        if sim.status == "published":
            prompt_changed = payload.roleplay_prompt is not None and payload.roleplay_prompt != sim.roleplay_prompt
            config_changed = payload.evaluation_config_id is not None and payload.evaluation_config_id != sim.evaluation_config_id

        if payload.name is not None:
            sim.name = payload.name
        if payload.roleplay_prompt is not None:
            sim.roleplay_prompt = payload.roleplay_prompt
        if payload.objective is not None:
            sim.objective = payload.objective
        if payload.difficulty is not None:
            sim.difficulty = payload.difficulty

        # If simulation is already published and we update either prompt or config, increment version
        if prompt_changed or config_changed:
                # Retrieve active config details to snapshot
                cfg_snap = {}
                if sim.evaluation_config_id:
                    cfg = await TrainerService.get_evaluation_config(db, sim.evaluation_config_id)
                    if cfg:
                        cfg_snap = {
                            "config_id": cfg.config_id,
                            "name": cfg.name,
                            "speech_structure_id": cfg.speech_structure_id,
                            "extra_instructions": cfg.extra_instructions,
                        }
                
                # Fetch max version number
                stmt_v = select(func.max(TrainerSimulationVersion.version_number)).where(
                    TrainerSimulationVersion.simulation_id == simulation_id
                )
                res_v = await db.execute(stmt_v)
                max_v = res_v.scalar() or 0
                
                new_v = TrainerSimulationVersion(
                    simulation_id=simulation_id,
                    version_number=max_v + 1,
                    roleplay_prompt_snapshot=payload.roleplay_prompt or sim.roleplay_prompt,
                    evaluation_config_snapshot=cfg_snap,
                    service_id=sim.service_id,
                    evaluation_config_id=sim.evaluation_config_id,
                    created_by=updated_by,
                )
                db.add(new_v)
                logger.info("Created new simulation version %d for simulation %d during published edit.", max_v + 1, simulation_id)

        sim.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(sim)
        return sim

    @staticmethod
    async def publish_simulation(db: AsyncSession, simulation_id: int, user_email: Optional[str] = None) -> TrainerSimulation:
        stmt = select(TrainerSimulation).where(TrainerSimulation.simulation_id == simulation_id)
        res = await db.execute(stmt)
        sim = res.scalars().first()
        if not sim:
            raise ValueError("La simulación no existe.")

        # Validations
        if not sim.name or not sim.code or not sim.service_id or not sim.roleplay_prompt:
            raise ValueError("No se puede publicar una simulación incompleta. Debe tener nombre, código, servicio y prompt de roleplay.")
        if not sim.evaluation_config_id:
            raise ValueError("Debe asignar una configuración de evaluación antes de publicar la simulación.")

        cfg = await TrainerService.get_evaluation_config(db, sim.evaluation_config_id)
        if not cfg or not cfg.is_active:
            raise ValueError("La configuración de evaluación asociada no existe o está inactiva.")
        if cfg.service_id != sim.service_id:
            raise ValueError("La configuración de evaluación y la simulación deben pertenecer al mismo servicio.")

        # Check if version exists. If not, generate version 1
        stmt_v_count = select(func.count(TrainerSimulationVersion.version_id)).where(
            TrainerSimulationVersion.simulation_id == simulation_id
        )
        res_v_count = await db.execute(stmt_v_count)
        v_count = res_v_count.scalar() or 0

        if v_count == 0:
            cfg_snap = {
                "config_id": cfg.config_id,
                "name": cfg.name,
                "speech_structure_id": cfg.speech_structure_id,
                "extra_instructions": cfg.extra_instructions,
            }
            v1 = TrainerSimulationVersion(
                simulation_id=simulation_id,
                version_number=1,
                roleplay_prompt_snapshot=sim.roleplay_prompt,
                evaluation_config_snapshot=cfg_snap,
                service_id=sim.service_id,
                evaluation_config_id=sim.evaluation_config_id,
                created_by=user_email,
            )
            db.add(v1)
            logger.info("Initial publication: simulation version 1 created.")

        sim.status = "published"
        sim.published_at = datetime.now(timezone.utc)
        sim.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(sim)
        return sim

    @staticmethod
    async def archive_simulation(db: AsyncSession, simulation_id: int) -> Optional[TrainerSimulation]:
        stmt = select(TrainerSimulation).where(TrainerSimulation.simulation_id == simulation_id)
        res = await db.execute(stmt)
        sim = res.scalars().first()
        if not sim:
            return None

        sim.status = "archived"
        sim.archived_at = datetime.now(timezone.utc)
        sim.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(sim)
        return sim

    @staticmethod
    async def duplicate_simulation(db: AsyncSession, simulation_id: int, user_email: Optional[str] = None) -> TrainerSimulation:
        stmt = select(TrainerSimulation).where(TrainerSimulation.simulation_id == simulation_id)
        res = await db.execute(stmt)
        sim = res.scalars().first()
        if not sim:
            raise ValueError("La simulación original no existe.")

        # Find unique code for duplicated simulation
        is_demo = await TrainerService.is_demo_company(db, sim.company_id)
        if is_demo or TrainerService.is_valid_demo_simulation_code(sim.code):
            m = re.match(r"^([A-Z0-9]{2,6}?)(\d{2})$", sim.code)
            if m:
                prefix = m.group(1)
                curr_num = int(m.group(2))
            else:
                prefix = sim.code[:4] if len(sim.code) >= 4 else sim.code.ljust(4, "X")
                curr_num = 1

            next_num = curr_num + 1
            while True:
                candidate = f"{prefix}{next_num:02d}"
                if len(candidate) > 8:
                    candidate = f"{prefix[:5]}{next_num:03d}"[:8]
                stmt_dup = select(TrainerSimulation).where(TrainerSimulation.code == candidate)
                res_dup = await db.execute(stmt_dup)
                if not res_dup.scalars().first():
                    new_code = candidate
                    break
                next_num += 1
        else:
            suffix = 1
            new_code = f"{sim.code}_COPY"
            while True:
                stmt_dup = select(TrainerSimulation).where(TrainerSimulation.code == new_code)
                res_dup = await db.execute(stmt_dup)
                if not res_dup.scalars().first():
                    break
                suffix += 1
                new_code = f"{sim.code}_COPY{suffix}"

        new_sim = TrainerSimulation(
            name=f"{sim.name} (Copia)",
            code=new_code,
            service_id=sim.service_id,
            company_id=sim.company_id,
            evaluation_config_id=sim.evaluation_config_id,
            roleplay_prompt=sim.roleplay_prompt,
            objective=sim.objective,
            difficulty=sim.difficulty,
            status="draft",
            created_by=user_email,
        )
        db.add(new_sim)
        await db.commit()
        await db.refresh(new_sim)
        return new_sim

    @staticmethod
    async def get_simulation(db: AsyncSession, simulation_id: int) -> Optional[TrainerSimulation]:
        stmt = select(TrainerSimulation).where(TrainerSimulation.simulation_id == simulation_id)
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def list_simulations(
        db: AsyncSession,
        service_id: Optional[int] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        code: Optional[str] = None,
        company_ids: Optional[List[int]] = None,
        allowed_service_ids: Optional[List[int]] = None,
    ) -> List[TrainerSimulation]:
        stmt = select(TrainerSimulation)
        filters = []
        if service_id is not None:
            filters.append(TrainerSimulation.service_id == service_id)
        if allowed_service_ids is not None:
            filters.append(TrainerSimulation.service_id.in_(allowed_service_ids))
        if company_ids is not None:
            filters.append(TrainerSimulation.company_id.in_(company_ids))
        if status is not None:
            filters.append(TrainerSimulation.status == status)
        if code is not None:
            filters.append(TrainerSimulation.code == code.strip())
        if search:
            filters.append(
                or_(
                    TrainerSimulation.name.ilike(f"%{search}%"),
                    TrainerSimulation.objective.ilike(f"%{search}%"),
                )
            )
        if filters:
            stmt = stmt.where(and_(*filters))
        stmt = stmt.order_by(desc(TrainerSimulation.created_at))
        res = await db.execute(stmt)
        return list(res.scalars().all())


    # ── AI Prompts Generation / Improvement ───────────────────────────────────────

    @staticmethod
    async def generate_roleplay_prompt_ai(payload: AIPromptGenerateRequest, db: Optional[AsyncSession] = None) -> str:
        service_name = None
        company_name = None
        is_healthcare = False
        is_demo = False

        if db and payload.service_id:
            stmt_svc = select(Service).options(selectinload(Service.company)).where(Service.service_id == payload.service_id)
            res_svc = await db.execute(stmt_svc)
            svc = res_svc.scalars().first()
            if svc:
                service_name = svc.service_name
                comp = getattr(svc, "company", None)
                if comp:
                    company_name = comp.company_name
                    is_demo = bool(comp.is_demo or comp.company_key == "empresa-demo")
                    if not is_demo:
                        c_sector = (comp.sector or "").lower()
                        c_name = (comp.company_name or "").lower()
                        c_key = (comp.company_key or "").lower()
                        if c_sector in ("healthcare", "salud", "medico", "médico", "clinica", "clínica"):
                            is_healthcare = True
                        elif "boston" in c_name or "boston" in c_key or comp.company_id == 1:
                            is_healthcare = True

        if is_healthcare:
            role_desc = f"un paciente llamando a {company_name or 'una clínica médica'}"
            context_bullet = "1. Su nombre, edad y contexto clínico ficticio acorde al servicio.\n"
            char_rule = "nunca salirse del personaje de paciente"
        else:
            org_desc = f" de {company_name}" if company_name and not is_demo else ""
            svc_desc = f" del servicio de {service_name}" if service_name else ""
            role_desc = f"un cliente o interlocutor llamando{svc_desc}{org_desc}"
            context_bullet = "1. Su nombre, perfil y motivo concreto de la llamada acorde al servicio.\n"
            char_rule = "nunca salirse del personaje simulado de cliente/interlocutor"

        system_instruction = (
            "Eres un experto en redactar prompts de juego de rol (roleplay) inmersivos en español para simulaciones de voz interactiva.\n"
            f"Tu tarea es diseñar un prompt para un modelo de lenguaje que simulará a {role_desc}.\n"
            "El prompt debe ser muy detallado e instruir al modelo sobre:\n"
            f"{context_bullet}"
            "2. Su personalidad, estado emocional (ej. ansioso, tímido, enfadado, exigente o apresurado) y tono.\n"
            "3. Pautas de conversación: responder de forma natural, dar respuestas cortas típicas de llamadas telefónicas, interrumpir si el agente habla demasiado, simular vacilaciones.\n"
            "4. Sus objeciones principales o dudas que el agente telefónico debe resolver.\n"
            f"5. Reglas estrictas de juego: {char_rule}, colgar limpiamente llamando al tool `hangup_call` cuando el roleplay sea exitoso o si el agente es grosero.\n"
            "Devuelve única y exclusivamente el texto final del prompt listo para ser copiado y guardado, sin formato markdown ni texto introductorio."
        )
        user_message = (
            f"Por favor genera un prompt de roleplay basado en los siguientes parámetros:\n"
            f"- Servicio: {service_name or payload.service_id}\n"
            f"- Objetivo de la llamada: {payload.objective}\n"
            f"- Ideas clave del escenario: {payload.ideas}\n"
            f"- Dificultad sugerida: {payload.difficulty or 'media'}\n"
            f"- Tono: {payload.tone or 'neutral/realista'}"
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_message}
        ]
        raw = await openai_service.complete_text(messages=messages, response_format=None)
        return raw.strip()

    @staticmethod
    async def improve_roleplay_prompt_ai(payload: AIPromptImproveRequest) -> str:
        system_instruction = (
            "Eres un experto en refinar y pulir prompts de juego de rol (roleplay) para simulaciones telefónicas en español.\n"
            "Tu tarea es mejorar el prompt proporcionado por el usuario, aplicando exactamente los cambios solicitados.\n"
            "Mantén el formato inmersivo y las instrucciones de control del personaje, objeciones y llamadas a herramientas (hangup_call).\n"
            "Devuelve única y exclusivamente el texto final del prompt mejorado y corregido, sin formato markdown ni texto introductorio."
        )
        user_message = (
            f"Prompt actual a mejorar:\n\"\"\"\n{payload.current_prompt}\n\"\"\"\n\n"
            f"Cambios solicitados por el usuario:\n{payload.requested_changes}"
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_message}
        ]
        raw = await openai_service.complete_text(messages=messages, response_format=None)
        return raw.strip()


    # ── Phone Integration webhooks ────────────────────────────────────────────────

    @staticmethod
    async def validate_agent_code(db: AsyncSession, agent_code: str) -> Optional[dict]:
        if not agent_code:
            return None
        from app.utils.phone_code_normalizer import normalize_spoken_code
        normalized = normalize_spoken_code(agent_code)
        cleaned = normalized if normalized else agent_code.replace(" ", "").upper()
        if not cleaned:
            return None

        # Count active agents that have at least one code set (for diagnostics)
        stmt_all = select(TrainingAgentSetting).where(
            TrainingAgentSetting.training_code_enabled == True,
        )
        res_all = await db.execute(stmt_all)
        active_with_codes = list(res_all.scalars().all())
        active_count = len(active_with_codes)

        # Check for duplicate code collision (defensive: detect ambiguous state)
        matching_all = [
            s for s in active_with_codes
            if (s.training_code and s.training_code.upper() == cleaned)
            or (s.training_numeric_code and s.training_numeric_code.upper() == cleaned)
        ]
        if len(matching_all) > 1:
            names = [s.agent_name for s in matching_all]
            logger.error(
                "Training Hub agent validation CONFLICT: normalized_code=%s "
                "matches %d agents (%s). Rejecting to avoid ambiguity.",
                cleaned, len(matching_all), ", ".join(names)
            )
            return None

        stmt = select(TrainingAgentSetting).where(
            and_(
                or_(
                    func.upper(TrainingAgentSetting.training_code) == cleaned,
                    TrainingAgentSetting.training_numeric_code == cleaned,
                ),
                TrainingAgentSetting.training_code_enabled == True,
            )
        )
        res = await db.execute(stmt)
        setting = res.scalars().first()
        if not setting:
            # Build a compact code map for diagnostics (initials -> numeric_code)
            code_map = ", ".join(
                f"{s.agent_initials}->{s.training_numeric_code or '-'}"
                for s in active_with_codes
                if s.training_numeric_code
            )
            logger.warning(
                "Training Hub agent validation failed: "
                "normalized_code=%s | searched_fields=training_code,training_numeric_code | "
                "active_agents_with_codes=%d | code_map=[%s] | reason=not_found",
                cleaned, active_count, code_map
            )
            return None

        logger.info(
            "Training Hub agent validation OK: normalized_code=%s -> agent=%s (%s)",
            cleaned, setting.agent_name, setting.agent_initials
        )
        return {
            "agent_id": setting.hubspot_owner_id,
            "agent_name": setting.agent_name,
            "agent_initials": setting.agent_initials,
        }

    @staticmethod
    async def log_agent_code_map(db: AsyncSession) -> None:
        """Startup diagnostic: print all active agent codes to logs."""
        stmt = select(TrainingAgentSetting).order_by(TrainingAgentSetting.agent_initials)
        res = await db.execute(stmt)
        agents = list(res.scalars().all())
        lines = []
        for s in agents:
            code_part = s.training_numeric_code or "-"
            alpha_part = s.training_code or "-"
            enabled_flag = "active" if s.training_code_enabled else "disabled"
            lines.append(f"  {s.agent_initials} -> numeric={code_part}, alpha={alpha_part} [{enabled_flag}]")
        if lines:
            logger.info("Training agent code map:\n%s", "\n".join(lines))
        else:
            logger.warning("Training agent code map: no agents found in bm_training_agent_settings")



    @staticmethod
    async def validate_simulation_code(db: AsyncSession, simulation_code: str) -> Optional[TrainerSimulation]:
        if not simulation_code:
            return None
        raw_clean = simulation_code.replace(" ", "").strip().upper()
        stripped_clean = raw_clean.replace("-", "").replace("_", "")

        candidates = [raw_clean, stripped_clean]
        # Support spoken single digit like 'ATEN1' -> 'ATEN01'
        m = re.match(r"^([A-Z]{3,6})(\d)$", stripped_clean)
        if m:
            candidates.append(f"{m.group(1)}0{m.group(2)}")

        unique_candidates = list(dict.fromkeys(c for c in candidates if c))

        stmt = select(TrainerSimulation).where(
            and_(
                or_(
                    func.upper(TrainerSimulation.code).in_(unique_candidates),
                    func.replace(func.replace(func.upper(TrainerSimulation.code), "-", ""), "_", "").in_(unique_candidates),
                ),
                TrainerSimulation.status == "published",
            )
        )
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def get_agent_service_context(db: AsyncSession, agent_id: str) -> Dict[str, Any]:
        """
        Resolve service context for an agent by hubspot_owner_id or user_id.
        Returns:
            {
                "service_ids": Set[int],
                "primary_service_name": Optional[str],
                "company_id": Optional[int]
            }
        """
        if not agent_id:
            return {"service_ids": set(), "primary_service_name": None, "company_id": None}

        service_ids: Set[int] = set()
        primary_service_name: Optional[str] = None
        company_id: Optional[int] = None

        # 1. Try to find user in bm_users
        stmt_user = (
            select(User)
            .options(selectinload(User.primary_service))
            .where(
                or_(
                    User.hubspot_owner_id == agent_id,
                    func.cast(User.user_id, Text) == agent_id,
                )
            )
        )
        res_user = await db.execute(stmt_user)
        user = res_user.scalars().first()

        if user:
            company_id = user.company_id
            if user.primary_service_id:
                service_ids.add(user.primary_service_id)
                if user.primary_service:
                    primary_service_name = user.primary_service.service_name

            # Secondary / additional services in bm_user_services
            stmt_assoc = select(UserServiceAssociation.service_id).where(
                UserServiceAssociation.user_id == user.user_id
            )
            res_assoc = await db.execute(stmt_assoc)
            for sid in res_assoc.scalars().all():
                service_ids.add(sid)

        # 2. If company_id not found from User, check TrainingAgentSetting
        if company_id is None:
            stmt_setting = select(TrainingAgentSetting.company_id).where(
                TrainingAgentSetting.hubspot_owner_id == agent_id
            )
            res_setting = await db.execute(stmt_setting)
            setting_cid = res_setting.scalar()
            if setting_cid:
                company_id = setting_cid

        # 3. If no services or company_id found from User, check TrainingAgentReport as fallback
        if not service_ids or company_id is None:
            stmt_rep = (
                select(TrainingAgentReport.service_id, Service.service_name, TrainingAgentReport.company_id)
                .outerjoin(Service, TrainingAgentReport.service_id == Service.service_id)
                .where(
                    and_(
                        TrainingAgentReport.hubspot_owner_id == agent_id,
                        TrainingAgentReport.service_id.is_not(None),
                    )
                )
                .order_by(desc(TrainingAgentReport.training_report_id))
                .limit(1)
            )
            res_rep = await db.execute(stmt_rep)
            rep_row = res_rep.first()
            if rep_row:
                if rep_row[0] and not service_ids:
                    service_ids.add(rep_row[0])
                if not primary_service_name and rep_row[1]:
                    primary_service_name = rep_row[1]
                if not company_id and rep_row[2]:
                    company_id = rep_row[2]

        # 4. If still no company_id but we have service_ids, resolve from Service
        if not company_id and service_ids:
            first_sid = next(iter(service_ids))
            stmt_svc_comp = select(Service.company_id).where(Service.service_id == first_sid)
            res_svc_comp = await db.execute(stmt_svc_comp)
            company_id = res_svc_comp.scalar()

        return {
            "service_ids": service_ids,
            "primary_service_name": primary_service_name,
            "company_id": company_id,
        }

    @staticmethod
    async def validate_simulation_for_agent(
        db: AsyncSession, simulation_code: str, agent_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Validate simulation code and check service match with the identified agent.
        Returns:
            {
                "valid": bool,
                "simulation": Optional[TrainerSimulation],
                "status": "valid" | "not_found" | "service_mismatch",
                "reason": Optional[str],
                "message": Optional[str],
                "agent_service_name": Optional[str],
                "simulation_service_name": Optional[str],
            }
        """
        sim = await TrainerService.validate_simulation_code(db, simulation_code)
        if not sim:
            return {
                "valid": False,
                "simulation": None,
                "status": "not_found",
                "reason": "not_found",
                "message": "No he encontrado ese código de simulación. Por favor, repítelo.",
                "agent_service_name": None,
                "simulation_service_name": None,
            }

        if not agent_id:
            return {
                "valid": True,
                "simulation": sim,
                "status": "valid",
                "reason": None,
                "message": None,
                "agent_service_name": None,
                "simulation_service_name": getattr(sim.service, "service_name", None) if getattr(sim, "service", None) else None,
            }

        service_ctx = await TrainerService.get_agent_service_context(db, agent_id)
        agent_company_id = service_ctx.get("company_id")
        allowed_service_ids = service_ctx.get("service_ids") or set()

        # 1. Company isolation check
        if agent_company_id is not None and sim.company_id is not None and sim.company_id != agent_company_id:
            logger.warning(
                "Trainer simulation validation COMPANY MISMATCH: agent_id=%s (company_id=%s) "
                "attempted simulation %s (company_id=%s)",
                agent_id, agent_company_id, sim.code, sim.company_id
            )
            return {
                "valid": False,
                "simulation": sim,
                "status": "company_mismatch",
                "reason": "company_mismatch",
                "message": "Ese código de simulación pertenece a otra organización y no a la tuya. Por favor, introduce un código de simulación válido.",
                "agent_service_name": service_ctx.get("primary_service_name"),
                "simulation_service_name": getattr(sim.service, "service_name", None) if getattr(sim, "service", None) else None,
            }

        # 2. Service match check
        if allowed_service_ids and sim.service_id not in allowed_service_ids:
            agent_service_name = service_ctx.get("primary_service_name")
            if agent_service_name:
                msg = f"Ese código de simulación pertenece a otro servicio y no al tuyo actual ({agent_service_name}). Por favor, introduce un código de simulación de tu servicio."
            else:
                msg = "Ese código de simulación pertenece a otro servicio y no a tu servicio actual. Por favor, introduce un código de tu servicio."
            return {
                "valid": False,
                "simulation": sim,
                "status": "service_mismatch",
                "reason": "service_mismatch",
                "message": msg,
                "agent_service_name": agent_service_name,
                "simulation_service_name": getattr(sim.service, "service_name", None) if getattr(sim, "service", None) else None,
            }

        return {
            "valid": True,
            "simulation": sim,
            "status": "valid",
            "reason": None,
            "message": None,
            "agent_service_name": service_ctx.get("primary_service_name"),
            "simulation_service_name": getattr(sim.service, "service_name", None) if getattr(sim, "service", None) else None,
        }

    @staticmethod
    async def start_phone_session(
        db: AsyncSession, agent_code: str, simulation_code: str, call_id: str, external_call_sid: Optional[str] = None
    ) -> TrainerSession:
        agent = await TrainerService.validate_agent_code(db, agent_code)
        if not agent:
            raise ValueError(f"Código de agente '{agent_code}' no válido o inactivo.")

        sim = await TrainerService.validate_simulation_code(db, simulation_code)
        if not sim:
            raise ValueError(f"Código de simulación '{simulation_code}' no válido o no está publicada.")

        # Enforce company isolation and service match
        val_res = await TrainerService.validate_simulation_for_agent(db, simulation_code, agent["agent_id"])
        if not val_res["valid"]:
            raise ValueError(val_res.get("message") or f"La simulación '{simulation_code}' no es accesible para el agente '{agent_code}'.")

        # Find active version of simulation
        stmt_v = select(TrainerSimulationVersion).where(
            TrainerSimulationVersion.simulation_id == sim.simulation_id
        ).order_by(desc(TrainerSimulationVersion.version_number)).limit(1)
        res_v = await db.execute(stmt_v)
        active_version = res_v.scalars().first()
        active_version_id = active_version.version_id if active_version else None

        # Check for active execution lock for this call_id to prevent duplicate session records
        stmt_lock = select(TrainerSession).where(
            and_(
                TrainerSession.call_id == call_id,
                TrainerSession.status == "started"
            )
        )
        res_lock = await db.execute(stmt_lock)
        existing_session = res_lock.scalars().first()
        if existing_session:
            logger.info("Found existing started session for call_id=%s. Reusing session_id=%d.", call_id, existing_session.session_id)
            return existing_session

        sess = TrainerSession(
            simulation_id=sim.simulation_id,
            simulation_version_id=active_version_id,
            agent_id=agent["agent_id"],
            agent_code=agent_code.replace(" ", "").upper(),
            company_id=sim.company_id,
            service_id=sim.service_id,
            call_id=call_id,
            external_call_sid=external_call_sid or call_id,
            status="started",
            evaluation_status="started",
            started_at=datetime.now(timezone.utc),
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess

    @staticmethod
    async def complete_phone_session(
        db: AsyncSession,
        session_id: int,
        transcript: Optional[str] = None,
        recording_url: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> TrainerSession:
        stmt = select(TrainerSession).where(TrainerSession.session_id == session_id)
        res = await db.execute(stmt)
        sess = res.scalars().first()
        if not sess:
            raise ValueError(f"Sesión Trainer ID {session_id} no encontrada.")

        if transcript:
            sess.transcript = transcript
        if recording_url:
            sess.recording_url = recording_url
        if duration_seconds is not None:
            sess.duration_seconds = duration_seconds

        sess.status = "completed"
        sess.evaluation_status = "evaluation_pending"
        sess.ended_at = datetime.now(timezone.utc)
        sess.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(sess)

        # Trigger background evaluation task
        from app.db import AsyncSessionLocal
        
        async def run_evaluation_task():
            async with AsyncSessionLocal() as task_db:
                try:
                    await TrainerService.evaluate_session_task(task_db, session_id)
                except Exception as e_task:
                    logger.exception("Failed background evaluation for trainer session %d: %s", session_id, e_task)

        asyncio.create_task(run_evaluation_task())
        return sess


    @staticmethod
    async def download_trainer_recording_audio(recording_url: str) -> bytes:
        """Download trainer call recording audio using Twilio credentials."""
        if not recording_url:
            raise ValueError("recording_url is empty")
        from app.services.twilio_service import TwilioService
        twilio = TwilioService()
        return await twilio.download_audio(recording_url)


    @staticmethod
    def _extract_robust_score(parsed_res: dict, criteria_evals: list = None) -> Optional[Decimal]:
        """Robustly extract numerical score from LLM response handling various formats and fallbacks."""
        if not parsed_res or not isinstance(parsed_res, dict):
            return None

        # 1. Candidate keys in root
        candidate_keys = [
            "score", "evaluacion_global", "puntuacion", "calificacion",
            "nota", "nota_global", "global_score", "puntaje"
        ]
        
        raw_val = None
        for k in candidate_keys:
            if k in parsed_res and parsed_res[k] is not None:
                raw_val = parsed_res[k]
                break

        # Check nested structures: {"evaluacion": {"score": ...}}, {"evaluation": {...}}
        if raw_val is None:
            for parent in ["evaluacion", "evaluation", "resumen", "summary"]:
                sub = parsed_res.get(parent)
                if isinstance(sub, dict):
                    for k in candidate_keys:
                        if k in sub and sub[k] is not None:
                            raw_val = sub[k]
                            break
                if raw_val is not None:
                    break

        if raw_val is not None:
            clean_str = str(raw_val).strip()
            # Clean "8.5/10", "8/10", "8 / 10"
            clean_str = re.sub(r"\s*/\s*10(\.0+)?.*$", "", clean_str, flags=re.IGNORECASE)
            # Clean "8 sobre 10", "8 de 10"
            clean_str = re.sub(r"\s*(sobre|de)\s*10(\.0+)?.*$", "", clean_str, flags=re.IGNORECASE)
            clean_str = clean_str.replace(",", ".").replace("%", "").strip()
            try:
                dec = Decimal(clean_str)
                if 0 <= dec <= 10:
                    return Decimal(str(round(float(dec), 2)))
                elif 10 < dec <= 100:
                    return Decimal(str(round(float(dec) / 10.0, 2)))
            except Exception:
                pass

        # 2. Fallback: average scores from criteria_evaluations if present
        evals_to_check = criteria_evals if criteria_evals is not None else parsed_res.get("criteria_evaluations")
        if isinstance(evals_to_check, list) and evals_to_check:
            crit_scores = []
            for item in evals_to_check:
                if isinstance(item, dict) and item.get("score") is not None:
                    try:
                        s_str = str(item["score"]).replace(",", ".").replace("%", "").strip()
                        s_str = re.sub(r"\s*/\s*10.*$", "", s_str)
                        val = float(s_str)
                        if 0 <= val <= 10:
                            crit_scores.append(val)
                    except (ValueError, TypeError):
                        pass
            if crit_scores:
                avg = sum(crit_scores) / len(crit_scores)
                return Decimal(str(round(avg, 2)))

        return None

    # ── Background Evaluation Execution ──────────────────────────────────────────

    @staticmethod
    async def evaluate_session_task(db: AsyncSession, session_id: int) -> None:
        logger.info("Executing background evaluation task for Trainer session %d...", session_id)
        
        stmt = select(TrainerSession).where(TrainerSession.session_id == session_id)
        res = await db.execute(stmt)
        sess = res.scalars().first()
        if not sess:
            logger.error("Session %d not found for background evaluation.", session_id)
            return

        sess.evaluation_status = "running"
        await db.commit()

        try:
            # 1. Resolve transcription
            transcript_text = sess.transcript
            
            if not transcript_text and sess.recording_url:
                # Download and transcribe audio
                logger.info("Downloading call audio for session %d from: %s", session_id, sess.recording_url)
                audio_bytes = await TrainerService.download_trainer_recording_audio(sess.recording_url)
                
                logger.info("Transcribing call audio for session %d via Whisper...", session_id)
                transcription_result = await openai_service.transcribe_audio(audio_bytes, filename="call.mp3")
                transcript_text = transcription_result.get("text")
                sess.transcript = transcript_text
                await db.commit()

            if not transcript_text:
                raise ValueError("No se pudo obtener la transcripción de la llamada (el audio no se pudo procesar o está vacío).")

            # 2. Resolve simulation details and config
            # Try to get snapshot details from Simulation Version
            version = None
            if sess.simulation_version_id:
                stmt_v = select(TrainerSimulationVersion).where(
                    TrainerSimulationVersion.version_id == sess.simulation_version_id
                )
                res_v = await db.execute(stmt_v)
                version = res_v.scalars().first()

            stmt_sim = select(TrainerSimulation).where(TrainerSimulation.simulation_id == sess.simulation_id)
            res_sim = await db.execute(stmt_sim)
            sim = res_sim.scalars().first()
            if not sim:
                raise ValueError(f"La simulación asociada ID {sess.simulation_id} no existe.")

            roleplay_prompt = version.roleplay_prompt_snapshot if version else sim.roleplay_prompt
            config_id = version.evaluation_config_id if version else sim.evaluation_config_id

            if not config_id:
                raise ValueError("La simulación no tiene una configuración de evaluación asociada.")

            # Load evaluation config
            stmt_cfg = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config_id)
            res_cfg = await db.execute(stmt_cfg)
            cfg = res_cfg.scalars().first()
            if not cfg:
                raise ValueError(f"La configuración de evaluación ID {config_id} no existe.")

            # 3. Retrieve Speech evaluation structure template and active criteria
            stmt_prompt = select(PromptVersion).where(
                and_(
                    PromptVersion.prompt_id == cfg.speech_structure_id,
                    PromptVersion.is_current == True,
                    PromptVersion.is_archived == False,
                )
            )
            res_prompt = await db.execute(stmt_prompt)
            prompt_version = res_prompt.scalars().first()
            if not prompt_version:
                raise ValueError(f"La estructura base de Speech ID {cfg.speech_structure_id} no tiene una versión activa configurada.")

            prompt_content = prompt_version.prompt

            stmt_crits = select(PromptCriterion).where(
                and_(
                    PromptCriterion.prompt_id == cfg.speech_structure_id,
                    PromptCriterion.is_active == True,
                    PromptCriterion.deleted_at.is_(None),
                )
            ).order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())
            res_crits = await db.execute(stmt_crits)
            active_criteria = list(res_crits.scalars().all())

            # If no active criteria exist in speech structure, mark as non-evaluable by configuration
            # per user specification: do NOT invent fallback standard criteria.
            if not active_criteria:
                logger.warning(
                    "Session %d: speech structure %d has no active criteria configured. "
                    "Marking session as completed_without_score (non-evaluable by configuration).",
                    session_id, cfg.speech_structure_id
                )
                eval_record = TrainerEvaluation(
                    session_id=session_id,
                    evaluation_config_id=config_id,
                    prompt_snapshot="No active criteria configured in speech structure",
                    result_json={
                        "error": "no_active_criteria_configured",
                        "speech_structure_id": cfg.speech_structure_id,
                        "criteria_evaluations": []
                    },
                    score=None,
                    summary="No se pudo evaluar: la estructura de evaluación no tiene criterios activos configurados.",
                    strengths={},
                    improvement_points={},
                )
                db.add(eval_record)
                sess.evaluation_status = "completed_without_score"
                sess.updated_at = datetime.now(timezone.utc)
                await db.commit()
                return

            criterios_desc = []
            criterios_keys = []
            example_evals = []

            for crit in active_criteria:
                c_key = crit.output_key or crit.criterion_key
                c_name = crit.criterion_name
                c_desc = crit.criterion_description or crit.criterion_name
                c_type = crit.criterion_type or "score_1_10"
                criterios_desc.append(f"- {c_name} (clave: '{c_key}', tipo: {c_type}): {c_desc}")
                criterios_keys.append(c_key)

            criterios_text = "\n".join(criterios_desc)

            for crit in active_criteria[:2]:
                c_slug = crit.criterion_key or (crit.output_key or "").replace("_score", "")
                example_evals.append(
                    f'    {{\n'
                    f'      "criterion_key": "{c_slug}",\n'
                    f'      "criterion_name": "{crit.criterion_name}",\n'
                    f'      "score": 8.0,\n'
                    f'      "passed": true,\n'
                    f'      "expected_behavior": "Comportamiento específico que debía mostrar el agente humano en esta simulación.",\n'
                    f'      "observed_behavior": "Comportamiento real y observable que mostró el agente humano durante la llamada.",\n'
                    f'      "evidence_quote": "Cita textual REAL y literal extraída de la transcripción.",\n'
                    f'      "relevant_turns": [1, 2],\n'
                    f'      "reasoning": "Justificación objetiva de la valoración y puntuación obtenida en este criterio.",\n'
                    f'      "improvement_tip": "Consejo concreto y accionable para mejorar en este criterio en futuras llamadas."\n'
                    f'    }}'
                )
            example_evals_str = ",\n".join(example_evals)

            # 4. Build system prompt with strict company isolation
            from app.models.companies import Company
            company = getattr(sim, "company", None) or getattr(sess, "company", None) or getattr(cfg, "company", None)
            comp_id = getattr(sim, "company_id", None) or getattr(sess, "company_id", None) or getattr(cfg, "company_id", None)

            if isinstance(comp_id, int) and (not company or not getattr(company, "company_name", None)):
                try:
                    stmt_c = select(Company).where(Company.company_id == comp_id)
                    res_c = await db.execute(stmt_c)
                    resolved_c = res_c.scalars().first()
                    if resolved_c:
                        company = resolved_c
                except Exception as e_c:
                    logger.debug("Could not resolve Company entity for evaluation: %s", e_c)

            is_demo = False
            is_healthcare = False

            if company:
                c_is_demo = getattr(company, "is_demo", None)
                c_key = getattr(company, "company_key", None)
                if c_is_demo is True or c_key == "empresa-demo" or getattr(company, "company_id", None) == 7:
                    is_demo = True
            else:
                if comp_id == 7:
                    is_demo = True
                elif isinstance(getattr(sim, "code", None), str) and any(sim.code.startswith(p) for p in ("ATEN", "VENT", "SIM-DEMO")):
                    is_demo = True

            if is_demo:
                is_healthcare = False
            elif company:
                c_sector = (getattr(company, "sector", "") or "").lower() if isinstance(getattr(company, "sector", None), str) else ""
                c_name = (getattr(company, "company_name", "") or "").lower() if isinstance(getattr(company, "company_name", None), str) else ""
                c_key = (getattr(company, "company_key", "") or "").lower() if isinstance(getattr(company, "company_key", None), str) else ""
                if c_sector in ("healthcare", "salud", "medico", "médico", "clinica", "clínica"):
                    is_healthcare = True
                elif "boston" in c_name or "boston" in c_key or getattr(company, "company_id", None) == 1:
                    is_healthcare = True
            elif comp_id == 1:
                is_healthcare = True

            # Resolve service name if available
            svc_name = None
            if sim and getattr(sim, "service", None) and isinstance(getattr(sim.service, "service_name", None), str):
                svc_name = sim.service.service_name
            elif cfg and getattr(cfg, "service", None) and isinstance(getattr(cfg.service, "service_name", None), str):
                svc_name = cfg.service.service_name

            if is_healthcare:
                company_display = (getattr(company, "brand_name", None) or getattr(company, "company_name", None)) if company and (isinstance(getattr(company, "brand_name", None), str) or isinstance(getattr(company, "company_name", None), str)) else "Boston Medical Group"
                system_intro = f"Estás evaluando una simulación de entrenamiento telefónico (roleplay) realizada por un agente de {company_display}."
                roles_context = f"""=== CONTEXTO DE ROLES ===
- La llamada es entre UN AGENTE HUMANO de {company_display} y UN PACIENTE SIMULADO por IA.
- El AGENTE HUMANO es quien se identifica como representante de {company_display}, inicia la llamada, presenta servicios y maneja objeciones.
- El PACIENTE SIMULADO es quien hace preguntas, pone objeciones y actúa como cliente o paciente potencial.
- Ignora cualquier frase introductoria del sistema como 'Perfecto, [nombre]...' o 'Iniciamos el roleplay' — estas NO son parte de la conversación real.
- Evalúa ÚNICAMENTE al agente humano. No penalices al agente por frases dichas por el paciente simulado."""
                scenario_desc = f"- Escenario/Personaje del paciente simulado: {roleplay_prompt}"
                extra_instructions_text = cfg.extra_instructions or ""
                isolation_rule = ""
            elif is_demo:
                svc_display = svc_name or "Atención al Cliente"
                company_display = "Empresa Demo"
                system_intro = f"Estás evaluando una simulación de entrenamiento telefónico (roleplay) de {svc_display} realizada por un agente de {company_display}."
                roles_context = f"""=== CONTEXTO DE ROLES ===
- La llamada es entre UN AGENTE HUMANO de {company_display} ({svc_display}) y UN CLIENTE SIMULADO por IA.
- El AGENTE HUMANO es quien atiende o inicia la llamada, se identifica cordialmente con su nombre y servicio/empresa ({company_display}), asiste al interlocutor, presenta soluciones y maneja consultas, quejas u objeciones.
- El CLIENTE SIMULADO es quien hace preguntas, expone su caso, duda o reclamación y actúa como usuario o cliente.
- Ignora cualquier frase introductoria del sistema como 'Perfecto, [nombre]...' o 'Iniciamos el roleplay' — estas NO son parte de la conversación real.
- Evalúa ÚNICAMENTE al agente humano. No penalices al agente por frases dichas por el cliente simulado.
- AISLAMIENTO CORPORATIVO ESTRICTO: Esta simulación pertenece exclusivamente a {company_display} ({svc_display}). La evaluación debe realizarse únicamente conforme al estándar y contexto de atención y soporte corporativo general. En los criterios de 'Saludo e Identificación' y 'Cierre y Despedida', el agente debe identificarse y despedirse de forma cordial y profesional conforme a {company_display} o su servicio corporativo; bajo ninguna circunstancia se debe exigir marcas ajenas ni protocolos de otros sectores o empresas."""
                scenario_desc = f"- Escenario/Personaje del cliente simulado: {roleplay_prompt}"

                # Sanitize accidental Boston Medical / clinical references if present in legacy templates
                if "boston medical" in prompt_content.lower() or "bmg" in prompt_content.lower():
                    prompt_content = re.sub(r"(?i)boston\s+medical(\s+group)?", "Empresa Demo", prompt_content)
                    prompt_content = re.sub(r"\bBMG\b", "Empresa Demo", prompt_content)
                extra_raw = cfg.extra_instructions or ""
                if "boston medical" in extra_raw.lower() or "bmg" in extra_raw.lower():
                    extra_raw = re.sub(r"(?i)boston\s+medical(\s+group)?", "Empresa Demo", extra_raw)
                    extra_raw = re.sub(r"\bBMG\b", "Empresa Demo", extra_raw)
                extra_instructions_text = extra_raw
                isolation_rule = (
                    f"\n7. DIRECTRIZ CORPORATIVA: Para {company_display}, las recomendaciones y conductas esperadas "
                    "('expected_behavior', 'improvement_tip') deben referirse exclusivamente al estándar de atención al cliente "
                    f"y a {company_display}. En ningún caso exijas marcas o protocolos de otras organizaciones."
                )
            else:
                comp_name = getattr(company, "company_name", None) if company and isinstance(getattr(company, "company_name", None), str) else "la organización"
                svc_display = svc_name or "Atención al Cliente"
                system_intro = f"Estás evaluando una simulación de entrenamiento telefónico (roleplay) realizada por un agente de {comp_name} ({svc_display})."
                roles_context = f"""=== CONTEXTO DE ROLES ===
- La llamada es entre UN AGENTE HUMANO de {comp_name} y UN CLIENTE SIMULADO por IA.
- El AGENTE HUMANO es quien se identifica como representante del servicio ({svc_display}), inicia o atiende la llamada, presenta soluciones y maneja objeciones.
- El CLIENTE SIMULADO es quien hace preguntas, expone su caso y actúa como usuario o cliente.
- Ignora cualquier frase introductoria del sistema como 'Perfecto, [nombre]...' o 'Iniciamos el roleplay' — estas NO son parte de la conversación real.
- Evalúa ÚNICAMENTE al agente humano. No penalices al agente por frases dichas por el paciente simulado."""
                scenario_desc = f"- Escenario/Personaje del cliente simulado: {roleplay_prompt}"
                extra_instructions_text = cfg.extra_instructions or ""
                isolation_rule = ""

            system_prompt = f"""{system_intro}

{roles_context}

=== INFORMACIÓN DE LA SIMULACIÓN ===
- Nombre: {sim.name}
- Objetivo: {sim.objective or 'No especificado'}
- Dificultad: {sim.difficulty or 'media'}
{scenario_desc}

=== ESTRUCTURA BASE DE EVALUACIÓN ===
\"\"\"{prompt_content}\"\"\"

=== INSTRUCCIONES ADICIONALES DEL MÓDULO TRAINER ===
\"\"\"{extra_instructions_text}\"\"\"

=== CRITERIOS OBLIGATORIOS A EVALUAR ===
Debes auditar individualmente CADA UNO de los siguientes criterios activos de la estructura:
{criterios_text}

=== REGLAS PARA LA EVALUACIÓN ===
1. 'score' (número decimal de 1.0 a 10.0): Puntuación global del desempeño del agente en la simulación.
2. 'feedback' (string detallado): Análisis pedagógico y constructivo con lo que hizo bien y recomendaciones.
3. 'strengths' (lista de strings): Puntos fuertes destacados del agente.
4. 'improvement_points' (lista de strings): Puntos concretos de mejora.
5. 'result_json' (objeto clave-valor): Valor obtenido para cada criterio usando su clave ('output_key' descrita arriba).
6. 'criteria_evaluations' (lista exhaustiva de evidencias por criterio):
   - DEBE contener obligatoriamente un objeto para CADA uno de los criterios listados arriba.
   - 'criterion_key': slug identificador del criterio.
   - 'criterion_name': nombre exacto del criterio.
   - 'score': número decimal de 1.0 a 10.0 obtenido en este criterio.
   - 'passed': booleano indicando si superó el criterio (true si score >= 6.0, false si menor).
   - 'expected_behavior': qué conducta se esperaba del agente humano.
   - 'observed_behavior': qué hizo o dijo realmente el agente en la llamada.
   - 'evidence_quote': cita textual literal de la llamada que demuestra el comportamiento (o 'Sin evidencia suficiente en la conversación').
   - 'relevant_turns': lista de enteros con números de turno de la evidencia.
   - 'reasoning': justificación breve y objetiva de la nota en este criterio.
   - 'improvement_tip': recomendación accionable de mejora para este criterio.{isolation_rule}

=== FORMATO DE SALIDA ESTRICTO ===
Devuelve EXCLUSIVAMENTE un objeto JSON válido (sin markdown ```json ni texto adicional):
{{
  "score": 8.5,
  "feedback": "El agente demostró buena capacidad de escucha y empatía...",
  "strengths": ["Escucha activa adecuada", "Saludo institucional formal"],
  "improvement_points": ["Reforzar el cierre formal y comprobación de dudas"],
  "result_json": {{
    "{criterios_keys[0]}": 8.5
  }},
  "criteria_evaluations": [
{example_evals_str}
  ]
}}
"""

            user_prompt = f"Transcripción de la llamada telefónica:\n\n{transcript_text}"

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            # 5. Call AI completion
            logger.info("Calling AI completion for session %d...", session_id)
            raw_response = await openai_service.complete_text(
                messages=messages,
                response_format="json_object",
            )

            # 6. Parse and extract results
            parsed_res = safe_parse_json(raw_response)
            if not parsed_res:
                raise ValueError(f"La IA no devolvió un JSON válido. Respuesta cruda: {raw_response[:500]}")

            # Normalize criteria_evaluations
            raw_crit_evals = parsed_res.get("criteria_evaluations")
            normalized_crit_evals = []
            if isinstance(raw_crit_evals, list):
                for item in raw_crit_evals:
                    if isinstance(item, dict):
                        crit_score = None
                        if item.get("score") is not None:
                            try:
                                s_clean = re.sub(r"\s*/\s*10.*$", "", str(item["score"])).replace(",", ".").strip()
                                crit_score = round(float(s_clean), 2)
                            except Exception:
                                pass
                        passed = item.get("passed")
                        if passed is None and crit_score is not None:
                            passed = (crit_score >= 6.0)

                        normalized_item = {
                            "criterion_key": item.get("criterion_key") or "",
                            "criterion_name": item.get("criterion_name") or "",
                            "score": crit_score,
                            "passed": bool(passed) if passed is not None else None,
                            "expected_behavior": item.get("expected_behavior") or "",
                            "observed_behavior": item.get("observed_behavior") or "",
                            "evidence_quote": item.get("evidence_quote") or "Sin evidencia suficiente en la conversación",
                            "relevant_turns": item.get("relevant_turns") or [],
                            "reasoning": item.get("reasoning") or item.get("feedback") or "",
                            "improvement_tip": item.get("improvement_tip") or "",
                        }
                        normalized_crit_evals.append(normalized_item)

            # Robust score extraction
            score_decimal = TrainerService._extract_robust_score(parsed_res, normalized_crit_evals)

            # Fallback criteria averaging if top-level score was None
            if score_decimal is None:
                numeric_scores = []
                for crit in active_criteria:
                    out_k = crit.output_key
                    if out_k and out_k in parsed_res:
                        try:
                            numeric_scores.append(float(str(parsed_res[out_k]).replace("%", "").strip()))
                        except (ValueError, TypeError):
                            pass
                if numeric_scores:
                    avg = sum(numeric_scores) / len(numeric_scores)
                    score_decimal = Decimal(str(round(avg, 2)))
                    logger.info(
                        "Session %d: computed criteria average: %s (from %d criteria).",
                        session_id, score_decimal, len(numeric_scores)
                    )

            # Synchronize flat result_json keys with criteria_evaluations
            for crit in active_criteria:
                out_k = crit.output_key
                feed_k = crit.feed_key
                c_slug = crit.criterion_key or (out_k or "").replace("_score", "")
                matched_eval = None
                for ce in normalized_crit_evals:
                    if ce["criterion_key"] in (c_slug, out_k, crit.criterion_key) or \
                       (ce["criterion_name"] and ce["criterion_name"].lower() == crit.criterion_name.lower()):
                        matched_eval = ce
                        break

                if matched_eval:
                    if out_k and out_k not in parsed_res:
                        if crit.criterion_type == "score_1_10":
                            parsed_res[out_k] = matched_eval["score"]
                        elif crit.criterion_type == "boolean":
                            parsed_res[out_k] = matched_eval["passed"]
                        else:
                            parsed_res[out_k] = matched_eval["score"] if matched_eval["score"] is not None else matched_eval["passed"]
                    if feed_k and feed_k not in parsed_res:
                        parsed_res[feed_k] = matched_eval["reasoning"] or matched_eval["improvement_tip"]
                elif out_k and out_k in parsed_res:
                    val = parsed_res[out_k]
                    feed_val = parsed_res.get(feed_k, "") if feed_k else ""
                    val_score = None
                    try:
                        val_score = float(str(val).replace("%", "").strip())
                    except Exception:
                        pass
                    normalized_crit_evals.append({
                        "criterion_key": c_slug,
                        "criterion_name": crit.criterion_name,
                        "score": val_score,
                        "passed": (val_score >= 6.0) if val_score is not None else bool(val),
                        "expected_behavior": f"Cumplir con {crit.criterion_name}",
                        "observed_behavior": str(feed_val) if feed_val else "Observado en llamada",
                        "evidence_quote": "Sin evidencia textual detallada",
                        "relevant_turns": [],
                        "reasoning": str(feed_val) if feed_val else "",
                        "improvement_tip": "",
                    })

            parsed_res["criteria_evaluations"] = normalized_crit_evals

            summary = parsed_res.get("feedback") or parsed_res.get("summary")
            if not summary:
                feedback_parts = [
                    str(v).strip()
                    for k, v in parsed_res.items()
                    if (k.startswith("feedback_") or k.endswith("_feedback") or k.endswith("_fb"))
                    and isinstance(v, str) and v.strip()
                ]
                if feedback_parts:
                    summary = " ".join(feedback_parts[:3])

            strengths = parsed_res.get("puntos_fuertes") or parsed_res.get("strengths") or {}
            improvement = parsed_res.get("puntos_mejora") or parsed_res.get("improvement_points") or {}

            # Save TrainerEvaluation
            eval_record = TrainerEvaluation(
                session_id=session_id,
                evaluation_config_id=config_id,
                prompt_snapshot=system_prompt,
                result_json=parsed_res,
                score=score_decimal,
                summary=summary,
                strengths=strengths if isinstance(strengths, dict) else {"text": str(strengths)},
                improvement_points=improvement if isinstance(improvement, dict) else {"text": str(improvement)},
            )
            db.add(eval_record)

            # Update session evaluation_status
            if score_decimal is None:
                sess.evaluation_status = "completed_without_score"
                logger.warning("Session %d evaluated but completed without score (insufficient scoring data).", session_id)
            else:
                sess.evaluation_status = "evaluated"
                logger.info("Session %d evaluated successfully with score %s.", session_id, score_decimal)
            sess.updated_at = datetime.now(timezone.utc)
            await db.commit()

        except Exception as e:
            logger.exception("Evaluation execution failed for session %d: %s", session_id, e)
            sess.evaluation_status = "evaluation_error"
            sess.updated_at = datetime.now(timezone.utc)

            eval_config_id = None
            if 'sim' in locals() and sim:
                eval_config_id = sim.evaluation_config_id

            # Save evaluation record with error
            eval_record = TrainerEvaluation(
                session_id=session_id,
                evaluation_config_id=eval_config_id,
                prompt_snapshot="Execution failed",
                result_json={"error": str(e)},
                error_message=str(e),
            )
            db.add(eval_record)
            await db.commit()


    # ── Querying Trainer Sessions ─────────────────────────────────────────────────

    @staticmethod
    async def list_sessions(
        db: AsyncSession,
        agent_id: Optional[str] = None,
        service_id: Optional[int] = None,
        simulation_id: Optional[int] = None,
        status: Optional[str] = None,
        evaluation_status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        min_score: Optional[Decimal] = None,
        max_score: Optional[Decimal] = None,
        limit: int = 100,
        company_ids: Optional[List[int]] = None,
        allowed_service_ids: Optional[List[int]] = None,
        agent_ids: Optional[List[str]] = None,
    ) -> tuple[List[TrainerSession], int]:
        stmt = select(TrainerSession).join(TrainerSimulation, TrainerSession.simulation_id == TrainerSimulation.simulation_id)
        
        # Build filters
        filters = []
        if agent_id:
            filters.append(TrainerSession.agent_id == agent_id)
        if agent_ids is not None:
            filters.append(TrainerSession.agent_id.in_(agent_ids))
        if service_id is not None:
            filters.append(TrainerSession.service_id == service_id)
        if allowed_service_ids is not None:
            filters.append(TrainerSession.service_id.in_(allowed_service_ids))
        if company_ids is not None:
            filters.append(TrainerSession.company_id.in_(company_ids))
        if simulation_id is not None:
            filters.append(TrainerSession.simulation_id == simulation_id)
        if status:
            filters.append(TrainerSession.status == status)
        if evaluation_status:
            filters.append(TrainerSession.evaluation_status == evaluation_status)
        if date_from:
            filters.append(TrainerSession.started_at >= date_from)
        if date_to:
            filters.append(TrainerSession.started_at <= date_to)

        if min_score is not None or max_score is not None:
            stmt = stmt.outerjoin(TrainerEvaluation, TrainerSession.session_id == TrainerEvaluation.session_id)
            if min_score is not None:
                filters.append(TrainerEvaluation.score >= min_score)
            if max_score is not None:
                filters.append(TrainerEvaluation.score <= max_score)

        if filters:
            stmt = stmt.where(and_(*filters))

        # Count total matches
        stmt_count = select(func.count(TrainerSession.session_id))
        if filters:
            stmt_count = stmt_count.where(and_(*filters))
        res_count = await db.execute(stmt_count)
        total_count = res_count.scalar() or 0

        # Sort and limit
        from sqlalchemy.orm import selectinload
        stmt = (
            stmt
            .options(
                selectinload(TrainerSession.simulation)
            )
            .order_by(desc(TrainerSession.started_at))
            .limit(limit)
        )
        res = await db.execute(stmt)
        sessions = list(res.scalars().all())

        if sessions:
            session_ids = [s.session_id for s in sessions]

            # Eager-load evaluations
            stmt_evals = select(TrainerEvaluation).where(TrainerEvaluation.session_id.in_(session_ids))
            res_evals = await db.execute(stmt_evals)
            evals_map = {e.session_id: e for e in res_evals.scalars().all()}

            # Eager-load agent settings for agent_name
            agent_codes = list({s.agent_code for s in sessions if s.agent_code})
            agent_name_map: dict = {}
            if agent_codes:
                stmt_agents = select(TrainingAgentSetting).where(
                    TrainingAgentSetting.training_code.in_(agent_codes)
                )
                res_agents = await db.execute(stmt_agents)
                for ag in res_agents.scalars().all():
                    agent_name_map[ag.training_code] = ag.agent_name

            # Eager-load service names via simulation
            service_ids = list({s.simulation.service_id for s in sessions if s.simulation and s.simulation.service_id})
            service_name_map: dict = {}
            if service_ids:
                from app.models.services import Service
                stmt_svcs = select(Service).where(Service.service_id.in_(service_ids))
                res_svcs = await db.execute(stmt_svcs)
                for svc in res_svcs.scalars().all():
                    service_name_map[svc.service_id] = svc.service_name

            # Eager-load configs and active criteria to map scores in batch
            prompt_ids = set()
            config_ids = {e.evaluation_config_id for e in evals_map.values() if e.evaluation_config_id}
            for s in sessions:
                if s.simulation and s.simulation.evaluation_config_id:
                    config_ids.add(s.simulation.evaluation_config_id)
            config_map = {}
            if config_ids:
                stmt_cfgs = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id.in_(list(config_ids)))
                res_cfgs = await db.execute(stmt_cfgs)
                for cfg in res_cfgs.scalars().all():
                    config_map[cfg.config_id] = cfg
                    prompt_ids.add(cfg.speech_structure_id)
            criteria_map = {}
            if prompt_ids:
                stmt_crits = select(PromptCriterion).where(
                    PromptCriterion.prompt_id.in_(list(prompt_ids)),
                    PromptCriterion.is_active == True,
                    PromptCriterion.deleted_at.is_(None)
                ).order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())
                res_crits = await db.execute(stmt_crits)
                for crit in res_crits.scalars().all():
                    criteria_map.setdefault(crit.prompt_id, []).append(crit)

            for s in sessions:
                s.evaluation = evals_map.get(s.session_id)
                # Attach denormalised fields as transient attributes
                s.__dict__["agent_name"] = agent_name_map.get(s.agent_code)
                sim = s.simulation
                s.__dict__["simulation_name"] = sim.name if sim else None
                s.__dict__["simulation_code"] = sim.code if sim else None
                s.__dict__["service_name"] = service_name_map.get(sim.service_id) if sim else None

                cfg = config_map.get(s.evaluation.evaluation_config_id) if s.evaluation else None
                if not cfg and sim:
                    cfg = config_map.get(sim.evaluation_config_id)
                active_crits = criteria_map.get(cfg.speech_structure_id) if cfg else None

                await TrainerService._map_session_evaluation_details(
                    db,
                    s,
                    active_criteria=active_crits,
                    config=cfg
                )
        else:
            for s in sessions:
                s.evaluation = None

        return sessions, total_count

    @staticmethod
    async def get_session_detail(db: AsyncSession, session_id: int) -> Optional[TrainerSession]:
        stmt = select(TrainerSession).where(TrainerSession.session_id == session_id)
        res = await db.execute(stmt)
        session = res.scalars().first()
        if not session:
            return None
            
        # Eager load simulation, evaluation
        stmt_sim = select(TrainerSimulation).where(TrainerSimulation.simulation_id == session.simulation_id)
        res_sim = await db.execute(stmt_sim)
        session.simulation = res_sim.scalars().first()

        stmt_eval = select(TrainerEvaluation).where(TrainerEvaluation.session_id == session.session_id)
        res_eval = await db.execute(stmt_eval)
        session.evaluation = res_eval.scalars().first()
        
        # Populate denormalized fields
        # Fetch agent_name
        stmt_agent = select(TrainingAgentSetting.agent_name).where(
            TrainingAgentSetting.training_code == session.agent_code
        )
        res_agent = await db.execute(stmt_agent)
        session.__dict__["agent_name"] = res_agent.scalar()

        if session.simulation:
            session.__dict__["simulation_name"] = session.simulation.name
            session.__dict__["simulation_code"] = session.simulation.code
            
            # Fetch service_name
            from app.models.services import Service
            stmt_svc = select(Service.service_name).where(Service.service_id == session.simulation.service_id)
            res_svc = await db.execute(stmt_svc)
            session.__dict__["service_name"] = res_svc.scalar()

        # Map detail evaluation structure and fields
        await TrainerService._map_session_evaluation_details(db, session)
        
        return session

    @staticmethod
    def _map_trainer_criteria_scores(result_json: dict | None, active_criteria: list) -> list[dict]:
        if not result_json:
            result_json = {}
        criteria_scores = []
        
        _TRUE_VALUES = {"si", "sí", "yes", "true", "1", True}
        _FALSE_VALUES = {"no", "false", "0", False}
        
        crit_evals_list = result_json.get("criteria_evaluations") if isinstance(result_json.get("criteria_evaluations"), list) else []

        for crit in active_criteria:
            output_key = crit.output_key
            feed_key = crit.feed_key
            item_type = crit.criterion_type or "text"
            is_score = (item_type == "score_1_10")
            max_score = 10 if is_score else None
            
            raw_val = result_json.get(output_key) if output_key else None
            raw_feedback = result_json.get(feed_key) if feed_key else None
            
            # Check in criteria_evaluations if not found in flat result_json
            matched_ce = None
            c_slug = crit.criterion_key or (output_key or "").replace("_score", "")
            for ce in crit_evals_list:
                if isinstance(ce, dict):
                    if (ce.get("criterion_key") and ce["criterion_key"] in (c_slug, output_key, crit.criterion_key)) or \
                       (ce.get("criterion_name") and ce["criterion_name"].lower() == crit.criterion_name.lower()):
                        matched_ce = ce
                        break

            if raw_val is None and matched_ce:
                if is_score:
                    raw_val = matched_ce.get("score")
                elif item_type == "boolean":
                    raw_val = matched_ce.get("passed")
                else:
                    raw_val = matched_ce.get("score") if matched_ce.get("score") is not None else matched_ce.get("passed")

            if raw_feedback is None and matched_ce:
                raw_feedback = matched_ce.get("reasoning") or matched_ce.get("improvement_tip")

            # Coerce/Extract value and display_value
            value = None
            score = None
            display_value = None
            
            if raw_val is None:
                display_value = "No evaluable"
            else:
                if item_type in ("score_1_10", "percentage", "number"):
                    try:
                        val_str = str(raw_val).replace("%", "").strip()
                        val_str = re.sub(r"\s*/\s*10.*$", "", val_str)
                        coerced_val = float(val_str)
                        value = coerced_val
                        if is_score:
                            score = coerced_val
                            display_value = f"{int(coerced_val)}/10" if coerced_val.is_integer() else f"{coerced_val}/10"
                        elif item_type == "percentage":
                            display_value = f"{coerced_val}%"
                        else:
                            display_value = str(coerced_val)
                    except (ValueError, TypeError):
                        value = raw_val
                        display_value = str(raw_val)
                elif item_type == "boolean":
                    if isinstance(raw_val, bool):
                        value = raw_val
                        display_value = "Sí" if raw_val else "No"
                    else:
                        normalized = str(raw_val).strip().lower()
                        if normalized in _TRUE_VALUES:
                            value = True
                            display_value = "Sí"
                        elif normalized in _FALSE_VALUES:
                            value = False
                            display_value = "No"
                        else:
                            value = raw_val
                            display_value = str(raw_val)
                else: # text, category, etc.
                    value = raw_val
                    display_value = str(raw_val)
                    
            # Parse feedback
            feedback = None
            if raw_feedback is not None:
                if isinstance(raw_feedback, dict):
                    feedback = raw_feedback.get("text") or str(raw_feedback)
                else:
                    feedback = str(raw_feedback)
                    
            criteria_scores.append({
                "criterion_id": crit.criterion_id,
                "criterion_name": crit.criterion_name,
                "output_key": output_key,
                "feed_key": feed_key,
                "item_type": item_type,
                "score": score,
                "max_score": max_score,
                "value": value,
                "feedback": feedback,
                "display_value": display_value,
                "is_score": is_score,
                "expected_behavior": matched_ce.get("expected_behavior") if matched_ce else None,
                "observed_behavior": matched_ce.get("observed_behavior") if matched_ce else None,
                "evidence_quote": matched_ce.get("evidence_quote") if matched_ce else None,
                "relevant_turns": matched_ce.get("relevant_turns") if matched_ce else [],
                "reasoning": matched_ce.get("reasoning") if matched_ce else None,
                "improvement_tip": matched_ce.get("improvement_tip") if matched_ce else None,
            })
            
        return criteria_scores

    @staticmethod
    async def _map_session_evaluation_details(
        db: AsyncSession,
        session: TrainerSession,
        active_criteria: Optional[List[Any]] = None,
        config: Optional[TrainerEvaluationConfig] = None
    ) -> None:
        # 1. Alias basic status/transcription
        session.__dict__["call_status"] = session.status
        session.__dict__["transcription"] = session.transcript
        
        # 2. Defaults for evaluation fields
        session.__dict__["score"] = None
        session.__dict__["score_max"] = None
        session.__dict__["score_source"] = "none"
        session.__dict__["evaluation_summary"] = None
        session.__dict__["criteria_scores"] = []
        session.__dict__["criteria_evaluations"] = []
        session.__dict__["extraction_values"] = {}
        session.__dict__["score_items"] = []
        session.__dict__["non_score_items"] = []
        session.__dict__["evaluation_json"] = {}
        
        session.__dict__["evaluation_config_id"] = None
        session.__dict__["evaluation_config_name"] = None
        session.__dict__["speech_structure_id"] = None
        session.__dict__["speech_structure_name"] = None
        
        evaluation = session.evaluation
        
        # 3. Resolve evaluation_config_id
        config_id = None
        if evaluation and evaluation.evaluation_config_id:
            config_id = evaluation.evaluation_config_id
        elif session.simulation and session.simulation.evaluation_config_id:
            config_id = session.simulation.evaluation_config_id
            
        # 4. Resolve Config and Speech Structure details
        if config_id:
            session.__dict__["evaluation_config_id"] = config_id
            if not config:
                stmt_cfg = select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == config_id)
                res_cfg = await db.execute(stmt_cfg)
                config = res_cfg.scalars().first()
                
            if config:
                session.__dict__["evaluation_config_name"] = config.name
                session.__dict__["speech_structure_id"] = config.speech_structure_id
                session.__dict__["speech_structure_name"] = config.speech_structure_name
                
        # 5. Fetch active criteria if not passed
        if active_criteria is None and session.__dict__["speech_structure_id"]:
            stmt_crits = select(PromptCriterion).where(
                PromptCriterion.prompt_id == session.__dict__["speech_structure_id"],
                PromptCriterion.is_active == True,
                PromptCriterion.deleted_at.is_(None)
            ).order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())
            res_crits = await db.execute(stmt_crits)
            active_criteria = list(res_crits.scalars().all())
            
        # 6. Map evaluation details
        if evaluation:
            result_json = evaluation.result_json or {}
            session.__dict__["evaluation_json"] = result_json
            session.__dict__["evaluation_summary"] = evaluation.summary
            session.__dict__["criteria_evaluations"] = result_json.get("criteria_evaluations") or []
            
            # Map criteria_scores if we have active criteria
            if active_criteria:
                scores = TrainerService._map_trainer_criteria_scores(result_json, active_criteria)
                session.__dict__["criteria_scores"] = scores
                session.__dict__["score_items"] = [item for item in scores if item["is_score"]]
                session.__dict__["non_score_items"] = [item for item in scores if not item["is_score"]]
                session.__dict__["extraction_values"] = {item["output_key"]: item["value"] for item in scores if item.get("output_key")}
                
            # Score resolution logic
            if evaluation.score is not None:
                session.__dict__["score"] = float(evaluation.score)
                session.__dict__["score_max"] = 10
                session.__dict__["score_source"] = "evaluation_score"
            else:
                # Calculate average of numeric score_1_10 criteria
                score_items = session.__dict__.get("score_items", [])
                numeric_scores = [item["score"] for item in score_items if item["score"] is not None]
                if numeric_scores:
                    avg_score = sum(numeric_scores) / len(numeric_scores)
                    session.__dict__["score"] = round(avg_score, 2)
                    session.__dict__["score_max"] = 10
                    session.__dict__["score_source"] = "criteria_average"
                    
        # 7. Check for completed_without_score status
        if session.evaluation_status in ("evaluated", "completed_without_score"):
            if session.__dict__["score"] is None:
                session.evaluation_status = "completed_without_score"
            else:
                session.evaluation_status = "evaluated"
