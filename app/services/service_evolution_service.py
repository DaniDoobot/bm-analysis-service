"""Service logic for Service Evolution dashboard."""
import logging
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.service_evolution import (
    ServiceEvolutionResponse,
    ServiceEvolutionFilters,
    ServiceEvolutionSummary,
    ServiceEvolutionSeriesItem,
    ServiceEvolutionTypologyItem,
    ServiceEvolutionAgentItem,
    ServiceEvolutionCriteriaRankingItem,
    ServiceListItem,
    CriterionListItem,
)

logger = logging.getLogger(__name__)


def parse_date_bounds(date_from: str | None, date_to: str | None) -> tuple[datetime | None, datetime | None]:
    """
    Parses start and end date/timestamp parameters safely.
    For date-only strings (like YYYY-MM-DD), date_from represents start of day (00:00:00.000000)
    and date_to represents inclusive end of day (23:59:59.999999).
    """
    parsed_date_from = None
    parsed_date_to = None

    if date_from:
        is_date_only = len(date_from.strip()) == 10 and "-" in date_from and ":" not in date_from
        try:
            parsed_date_from = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date_from = datetime.strptime(date_from, "%Y-%m-%d")
                is_date_only = True
            except ValueError:
                logger.warning("Invalid date_from format: %s", date_from)
                
        if parsed_date_from and is_date_only:
            # Already represents 00:00:00, which is correct (start of the day)
            pass

    if date_to:
        is_date_only = len(date_to.strip()) == 10 and "-" in date_to and ":" not in date_to
        try:
            parsed_date_to = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date_to = datetime.strptime(date_to, "%Y-%m-%d")
                is_date_only = True
            except ValueError:
                logger.warning("Invalid date_to format: %s", date_to)
                
        if parsed_date_to and is_date_only:
            # If date only, make inclusive of the end of the day: 23:59:59.999999
            parsed_date_to = parsed_date_to.replace(hour=23, minute=59, second=59, microsecond=999999)

    return parsed_date_from, parsed_date_to


