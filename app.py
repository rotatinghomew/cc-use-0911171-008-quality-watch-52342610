import argparse
import copy
import json
from pathlib import Path
from records import load, save


def inspect(data, as_of):
    from measurements import grouped
    from incidents import rule_alerts
    from monitoring import evaluate_window
    from events import replay_projection, explain_quarantine
    alerts = rule_alerts(data, as_of)
    window = evaluate_window(data, as_of)
    # 只读投影：在副本上按日重放事件台账，不修改输入数据
    projection_copy = copy.deepcopy(data)
    projection, _ = replay_projection(projection_copy, as_of)
    status_count = {}
    for finding in window["findings"]:
        status_count[finding["status"]] = status_count.get(finding["status"], 0) + 1
    return {
        "observed_groups": len(grouped(data, as_of)),
        "rule_alert_count": len(alerts),
        "alerts": alerts[:8],
        "drift_monitor": {
            "window": window["window"],
            "fdr_q": window["fdr_q"],
            "test_family_size": window["test_family_size"],
            "finding_status_count": status_count,
            "findings": window["findings"],
            "events": projection,
            "quarantined_reference_windows": explain_quarantine(projection_copy, as_of),
        },
    }



def main():
    parser = argparse.ArgumentParser(description="质量检验抽样核对、固定规则警报与人工复核。")
    parser.add_argument("action", choices=["demo", "inspect"])
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "fixtures/data.json")
    parser.add_argument("--as-of", default="2026-10-30")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect(load(args.data), args.as_of)
    if args.output:
        save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
