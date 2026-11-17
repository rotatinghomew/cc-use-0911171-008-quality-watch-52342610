import copy
import json
import unittest
from pathlib import Path
from app import inspect
from records import load


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.data = copy.deepcopy(load(Path(__file__).parents[1] / "fixtures/data.json"))
        self.day = "2026-10-30"

    def test_end_to_end_inspection_is_json_and_repeatable(self):
        before = copy.deepcopy(self.data)
        first = inspect(self.data, self.day)
        self.assertEqual(first, inspect(self.data, self.day))
        self.assertEqual(before, self.data)
        self.assertTrue(json.dumps(first, ensure_ascii=False, allow_nan=False))

    def test_group_rate_has_real_denominator(self):
        from measurements import grouped
        row = grouped(self.data, self.day)[0]
        self.assertAlmostEqual(row["damage_rate"], row["damaged_kg"] / row["checked_kg"])

    def test_reference_is_preceding_and_comparable(self):
        from reference import comparable
        target = self.data["samples"][-1]
        rows = comparable(self.data, target, self.day)
        self.assertTrue(rows)
        self.assertTrue(all(row["day"] < target["day"] and row["origin"] == target["origin"] and row["grade"] == target["grade"] for row in rows))

    def test_alert_contains_evidence(self):
        from incidents import rule_alerts
        alerts = rule_alerts(self.data, self.day)
        self.assertTrue(alerts)
        self.assertTrue(all(row["samples"] and "reference" in row for row in alerts))

    def test_review_is_not_automatic_reference_update(self):
        from incidents import review, rule_alerts
        from reference import describe
        alert = rule_alerts(self.data, self.day)[0]
        before = describe(self.data, alert, self.day)
        review(self.data, alert["alert_id"], "instrument", self.day, "称重仪偏移")
        self.assertEqual(before, describe(self.data, alert, self.day))
        self.assertEqual(rule_alerts(self.data, self.day)[0]["review"]["verdict"], "instrument")

    def test_withdrawn_sample_is_removed(self):
        from measurements import samples
        before = len(samples(self.data, self.day))
        self.data["samples"].append({**self.data["samples"][0], "recorded_at": self.day, "withdrawn": True})
        self.assertEqual(len(samples(self.data, self.day)), before - 1)

    def test_bad_measurement_rejected(self):
        from measurements import samples
        self.data["samples"][0]["damaged_kg"] = 1000
        with self.assertRaises(ValueError):
            samples(self.data, self.day)
