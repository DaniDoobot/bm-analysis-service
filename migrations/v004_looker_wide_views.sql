-- SQL Migration: Wide reporting views for Looker
DROP VIEW IF EXISTS vw_bm_mass_evaluation_report_wide CASCADE;
DROP VIEW IF EXISTS vw_bm_individual_analysis_report_wide CASCADE;

-- 1) vw_bm_mass_evaluation_report_wide
CREATE OR REPLACE VIEW vw_bm_mass_evaluation_report_wide AS
WITH display_cte AS (
    SELECT
        mass_evaluation_result_id,
        criterion_id,
        criterion_key,
        criterion_name,
        canonical_criterion_key,
        canonical_criterion_name,
        criterion_type,
        feedback,
        is_applicable,
        numeric_value,
        percentage_value,
        boolean_value,
        category_value,
        text_value,
        raw_value,
        CASE
            WHEN criterion_type IN ('score_1_10', 'number') THEN numeric_value::text
            WHEN criterion_type = 'percentage' THEN percentage_value::text
            WHEN criterion_type = 'boolean' THEN boolean_value::text
            WHEN criterion_type = 'category' THEN category_value::text
            WHEN criterion_type = 'text' THEN text_value::text
            ELSE COALESCE(raw_value#>>'{}', raw_value::text)
        END AS display_value,
        CASE canonical_criterion_key
                    WHEN 'evaluacion_global' THEN 1
                    WHEN 'claridad' THEN 2
                    WHEN 'procedimiento' THEN 3
                    WHEN 'cierre_cita' THEN 4
                    WHEN 'tipo_llamada' THEN 5
                    WHEN 'patologia' THEN 6
                    WHEN 'objecion_1' THEN 7
                    WHEN 'objecion_2' THEN 8
                    WHEN 'objecion_3' THEN 9
                    WHEN 'motivo_no_cita' THEN 10
                    WHEN 'objeciones' THEN 11
                    WHEN 'n3_preguntas' THEN 12
                    WHEN 'gestion_objeciones' THEN 13
                    WHEN 'propension' THEN 14
                    WHEN 'saludo_inicio' THEN 15
                    WHEN 'uso_preguntas' THEN 16
                    WHEN 'uso_nombre_paciente' THEN 17
                    WHEN 'trato_usted' THEN 18
                    WHEN 'empatia' THEN 19
                    WHEN 'simpatia' THEN 20
                    WHEN 'sentiment' THEN 21
                    WHEN 'explicaciones_medicas' THEN 22
                    WHEN 'claridad_explicacion_economica' THEN 23
                    WHEN 'claridad_de_explicacion_de_precio_en_consulta' THEN 24
                    WHEN 'despedida_con_refuerzo' THEN 25
                    WHEN 'siguiente_paso' THEN 26
                    WHEN 'velocidad_hablando_agente' THEN 27
                    WHEN 'interrupciones' THEN 28
                    WHEN 'hablando_agente' THEN 29
                    WHEN 'hablando_paciente' THEN 30
                    WHEN 'palabras_minuto_agente' THEN 31
                    WHEN 'meses_patologia' THEN 32
                    WHEN 'verifica_patologia' THEN 33
                    WHEN 'reformula_patologia' THEN 34
                    WHEN 'precio_consulta' THEN 35
                    WHEN 'tratamiento_no_en_precio' THEN 36
                    WHEN 'duracion_consulta' THEN 37
                    WHEN 'direccion_y_referencias' THEN 38
                    WHEN 'puntualidad' THEN 39
                    WHEN 'conocimiento_boston_medical' THEN 40
                    WHEN 'puede_adelantar_cita' THEN 41
                    WHEN 'pregunta_pareja' THEN 42
                    WHEN 'recomienda_pareja' THEN 43
                    WHEN 'pareja_conocedora' THEN 44
                    WHEN 'pareja_asistira' THEN 45
                    WHEN 'medio' THEN 46
                    WHEN 'cuanto_tiempo' THEN 47
                    WHEN 'por_que_ahora' THEN 48
                    ELSE 999
                END AS criterion_order
    FROM vw_bm_mass_evaluation_criteria_flat
),
ordered_cte AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY mass_evaluation_result_id
            ORDER BY criterion_order ASC, canonical_criterion_key ASC
        ) AS item_number
    FROM display_cte
    WHERE canonical_criterion_key IS DISTINCT FROM 'tipo_llamada'
),
pivoted_cte AS (
    SELECT
        mass_evaluation_result_id,
        BOOL_OR(CASE WHEN canonical_criterion_key = 'cierre_cita' AND is_applicable THEN boolean_value END) AS cierre_cita_criterio,
        MAX(CASE WHEN item_number = 1 THEN canonical_criterion_name END) AS nombre_item_1,
        MAX(CASE WHEN item_number = 1 THEN canonical_criterion_key END) AS key_item_1,
        MAX(CASE WHEN item_number = 1 THEN criterion_type END) AS tipo_item_1,
        MAX(CASE WHEN item_number = 1 THEN display_value END) AS valor_item_1,
        MAX(CASE WHEN item_number = 1 THEN feedback END) AS feedback_item_1,
        MAX(CASE WHEN item_number = 2 THEN canonical_criterion_name END) AS nombre_item_2,
        MAX(CASE WHEN item_number = 2 THEN canonical_criterion_key END) AS key_item_2,
        MAX(CASE WHEN item_number = 2 THEN criterion_type END) AS tipo_item_2,
        MAX(CASE WHEN item_number = 2 THEN display_value END) AS valor_item_2,
        MAX(CASE WHEN item_number = 2 THEN feedback END) AS feedback_item_2,
        MAX(CASE WHEN item_number = 3 THEN canonical_criterion_name END) AS nombre_item_3,
        MAX(CASE WHEN item_number = 3 THEN canonical_criterion_key END) AS key_item_3,
        MAX(CASE WHEN item_number = 3 THEN criterion_type END) AS tipo_item_3,
        MAX(CASE WHEN item_number = 3 THEN display_value END) AS valor_item_3,
        MAX(CASE WHEN item_number = 3 THEN feedback END) AS feedback_item_3,
        MAX(CASE WHEN item_number = 4 THEN canonical_criterion_name END) AS nombre_item_4,
        MAX(CASE WHEN item_number = 4 THEN canonical_criterion_key END) AS key_item_4,
        MAX(CASE WHEN item_number = 4 THEN criterion_type END) AS tipo_item_4,
        MAX(CASE WHEN item_number = 4 THEN display_value END) AS valor_item_4,
        MAX(CASE WHEN item_number = 4 THEN feedback END) AS feedback_item_4,
        MAX(CASE WHEN item_number = 5 THEN canonical_criterion_name END) AS nombre_item_5,
        MAX(CASE WHEN item_number = 5 THEN canonical_criterion_key END) AS key_item_5,
        MAX(CASE WHEN item_number = 5 THEN criterion_type END) AS tipo_item_5,
        MAX(CASE WHEN item_number = 5 THEN display_value END) AS valor_item_5,
        MAX(CASE WHEN item_number = 5 THEN feedback END) AS feedback_item_5,
        MAX(CASE WHEN item_number = 6 THEN canonical_criterion_name END) AS nombre_item_6,
        MAX(CASE WHEN item_number = 6 THEN canonical_criterion_key END) AS key_item_6,
        MAX(CASE WHEN item_number = 6 THEN criterion_type END) AS tipo_item_6,
        MAX(CASE WHEN item_number = 6 THEN display_value END) AS valor_item_6,
        MAX(CASE WHEN item_number = 6 THEN feedback END) AS feedback_item_6,
        MAX(CASE WHEN item_number = 7 THEN canonical_criterion_name END) AS nombre_item_7,
        MAX(CASE WHEN item_number = 7 THEN canonical_criterion_key END) AS key_item_7,
        MAX(CASE WHEN item_number = 7 THEN criterion_type END) AS tipo_item_7,
        MAX(CASE WHEN item_number = 7 THEN display_value END) AS valor_item_7,
        MAX(CASE WHEN item_number = 7 THEN feedback END) AS feedback_item_7,
        MAX(CASE WHEN item_number = 8 THEN canonical_criterion_name END) AS nombre_item_8,
        MAX(CASE WHEN item_number = 8 THEN canonical_criterion_key END) AS key_item_8,
        MAX(CASE WHEN item_number = 8 THEN criterion_type END) AS tipo_item_8,
        MAX(CASE WHEN item_number = 8 THEN display_value END) AS valor_item_8,
        MAX(CASE WHEN item_number = 8 THEN feedback END) AS feedback_item_8,
        MAX(CASE WHEN item_number = 9 THEN canonical_criterion_name END) AS nombre_item_9,
        MAX(CASE WHEN item_number = 9 THEN canonical_criterion_key END) AS key_item_9,
        MAX(CASE WHEN item_number = 9 THEN criterion_type END) AS tipo_item_9,
        MAX(CASE WHEN item_number = 9 THEN display_value END) AS valor_item_9,
        MAX(CASE WHEN item_number = 9 THEN feedback END) AS feedback_item_9,
        MAX(CASE WHEN item_number = 10 THEN canonical_criterion_name END) AS nombre_item_10,
        MAX(CASE WHEN item_number = 10 THEN canonical_criterion_key END) AS key_item_10,
        MAX(CASE WHEN item_number = 10 THEN criterion_type END) AS tipo_item_10,
        MAX(CASE WHEN item_number = 10 THEN display_value END) AS valor_item_10,
        MAX(CASE WHEN item_number = 10 THEN feedback END) AS feedback_item_10,
        MAX(CASE WHEN item_number = 11 THEN canonical_criterion_name END) AS nombre_item_11,
        MAX(CASE WHEN item_number = 11 THEN canonical_criterion_key END) AS key_item_11,
        MAX(CASE WHEN item_number = 11 THEN criterion_type END) AS tipo_item_11,
        MAX(CASE WHEN item_number = 11 THEN display_value END) AS valor_item_11,
        MAX(CASE WHEN item_number = 11 THEN feedback END) AS feedback_item_11,
        MAX(CASE WHEN item_number = 12 THEN canonical_criterion_name END) AS nombre_item_12,
        MAX(CASE WHEN item_number = 12 THEN canonical_criterion_key END) AS key_item_12,
        MAX(CASE WHEN item_number = 12 THEN criterion_type END) AS tipo_item_12,
        MAX(CASE WHEN item_number = 12 THEN display_value END) AS valor_item_12,
        MAX(CASE WHEN item_number = 12 THEN feedback END) AS feedback_item_12,
        MAX(CASE WHEN item_number = 13 THEN canonical_criterion_name END) AS nombre_item_13,
        MAX(CASE WHEN item_number = 13 THEN canonical_criterion_key END) AS key_item_13,
        MAX(CASE WHEN item_number = 13 THEN criterion_type END) AS tipo_item_13,
        MAX(CASE WHEN item_number = 13 THEN display_value END) AS valor_item_13,
        MAX(CASE WHEN item_number = 13 THEN feedback END) AS feedback_item_13,
        MAX(CASE WHEN item_number = 14 THEN canonical_criterion_name END) AS nombre_item_14,
        MAX(CASE WHEN item_number = 14 THEN canonical_criterion_key END) AS key_item_14,
        MAX(CASE WHEN item_number = 14 THEN criterion_type END) AS tipo_item_14,
        MAX(CASE WHEN item_number = 14 THEN display_value END) AS valor_item_14,
        MAX(CASE WHEN item_number = 14 THEN feedback END) AS feedback_item_14,
        MAX(CASE WHEN item_number = 15 THEN canonical_criterion_name END) AS nombre_item_15,
        MAX(CASE WHEN item_number = 15 THEN canonical_criterion_key END) AS key_item_15,
        MAX(CASE WHEN item_number = 15 THEN criterion_type END) AS tipo_item_15,
        MAX(CASE WHEN item_number = 15 THEN display_value END) AS valor_item_15,
        MAX(CASE WHEN item_number = 15 THEN feedback END) AS feedback_item_15,
        MAX(CASE WHEN item_number = 16 THEN canonical_criterion_name END) AS nombre_item_16,
        MAX(CASE WHEN item_number = 16 THEN canonical_criterion_key END) AS key_item_16,
        MAX(CASE WHEN item_number = 16 THEN criterion_type END) AS tipo_item_16,
        MAX(CASE WHEN item_number = 16 THEN display_value END) AS valor_item_16,
        MAX(CASE WHEN item_number = 16 THEN feedback END) AS feedback_item_16,
        MAX(CASE WHEN item_number = 17 THEN canonical_criterion_name END) AS nombre_item_17,
        MAX(CASE WHEN item_number = 17 THEN canonical_criterion_key END) AS key_item_17,
        MAX(CASE WHEN item_number = 17 THEN criterion_type END) AS tipo_item_17,
        MAX(CASE WHEN item_number = 17 THEN display_value END) AS valor_item_17,
        MAX(CASE WHEN item_number = 17 THEN feedback END) AS feedback_item_17,
        MAX(CASE WHEN item_number = 18 THEN canonical_criterion_name END) AS nombre_item_18,
        MAX(CASE WHEN item_number = 18 THEN canonical_criterion_key END) AS key_item_18,
        MAX(CASE WHEN item_number = 18 THEN criterion_type END) AS tipo_item_18,
        MAX(CASE WHEN item_number = 18 THEN display_value END) AS valor_item_18,
        MAX(CASE WHEN item_number = 18 THEN feedback END) AS feedback_item_18,
        MAX(CASE WHEN item_number = 19 THEN canonical_criterion_name END) AS nombre_item_19,
        MAX(CASE WHEN item_number = 19 THEN canonical_criterion_key END) AS key_item_19,
        MAX(CASE WHEN item_number = 19 THEN criterion_type END) AS tipo_item_19,
        MAX(CASE WHEN item_number = 19 THEN display_value END) AS valor_item_19,
        MAX(CASE WHEN item_number = 19 THEN feedback END) AS feedback_item_19,
        MAX(CASE WHEN item_number = 20 THEN canonical_criterion_name END) AS nombre_item_20,
        MAX(CASE WHEN item_number = 20 THEN canonical_criterion_key END) AS key_item_20,
        MAX(CASE WHEN item_number = 20 THEN criterion_type END) AS tipo_item_20,
        MAX(CASE WHEN item_number = 20 THEN display_value END) AS valor_item_20,
        MAX(CASE WHEN item_number = 20 THEN feedback END) AS feedback_item_20,
        MAX(CASE WHEN item_number = 21 THEN canonical_criterion_name END) AS nombre_item_21,
        MAX(CASE WHEN item_number = 21 THEN canonical_criterion_key END) AS key_item_21,
        MAX(CASE WHEN item_number = 21 THEN criterion_type END) AS tipo_item_21,
        MAX(CASE WHEN item_number = 21 THEN display_value END) AS valor_item_21,
        MAX(CASE WHEN item_number = 21 THEN feedback END) AS feedback_item_21,
        MAX(CASE WHEN item_number = 22 THEN canonical_criterion_name END) AS nombre_item_22,
        MAX(CASE WHEN item_number = 22 THEN canonical_criterion_key END) AS key_item_22,
        MAX(CASE WHEN item_number = 22 THEN criterion_type END) AS tipo_item_22,
        MAX(CASE WHEN item_number = 22 THEN display_value END) AS valor_item_22,
        MAX(CASE WHEN item_number = 22 THEN feedback END) AS feedback_item_22,
        MAX(CASE WHEN item_number = 23 THEN canonical_criterion_name END) AS nombre_item_23,
        MAX(CASE WHEN item_number = 23 THEN canonical_criterion_key END) AS key_item_23,
        MAX(CASE WHEN item_number = 23 THEN criterion_type END) AS tipo_item_23,
        MAX(CASE WHEN item_number = 23 THEN display_value END) AS valor_item_23,
        MAX(CASE WHEN item_number = 23 THEN feedback END) AS feedback_item_23,
        MAX(CASE WHEN item_number = 24 THEN canonical_criterion_name END) AS nombre_item_24,
        MAX(CASE WHEN item_number = 24 THEN canonical_criterion_key END) AS key_item_24,
        MAX(CASE WHEN item_number = 24 THEN criterion_type END) AS tipo_item_24,
        MAX(CASE WHEN item_number = 24 THEN display_value END) AS valor_item_24,
        MAX(CASE WHEN item_number = 24 THEN feedback END) AS feedback_item_24,
        MAX(CASE WHEN item_number = 25 THEN canonical_criterion_name END) AS nombre_item_25,
        MAX(CASE WHEN item_number = 25 THEN canonical_criterion_key END) AS key_item_25,
        MAX(CASE WHEN item_number = 25 THEN criterion_type END) AS tipo_item_25,
        MAX(CASE WHEN item_number = 25 THEN display_value END) AS valor_item_25,
        MAX(CASE WHEN item_number = 25 THEN feedback END) AS feedback_item_25,
        MAX(CASE WHEN item_number = 26 THEN canonical_criterion_name END) AS nombre_item_26,
        MAX(CASE WHEN item_number = 26 THEN canonical_criterion_key END) AS key_item_26,
        MAX(CASE WHEN item_number = 26 THEN criterion_type END) AS tipo_item_26,
        MAX(CASE WHEN item_number = 26 THEN display_value END) AS valor_item_26,
        MAX(CASE WHEN item_number = 26 THEN feedback END) AS feedback_item_26,
        MAX(CASE WHEN item_number = 27 THEN canonical_criterion_name END) AS nombre_item_27,
        MAX(CASE WHEN item_number = 27 THEN canonical_criterion_key END) AS key_item_27,
        MAX(CASE WHEN item_number = 27 THEN criterion_type END) AS tipo_item_27,
        MAX(CASE WHEN item_number = 27 THEN display_value END) AS valor_item_27,
        MAX(CASE WHEN item_number = 27 THEN feedback END) AS feedback_item_27,
        MAX(CASE WHEN item_number = 28 THEN canonical_criterion_name END) AS nombre_item_28,
        MAX(CASE WHEN item_number = 28 THEN canonical_criterion_key END) AS key_item_28,
        MAX(CASE WHEN item_number = 28 THEN criterion_type END) AS tipo_item_28,
        MAX(CASE WHEN item_number = 28 THEN display_value END) AS valor_item_28,
        MAX(CASE WHEN item_number = 28 THEN feedback END) AS feedback_item_28,
        MAX(CASE WHEN item_number = 29 THEN canonical_criterion_name END) AS nombre_item_29,
        MAX(CASE WHEN item_number = 29 THEN canonical_criterion_key END) AS key_item_29,
        MAX(CASE WHEN item_number = 29 THEN criterion_type END) AS tipo_item_29,
        MAX(CASE WHEN item_number = 29 THEN display_value END) AS valor_item_29,
        MAX(CASE WHEN item_number = 29 THEN feedback END) AS feedback_item_29,
        MAX(CASE WHEN item_number = 30 THEN canonical_criterion_name END) AS nombre_item_30,
        MAX(CASE WHEN item_number = 30 THEN canonical_criterion_key END) AS key_item_30,
        MAX(CASE WHEN item_number = 30 THEN criterion_type END) AS tipo_item_30,
        MAX(CASE WHEN item_number = 30 THEN display_value END) AS valor_item_30,
        MAX(CASE WHEN item_number = 30 THEN feedback END) AS feedback_item_30,
        MAX(CASE WHEN item_number = 31 THEN canonical_criterion_name END) AS nombre_item_31,
        MAX(CASE WHEN item_number = 31 THEN canonical_criterion_key END) AS key_item_31,
        MAX(CASE WHEN item_number = 31 THEN criterion_type END) AS tipo_item_31,
        MAX(CASE WHEN item_number = 31 THEN display_value END) AS valor_item_31,
        MAX(CASE WHEN item_number = 31 THEN feedback END) AS feedback_item_31,
        MAX(CASE WHEN item_number = 32 THEN canonical_criterion_name END) AS nombre_item_32,
        MAX(CASE WHEN item_number = 32 THEN canonical_criterion_key END) AS key_item_32,
        MAX(CASE WHEN item_number = 32 THEN criterion_type END) AS tipo_item_32,
        MAX(CASE WHEN item_number = 32 THEN display_value END) AS valor_item_32,
        MAX(CASE WHEN item_number = 32 THEN feedback END) AS feedback_item_32,
        MAX(CASE WHEN item_number = 33 THEN canonical_criterion_name END) AS nombre_item_33,
        MAX(CASE WHEN item_number = 33 THEN canonical_criterion_key END) AS key_item_33,
        MAX(CASE WHEN item_number = 33 THEN criterion_type END) AS tipo_item_33,
        MAX(CASE WHEN item_number = 33 THEN display_value END) AS valor_item_33,
        MAX(CASE WHEN item_number = 33 THEN feedback END) AS feedback_item_33,
        MAX(CASE WHEN item_number = 34 THEN canonical_criterion_name END) AS nombre_item_34,
        MAX(CASE WHEN item_number = 34 THEN canonical_criterion_key END) AS key_item_34,
        MAX(CASE WHEN item_number = 34 THEN criterion_type END) AS tipo_item_34,
        MAX(CASE WHEN item_number = 34 THEN display_value END) AS valor_item_34,
        MAX(CASE WHEN item_number = 34 THEN feedback END) AS feedback_item_34,
        MAX(CASE WHEN item_number = 35 THEN canonical_criterion_name END) AS nombre_item_35,
        MAX(CASE WHEN item_number = 35 THEN canonical_criterion_key END) AS key_item_35,
        MAX(CASE WHEN item_number = 35 THEN criterion_type END) AS tipo_item_35,
        MAX(CASE WHEN item_number = 35 THEN display_value END) AS valor_item_35,
        MAX(CASE WHEN item_number = 35 THEN feedback END) AS feedback_item_35,
        MAX(CASE WHEN item_number = 36 THEN canonical_criterion_name END) AS nombre_item_36,
        MAX(CASE WHEN item_number = 36 THEN canonical_criterion_key END) AS key_item_36,
        MAX(CASE WHEN item_number = 36 THEN criterion_type END) AS tipo_item_36,
        MAX(CASE WHEN item_number = 36 THEN display_value END) AS valor_item_36,
        MAX(CASE WHEN item_number = 36 THEN feedback END) AS feedback_item_36,
        MAX(CASE WHEN item_number = 37 THEN canonical_criterion_name END) AS nombre_item_37,
        MAX(CASE WHEN item_number = 37 THEN canonical_criterion_key END) AS key_item_37,
        MAX(CASE WHEN item_number = 37 THEN criterion_type END) AS tipo_item_37,
        MAX(CASE WHEN item_number = 37 THEN display_value END) AS valor_item_37,
        MAX(CASE WHEN item_number = 37 THEN feedback END) AS feedback_item_37,
        MAX(CASE WHEN item_number = 38 THEN canonical_criterion_name END) AS nombre_item_38,
        MAX(CASE WHEN item_number = 38 THEN canonical_criterion_key END) AS key_item_38,
        MAX(CASE WHEN item_number = 38 THEN criterion_type END) AS tipo_item_38,
        MAX(CASE WHEN item_number = 38 THEN display_value END) AS valor_item_38,
        MAX(CASE WHEN item_number = 38 THEN feedback END) AS feedback_item_38,
        MAX(CASE WHEN item_number = 39 THEN canonical_criterion_name END) AS nombre_item_39,
        MAX(CASE WHEN item_number = 39 THEN canonical_criterion_key END) AS key_item_39,
        MAX(CASE WHEN item_number = 39 THEN criterion_type END) AS tipo_item_39,
        MAX(CASE WHEN item_number = 39 THEN display_value END) AS valor_item_39,
        MAX(CASE WHEN item_number = 39 THEN feedback END) AS feedback_item_39,
        MAX(CASE WHEN item_number = 40 THEN canonical_criterion_name END) AS nombre_item_40,
        MAX(CASE WHEN item_number = 40 THEN canonical_criterion_key END) AS key_item_40,
        MAX(CASE WHEN item_number = 40 THEN criterion_type END) AS tipo_item_40,
        MAX(CASE WHEN item_number = 40 THEN display_value END) AS valor_item_40,
        MAX(CASE WHEN item_number = 40 THEN feedback END) AS feedback_item_40,
        MAX(CASE WHEN item_number = 41 THEN canonical_criterion_name END) AS nombre_item_41,
        MAX(CASE WHEN item_number = 41 THEN canonical_criterion_key END) AS key_item_41,
        MAX(CASE WHEN item_number = 41 THEN criterion_type END) AS tipo_item_41,
        MAX(CASE WHEN item_number = 41 THEN display_value END) AS valor_item_41,
        MAX(CASE WHEN item_number = 41 THEN feedback END) AS feedback_item_41,
        MAX(CASE WHEN item_number = 42 THEN canonical_criterion_name END) AS nombre_item_42,
        MAX(CASE WHEN item_number = 42 THEN canonical_criterion_key END) AS key_item_42,
        MAX(CASE WHEN item_number = 42 THEN criterion_type END) AS tipo_item_42,
        MAX(CASE WHEN item_number = 42 THEN display_value END) AS valor_item_42,
        MAX(CASE WHEN item_number = 42 THEN feedback END) AS feedback_item_42,
        MAX(CASE WHEN item_number = 43 THEN canonical_criterion_name END) AS nombre_item_43,
        MAX(CASE WHEN item_number = 43 THEN canonical_criterion_key END) AS key_item_43,
        MAX(CASE WHEN item_number = 43 THEN criterion_type END) AS tipo_item_43,
        MAX(CASE WHEN item_number = 43 THEN display_value END) AS valor_item_43,
        MAX(CASE WHEN item_number = 43 THEN feedback END) AS feedback_item_43,
        MAX(CASE WHEN item_number = 44 THEN canonical_criterion_name END) AS nombre_item_44,
        MAX(CASE WHEN item_number = 44 THEN canonical_criterion_key END) AS key_item_44,
        MAX(CASE WHEN item_number = 44 THEN criterion_type END) AS tipo_item_44,
        MAX(CASE WHEN item_number = 44 THEN display_value END) AS valor_item_44,
        MAX(CASE WHEN item_number = 44 THEN feedback END) AS feedback_item_44,
        MAX(CASE WHEN item_number = 45 THEN canonical_criterion_name END) AS nombre_item_45,
        MAX(CASE WHEN item_number = 45 THEN canonical_criterion_key END) AS key_item_45,
        MAX(CASE WHEN item_number = 45 THEN criterion_type END) AS tipo_item_45,
        MAX(CASE WHEN item_number = 45 THEN display_value END) AS valor_item_45,
        MAX(CASE WHEN item_number = 45 THEN feedback END) AS feedback_item_45,
        MAX(CASE WHEN item_number = 46 THEN canonical_criterion_name END) AS nombre_item_46,
        MAX(CASE WHEN item_number = 46 THEN canonical_criterion_key END) AS key_item_46,
        MAX(CASE WHEN item_number = 46 THEN criterion_type END) AS tipo_item_46,
        MAX(CASE WHEN item_number = 46 THEN display_value END) AS valor_item_46,
        MAX(CASE WHEN item_number = 46 THEN feedback END) AS feedback_item_46,
        MAX(CASE WHEN item_number = 47 THEN canonical_criterion_name END) AS nombre_item_47,
        MAX(CASE WHEN item_number = 47 THEN canonical_criterion_key END) AS key_item_47,
        MAX(CASE WHEN item_number = 47 THEN criterion_type END) AS tipo_item_47,
        MAX(CASE WHEN item_number = 47 THEN display_value END) AS valor_item_47,
        MAX(CASE WHEN item_number = 47 THEN feedback END) AS feedback_item_47,
        MAX(CASE WHEN item_number = 48 THEN canonical_criterion_name END) AS nombre_item_48,
        MAX(CASE WHEN item_number = 48 THEN canonical_criterion_key END) AS key_item_48,
        MAX(CASE WHEN item_number = 48 THEN criterion_type END) AS tipo_item_48,
        MAX(CASE WHEN item_number = 48 THEN display_value END) AS valor_item_48,
        MAX(CASE WHEN item_number = 48 THEN feedback END) AS feedback_item_48,
        MAX(CASE WHEN item_number = 49 THEN canonical_criterion_name END) AS nombre_item_49,
        MAX(CASE WHEN item_number = 49 THEN canonical_criterion_key END) AS key_item_49,
        MAX(CASE WHEN item_number = 49 THEN criterion_type END) AS tipo_item_49,
        MAX(CASE WHEN item_number = 49 THEN display_value END) AS valor_item_49,
        MAX(CASE WHEN item_number = 49 THEN feedback END) AS feedback_item_49,
        MAX(CASE WHEN item_number = 50 THEN canonical_criterion_name END) AS nombre_item_50,
        MAX(CASE WHEN item_number = 50 THEN canonical_criterion_key END) AS key_item_50,
        MAX(CASE WHEN item_number = 50 THEN criterion_type END) AS tipo_item_50,
        MAX(CASE WHEN item_number = 50 THEN display_value END) AS valor_item_50,
        MAX(CASE WHEN item_number = 50 THEN feedback END) AS feedback_item_50,
        MAX(CASE WHEN item_number = 51 THEN canonical_criterion_name END) AS nombre_item_51,
        MAX(CASE WHEN item_number = 51 THEN canonical_criterion_key END) AS key_item_51,
        MAX(CASE WHEN item_number = 51 THEN criterion_type END) AS tipo_item_51,
        MAX(CASE WHEN item_number = 51 THEN display_value END) AS valor_item_51,
        MAX(CASE WHEN item_number = 51 THEN feedback END) AS feedback_item_51,
        MAX(CASE WHEN item_number = 52 THEN canonical_criterion_name END) AS nombre_item_52,
        MAX(CASE WHEN item_number = 52 THEN canonical_criterion_key END) AS key_item_52,
        MAX(CASE WHEN item_number = 52 THEN criterion_type END) AS tipo_item_52,
        MAX(CASE WHEN item_number = 52 THEN display_value END) AS valor_item_52,
        MAX(CASE WHEN item_number = 52 THEN feedback END) AS feedback_item_52,
        MAX(CASE WHEN item_number = 53 THEN canonical_criterion_name END) AS nombre_item_53,
        MAX(CASE WHEN item_number = 53 THEN canonical_criterion_key END) AS key_item_53,
        MAX(CASE WHEN item_number = 53 THEN criterion_type END) AS tipo_item_53,
        MAX(CASE WHEN item_number = 53 THEN display_value END) AS valor_item_53,
        MAX(CASE WHEN item_number = 53 THEN feedback END) AS feedback_item_53,
        MAX(CASE WHEN item_number = 54 THEN canonical_criterion_name END) AS nombre_item_54,
        MAX(CASE WHEN item_number = 54 THEN canonical_criterion_key END) AS key_item_54,
        MAX(CASE WHEN item_number = 54 THEN criterion_type END) AS tipo_item_54,
        MAX(CASE WHEN item_number = 54 THEN display_value END) AS valor_item_54,
        MAX(CASE WHEN item_number = 54 THEN feedback END) AS feedback_item_54,
        MAX(CASE WHEN item_number = 55 THEN canonical_criterion_name END) AS nombre_item_55,
        MAX(CASE WHEN item_number = 55 THEN canonical_criterion_key END) AS key_item_55,
        MAX(CASE WHEN item_number = 55 THEN criterion_type END) AS tipo_item_55,
        MAX(CASE WHEN item_number = 55 THEN display_value END) AS valor_item_55,
        MAX(CASE WHEN item_number = 55 THEN feedback END) AS feedback_item_55,
        MAX(CASE WHEN item_number = 56 THEN canonical_criterion_name END) AS nombre_item_56,
        MAX(CASE WHEN item_number = 56 THEN canonical_criterion_key END) AS key_item_56,
        MAX(CASE WHEN item_number = 56 THEN criterion_type END) AS tipo_item_56,
        MAX(CASE WHEN item_number = 56 THEN display_value END) AS valor_item_56,
        MAX(CASE WHEN item_number = 56 THEN feedback END) AS feedback_item_56,
        MAX(CASE WHEN item_number = 57 THEN canonical_criterion_name END) AS nombre_item_57,
        MAX(CASE WHEN item_number = 57 THEN canonical_criterion_key END) AS key_item_57,
        MAX(CASE WHEN item_number = 57 THEN criterion_type END) AS tipo_item_57,
        MAX(CASE WHEN item_number = 57 THEN display_value END) AS valor_item_57,
        MAX(CASE WHEN item_number = 57 THEN feedback END) AS feedback_item_57,
        MAX(CASE WHEN item_number = 58 THEN canonical_criterion_name END) AS nombre_item_58,
        MAX(CASE WHEN item_number = 58 THEN canonical_criterion_key END) AS key_item_58,
        MAX(CASE WHEN item_number = 58 THEN criterion_type END) AS tipo_item_58,
        MAX(CASE WHEN item_number = 58 THEN display_value END) AS valor_item_58,
        MAX(CASE WHEN item_number = 58 THEN feedback END) AS feedback_item_58,
        MAX(CASE WHEN item_number = 59 THEN canonical_criterion_name END) AS nombre_item_59,
        MAX(CASE WHEN item_number = 59 THEN canonical_criterion_key END) AS key_item_59,
        MAX(CASE WHEN item_number = 59 THEN criterion_type END) AS tipo_item_59,
        MAX(CASE WHEN item_number = 59 THEN display_value END) AS valor_item_59,
        MAX(CASE WHEN item_number = 59 THEN feedback END) AS feedback_item_59,
        MAX(CASE WHEN item_number = 60 THEN canonical_criterion_name END) AS nombre_item_60,
        MAX(CASE WHEN item_number = 60 THEN canonical_criterion_key END) AS key_item_60,
        MAX(CASE WHEN item_number = 60 THEN criterion_type END) AS tipo_item_60,
        MAX(CASE WHEN item_number = 60 THEN display_value END) AS valor_item_60,
        MAX(CASE WHEN item_number = 60 THEN feedback END) AS feedback_item_60
    FROM ordered_cte
    GROUP BY mass_evaluation_result_id
)
SELECT
    m.mass_analysis_id          AS mass_evaluation_result_id,
    m.call_id                   AS conversation_id,
    m.hs_object_id,
    CASE 
        WHEN m.hs_object_id IS NOT NULL OR m.call_id IS NOT NULL THEN 'https://app.hubspot.com/calls/140451581/review/' || COALESCE(m.hs_object_id, m.call_id)
        ELSE NULL 
    END                         AS link_hubspot,
    m.job_id,
    m.run_id,
    m.service_id,
    m.service_key,
    m.service_name,
    m.typology_id,
    m.typology_key,
    m.typology_name,
    COALESCE(
        (SELECT COALESCE(category_value, text_value) 
         FROM bm_mass_evaluation_criterion_results 
         WHERE mass_analysis_id = m.mass_analysis_id 
           AND criterion_key = 'tipo_llamada' 
           AND is_applicable = true 
         LIMIT 1),
        m.typology_name
    )                           AS tipo_llamada,
    m.hubspot_owner_id          AS agent_owner_id,
    m.agent_name,
    m.call_timestamp,
    m.call_timestamp::date      AS call_date,
    m.call_duration_seconds     AS duration_seconds,
    m.direction,
    m.prompt_id,
    m.prompt_name,
    m.prompt_version_id,
    m.prompt_version_name,
    m.prompt_version_label,
    m.created_at                AS analysis_timestamp,
    COALESCE(m.result_json->>'resumen', m.result_json->>'resumen_llamada', m.result_json->>'summary') AS resumen,
    (m.result_json->>'evaluacion_global')::numeric AS evaluacion_global,
    p.cierre_cita_criterio      AS cierre_cita,
    p.nombre_item_1, p.key_item_1, p.tipo_item_1, p.valor_item_1, p.feedback_item_1,
    p.nombre_item_2, p.key_item_2, p.tipo_item_2, p.valor_item_2, p.feedback_item_2,
    p.nombre_item_3, p.key_item_3, p.tipo_item_3, p.valor_item_3, p.feedback_item_3,
    p.nombre_item_4, p.key_item_4, p.tipo_item_4, p.valor_item_4, p.feedback_item_4,
    p.nombre_item_5, p.key_item_5, p.tipo_item_5, p.valor_item_5, p.feedback_item_5,
    p.nombre_item_6, p.key_item_6, p.tipo_item_6, p.valor_item_6, p.feedback_item_6,
    p.nombre_item_7, p.key_item_7, p.tipo_item_7, p.valor_item_7, p.feedback_item_7,
    p.nombre_item_8, p.key_item_8, p.tipo_item_8, p.valor_item_8, p.feedback_item_8,
    p.nombre_item_9, p.key_item_9, p.tipo_item_9, p.valor_item_9, p.feedback_item_9,
    p.nombre_item_10, p.key_item_10, p.tipo_item_10, p.valor_item_10, p.feedback_item_10,
    p.nombre_item_11, p.key_item_11, p.tipo_item_11, p.valor_item_11, p.feedback_item_11,
    p.nombre_item_12, p.key_item_12, p.tipo_item_12, p.valor_item_12, p.feedback_item_12,
    p.nombre_item_13, p.key_item_13, p.tipo_item_13, p.valor_item_13, p.feedback_item_13,
    p.nombre_item_14, p.key_item_14, p.tipo_item_14, p.valor_item_14, p.feedback_item_14,
    p.nombre_item_15, p.key_item_15, p.tipo_item_15, p.valor_item_15, p.feedback_item_15,
    p.nombre_item_16, p.key_item_16, p.tipo_item_16, p.valor_item_16, p.feedback_item_16,
    p.nombre_item_17, p.key_item_17, p.tipo_item_17, p.valor_item_17, p.feedback_item_17,
    p.nombre_item_18, p.key_item_18, p.tipo_item_18, p.valor_item_18, p.feedback_item_18,
    p.nombre_item_19, p.key_item_19, p.tipo_item_19, p.valor_item_19, p.feedback_item_19,
    p.nombre_item_20, p.key_item_20, p.tipo_item_20, p.valor_item_20, p.feedback_item_20,
    p.nombre_item_21, p.key_item_21, p.tipo_item_21, p.valor_item_21, p.feedback_item_21,
    p.nombre_item_22, p.key_item_22, p.tipo_item_22, p.valor_item_22, p.feedback_item_22,
    p.nombre_item_23, p.key_item_23, p.tipo_item_23, p.valor_item_23, p.feedback_item_23,
    p.nombre_item_24, p.key_item_24, p.tipo_item_24, p.valor_item_24, p.feedback_item_24,
    p.nombre_item_25, p.key_item_25, p.tipo_item_25, p.valor_item_25, p.feedback_item_25,
    p.nombre_item_26, p.key_item_26, p.tipo_item_26, p.valor_item_26, p.feedback_item_26,
    p.nombre_item_27, p.key_item_27, p.tipo_item_27, p.valor_item_27, p.feedback_item_27,
    p.nombre_item_28, p.key_item_28, p.tipo_item_28, p.valor_item_28, p.feedback_item_28,
    p.nombre_item_29, p.key_item_29, p.tipo_item_29, p.valor_item_29, p.feedback_item_29,
    p.nombre_item_30, p.key_item_30, p.tipo_item_30, p.valor_item_30, p.feedback_item_30,
    p.nombre_item_31, p.key_item_31, p.tipo_item_31, p.valor_item_31, p.feedback_item_31,
    p.nombre_item_32, p.key_item_32, p.tipo_item_32, p.valor_item_32, p.feedback_item_32,
    p.nombre_item_33, p.key_item_33, p.tipo_item_33, p.valor_item_33, p.feedback_item_33,
    p.nombre_item_34, p.key_item_34, p.tipo_item_34, p.valor_item_34, p.feedback_item_34,
    p.nombre_item_35, p.key_item_35, p.tipo_item_35, p.valor_item_35, p.feedback_item_35,
    p.nombre_item_36, p.key_item_36, p.tipo_item_36, p.valor_item_36, p.feedback_item_36,
    p.nombre_item_37, p.key_item_37, p.tipo_item_37, p.valor_item_37, p.feedback_item_37,
    p.nombre_item_38, p.key_item_38, p.tipo_item_38, p.valor_item_38, p.feedback_item_38,
    p.nombre_item_39, p.key_item_39, p.tipo_item_39, p.valor_item_39, p.feedback_item_39,
    p.nombre_item_40, p.key_item_40, p.tipo_item_40, p.valor_item_40, p.feedback_item_40,
    p.nombre_item_41, p.key_item_41, p.tipo_item_41, p.valor_item_41, p.feedback_item_41,
    p.nombre_item_42, p.key_item_42, p.tipo_item_42, p.valor_item_42, p.feedback_item_42,
    p.nombre_item_43, p.key_item_43, p.tipo_item_43, p.valor_item_43, p.feedback_item_43,
    p.nombre_item_44, p.key_item_44, p.tipo_item_44, p.valor_item_44, p.feedback_item_44,
    p.nombre_item_45, p.key_item_45, p.tipo_item_45, p.valor_item_45, p.feedback_item_45,
    p.nombre_item_46, p.key_item_46, p.tipo_item_46, p.valor_item_46, p.feedback_item_46,
    p.nombre_item_47, p.key_item_47, p.tipo_item_47, p.valor_item_47, p.feedback_item_47,
    p.nombre_item_48, p.key_item_48, p.tipo_item_48, p.valor_item_48, p.feedback_item_48,
    p.nombre_item_49, p.key_item_49, p.tipo_item_49, p.valor_item_49, p.feedback_item_49,
    p.nombre_item_50, p.key_item_50, p.tipo_item_50, p.valor_item_50, p.feedback_item_50,
    p.nombre_item_51, p.key_item_51, p.tipo_item_51, p.valor_item_51, p.feedback_item_51,
    p.nombre_item_52, p.key_item_52, p.tipo_item_52, p.valor_item_52, p.feedback_item_52,
    p.nombre_item_53, p.key_item_53, p.tipo_item_53, p.valor_item_53, p.feedback_item_53,
    p.nombre_item_54, p.key_item_54, p.tipo_item_54, p.valor_item_54, p.feedback_item_54,
    p.nombre_item_55, p.key_item_55, p.tipo_item_55, p.valor_item_55, p.feedback_item_55,
    p.nombre_item_56, p.key_item_56, p.tipo_item_56, p.valor_item_56, p.feedback_item_56,
    p.nombre_item_57, p.key_item_57, p.tipo_item_57, p.valor_item_57, p.feedback_item_57,
    p.nombre_item_58, p.key_item_58, p.tipo_item_58, p.valor_item_58, p.feedback_item_58,
    p.nombre_item_59, p.key_item_59, p.tipo_item_59, p.valor_item_59, p.feedback_item_59,
    p.nombre_item_60, p.key_item_60, p.tipo_item_60, p.valor_item_60, p.feedback_item_60
