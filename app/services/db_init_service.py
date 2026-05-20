"""Database initialization service for prompt base structures."""
import logging
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.prompts import PromptBaseStructure, PromptVersion, Prompt

logger = logging.getLogger(__name__)

DEFAULT_STRUCTURES = [
    {
        "structure_key": "boston_medical_audio",
        "structure_name": "Boston Medical - Audio comercial",
        "description": "Estructura de prompt base original de Boston Medical para audios comerciales.",
        "prompt_type": "audio",
        "base_prompt": "",  # Will be populated dynamically or fallback
        "default_criteria": None,
        "is_active": True,
    },
    {
        "structure_key": "boston_medical_appointment",
        "structure_name": "Boston Medical - Confirmación de cita",
        "description": "Evaluación de llamadas para confirmación de citas en clínicas de Boston Medical.",
        "prompt_type": "audio",
        "base_prompt": (
            "### CONFIRMACIÓN DE CITA - BOSTON MEDICAL\n"
            "Eres un evaluador de llamadas de confirmación de cita de Boston Medical. "
            "Debes verificar si el agente confirma correctamente la fecha, hora, especialista y dirección de la cita con el paciente, "
            "manteniendo un tono profesional y empático.\n\n"
            "### FORMATO DE SALIDA JSON\n"
            "Devuelve la información estructurada en JSON incluyendo la clasificación de la llamada y justificaciones del agente."
        ),
        "default_criteria": None,
        "is_active": True,
    },
    {
        "structure_key": "generic_customer_service",
        "structure_name": "Atención al cliente genérico",
        "description": "Estructura estándar para evaluar calidad de servicio y atención al cliente en llamadas comerciales de soporte.",
        "prompt_type": "audio",
        "base_prompt": (
            "### ATENCIÓN AL CLIENTE GENÉRICO\n"
            "Analiza la interacción de atención al cliente. "
            "Evalúa la cortesía, la capacidad de resolución de problemas, el tiempo de respuesta y la claridad de la información proporcionada por el agente.\n\n"
            "### FORMATO DE SALIDA JSON\n"
            "Devuelve la evaluación detallada en un formato JSON estructurado."
        ),
        "default_criteria": None,
        "is_active": True,
    },
    {
        "structure_key": "commercial_quality",
        "structure_name": "Evaluación de calidad comercial",
        "description": "Estructura para analizar técnicas de venta, manejo de objeciones comerciales y efectividad de cierre.",
        "prompt_type": "audio",
        "base_prompt": (
            "### EVALUACIÓN DE CALIDAD COMERCIAL\n"
            "Evalúa el desempeño comercial en la llamada. "
            "Analiza el manejo de objeciones, la presentación de la propuesta de valor, las técnicas de cierre y la efectividad general del agente comercial.\n\n"
            "### FORMATO DE SALIDA JSON\n"
            "Devuelve los resultados estructurados en un formato JSON con valoraciones numéricas o booleanas y sus correspondientes explicaciones."
        ),
        "default_criteria": None,
        "is_active": True,
    },
    {
        "structure_key": "blank",
        "structure_name": "Prompt desde cero",
        "description": "Crea un prompt vacío sin criterios iniciales.",
        "prompt_type": "audio",
        "base_prompt": "",
        "default_criteria": None,
        "is_active": True,
    }
]

FALLBACK_BOSTON_PROMPT = (
    "### ESTRUCTURA DE PROMPT BASE - BOSTON MEDICAL\n"
    "Eres un analizador experto de llamadas comerciales para Boston Medical Group (clínica de salud sexual masculina). "
    "Tu tarea es evaluar el desempeño del agente telefónico en base a los criterios definidos y clasificar la llamada.\n\n"
    "### FORMATO DE SALIDA JSON\n"
    "Debes devolver la evaluación en formato JSON estructurado, incluyendo la clasificación del tipo de llamada y el valor/justificación de cada criterio."
)


