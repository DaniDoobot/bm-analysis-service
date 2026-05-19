"""Database initialization service for prompt base structures."""
import logging
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.prompts import PromptBaseStructure, PromptVersion

logger = logging.getLogger(__name__)

DEFAULT_STRUCTURES = [
    {
        "structure_key": "boston_medical_audio",
        "structure_name": "Boston Medical - Audio comercial",
        "description": "Estructura de prompt base original de Boston Medical para audios comerciales.",
        "prompt_type": "audio",
        "base_prompt": "",  # Will be populated dynamically or fallback
        "default_criteria": [
            {
                "criterion_key": "saludo_identificacion",
                "criterion_name": "Saludo e Identificación",
                "criterion_description": "El agente saluda cordialmente y se identifica correctamente con su nombre y el nombre de la empresa.",
                "criterion_type": "boolean",
                "output_key": "saludo_identificacion",
                "feed_key": "saludo_identificacion_feed",
                "order_index": 10,
                "is_required": True,
                "is_active": True
            },
            {
                "criterion_key": "empatia",
                "criterion_name": "Empatía",
                "criterion_description": "El agente muestra empatía y escucha activa durante la llamada.",
                "criterion_type": "boolean",
                "output_key": "empatia",
                "feed_key": "empatia_feed",
                "order_index": 20,
                "is_required": True,
                "is_active": True
            },
            {
                "criterion_key": "claridad",
                "criterion_name": "Claridad",
                "criterion_description": "El agente se expresa con claridad, tono y volumen adecuados.",
                "criterion_type": "boolean",
                "output_key": "claridad",
                "feed_key": "claridad_feed",
                "order_index": 30,
                "is_required": True,
                "is_active": True
            },
            {
                "criterion_key": "gestion_objeciones",
                "criterion_name": "Gestión de Objeciones",
                "criterion_description": "El agente maneja adecuadamente las dudas u objeciones presentadas.",
                "criterion_type": "boolean",
                "output_key": "gestion_objeciones",
                "feed_key": "gestion_objeciones_feed",
                "order_index": 40,
                "is_required": True,
                "is_active": True
            }
        ],
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
        "default_criteria": [
            {
                "criterion_key": "confirmacion_fecha_hora",
                "criterion_name": "Confirmación de Fecha y Hora",
                "criterion_description": "El agente confirma claramente la fecha y la hora programada de la cita.",
                "criterion_type": "boolean",
                "output_key": "confirmacion_fecha_hora",
                "feed_key": "confirmacion_fecha_hora_feed",
                "order_index": 10,
                "is_required": True,
                "is_active": True
            },
            {
                "criterion_key": "confirmacion_direccion",
                "criterion_name": "Confirmación de Dirección de Clínica",
                "criterion_description": "El agente indica o confirma la dirección de la clínica correspondiente.",
                "criterion_type": "boolean",
                "output_key": "confirmacion_direccion",
                "feed_key": "confirmacion_direccion_feed",
                "order_index": 20,
                "is_required": True,
                "is_active": True
            }
        ],
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
        "default_criteria": [
            {
                "criterion_key": "cortesia_amabilidad",
                "criterion_name": "Cortesía y Amabilidad",
                "criterion_description": "El agente saluda de manera educada, mantiene un trato respetuoso y se despide cordialmente.",
                "criterion_type": "boolean",
                "output_key": "cortesia_amabilidad",
                "feed_key": "cortesia_amabilidad_feed",
                "order_index": 10,
                "is_required": True,
                "is_active": True
            },
            {
                "criterion_key": "resolucion_dudas",
                "criterion_name": "Capacidad de Resolución",
                "criterion_description": "El agente responde eficazmente a las preguntas o soluciona el problema del cliente.",
                "criterion_type": "boolean",
                "output_key": "resolucion_dudas",
                "feed_key": "resolucion_dudas_feed",
                "order_index": 20,
                "is_required": True,
                "is_active": True
            }
        ],
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
        "default_criteria": [
            {
                "criterion_key": "propuesta_valor",
                "criterion_name": "Presentación de Propuesta de Valor",
                "criterion_description": "El agente explica claramente los beneficios del producto o servicio y la propuesta única de valor.",
                "criterion_type": "boolean",
                "output_key": "propuesta_valor",
                "feed_key": "propuesta_valor_feed",
                "order_index": 10,
                "is_required": True,
                "is_active": True
            },
            {
                "criterion_key": "cierre_comercial",
                "criterion_name": "Intento de Cierre Comercial",
                "criterion_description": "El agente realiza un intento claro de cierre de venta o agendamiento al final de la llamada.",
                "criterion_type": "boolean",
                "output_key": "cierre_comercial",
                "feed_key": "cierre_comercial_feed",
                "order_index": 20,
                "is_required": True,
                "is_active": True
            }
        ],
        "is_active": True,
    },
    {
        "structure_key": "blank",
        "structure_name": "Prompt desde cero",
        "description": "Crea un prompt vacío sin criterios iniciales.",
        "prompt_type": "audio",
        "base_prompt": "",
        "default_criteria": [],
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


        # 2. Seed structures in a safe, non-destructive session
        from app.dependencies import get_db
        async with AsyncSession(engine) as db:
            # Populate boston_medical_audio dynamic base prompt and default_criteria if possible
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

                # Query active criteria of prompt 1
                from app.models.criteria import PromptCriterion
                crit_res = await db.execute(
                    select(PromptCriterion)
                    .where(PromptCriterion.prompt_id == 1, PromptCriterion.is_active == True)
                    .order_by(PromptCriterion.order_index.asc())
                )
                crit_list = crit_res.scalars().all()
                if crit_list:
                    mapped = []
                    for c in crit_list:
                        mapped.append({
                            "criterion_key": c.criterion_key,
                            "criterion_name": c.criterion_name,
                            "criterion_description": c.criterion_description,
                            "criterion_type": c.criterion_type,
                            "output_key": c.output_key,
                            "feed_key": c.feed_key,
                            "is_active": c.is_active,
                            "is_required": c.is_required,
                            "order_index": c.order_index
                        })
                    boston_audio_struct["default_criteria"] = mapped
                    logger.info("Loaded %d active criteria for boston_medical_audio default_criteria from prompt 1.", len(mapped))
            except Exception as e:
                if not boston_audio_struct["base_prompt"]:
                    boston_audio_struct["base_prompt"] = FALLBACK_BOSTON_PROMPT
                logger.warning("Error fetching active prompt version 1 / criteria for seeding: %s. Using default fallback.", e)

            # Insert default records if structure_key doesn't exist
            for struct_data in DEFAULT_STRUCTURES:
                key = struct_data["structure_key"]
                
                # Check if exists
                stmt = select(PromptBaseStructure).where(PromptBaseStructure.structure_key == key)
                q_res = await db.execute(stmt)
                existing = q_res.scalars().first()
                
                if existing:
                    if key == "boston_medical_audio":
                        # Check if it has the fallback 4 criteria (saludo_identificacion, empatia, claridad, gestion_objeciones)
                        is_fallback = False
                        if existing.default_criteria and len(existing.default_criteria) <= 4:
                            fallback_keys = {"saludo_identificacion", "empatia", "claridad", "gestion_objeciones"}
                            existing_keys = {c.get("criterion_key") for c in existing.default_criteria if c}
                            if existing_keys.issubset(fallback_keys):
                                is_fallback = True

                        if is_fallback:
                            logger.info("Detected legacy 4-criteria fallback in boston_medical_audio in DB. Upgrading dynamically...")
                            from app.models.criteria import PromptCriterion
                            crit_res = await db.execute(
                                select(PromptCriterion)
                                .where(PromptCriterion.prompt_id == 1, PromptCriterion.is_active == True)
                                .order_by(PromptCriterion.order_index.asc())
                            )
                            crit_list = crit_res.scalars().all()
                            if crit_list:
                                upgraded_criteria = []
                                for c in crit_list:
                                    upgraded_criteria.append({
                                        "criterion_key": c.criterion_key,
                                        "criterion_name": c.criterion_name,
                                        "criterion_description": c.criterion_description,
                                        "criterion_type": c.criterion_type,
                                        "output_key": c.output_key,
                                        "feed_key": c.feed_key,
                                        "is_active": c.is_active,
                                        "is_required": c.is_required,
                                        "order_index": c.order_index
                                    })
                                existing.default_criteria = upgraded_criteria
                                logger.info("Upgraded existing boston_medical_audio default_criteria in DB dynamically to %d real active criteria.", len(upgraded_criteria))

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
                                logger.info("No active criteria found in DB for prompt 1. Keeping fallback.")
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
            
            await db.commit()
            logger.info("db_init_service initialization completed successfully.")
            
    except Exception as e:
        logger.error("Failed to initialize database structures in startup: %s", e, exc_info=True)
