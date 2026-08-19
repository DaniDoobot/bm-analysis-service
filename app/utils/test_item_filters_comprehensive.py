"""
Comprehensive Test Suite for Item Filters (ERR-03 / ERR-04).
============================================================
Tests:
1. Numeric item filtering (ranges, operators, scaling).
2. Boolean item filtering (true/false, si/no, is_true/is_false).
3. Combinations of up to 3 filters.
4. Neutral 0-10 filters discarding.
5. Max 3 filters enforcement and 422 validation errors.
6. MassEvaluationService list_results and count_results SQL integration.
7. Resilience against null/empty items_json and result_json.
8. Filter options metadata generation.
"""
import os
import sys
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///item_filters_test.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import unittest
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.services.mass_evaluation_service import MassEvaluationService
from app.utils.item_score_filters import (
    parse_item_score_filters,
    parse_item_score_filters_detailed,
    build_item_filters_sql,
    filter_mass_results_by_items,
    extract_boolean_from_mass,
    get_evaluation_item_filter_options,
)


class TestItemFiltersComprehensive(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Setup clean test SQLite DB for asynchronous testing
        self.engine = create_async_engine("sqlite+aiosqlite:///item_filters_test.db", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()
        if os.path.exists("item_filters_test.db"):
            try:
                os.remove("item_filters_test.db")
            except Exception:
                pass

    def test_parse_numeric_operators(self):
        # 1. gte operator
        f_gte = parse_item_score_filters('[{"key": "empatia", "operator": "gte", "value": 7.5}]')
        self.assertEqual(len(f_gte), 1)
        self.assertEqual(f_gte[0]["key"], "empatia")
        self.assertEqual(f_gte[0]["min"], 7.5)
        self.assertEqual(f_gte[0]["max"], 10.0)

        # 2. lte operator
        f_lte = parse_item_score_filters('[{"key": "claridad", "operator": "lte", "value": 4.0}]')
        self.assertEqual(len(f_lte), 1)
        self.assertEqual(f_lte[0]["key"], "claridad")
        self.assertEqual(f_lte[0]["min"], 0.0)
        self.assertEqual(f_lte[0]["max"], 4.0)

        # 3. between operator
        f_between = parse_item_score_filters('[{"key": "procedimiento", "min": 5.0, "max": 8.0}]')
        self.assertEqual(len(f_between), 1)
        self.assertEqual(f_between[0]["min"], 5.0)
        self.assertEqual(f_between[0]["max"], 8.0)

    def test_parse_boolean_operators_and_formats(self):
        # 1. is_true operator
        f_is_true = parse_item_score_filters('[{"key": "cierre_cita", "operator": "is_true"}]')
        self.assertEqual(len(f_is_true), 1)
        self.assertEqual(f_is_true[0]["type"], "boolean")
        self.assertEqual(f_is_true[0]["expected_bool"], True)

        # 2. is_false operator
        f_is_false = parse_item_score_filters('[{"key": "cierre_cita", "operator": "is_false"}]')
        self.assertEqual(len(f_is_false), 1)
        self.assertEqual(f_is_false[0]["type"], "boolean")
        self.assertEqual(f_is_false[0]["expected_bool"], False)

        # 3. value: "si"
        f_si = parse_item_score_filters('[{"key": "pareja_conocedora", "value": "si"}]')
        self.assertEqual(len(f_si), 1)
        self.assertEqual(f_si[0]["expected_bool"], True)

        # 4. value: "no"
        f_no = parse_item_score_filters('[{"key": "pareja_conocedora", "value": "no"}]')
        self.assertEqual(len(f_no), 1)
        self.assertEqual(f_no[0]["expected_bool"], False)

        # 5. value: True
        f_bool_true = parse_item_score_filters('[{"key": "direccion_y_referencias", "value": true}]')
        self.assertEqual(len(f_bool_true), 1)
        self.assertEqual(f_bool_true[0]["expected_bool"], True)

    def test_parse_combination_up_to_3_filters(self):
        json_str = '[{"key": "empatia", "min": 7.0, "max": 10.0}, {"key": "cierre_cita", "value": true}, {"key": "claridad", "min": 6.0, "max": 9.0}]'
        f = parse_item_score_filters(json_str)
        self.assertEqual(len(f), 3)
        self.assertEqual(f[0]["key"], "empatia")
        self.assertEqual(f[1]["key"], "cierre_cita")
        self.assertEqual(f[1]["type"], "boolean")
        self.assertEqual(f[2]["key"], "claridad")

    def test_parse_neutral_0_10_filters_discarded(self):
        json_str = '[{"key": "empatia", "min": 0.0, "max": 10.0}, {"key": "cierre_cita", "value": true}]'
        info = parse_item_score_filters_detailed(json_str)
        self.assertEqual(info["raw_count"], 2)
        self.assertEqual(info["discarded_neutral_count"], 1)
        self.assertEqual(len(info["active_filters"]), 1)
        self.assertEqual(info["active_filters"][0]["key"], "cierre_cita")

    def test_parse_validation_errors(self):
        # > 3 items -> 422
        with self.assertRaises(HTTPException) as ctx:
            parse_item_score_filters('[{"key": "a"}, {"key": "b"}, {"key": "c"}, {"key": "d"}]')
        self.assertEqual(ctx.exception.status_code, 422)

        # min > max -> 422
        with self.assertRaises(HTTPException) as ctx:
            parse_item_score_filters('[{"key": "empatia", "min": 8.0, "max": 4.0}]')
        self.assertEqual(ctx.exception.status_code, 422)

        # invalid json -> 422
        with self.assertRaises(HTTPException) as ctx:
            parse_item_score_filters('not json')
        self.assertEqual(ctx.exception.status_code, 422)

    def test_extract_boolean_from_mass(self):
        # Case 1: inside items_json as boolean
        ij1 = [{"criterion_key": "cierre_cita", "boolean_value": True, "value": True}]
        self.assertEqual(extract_boolean_from_mass(None, ij1, "cierre_cita"), True)

        # Case 2: inside items_json as text "No"
        ij2 = [{"criterion_key": "cierre_cita", "boolean_value": None, "text_value": "No"}]
        self.assertEqual(extract_boolean_from_mass(None, ij2, "cierre_cita"), False)

        # Case 3: inside result_json
        rj3 = {"cierre_cita": "Si"}
        self.assertEqual(extract_boolean_from_mass(rj3, None, "cierre_cita"), True)

        # Case 4: None if key not found
        self.assertIsNone(extract_boolean_from_mass({}, [], "no_key"))

    def test_filter_mass_results_by_items_memory(self):
        r1 = SimpleNamespace(
            mass_analysis_id=1,
            result_json={"empatia": 8.0, "cierre_cita": "Si"},
            items_json=[
                {"criterion_key": "empatia", "numeric_value": 8.0},
                {"criterion_key": "cierre_cita", "boolean_value": True}
            ]
        )
        r2 = SimpleNamespace(
            mass_analysis_id=2,
            result_json={"empatia": 9.0, "cierre_cita": "No"},
            items_json=[
                {"criterion_key": "empatia", "numeric_value": 9.0},
                {"criterion_key": "cierre_cita", "boolean_value": False}
            ]
        )
        r3 = SimpleNamespace(
            mass_analysis_id=3,
            result_json={"empatia": 4.0, "cierre_cita": "Si"},
            items_json=[
                {"criterion_key": "empatia", "numeric_value": 4.0},
                {"criterion_key": "cierre_cita", "boolean_value": True}
            ]
        )

        calls = [r1, r2, r3]

        # Filter by cierre_cita = true -> r1, r3
        f_bool = parse_item_score_filters('[{"key": "cierre_cita", "value": true}]')
        res_bool = filter_mass_results_by_items(calls, f_bool)
        self.assertEqual({c.mass_analysis_id for c in res_bool}, {1, 3})

        # Filter by empatia >= 7 AND cierre_cita = true -> r1 only
        f_comb = parse_item_score_filters('[{"key": "empatia", "min": 7.0, "max": 10.0}, {"key": "cierre_cita", "value": true}]')
        res_comb = filter_mass_results_by_items(calls, f_comb)
        self.assertEqual({c.mass_analysis_id for c in res_comb}, {1})

    async def test_sql_mass_evaluation_service_item_filtering(self):
        now = datetime.now(timezone.utc)
        async with self.async_session() as db:
            # Create Job and Run
            job = MassEvaluationJob(job_id=1, job_name="Test Job", prompt_id=1, service_id=1)
            run = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed", calls_analyzed=3)
            db.add_all([job, run])
            await db.flush()

            # Create 3 MassEvaluationResults
            # Result 1: empatia=8.0, cierre_cita=True
            res1 = MassEvaluationResult(
                mass_analysis_id=101,
                job_id=1,
                run_id=1,
                call_id="call_101",
                service_id=1,
                status="completed",
                prompt_id=1,
                prompt_snapshot="{}",
                evaluacion_global=8.0,
                created_at=now
            )
            crit1_emp = MassEvaluationCriterionResult(
                id=1, mass_analysis_id=101, job_id=1, run_id=1, call_id="call_101",
                criterion_key="empatia", criterion_type="score_1_10", numeric_value=8.0
            )
            crit1_cie = MassEvaluationCriterionResult(
                id=2, mass_analysis_id=101, job_id=1, run_id=1, call_id="call_101",
                criterion_key="cierre_cita", criterion_type="boolean", boolean_value=True, text_value="Si"
            )

            # Result 2: empatia=9.0, cierre_cita=False
            res2 = MassEvaluationResult(
                mass_analysis_id=102,
                job_id=1,
                run_id=1,
                call_id="call_102",
                service_id=1,
                status="completed",
                prompt_id=1,
                prompt_snapshot="{}",
                evaluacion_global=9.0,
                created_at=now
            )
            crit2_emp = MassEvaluationCriterionResult(
                id=3, mass_analysis_id=102, job_id=1, run_id=1, call_id="call_102",
                criterion_key="empatia", criterion_type="score_1_10", numeric_value=9.0
            )
            crit2_cie = MassEvaluationCriterionResult(
                id=4, mass_analysis_id=102, job_id=1, run_id=1, call_id="call_102",
                criterion_key="cierre_cita", criterion_type="boolean", boolean_value=False, text_value="No"
            )

            # Result 3: empatia=4.0, cierre_cita=True
            res3 = MassEvaluationResult(
                mass_analysis_id=103,
                job_id=1,
                run_id=1,
                call_id="call_103",
                service_id=1,
                status="completed",
                prompt_id=1,
                prompt_snapshot="{}",
                evaluacion_global=4.0,
                created_at=now
            )
            crit3_emp = MassEvaluationCriterionResult(
                id=5, mass_analysis_id=103, job_id=1, run_id=1, call_id="call_103",
                criterion_key="empatia", criterion_type="score_1_10", numeric_value=4.0
            )
            crit3_cie = MassEvaluationCriterionResult(
                id=6, mass_analysis_id=103, job_id=1, run_id=1, call_id="call_103",
                criterion_key="cierre_cita", criterion_type="boolean", boolean_value=True, text_value="Si"
            )

            db.add_all([res1, crit1_emp, crit1_cie, res2, crit2_emp, crit2_cie, res3, crit3_emp, crit3_cie])
            await db.commit()

            # 1. Total without filters -> 3
            total_all = await MassEvaluationService.count_results(db, service_id=1)
            self.assertEqual(total_all, 3)

            # 2. Filter by cierre_cita = True -> count=2, list=[101, 103]
            f_bool = '[{"key": "cierre_cita", "value": true}]'
            cnt_bool = await MassEvaluationService.count_results(db, service_id=1, item_filters=f_bool)
            list_bool = await MassEvaluationService.list_results(db, service_id=1, item_filters=f_bool)
            self.assertEqual(cnt_bool, 2)
            self.assertEqual(len(list_bool), 2)
            self.assertEqual({r.mass_analysis_id for r in list_bool}, {101, 103})

            # 3. Filter by cierre_cita = False -> count=1, list=[102]
            f_false = '[{"key": "cierre_cita", "value": false}]'
            cnt_false = await MassEvaluationService.count_results(db, service_id=1, item_filters=f_false)
            list_false = await MassEvaluationService.list_results(db, service_id=1, item_filters=f_false)
            self.assertEqual(cnt_false, 1)
            self.assertEqual(len(list_false), 1)
            self.assertEqual(list_false[0].mass_analysis_id, 102)

            # 4. Filter by empatia >= 7.0 AND cierre_cita = True -> count=1, list=[101]
            f_comb = '[{"key": "empatia", "min": 7.0, "max": 10.0}, {"key": "cierre_cita", "value": true}]'
            cnt_comb = await MassEvaluationService.count_results(db, service_id=1, item_filters=f_comb)
            list_comb = await MassEvaluationService.list_results(db, service_id=1, item_filters=f_comb)
            self.assertEqual(cnt_comb, 1)
            self.assertEqual(len(list_comb), 1)
            self.assertEqual(list_comb[0].mass_analysis_id, 101)

            # 5. Neutral filter 0-10 -> count=3, list=3 (no restriction)
            f_neutral = '[{"key": "empatia", "min": 0.0, "max": 10.0}]'
            cnt_neutral = await MassEvaluationService.count_results(db, service_id=1, item_filters=f_neutral)
            self.assertEqual(cnt_neutral, 3)

    async def test_get_evaluation_item_filter_options_metadata(self):
        async with self.async_session() as db:
            crit_bool = MassEvaluationCriterionResult(
                id=10, mass_analysis_id=201, job_id=1, run_id=1, call_id="call_201",
                criterion_key="cierre_cita", criterion_name="Cierre de Cita", criterion_type="boolean",
                service_id=1
            )
            crit_num = MassEvaluationCriterionResult(
                id=11, mass_analysis_id=201, job_id=1, run_id=1, call_id="call_201",
                criterion_key="empatia", criterion_name="Empatía", criterion_type="score_1_10",
                service_id=1
            )
            db.add_all([crit_bool, crit_num])
            await db.commit()

            options = await get_evaluation_item_filter_options(db, service_ids=[1])
            self.assertTrue(len(options) >= 2)

            cie_opt = next((o for o in options if o["key"] == "cierre_cita"), None)
            self.assertIsNotNone(cie_opt)
            self.assertEqual(cie_opt["type"], "boolean")
            self.assertIn("options", cie_opt)
            self.assertEqual(len(cie_opt["options"]), 2)

            emp_opt = next((o for o in options if o["key"] == "empatia"), None)
            self.assertIsNotNone(emp_opt)
            self.assertEqual(emp_opt["type"], "score")
            self.assertEqual(emp_opt["min_score"], 0.0)
            self.assertEqual(emp_opt["max_score"], 10.0)


if __name__ == "__main__":
    unittest.main()
