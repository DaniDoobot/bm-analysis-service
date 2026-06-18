"""Database initialization service for prompt base structures."""
import logging
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
import app.models
from app.models.prompts import PromptBaseStructure, PromptVersion, Prompt
from app.models.services import Service
from app.models.typologies import Typology
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
)

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

        # Early dynamic column migration for bm_users to avoid ProgrammingError on User model queries
        async with engine.begin() as conn:
            for col_name, col_type in [
                ("name", "TEXT NULL"),
                ("hubspot_owner_id", "TEXT NULL"),
                ("agent_initials", "TEXT NULL"),
                ("must_reset_password", "BOOLEAN DEFAULT FALSE NOT NULL"),
                ("password_set_at", "TIMESTAMPTZ NULL"),
                ("reset_token", "TEXT NULL"),
                ("reset_token_expires_at", "TIMESTAMPTZ NULL"),
                ("last_login_at", "TIMESTAMPTZ NULL"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = 'bm_users' 
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                col_exists = res.scalar()
                if not col_exists:
                    logger.info("Adding column '%s' to 'bm_users' table...", col_name)
                    await conn.execute(
                        text(f"ALTER TABLE bm_users ADD COLUMN {col_name} {col_type};")
                    )
                    if col_name == "hubspot_owner_id":
                        try:
                            await conn.execute(text("ALTER TABLE bm_users ADD CONSTRAINT uq_bm_users_hubspot_owner_id UNIQUE (hubspot_owner_id);"))
                        except Exception as e_uq:
                            logger.warning("Could not add unique constraint to hubspot_owner_id: %s", e_uq)
                    logger.info("Column '%s' added successfully to 'bm_users'.", col_name)
                else:
                    logger.info("Column '%s' already exists on 'bm_users' table.", col_name)

        # 1.2. Seed default developer user if bm_users is empty
        from app.models.users import User
        from app.utils.security import hash_password
        
        async with AsyncSession(engine) as session:
            try:
                res = await session.execute(select(User).limit(1))
                if not res.scalars().first():
                    logger.info("Seeding default developer user 'admin'...")
                    default_user = User(
                        username="admin",
                        email="admin@doobot.ai",
                        role="admin",
                        is_active=True,
                        password_hash=hash_password("admin123"),
                        password_plain_dev=None
                    )
                    session.add(default_user)
                    await session.commit()
                    logger.info("Default developer user 'admin' seeded successfully.")
                else:
                    logger.info("Users table is not empty, skipping seeding.")
            except Exception as e:
                logger.error("Failed to seed default developer user: %s", e)

        # 1.2.b Clear all plain-text passwords from the database for security
        async with AsyncSession(engine) as session:
            try:
                await session.execute(text("UPDATE bm_users SET password_plain_dev = NULL WHERE password_plain_dev IS NOT NULL;"))
                await session.commit()
                logger.info("Cleared all plain-text dev passwords from bm_users successfully.")
            except Exception as e:
                logger.error("Failed to clear plain-text dev passwords: %s", e)

        # 1.2.b-2 Ensure partial unique index on bm_users (hubspot_owner_id) WHERE hubspot_owner_id IS NOT NULL
        async with AsyncSession(engine) as session:
            try:
                res_dups = await session.execute(text("""
                    SELECT hubspot_owner_id, COUNT(*), ARRAY_AGG(user_id) as user_ids, ARRAY_AGG(email) as emails
                    FROM bm_users
                    WHERE hubspot_owner_id IS NOT NULL
                    GROUP BY hubspot_owner_id
                    HAVING COUNT(*) > 1;
                """))
                dups = res_dups.all()
                if dups:
                    logger.warning("DUPLICATE HUBSPOT OWNER IDS FOUND IN bm_users! Partial unique index cannot be created:")
                    for row in dups:
                        logger.warning("  hubspot_owner_id: '%s', count: %s, user_ids: %s, emails: %s", row[0], row[1], row[2], row[3])
                else:
                    logger.info("No duplicate hubspot_owner_id values found in bm_users. Applying uniqueness index...")
                    async with engine.begin() as conn:
                        await conn.execute(text("""
                            CREATE UNIQUE INDEX IF NOT EXISTS uq_idx_bm_users_hubspot_owner_id 
                            ON bm_users (hubspot_owner_id) 
                            WHERE hubspot_owner_id IS NOT NULL;
                        """))
                    logger.info("Unique index uq_idx_bm_users_hubspot_owner_id ensured successfully.")
            except Exception as e:
                logger.error("Failed to check or apply hubspot_owner_id uniqueness migration: %s", e)

        # 1.2.c Early dynamic column migration for bm_training_agent_settings (required before query/seeding)
        async with engine.begin() as conn:
            for col_name, col_type, col_default in [
                ("training_code", "TEXT", "NULL"),
                ("training_numeric_code", "TEXT", "NULL"),
                ("training_code_enabled", "BOOLEAN", "DEFAULT TRUE NOT NULL"),
                ("training_code_updated_at", "TIMESTAMPTZ", "DEFAULT CURRENT_TIMESTAMP NOT NULL"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = 'bm_training_agent_settings' 
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Early adding column '%s' to 'bm_training_agent_settings' table...", col_name)
                    await conn.execute(
                        text(f"ALTER TABLE bm_training_agent_settings ADD COLUMN {col_name} {col_type} {col_default};")
                    )
                    if col_name in ["training_code", "training_numeric_code"]:
                        try:
                            suffix = "code" if col_name == "training_code" else "numeric"
                            await conn.execute(text(f"ALTER TABLE bm_training_agent_settings ADD CONSTRAINT uq_bm_training_settings_{suffix} UNIQUE ({col_name});"))
                        except Exception as e_uq:
                            logger.warning("Could not add unique constraint to %s: %s", col_name, e_uq)
                    logger.info("Column '%s' added successfully to 'bm_training_agent_settings'.", col_name)

        # 1.3. Seed default training agents if bm_training_agent_settings is empty
        from app.models.personalized_training import TrainingAgentSetting
        async with AsyncSession(engine) as session:
            try:
                res = await session.execute(select(TrainingAgentSetting).limit(1))
                if not res.scalars().first():
                    logger.info("Seeding default training agents...")
                    default_agents = [
                        {"initials": "ST", "owner_id": "1459417733", "name": "Santiago Taboada"},
                        {"initials": "LD", "owner_id": "1375831790", "name": "Luci Dos Santos Furtado"},
                        {"initials": "FR", "owner_id": "1539993532", "name": "Fernanda Rodrigues"},
                        {"initials": "RG", "owner_id": "1375831787", "name": "Roberto Galán"},
                        {"initials": "EC", "owner_id": "1375831791", "name": "Eugenia Carreno"},
                        {"initials": "BH", "owner_id": "33013277", "name": "Bryan Herrera"},
                        {"initials": "CM", "owner_id": "33013276", "name": "Cristina Montenegro"},
                    ]
                    for agent in default_agents:
                        new_agent = TrainingAgentSetting(
                            hubspot_owner_id=agent["owner_id"],
                            agent_name=agent["name"],
                            agent_initials=agent["initials"],
                            is_enabled=True
                        )
                        session.add(new_agent)
                    await session.commit()
                    logger.info("Default training agents seeded successfully.")
                else:
                    logger.info("Training agents table is not empty, skipping seeding.")
            except Exception as e:
                logger.error("Failed to seed default training agents: %s", e)

        # 1.4. (bm_users columns ensured early in init_db)

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

        # 1.5.b Structure ownership columns and tables are managed via manual SQL migrations.
        # Startup dynamic alterations for permissions have been removed to prevent DDL executions in runtime.

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
                ("evaluacion_global", "NUMERIC(5, 2)"),
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

            # 1.6.6 bm_mass_evaluation_* tables — execution_source support columns
            for table_name in [
                "bm_mass_evaluation_jobs",
                "bm_mass_evaluation_runs",
                "bm_mass_evaluation_results",
                "bm_mass_evaluation_criterion_results"
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = '{table_name}' 
                              AND column_name = 'execution_source'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Adding column 'execution_source' to '%s' table...", table_name)
                    await conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN execution_source TEXT DEFAULT 'on_demand';")
                    )
                    logger.info("Column 'execution_source' added successfully to '%s'.", table_name)

            # Check for heartbeat_at in bm_mass_evaluation_runs
            res_heartbeat = await conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns 
                        WHERE table_schema = 'public' 
                          AND table_name = 'bm_mass_evaluation_runs' 
                          AND column_name = 'heartbeat_at'
                    );
                """)
            )
            if not res_heartbeat.scalar():
                logger.info("Adding column 'heartbeat_at' to 'bm_mass_evaluation_runs' table...")
                await conn.execute(
                    text("ALTER TABLE bm_mass_evaluation_runs ADD COLUMN heartbeat_at TIMESTAMPTZ NULL;")
                )
                logger.info("Column 'heartbeat_at' added successfully to 'bm_mass_evaluation_runs'.")

            # 1.6.7 bm_mass_analysis_automations — job_id support column
            res = await conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns 
                        WHERE table_schema = 'public' 
                          AND table_name = 'bm_mass_analysis_automations' 
                          AND column_name = 'job_id'
                    );
                """)
            )
            if not res.scalar():
                logger.info("Adding column 'job_id' to 'bm_mass_analysis_automations' table...")
                await conn.execute(
                    text("ALTER TABLE bm_mass_analysis_automations ADD COLUMN job_id INTEGER NULL;")
                )
                logger.info("Column 'job_id' added successfully to 'bm_mass_analysis_automations'.")

            # 1.6.8 Training voice roleplay support columns
            # 1.6.8.1 bm_training_agent_settings
            for col_name, col_type, col_default in [
                ("training_code", "TEXT", "NULL"),
                ("training_numeric_code", "TEXT", "NULL"),
                ("training_code_enabled", "BOOLEAN", "DEFAULT TRUE NOT NULL"),
                ("training_code_updated_at", "TIMESTAMPTZ", "DEFAULT CURRENT_TIMESTAMP NOT NULL"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = 'bm_training_agent_settings' 
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Adding column '%s' to 'bm_training_agent_settings' table...", col_name)
                    await conn.execute(
                        text(f"ALTER TABLE bm_training_agent_settings ADD COLUMN {col_name} {col_type} {col_default};")
                    )
                    if col_name in ["training_code", "training_numeric_code"]:
                        try:
                            suffix = "code" if col_name == "training_code" else "numeric"
                            await conn.execute(text(f"ALTER TABLE bm_training_agent_settings ADD CONSTRAINT uq_bm_training_settings_{suffix} UNIQUE ({col_name});"))
                        except Exception as e_uq:
                            logger.warning("Could not add unique constraint to %s: %s", col_name, e_uq)
                    logger.info("Column '%s' added successfully to 'bm_training_agent_settings'.", col_name)

            # 1.6.8.2 bm_training_agent_reports
            res = await conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns 
                        WHERE table_schema = 'public' 
                          AND table_name = 'bm_training_agent_reports' 
                          AND column_name = 'final_report_json'
                    );
                """)
            )
            if not res.scalar():
                logger.info("Adding column 'final_report_json' to 'bm_training_agent_reports' table...")
                await conn.execute(
                    text("ALTER TABLE bm_training_agent_reports ADD COLUMN final_report_json JSONB NULL;")
                )
                logger.info("Column 'final_report_json' added successfully to 'bm_training_agent_reports'.")

            # 1.6.8.3 bm_training_completion_status
            for col_name, col_type, col_ref in [
                ("call_session_id", "INTEGER", "REFERENCES bm_training_call_sessions(session_id) ON DELETE SET NULL"),
                ("evaluation_id", "INTEGER", "REFERENCES bm_training_call_evaluations(evaluation_id) ON DELETE SET NULL"),
            ]:
                res = await conn.execute(
                    text(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.columns 
                            WHERE table_schema = 'public' 
                              AND table_name = 'bm_training_completion_status' 
                              AND column_name = '{col_name}'
                        );
                    """)
                )
                if not res.scalar():
                    logger.info("Adding column '%s' to 'bm_training_completion_status' table...", col_name)
                    await conn.execute(
                        text(f"ALTER TABLE bm_training_completion_status ADD COLUMN {col_name} {col_type} {col_ref};")
                    )
                    logger.info("Column '%s' added successfully to 'bm_training_completion_status'.", col_name)

            # Drop individual views first to avoid InvalidTableDefinitionError if column names or positions changed
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_analysis_criteria_flat CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_analysis_results_pivot CASCADE;"))

            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_analysis_criteria_flat AS
                SELECT
                    a.analysis_id,
                    a.call_id,
                    a.created_at AS analyzed_at,
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
                    c.numeric_value,
                    c.boolean_value,
                    c.text_value,
                    c.category_value,
                    c.percentage_value,
                    c.feedback,
                    c.is_applicable,
                    c.not_applicable,
                    c.created_at,
                    c.updated_at
                FROM bm_analyses a
                JOIN bm_analysis_criterion_results c ON a.analysis_id = c.analysis_id;
            """))

            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_analysis_results_pivot AS
                SELECT
                    a.analysis_id,
                    a.call_id,
                    MAX(c.hs_object_id) AS hs_object_id,
                    a.created_at AS analyzed_at,
                    a.created_at,
                    a.call_timestamp,
                    a.call_timestamp::date AS call_date,
                    NULL::integer AS duration_seconds,
                    a.call_direction AS direction,
                    a.hubspot_owner_id AS agent_owner_id,
                    a.agente_telefonico AS agent_name,
                    a.status,
                    a.error_message,
                    
                    -- Prompt details
                    a.prompt_id,
                    p.prompt_name,
                    a.prompt_version_id,
                    pv.version_name AS prompt_version_name,
                    pv.version_label AS prompt_version_label,

                    -- Service / typology
                    MAX(c.service_id) AS service_id,
                    MAX(c.service_key) AS service_key,
                    MAX(c.service_name) AS service_name,
                    MAX(c.typology_id) AS typology_id,
                    MAX(c.typology_key) AS typology_key,
                    MAX(c.typology_name) AS typology_name,

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
                    MAX(CASE WHEN c.criterion_key = 'hablando_agente' AND c.is_applicable = true THEN COALESCE(c.percentage_value, c.numeric_value) END) AS hablando_agente,
                    MAX(CASE WHEN c.criterion_key = 'hablando_paciente' AND c.is_applicable = true THEN COALESCE(c.percentage_value, c.numeric_value) END) AS hablando_paciente,
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
                    MAX(CASE WHEN c.criterion_key = 'tipo_llamada' AND c.is_applicable = true THEN COALESCE(c.category_value, c.text_value) END) AS tipo_llamada,
                    MAX(CASE WHEN c.criterion_key = 'patologia' AND c.is_applicable = true THEN COALESCE(c.category_value, c.text_value) END) AS patologia,
                    MAX(CASE WHEN c.criterion_key = 'objeciones' AND c.is_applicable = true THEN c.text_value END) AS objeciones,
                    MAX(CASE WHEN c.criterion_key = 'objecion_1' AND c.is_applicable = true THEN COALESCE(c.category_value, c.text_value) END) AS objecion_1,
                    MAX(CASE WHEN c.criterion_key = 'objecion_2' AND c.is_applicable = true THEN COALESCE(c.category_value, c.text_value) END) AS objecion_2,
                    MAX(CASE WHEN c.criterion_key = 'objecion_3' AND c.is_applicable = true THEN COALESCE(c.category_value, c.text_value) END) AS objecion_3,
                    MAX(CASE WHEN c.criterion_key = 'motivo_no_cita' AND c.is_applicable = true THEN COALESCE(c.category_value, c.text_value) END) AS motivo_no_cita,
                    MAX(CASE WHEN c.criterion_key = 'cuanto_tiempo' AND c.is_applicable = true THEN c.text_value END) AS cuanto_tiempo,
                    MAX(CASE WHEN c.criterion_key = 'por_que_ahora' AND c.is_applicable = true THEN c.text_value END) AS por_que_ahora

                FROM bm_analyses a
                JOIN bm_analysis_criterion_results c ON a.analysis_id = c.analysis_id
                LEFT JOIN bm_prompts p ON a.prompt_id = p.prompt_id
                LEFT JOIN bm_prompt_versions pv ON a.prompt_version_id = pv.id
                GROUP BY
                    a.analysis_id,
                    a.call_id,
                    a.created_at,
                    a.call_timestamp,
                    a.call_direction,
                    a.hubspot_owner_id,
                    a.agente_telefonico,
                    a.status,
                    a.error_message,
                    a.prompt_id,
                    p.prompt_name,
                    a.prompt_version_id,
                    pv.version_name,
                    pv.version_label;
            """))

            # ── Looker-ready views: drop first (column order may have changed) ──────
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_criteria_flat CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_results_pivot CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_calls_summary CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_mass_evaluation_daily_summary CASCADE;"))

            # A) vw_bm_mass_evaluation_criteria_flat
            #    One row per (call × criterion). Safe for Looker exploration.
            #    evaluacion_global is NOT a criterion row; it lives in result_json.
            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_mass_evaluation_criteria_flat AS
                SELECT
                    r.mass_analysis_id          AS mass_evaluation_result_id,
                    r.call_id                   AS conversation_id,
                    r.job_id,
                    r.run_id,
                    r.hs_object_id,
                    COALESCE(c.service_id,   r.service_id)   AS service_id,
                    COALESCE(c.service_key,  r.service_key)  AS service_key,
                    COALESCE(c.service_name, r.service_name) AS service_name,
                    COALESCE(c.typology_id,   r.typology_id)   AS typology_id,
                    COALESCE(c.typology_key,  r.typology_key)  AS typology_key,
                    COALESCE(c.typology_name, r.typology_name) AS typology_name,
                    r.hubspot_owner_id          AS agent_owner_id,
                    r.agent_name,
                    r.call_timestamp,
                    r.call_timestamp::date      AS call_date,
                    r.call_duration_seconds     AS duration_seconds,
                    r.direction,
                    c.criterion_id,
                    c.criterion_key,
                    c.criterion_name,
                    CASE
                        WHEN c.criterion_key = 'trato_ustad' THEN 'trato_usted'
                        WHEN c.criterion_key = 'puntalidad' THEN 'puntualidad'
                        ELSE c.criterion_key
                    END AS canonical_criterion_key,
                    CASE
                        WHEN c.criterion_key = 'trato_ustad' OR c.criterion_key = 'trato_usted' THEN 'Trato de usted'
                        WHEN c.criterion_key = 'saludo_inicio' THEN 'Saludo e Identificación'
                        WHEN c.criterion_key = 'explicaciones_medicas' THEN 'Explicaciones médicas'
                        WHEN c.criterion_key IN ('puntalidad', 'puntualidad') THEN 'Puntualidad'
                        WHEN c.criterion_key = 'cierre_cita' THEN 'Cierre de cita'
                        WHEN c.criterion_key = 'n3_preguntas' THEN 'Tres preguntas clave'
                        WHEN c.criterion_key = 'tipo_llamada' THEN 'Tipo de llamada'
                        WHEN c.criterion_key = 'motivo_no_cita' THEN 'Motivo no cita'
                        WHEN c.criterion_key = 'duracion_consulta' THEN 'Duración de consulta'
                        WHEN c.criterion_key = 'precio_consulta' THEN 'Precio de consulta'
                        WHEN c.criterion_key = 'verifica_patologia' THEN 'Verifica patología'
                        WHEN c.criterion_key = 'reformula_patologia' THEN 'Reformula patología'
                        WHEN c.criterion_key = 'conocimiento_boston_medical' THEN 'Conocimiento previo de Boston Medical'
                        WHEN c.criterion_key = 'direccion_y_referencias' THEN 'Dirección y referencias'
                        WHEN c.criterion_key = 'medio' THEN 'Medio'
                        WHEN c.criterion_key = 'edad' THEN 'Edad'
                        WHEN c.criterion_key = 'patologia' THEN 'Patología'
                        WHEN c.criterion_key = 'objeciones' THEN 'Objeciones'
                        WHEN c.criterion_key = 'objecion_1' THEN 'Objeción principal'
                        WHEN c.criterion_key = 'objecion_2' THEN 'Segunda objeción'
                        WHEN c.criterion_key = 'objecion_3' THEN 'Tercera objeción'
                        WHEN c.criterion_key = 'puede_adelantar_cita' THEN 'Puede adelantar cita'
                        WHEN c.criterion_key = 'pregunta_pareja' THEN 'Pregunta por pareja'
                        WHEN c.criterion_key = 'recomienda_pareja' THEN 'Recomienda venir con pareja'
                        WHEN c.criterion_key = 'pareja_conocedora' THEN 'Pareja conocedora de la cita'
                        WHEN c.criterion_key = 'pareja_asistira' THEN 'Pareja asistirá a la cita'
                        WHEN c.criterion_key = 'claridad' THEN 'Claridad'
                        WHEN c.criterion_key = 'procedimiento' THEN 'Explicación del procedimiento'
                        WHEN c.criterion_key = 'gestion_objeciones' THEN 'Gestión de objeciones'
                        WHEN c.criterion_key = 'propension' THEN 'Propensión al cierre'
                        WHEN c.criterion_key = 'uso_preguntas' THEN 'Uso de preguntas'
                        WHEN c.criterion_key = 'uso_nombre_paciente' THEN 'Uso del nombre del paciente'
                        WHEN c.criterion_key = 'empatia' THEN 'Empatía'
                        WHEN c.criterion_key = 'simpatia' THEN 'Simpatía'
                        WHEN c.criterion_key = 'claridad_explicacion_economica' THEN 'Claridad en explicación económica'
                        WHEN c.criterion_key = 'claridad_de_explicacion_de_precio_en_consulta' THEN 'Claridad en precio de consulta'
                        WHEN c.criterion_key = 'despedida_con_refuerzo' THEN 'Despedida con refuerzo'
                        WHEN c.criterion_key = 'siguiente_paso' THEN 'Siguiente paso establecido'
                        WHEN c.criterion_key = 'velocidad_hablando_agente' THEN 'Velocidad hablando agente'
                        WHEN c.criterion_key = 'interrupciones' THEN 'Interrupciones'
                        WHEN c.criterion_key = 'sentiment' THEN 'Sentimiento de la llamada'
                        WHEN c.criterion_key = 'hablando_agente' THEN 'Porcentaje hablando agente'
                        WHEN c.criterion_key = 'hablando_paciente' THEN 'Porcentaje hablando paciente'
                        WHEN c.criterion_key = 'palabras_minuto_agente' THEN 'Palabras por minuto agente'
                        WHEN c.criterion_key = 'meses_patologia' THEN 'Meses con la patología'
                        WHEN c.criterion_key = 'tratamiento_no_en_precio' THEN 'Tratamiento no en precio'
                        ELSE COALESCE(c.criterion_name, INITCAP(REPLACE(c.criterion_key, '_', ' ')))
                    END AS canonical_criterion_name,
                    c.criterion_type,
                    c.is_applicable,
                    c.not_applicable,
                    c.value_raw                 AS raw_value,
                    COALESCE(
                        c.numeric_value,
                        CASE WHEN c.criterion_type = 'percentage' AND c.value_raw IS NOT NULL
                             THEN NULLIF(regexp_replace(c.value_raw#>>'{}', '[^0-9.]', '', 'g'), '')::numeric
                        END
                    ) AS numeric_value,
                    c.boolean_value,
                    c.text_value,
                    c.category_value,
                    COALESCE(
                        c.percentage_value,
                        CASE WHEN c.criterion_type = 'percentage' AND c.value_raw IS NOT NULL
                             THEN NULLIF(regexp_replace(c.value_raw#>>'{}', '[^0-9.]', '', 'g'), '')::numeric
                        END
                    ) AS percentage_value,
                    c.feedback,
                    r.prompt_id,
                    r.prompt_name,
                    r.prompt_version_id,
                    r.prompt_version_name,
                    r.prompt_version_label,
                    r.created_at                AS analysis_timestamp
                FROM bm_mass_evaluation_results r
                JOIN bm_mass_evaluation_criterion_results c
                    ON r.mass_analysis_id = c.mass_analysis_id
                WHERE r.status = 'completed';
            """))

            # B) vw_bm_mass_evaluation_calls_summary
            #    One row per call; key criteria pivoted as columns.
            #    evaluacion_global extracted from result_json JSONB.
            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_mass_evaluation_calls_summary AS
                SELECT
                    r.mass_analysis_id          AS mass_evaluation_result_id,
                    r.call_id                   AS conversation_id,
                    r.job_id,
                    r.run_id,
                    r.hs_object_id,
                    r.service_id,
                    r.service_key,
                    r.service_name,
                    r.typology_id,
                    r.typology_key,
                    r.typology_name,
                    r.hubspot_owner_id          AS agent_owner_id,
                    r.agent_name,
                    r.call_timestamp,
                    r.call_timestamp::date      AS call_date,
                    r.call_duration_seconds     AS duration_seconds,
                    r.direction,
                    r.prompt_id,
                    r.prompt_name,
                    r.prompt_version_id,
                    r.prompt_version_name,
                    r.prompt_version_label,
                    (r.result_json->>'evaluacion_global')::numeric AS evaluacion_global,
                    MAX(CASE WHEN c.criterion_key = 'tipo_llamada'   AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS tipo_llamada,
                    MAX(CASE WHEN c.criterion_key = 'patologia'      AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS patologia,
                    MAX(CASE WHEN c.criterion_key = 'objecion_1'     AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS objecion_1,
                    MAX(CASE WHEN c.criterion_key = 'objecion_2'     AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS objecion_2,
                    MAX(CASE WHEN c.criterion_key = 'objecion_3'     AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS objecion_3,
                    MAX(CASE WHEN c.criterion_key = 'motivo_no_cita' AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS motivo_no_cita,
                    MAX(CASE WHEN c.criterion_key = 'objeciones'     AND c.is_applicable THEN c.text_value END) AS objeciones,
                    BOOL_OR(CASE WHEN c.criterion_key = 'cierre_cita'              AND c.is_applicable THEN c.boolean_value END) AS cierre_cita,
                    BOOL_OR(CASE WHEN c.criterion_key = 'verifica_patologia'       AND c.is_applicable THEN c.boolean_value END) AS verifica_patologia,
                    BOOL_OR(CASE WHEN c.criterion_key = 'reformula_patologia'      AND c.is_applicable THEN c.boolean_value END) AS reformula_patologia,
                    BOOL_OR(CASE WHEN c.criterion_key = 'precio_consulta'          AND c.is_applicable THEN c.boolean_value END) AS precio_consulta,
                    BOOL_OR(CASE WHEN c.criterion_key = 'tratamiento_no_en_precio' AND c.is_applicable THEN c.boolean_value END) AS tratamiento_no_en_precio,
                    BOOL_OR(CASE WHEN c.criterion_key = 'duracion_consulta'        AND c.is_applicable THEN c.boolean_value END) AS duracion_consulta,
                    BOOL_OR(CASE WHEN c.criterion_key = 'direccion_y_referencias'  AND c.is_applicable THEN c.boolean_value END) AS direccion_y_referencias,
                    BOOL_OR(CASE WHEN c.criterion_key IN ('puntualidad','puntalidad') AND c.is_applicable THEN c.boolean_value END) AS puntualidad,
                    BOOL_OR(CASE WHEN c.criterion_key = 'conocimiento_boston_medical' AND c.is_applicable THEN c.boolean_value END) AS conocimiento_boston_medical,
                    BOOL_OR(CASE WHEN c.criterion_key = 'puede_adelantar_cita'     AND c.is_applicable THEN c.boolean_value END) AS puede_adelantar_cita,
                    BOOL_OR(CASE WHEN c.criterion_key = 'pregunta_pareja'          AND c.is_applicable THEN c.boolean_value END) AS pregunta_pareja,
                    BOOL_OR(CASE WHEN c.criterion_key = 'recomienda_pareja'        AND c.is_applicable THEN c.boolean_value END) AS recomienda_pareja,
                    BOOL_OR(CASE WHEN c.criterion_key = 'pareja_conocedora'        AND c.is_applicable THEN c.boolean_value END) AS pareja_conocedora,
                    BOOL_OR(CASE WHEN c.criterion_key = 'pareja_asistira'          AND c.is_applicable THEN c.boolean_value END) AS pareja_asistira,
                    BOOL_OR(CASE WHEN c.criterion_key = 'medio'                    AND c.is_applicable THEN c.boolean_value END) AS medio,
                    MAX(CASE WHEN c.criterion_key = 'claridad'                AND c.is_applicable THEN c.numeric_value END) AS claridad,
                    MAX(CASE WHEN c.criterion_key = 'procedimiento'           AND c.is_applicable THEN c.numeric_value END) AS procedimiento,
                    MAX(CASE WHEN c.criterion_key = 'n3_preguntas'            AND c.is_applicable THEN c.numeric_value END) AS n3_preguntas,
                    MAX(CASE WHEN c.criterion_key = 'gestion_objeciones'      AND c.is_applicable THEN c.numeric_value END) AS gestion_objeciones,
                    MAX(CASE WHEN c.criterion_key = 'propension'              AND c.is_applicable THEN c.numeric_value END) AS propension,
                    MAX(CASE WHEN c.criterion_key = 'saludo_inicio'           AND c.is_applicable THEN c.numeric_value END) AS saludo_inicio,
                    MAX(CASE WHEN c.criterion_key = 'uso_preguntas'           AND c.is_applicable THEN c.numeric_value END) AS uso_preguntas,
                    MAX(CASE WHEN c.criterion_key = 'uso_nombre_paciente'     AND c.is_applicable THEN c.numeric_value END) AS uso_nombre_paciente,
                    MAX(CASE WHEN c.criterion_key IN ('trato_usted', 'trato_ustad') AND c.is_applicable THEN c.numeric_value END) AS trato_usted,
                    MAX(CASE WHEN c.criterion_key = 'empatia'                 AND c.is_applicable THEN c.numeric_value END) AS empatia,
                    MAX(CASE WHEN c.criterion_key = 'simpatia'                AND c.is_applicable THEN c.numeric_value END) AS simpatia,
                    MAX(CASE WHEN c.criterion_key = 'explicaciones_medicas'   AND c.is_applicable THEN c.numeric_value END) AS explicaciones_medicas,
                    MAX(CASE WHEN c.criterion_key = 'claridad_explicacion_economica' AND c.is_applicable THEN c.numeric_value END) AS claridad_explicacion_economica,
                    MAX(CASE WHEN c.criterion_key = 'claridad_de_explicacion_de_precio_en_consulta' AND c.is_applicable THEN c.numeric_value END) AS claridad_de_explicacion_de_precio_en_consulta,
                    MAX(CASE WHEN c.criterion_key = 'despedida_con_refuerzo'  AND c.is_applicable THEN c.numeric_value END) AS despedida_con_refuerzo,
                    MAX(CASE WHEN c.criterion_key = 'siguiente_paso'          AND c.is_applicable THEN c.numeric_value END) AS siguiente_paso,
                    MAX(CASE WHEN c.criterion_key = 'velocidad_hablando_agente' AND c.is_applicable THEN c.numeric_value END) AS velocidad_hablando_agente,
                    MAX(CASE WHEN c.criterion_key = 'interrupciones'          AND c.is_applicable THEN c.numeric_value END) AS interrupciones,
                    MAX(CASE WHEN c.criterion_key = 'sentiment'               AND c.is_applicable THEN c.numeric_value END) AS sentiment,
                    MAX(CASE WHEN c.criterion_key = 'hablando_agente'         AND c.is_applicable THEN COALESCE(c.percentage_value, c.numeric_value) END) AS hablando_agente_pct,
                    MAX(CASE WHEN c.criterion_key = 'hablando_paciente'       AND c.is_applicable THEN COALESCE(c.percentage_value, c.numeric_value) END) AS hablando_paciente_pct,
                    MAX(CASE WHEN c.criterion_key = 'palabras_minuto_agente'  AND c.is_applicable THEN c.numeric_value END) AS palabras_minuto_agente,
                    MAX(CASE WHEN c.criterion_key = 'meses_patologia'         AND c.is_applicable THEN c.numeric_value END) AS meses_patologia,
                    r.created_at                AS analysis_timestamp
                FROM bm_mass_evaluation_results r
                JOIN bm_mass_evaluation_criterion_results c
                    ON r.mass_analysis_id = c.mass_analysis_id
                WHERE r.status = 'completed'
                GROUP BY
                    r.mass_analysis_id, r.call_id, r.job_id, r.run_id, r.hs_object_id,
                    r.service_id, r.service_key, r.service_name,
                    r.typology_id, r.typology_key, r.typology_name,
                    r.hubspot_owner_id, r.agent_name,
                    r.call_timestamp, r.call_duration_seconds, r.direction,
                    r.prompt_id, r.prompt_name, r.prompt_version_id,
                    r.prompt_version_name, r.prompt_version_label,
                    r.result_json, r.created_at;
            """))

            # C) vw_bm_mass_evaluation_daily_summary  (operational aggregation, unchanged)
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
                        THEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value = true THEN 1 END)::numeric
                             / COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END)
                        ELSE NULL
                    END AS yes_rate,
                    CASE
                        WHEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END) > 0
                        THEN COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value = false THEN 1 END)::numeric
                             / COUNT(CASE WHEN c.is_applicable = true AND c.boolean_value IS NOT NULL THEN 1 END)
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
            logger.info("Reporting views ensured (criteria_flat, calls_summary, daily_summary).")

            # ── Individual analysis Looker views ──────────────────────────────
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_individual_analysis_criteria_flat CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_individual_analysis_summary CASCADE;"))
            await conn.execute(text("DROP VIEW IF EXISTS vw_bm_all_analysis_criteria_flat CASCADE;"))

            # C) vw_bm_individual_analysis_criteria_flat
            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_individual_analysis_criteria_flat AS
                SELECT
                    a.analysis_id,
                    a.call_id                       AS conversation_id,
                    a.analysis_type,
                    a.hubspot_owner_id              AS agent_owner_id,
                    a.agente_telefonico             AS agent_name,
                    a.call_timestamp,
                    a.call_timestamp::date          AS call_date,
                    a.fecha_eval::date              AS eval_date,
                    a.tipo_llamada,
                    a.source,
                    ar.criterion_id,
                    ar.criterion_key,
                    COALESCE(ar.criterion_name, INITCAP(REPLACE(ar.criterion_key, '_', ' '))) AS criterion_name,
                    CASE
                        WHEN ar.criterion_key = 'trato_ustad' THEN 'trato_usted'
                        WHEN ar.criterion_key = 'puntalidad' THEN 'puntualidad'
                        ELSE ar.criterion_key
                    END AS canonical_criterion_key,
                    CASE
                        WHEN ar.criterion_key = 'trato_ustad' OR ar.criterion_key = 'trato_usted' THEN 'Trato de usted'
                        WHEN ar.criterion_key = 'saludo_inicio' THEN 'Saludo e Identificación'
                        WHEN ar.criterion_key = 'explicaciones_medicas' THEN 'Explicaciones médicas'
                        WHEN ar.criterion_key IN ('puntalidad', 'puntualidad') THEN 'Puntualidad'
                        WHEN ar.criterion_key = 'cierre_cita' THEN 'Cierre de cita'
                        WHEN ar.criterion_key = 'n3_preguntas' THEN 'Tres preguntas clave'
                        WHEN ar.criterion_key = 'tipo_llamada' THEN 'Tipo de llamada'
                        WHEN ar.criterion_key = 'motivo_no_cita' THEN 'Motivo no cita'
                        WHEN ar.criterion_key = 'duracion_consulta' THEN 'Duración de consulta'
                        WHEN ar.criterion_key = 'precio_consulta' THEN 'Precio de consulta'
                        WHEN ar.criterion_key = 'verifica_patologia' THEN 'Verifica patología'
                        WHEN ar.criterion_key = 'reformula_patologia' THEN 'Reformula patología'
                        WHEN ar.criterion_key = 'conocimiento_boston_medical' THEN 'Conocimiento previo de Boston Medical'
                        WHEN ar.criterion_key = 'direccion_y_referencias' THEN 'Dirección y referencias'
                        WHEN ar.criterion_key = 'medio' THEN 'Medio'
                        WHEN ar.criterion_key = 'edad' THEN 'Edad'
                        WHEN ar.criterion_key = 'patologia' THEN 'Patología'
                        WHEN ar.criterion_key = 'objeciones' THEN 'Objeciones'
                        WHEN ar.criterion_key = 'objecion_1' THEN 'Objeción principal'
                        WHEN ar.criterion_key = 'objecion_2' THEN 'Segunda objeción'
                        WHEN ar.criterion_key = 'objecion_3' THEN 'Tercera objeción'
                        WHEN ar.criterion_key = 'puede_adelantar_cita' THEN 'Puede adelantar cita'
                        WHEN ar.criterion_key = 'pregunta_pareja' THEN 'Pregunta por pareja'
                        WHEN ar.criterion_key = 'recomienda_pareja' THEN 'Recomienda venir con pareja'
                        WHEN ar.criterion_key = 'pareja_conocedora' THEN 'Pareja conocedora de la cita'
                        WHEN ar.criterion_key = 'pareja_asistira' THEN 'Pareja asistirá a la cita'
                        WHEN ar.criterion_key = 'claridad' THEN 'Claridad'
                        WHEN ar.criterion_key = 'procedimiento' THEN 'Explicación del procedimiento'
                        WHEN ar.criterion_key = 'gestion_objeciones' THEN 'Gestión de objeciones'
                        WHEN ar.criterion_key = 'propension' THEN 'Propensión al cierre'
                        WHEN ar.criterion_key = 'uso_preguntas' THEN 'Uso de preguntas'
                        WHEN ar.criterion_key = 'uso_nombre_paciente' THEN 'Uso del nombre del paciente'
                        WHEN ar.criterion_key = 'empatia' THEN 'Empatía'
                        WHEN ar.criterion_key = 'simpatia' THEN 'Simpatía'
                        WHEN ar.criterion_key = 'claridad_explicacion_economica' THEN 'Claridad en explicación económica'
                        WHEN ar.criterion_key = 'claridad_de_explicacion_de_precio_en_consulta' THEN 'Claridad en precio de consulta'
                        WHEN ar.criterion_key = 'despedida_con_refuerzo' THEN 'Despedida con refuerzo'
                        WHEN ar.criterion_key = 'siguiente_paso' THEN 'Siguiente paso establecido'
                        WHEN ar.criterion_key = 'velocidad_hablando_agente' THEN 'Velocidad hablando agente'
                        WHEN ar.criterion_key = 'interrupciones' THEN 'Interrupciones'
                        WHEN ar.criterion_key = 'sentiment' THEN 'Sentimiento de la llamada'
                        WHEN ar.criterion_key = 'hablando_agente' THEN 'Porcentaje hablando agente'
                        WHEN ar.criterion_key = 'hablando_paciente' THEN 'Porcentaje hablando paciente'
                        WHEN ar.criterion_key = 'palabras_minuto_agente' THEN 'Palabras por minuto agente'
                        WHEN ar.criterion_key = 'meses_patologia' THEN 'Meses con la patología'
                        WHEN ar.criterion_key = 'tratamiento_no_en_precio' THEN 'Tratamiento no en precio'
                        ELSE COALESCE(ar.criterion_name, INITCAP(REPLACE(ar.criterion_key, '_', ' ')))
                    END AS canonical_criterion_name,
                    ar.criterion_type,
                    ar.raw_value,
                    COALESCE(
                        ar.value_number,
                        CASE WHEN ar.criterion_type = 'percentage' AND ar.raw_value IS NOT NULL
                             THEN NULLIF(regexp_replace(ar.raw_value#>>'{}', '[^0-9.]', '', 'g'), '')::numeric
                        END
                    ) AS numeric_value,
                    ar.value_boolean                AS boolean_value,
                    ar.value_text                   AS text_value,
                    ar.value_category               AS category_value,
                    CASE WHEN ar.criterion_type = 'percentage' AND ar.raw_value IS NOT NULL
                         THEN NULLIF(regexp_replace(ar.raw_value#>>'{}', '[^0-9.]', '', 'g'), '')::numeric
                    END                             AS percentage_value,
                    ar.feed                         AS feedback,
                    a.prompt_id,
                    a.prompt_version_id,
                    a.created_at                    AS analysis_timestamp,
                    'individual_legacy'::text       AS source_type
                FROM bm_analyses a
                JOIN bm_analysis_results ar ON a.analysis_id = ar.analysis_id
                WHERE a.status = 'completed'
                UNION ALL
                SELECT
                    a.analysis_id,
                    a.call_id                       AS conversation_id,
                    a.analysis_type,
                    a.hubspot_owner_id              AS agent_owner_id,
                    a.agente_telefonico             AS agent_name,
                    a.call_timestamp,
                    a.call_timestamp::date          AS call_date,
                    a.fecha_eval::date              AS eval_date,
                    a.tipo_llamada,
                    a.source,
                    acr.criterion_id,
                    acr.criterion_key,
                    COALESCE(acr.criterion_name, INITCAP(REPLACE(acr.criterion_key, '_', ' '))) AS criterion_name,
                    CASE
                        WHEN acr.criterion_key = 'trato_ustad' THEN 'trato_usted'
                        WHEN acr.criterion_key = 'puntalidad' THEN 'puntualidad'
                        ELSE acr.criterion_key
                    END AS canonical_criterion_key,
                    CASE
                        WHEN acr.criterion_key = 'trato_ustad' OR acr.criterion_key = 'trato_usted' THEN 'Trato de usted'
                        WHEN acr.criterion_key = 'saludo_inicio' THEN 'Saludo e Identificación'
                        WHEN acr.criterion_key = 'explicaciones_medicas' THEN 'Explicaciones médicas'
                        WHEN acr.criterion_key IN ('puntalidad', 'puntualidad') THEN 'Puntualidad'
                        WHEN acr.criterion_key = 'cierre_cita' THEN 'Cierre de cita'
                        WHEN acr.criterion_key = 'n3_preguntas' THEN 'Tres preguntas clave'
                        WHEN acr.criterion_key = 'tipo_llamada' THEN 'Tipo de llamada'
                        WHEN acr.criterion_key = 'motivo_no_cita' THEN 'Motivo no cita'
                        WHEN acr.criterion_key = 'duracion_consulta' THEN 'Duración de consulta'
                        WHEN acr.criterion_key = 'precio_consulta' THEN 'Precio de consulta'
                        WHEN acr.criterion_key = 'verifica_patologia' THEN 'Verifica patología'
                        WHEN acr.criterion_key = 'reformula_patologia' THEN 'Reformula patología'
                        WHEN acr.criterion_key = 'conocimiento_boston_medical' THEN 'Conocimiento previo de Boston Medical'
                        WHEN acr.criterion_key = 'direccion_y_referencias' THEN 'Dirección y referencias'
                        WHEN acr.criterion_key = 'medio' THEN 'Medio'
                        WHEN acr.criterion_key = 'edad' THEN 'Edad'
                        WHEN acr.criterion_key = 'patologia' THEN 'Patología'
                        WHEN acr.criterion_key = 'objeciones' THEN 'Objeciones'
                        WHEN acr.criterion_key = 'objecion_1' THEN 'Objeción principal'
                        WHEN acr.criterion_key = 'objecion_2' THEN 'Segunda objeción'
                        WHEN acr.criterion_key = 'objecion_3' THEN 'Tercera objeción'
                        WHEN acr.criterion_key = 'puede_adelantar_cita' THEN 'Puede adelantar cita'
                        WHEN acr.criterion_key = 'pregunta_pareja' THEN 'Pregunta por pareja'
                        WHEN acr.criterion_key = 'recomienda_pareja' THEN 'Recomienda venir con pareja'
                        WHEN acr.criterion_key = 'pareja_conocedora' THEN 'Pareja conocedora de la cita'
                        WHEN acr.criterion_key = 'pareja_asistira' THEN 'Pareja asistirá a la cita'
                        WHEN acr.criterion_key = 'claridad' THEN 'Claridad'
                        WHEN acr.criterion_key = 'procedimiento' THEN 'Explicación del procedimiento'
                        WHEN acr.criterion_key = 'gestion_objeciones' THEN 'Gestión de objeciones'
                        WHEN acr.criterion_key = 'propension' THEN 'Propensión al cierre'
                        WHEN acr.criterion_key = 'uso_preguntas' THEN 'Uso de preguntas'
                        WHEN acr.criterion_key = 'uso_nombre_paciente' THEN 'Uso del nombre del paciente'
                        WHEN acr.criterion_key = 'empatia' THEN 'Empatía'
                        WHEN acr.criterion_key = 'simpatia' THEN 'Simpatía'
                        WHEN acr.criterion_key = 'claridad_explicacion_economica' THEN 'Claridad en explicación económica'
                        WHEN acr.criterion_key = 'claridad_de_explicacion_de_precio_en_consulta' THEN 'Claridad en precio de consulta'
                        WHEN acr.criterion_key = 'despedida_con_refuerzo' THEN 'Despedida con refuerzo'
                        WHEN acr.criterion_key = 'siguiente_paso' THEN 'Siguiente paso establecido'
                        WHEN acr.criterion_key = 'velocidad_hablando_agente' THEN 'Velocidad hablando agente'
                        WHEN acr.criterion_key = 'interrupciones' THEN 'Interrupciones'
                        WHEN acr.criterion_key = 'sentiment' THEN 'Sentimiento de la llamada'
                        WHEN acr.criterion_key = 'hablando_agente' THEN 'Porcentaje hablando agente'
                        WHEN acr.criterion_key = 'hablando_paciente' THEN 'Porcentaje hablando paciente'
                        WHEN acr.criterion_key = 'palabras_minuto_agente' THEN 'Palabras por minuto agente'
                        WHEN acr.criterion_key = 'meses_patologia' THEN 'Meses con la patología'
                        WHEN acr.criterion_key = 'tratamiento_no_en_precio' THEN 'Tratamiento no en precio'
                        ELSE COALESCE(acr.criterion_name, INITCAP(REPLACE(acr.criterion_key, '_', ' ')))
                    END AS canonical_criterion_name,
                    acr.criterion_type,
                    acr.value_raw                   AS raw_value,
                    COALESCE(
                        acr.numeric_value,
                        CASE WHEN acr.criterion_type = 'percentage' AND acr.value_raw IS NOT NULL
                             THEN NULLIF(regexp_replace(acr.value_raw#>>'{}', '[^0-9.]', '', 'g'), '')::numeric
                        END
                    ) AS numeric_value,
                    acr.boolean_value,
                    acr.text_value,
                    acr.category_value,
                    COALESCE(
                        acr.percentage_value,
                        CASE WHEN acr.criterion_type = 'percentage' AND acr.value_raw IS NOT NULL
                             THEN NULLIF(regexp_replace(acr.value_raw#>>'{}', '[^0-9.]', '', 'g'), '')::numeric
                        END
                    ) AS percentage_value,
                    acr.feedback,
                    a.prompt_id,
                    a.prompt_version_id,
                    a.created_at                    AS analysis_timestamp,
                    'individual'::text              AS source_type
                FROM bm_analyses a
                JOIN bm_analysis_criterion_results acr ON a.analysis_id = acr.analysis_id
                WHERE a.status = 'completed';
            """))

            # D) vw_bm_individual_analysis_summary
            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_individual_analysis_summary AS
                SELECT
                    a.analysis_id,
                    a.call_id                       AS conversation_id,
                    a.analysis_type,
                    a.hubspot_owner_id              AS agent_owner_id,
                    a.agente_telefonico             AS agent_name,
                    a.call_timestamp,
                    a.call_timestamp::date          AS call_date,
                    a.fecha_eval::date              AS eval_date,
                    a.tipo_llamada,
                    a.source,
                    a.prompt_id,
                    a.prompt_version_id,
                    a.created_at                    AS analysis_timestamp,
                    a.evaluacion_global,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'tipo_llamada' THEN COALESCE(ar.value_category, ar.value_text) END),
                        MAX(CASE WHEN acr.criterion_key = 'tipo_llamada' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END),
                        a.tipo_llamada) AS tipo_llamada_criterio,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'patologia' THEN COALESCE(ar.value_category, ar.value_text) END),
                        MAX(CASE WHEN acr.criterion_key = 'patologia' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)) AS patologia,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'objecion_1' THEN COALESCE(ar.value_category, ar.value_text) END),
                        MAX(CASE WHEN acr.criterion_key = 'objecion_1' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)) AS objecion_1,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'objecion_2' THEN COALESCE(ar.value_category, ar.value_text) END),
                        MAX(CASE WHEN acr.criterion_key = 'objecion_2' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)) AS objecion_2,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'objecion_3' THEN COALESCE(ar.value_category, ar.value_text) END),
                        MAX(CASE WHEN acr.criterion_key = 'objecion_3' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)) AS objecion_3,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'motivo_no_cita' THEN COALESCE(ar.value_category, ar.value_text) END),
                        MAX(CASE WHEN acr.criterion_key = 'motivo_no_cita' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)) AS motivo_no_cita,
                    COALESCE(
                        BOOL_OR(CASE WHEN ar.criterion_key = 'cierre_cita' THEN ar.value_boolean END),
                        BOOL_OR(CASE WHEN acr.criterion_key = 'cierre_cita' AND acr.is_applicable THEN acr.boolean_value END)) AS cierre_cita,
                    COALESCE(
                        BOOL_OR(CASE WHEN ar.criterion_key = 'verifica_patologia' THEN ar.value_boolean END),
                        BOOL_OR(CASE WHEN acr.criterion_key = 'verifica_patologia' AND acr.is_applicable THEN acr.boolean_value END)) AS verifica_patologia,
                    COALESCE(
                        BOOL_OR(CASE WHEN ar.criterion_key IN ('puntualidad','puntalidad') THEN ar.value_boolean END),
                        BOOL_OR(CASE WHEN acr.criterion_key IN ('puntualidad','puntalidad') AND acr.is_applicable THEN acr.boolean_value END)) AS puntualidad,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'claridad' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'claridad' AND acr.is_applicable THEN acr.numeric_value END)) AS claridad,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'procedimiento' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'procedimiento' AND acr.is_applicable THEN acr.numeric_value END)) AS procedimiento,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'n3_preguntas' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'n3_preguntas' AND acr.is_applicable THEN acr.numeric_value END)) AS n3_preguntas,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'gestion_objeciones' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'gestion_objeciones' AND acr.is_applicable THEN acr.numeric_value END)) AS gestion_objeciones,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'propension' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'propension' AND acr.is_applicable THEN acr.numeric_value END)) AS propension,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'saludo_inicio' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'saludo_inicio' AND acr.is_applicable THEN acr.numeric_value END)) AS saludo_inicio,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'uso_preguntas' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'uso_preguntas' AND acr.is_applicable THEN acr.numeric_value END)) AS uso_preguntas,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'empatia' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'empatia' AND acr.is_applicable THEN acr.numeric_value END)) AS empatia,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'simpatia' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'simpatia' AND acr.is_applicable THEN acr.numeric_value END)) AS simpatia,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key = 'sentiment' THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key = 'sentiment' AND acr.is_applicable THEN acr.numeric_value END)) AS sentiment,
                    COALESCE(
                        MAX(CASE WHEN ar.criterion_key IN ('trato_usted', 'trato_ustad') THEN ar.value_number END),
                        MAX(CASE WHEN acr.criterion_key IN ('trato_usted', 'trato_ustad') AND acr.is_applicable THEN acr.numeric_value END)) AS trato_usted
                FROM bm_analyses a
                LEFT JOIN bm_analysis_results ar ON a.analysis_id = ar.analysis_id
                LEFT JOIN bm_analysis_criterion_results acr ON a.analysis_id = acr.analysis_id
                WHERE a.status = 'completed'
                GROUP BY
                    a.analysis_id, a.call_id, a.analysis_type,
                    a.hubspot_owner_id, a.agente_telefonico,
                    a.call_timestamp, a.fecha_eval, a.tipo_llamada,
                    a.source, a.evaluacion_global, a.prompt_id,
                    a.prompt_version_id, a.created_at;
            """))

            # E) vw_bm_all_analysis_criteria_flat (unified UNION ALL)
            await conn.execute(text("""
                CREATE OR REPLACE VIEW vw_bm_all_analysis_criteria_flat AS
                SELECT
                    mass_evaluation_result_id AS analysis_id, conversation_id,
                    'mass'::text AS analysis_source,
                    service_key, service_name, agent_owner_id, agent_name,
                    call_timestamp, call_date, typology_key, typology_name,
                    criterion_id, criterion_key, criterion_name,
                    canonical_criterion_key, canonical_criterion_name,
                    criterion_type,
                    raw_value, numeric_value, boolean_value, text_value, category_value,
                    percentage_value, feedback, is_applicable, analysis_timestamp
                FROM vw_bm_mass_evaluation_criteria_flat
                UNION ALL
                SELECT
                    analysis_id, conversation_id, source_type AS analysis_source,
                    NULL::text AS service_key, NULL::text AS service_name,
                    agent_owner_id, agent_name, call_timestamp, call_date,
                    NULL::text AS typology_key, NULL::text AS typology_name,
                    criterion_id, criterion_key, criterion_name,
                    canonical_criterion_key, canonical_criterion_name,
                    criterion_type,
                    raw_value, numeric_value, boolean_value, text_value, category_value,
                    percentage_value, feedback, TRUE AS is_applicable, analysis_timestamp
                FROM vw_bm_individual_analysis_criteria_flat;
            """))
            logger.info("Individual analysis Looker views ensured (individual_criteria_flat, individual_summary, all_analysis_criteria_flat).")

            # F) vw_bm_mass_evaluation_report_wide & vw_bm_individual_analysis_report_wide
            import os
            migration_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "migrations",
                "v004_looker_wide_views.sql"
            )
            if os.path.exists(migration_path):
                logger.info(f"Applying Looker wide reporting views migration from {migration_path}...")
                with open(migration_path, "r", encoding="utf-8") as mf:
                    sql_content = mf.read()
                # Split statements by semicolon and execute them
                statements = [stmt.strip() for stmt in sql_content.split(";") if stmt.strip()]
                for stmt in statements:
                    await conn.execute(text(stmt))
                logger.info("Looker wide reporting views migration applied successfully.")
            else:
                msg = f"CRITICAL: Looker wide reporting views migration file not found at {migration_path}! Rolling back transaction."
                logger.error(msg)
                raise FileNotFoundError(msg)



        # Isolated backfill block was removed to prevent destructive wiping of base structure criteria and prompt types on startup.
        logger.info("Skipping legacy isolated backfill to preserve custom base structures and criteria.")

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