async def init_db():
    """
    Initialize prompt base structures table and populate default records.
    Fully idempotent and non-destructive.
    """
    logger.info("Starting db_init_service initialization...")
    try:
        engine = get_engine()
        
        # 1. Create table if not exists via Base.metadata
        async with engine.begin() as conn:
            # Check if table already exists first
            res = await conn.execute(
                text("SELECT EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'bm_prompt_base_structures');")
            )
            table_exists = res.scalar()
            if table_exists:
                logger.info("Table 'bm_prompt_base_structures' already exists.")
            else:
                logger.info("Table 'bm_prompt_base_structures' does not exist. Creating via SQLAlchemy metadata...")
                await conn.run_sync(Base.metadata.create_all)
                logger.info("Table 'bm_prompt_base_structures' created successfully.")

        # 1.1. Create mass evaluation tables if not exists via Base.metadata
        async with engine.begin() as conn:
            res = await conn.execute(
                text("SELECT EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'bm_mass_evaluation_jobs');")
            )
            mass_tables_exist = res.scalar()
            if mass_tables_exist:
                logger.info("Mass evaluation tables already exist.")
            else:
                logger.info("Mass evaluation tables do not exist. Creating via SQLAlchemy metadata...")
                await conn.run_sync(Base.metadata.create_all)
                logger.info("Mass evaluation tables created successfully.")

        # 1.5. Ensure columns exist on bm_prompts table dynamically and non-destructively
        async with engine.begin() as conn:
            for col_name, col_type in [
                ("base_structure_id", "INTEGER"),
                ("base_structure_key", "TEXT"),
                ("base_structure_name", "TEXT"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = 'bm_prompts' 
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                col_exists = res.scalar()
                if not col_exists:
                    logger.info("Adding column '%s' to 'bm_prompts' table...", col_name)
                    await conn.execute(
                        text(f"ALTER TABLE bm_prompts ADD COLUMN {col_name} {col_type} NULL;")
                    )
                    logger.info("Column '%s' added successfully.", col_name)
                else:
                    logger.info("Column '%s' already exists on 'bm_prompts' table.", col_name)


        # 2. Safe backfill: Force default_criteria to NULL on ALL base structures.
        # This runs in its own isolated transaction so it always commits, regardless of
        # any failures in subsequent seeding steps.
        async with engine.begin() as conn:
            logger.info("Executing isolated backfill: SET default_criteria = NULL for all bm_prompt_base_structures...")
            result = await conn.execute(
                text("UPDATE bm_prompt_base_structures SET default_criteria = NULL WHERE default_criteria IS NOT NULL;")
            )
            logger.info("Backfill complete. Rows updated: %d", result.rowcount)

        # 3. Seed structures in a safe, non-destructive session
        from app.dependencies import get_db
        async with AsyncSession(engine) as db:
            # Populate boston_medical_audio dynamic base prompt if possible
            boston_audio_struct = DEFAULT_STRUCTURES[0]
            try:
                # Query active prompt 1 current version
                result = await db.execute(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_id == 1, PromptVersion.is_current == True)
                    .limit(1)
                )
                v = result.scalars().first()
                if v and v.prompt:
                    boston_audio_struct["base_prompt"] = v.prompt
                    logger.info("Loaded default boston_medical_audio base prompt from active prompt version 1.")
                else:
                    boston_audio_struct["base_prompt"] = FALLBACK_BOSTON_PROMPT
                    logger.info("No active version found for prompt 1. Using fallback Boston Medical base prompt.")
            except Exception as e:
                if not boston_audio_struct["base_prompt"]:
                    boston_audio_struct["base_prompt"] = FALLBACK_BOSTON_PROMPT
                logger.warning("Error fetching active prompt version 1 for seeding: %s. Using default fallback.", e)

            # Insert default records if structure_key doesn't exist
            for struct_data in DEFAULT_STRUCTURES:
                key = struct_data["structure_key"]
                
                # Check if exists
                stmt = select(PromptBaseStructure).where(PromptBaseStructure.structure_key == key)
                q_res = await db.execute(stmt)
                existing = q_res.scalars().first()
                
                if existing:
                    if key == "boston_medical_audio":
                        # Synchronize base prompt too if it has changed
                        try:
                            result_v = await db.execute(
                                select(PromptVersion)
                                .where(PromptVersion.prompt_id == 1, PromptVersion.is_current == True)
                                .limit(1)
                            )
                            v = result_v.scalars().first()
                            if v and v.prompt:
                                existing.base_prompt = v.prompt
                                logger.info("Synchronized boston_medical_audio base prompt with prompt version 1.")
                        except Exception as ep:
                            logger.warning("Failed to sync base prompt: %s", ep)
                    else:
                        logger.info("Structure base key '%s' already exists in database. Skipping to prevent overwrite.", key)
                else:
                    new_struct = PromptBaseStructure(
                        structure_key=key,
                        structure_name=struct_data["structure_name"],
                        description=struct_data["description"],
                        prompt_type=struct_data["prompt_type"],
                        base_prompt=struct_data["base_prompt"],
                        default_criteria=struct_data["default_criteria"],
                        is_active=struct_data["is_active"],
                        created_by="system",
                        created_by_email="system@doobot.ai"
                    )
                    db.add(new_struct)
                    logger.info("Inserting default structure base: %s", key)
            
            # 3. Cleanup duplicate active prompts of the same type prudently
            for p_type in ["audio", "text"]:
                stmt = select(Prompt).where(Prompt.prompt_type == p_type, Prompt.is_active == True)
                res = await db.execute(stmt)
                active_prompts = res.scalars().all()
                if len(active_prompts) > 1:
                    logger.warning(
                        "Found %d active prompts for type '%s' in database. Performing cleanup...",
                        len(active_prompts), p_type
                    )
                    
                    target_active = None
                    # Rule A: If audio type and prompt_id = 1 is active, keep it active
                    if p_type == "audio":
                        for p in active_prompts:
                            if p.prompt_id == 1:
                                target_active = p
                                break
                    
                    # Rule B: Otherwise, keep the prompt with the most recent updated_at or version
                    if not target_active:
                        from datetime import datetime, timezone
                        def get_sort_key(p):
                            dt = p.updated_at
                            if dt is None:
                                return (datetime.min.replace(tzinfo=timezone.utc), p.prompt_id)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            else:
                                dt = dt.astimezone(timezone.utc)
                            return (dt, p.prompt_id)
                        
                        sorted_prompts = sorted(active_prompts, key=get_sort_key, reverse=True)
                        target_active = sorted_prompts[0]
                    
                    logger.info("Selected prompt ID %d ('%s') to REMAIN ACTIVE.", target_active.prompt_id, target_active.prompt_name)
                    
                    # Deactivate the others
                    for p in active_prompts:
                        if p.prompt_id != target_active.prompt_id:
                            p.is_active = False
                            logger.info(
                                "Deactivating duplicate active prompt: ID %d, Name '%s' (type '%s')",
                                p.prompt_id, p.prompt_name, p_type
                            )
            # Clean up duplicate is_current prompt versions
            logger.info("Cleaning up duplicate is_current prompt versions...")
            try:
                # Find prompt_ids that have more than one version marked as is_current
                dup_stmt = (
                    select(PromptVersion.prompt_id)
                    .where(PromptVersion.is_current == True)
                    .group_by(PromptVersion.prompt_id)
                    .having(text("COUNT(*) > 1"))
                )
                dup_res = await db.execute(dup_stmt)
                dup_prompt_ids = dup_res.scalars().all()
                
                for p_id in dup_prompt_ids:
                    logger.info("Found duplicate current versions for prompt_id=%d", p_id)
                    # Fetch all current versions for this prompt_id, sorted by id desc
                    v_stmt = (
                        select(PromptVersion)
                        .where(PromptVersion.prompt_id == p_id, PromptVersion.is_current == True)
                        .order_by(PromptVersion.id.desc())
                    )
                    v_res = await db.execute(v_stmt)
                    versions = v_res.scalars().all()
                    
                    if len(versions) > 1:
                        # Keep the first one (highest ID) as is_current=True, and unset the others
                        highest_v = versions[0]
                        logger.info("Keeping version ID %d as current for prompt %d", highest_v.id, p_id)
                        
                        for other_v in versions[1:]:
                            other_v.is_current = False
                            logger.info("Unsetting is_current for duplicate version ID %d", other_v.id)
            except Exception as e_dup:
                logger.error("Error cleaning up duplicate prompt versions: %s", e_dup)

            await db.commit()
            logger.info("db_init_service initialization completed successfully.")
            
    except Exception as e:
        logger.error("Failed to initialize database structures in startup: %s", e, exc_info=True)