class ServiceEvolutionService:
    @staticmethod
    async def get_services(
        db: AsyncSession,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[ServiceListItem]:
        """
        GET /bm/service-evolution/services
        Returns all active services with calls count and analysis date ranges.
        """
        parsed_date_from, parsed_date_to = parse_date_bounds(date_from, date_to)
        logger.info(
            "Fetching services list: date_from=%s (parsed: %s), date_to=%s (parsed: %s)",
            date_from, parsed_date_from, date_to, parsed_date_to
        )

        query = text("""
            SELECT 
                s.service_id,
                s.service_key,
                s.service_name,
                COALESCE(COUNT(DISTINCT r.mass_analysis_id), 0) AS total_calls,
                MIN(r.created_at::date)::text AS first_analysis_date,
                MAX(r.created_at::date)::text AS last_analysis_date
            FROM bm_services s
            LEFT JOIN bm_mass_evaluation_results r ON s.service_id = r.service_id 
              AND r.status = 'completed'
              AND (CAST(:date_from AS timestamptz) IS NULL OR r.created_at >= CAST(:date_from AS timestamptz))
              AND (CAST(:date_to AS timestamptz) IS NULL OR r.created_at <= CAST(:date_to AS timestamptz))
            WHERE s.is_active = true
            GROUP BY s.service_id, s.service_key, s.service_name
            ORDER BY s.service_name;
        """)
        
        result = await db.execute(query, {
            "date_from": parsed_date_from,
            "date_to": parsed_date_to,
        })
        rows = result.fetchall()
        
        services_list = []
        for r in rows:
            services_list.append(ServiceListItem(
                service_id=r[0],
                service_key=r[1],
                service_name=r[2],
                total_calls=r[3],
                first_analysis_date=r[4],
                last_analysis_date=r[5],
            ))
        return services_list

    @staticmethod
    async def get_criteria(
        db: AsyncSession,
        service_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[CriterionListItem]:
        """
        GET /bm/service-evolution/criteria
        Returns all criteria key details.
        """
        parsed_date_from, parsed_date_to = parse_date_bounds(date_from, date_to)
        logger.info(
            "Fetching criteria list: service_id=%s, date_from=%s (parsed: %s), date_to=%s (parsed: %s)",
            service_id, date_from, parsed_date_from, date_to, parsed_date_to
        )

        query = text("""
            SELECT 
                c.criterion_key,
                MAX(c.criterion_name) AS criterion_name,
                MAX(c.criterion_type) AS criterion_type,
                COUNT(CASE WHEN c.is_applicable = true THEN 1 END) AS total_applicable
            FROM bm_mass_evaluation_criterion_results c
            JOIN bm_mass_evaluation_results r ON c.mass_analysis_id = r.mass_analysis_id
            WHERE r.status = 'completed'
              AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
              AND (CAST(:date_from AS timestamptz) IS NULL OR r.created_at >= CAST(:date_from AS timestamptz))
              AND (CAST(:date_to AS timestamptz) IS NULL OR r.created_at <= CAST(:date_to AS timestamptz))
            GROUP BY c.criterion_key
            ORDER BY total_applicable DESC;
        """)
        
        result = await db.execute(query, {
            "service_id": service_id,
            "date_from": parsed_date_from,
            "date_to": parsed_date_to,
        })
        rows = result.fetchall()
        
        criteria_list = []
        for r in rows:
            criteria_list.append(CriterionListItem(
                criterion_key=r[0],
                criterion_name=r[1],
                criterion_type=r[2],
                total_applicable=r[3],
            ))
        return criteria_list

    @staticmethod
    async def get_evolution(
        db: AsyncSession,
        service_id: int | None = None,
        service_key: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        granularity: str = "day",
        typology_key: str | None = None,
        agent_owner_id: str | None = None,
        criteria: str | None = None,
        typology_ids: list[int] | None = None,
        duration_min_seconds: int | None = None,
        duration_max_seconds: int | None = None,
        avg_score_min: float | None = None,
        avg_score_max: float | None = None,
    ) -> ServiceEvolutionResponse:
        """
        GET /bm/service-evolution
        Retrieves complete service evolution details with series, typologies, agents, and criteria ranking.
        """
        _EG_EXPR = """
            COALESCE(
                NULLIF((r.result_json->>'evaluacion_global')::numeric, 0),
                (
                    SELECT AVG(sub.numeric_value)
                    FROM bm_mass_evaluation_criterion_results sub
                    WHERE sub.mass_analysis_id = r.mass_analysis_id
                      AND sub.criterion_type = 'score_1_10'
                      AND sub.is_applicable = true
                      AND sub.numeric_value IS NOT NULL
                      AND sub.criterion_key NOT IN (
                          'hablando_agente', 'hablando_paciente',
                          'palabras_minuto_agente', 'palabras_minuto_paciente',
                          'velocidad_hablando_agente', 'velocidad_hablando_paciente',
                          'meses_patologia', 'cuanto_tiempo', 'duracion_consulta'
                      )
                )
            )
        """
        parsed_date_from, parsed_date_to = parse_date_bounds(date_from, date_to)
        logger.info(
            "Service evolution query: service_id=%s, service_key=%s, granularity=%s, typology=%s, agent=%s, "
            "date_from_raw=%s (parsed: %s), date_to_raw=%s (parsed: %s)",
            service_id, service_key, granularity, typology_key, agent_owner_id,
            date_from, parsed_date_from, date_to, parsed_date_to
        )

        # 1. Resolve effective service name
        service_name = None
        if service_id:
            s_res = await db.execute(text("SELECT service_name FROM bm_services WHERE service_id = :id"), {"id": service_id})
            s_row = s_res.fetchone()
            if s_row:
                service_name = s_row[0]
        elif service_key:
            s_res = await db.execute(text("SELECT service_name, service_id FROM bm_services WHERE service_key = :key"), {"key": service_key})
            s_row = s_res.fetchone()
            if s_row:
                service_name = s_row[0]
                service_id = s_row[1]

        filters = ServiceEvolutionFilters(
            service_id=service_id,
            service_key=service_key,
            service_name=service_name,
            date_from=date_from,
            date_to=date_to,
            granularity=granularity
        )

        params = {
            "service_id": service_id,
            "service_key": service_key,
            "date_from": parsed_date_from,
            "date_to": parsed_date_to,
            "typology_key": typology_key,
            "agent_owner_id": agent_owner_id,
        }

        # Official scale: 0-10 (matches DB storage). Legacy compat: if value > 10 assume 0-100 and divide.
        score_min_scaled = (avg_score_min / 10.0 if avg_score_min > 10.0 else avg_score_min) if avg_score_min is not None else None
        score_max_scaled = (avg_score_max / 10.0 if avg_score_max > 10.0 else avg_score_max) if avg_score_max is not None else None

        extra_sql = ""
        extra_sql_left_join = ""
        if typology_ids:
            ids_str = ",".join(str(tid) for tid in typology_ids)
            extra_sql += f" AND r.typology_id IN ({ids_str})"
            extra_sql_left_join += f" AND r.typology_id IN ({ids_str})"
        if duration_min_seconds is not None:
            extra_sql += " AND r.call_duration_seconds >= :duration_min_seconds"
            extra_sql_left_join += " AND r.call_duration_seconds >= :duration_min_seconds"
            params["duration_min_seconds"] = duration_min_seconds
        if duration_max_seconds is not None:
            extra_sql += " AND r.call_duration_seconds <= :duration_max_seconds"
            extra_sql_left_join += " AND r.call_duration_seconds <= :duration_max_seconds"
            params["duration_max_seconds"] = duration_max_seconds
        if score_min_scaled is not None:
            extra_sql += f" AND ({_EG_EXPR}) >= :score_min_scaled"
            extra_sql_left_join += f" AND ({_EG_EXPR}) >= :score_min_scaled"
            params["score_min_scaled"] = score_min_scaled
        if score_max_scaled is not None:
            extra_sql += f" AND ({_EG_EXPR}) <= :score_max_scaled"
            extra_sql_left_join += f" AND ({_EG_EXPR}) <= :score_max_scaled"
            params["score_max_scaled"] = score_max_scaled

        # 2. Get Summary metrics
        # evaluacion_global fallback: if result_json has no 'evaluacion_global' key (analyses produced
        # with the dynamic-criteria system), compute it as the mean of all score_1_10 applicable
        # criterion_results, excluding non-qualitative keys (percentages, raw numbers, text, booleans).

        summary_query = text(f"""
            SELECT 
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                AVG({_EG_EXPR}) AS avg_evaluacion_global,
                AVG(CASE WHEN c.criterion_key = 'claridad' AND c.is_applicable = true THEN c.numeric_value END) AS avg_claridad,
                AVG(CASE WHEN c.criterion_key = 'empatia' AND c.is_applicable = true THEN c.numeric_value END) AS avg_empatia,
                AVG(CASE WHEN c.criterion_key = 'procedimiento' AND c.is_applicable = true THEN c.numeric_value END) AS avg_procedimiento,
                AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate
            FROM bm_mass_evaluation_results r
            LEFT JOIN bm_mass_evaluation_criterion_results c ON r.mass_analysis_id = c.mass_analysis_id
            WHERE r.status = 'completed'
              AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
              AND (CAST(:service_key AS text) IS NULL OR r.service_key = CAST(:service_key AS text))
              AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
              AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
              AND (CAST(:typology_key AS text) IS NULL OR r.typology_key = CAST(:typology_key AS text))
              AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
              {extra_sql}
        """)
        sum_res = await db.execute(summary_query, params)
        sum_row = sum_res.fetchone()
        
        total_calls = 0
        avg_eg = None
        avg_cl = None
        avg_em = None
        avg_pr = None
        cc_rate = None
        
        if sum_row and sum_row[0] > 0:
            total_calls = sum_row[0]
            avg_eg = float(sum_row[1]) if sum_row[1] is not None else None
            avg_cl = float(sum_row[2]) if sum_row[2] is not None else None
            avg_em = float(sum_row[3]) if sum_row[3] is not None else None
            avg_pr = float(sum_row[4]) if sum_row[4] is not None else None
            cc_rate = float(sum_row[5]) if sum_row[5] is not None else None

        logger.info(
            "Service evolution query completed. total_calls matched: %s", total_calls
        )

        # 3. Get Main Typology
        main_typo = None
        if total_calls > 0:
            mt_query = text(f"""
                SELECT 
                    r.typology_name
                FROM bm_mass_evaluation_results r
                WHERE r.status = 'completed' AND r.typology_name IS NOT NULL
                  AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
                  AND (CAST(:service_key AS text) IS NULL OR r.service_key = CAST(:service_key AS text))
                  AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
                  AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
                  AND (CAST(:typology_key AS text) IS NULL OR r.typology_key = CAST(:typology_key AS text))
                  AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
                  {extra_sql}
                GROUP BY r.typology_name
                ORDER BY COUNT(*) DESC
                LIMIT 1;
            """)
            mt_res = await db.execute(mt_query, params)
            mt_row = mt_res.fetchone()
            if mt_row:
                main_typo = mt_row[0]

        summary = ServiceEvolutionSummary(
            total_calls=total_calls,
            avg_evaluacion_global=avg_eg,
            avg_claridad=avg_cl,
            avg_empatia=avg_em,
            avg_procedimiento=avg_pr,
            cierre_cita_rate=cc_rate,
            main_typology=main_typo
        )

        # 4. Get Series data (granularity-based period grouping)
        # NOTE: we group by call_timestamp (when the call actually happened), NOT created_at
        # (when the mass analysis ran). Using created_at collapses everything into one day.
        if granularity == "week":
            period_expr = "date_trunc('week', r.call_timestamp)::date"
        elif granularity == "month":
            period_expr = "date_trunc('month', r.call_timestamp)::date"
        else:
            period_expr = "r.call_timestamp::date"

        # Column indices:  0=period, 1=service_id, 2=service_name, 3=total_calls,
        #   4=avg_evaluacion_global (fallback: mean of score_1_10 criteria if not in result_json),
        #   5=avg_sentiment, 6=avg_empatia, 7=avg_simpatia,
        #   8=avg_claridad, 9=avg_procedimiento, 10=avg_saludo_inicio, 11=avg_n3_preguntas,
        #   12=avg_gestion_objeciones, 13=avg_propension, 14=cierre_cita_rate
        series_query = text(f"""
            SELECT 
                {period_expr} AS period,
                r.service_id,
                r.service_name,
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                AVG({_EG_EXPR}) AS avg_evaluacion_global,
                AVG(CASE WHEN c.criterion_key = 'sentiment' AND c.is_applicable = true THEN c.numeric_value END) AS avg_sentiment,
                AVG(CASE WHEN c.criterion_key = 'empatia' AND c.is_applicable = true THEN c.numeric_value END) AS avg_empatia,
                AVG(CASE WHEN c.criterion_key = 'simpatia' AND c.is_applicable = true THEN c.numeric_value END) AS avg_simpatia,
                AVG(CASE WHEN c.criterion_key = 'claridad' AND c.is_applicable = true THEN c.numeric_value END) AS avg_claridad,
                AVG(CASE WHEN c.criterion_key = 'procedimiento' AND c.is_applicable = true THEN c.numeric_value END) AS avg_procedimiento,
                AVG(CASE WHEN c.criterion_key = 'saludo_inicio' AND c.is_applicable = true THEN c.numeric_value END) AS avg_saludo_inicio,
                AVG(CASE WHEN c.criterion_key = 'n3_preguntas' AND c.is_applicable = true THEN c.numeric_value END) AS avg_n3_preguntas,
                AVG(CASE WHEN c.criterion_key = 'gestion_objeciones' AND c.is_applicable = true THEN c.numeric_value END) AS avg_gestion_objeciones,
                AVG(CASE WHEN c.criterion_key = 'propension' AND c.is_applicable = true THEN c.numeric_value END) AS avg_propension,
                AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate
            FROM bm_mass_evaluation_results r
            LEFT JOIN bm_mass_evaluation_criterion_results c ON r.mass_analysis_id = c.mass_analysis_id
            WHERE r.status = 'completed'
              AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
              AND (CAST(:service_key AS text) IS NULL OR r.service_key = CAST(:service_key AS text))
              AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
              AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
              AND (CAST(:typology_key AS text) IS NULL OR r.typology_key = CAST(:typology_key AS text))
              AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
              {extra_sql}
            GROUP BY period, r.service_id, r.service_name
            ORDER BY period ASC, service_name ASC;
        """)
        
        series_res = await db.execute(series_query, params)
        series_rows = series_res.fetchall()
        
        series_list = []
        for row in series_rows:
            series_list.append(ServiceEvolutionSeriesItem(
                period=str(row[0]),
                service_id=row[1],
                service_name=row[2],
                total_calls=row[3],
                analysis_count=row[3],
                avg_evaluacion_global=float(row[4]) if row[4] is not None else None,
                avg_sentiment=float(row[5]) if row[5] is not None else None,
                avg_empatia=float(row[6]) if row[6] is not None else None,
                avg_simpatia=float(row[7]) if row[7] is not None else None,
                avg_claridad=float(row[8]) if row[8] is not None else None,
                avg_procedimiento=float(row[9]) if row[9] is not None else None,
                avg_saludo_inicio=float(row[10]) if row[10] is not None else None,  # row[10]
                avg_n3_preguntas=float(row[11]) if row[11] is not None else None,   # row[11]
                avg_gestion_objeciones=float(row[12]) if row[12] is not None else None,  # row[12]
                avg_propension=float(row[13]) if row[13] is not None else None,     # row[13]
                cierre_cita_rate=float(row[14]) if row[14] is not None else None,   # row[14]
            ))

        # 5. Get Typology split
        # Query 1: Active typologies from bm_typologies + LEFT JOIN call statistics
        active_typo_query = text(f"""
            SELECT 
                t.typology_id,
                t.typology_key,
                t.typology_name,
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                AVG({_EG_EXPR}) AS avg_evaluacion_global,
                AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate
            FROM bm_typologies t
            LEFT JOIN bm_mass_evaluation_results r 
                ON t.typology_key = r.typology_key 
                AND t.service_id = r.service_id
                AND r.status = 'completed'
                AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
                AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
                AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
                {extra_sql_left_join}
            LEFT JOIN bm_mass_evaluation_criterion_results c 
                ON r.mass_analysis_id = c.mass_analysis_id
            WHERE t.is_active = true
              AND (CAST(:service_id AS integer) IS NULL OR t.service_id = CAST(:service_id AS integer))
              AND (CAST(:typology_key AS text) IS NULL OR t.typology_key = CAST(:typology_key AS text))
            GROUP BY t.typology_id, t.typology_key, t.typology_name, t.sort_order
            ORDER BY t.sort_order ASC, t.typology_id ASC;
        """)
        active_typo_res = await db.execute(active_typo_query, params)
        active_typo_rows = active_typo_res.fetchall()
        
        by_typology = []
        for row in active_typo_rows:
            by_typology.append(ServiceEvolutionTypologyItem(
                typology_id=row[0],
                typology_key=row[1],
                typology_name=row[2],
                total_calls=row[3],
                avg_evaluacion_global=float(row[4]) if row[4] is not None else None,
                cierre_cita_rate=float(row[5]) if row[5] is not None else None,
            ))

        # Query 2: Unclassified/unlisted calls
        # Only run if typology_key is either None or "unclassified"
        if typology_key is None or typology_key.lower() == "unclassified":
            unclass_query = text(f"""
                SELECT 
                    COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                    AVG({_EG_EXPR}) AS avg_evaluacion_global,
                    AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate
                FROM bm_mass_evaluation_results r
                LEFT JOIN bm_mass_evaluation_criterion_results c ON r.mass_analysis_id = c.mass_analysis_id
                WHERE r.status = 'completed'
                  AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
                  AND (CAST(:service_key AS text) IS NULL OR r.service_key = CAST(:service_key AS text))
                  AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
                  AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
                  AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
                  {extra_sql}
                  AND (
                    r.typology_key IS NULL 
                    OR r.typology_key NOT IN (
                        SELECT typology_key 
                        FROM bm_typologies 
                        WHERE is_active = true 
                          AND (CAST(:service_id AS integer) IS NULL OR service_id = CAST(:service_id AS integer))
                    )
                  );
            """)
            unclass_res = await db.execute(unclass_query, params)
            unclass_row = unclass_res.fetchone()
            
            if unclass_row and unclass_row[0] > 0:
                by_typology.append(ServiceEvolutionTypologyItem(
                    typology_id=None,
                    typology_key="unclassified",
                    typology_name="Sin clasificar",
                    total_calls=unclass_row[0],
                    avg_evaluacion_global=float(unclass_row[1]) if unclass_row[1] is not None else None,
                    cierre_cita_rate=float(unclass_row[2]) if unclass_row[2] is not None else None,
                ))

        # 6. Get Agent split
        agent_query = text(f"""
            SELECT 
                r.hubspot_owner_id AS agent_owner_id,
                COALESCE(r.agent_name, 'Unknown') AS agent_name,
                COUNT(DISTINCT r.mass_analysis_id) AS total_calls,
                AVG({_EG_EXPR}) AS avg_evaluacion_global,
                AVG(CASE WHEN c.criterion_key = 'claridad' AND c.is_applicable = true THEN c.numeric_value END) AS avg_claridad,
                AVG(CASE WHEN c.criterion_key = 'cierre_cita' AND c.is_applicable = true AND c.boolean_value IS NOT NULL THEN c.boolean_value::int END) AS cierre_cita_rate
            FROM bm_mass_evaluation_results r
            LEFT JOIN bm_mass_evaluation_criterion_results c ON r.mass_analysis_id = c.mass_analysis_id
            WHERE r.status = 'completed'
              AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
              AND (CAST(:service_key AS text) IS NULL OR r.service_key = CAST(:service_key AS text))
              AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
              AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
              AND (CAST(:typology_key AS text) IS NULL OR r.typology_key = CAST(:typology_key AS text))
              AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
              {extra_sql}
            GROUP BY r.hubspot_owner_id, r.agent_name
            ORDER BY total_calls DESC;
        """)
        agent_res = await db.execute(agent_query, params)
        agent_rows = agent_res.fetchall()
        by_agent = []
        for row in agent_rows:
            by_agent.append(ServiceEvolutionAgentItem(
                agent_owner_id=row[0],
                agent_name=row[1],
                total_calls=row[2],
                avg_evaluacion_global=float(row[3]) if row[3] is not None else None,
                avg_claridad=float(row[4]) if row[4] is not None else None,
                cierre_cita_rate=float(row[5]) if row[5] is not None else None,
            ))

        # 7. Get Criteria Ranking
        ranking_query = text(f"""
            SELECT 
                c.criterion_key,
                MAX(c.criterion_name) AS criterion_name,
                AVG(c.numeric_value) AS avg_value,
                COUNT(CASE WHEN c.is_applicable = true THEN 1 END) AS total_applicable
            FROM bm_mass_evaluation_criterion_results c
            JOIN bm_mass_evaluation_results r ON c.mass_analysis_id = r.mass_analysis_id
            WHERE r.status = 'completed'
              AND c.is_applicable = true
              AND c.numeric_value IS NOT NULL
              AND (CAST(:service_id AS integer) IS NULL OR r.service_id = CAST(:service_id AS integer))
              AND (CAST(:service_key AS text) IS NULL OR r.service_key = CAST(:service_key AS text))
              AND (CAST(:date_from AS timestamptz) IS NULL OR r.call_timestamp >= CAST(:date_from AS timestamptz))
              AND (CAST(:date_to AS timestamptz) IS NULL OR r.call_timestamp <= CAST(:date_to AS timestamptz))
              AND (CAST(:typology_key AS text) IS NULL OR r.typology_key = CAST(:typology_key AS text))
              AND (CAST(:agent_owner_id AS text) IS NULL OR r.hubspot_owner_id = CAST(:agent_owner_id AS text))
              {extra_sql}
            GROUP BY c.criterion_key
            ORDER BY avg_value DESC;
        """)
        rank_res = await db.execute(ranking_query, params)
        rank_rows = rank_res.fetchall()
        criteria_ranking = []
        for row in rank_rows:
            criteria_ranking.append(ServiceEvolutionCriteriaRankingItem(
                criterion_key=row[0],
                criterion_name=row[1],
                avg_value=float(row[2]) if row[2] is not None else None,
                total_applicable=row[3]
            ))

        # Apply specific criteria whitelist filter if requested
        if criteria:
            whitelist = [k.strip().lower() for k in criteria.split(",") if k.strip()]
            if whitelist:
                criteria_ranking = [item for item in criteria_ranking if item.criterion_key.lower() in whitelist]
                
        return ServiceEvolutionResponse(
            filters=filters,
            summary=summary,
            series=series_list,
            by_typology=by_typology,
            by_agent=by_agent,
            criteria_ranking=criteria_ranking,
        )
