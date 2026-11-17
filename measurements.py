from records import latest, quantity


def samples(data, as_of):
    result = []
    for row in latest(data["samples"], "sample_id", as_of):
        if row["day"] > as_of:
            continue
        checked = quantity(row["checked_kg"])
        damaged = quantity(row["damaged_kg"])
        if not checked or damaged > checked:
            raise ValueError("抽检数量无效")
        result.append({**row, "damage_rate": damaged / checked})
    return result


def grouped(data, as_of):
    groups = {}
    for row in samples(data, as_of):
        key = (row["day"], row["line"], row["origin"], row["grade"], row.get("shift", "default"))
        group = groups.setdefault(key, {"checked_kg": 0, "damaged_kg": 0, "samples": [],
                                        "diameter_values": []})
        group["checked_kg"] += row["checked_kg"]
        group["damaged_kg"] += row["damaged_kg"]
        group["samples"].append(row["sample_id"])
        if row.get("mean_diameter_mm") is not None:
            group["diameter_values"].append(row["mean_diameter_mm"])
    result = []
    for key, row in sorted(groups.items()):
        values = row.pop("diameter_values")
        result.append({
            "day": key[0], "line": key[1], "origin": key[2], "grade": key[3], "shift": key[4],
            "mean_diameter_mm": sum(values) / len(values) if values else None,
            **row,
            "damage_rate": row["damaged_kg"] / row["checked_kg"],
        })
    return result
