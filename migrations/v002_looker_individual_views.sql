-- =============================================================================
-- Migration: v002_looker_individual_views.sql
-- Description: Looker-ready views for BM Individual Analyses (bm_analyses).
--
-- Table mapping (from real schema inspection):
--   bm_analyses               → one row per analysis (29 rows, completed)
--   bm_analysis_results       → criteria rows (1085 rows) — ACTIVE legacy table
--                               columns: value_number, value_text, value_boolean,
--                                        value_category, criterion_key, criterion_name,
--                                        criterion_type, feed, raw_value
--   bm_analysis_criterion_results → new normalized table (0 rows currently, future use)
--
-- NOTE: bm_analyses has no service_id/service_key (different from mass eval).
--       evaluacion_global IS a direct column on bm_analyses.
--       Both bm_analysis_results AND bm_analysis_criterion_results are unioned
--       in the flat view so we're ready when the new table receives data.
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- C) vw_bm_individual_analysis_criteria_flat
--    One row per (analysis × criterion). Sources:
--      - bm_analysis_results       (legacy, active)
--      - bm_analysis_criterion_results (new, future)
--    Looker can filter by source_type if needed.
-- ─────────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS vw_bm_individual_analysis_criteria_flat CASCADE;

CREATE OR REPLACE VIEW vw_bm_individual_analysis_criteria_flat AS

-- ── Source 1: bm_analysis_results (legacy active table) ──────────────────────
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
    -- Criterion identity
    ar.criterion_id,
    ar.criterion_key,
    COALESCE(ar.criterion_name,
        INITCAP(REPLACE(ar.criterion_key, '_', ' ')))   AS criterion_name,
        
    -- Canonical fields for Looker grouping
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
    -- Values
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
    -- Traceability
    a.prompt_id,
    a.prompt_version_id,
    a.created_at                    AS analysis_timestamp,
    'individual_legacy'::text       AS source_type

FROM bm_analyses a
JOIN bm_analysis_results ar ON a.analysis_id = ar.analysis_id
WHERE a.status = 'completed'

UNION ALL

-- ── Source 2: bm_analysis_criterion_results (new normalized table) ────────────
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
    -- Criterion identity
    acr.criterion_id,
    acr.criterion_key,
    COALESCE(acr.criterion_name,
        INITCAP(REPLACE(acr.criterion_key, '_', ' ')))  AS criterion_name,
        
    -- Canonical fields for Looker grouping
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
    -- Values
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
    -- Traceability
    a.prompt_id,
    a.prompt_version_id,
    a.created_at                    AS analysis_timestamp,
    'individual'::text              AS source_type

FROM bm_analyses a
JOIN bm_analysis_criterion_results acr ON a.analysis_id = acr.analysis_id
WHERE a.status = 'completed';


-- ─────────────────────────────────────────────────────────────────────────────
-- D) vw_bm_individual_analysis_summary
--    One row per analysis. Key criteria pivoted as columns.
--    Uses bm_analysis_results (legacy active) + bm_analysis_criterion_results (future).
--    evaluacion_global is a direct column on bm_analyses.
-- ─────────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS vw_bm_individual_analysis_summary CASCADE;

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

    -- evaluacion_global is a direct column (not in criteria table)
    a.evaluacion_global,

    -- ── Criteria pivoted from bm_analysis_results (legacy) ─────────────────
    -- NOTE: new rows from bm_analysis_criterion_results use COALESCE fallback
    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'tipo_llamada'  THEN COALESCE(ar.value_category, ar.value_text) END),
        MAX(CASE WHEN acr.criterion_key = 'tipo_llamada' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END),
        a.tipo_llamada
    ) AS tipo_llamada_criterio,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'patologia'  THEN COALESCE(ar.value_category, ar.value_text) END),
        MAX(CASE WHEN acr.criterion_key = 'patologia' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)
    ) AS patologia,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'objecion_1'  THEN COALESCE(ar.value_category, ar.value_text) END),
        MAX(CASE WHEN acr.criterion_key = 'objecion_1' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)
    ) AS objecion_1,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'objecion_2'  THEN COALESCE(ar.value_category, ar.value_text) END),
        MAX(CASE WHEN acr.criterion_key = 'objecion_2' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)
    ) AS objecion_2,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'objecion_3'  THEN COALESCE(ar.value_category, ar.value_text) END),
        MAX(CASE WHEN acr.criterion_key = 'objecion_3' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)
    ) AS objecion_3,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'motivo_no_cita'  THEN COALESCE(ar.value_category, ar.value_text) END),
        MAX(CASE WHEN acr.criterion_key = 'motivo_no_cita' AND acr.is_applicable THEN COALESCE(acr.category_value, acr.text_value) END)
    ) AS motivo_no_cita,

    -- Boolean criteria (COALESCE because OR between aggregates is invalid in PostgreSQL)
    COALESCE(
        BOOL_OR(CASE WHEN ar.criterion_key = 'cierre_cita' THEN ar.value_boolean END),
        BOOL_OR(CASE WHEN acr.criterion_key = 'cierre_cita' AND acr.is_applicable THEN acr.boolean_value END)
    ) AS cierre_cita,

    COALESCE(
        BOOL_OR(CASE WHEN ar.criterion_key = 'verifica_patologia' THEN ar.value_boolean END),
        BOOL_OR(CASE WHEN acr.criterion_key = 'verifica_patologia' AND acr.is_applicable THEN acr.boolean_value END)
    ) AS verifica_patologia,

    COALESCE(
        BOOL_OR(CASE WHEN ar.criterion_key = 'reformula_patologia' THEN ar.value_boolean END),
        BOOL_OR(CASE WHEN acr.criterion_key = 'reformula_patologia' AND acr.is_applicable THEN acr.boolean_value END)
    ) AS reformula_patologia,

    COALESCE(
        BOOL_OR(CASE WHEN ar.criterion_key IN ('puntualidad','puntalidad') THEN ar.value_boolean END),
        BOOL_OR(CASE WHEN acr.criterion_key IN ('puntualidad','puntalidad') AND acr.is_applicable THEN acr.boolean_value END)
    ) AS puntualidad,

    -- Numeric criteria
    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'claridad'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'claridad' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS claridad,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'procedimiento'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'procedimiento' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS procedimiento,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'n3_preguntas'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'n3_preguntas' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS n3_preguntas,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'gestion_objeciones'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'gestion_objeciones' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS gestion_objeciones,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'propension'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'propension' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS propension,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'saludo_inicio'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'saludo_inicio' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS saludo_inicio,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'uso_preguntas'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'uso_preguntas' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS uso_preguntas,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'empatia'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'empatia' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS empatia,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'simpatia'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'simpatia' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS simpatia,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'sentiment'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'sentiment' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS sentiment,

    COALESCE(
        MAX(CASE WHEN ar.criterion_key = 'trato_usted'  THEN ar.value_number END),
        MAX(CASE WHEN acr.criterion_key = 'trato_usted' AND acr.is_applicable THEN acr.numeric_value END)
    ) AS trato_usted

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


