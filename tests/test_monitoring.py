"""漂移监测与事件引擎测试：用合成数据构造独立场景，不修改原始 fixture。"""
import copy
import json
import unittest
from pathlib import Path

from events import (
    AUTO_CLOSED,
    CLOSED,
    CONFIRMED,
    EVIDENCE_REVISED,
    WATCHING,
    explain_quarantine,
    project_events,
    replay_projection,
    review_event,
)
from measurements import grouped
from monitoring import (
    INSUFFICIENT_STATUS,
    NO_BASELINE_STATUS,
    build_baseline,
    configure,
    evaluate_window,
    signals,
)
from records import load

FIXTURE = Path(__file__).parents[1] / "fixtures/data.json"
RULES = [{"grade": "A", "damage_limit": 0.025}, {"grade": "B", "damage_limit": 0.04}]


def make_sample(day, line, origin, grade, checked, damaged, diameter=38.0,
                shift="default", recorded_at=None, revision=0, withdrawn=False):
    sample_id = f"{day}-{line}-{origin}-{grade}-{shift}-{revision}-{recorded_at or day}"
    return {
        "sample_id": sample_id, "day": day, "line": line, "origin": origin,
        "grade": grade, "shift": shift, "checked_kg": checked, "damaged_kg": damaged,
        "mean_diameter_mm": diameter, "recorded_at": recorded_at or day,
        "revision": revision, "withdrawn": withdrawn,
    }


def make_data(sample_rows, reference_start="2026-11-01"):
    return {"samples": sample_rows, "reference_start": reference_start,
            "rules": copy.deepcopy(RULES), "reviews": [], "events": []}


def history_stratum(days, line="L1", origin="east", grade="A", rate=0.02, diameter=38.0, kg=40.0,
                    shift="default"):
    rows = []
    for index, day in enumerate(days):
        rows.append(make_sample(day, line, origin, grade, kg, round(kg * rate, 3), diameter, shift=shift))
    return rows


class ComparabilityTests(unittest.TestCase):
    def test_new_origin_never_compared_with_old_origin(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 10)]
        rows = history_stratum(days, origin="east")
        # 新产地 north 当天才首次出现，同层无任何历史
        rows.append(make_sample("2026-11-10", "L1", "north", "A", 40.0, 4.0, 38.0))
        data = make_data(rows)
        findings = evaluate_window(data, "2026-11-10")["findings"]
        target = next(item for item in findings if "north" in item["stratum"])
        self.assertEqual(target["status"], NO_BASELINE_STATUS)
        self.assertTrue(all("跨产地" in reason for reason in target["reasons"]))
        # 老产地在前一日仍只与自己的同层历史比较，绝不借用 north 等其他产地
        prior = evaluate_window(data, "2026-11-09")["findings"][0]
        self.assertEqual(prior["baseline"]["stratum"][1], "east")
        self.assertTrue(all("east" in sample_id for sample_id in prior["baseline"]["sample_ids"]))

    def test_shift_is_its_own_stratum(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 9)]
        rows = history_stratum(days, shift="day") + history_stratum(days, shift="night")
        rows.append(make_sample("2026-11-09", "L1", "east", "A", 40.0, 0.8, 38.0, shift="night"))
        data = make_data(rows)
        baseline_night = build_baseline(data, grouped(data, "2026-11-09")[-1], "2026-11-09")
        self.assertEqual(baseline_night["stratum"][3], "night")
        self.assertTrue(all(sample_id.endswith("night-0-2026-11-08") or "night" in sample_id
                            for sample_id in baseline_night["sample_ids"]))


class SampleSizeTests(unittest.TestCase):
    def _drift_data(self, window_kg, window_damaged):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", window_kg, window_damaged))
        return make_data(rows)

    def test_large_sample_does_not_alert_on_tiny_effect(self):
        # 效应远低于最小实际偏移：即使显著也不报警，且必须显式给出效应量门槛理由
        data = self._drift_data(window_kg=2000.0, window_damaged=50.0)  # 率 2.5%，效应 0.5 个百分点
        self.assertEqual(signals(data, "2026-11-11"), [])
        finding = evaluate_window(data, "2026-11-11")["findings"][0]
        self.assertEqual(finding["status"], "normal")
        self.assertTrue(any("效应量" in reason for reason in finding["reasons"]))

    def test_small_sample_is_insufficient_not_normal(self):
        data = self._drift_data(window_kg=10.0, window_damaged=1.0)  # 10% 但只有 10kg
        finding = evaluate_window(data, "2026-11-11")["findings"][0]
        self.assertEqual(finding["status"], INSUFFICIENT_STATUS)
        self.assertNotEqual(finding["status"], "normal")

    def test_real_drift_persists_through_shrinkage(self):
        # 足够窗口重量 + 远超门控的偏移，应产生信号，且依据完整
        data = self._drift_data(window_kg=120.0, window_damaged=9.6)  # 8%
        finding = signals(data, "2026-11-11")[0]
        self.assertEqual(finding["metric"], "damage")
        for key in ("baseline_rate", "shrunk_estimate", "effect", "z_score", "p_value",
                    "shrink_prior", "sample_ids", "baseline"):
            self.assertIn(key, finding)
        self.assertTrue(finding["baseline"]["window_days"])


