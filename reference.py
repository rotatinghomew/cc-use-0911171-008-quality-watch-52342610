from measurements import samples


def comparable(data, target, as_of):
    rows = []
    for row in samples(data, as_of):
        if row["day"] >= target["day"] or row["day"] < data["reference_start"]:
            continue
        if row["origin"] == target["origin"] and row["grade"] == target["grade"]:
            rows.append(row)
    return rows


def describe(data, target, as_of):
    rows = comparable(data, target, as_of)
    denominator = sum(row["checked_kg"] for row in rows)
    return {"origin": target["origin"], "grade": target["grade"], "samples": len(rows),
            "checked_kg": denominator,
            "historical_damage_rate": sum(row["damaged_kg"] for row in rows) / denominator if denominator else None}


def fixed_rule(data, grade):
    rules = [row for row in data["rules"] if row["grade"] == grade]
    if len(rules) != 1:
        raise ValueError("等级阈值配置缺失或重复")
    return dict(rules[0])
