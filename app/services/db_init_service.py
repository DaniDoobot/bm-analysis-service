"""Database initialization service for prompt base structures."""
import logging
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.prompts import PromptBaseStructure, PromptVersion, Prompt
from app.models.services import Service
from app.models.typologies import Typology
from app.models.criteria import PromptCriterion, PromptCriterionTypology

logger = logging.getLogger(__name__)

DEFAULT_STRUCTURES = [
    {
        "structure_key": "boston_medical_audio",
        "structure_name": "Boston Medical - Audio comercial",
        "description": "Estructura de prompt base original de Boston Medical para audios comerciales.",
        "prompt_type": "text",
        "base_prompt": "",  # Will be populated dynamically or fallback
        "default_criteria": None,
        "is_active": True,
    },
    {
        "structure_key": "boston_medical_appointment",
        "structure_name": "Boston Medical - Confirmación de cita",
        "description": "Evaluación de llamadas para confirmación de citas en clínicas de Boston Medical.",
        "prompt_type": "text",
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
        "prompt_type": "text",
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
        "prompt_type": "text",
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
        "prompt_type": "text",
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
    "Debes devolver la evaluación en formato JSON estructurado, incluyendo la clasificación del tipo de llamada, el valor/justificación de cada criterio y obligatoriamente una clave 'resumen' (string | null) de 2-4 frases que sintetice qué ocurrió, la actitud del paciente, la actuación del agente y el resultado final."
)


async def init_db():
    """
    Initialize prompt base structures table and populate default records.
    Fully idempotent and non-destructive.
    """
    logger.info("Starting db_init_service initialization...")
    try:
        engine = get_engine()
        
        # 1. Create all missing tables unconditionally via Base.metadata.create_all
        # SQLAlchemy is native, safe, and idempotent. It only creates tables that do not yet exist.
        async with engine.begin() as conn:
            logger.info("Initializing database tables via SQLAlchemy metadata (unconditional & safe)...")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables initialized successfully.")

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

        # 1.6. Ensure service and typology columns exist on other tables dynamically and non-destructively
        async with engine.begin() as conn:
            # 1.6.1 bm_prompt_base_structures
            res = await conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns 
                        WHERE table_schema = 'public' 
                          AND table_name = 'bm_prompt_base_structures' 
                          AND column_name = 'service_id'
                    );
                """)
            )
            if not res.scalar():
                logger.info("Adding column 'service_id' to 'bm_prompt_base_structures' table...")
                await conn.execute(text("ALTER TABLE bm_prompt_base_structures ADD COLUMN service_id INTEGER NULL;"))
                logger.info("Column 'service_id' added successfully to 'bm_prompt_base_structures'.")

            # 1.6.2 bm_prompts
            res = await conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns 
                        WHERE table_schema = 'public' 
                          AND table_name = 'bm_prompts' 
                          AND column_name = 'service_id'
                    );
                """)
            )
            if not res.scalar():
                logger.info("Adding column 'service_id' to 'bm_prompts' table...")
                await conn.execute(text("ALTER TABLE bm_prompts ADD COLUMN service_id INTEGER NULL;"))
                logger.info("Column 'service_id' added successfully to 'bm_prompts'.")

            # 1.6.3 bm_mass_evaluation_results
            for col_name, col_type in [
                ("service_id", "INTEGER"),
                ("service_key", "TEXT"),
                ("service_name", "TEXT"),
                ("typology_id", "INTEGER"),
                ("typology_key", "TEXT"),
                ("typology_name", "TEXT"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = 'bm_mass_evaluation_results' 
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Adding column '%s' to 'bm_mass_evaluation_results' table...", col_name)
                    await conn.execute(text(f"ALTER TABLE bm_mass_evaluation_results ADD COLUMN {col_name} {col_type} NULL;"))
                    logger.info("Column '%s' added successfully to 'bm_mass_evaluation_results'.")

            # 1.6.4 bm_prompt_versions — archiving support columns
            for col_name, col_type, col_default in [
                ("is_archived", "BOOLEAN", "DEFAULT FALSE NOT NULL"),
                ("archived_at", "TIMESTAMPTZ", "NULL"),
                ("archived_by_email", "TEXT", "NULL"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = 'bm_prompt_versions'
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Adding column '%s' to 'bm_prompt_versions' table...", col_name)
                    await conn.execute(text(f"ALTER TABLE bm_prompt_versions ADD COLUMN {col_name} {col_type} {col_default};"))
                    logger.info("Column '%s' added successfully to 'bm_prompt_versions'.", col_name)

            # 1.6.5 bm_prompt_criteria — soft delete support columns
            for col_name, col_type, col_default in [
                ("deleted_at", "TIMESTAMPTZ", "NULL"),
                ("deleted_by_email", "TEXT", "NULL"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = 'bm_prompt_criteria'
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Adding column '%s' to 'bm_prompt_criteria' table...", col_name)
                    await conn.execute(text(f"ALTER TABLE bm_prompt_criteria ADD COLUMN {col_name} {col_type} {col_default};"))
                    logger.info("Column '%s' added successfully to 'bm_prompt_criteria'.", col_name)

            # 1.7 Create flat reporting views for normalized criteria results
            logger.info("Ensuring reporting views exist...")
            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_analysis_criteria_flat AS
                SELECT
                    a.analysis_id,
                    a.call_id,
                    a.hubspot_url,
                    a.call_timestamp,
                    a.agente_telefonico,
                    a.tipo_llamada,
                    a.evaluacion_global,
                    a.prompt_id,
                    c.criterion_key,
                    c.criterion_name,
                    c.criterion_type,
                    c.numeric_value,
                    c.text_value,
                    c.boolean_value,
                    c.category_value,
                    c.percentage_value,
                    c.feedback,
                    c.is_applicable,
                    c.service_name,
                    c.typology_name
                FROM bm_analyses a
                JOIN bm_analysis_criterion_results c ON a.analysis_id = c.analysis_id;
            """))

            # Drop views first to avoid InvalidTableDefinitionError if column names or positions changed
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_criteria_flat CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_results_pivot CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_daily_summary CASCADE;"))

            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_mass_evaluation_criteria_flat AS
                SELECT
                    m.mass_analysis_id,
                    m.job_id,
                    m.run_id,
                    m.call_id,
                    m.hs_object_id,
                    m.call_timestamp,
                    m.call_timestamp::date AS call_date,
                    m.created_at AS analyzed_at,
                    m.call_duration_seconds AS duration_seconds,
                    m.direction,
                    m.hubspot_owner_id AS agent_owner_id,
                    m.agent_name,
                    m.status,
                    m.error_message,
                    m.prompt_id,
                    m.prompt_name,
                    m.prompt_version_id,
                    m.prompt_version_name,
                    m.prompt_version_label,
                    c.service_id,
                    c.service_key,
                    c.service_name,
                    c.typology_id,
                    c.typology_key,
                    c.typology_name,
                    c.criterion_id,
                    c.criterion_key,
                    c.criterion_name,
                    c.criterion_type,
                    c.feed_key,
                    c.numeric_value,
                    c.percentage_value,
                    c.boolean_value,
                    c.category_value,
                    c.text_value,
                    c.feedback,
                    c.is_applicable,
                    c.not_applicable,
                    c.created_at,
                    c.updated_at
                FROM bm_mass_evaluation_results m
                JOIN bm_mass_evaluation_criterion_results c ON m.mass_analysis_id = c.mass_analysis_id;
            """))

            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_mass_evaluation_results_pivot AS
                SELECT
                    m.mass_analysis_id,
                    m.job_id,
                    m.run_id,
                    m.call_id,
                    m.call_timestamp,
                    m.call_timestamp::date AS call_date,
                    m.created_at AS analyzed_at,
                    m.call_duration_seconds AS duration_seconds,
                    m.direction,
                    m.hubspot_owner_id AS agent_owner_id,
                    m.agent_name,
                    m.status,
                    m.prompt_id,
                    m.prompt_name,
                    m.prompt_version_id,
                    m.prompt_version_name,
                    m.service_id,
                    m.service_name,
                    m.typology_key,
                    m.typology_name,

                    -- Scores numéricos
                    MAX(CASE WHEN c.criterion_key = 'sentiment' AND c.is_applicable = true THEN c.numeric_value END) AS sentiment,
                    MAX(CASE WHEN c.criterion_key = 'evaluacion_global' AND c.is_applicable = true THEN c.numeric_value END) AS evaluacion_global,
                    MAX(CASE WHEN c.criterion_key = 'empatia' AND c.is_applicable = true THEN c.numeric_value END) AS empatia,
                    MAX(CASE WHEN c.criterion_key = 'simpatia' AND c.is_applicable = true THEN c.numeric_value END) AS simpatia,
                    MAX(CASE WHEN c.criterion_key = 'claridad' AND c.is_applicable = true THEN c.numeric_value END) AS claridad,
                    MAX(CASE WHEN c.criterion_key = 'procedimiento' AND c.is_applicable = true THEN c.numeric_value END) AS procedimiento,
                    MAX(CASE WHEN c.criterion_key = 'saludo_inicio' AND c.is_applicable = true THEN c.numeric_value END) AS saludo_inicio,
                    MAX(CASE WHEN c.criterion_key = 'trato_usted' AND c.is_applicable = true THEN c.numeric_value END) AS trato_usted,
                    MAX(CASE WHEN c.criterion_key = 'explicaciones_medicas' AND c.is_applicable = true THEN c.numeric_value END) AS explicaciones_medicas,
                    MAX(CASE WHEN c.criterion_key = 'claridad_explicacion_economica' AND c.is_applicable = true THEN c.numeric_value END) AS claridad_explicacion_economica,
                    MAX(CASE WHEN c.criterion_key = 'n3_preguntas' AND c.is_applicable = true THEN c.numeric_value END) AS n3_preguntas,
                    MAX(CASE WHEN c.criterion_key = 'claridad_de_explicacion_de_precio_en_consulta' AND c.is_applicable = true THEN c.numeric_value END) AS claridad_de_explicacion_de_precio_en_consulta,
                    MAX(CASE WHEN c.criterion_key = 'interrupciones' AND c.is_applicable = true THEN c.numeric_value END) AS interrupciones,
                    MAX(CASE WHEN c.criterion_key = 'velocidad_hablando_agente' AND c.is_applicable = true THEN c.numeric_value END) AS velocidad_hablando_agente,
                    MAX(CASE WHEN c.criterion_key = 'despedida_con_refuerzo' AND c.is_applicable = true THEN c.numeric_value END) AS despedida_con_refuerzo,
                    MAX(CASE WHEN c.criterion_key = 'siguiente_paso' AND c.is_applicable = true THEN c.numeric_value END) AS siguiente_paso,
                    MAX(CASE WHEN c.criterion_key = 'gestion_objeciones' AND c.is_applicable = true THEN c.numeric_value END) AS gestion_objeciones,
                    MAX(CASE WHEN c.criterion_key = 'uso_nombre_paciente' AND c.is_applicable = true THEN c.numeric_value END) AS uso_nombre_paciente,
                    MAX(CASE WHEN c.criterion_key = 'uso_preguntas' AND c.is_applicable = true THEN c.numeric_value END) AS uso_preguntas,
                    MAX(CASE WHEN c.criterion_key = 'propension' AND c.is_applicable = true THEN c.numeric_value END) AS propension,

                    -- Feedbacks
                    MAX(CASE WHEN (c.criterion_key = 'sentiment' OR c.feed_key = 'sentiment_feed') AND c.is_applicable = true THEN c.feedback END) AS sentiment_feed,
                    MAX(CASE WHEN (c.criterion_key = 'empatia' OR c.feed_key = 'empatia_feed') AND c.is_applicable = true THEN c.feedback END) AS empatia_feed,
                    MAX(CASE WHEN (c.criterion_key = 'simpatia' OR c.feed_key = 'simpatia_feed') AND c.is_applicable = true THEN c.feedback END) AS simpatia_feed,
                    MAX(CASE WHEN (c.criterion_key = 'claridad' OR c.feed_key = 'claridad_feed') AND c.is_applicable = true THEN c.feedback END) AS claridad_feed,
                    MAX(CASE WHEN (c.criterion_key = 'saludo_inicio' OR c.feed_key = 'saludo_inicio_feed') AND c.is_applicable = true THEN c.feedback END) AS saludo_inicio_feed,
                    MAX(CASE WHEN (c.criterion_key = 'trato_usted' OR c.feed_key = 'trato_usted_feed') AND c.is_applicable = true THEN c.feedback END) AS trato_usted_feed,
                    MAX(CASE WHEN (c.criterion_key = 'explicaciones_medicas' OR c.feed_key = 'explicaciones_medicas_feed') AND c.is_applicable = true THEN c.feedback END) AS explicaciones_medicas_feed,
                    MAX(CASE WHEN (c.criterion_key = 'claridad_explicacion_economica' OR c.feed_key = 'claridad_explicacion_economica_feed') AND c.is_applicable = true THEN c.feedback END) AS claridad_explicacion_economica_feed,
                    MAX(CASE WHEN (c.criterion_key = 'n3_preguntas' OR c.feed_key = 'n3_preguntas_feedback') AND c.is_applicable = true THEN c.feedback END) AS n3_preguntas_feedback,
                    MAX(CASE WHEN (c.criterion_key = 'claridad_de_explicacion_de_precio_en_consulta' OR c.feed_key = 'claridad_de_explicacion_de_precio_en_consulta_feed') AND c.is_applicable = true THEN c.feedback END) AS claridad_de_explicacion_de_precio_en_consulta_feed,
                    MAX(CASE WHEN (c.criterion_key = 'interrupciones' OR c.feed_key = 'interrupciones_feed') AND c.is_applicable = true THEN c.feedback END) AS interrupciones_feed,
                    MAX(CASE WHEN (c.criterion_key = 'velocidad_hablando_agente' OR c.feed_key = 'velocidad_hablando_agente_feed') AND c.is_applicable = true THEN c.feedback END) AS velocidad_hablando_agente_feed,
                    MAX(CASE WHEN (c.criterion_key = 'despedida_con_refuerzo' OR c.feed_key = 'despedida_con_refuerzo_feed') AND c.is_applicable = true THEN c.feedback END) AS despedida_con_refuerzo_feed,
                    MAX(CASE WHEN (c.criterion_key = 'gestion_objeciones' OR c.feed_key = 'gestion_objeciones_feed') AND c.is_applicable = true THEN c.feedback END) AS gestion_objeciones_feed,
                    MAX(CASE WHEN (c.criterion_key = 'uso_nombre_paciente' OR c.feed_key = 'uso_nombre_paciente_feed') AND c.is_applicable = true THEN c.feedback END) AS uso_nombre_paciente_feed,
                    MAX(CASE WHEN (c.criterion_key = 'uso_preguntas' OR c.feed_key = 'uso_preguntas_feed') AND c.is_applicable = true THEN c.feedback END) AS uso_preguntas_feed,

                    -- Porcentajes / números
                    MAX(CASE WHEN c.criterion_key = 'hablando_agente' AND c.is_applicable = true THEN c.percentage_value END) AS hablando_agente,
                    MAX(CASE WHEN c.criterion_key = 'hablando_paciente' AND c.is_applicable = true THEN c.percentage_value END) AS hablando_paciente,
                    MAX(CASE WHEN c.criterion_key = 'palabras_minuto_agente' AND c.is_applicable = true THEN c.numeric_value END) AS palabras_minuto_agente,
                    MAX(CASE WHEN c.criterion_key = 'meses_patologia' AND c.is_applicable = true THEN c.numeric_value END) AS meses_patologia,
                    MAX(CASE WHEN c.criterion_key = 'edad' AND c.is_applicable = true THEN c.numeric_value END) AS edad,

                    -- Booleanos
                    BOOL_OR(CASE WHEN c.criterion_key = 'verifica_patologia' AND c.is_applicable = true THEN c.boolean_value END) AS verifica_patologia,
                    BOOL_OR(CASE WHEN c.criterion_key = 'reformula_patologia' AND c.is_applicable = true THEN c.boolean_value END) AS reformula_patologia,
                    BOOL_OR(CASE WHEN c.criterion_key = 'medio' AND c.is_applicable = true THEN c.boolean_value END) AS medio,
                    BOOL_OR(CASE WHEN c.criterion_key = 'precio_consulta' AND c.is_applicable = true THEN c.boolean_value END) AS precio_consulta,
                    BOOL_OR(CASE WHEN c.criterion_key = 'tratamiento_no_en_precio' AND c.is_applicable = true THEN c.boolean_value END) AS tratamiento_no_en_precio,
                    BOOL_OR(CASE WHEN c.criterion_key = 'duracion_consulta' AND c.is_applicable = true THEN c.boolean_value END) AS duracion_consulta,
                    BOOL_OR(CASE WHEN c.criterion_key = 'direccion_y_referencias' AND c.is_applicable = true THEN c.boolean_value END) AS direccion_y_referencias,
                    BOOL_OR(CASE WHEN (c.criterion_key = 'puntalidad' OR c.criterion_key = 'puntualidad') AND c.is_applicable = true THEN c.boolean_value END) AS puntualidad,
                    BOOL_OR(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true THEN c.boolean_value END) AS cierre_cita,
                    BOOL_OR(CASE WHEN c.criterion_key = 'conocimiento_boston_medical' AND c.is_applicable = true THEN c.boolean_value END) AS conocimiento_boston_medical,
                    BOOL_OR(CASE WHEN c.criterion_key = 'puede_adelantar_cita' AND c.is_applicable = true THEN c.boolean_value END) AS puede_adelantar_cita,
                    BOOL_OR(CASE WHEN c.criterion_key = 'pregunta_pareja' AND c.is_applicable = true THEN c.boolean_value END) AS pregunta_pareja,
                    BOOL_OR(CASE WHEN c.criterion_key = 'recomienda_pareja' AND c.is_applicable = true THEN c.boolean_value END) AS recomienda_pareja,
                    BOOL_OR(CASE WHEN c.criterion_key = 'pareja_conocedora' AND c.is_applicable = true THEN c.boolean_value END) AS pareja_conocedora,
                    BOOL_OR(CASE WHEN c.criterion_key = 'pareja_asistira' AND c.is_applicable = true THEN c.boolean_value END) AS pareja_asistira,

                    -- Categorías / textos
                    MAX(CASE WHEN c.criterion_key = 'tipo_llamada' AND c.is_applicable = true THEN c.category_value END) AS tipo_llamada,
                    MAX(CASE WHEN c.criterion_key = 'patologia' AND c.is_applicable = true THEN c.category_value END) AS patologia,
                    MAX(CASE WHEN c.criterion_key = 'objeciones' AND c.is_applicable = true THEN c.text_value END) AS objeciones,
                    MAX(CASE WHEN c.criterion_key = 'objecion_1' AND c.is_applicable = true THEN c.category_value END) AS objecion_1,
                    MAX(CASE WHEN c.criterion_key = 'objecion_2' AND c.is_applicable = true THEN c.category_value END) AS objecion_2,
                    MAX(CASE WHEN c.criterion_key = 'objecion_3' AND c.is_applicable = true THEN c.category_value END) AS objecion_3,
                    MAX(CASE WHEN c.criterion_key = 'motivo_no_cita' AND c.is_applicable = true THEN c.category_value END) AS motivo_no_cita,
                    MAX(CASE WHEN c.criterion_key = 'cuanto_tiempo' AND c.is_applicable = true THEN c.text_value END) AS cuanto_tiempo,
                    MAX(CASE WHEN c.criterion_key = 'por_que_ahora' AND c.is_applicable = true THEN c.text_value END) AS por_que_ahora

                FROM bm_mass_evaluation_results m
                JOIN bm_mass_evaluation_criterion_results c ON m.mass_analysis_id = c.mass_analysis_id
                GROUP BY
                    m.mass_analysis_id,
                    m.job_id,
                    m.run_id,
                    m.call_id,
                    m.call_timestamp,
                    m.created_at,
                    m.call_duration_seconds,
                    m.direction,
                    m.hubspot_owner_id,
                    m.agent_name,
                    m.status,
                    m.prompt_id,
                    m.prompt_name,
                    m.prompt_version_id,
                    m.prompt_version_name,
                    m.service_id,
                    m.service_name,
                    m.typology_key,
                    m.typology_name;
            """))

            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_mass_evaluation_daily_summary AS
                SELECT
                    c.created_at::date AS analysis_date,
                    date_trunc('week', c.created_at)::date AS week_start,
                    date_trunc('month', c.created_at)::date AS month_start,
                    m.hubspot_owner_id AS agent_owner_id,
                    m.agent_name,
                    c.service_id,
                    c.service_name,
                    c.typology_key,
                    c.typology_name,
                    c.criterion_key,
                    c.criterion_name,
                    c.criterion_type,
                    COUNT(*) AS total_rows,
                    COUNT(CASE WHEN c.is_applicable = true THEN 1 END) AS total_applicable,
                    COUNT(CASE WHEN c.not_applicable = true THEN 1 END) AS total_not_applicable,
                    AVG(CASE WHEN c.is_applicable = true THEN c.numeric_value END) AS avg_numeric_value,
                    AVG(CASE WHEN c.is_applicable = true THEN c.percentage_value END) AS avg_percentage_value,
                    COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value = true THEN 1 END) AS yes_count,
                    COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value = false THEN 1 END) AS no_count,
                    CASE 
                        WHEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END) > 0 
                        THEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value = true THEN 1 END)::numeric / COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END) 
                        ELSE NULL 
                    END AS yes_rate,
                    CASE 
                        WHEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END) > 0 
                        THEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value = false THEN 1 END)::numeric / COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END) 
                        ELSE NULL 
                    END AS no_rate,
                    COUNT(CASE WHEN c.is_applicable = true AND c.text_value IS NOT NULL AND c.text_value != '' THEN 1 END) AS text_count
                FROM bm_mass_evaluation_results m
                JOIN bm_mass_evaluation_criterion_results c ON m.mass_analysis_id = c.mass_analysis_id
                GROUP BY
                    c.created_at::date,
                    date_trunc('week', c.created_at)::date,
                    date_trunc('month', c.created_at)::date,
                    m.hubspot_owner_id,
                    m.agent_name,
                    c.service_id,
                    c.service_name,
                    c.typology_key,
                    c.typology_name,
                    c.criterion_key,
                    c.criterion_name,
                    c.criterion_type;
            """))
            logger.info("Reporting views ensured.")

        # 2. Safe backfill: Force default_criteria to NULL and prompt_type to 'text' on ALL base structures.
        # This runs in its own isolated transaction so it always commits, regardless of
        # any failures in subsequent seeding steps.
        async with engine.begin() as conn:
            logger.info("Executing isolated backfill: SET default_criteria = NULL and prompt_type = 'text' for all bm_prompt_base_structures...")
            result_criteria = await conn.execute(
                text("UPDATE bm_prompt_base_structures SET default_criteria = NULL WHERE default_criteria IS NOT NULL;")
            )
            result_type = await conn.execute(
                text("UPDATE bm_prompt_base_structures SET prompt_type = 'text' WHERE prompt_type IS DISTINCT FROM 'text';")
            )
            logger.info("Backfill complete. Criteria rows updated: %d, Type rows updated: %d", result_criteria.rowcount, result_type.rowcount)

        # 3. Seed structures in a safe, non-destructive session
        from app.dependencies import get_db
        async with AsyncSession(engine) as db:
            # Seed default Services
            services_data = [
                {"key": "front", "name": "Front", "desc": "Servicio de Front Desk / Recepción"},
                {"key": "experiencia_paciente", "name": "Experiencia de Paciente", "desc": "Servicio de Experiencia de Paciente"},
                {"key": "asesorias", "name": "Asesorías", "desc": "Servicio de Asesorías / Consultas"}
            ]
            service_ids_map = {}
            for s_item in services_data:
                s_key = s_item["key"]
                stmt_s = select(Service).where(Service.service_key == s_key)
                res_s = await db.execute(stmt_s)
                existing_s = res_s.scalars().first()
                if not existing_s:
                    new_s = Service(
                        service_key=s_key,
                        service_name=s_item["name"],
                        description=s_item["desc"],
                        is_active=True
                    )
                    db.add(new_s)
                    await db.flush() # flush to generate ID
                    service_ids_map[s_key] = new_s.service_id
                    logger.info("Seeded service: %s", s_key)
                else:
                    service_ids_map[s_key] = existing_s.service_id

            # Seed default Typologies for 'front' service
            front_service_id = service_ids_map.get("front")
            if front_service_id:
                typologies_data = [
                    {"key": "cita", "name": "Cita", "order": 10},
                    {"key": "confirmacion", "name": "Confirmación", "order": 20},
                    {"key": "cancelacion", "name": "Cancelación", "order": 30},
                    {"key": "reagendo", "name": "Reagendo", "order": 40},
                    {"key": "falta", "name": "Falta", "order": 50},
                    {"key": "otros", "name": "Otros", "order": 60}
                ]
                typology_ids = []
                for t_item in typologies_data:
                    t_key = t_item["key"]
                    stmt_t = select(Typology).where(Typology.service_id == front_service_id, Typology.typology_key == t_key)
                    res_t = await db.execute(stmt_t)
                    existing_t = res_t.scalars().first()
                    if not existing_t:
                        new_t = Typology(
                            service_id=front_service_id,
                            typology_key=t_key,
                            typology_name=t_item["name"],
                            sort_order=t_item["order"],
                            is_active=True
                        )
                        db.add(new_t)
                        await db.flush()
                        typology_ids.append(new_t.typology_id)
                        logger.info("Seeded typology: %s for service front", t_key)
                    else:
                        typology_ids.append(existing_t.typology_id)

                # Backfill: assign service_id to all existing base structures that don't have one
                await db.execute(
                    text("UPDATE bm_prompt_base_structures SET service_id = :front_id WHERE service_id IS NULL"),
                    {"front_id": front_service_id}
                )

                # Backfill: assign service_id to all existing prompts that don't have one
                await db.execute(
                    text("UPDATE bm_prompts SET service_id = :front_id WHERE service_id IS NULL"),
                    {"front_id": front_service_id}
                )

                # Backfill: associate all existing criteria with all active typologies of the service front
                # Retrieve all active criteria
                c_stmt = select(PromptCriterion.criterion_id)
                c_res = await db.execute(c_stmt)
                all_c_ids = c_res.scalars().all()
                for c_id in all_c_ids:
                    for t_id in typology_ids:
                        assoc_stmt = select(PromptCriterionTypology).where(
                            PromptCriterionTypology.criterion_id == c_id,
                            PromptCriterionTypology.typology_id == t_id
                        )
                        assoc_res = await db.execute(assoc_stmt)
                        existing_assoc = assoc_res.scalars().first()
                        if not existing_assoc:
                            new_assoc = PromptCriterionTypology(
                                criterion_id=c_id,
                                typology_id=t_id
                            )
                            db.add(new_assoc)
                await db.flush()
                logger.info("Backfilled %d criteria associations for retrocompatibility.", len(all_c_ids))

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