class EventLifecycleTests(unittest.TestCase):
    def _multi_day_drift(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        # 连续三天真实偏移
        for day in ("2026-11-11", "2026-11-12", "2026-11-13"):
            rows.append(make_sample(day, "L1", "east", "A", 120.0, 9.6))
        return make_data(rows)

    def test_consecutive_windows_form_one_event_not_three(self):
        data = self._multi_day_drift()
        events, _ = replay_projection(data, "2026-11-13")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(len(event["evidence"]), 3)
        self.assertEqual(event["status"], CONFIRMED)
        # 事件 ID 稳定：重放结果一致
        again, _ = replay_projection(copy.deepcopy(self._multi_day_drift()), "2026-11-13")
        self.assertEqual(again[0]["event_id"], event["event_id"])

    def test_related_metrics_merge_into_same_event(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        # 直径有波动，保证参照 sd > 0
        rows = []
        for index, day in enumerate(days):
            diameter = 38.0 if index % 2 == 0 else 37.5
            rows.append(make_sample(day, "L1", "east", "A", 40.0, 0.8, diameter))
        # 同一天破损与规格同时偏移
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6, 33.0))
        data = make_data(rows)
        day_signals = signals(data, "2026-11-11")
        self.assertEqual({item["metric"] for item in day_signals}, {"damage", "diameter"})
        events = project_events(data, "2026-11-11")
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]["evidence"][0]["metrics"]), {"damage", "diameter"})
        # 多指标同日即可确认
        self.assertEqual(events[0]["status"], CONFIRMED)

    def test_auto_close_is_not_confirmed_normal_and_stays_quarantined(self):
        data = self._multi_day_drift()
        replay_projection(data, "2026-11-13")
        # 之后连续 3 个有数据窗口恢复正常
        for day in ("2026-11-14", "2026-11-15", "2026-11-16"):
            data["samples"].append(make_sample(day, "L1", "east", "A", 40.0, 0.8))
        events, _ = replay_projection(data, "2026-11-16")
        self.assertEqual(events[0]["status"], AUTO_CLOSED)
        quarantine = explain_quarantine(data, "2026-11-16")
        self.assertTrue({row["window"] for row in quarantine} >= {"2026-11-11", "2026-11-12", "2026-11-13"})
        # 被隔离窗口不进入参照
        baseline = build_baseline(data, grouped(data, "2026-11-16")[-1], "2026-11-16")
        self.assertFalse(set(baseline["window_days"]) & {"2026-11-11", "2026-11-12", "2026-11-13"})


class LearningGuardTests(unittest.TestCase):
    def test_unconfirmed_anomaly_is_never_learned_as_normal(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6))
        data = make_data(rows)
        project_events(data, "2026-11-11")
        # 无复核：11 日被隔离，基线只含 1-10 日
        baseline = build_baseline(data, grouped(data, "2026-11-12")[-1], "2026-11-12")
        self.assertNotIn("2026-11-11", baseline["window_days"])

    def test_normal_batch_review_releases_reference(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6))
        data = make_data(rows)
        events = project_events(data, "2026-11-11")
        review_event(data, events[0]["event_id"], "normal_batch", "2026-11-12", "确认换批，属正常结构变化")
        self.assertEqual(explain_quarantine(data, "2026-11-12"), [])
        baseline = build_baseline(data, grouped(data, "2026-11-12")[-1], "2026-11-12")
        self.assertIn("2026-11-11", baseline["window_days"])

    def test_instrument_verdict_keeps_window_quarantined(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6))
        data = make_data(rows)
        events = project_events(data, "2026-11-11")
        review_event(data, events[0]["event_id"], "instrument", "2026-11-12", "称重仪故障")
        quarantine = explain_quarantine(data, "2026-11-12")
        self.assertTrue(any(row["window"] == "2026-11-11" for row in quarantine))


