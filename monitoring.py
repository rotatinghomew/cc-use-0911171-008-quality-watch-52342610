"""质量漂移监测：分层参照、样本量自适应检验与误报控制。

设计原则（对应 docs/monitoring_design.md）：

- 只在可比层内比较：层 = 产线 × 产地 × 等级 × 班次，参照仅取同层历史窗口，
  从不把新产地与不可比的老产地比较。
- 检验同时要求“统计显著性”和“最小实际偏移量”，并对小样本做收缩与证据门控，
  抽样量大不会更容易报警，抽样量不足给出 evidence_insufficient，而不是确认正常。
- 每个评估窗口内，对全部层×指标的候选信号做 Benjamini-Hochberg FDR 控制，
  明确误报控制范围。
- 输出携带完整依据：参照窗口、样本标识、基线与当前估计、标准差、效应量、
  p 值与收缩参数，复核人员看得到比较对象。

仅使用 Python 3.11 标准库。
"""
import math

from measurements import grouped
from records import fingerprint


def _normal_sf(z):
    """标准正态上尾概率 P(Z > z)。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))

DEFAULT_CONFIG = {
    # 参照资格门控：同层历史至少覆盖的天数、抽检重量、直径样本数
    "min_reference_days": 3,
    "min_reference_kg": 60.0,
    "min_reference_diameter": 2,
    # 事件确认：连续触发窗口数 / 多指标同时触发
    "persistence_windows": 2,
    # 最小实际偏移（达不到只记噪声，不产生信号）
    "min_damage_effect": 0.01,
    "min_diameter_effect_mm": 1.0,
    # 证据不足门控：当前窗口抽检重量低于该值不做判定
    "min_window_kg": 30.0,
    # FDR 控制水平（每个评估日内所有层×指标检验构成一个检验族）
    "fdr_q": 0.05,
}

METRICS = ("damage", "diameter")
NORMAL_STATUS = "normal"
SIGNAL_STATUS = "signal"
INSUFFICIENT_STATUS = "evidence_insufficient"
NO_BASELINE_STATUS = "no_comparable_baseline"


def configure(overrides=None):
    config = dict(DEFAULT_CONFIG)
    if overrides:
        unknown = set(overrides) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"未知监测配置: {sorted(unknown)}")
        config.update(overrides)
    return config


def stratum_key(row):
    return (row["line"], row["origin"], row["grade"], row.get("shift", "default"))


def _quarantined_windows(data, as_of):
    """被隔离的 (层, 窗口日)：属于尚未经 normal_batch 复核放行的事件证据。

    未确认异常绝不自动学成正常——这些窗口在参照构建时被排除；
    normal_batch 复核仅放行该结论作出时已存在的证据窗口，之后新增窗口仍隔离。
    """
    quarantined = set()
    for event in data.get("events", []):
        released = set()
        for review in event.get("reviews", []):
            if review.get("verdict") == "normal_batch":
                released.update(review.get("evidence_windows", []))
        for evidence in event.get("evidence", []):
            if evidence["window"] in released:
                continue
            quarantined.add((tuple(event["stratum"]), evidence["window"]))
    return quarantined


def eligible_reference(group_rows, as_of, config, data):
    """返回同层、窗口日在 as_of 之前、且未被隔离的参照分组行。"""
    quarantined = _quarantined_windows(data, as_of)
    rows = []
    for row in group_rows:
        if row["day"] >= as_of:
            continue
        key = stratum_key(row)
        if (key, row["day"]) in quarantined:
            continue
        rows.append(row)
    return rows


def _damage_stats(rows):
    checked = sum(row["checked_kg"] for row in rows)
    damaged = sum(row["damaged_kg"] for row in rows)
    rate = damaged / checked if checked else None
    return checked, damaged, rate


def _diameter_stats(rows):
    values = [row["mean_diameter_mm"] for row in rows if row.get("mean_diameter_mm") is not None]
    if not values:
        return None
    mean = sum(values) / len(values)
    if len(values) == 1:
        variance = 0.0
    else:
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return {"n": len(values), "mean": mean, "sd": variance ** 0.5}


def build_baseline(data, target, as_of, config=None):
    """为目标分组行构建同层参照基线；不满足资格时返回 None。"""
    config = configure(config)
    target_key = stratum_key(target)
    same_stratum = [row for row in grouped(data, as_of) if stratum_key(row) == target_key]
    reference_rows = eligible_reference(same_stratum, as_of, config, data)

    days = {row["day"] for row in reference_rows}
    checked, damaged, rate = _damage_stats(reference_rows)
    diameter = _diameter_stats(reference_rows)
    qualified = (
        len(days) >= config["min_reference_days"]
        and checked >= config["min_reference_kg"]
        and diameter is not None
        and diameter["n"] >= config["min_reference_diameter"]
    )
    return {
        "stratum": list(target_key),
        "qualified": qualified,
        "window_days": sorted(days),
        "sample_ids": sorted(sample_id for row in reference_rows for sample_id in row["samples"]),
        "reference_fingerprint": fingerprint(sorted(
            (sample_id, as_of) for row in reference_rows for sample_id in row["samples"]
        )),
        "damage": {"checked_kg": checked, "damaged_kg": damaged, "rate": rate},
        "diameter": diameter,
    }


def _shrink_damage(current, baseline):
    """Beta-二项收缩：小样本当前估计向同层基线靠拢，避免小分母噪声。

    以基线率构造 Beta 先验，先验强度取参照期总抽检重量；后验均值为收缩估计，
    收缩程度随当前窗口重量增加而下降。
    """
    n0 = baseline["damage"]["checked_kg"]
    p0 = baseline["damage"]["rate"]
    n = current["checked_kg"]
    p = current["damage_rate"]
    alpha0, beta0 = p0 * n0, (1.0 - p0) * n0
    posterior_mean = (alpha0 + current["damaged_kg"]) / (alpha0 + beta0 + n)
    variance = posterior_mean * (1.0 - posterior_mean) / (alpha0 + beta0 + n)
    return posterior_mean, variance ** 0.5, {"prior_alpha": alpha0, "prior_beta": beta0}


def _bh_adjust(candidates, q):
    """Benjamini-Hochberg FDR 调整。candidates 为含 p_value 的字典列表，原地写入 fdr_significant。"""
    ordered = sorted(candidates, key=lambda item: item["p_value"])
    total = len(ordered)
    thresholds = {}
    for rank, item in enumerate(ordered, start=1):
        thresholds[id(item)] = rank / total * q
    # 保证单调性：从最大秩向前，一旦不显著，更小的秩也不显著
    accepted_until = 0
    for rank, item in enumerate(ordered, start=1):
        if item["p_value"] <= thresholds[id(item)]:
            accepted_until = rank
    for rank, item in enumerate(ordered, start=1):
        item["fdr_significant"] = rank <= accepted_until


def evaluate_window(data, day, config=None):
    """评估单日全部层，返回每个层×指标的判定与完整依据。"""
    config = configure(config)
    targets = [row for row in grouped(data, day) if row["day"] == day]
    findings = []
    candidates = []
    for target in targets:
        baseline = build_baseline(data, target, day, config)
        evidence_ids = sorted(target["samples"])
        if not baseline["qualified"]:
            findings.append({
                "stratum": list(stratum_key(target)),
                "window": day,
                "status": NO_BASELINE_STATUS,
                "baseline": baseline,
                "sample_ids": evidence_ids,
                "reasons": ["同层参照未达资格门控，禁止跨产地/等级比较"],
            })
            continue
        if target["checked_kg"] < config["min_window_kg"]:
            findings.append({
                "stratum": list(stratum_key(target)),
                "window": day,
                "status": INSUFFICIENT_STATUS,
                "baseline": baseline,
                "sample_ids": evidence_ids,
                "reasons": [f"当前窗口抽检 {target['checked_kg']}kg 低于门控 {config['min_window_kg']}kg"],
            })
            continue

        # 指标一：破损率（单侧上尾，Beta 收缩 + 正态近似）
        shrunk, se, prior = _shrink_damage(target, baseline)
        p0 = baseline["damage"]["rate"]
        z_damage = (shrunk - p0) / se if se > 0 else 0.0
        p_damage = _normal_sf(z_damage)
        effect_damage = shrunk - p0
        damage_item = {
            "metric": "damage",
            "stratum": list(stratum_key(target)),
            "window": day,
            "status": SIGNAL_STATUS,
            "baseline_rate": p0,
            "observed_rate": target["damage_rate"],
            "shrunk_estimate": shrunk,
            "effect": effect_damage,
            "z_score": z_damage,
            "p_value": p_damage,
            "min_effect": config["min_damage_effect"],
            "shrink_prior": prior,
            "baseline": baseline,
            "sample_ids": evidence_ids,
            "fdr_significant": False,
        }
        candidates.append(damage_item)

        # 指标二：规格偏差（直径均值，双侧 z 检验）
        diameter_now = _diameter_stats([target])
        base_d = baseline["diameter"]
        if diameter_now is not None and base_d["sd"] > 0:
            se_d = base_d["sd"] / (diameter_now["n"] ** 0.5)
            z_diam = (diameter_now["mean"] - base_d["mean"]) / se_d
            p_diam = 2 * _normal_sf(abs(z_diam))
            effect_diam = diameter_now["mean"] - base_d["mean"]
            candidates.append({
                "metric": "diameter",
                "stratum": list(stratum_key(target)),
                "window": day,
                "status": SIGNAL_STATUS,
                "baseline_mean_mm": base_d["mean"],
                "observed_mean_mm": diameter_now["mean"],
                "baseline_sd_mm": base_d["sd"],
                "effect": effect_diam,
                "z_score": z_diam,
                "p_value": p_diam,
                "min_effect": config["min_diameter_effect_mm"],
                "baseline": baseline,
                "sample_ids": evidence_ids,
                "fdr_significant": False,
            })
        elif diameter_now is not None and base_d["sd"] == 0 and diameter_now["mean"] != base_d["mean"]:
            # 参照期恒定而当前取值改变：零方差下无法做正态检验，显式标记为结构变化，
            # 不进入 FDR 检验族（无可估计的离散度），交人工判断。
            findings.append({
                "metric": "diameter",
                "stratum": list(stratum_key(target)),
                "window": day,
                "status": "structural_change",
                "baseline_mean_mm": base_d["mean"],
                "observed_mean_mm": diameter_now["mean"],
                "baseline_sd_mm": 0.0,
                "effect": diameter_now["mean"] - base_d["mean"],
                "z_score": None,
                "p_value": None,
                "min_effect": config["min_diameter_effect_mm"],
                "baseline": baseline,
                "sample_ids": evidence_ids,
                "reasons": ["参照期规格恒定而当前取值改变，无法估计离散度，标记为结构变化待人工判断"],
            })

    _bh_adjust(candidates, config["fdr_q"])
    for item in candidates:
        effect_ok = abs(item["effect"]) >= item["min_effect"]
        item["status"] = SIGNAL_STATUS if (item["fdr_significant"] and effect_ok) else NORMAL_STATUS
        reasons = []
        if not item["fdr_significant"]:
            reasons.append("未通过 FDR 检验族控制")
        if not effect_ok:
            reasons.append(f"效应量 {abs(item['effect']):.4f} 低于最小实际偏移 {item['min_effect']}")
        item["reasons"] = reasons
        findings.append(item)
    return {
        "window": day,
        "fdr_q": config["fdr_q"],
        "test_family_size": len(candidates),
        "findings": findings,
    }


def signals(data, day, config=None):
    """单日通过全部门控的信号（供事件引擎消费）。"""
    return [item for item in evaluate_window(data, day, config)["findings"] if item["status"] == SIGNAL_STATUS]
