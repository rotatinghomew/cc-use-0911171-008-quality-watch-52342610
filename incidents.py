from measurements import grouped
from reference import describe, fixed_rule
from records import fingerprint


def rule_alerts(data, as_of):
    result = []
    reviews = {row["alert_id"]: row for row in data["reviews"] if row["reviewed_at"] <= as_of}
    for row in grouped(data, as_of):
        rule = fixed_rule(data, row["grade"])
        if row["damage_rate"] <= rule["damage_limit"]:
            continue
        identity = fingerprint([row["day"], row["line"], row["origin"], row["grade"], "fixed-damage"])
        review = reviews.get(identity)
        result.append({"alert_id": identity, **row, "reference": describe(data, row, as_of),
                       "rule_limit": rule["damage_limit"], "review": review})
    return result


def review(data, alert_id, verdict, reviewed_at, note):
    if verdict not in {"true_anomaly", "instrument", "normal_batch"} or not note.strip():
        raise ValueError("复核意见不完整")
    alerts = {row["alert_id"] for row in rule_alerts(data, reviewed_at)}
    if alert_id not in alerts:
        raise ValueError("不存在可复核警报")
    if any(row["alert_id"] == alert_id for row in data["reviews"]):
        raise ValueError("已有复核记录")
    row = {"alert_id": alert_id, "verdict": verdict, "reviewed_at": reviewed_at, "note": note}
    data["reviews"].append(row)
    return row
