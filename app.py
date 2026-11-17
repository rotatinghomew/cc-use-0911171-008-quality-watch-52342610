import argparse
import json
from pathlib import Path
from records import load, save


def inspect(data, as_of):
    from measurements import grouped
    from incidents import rule_alerts
    alerts = rule_alerts(data, as_of)
    return {"observed_groups": len(grouped(data, as_of)), "rule_alert_count": len(alerts), "alerts": alerts[:8]}



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