-- ─────────────────────────────────────────────────────────────────────────────
-- E) vw_bm_all_analysis_criteria_flat
--    UNION ALL of mass + individual analyses. analysis_source distinguishes origin.
--    Looker can filter by analysis_source or analyze everything together.
-- ─────────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS vw_bm_all_analysis_criteria_flat CASCADE;

CREATE OR REPLACE VIEW vw_bm_all_analysis_criteria_flat AS

-- Mass evaluations
SELECT
    mass_evaluation_result_id       AS analysis_id,
    conversation_id,
    'mass'::text                    AS analysis_source,
    service_key,
    service_name,
    agent_owner_id,
    agent_name,
    call_timestamp,
    call_date,
    typology_key,
    typology_name,
    criterion_id,
    criterion_key,
    criterion_name,
    canonical_criterion_key,
    canonical_criterion_name,
    criterion_type,
    raw_value,
    numeric_value,
    boolean_value,
    text_value,
    category_value,
    percentage_value,
    feedback,
    is_applicable,
    analysis_timestamp
FROM vw_bm_mass_evaluation_criteria_flat

UNION ALL

-- Individual analyses
SELECT
    analysis_id,
    conversation_id,
    source_type                     AS analysis_source,
    NULL::text                      AS service_key,
    NULL::text                      AS service_name,
    agent_owner_id,
    agent_name,
    call_timestamp,
    call_date,
    NULL::text                      AS typology_key,
    NULL::text                      AS typology_name,
    criterion_id,
    criterion_key,
    criterion_name,
    canonical_criterion_key,
    canonical_criterion_name,
    criterion_type,
    raw_value,
    numeric_value,
    boolean_value,
    text_value,
    category_value,
    percentage_value,
    feedback,
    TRUE                            AS is_applicable,
    analysis_timestamp
FROM vw_bm_individual_analysis_criteria_flat;


-- =============================================================================
-- Verification queries:
--
-- SELECT * FROM vw_bm_individual_analysis_criteria_flat LIMIT 50;
-- SELECT criterion_key, criterion_name, criterion_type, COUNT(*)
--   FROM vw_bm_individual_analysis_criteria_flat
--   GROUP BY criterion_key, criterion_name, criterion_type
--   ORDER BY COUNT(*) DESC;
-- SELECT * FROM vw_bm_individual_analysis_summary LIMIT 50;
-- SELECT analysis_source, COUNT(*) FROM vw_bm_all_analysis_criteria_flat GROUP BY analysis_source;
-- SELECT analysis_source, criterion_key, criterion_name, criterion_type, COUNT(*)
--   FROM vw_bm_all_analysis_criteria_flat
--   GROUP BY analysis_source, criterion_key, criterion_name, criterion_type
--   ORDER BY analysis_source, COUNT(*) DESC LIMIT 30;
-- =============================================================================
