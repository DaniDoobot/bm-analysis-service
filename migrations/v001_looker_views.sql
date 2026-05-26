-- =============================================================================
-- Migration: v001_looker_views.sql
-- Description: Looker-ready views for BM Mass Evaluation analytics.
--   A) vw_bm_mass_evaluation_criteria_flat  – one row per criterion per call
--   B) vw_bm_mass_evaluation_calls_summary  – one row per call, key criteria pivoted
--
-- Notes:
--   • evaluacion_global lives in result_json (not in criterion_results table).
--     It is exposed in calls_summary as a calculated numeric column.
--   • Column aliases follow Looker naming conventions (snake_case, readable).
--   • This script is idempotent: uses CREATE OR REPLACE VIEW.
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- A) vw_bm_mass_evaluation_criteria_flat
--    One row per (call × criterion). Looker should use this as the base
--    for criterion-level metrics and filters.
-- ─────────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS vw_bm_mass_evaluation_criteria_flat CASCADE;

CREATE OR REPLACE VIEW vw_bm_mass_evaluation_criteria_flat AS
SELECT
    -- Call / Result identity
    r.mass_analysis_id          AS mass_evaluation_result_id,
    r.call_id                   AS conversation_id,
    r.job_id,
    r.run_id,
    r.hs_object_id,

    -- Service dimensions
    COALESCE(c.service_id,   r.service_id)   AS service_id,
    COALESCE(c.service_key,  r.service_key)  AS service_key,
    COALESCE(c.service_name, r.service_name) AS service_name,

    -- Typology / campaign dimensions
    COALESCE(c.typology_id,   r.typology_id)   AS typology_id,
    COALESCE(c.typology_key,  r.typology_key)  AS typology_key,
    COALESCE(c.typology_name, r.typology_name) AS typology_name,

    -- Agent dimensions
    r.hubspot_owner_id          AS agent_owner_id,
    r.agent_name,

    -- Time dimensions
    r.call_timestamp,
    r.call_timestamp::date      AS call_date,
    r.call_duration_seconds     AS duration_seconds,
    r.direction,

    -- Criterion identity (stable keys for Looker)
    c.criterion_id,
    c.criterion_key,
    c.criterion_name,
    
    -- Canonical fields for Looker grouping
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

    -- Applicability flag
    c.is_applicable,
    c.not_applicable,

    -- Values (typed columns – Looker should use these for aggregation/filtering)
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
    c.feedback,                 -- qualitative feedback / justification

    -- Prompt traceability
    r.prompt_id,
    r.prompt_name,
    r.prompt_version_id,
    r.prompt_version_name,
    r.prompt_version_label,

    -- Timestamps
    r.created_at                AS analysis_timestamp

FROM bm_mass_evaluation_results r
JOIN bm_mass_evaluation_criterion_results c
    ON r.mass_analysis_id = c.mass_analysis_id
WHERE r.status = 'completed';


-- ─────────────────────────────────────────────────────────────────────────────
-- B) vw_bm_mass_evaluation_calls_summary
--    One row per call. Key criteria pivoted as columns.
--    evaluacion_global is extracted from result_json (not in criterion table).
-- ─────────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS vw_bm_mass_evaluation_calls_summary CASCADE;

