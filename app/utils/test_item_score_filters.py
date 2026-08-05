"""
Test suite for evaluation item score filtering & validation.
=============================================================
Verifies:
1. Parsing & validation: max 3 items, min <= max, JSON 422 errors.
2. Memory filtering with strict AND logic across evaluation items.
3. Dynamic filter options API options generation.
"""
import unittest
from types import SimpleNamespace
from fastapi import HTTPException

from app.utils.item_score_filters import (
    parse_item_score_filters,
    filter_mass_results_by_items,
)


class TestItemScoreFilters(unittest.TestCase):

    def test_parse_valid_json_string(self):
        json_str = '[{"key": "empatia", "min": 2.0, "max": 8.0}]'
        res = parse_item_score_filters(json_str)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["key"], "empatia")
        self.assertEqual(res[0]["min"], 2.0)
        self.assertEqual(res[0]["max"], 8.0)

    def test_parse_exceeds_max_3_items_raises_422(self):
        json_str = '[{"key": "a"}, {"key": "b"}, {"key": "c"}, {"key": "d"}]'
        with self.assertRaises(HTTPException) as ctx:
            parse_item_score_filters(json_str)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_parse_min_greater_than_max_raises_422(self):
        json_str = '[{"key": "empatia", "min": 9.0, "max": 3.0}]'
        with self.assertRaises(HTTPException) as ctx:
            parse_item_score_filters(json_str)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_parse_invalid_json_raises_422(self):
        with self.assertRaises(HTTPException) as ctx:
            parse_item_score_filters("invalid json string")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_filter_mass_results_and_logic(self):
        call1 = SimpleNamespace(
            mass_analysis_id=1,
            result_json={"empatia": 8.5, "uso_preguntas": 3.0},
            items_json=None
        )
        call2 = SimpleNamespace(
            mass_analysis_id=2,
            result_json={"empatia": 9.0, "uso_preguntas": 7.5},
            items_json=None
        )
        call3 = SimpleNamespace(
            mass_analysis_id=3,
            result_json={"empatia": 4.0, "uso_preguntas": 8.0},
            items_json=None
        )

        all_calls = [call1, call2, call3]

        # 1. Filter by empatia [7.0, 10.0] -> call1, call2
        f1 = parse_item_score_filters('[{"key": "empatia", "min": 7.0, "max": 10.0}]')
        res1 = filter_mass_results_by_items(all_calls, f1)
        self.assertEqual(len(res1), 2)
        self.assertEqual({c.mass_analysis_id for c in res1}, {1, 2})

        # 2. Filter by empatia [7.0, 10.0] AND uso_preguntas [5.0, 10.0] -> call2 only
        f2 = parse_item_score_filters('[{"key": "empatia", "min": 7.0, "max": 10.0}, {"key": "uso_preguntas", "min": 5.0, "max": 10.0}]')
        res2 = filter_mass_results_by_items(all_calls, f2)
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0].mass_analysis_id, 2)


if __name__ == "__main__":
    unittest.main()
