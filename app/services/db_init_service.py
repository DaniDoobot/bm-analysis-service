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

        # 2. Seed structures in a safe, non-destructive session
        from app.dependencies import get_db
        async with AsyncSession(engine) as db:
            # Populate boston_medical_audio dynamic base prompt if possible
            boston_audio_struct = DEFAULT_STRUCTURES[0]
            if not boston_audio_struct["base_prompt"]:
                # Query active prompt 1 current version
                try:
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
                    boston_audio_struct["base_prompt"] = FALLBACK_BOSTON_PROMPT
                    logger.warning("Error fetching active prompt version 1 for seeding: %s. Using fallback.", e)

            # Insert default records if structure_key doesn't exist
            for struct_data in DEFAULT_STRUCTURES:
                key = struct_data["structure_key"]
                
                # Check if exists
                stmt = select(PromptBaseStructure).where(PromptBaseStructure.structure_key == key)
                q_res = await db.execute(stmt)
                existing = q_res.scalars().first()
                
                if existing:
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