CREATE OR REPLACE VIEW vw_bm_mass_evaluation_calls_summary AS
SELECT
    -- Call / Result identity
    r.mass_analysis_id          AS mass_evaluation_result_id,
    r.call_id                   AS conversation_id,
    r.job_id,
    r.run_id,
    r.hs_object_id,

    -- Service dimensions
    r.service_id,
    r.service_key,
    r.service_name,

    -- Typology / campaign dimensions
    r.typology_id,
    r.typology_key,
    r.typology_name,

    -- Agent dimensions
    r.hubspot_owner_id          AS agent_owner_id,
    r.agent_name,

    -- Time dimensions
    r.call_timestamp,
    r.call_timestamp::date      AS call_date,
    r.call_duration_seconds     AS duration_seconds,
    r.direction,

    -- Prompt traceability
    r.prompt_id,
    r.prompt_name,
    r.prompt_version_id,
    r.prompt_version_name,
    r.prompt_version_label,

    -- ── Evaluación global (from result_json JSONB) ──────────────────────────
    -- NOTE: evaluacion_global is scored by the AI and stored directly in
    -- result_json. It is NOT present in bm_mass_evaluation_criterion_results.
    (r.result_json->>'evaluacion_global')::numeric  AS evaluacion_global,

    -- ── Classification criteria (category / text) ──────────────────────────
    MAX(CASE WHEN c.criterion_key = 'tipo_llamada'  AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS tipo_llamada,
    MAX(CASE WHEN c.criterion_key = 'patologia'     AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS patologia,
    MAX(CASE WHEN c.criterion_key = 'objecion_1'    AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS objecion_1,
    MAX(CASE WHEN c.criterion_key = 'objecion_2'    AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS objecion_2,
    MAX(CASE WHEN c.criterion_key = 'objecion_3'    AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS objecion_3,
    MAX(CASE WHEN c.criterion_key = 'motivo_no_cita' AND c.is_applicable THEN COALESCE(c.category_value, c.text_value) END) AS motivo_no_cita,
    MAX(CASE WHEN c.criterion_key = 'objeciones'    AND c.is_applicable THEN c.text_value END) AS objeciones,

    -- ── Boolean criteria ────────────────────────────────────────────────────
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

    -- ── Numeric / score_1_10 criteria ───────────────────────────────────────
    MAX(CASE WHEN c.criterion_key = 'claridad'                AND c.is_applicable THEN c.numeric_value END) AS claridad,
    MAX(CASE WHEN c.criterion_key = 'procedimiento'           AND c.is_applicable THEN c.numeric_value END) AS procedimiento,
    MAX(CASE WHEN c.criterion_key = 'n3_preguntas'            AND c.is_applicable THEN c.numeric_value END) AS n3_preguntas,
    MAX(CASE WHEN c.criterion_key = 'gestion_objeciones'      AND c.is_applicable THEN c.numeric_value END) AS gestion_objeciones,
    MAX(CASE WHEN c.criterion_key = 'propension'              AND c.is_applicable THEN c.numeric_value END) AS propension,
    MAX(CASE WHEN c.criterion_key = 'saludo_inicio'           AND c.is_applicable THEN c.numeric_value END) AS saludo_inicio,
    MAX(CASE WHEN c.criterion_key = 'uso_preguntas'           AND c.is_applicable THEN c.numeric_value END) AS uso_preguntas,
    MAX(CASE WHEN c.criterion_key = 'uso_nombre_paciente'     AND c.is_applicable THEN c.numeric_value END) AS uso_nombre_paciente,
    MAX(CASE WHEN c.criterion_key = 'trato_usted'             AND c.is_applicable THEN c.numeric_value END) AS trato_usted,
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

    -- ── Percentage criteria ─────────────────────────────────────────────────
    MAX(CASE WHEN c.criterion_key = 'hablando_agente'         AND c.is_applicable THEN COALESCE(c.percentage_value, c.numeric_value) END) AS hablando_agente_pct,
    MAX(CASE WHEN c.criterion_key = 'hablando_paciente'       AND c.is_applicable THEN COALESCE(c.percentage_value, c.numeric_value) END) AS hablando_paciente_pct,

    -- ── Other numeric fields ────────────────────────────────────────────────
    MAX(CASE WHEN c.criterion_key = 'palabras_minuto_agente'  AND c.is_applicable THEN c.numeric_value END) AS palabras_minuto_agente,
    MAX(CASE WHEN c.criterion_key = 'meses_patologia'         AND c.is_applicable THEN c.numeric_value END) AS meses_patologia,

    -- Timestamps
    r.created_at                AS analysis_timestamp

FROM bm_mass_evaluation_results r
JOIN bm_mass_evaluation_criterion_results c
    ON r.mass_analysis_id = c.mass_analysis_id
WHERE r.status = 'completed'
GROUP BY
    r.mass_analysis_id,
    r.call_id,
    r.job_id,
    r.run_id,
    r.hs_object_id,
    r.service_id,
    r.service_key,
    r.service_name,
    r.typology_id,
    r.typology_key,
    r.typology_name,
    r.hubspot_owner_id,
    r.agent_name,
    r.call_timestamp,
    r.call_duration_seconds,
    r.direction,
    r.prompt_id,
    r.prompt_name,
    r.prompt_version_id,
    r.prompt_version_name,
    r.prompt_version_label,
    r.result_json,
    r.created_at;


-- =============================================================================
-- Verification queries (run manually after applying this migration):
--
-- SELECT * FROM vw_bm_mass_evaluation_criteria_flat LIMIT 50;
--
-- SELECT criterion_key, criterion_name, criterion_type, COUNT(*)
-- FROM vw_bm_mass_evaluation_criteria_flat
-- GROUP BY criterion_key, criterion_name, criterion_type
-- ORDER BY COUNT(*) DESC;
--
-- SELECT * FROM vw_bm_mass_evaluation_calls_summary LIMIT 50;
-- =============================================================================