FROM bm_mass_evaluation_results m
LEFT JOIN pivoted_cte p ON m.mass_analysis_id = p.mass_evaluation_result_id
WHERE m.status = 'completed';

-- 2) vw_bm_individual_analysis_report_wide
CREATE OR REPLACE VIEW vw_bm_individual_analysis_report_wide AS
WITH display_cte AS (
    SELECT
        analysis_id,
        criterion_id,
        criterion_key,
        criterion_name,
        canonical_criterion_key,
        canonical_criterion_name,
        criterion_type,
        feedback,
        numeric_value,
        percentage_value,
        boolean_value,
        category_value,
        text_value,
        raw_value,
        CASE
            WHEN criterion_type IN ('score_1_10', 'number') THEN numeric_value::text
            WHEN criterion_type = 'percentage' THEN percentage_value::text
            WHEN criterion_type = 'boolean' THEN boolean_value::text
            WHEN criterion_type = 'category' THEN category_value::text
            WHEN criterion_type = 'text' THEN text_value::text
            ELSE COALESCE(raw_value#>>'{}', raw_value::text)
        END AS display_value,
        CASE canonical_criterion_key
                    WHEN 'evaluacion_global' THEN 1
                    WHEN 'claridad' THEN 2
                    WHEN 'procedimiento' THEN 3
                    WHEN 'cierre_cita' THEN 4
                    WHEN 'tipo_llamada' THEN 5
                    WHEN 'patologia' THEN 6
                    WHEN 'objecion_1' THEN 7
                    WHEN 'objecion_2' THEN 8
                    WHEN 'objecion_3' THEN 9
                    WHEN 'motivo_no_cita' THEN 10
                    WHEN 'objeciones' THEN 11
                    WHEN 'n3_preguntas' THEN 12
                    WHEN 'gestion_objeciones' THEN 13
                    WHEN 'propension' THEN 14
                    WHEN 'saludo_inicio' THEN 15
                    WHEN 'uso_preguntas' THEN 16
                    WHEN 'uso_nombre_paciente' THEN 17
                    WHEN 'trato_usted' THEN 18
                    WHEN 'empatia' THEN 19
                    WHEN 'simpatia' THEN 20
                    WHEN 'sentiment' THEN 21
                    WHEN 'explicaciones_medicas' THEN 22
                    WHEN 'claridad_explicacion_economica' THEN 23
                    WHEN 'claridad_de_explicacion_de_precio_en_consulta' THEN 24
                    WHEN 'despedida_con_refuerzo' THEN 25
                    WHEN 'siguiente_paso' THEN 26
                    WHEN 'velocidad_hablando_agente' THEN 27
                    WHEN 'interrupciones' THEN 28
                    WHEN 'hablando_agente' THEN 29
                    WHEN 'hablando_paciente' THEN 30
                    WHEN 'palabras_minuto_agente' THEN 31
                    WHEN 'meses_patologia' THEN 32
                    WHEN 'verifica_patologia' THEN 33
                    WHEN 'reformula_patologia' THEN 34
                    WHEN 'precio_consulta' THEN 35
                    WHEN 'tratamiento_no_en_precio' THEN 36
                    WHEN 'duracion_consulta' THEN 37
                    WHEN 'direccion_y_referencias' THEN 38
                    WHEN 'puntualidad' THEN 39
                    WHEN 'conocimiento_boston_medical' THEN 40
                    WHEN 'puede_adelantar_cita' THEN 41
                    WHEN 'pregunta_pareja' THEN 42
                    WHEN 'recomienda_pareja' THEN 43
                    WHEN 'pareja_conocedora' THEN 44
                    WHEN 'pareja_asistira' THEN 45
                    WHEN 'medio' THEN 46
                    WHEN 'cuanto_tiempo' THEN 47
                    WHEN 'por_que_ahora' THEN 48
                    ELSE 999
                END AS criterion_order
    FROM vw_bm_individual_analysis_criteria_flat
),
ordered_cte AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY analysis_id
            ORDER BY criterion_order ASC, canonical_criterion_key ASC
        ) AS item_number
    FROM display_cte
    WHERE canonical_criterion_key IS DISTINCT FROM 'tipo_llamada'
),
pivoted_cte AS (
    SELECT
        analysis_id,
        BOOL_OR(CASE WHEN canonical_criterion_key = 'cierre_cita' THEN boolean_value END) AS cierre_cita_criterio,
        MAX(CASE WHEN item_number = 1 THEN canonical_criterion_name END) AS nombre_item_1,
        MAX(CASE WHEN item_number = 1 THEN canonical_criterion_key END) AS key_item_1,
        MAX(CASE WHEN item_number = 1 THEN criterion_type END) AS tipo_item_1,
        MAX(CASE WHEN item_number = 1 THEN display_value END) AS valor_item_1,
        MAX(CASE WHEN item_number = 1 THEN feedback END) AS feedback_item_1,
        MAX(CASE WHEN item_number = 2 THEN canonical_criterion_name END) AS nombre_item_2,
        MAX(CASE WHEN item_number = 2 THEN canonical_criterion_key END) AS key_item_2,
        MAX(CASE WHEN item_number = 2 THEN criterion_type END) AS tipo_item_2,
        MAX(CASE WHEN item_number = 2 THEN display_value END) AS valor_item_2,
        MAX(CASE WHEN item_number = 2 THEN feedback END) AS feedback_item_2,
        MAX(CASE WHEN item_number = 3 THEN canonical_criterion_name END) AS nombre_item_3,
        MAX(CASE WHEN item_number = 3 THEN canonical_criterion_key END) AS key_item_3,
        MAX(CASE WHEN item_number = 3 THEN criterion_type END) AS tipo_item_3,
        MAX(CASE WHEN item_number = 3 THEN display_value END) AS valor_item_3,
        MAX(CASE WHEN item_number = 3 THEN feedback END) AS feedback_item_3,
        MAX(CASE WHEN item_number = 4 THEN canonical_criterion_name END) AS nombre_item_4,
        MAX(CASE WHEN item_number = 4 THEN canonical_criterion_key END) AS key_item_4,
        MAX(CASE WHEN item_number = 4 THEN criterion_type END) AS tipo_item_4,
        MAX(CASE WHEN item_number = 4 THEN display_value END) AS valor_item_4,
        MAX(CASE WHEN item_number = 4 THEN feedback END) AS feedback_item_4,
        MAX(CASE WHEN item_number = 5 THEN canonical_criterion_name END) AS nombre_item_5,
        MAX(CASE WHEN item_number = 5 THEN canonical_criterion_key END) AS key_item_5,
        MAX(CASE WHEN item_number = 5 THEN criterion_type END) AS tipo_item_5,
        MAX(CASE WHEN item_number = 5 THEN display_value END) AS valor_item_5,
        MAX(CASE WHEN item_number = 5 THEN feedback END) AS feedback_item_5,
        MAX(CASE WHEN item_number = 6 THEN canonical_criterion_name END) AS nombre_item_6,
        MAX(CASE WHEN item_number = 6 THEN canonical_criterion_key END) AS key_item_6,
        MAX(CASE WHEN item_number = 6 THEN criterion_type END) AS tipo_item_6,
        MAX(CASE WHEN item_number = 6 THEN display_value END) AS valor_item_6,
        MAX(CASE WHEN item_number = 6 THEN feedback END) AS feedback_item_6,
        MAX(CASE WHEN item_number = 7 THEN canonical_criterion_name END) AS nombre_item_7,
        MAX(CASE WHEN item_number = 7 THEN canonical_criterion_key END) AS key_item_7,
        MAX(CASE WHEN item_number = 7 THEN criterion_type END) AS tipo_item_7,
        MAX(CASE WHEN item_number = 7 THEN display_value END) AS valor_item_7,
        MAX(CASE WHEN item_number = 7 THEN feedback END) AS feedback_item_7,
        MAX(CASE WHEN item_number = 8 THEN canonical_criterion_name END) AS nombre_item_8,
        MAX(CASE WHEN item_number = 8 THEN canonical_criterion_key END) AS key_item_8,
        MAX(CASE WHEN item_number = 8 THEN criterion_type END) AS tipo_item_8,
        MAX(CASE WHEN item_number = 8 THEN display_value END) AS valor_item_8,
        MAX(CASE WHEN item_number = 8 THEN feedback END) AS feedback_item_8,
        MAX(CASE WHEN item_number = 9 THEN canonical_criterion_name END) AS nombre_item_9,
        MAX(CASE WHEN item_number = 9 THEN canonical_criterion_key END) AS key_item_9,
        MAX(CASE WHEN item_number = 9 THEN criterion_type END) AS tipo_item_9,
        MAX(CASE WHEN item_number = 9 THEN display_value END) AS valor_item_9,
        MAX(CASE WHEN item_number = 9 THEN feedback END) AS feedback_item_9,
        MAX(CASE WHEN item_number = 10 THEN canonical_criterion_name END) AS nombre_item_10,
        MAX(CASE WHEN item_number = 10 THEN canonical_criterion_key END) AS key_item_10,
        MAX(CASE WHEN item_number = 10 THEN criterion_type END) AS tipo_item_10,
        MAX(CASE WHEN item_number = 10 THEN display_value END) AS valor_item_10,
        MAX(CASE WHEN item_number = 10 THEN feedback END) AS feedback_item_10,
        MAX(CASE WHEN item_number = 11 THEN canonical_criterion_name END) AS nombre_item_11,
        MAX(CASE WHEN item_number = 11 THEN canonical_criterion_key END) AS key_item_11,
        MAX(CASE WHEN item_number = 11 THEN criterion_type END) AS tipo_item_11,
        MAX(CASE WHEN item_number = 11 THEN display_value END) AS valor_item_11,
        MAX(CASE WHEN item_number = 11 THEN feedback END) AS feedback_item_11,
        MAX(CASE WHEN item_number = 12 THEN canonical_criterion_name END) AS nombre_item_12,
        MAX(CASE WHEN item_number = 12 THEN canonical_criterion_key END) AS key_item_12,
        MAX(CASE WHEN item_number = 12 THEN criterion_type END) AS tipo_item_12,
        MAX(CASE WHEN item_number = 12 THEN display_value END) AS valor_item_12,
        MAX(CASE WHEN item_number = 12 THEN feedback END) AS feedback_item_12,
        MAX(CASE WHEN item_number = 13 THEN canonical_criterion_name END) AS nombre_item_13,
        MAX(CASE WHEN item_number = 13 THEN canonical_criterion_key END) AS key_item_13,
        MAX(CASE WHEN item_number = 13 THEN criterion_type END) AS tipo_item_13,
        MAX(CASE WHEN item_number = 13 THEN display_value END) AS valor_item_13,
        MAX(CASE WHEN item_number = 13 THEN feedback END) AS feedback_item_13,
        MAX(CASE WHEN item_number = 14 THEN canonical_criterion_name END) AS nombre_item_14,
        MAX(CASE WHEN item_number = 14 THEN canonical_criterion_key END) AS key_item_14,
        MAX(CASE WHEN item_number = 14 THEN criterion_type END) AS tipo_item_14,
        MAX(CASE WHEN item_number = 14 THEN display_value END) AS valor_item_14,
        MAX(CASE WHEN item_number = 14 THEN feedback END) AS feedback_item_14,
        MAX(CASE WHEN item_number = 15 THEN canonical_criterion_name END) AS nombre_item_15,
        MAX(CASE WHEN item_number = 15 THEN canonical_criterion_key END) AS key_item_15,
        MAX(CASE WHEN item_number = 15 THEN criterion_type END) AS tipo_item_15,
        MAX(CASE WHEN item_number = 15 THEN display_value END) AS valor_item_15,
        MAX(CASE WHEN item_number = 15 THEN feedback END) AS feedback_item_15,
        MAX(CASE WHEN item_number = 16 THEN canonical_criterion_name END) AS nombre_item_16,
        MAX(CASE WHEN item_number = 16 THEN canonical_criterion_key END) AS key_item_16,
        MAX(CASE WHEN item_number = 16 THEN criterion_type END) AS tipo_item_16,
        MAX(CASE WHEN item_number = 16 THEN display_value END) AS valor_item_16,
        MAX(CASE WHEN item_number = 16 THEN feedback END) AS feedback_item_16,
        MAX(CASE WHEN item_number = 17 THEN canonical_criterion_name END) AS nombre_item_17,
        MAX(CASE WHEN item_number = 17 THEN canonical_criterion_key END) AS key_item_17,
        MAX(CASE WHEN item_number = 17 THEN criterion_type END) AS tipo_item_17,
        MAX(CASE WHEN item_number = 17 THEN display_value END) AS valor_item_17,
        MAX(CASE WHEN item_number = 17 THEN feedback END) AS feedback_item_17,
        MAX(CASE WHEN item_number = 18 THEN canonical_criterion_name END) AS nombre_item_18,
        MAX(CASE WHEN item_number = 18 THEN canonical_criterion_key END) AS key_item_18,
        MAX(CASE WHEN item_number = 18 THEN criterion_type END) AS tipo_item_18,
        MAX(CASE WHEN item_number = 18 THEN display_value END) AS valor_item_18,
        MAX(CASE WHEN item_number = 18 THEN feedback END) AS feedback_item_18,
        MAX(CASE WHEN item_number = 19 THEN canonical_criterion_name END) AS nombre_item_19,
        MAX(CASE WHEN item_number = 19 THEN canonical_criterion_key END) AS key_item_19,
        MAX(CASE WHEN item_number = 19 THEN criterion_type END) AS tipo_item_19,
        MAX(CASE WHEN item_number = 19 THEN display_value END) AS valor_item_19,
        MAX(CASE WHEN item_number = 19 THEN feedback END) AS feedback_item_19,
        MAX(CASE WHEN item_number = 20 THEN canonical_criterion_name END) AS nombre_item_20,
        MAX(CASE WHEN item_number = 20 THEN canonical_criterion_key END) AS key_item_20,
        MAX(CASE WHEN item_number = 20 THEN criterion_type END) AS tipo_item_20,
        MAX(CASE WHEN item_number = 20 THEN display_value END) AS valor_item_20,
        MAX(CASE WHEN item_number = 20 THEN feedback END) AS feedback_item_20,
        MAX(CASE WHEN item_number = 21 THEN canonical_criterion_name END) AS nombre_item_21,
        MAX(CASE WHEN item_number = 21 THEN canonical_criterion_key END) AS key_item_21,
        MAX(CASE WHEN item_number = 21 THEN criterion_type END) AS tipo_item_21,
        MAX(CASE WHEN item_number = 21 THEN display_value END) AS valor_item_21,
        MAX(CASE WHEN item_number = 21 THEN feedback END) AS feedback_item_21,
        MAX(CASE WHEN item_number = 22 THEN canonical_criterion_name END) AS nombre_item_22,
        MAX(CASE WHEN item_number = 22 THEN canonical_criterion_key END) AS key_item_22,
        MAX(CASE WHEN item_number = 22 THEN criterion_type END) AS tipo_item_22,
        MAX(CASE WHEN item_number = 22 THEN display_value END) AS valor_item_22,
        MAX(CASE WHEN item_number = 22 THEN feedback END) AS feedback_item_22,
        MAX(CASE WHEN item_number = 23 THEN canonical_criterion_name END) AS nombre_item_23,
        MAX(CASE WHEN item_number = 23 THEN canonical_criterion_key END) AS key_item_23,
        MAX(CASE WHEN item_number = 23 THEN criterion_type END) AS tipo_item_23,
        MAX(CASE WHEN item_number = 23 THEN display_value END) AS valor_item_23,
        MAX(CASE WHEN item_number = 23 THEN feedback END) AS feedback_item_23,
        MAX(CASE WHEN item_number = 24 THEN canonical_criterion_name END) AS nombre_item_24,
        MAX(CASE WHEN item_number = 24 THEN canonical_criterion_key END) AS key_item_24,
        MAX(CASE WHEN item_number = 24 THEN criterion_type END) AS tipo_item_24,
        MAX(CASE WHEN item_number = 24 THEN display_value END) AS valor_item_24,
        MAX(CASE WHEN item_number = 24 THEN feedback END) AS feedback_item_24,
        MAX(CASE WHEN item_number = 25 THEN canonical_criterion_name END) AS nombre_item_25,
        MAX(CASE WHEN item_number = 25 THEN canonical_criterion_key END) AS key_item_25,
        MAX(CASE WHEN item_number = 25 THEN criterion_type END) AS tipo_item_25,
        MAX(CASE WHEN item_number = 25 THEN display_value END) AS valor_item_25,
        MAX(CASE WHEN item_number = 25 THEN feedback END) AS feedback_item_25,
        MAX(CASE WHEN item_number = 26 THEN canonical_criterion_name END) AS nombre_item_26,
        MAX(CASE WHEN item_number = 26 THEN canonical_criterion_key END) AS key_item_26,
        MAX(CASE WHEN item_number = 26 THEN criterion_type END) AS tipo_item_26,
        MAX(CASE WHEN item_number = 26 THEN display_value END) AS valor_item_26,
        MAX(CASE WHEN item_number = 26 THEN feedback END) AS feedback_item_26,
        MAX(CASE WHEN item_number = 27 THEN canonical_criterion_name END) AS nombre_item_27,
        MAX(CASE WHEN item_number = 27 THEN canonical_criterion_key END) AS key_item_27,
        MAX(CASE WHEN item_number = 27 THEN criterion_type END) AS tipo_item_27,
        MAX(CASE WHEN item_number = 27 THEN display_value END) AS valor_item_27,
        MAX(CASE WHEN item_number = 27 THEN feedback END) AS feedback_item_27,
        MAX(CASE WHEN item_number = 28 THEN canonical_criterion_name END) AS nombre_item_28,
        MAX(CASE WHEN item_number = 28 THEN canonical_criterion_key END) AS key_item_28,
        MAX(CASE WHEN item_number = 28 THEN criterion_type END) AS tipo_item_28,
        MAX(CASE WHEN item_number = 28 THEN display_value END) AS valor_item_28,
        MAX(CASE WHEN item_number = 28 THEN feedback END) AS feedback_item_28,
        MAX(CASE WHEN item_number = 29 THEN canonical_criterion_name END) AS nombre_item_29,
        MAX(CASE WHEN item_number = 29 THEN canonical_criterion_key END) AS key_item_29,
        MAX(CASE WHEN item_number = 29 THEN criterion_type END) AS tipo_item_29,
        MAX(CASE WHEN item_number = 29 THEN display_value END) AS valor_item_29,
        MAX(CASE WHEN item_number = 29 THEN feedback END) AS feedback_item_29,
        MAX(CASE WHEN item_number = 30 THEN canonical_criterion_name END) AS nombre_item_30,
        MAX(CASE WHEN item_number = 30 THEN canonical_criterion_key END) AS key_item_30,
        MAX(CASE WHEN item_number = 30 THEN criterion_type END) AS tipo_item_30,
        MAX(CASE WHEN item_number = 30 THEN display_value END) AS valor_item_30,
        MAX(CASE WHEN item_number = 30 THEN feedback END) AS feedback_item_30,
        MAX(CASE WHEN item_number = 31 THEN canonical_criterion_name END) AS nombre_item_31,
        MAX(CASE WHEN item_number = 31 THEN canonical_criterion_key END) AS key_item_31,
        MAX(CASE WHEN item_number = 31 THEN criterion_type END) AS tipo_item_31,
        MAX(CASE WHEN item_number = 31 THEN display_value END) AS valor_item_31,
        MAX(CASE WHEN item_number = 31 THEN feedback END) AS feedback_item_31,
        MAX(CASE WHEN item_number = 32 THEN canonical_criterion_name END) AS nombre_item_32,
        MAX(CASE WHEN item_number = 32 THEN canonical_criterion_key END) AS key_item_32,
        MAX(CASE WHEN item_number = 32 THEN criterion_type END) AS tipo_item_32,
        MAX(CASE WHEN item_number = 32 THEN display_value END) AS valor_item_32,
        MAX(CASE WHEN item_number = 32 THEN feedback END) AS feedback_item_32,
        MAX(CASE WHEN item_number = 33 THEN canonical_criterion_name END) AS nombre_item_33,
        MAX(CASE WHEN item_number = 33 THEN canonical_criterion_key END) AS key_item_33,
        MAX(CASE WHEN item_number = 33 THEN criterion_type END) AS tipo_item_33,
        MAX(CASE WHEN item_number = 33 THEN display_value END) AS valor_item_33,
        MAX(CASE WHEN item_number = 33 THEN feedback END) AS feedback_item_33,
        MAX(CASE WHEN item_number = 34 THEN canonical_criterion_name END) AS nombre_item_34,
        MAX(CASE WHEN item_number = 34 THEN canonical_criterion_key END) AS key_item_34,
        MAX(CASE WHEN item_number = 34 THEN criterion_type END) AS tipo_item_34,
        MAX(CASE WHEN item_number = 34 THEN display_value END) AS valor_item_34,
        MAX(CASE WHEN item_number = 34 THEN feedback END) AS feedback_item_34,
        MAX(CASE WHEN item_number = 35 THEN canonical_criterion_name END) AS nombre_item_35,
        MAX(CASE WHEN item_number = 35 THEN canonical_criterion_key END) AS key_item_35,
        MAX(CASE WHEN item_number = 35 THEN criterion_type END) AS tipo_item_35,
        MAX(CASE WHEN item_number = 35 THEN display_value END) AS valor_item_35,
        MAX(CASE WHEN item_number = 35 THEN feedback END) AS feedback_item_35,
        MAX(CASE WHEN item_number = 36 THEN canonical_criterion_name END) AS nombre_item_36,
        MAX(CASE WHEN item_number = 36 THEN canonical_criterion_key END) AS key_item_36,
        MAX(CASE WHEN item_number = 36 THEN criterion_type END) AS tipo_item_36,
        MAX(CASE WHEN item_number = 36 THEN display_value END) AS valor_item_36,
        MAX(CASE WHEN item_number = 36 THEN feedback END) AS feedback_item_36,
        MAX(CASE WHEN item_number = 37 THEN canonical_criterion_name END) AS nombre_item_37,
        MAX(CASE WHEN item_number = 37 THEN canonical_criterion_key END) AS key_item_37,
        MAX(CASE WHEN item_number = 37 THEN criterion_type END) AS tipo_item_37,
        MAX(CASE WHEN item_number = 37 THEN display_value END) AS valor_item_37,
        MAX(CASE WHEN item_number = 37 THEN feedback END) AS feedback_item_37,
        MAX(CASE WHEN item_number = 38 THEN canonical_criterion_name END) AS nombre_item_38,
        MAX(CASE WHEN item_number = 38 THEN canonical_criterion_key END) AS key_item_38,
        MAX(CASE WHEN item_number = 38 THEN criterion_type END) AS tipo_item_38,
        MAX(CASE WHEN item_number = 38 THEN display_value END) AS valor_item_38,
        MAX(CASE WHEN item_number = 38 THEN feedback END) AS feedback_item_38,
        MAX(CASE WHEN item_number = 39 THEN canonical_criterion_name END) AS nombre_item_39,
        MAX(CASE WHEN item_number = 39 THEN canonical_criterion_key END) AS key_item_39,
        MAX(CASE WHEN item_number = 39 THEN criterion_type END) AS tipo_item_39,
        MAX(CASE WHEN item_number = 39 THEN display_value END) AS valor_item_39,
        MAX(CASE WHEN item_number = 39 THEN feedback END) AS feedback_item_39,
        MAX(CASE WHEN item_number = 40 THEN canonical_criterion_name END) AS nombre_item_40,
        MAX(CASE WHEN item_number = 40 THEN canonical_criterion_key END) AS key_item_40,
        MAX(CASE WHEN item_number = 40 THEN criterion_type END) AS tipo_item_40,
        MAX(CASE WHEN item_number = 40 THEN display_value END) AS valor_item_40,
        MAX(CASE WHEN item_number = 40 THEN feedback END) AS feedback_item_40,
        MAX(CASE WHEN item_number = 41 THEN canonical_criterion_name END) AS nombre_item_41,
        MAX(CASE WHEN item_number = 41 THEN canonical_criterion_key END) AS key_item_41,
        MAX(CASE WHEN item_number = 41 THEN criterion_type END) AS tipo_item_41,
        MAX(CASE WHEN item_number = 41 THEN display_value END) AS valor_item_41,
        MAX(CASE WHEN item_number = 41 THEN feedback END) AS feedback_item_41,
        MAX(CASE WHEN item_number = 42 THEN canonical_criterion_name END) AS nombre_item_42,
        MAX(CASE WHEN item_number = 42 THEN canonical_criterion_key END) AS key_item_42,
        MAX(CASE WHEN item_number = 42 THEN criterion_type END) AS tipo_item_42,
        MAX(CASE WHEN item_number = 42 THEN display_value END) AS valor_item_42,
        MAX(CASE WHEN item_number = 42 THEN feedback END) AS feedback_item_42,
        MAX(CASE WHEN item_number = 43 THEN canonical_criterion_name END) AS nombre_item_43,
        MAX(CASE WHEN item_number = 43 THEN canonical_criterion_key END) AS key_item_43,
        MAX(CASE WHEN item_number = 43 THEN criterion_type END) AS tipo_item_43,
        MAX(CASE WHEN item_number = 43 THEN display_value END) AS valor_item_43,
        MAX(CASE WHEN item_number = 43 THEN feedback END) AS feedback_item_43,
        MAX(CASE WHEN item_number = 44 THEN canonical_criterion_name END) AS nombre_item_44,
        MAX(CASE WHEN item_number = 44 THEN canonical_criterion_key END) AS key_item_44,
        MAX(CASE WHEN item_number = 44 THEN criterion_type END) AS tipo_item_44,
        MAX(CASE WHEN item_number = 44 THEN display_value END) AS valor_item_44,
        MAX(CASE WHEN item_number = 44 THEN feedback END) AS feedback_item_44,
        MAX(CASE WHEN item_number = 45 THEN canonical_criterion_name END) AS nombre_item_45,
        MAX(CASE WHEN item_number = 45 THEN canonical_criterion_key END) AS key_item_45,
        MAX(CASE WHEN item_number = 45 THEN criterion_type END) AS tipo_item_45,
        MAX(CASE WHEN item_number = 45 THEN display_value END) AS valor_item_45,
        MAX(CASE WHEN item_number = 45 THEN feedback END) AS feedback_item_45,
        MAX(CASE WHEN item_number = 46 THEN canonical_criterion_name END) AS nombre_item_46,
        MAX(CASE WHEN item_number = 46 THEN canonical_criterion_key END) AS key_item_46,
        MAX(CASE WHEN item_number = 46 THEN criterion_type END) AS tipo_item_46,
        MAX(CASE WHEN item_number = 46 THEN display_value END) AS valor_item_46,
        MAX(CASE WHEN item_number = 46 THEN feedback END) AS feedback_item_46,
        MAX(CASE WHEN item_number = 47 THEN canonical_criterion_name END) AS nombre_item_47,
        MAX(CASE WHEN item_number = 47 THEN canonical_criterion_key END) AS key_item_47,
        MAX(CASE WHEN item_number = 47 THEN criterion_type END) AS tipo_item_47,
        MAX(CASE WHEN item_number = 47 THEN display_value END) AS valor_item_47,
        MAX(CASE WHEN item_number = 47 THEN feedback END) AS feedback_item_47,
        MAX(CASE WHEN item_number = 48 THEN canonical_criterion_name END) AS nombre_item_48,
        MAX(CASE WHEN item_number = 48 THEN canonical_criterion_key END) AS key_item_48,
        MAX(CASE WHEN item_number = 48 THEN criterion_type END) AS tipo_item_48,
        MAX(CASE WHEN item_number = 48 THEN display_value END) AS valor_item_48,
        MAX(CASE WHEN item_number = 48 THEN feedback END) AS feedback_item_48,
        MAX(CASE WHEN item_number = 49 THEN canonical_criterion_name END) AS nombre_item_49,
        MAX(CASE WHEN item_number = 49 THEN canonical_criterion_key END) AS key_item_49,
        MAX(CASE WHEN item_number = 49 THEN criterion_type END) AS tipo_item_49,
        MAX(CASE WHEN item_number = 49 THEN display_value END) AS valor_item_49,
        MAX(CASE WHEN item_number = 49 THEN feedback END) AS feedback_item_49,
        MAX(CASE WHEN item_number = 50 THEN canonical_criterion_name END) AS nombre_item_50,
        MAX(CASE WHEN item_number = 50 THEN canonical_criterion_key END) AS key_item_50,
        MAX(CASE WHEN item_number = 50 THEN criterion_type END) AS tipo_item_50,
        MAX(CASE WHEN item_number = 50 THEN display_value END) AS valor_item_50,
        MAX(CASE WHEN item_number = 50 THEN feedback END) AS feedback_item_50,
        MAX(CASE WHEN item_number = 51 THEN canonical_criterion_name END) AS nombre_item_51,
        MAX(CASE WHEN item_number = 51 THEN canonical_criterion_key END) AS key_item_51,
        MAX(CASE WHEN item_number = 51 THEN criterion_type END) AS tipo_item_51,
        MAX(CASE WHEN item_number = 51 THEN display_value END) AS valor_item_51,
        MAX(CASE WHEN item_number = 51 THEN feedback END) AS feedback_item_51,
        MAX(CASE WHEN item_number = 52 THEN canonical_criterion_name END) AS nombre_item_52,
        MAX(CASE WHEN item_number = 52 THEN canonical_criterion_key END) AS key_item_52,
        MAX(CASE WHEN item_number = 52 THEN criterion_type END) AS tipo_item_52,
        MAX(CASE WHEN item_number = 52 THEN display_value END) AS valor_item_52,
        MAX(CASE WHEN item_number = 52 THEN feedback END) AS feedback_item_52,
        MAX(CASE WHEN item_number = 53 THEN canonical_criterion_name END) AS nombre_item_53,
        MAX(CASE WHEN item_number = 53 THEN canonical_criterion_key END) AS key_item_53,
        MAX(CASE WHEN item_number = 53 THEN criterion_type END) AS tipo_item_53,
        MAX(CASE WHEN item_number = 53 THEN display_value END) AS valor_item_53,
        MAX(CASE WHEN item_number = 53 THEN feedback END) AS feedback_item_53,
        MAX(CASE WHEN item_number = 54 THEN canonical_criterion_name END) AS nombre_item_54,
        MAX(CASE WHEN item_number = 54 THEN canonical_criterion_key END) AS key_item_54,
        MAX(CASE WHEN item_number = 54 THEN criterion_type END) AS tipo_item_54,
        MAX(CASE WHEN item_number = 54 THEN display_value END) AS valor_item_54,
        MAX(CASE WHEN item_number = 54 THEN feedback END) AS feedback_item_54,
        MAX(CASE WHEN item_number = 55 THEN canonical_criterion_name END) AS nombre_item_55,
        MAX(CASE WHEN item_number = 55 THEN canonical_criterion_key END) AS key_item_55,
        MAX(CASE WHEN item_number = 55 THEN criterion_type END) AS tipo_item_55,
        MAX(CASE WHEN item_number = 55 THEN display_value END) AS valor_item_55,
        MAX(CASE WHEN item_number = 55 THEN feedback END) AS feedback_item_55,
        MAX(CASE WHEN item_number = 56 THEN canonical_criterion_name END) AS nombre_item_56,
        MAX(CASE WHEN item_number = 56 THEN canonical_criterion_key END) AS key_item_56,
        MAX(CASE WHEN item_number = 56 THEN criterion_type END) AS tipo_item_56,
        MAX(CASE WHEN item_number = 56 THEN display_value END) AS valor_item_56,
        MAX(CASE WHEN item_number = 56 THEN feedback END) AS feedback_item_56,
        MAX(CASE WHEN item_number = 57 THEN canonical_criterion_name END) AS nombre_item_57,
        MAX(CASE WHEN item_number = 57 THEN canonical_criterion_key END) AS key_item_57,
        MAX(CASE WHEN item_number = 57 THEN criterion_type END) AS tipo_item_57,
        MAX(CASE WHEN item_number = 57 THEN display_value END) AS valor_item_57,
        MAX(CASE WHEN item_number = 57 THEN feedback END) AS feedback_item_57,
        MAX(CASE WHEN item_number = 58 THEN canonical_criterion_name END) AS nombre_item_58,
        MAX(CASE WHEN item_number = 58 THEN canonical_criterion_key END) AS key_item_58,
        MAX(CASE WHEN item_number = 58 THEN criterion_type END) AS tipo_item_58,
        MAX(CASE WHEN item_number = 58 THEN display_value END) AS valor_item_58,
        MAX(CASE WHEN item_number = 58 THEN feedback END) AS feedback_item_58,
        MAX(CASE WHEN item_number = 59 THEN canonical_criterion_name END) AS nombre_item_59,
        MAX(CASE WHEN item_number = 59 THEN canonical_criterion_key END) AS key_item_59,
        MAX(CASE WHEN item_number = 59 THEN criterion_type END) AS tipo_item_59,
        MAX(CASE WHEN item_number = 59 THEN display_value END) AS valor_item_59,
        MAX(CASE WHEN item_number = 59 THEN feedback END) AS feedback_item_59,
        MAX(CASE WHEN item_number = 60 THEN canonical_criterion_name END) AS nombre_item_60,
        MAX(CASE WHEN item_number = 60 THEN canonical_criterion_key END) AS key_item_60,
        MAX(CASE WHEN item_number = 60 THEN criterion_type END) AS tipo_item_60,
        MAX(CASE WHEN item_number = 60 THEN display_value END) AS valor_item_60,
        MAX(CASE WHEN item_number = 60 THEN feedback END) AS feedback_item_60
    FROM ordered_cte
    GROUP BY analysis_id
)
SELECT
    a.analysis_id,
    a.call_id                       AS conversation_id,
    CASE 
        WHEN LOWER(a.source) = 'hubspot' AND a.call_id IS NOT NULL THEN 'https://app.hubspot.com/calls/140451581/review/' || a.call_id
        ELSE NULL
    END                             AS link_hubspot,
    a.analysis_type,
    a.source,
    CASE 
        WHEN EXISTS (
            SELECT 1 FROM bm_analysis_criterion_results acr 
            WHERE acr.analysis_id = a.analysis_id
        ) THEN 'individual'::text
        ELSE 'individual_legacy'::text
    END                             AS source_type,
    (SELECT MAX(acr.service_id) FROM bm_analysis_criterion_results acr WHERE acr.analysis_id = a.analysis_id) AS service_id,
    (SELECT MAX(acr.service_key) FROM bm_analysis_criterion_results acr WHERE acr.analysis_id = a.analysis_id) AS service_key,
    (SELECT MAX(acr.service_name) FROM bm_analysis_criterion_results acr WHERE acr.analysis_id = a.analysis_id) AS service_name,
    (SELECT MAX(acr.typology_id) FROM bm_analysis_criterion_results acr WHERE acr.analysis_id = a.analysis_id) AS typology_id,
    COALESCE(
        (SELECT MAX(acr.typology_key) FROM bm_analysis_criterion_results acr WHERE acr.analysis_id = a.analysis_id),
        COALESCE(
            (SELECT COALESCE(category_value, text_value) 
             FROM bm_analysis_criterion_results 
             WHERE analysis_id = a.analysis_id 
               AND criterion_key = 'tipo_llamada' 
               AND is_applicable = true 
             LIMIT 1),
            (SELECT COALESCE(value_category, value_text) 
             FROM bm_analysis_results 
             WHERE analysis_id = a.analysis_id 
               AND criterion_key = 'tipo_llamada' 
             LIMIT 1),
            a.tipo_llamada
        )
    )                               AS typology_key,
    COALESCE(
        (SELECT MAX(acr.typology_name) FROM bm_analysis_criterion_results acr WHERE acr.analysis_id = a.analysis_id),
        CASE COALESCE(
            (SELECT COALESCE(category_value, text_value) 
             FROM bm_analysis_criterion_results 
             WHERE analysis_id = a.analysis_id 
               AND criterion_key = 'tipo_llamada' 
               AND is_applicable = true 
             LIMIT 1),
            (SELECT COALESCE(value_category, value_text) 
             FROM bm_analysis_results 
             WHERE analysis_id = a.analysis_id 
               AND criterion_key = 'tipo_llamada' 
             LIMIT 1),
            a.tipo_llamada
        )
            WHEN 'cita' THEN 'Cita'
            WHEN 'confirmacion' THEN 'Confirmación'
            WHEN 'cancelacion' THEN 'Cancelación'
            WHEN 'reagendo' THEN 'Reagendo'
            WHEN 'falta' THEN 'Falta'
            WHEN 'otros' THEN 'Otros'
            WHEN 'informacion_sin_cita' THEN 'Información sin cita'
            ELSE INITCAP(REPLACE(COALESCE(
                (SELECT COALESCE(category_value, text_value) 
                 FROM bm_analysis_criterion_results 
                 WHERE analysis_id = a.analysis_id 
                   AND criterion_key = 'tipo_llamada' 
                   AND is_applicable = true 
                 LIMIT 1),
                (SELECT COALESCE(value_category, value_text) 
                 FROM bm_analysis_results 
                 WHERE analysis_id = a.analysis_id 
                   AND criterion_key = 'tipo_llamada' 
                 LIMIT 1),
                a.tipo_llamada
            ), '_', ' '))
        END
    )                               AS typology_name,
    COALESCE(
        (SELECT COALESCE(category_value, text_value) 
         FROM bm_analysis_criterion_results 
         WHERE analysis_id = a.analysis_id 
           AND criterion_key = 'tipo_llamada' 
           AND is_applicable = true 
         LIMIT 1),
        (SELECT COALESCE(value_category, value_text) 
         FROM bm_analysis_results 
         WHERE analysis_id = a.analysis_id 
           AND criterion_key = 'tipo_llamada' 
         LIMIT 1),
        a.tipo_llamada
    )                               AS tipo_llamada,
    a.hubspot_owner_id              AS agent_owner_id,
    a.agente_telefonico             AS agent_name,
    a.call_timestamp,
    a.call_timestamp::date          AS call_date,
    a.fecha_eval::date              AS eval_date,
    a.prompt_id,
    a.prompt_version_id,
    a.created_at                    AS analysis_timestamp,
    COALESCE(a.result->>'resumen', a.result->>'resumen_llamada', a.result->>'summary') AS resumen,
    a.evaluacion_global,
    p.cierre_cita_criterio          AS cierre_cita,
    p.nombre_item_1, p.key_item_1, p.tipo_item_1, p.valor_item_1, p.feedback_item_1,
    p.nombre_item_2, p.key_item_2, p.tipo_item_2, p.valor_item_2, p.feedback_item_2,
    p.nombre_item_3, p.key_item_3, p.tipo_item_3, p.valor_item_3, p.feedback_item_3,
    p.nombre_item_4, p.key_item_4, p.tipo_item_4, p.valor_item_4, p.feedback_item_4,
    p.nombre_item_5, p.key_item_5, p.tipo_item_5, p.valor_item_5, p.feedback_item_5,
    p.nombre_item_6, p.key_item_6, p.tipo_item_6, p.valor_item_6, p.feedback_item_6,
    p.nombre_item_7, p.key_item_7, p.tipo_item_7, p.valor_item_7, p.feedback_item_7,
    p.nombre_item_8, p.key_item_8, p.tipo_item_8, p.valor_item_8, p.feedback_item_8,
    p.nombre_item_9, p.key_item_9, p.tipo_item_9, p.valor_item_9, p.feedback_item_9,
    p.nombre_item_10, p.key_item_10, p.tipo_item_10, p.valor_item_10, p.feedback_item_10,
    p.nombre_item_11, p.key_item_11, p.tipo_item_11, p.valor_item_11, p.feedback_item_11,
    p.nombre_item_12, p.key_item_12, p.tipo_item_12, p.valor_item_12, p.feedback_item_12,
    p.nombre_item_13, p.key_item_13, p.tipo_item_13, p.valor_item_13, p.feedback_item_13,
    p.nombre_item_14, p.key_item_14, p.tipo_item_14, p.valor_item_14, p.feedback_item_14,
    p.nombre_item_15, p.key_item_15, p.tipo_item_15, p.valor_item_15, p.feedback_item_15,
    p.nombre_item_16, p.key_item_16, p.tipo_item_16, p.valor_item_16, p.feedback_item_16,
    p.nombre_item_17, p.key_item_17, p.tipo_item_17, p.valor_item_17, p.feedback_item_17,
    p.nombre_item_18, p.key_item_18, p.tipo_item_18, p.valor_item_18, p.feedback_item_18,
    p.nombre_item_19, p.key_item_19, p.tipo_item_19, p.valor_item_19, p.feedback_item_19,
    p.nombre_item_20, p.key_item_20, p.tipo_item_20, p.valor_item_20, p.feedback_item_20,
    p.nombre_item_21, p.key_item_21, p.tipo_item_21, p.valor_item_21, p.feedback_item_21,
    p.nombre_item_22, p.key_item_22, p.tipo_item_22, p.valor_item_22, p.feedback_item_22,
    p.nombre_item_23, p.key_item_23, p.tipo_item_23, p.valor_item_23, p.feedback_item_23,
    p.nombre_item_24, p.key_item_24, p.tipo_item_24, p.valor_item_24, p.feedback_item_24,
    p.nombre_item_25, p.key_item_25, p.tipo_item_25, p.valor_item_25, p.feedback_item_25,
    p.nombre_item_26, p.key_item_26, p.tipo_item_26, p.valor_item_26, p.feedback_item_26,
    p.nombre_item_27, p.key_item_27, p.tipo_item_27, p.valor_item_27, p.feedback_item_27,
    p.nombre_item_28, p.key_item_28, p.tipo_item_28, p.valor_item_28, p.feedback_item_28,
    p.nombre_item_29, p.key_item_29, p.tipo_item_29, p.valor_item_29, p.feedback_item_29,
    p.nombre_item_30, p.key_item_30, p.tipo_item_30, p.valor_item_30, p.feedback_item_30,
    p.nombre_item_31, p.key_item_31, p.tipo_item_31, p.valor_item_31, p.feedback_item_31,
    p.nombre_item_32, p.key_item_32, p.tipo_item_32, p.valor_item_32, p.feedback_item_32,
    p.nombre_item_33, p.key_item_33, p.tipo_item_33, p.valor_item_33, p.feedback_item_33,
    p.nombre_item_34, p.key_item_34, p.tipo_item_34, p.valor_item_34, p.feedback_item_34,
    p.nombre_item_35, p.key_item_35, p.tipo_item_35, p.valor_item_35, p.feedback_item_35,
    p.nombre_item_36, p.key_item_36, p.tipo_item_36, p.valor_item_36, p.feedback_item_36,
    p.nombre_item_37, p.key_item_37, p.tipo_item_37, p.valor_item_37, p.feedback_item_37,
    p.nombre_item_38, p.key_item_38, p.tipo_item_38, p.valor_item_38, p.feedback_item_38,
    p.nombre_item_39, p.key_item_39, p.tipo_item_39, p.valor_item_39, p.feedback_item_39,
    p.nombre_item_40, p.key_item_40, p.tipo_item_40, p.valor_item_40, p.feedback_item_40,
    p.nombre_item_41, p.key_item_41, p.tipo_item_41, p.valor_item_41, p.feedback_item_41,
    p.nombre_item_42, p.key_item_42, p.tipo_item_42, p.valor_item_42, p.feedback_item_42,
    p.nombre_item_43, p.key_item_43, p.tipo_item_43, p.valor_item_43, p.feedback_item_43,
    p.nombre_item_44, p.key_item_44, p.tipo_item_44, p.valor_item_44, p.feedback_item_44,
    p.nombre_item_45, p.key_item_45, p.tipo_item_45, p.valor_item_45, p.feedback_item_45,
    p.nombre_item_46, p.key_item_46, p.tipo_item_46, p.valor_item_46, p.feedback_item_46,
    p.nombre_item_47, p.key_item_47, p.tipo_item_47, p.valor_item_47, p.feedback_item_47,
    p.nombre_item_48, p.key_item_48, p.tipo_item_48, p.valor_item_48, p.feedback_item_48,
    p.nombre_item_49, p.key_item_49, p.tipo_item_49, p.valor_item_49, p.feedback_item_49,
    p.nombre_item_50, p.key_item_50, p.tipo_item_50, p.valor_item_50, p.feedback_item_50,
    p.nombre_item_51, p.key_item_51, p.tipo_item_51, p.valor_item_51, p.feedback_item_51,
    p.nombre_item_52, p.key_item_52, p.tipo_item_52, p.valor_item_52, p.feedback_item_52,
    p.nombre_item_53, p.key_item_53, p.tipo_item_53, p.valor_item_53, p.feedback_item_53,
    p.nombre_item_54, p.key_item_54, p.tipo_item_54, p.valor_item_54, p.feedback_item_54,
    p.nombre_item_55, p.key_item_55, p.tipo_item_55, p.valor_item_55, p.feedback_item_55,
    p.nombre_item_56, p.key_item_56, p.tipo_item_56, p.valor_item_56, p.feedback_item_56,
    p.nombre_item_57, p.key_item_57, p.tipo_item_57, p.valor_item_57, p.feedback_item_57,
    p.nombre_item_58, p.key_item_58, p.tipo_item_58, p.valor_item_58, p.feedback_item_58,
    p.nombre_item_59, p.key_item_59, p.tipo_item_59, p.valor_item_59, p.feedback_item_59,
    p.nombre_item_60, p.key_item_60, p.tipo_item_60, p.valor_item_60, p.feedback_item_60
FROM bm_analyses a
LEFT JOIN pivoted_cte p ON a.analysis_id = p.analysis_id
WHERE a.status = 'completed';
