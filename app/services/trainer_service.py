"""Service class for managing Trainer simulations, versions, evaluations, and phone sessions."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional
from sqlalchemy import select, and_, or_, desc, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)
from app.models.personalized_training import TrainingAgentSetting
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

        config = TrainerEvaluationConfig(
            name=payload.name,
            service_id=payload.service_id,
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
        db: AsyncSession, service_id: Optional[int] = None, is_active: Optional[bool] = None
    ) -> List[TrainerEvaluationConfig]:
        stmt = select(TrainerEvaluationConfig)
        filters = []
        if service_id is not None:
            filters.append(TrainerEvaluationConfig.service_id == service_id)
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


    # ── Simulations ───────────────────────────────────────────────────────────────

    @staticmethod
    async def create_simulation(
        db: AsyncSession, payload: TrainerSimulationCreate, created_by: Optional[str] = None
    ) -> TrainerSimulation:
        # Check code uniqueness
        stmt_check = select(TrainerSimulation).where(TrainerSimulation.code == payload.code.strip())
        res_check = await db.execute(stmt_check)
        if res_check.scalars().first():
            raise ValueError(f"El código de simulación '{payload.code}' ya existe de manera global.")

        if payload.evaluation_config_id:
            # Validate config
            cfg = await TrainerService.get_evaluation_config(db, payload.evaluation_config_id)
            if not cfg:
                raise ValueError("La configuración de evaluación seleccionada no existe.")
            if cfg.service_id != payload.service_id:
                raise ValueError("La configuración de evaluación seleccionada no pertenece al mismo servicio.")

        sim = TrainerSimulation(
            name=payload.name,
            code=payload.code.strip(),
            service_id=payload.service_id,
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
            code_clean = payload.code.strip()
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
    ) -> List[TrainerSimulation]:
        stmt = select(TrainerSimulation)
        filters = []
        if service_id is not None:
            filters.append(TrainerSimulation.service_id == service_id)
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
    async def generate_roleplay_prompt_ai(payload: AIPromptGenerateRequest) -> str:
        system_instruction = (
            "Eres un experto en redactar prompts de juego de rol (roleplay) inmersivos en español para simulaciones de voz interactiva.\n"
            "Tu tarea es diseñar un prompt para un modelo de lenguaje que simulará a un paciente o cliente llamando a una clínica de Boston Medical Group.\n"
            "El prompt debe ser muy detallado e instruir al modelo sobre:\n"
            "1. Su nombre, edad y contexto clínico ficticio acorde al servicio.\n"
            "2. Su personalidad, estado emocional (ej. ansioso, tímido, impaciente) y tono.\n"
            "3. Pautas de conversación: responder de forma natural, dar respuestas cortas típicas de llamadas telefónicas, interrumpir si el agente habla demasiado, simular vacilaciones.\n"
            "4. Sus objeciones principales que el agente telefónico debe resolver.\n"
            "5. Reglas estrictas de juego: nunca salirse del personaje de paciente, colgar limpiamente llamando al tool `hangup_call` cuando el roleplay sea exitoso o si el agente es grosero.\n"
            "Devuelve única y exclusivamente el texto final del prompt listo para ser copiado y guardado, sin formato markdown ni texto introductorio."
        )
        user_message = (
            f"Por favor genera un prompt de roleplay basado en los siguientes parámetros:\n"
            f"- Servicio ID: {payload.service_id}\n"
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
        cleaned = agent_code.replace(" ", "").upper()
        stmt = select(TrainingAgentSetting).where(
            and_(
                func.upper(TrainingAgentSetting.training_code) == cleaned,
                TrainingAgentSetting.is_enabled == True,
                TrainingAgentSetting.training_code_enabled == True,
            )
        )
        res = await db.execute(stmt)
        setting = res.scalars().first()
        if not setting:
            return None
        return {
            "agent_id": setting.hubspot_owner_id,
            "agent_name": setting.agent_name,
            "agent_initials": setting.agent_initials,
        }

    @staticmethod
    async def validate_simulation_code(db: AsyncSession, simulation_code: str) -> Optional[TrainerSimulation]:
        cleaned = simulation_code.replace(" ", "").upper()
        stmt = select(TrainerSimulation).where(
            and_(
                func.upper(TrainerSimulation.code) == cleaned,
                TrainerSimulation.status == "published",
            )
        )
        res = await db.execute(stmt)
        return res.scalars().first()

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
                from app.services.twilio_service import TwilioService
                twilio = TwilioService()
                audio_bytes = await twilio.download_audio(sess.recording_url)
                
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

            # 3. Retrieve Speech evaluation structure template
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

            # 4. Build prompt
            system_prompt = (
                f"Estás evaluando una simulación de entrenamiento telefónico (roleplay) realizada por un agente.\n"
                f"Estructura de evaluación base:\n\"\"\"\n{prompt_content}\n\"\"\"\n\n"
                f"Instrucciones adicionales del módulo Trainer:\n\"\"\"\n{cfg.extra_instructions or ''}\n\"\"\"\n\n"
                f"Información de la simulación realizada:\n"
                f"- Nombre: {sim.name}\n"
                f"- Objetivo: {sim.objective or 'No especificado'}\n"
                f"- Escenario/Personaje: {roleplay_prompt}\n\n"
                f"Devuelve exclusivamente un objeto JSON válido que cumpla con el formato de salida JSON especificado en la estructura base. "
                f"No agregues texto explicativo ni bloques de código de markdown. Asegúrate de incluir los campos 'score' (o 'evaluacion_global'), "
                f"'summary' (o 'feedback'), 'strengths' y 'improvement_points' en el JSON."
            )

            user_prompt = f"Transcripción de la llamada telefónica:\n\n{transcript_text}"

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            # 5. Call OpenAI Text Completion
            logger.info("Calling OpenAI Text Completion for session %d...", session_id)
            raw_response = await openai_service.complete_text(
                messages=messages,
                response_format="json_object",
            )

            # 6. Parse and extract results
            parsed_res = safe_parse_json(raw_response)
            if not parsed_res:
                raise ValueError(f"La IA no devolvió un JSON válido. Respuesta cruda: {raw_response[:500]}")

            score_raw = parsed_res.get("evaluacion_global") or parsed_res.get("score")
            score_decimal = None
            if score_raw is not None:
                try:
                    score_decimal = Decimal(str(score_raw))
                except Exception:
                    logger.warning("Failed to parse evaluation score %s as decimal.", score_raw)

            summary = parsed_res.get("feedback") or parsed_res.get("summary")
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
            
            # Update session status
            sess.evaluation_status = "evaluated"
            sess.updated_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Session %d evaluated successfully with score %s.", session_id, score_decimal)

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
    ) -> tuple[List[TrainerSession], int]:
        stmt = select(TrainerSession).join(TrainerSimulation, TrainerSession.simulation_id == TrainerSimulation.simulation_id)
        
        # Build filters
        filters = []
        if agent_id:
            filters.append(TrainerSession.agent_id == agent_id)
        if service_id is not None:
            filters.append(TrainerSession.service_id == service_id)
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
        stmt = stmt.order_by(desc(TrainerSession.started_at)).limit(limit)
        res = await db.execute(stmt)
        sessions = list(res.scalars().all())

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
        
        return session