class EvidenceAndConflictTests(unittest.TestCase):
    def test_measurement_correction_is_traced_and_event_marked_revised(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6))
        data = make_data(rows)
        events = project_events(data, "2026-11-11")
        original_fp = events[0]["evidence"][0]["evidence_fingerprint"]
        # 更正测量：同 sample_id 新修订，破损率回落正常
        data["samples"].append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 2.4,
                                           recorded_at="2026-11-12", revision=1))
        events = project_events(data, "2026-11-12")
        event = events[0]
        self.assertEqual(event["status"], EVIDENCE_REVISED)
        revision = event["revisions"][0]
        self.assertEqual(revision["old_fingerprint"], original_fp)
        self.assertNotEqual(revision["new_fingerprint"], original_fp)
        self.assertEqual(revision["kind"], "measurement_correction")
        # 新窗口不再产生信号，但事件没有被悄悄关闭
        self.assertEqual(signals(data, "2026-11-12"), [])

    def test_withdrawn_evidence_is_traced(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        drifting = make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6)
        rows.append(drifting)
        data = make_data(rows)
        events = project_events(data, "2026-11-11")
        data["samples"].append({**drifting, "recorded_at": "2026-11-12", "withdrawn": True})
        events = project_events(data, "2026-11-12")
        self.assertTrue(any(rev["kind"] == "measurement_correction" for rev in events[0]["revisions"]))

    def test_statistical_conflict_never_overwrites_human_verdict(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, rate=0.02, kg=40.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 120.0, 9.6))
        data = make_data(rows)
        events = project_events(data, "2026-11-11")
        review_event(data, events[0]["event_id"], "normal_batch", "2026-11-12", "人工判断换批正常")
        # 放行后出现比放行水平更强的新漂移（15%），构成“人工正常 vs 统计异常”的冲突
        for day in ("2026-11-12", "2026-11-13", "2026-11-14"):
            data["samples"].append(make_sample(day, "L1", "east", "A", 120.0, 18.0))
        replay_projection(data, "2026-11-14")
        reviewed = next(event for event in data["events"]
                        if any(review["verdict"] == "normal_batch" for review in event["reviews"]))
        self.assertEqual(reviewed["reviews"][-1]["verdict"], "normal_batch")
        self.assertIsNotNone(reviewed["statistical_conflict"])
        self.assertIn("等待重新复核", reviewed["statistical_conflict"]["action"])
        self.assertIn("2026-11-12", reviewed["statistical_conflict"]["signal_windows"])
        # 人工放行的 11 日窗口在参照中（人工优先），但 12 日起的新窗口未被放行、保持隔离
        baseline = build_baseline(data, grouped(data, "2026-11-14")[-1], "2026-11-14")
        self.assertIn("2026-11-11", baseline["window_days"])
        self.assertNotIn("2026-11-12", baseline["window_days"])


class FalsePositiveControlTests(unittest.TestCase):
    def test_fdr_family_scope_is_explicit_and_json_serializable(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = []
        for origin in ("east", "west"):
            for grade in ("A", "B"):
                rows.extend(history_stratum(days, origin=origin, grade=grade, rate=0.02))
        data = make_data(rows)
        window = evaluate_window(data, "2026-11-10")
        # 检验族 = 4 个层 × 1 个指标（直径 sd=0 不进族），范围显式输出
        self.assertEqual(window["test_family_size"], 4)
        self.assertEqual(window["fdr_q"], configure()["fdr_q"])
        # 全部输出可 JSON 序列化（含 NaN 防护）
        json.dumps(window, ensure_ascii=False, allow_nan=False)

    def test_structural_change_in_constant_spec_is_flagged_not_silent(self):
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = history_stratum(days, diameter=38.0)
        rows.append(make_sample("2026-11-11", "L1", "east", "A", 40.0, 0.8, diameter=33.0))
        data = make_data(rows)
        findings = evaluate_window(data, "2026-11-11")["findings"]
        structural = next(item for item in findings if item.get("metric") == "diameter")
        self.assertEqual(structural["status"], "structural_change")
        self.assertIsNone(structural["p_value"])


class CompositionChangeTests(unittest.TestCase):
    def test_mix_shift_between_strata_does_not_create_drift(self):
        # 产地构成每天变化，但每个层内部保持稳定：不应产生漂移信号
        days = [f"2026-11-{index:02d}" for index in range(1, 11)]
        rows = []
        for day_index, day in enumerate(days):
            # 偶数日 east 抽得多，奇数日 west 抽得多——构成变化
            east_kg = 80.0 if day_index % 2 == 0 else 20.0
            west_kg = 20.0 if day_index % 2 == 0 else 80.0
            rows.append(make_sample(day, "L1", "east", "A", east_kg, round(east_kg * 0.02, 3)))
            rows.append(make_sample(day, "L1", "west", "A", west_kg, round(west_kg * 0.03, 3)))
        data = make_data(rows)
        window = evaluate_window(data, "2026-11-10")
        # 构成变化下没有任何漂移信号；小抽样组按门控给出证据不足而不是误报
        self.assertTrue(all(item["status"] != "signal" for item in window["findings"]))
        self.assertIn("evidence_insufficient",
                      {item["status"] for item in window["findings"]})


class ReadOnlyContractTests(unittest.TestCase):
    def test_inspect_does_not_mutate_input(self):
        from app import inspect
        data = load(FIXTURE)
        before = copy.deepcopy(data)
        inspect(data, "2026-10-30")
        self.assertEqual(before, data)


if __name__ == "__main__":
    unittest.main()
