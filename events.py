"""漂移事件引擎：把分散信号聚合成可跟进事件，并守护人工判断与证据溯源。

关键约定：

- 一个事件 = 一个层（产线×产地×等级×班次）上的一次漂移过程；
  同层多指标、连续窗口的信号追加为事件证据，而不是每窗一单。
- 事件台账（data["events"]）只追加：状态变迁、复核、证据修订全部留痕。
- 未经 normal_batch 复核放行的证据窗口保持隔离，不会进入参照学习。
- 测量更正导致证据变化时，追加修订记录并标记 evidence_revised；
  复核结论与最新统计不一致时只标 statistical_conflict，绝不覆盖人工判断。
"""
from measurements import samples
from monitoring import (
    configure,
    signals,
    stratum_key,
)
from records import fingerprint

WATCHING = "watching"
CONFIRMED = "confirmed"
EVIDENCE_REVISED = "evidence_revised"
AUTO_CLOSED = "auto_closed"
CLOSED = "closed"

TERMINAL_VERDICTS = {"true_anomaly", "instrument", "normal_batch"}
AUTO_CLOSE_GAP = 3


def _present_days(data, as_of):
    return sorted({row["day"] for row in samples(data, as_of) if row["day"] <= as_of})


def _evidence_fingerprint(data, day, stratum):
    # 直接读取原始行（含 withdrawn 记录），撤回本身必须能改变指纹
    rows = [row for row in data["samples"]
            if row["day"] == day and stratum_key(row) == tuple(stratum)]
    material = sorted(
        (row["sample_id"], row["recorded_at"], row.get("revision", 0), bool(row.get("withdrawn", False)))
        for row in rows
    )
    active_ids = sorted(row["sample_id"] for row in samples(data, day)
                        if stratum_key(row) == tuple(stratum))
    # 撤回可能导致窗口无生效样本，补充窗口标识避免退化成空集合指纹
    return fingerprint([day, list(stratum), material]), active_ids


def _event_id(stratum, sequence):
    return fingerprint(["drift-event", list(stratum), sequence])


def _new_event(stratum, sequence, opened_at):
    return {
        "event_id": _event_id(stratum, sequence),
        "sequence": sequence,
        "stratum": list(stratum),
        "status": WATCHING,
        "opened_at": opened_at,
        "closed_version": None,
        "evidence": [],
        "reviews": [],
        "revisions": [],
        "history": [{"at": opened_at, "from": None, "to": WATCHING, "reason": "首次信号"}],
        "statistical_conflict": None,
    }


def _open_event_for(data, stratum, day):
    prior = [event for event in data.setdefault("events", []) if tuple(event["stratum"]) == tuple(stratum)]
    terminal = [event for event in prior if event["status"] == CLOSED]
    sequence = len(terminal) + 1
    event = _new_event(stratum, sequence, day)
    data["events"].append(event)
    return event


def _active_event(data, stratum):
    for event in data.get("events", []):
        if tuple(event["stratum"]) != tuple(stratum):
            continue
        if event["status"] == CLOSED:
            continue
        return event
    return None


def _is_confirmed(event, config):
    windows = [entry["window"] for entry in event["evidence"]]
    if len(windows) >= config["persistence_windows"]:
        ordered = sorted(windows)
        for index in range(len(ordered) - config["persistence_windows"] + 1):
            segment = ordered[index:index + config["persistence_windows"]]
            # 连续窗口按数据出现日的序号判定，容忍没有抽样的自然日
            if segment == sorted(set(segment)) and len(segment) == config["persistence_windows"]:
                return True
    return any(len(entry["metrics"]) >= 2 for entry in event["evidence"])


def _append_signal(event, day, day_signals, data):
    stratum = tuple(event["stratum"])
    fp, sample_ids = _evidence_fingerprint(data, day, stratum)
    matched = [item for item in day_signals if tuple(item["stratum"]) == stratum]
    entry = {
        "window": day,
        "metrics": sorted(item["metric"] for item in matched),
        "sample_ids": sample_ids,
        "evidence_fingerprint": fp,
        "reference_fingerprint": matched[0]["baseline"]["reference_fingerprint"],
        "findings": [{
            "metric": item["metric"],
            "effect": item["effect"],
            "z_score": item["z_score"],
            "p_value": item["p_value"],
            "baseline_window_count": len(item["baseline"]["window_days"]),
        } for item in matched],
    }
    event["evidence"].append(entry)


def _detect_evidence_changes(event, data):
    """测量更正/撤回后，重算既有窗口指纹并留痕；返回是否发生变化。"""
    changed = False
    for entry in event["evidence"]:
        current_fp, current_ids = _evidence_fingerprint(data, entry["window"], event["stratum"])
        if current_fp == entry["evidence_fingerprint"]:
            continue
        event["revisions"].append({
            "at": entry["window"],
            "window": entry["window"],
            "kind": "measurement_correction",
            "old_fingerprint": entry["evidence_fingerprint"],
            "new_fingerprint": current_fp,
            "withdrawn_or_changed_samples": sorted(set(entry["sample_ids"]) ^ set(current_ids))
                or sorted(set(current_ids)),
        })
        entry["evidence_fingerprint"] = current_fp
        entry["sample_ids"] = current_ids
        changed = True
    return changed


def _conflict_check(event, evidence_windows):
    """人工结论与最新统计不一致：只标记冲突，不覆盖复核。

    evidence_windows 为事件当前全部证据窗口（含历史信号），冲突基于复核放行范围
    与证据窗口的差异判断，而不是仅看当日是否有信号。
    """
    if not event["reviews"]:
        event["statistical_conflict"] = None
        return
    latest_review = event["reviews"][-1]
    released_windows = {
        window
        for review in event["reviews"]
        for window in review.get("evidence_windows", [])
    }
    unreleased_windows = sorted(set(evidence_windows) - released_windows)
    if latest_review["verdict"] == "normal_batch" and unreleased_windows:
        event["statistical_conflict"] = {
            "review_verdict": "normal_batch",
            "latest_statistics": "人工放行后同层仍有未放行的信号证据",
            "signal_windows": unreleased_windows,
            "action": "等待重新复核，人工结论保留；未放行窗口保持隔离",
        }
    elif latest_review["verdict"] in {"true_anomaly", "instrument"} and not evidence_windows:
        event["statistical_conflict"] = {
            "review_verdict": latest_review["verdict"],
            "latest_statistics": "当前统计已无信号（可能由测量更正驱动）",
            "action": "等待重新复核，人工结论保留",
        }
    else:
        event["statistical_conflict"] = None


def project_events(data, as_of, config=None, commit=True):
    """把截至 as_of 的信号投影到事件台账。

    commit=True 时把追加结果写回 data["events"]；否则只返回投影副本（供只读入口使用）。
    投影可对同一 as_of 幂等重放：证据按窗口去重，状态变迁只在实际变化时追加。
    """
    config = configure(config)
    if commit:
        store = data
    else:
        import copy
        store = copy.deepcopy(data)
        store.setdefault("events", [])

    days = _present_days(store, as_of)
    signals_by_day = {day: signals(store, day, config) for day in days}
    # 层集合同时覆盖“当前有信号的层”和“台账已有事件的层”：
    # 证据撤回后该层可能不再产生信号，但修订检测与自动关闭仍须执行。
    strata = sorted(
        {tuple(item["stratum"]) for day_signals in signals_by_day.values() for item in day_signals}
        | {tuple(event["stratum"]) for event in store.get("events", [])}
    )

    for stratum in strata:
        event = _active_event(store, stratum)
        signal_days = sorted(day for day in days
                             if any(tuple(item["stratum"]) == stratum for item in signals_by_day[day]))
        if event is None:
            same_stratum_events = [row for row in store.get("events", [])
                                   if tuple(row["stratum"]) == tuple(stratum)]
            if same_stratum_events and signal_days:
                # 该层存在已人工关闭的事件且信号重现：重开同一事件而非新建
                event = same_stratum_events[-1]
            elif signal_days:
                event = _open_event_for(store, stratum, signal_days[0])
            else:
                continue

        # 人工关闭后信号再次出现：先重开同一事件（保留全部人工记录），再追加证据。
        # 仅在当前确为人工关闭时触发，保证重复投影不会重复留痕。
        if event["status"] == CLOSED and signal_days:
            event["history"].append({"at": as_of, "from": CLOSED, "to": WATCHING,
                                     "reason": "人工关闭后同层再次出现信号，重开跟踪，人工结论保留"})
            event["status"] = WATCHING
            event["closed_version"] = None

        # 证据修订检测（测量更正/撤回）：对全部历史窗口留痕
        if _detect_evidence_changes(event, store):
            if event["status"] != EVIDENCE_REVISED:
                event["history"].append({"at": as_of, "from": event["status"],
                                         "to": EVIDENCE_REVISED, "reason": "证据测量被更正或撤回"})
                event["status"] = EVIDENCE_REVISED

        # 追加尚未登记的信号窗口（多指标、连续窗口都汇聚到同一事件）
        known_windows = {entry["window"] for entry in event["evidence"]}
        for day in (day for day in signal_days if day not in known_windows):
            _append_signal(event, day, signals_by_day[day], store)

        # 确认规则：连续窗口或多指标
        if event["status"] in {WATCHING, EVIDENCE_REVISED} and _is_confirmed(event, config):
            event["history"].append({"at": as_of, "from": event["status"],
                                     "to": CONFIRMED, "reason": "连续窗口或多指标达到确认规则"})
            event["status"] = CONFIRMED

        # 信号消失超过间隔自动关闭（不等于确认正常，证据继续隔离）
        if signal_days and event["status"] in {WATCHING, CONFIRMED, EVIDENCE_REVISED}:
            gap = days.index(days[-1]) - days.index(signal_days[-1])
            if gap >= AUTO_CLOSE_GAP:
                event["history"].append({
                    "at": as_of, "from": event["status"], "to": AUTO_CLOSED,
                    "reason": f"连续 {AUTO_CLOSE_GAP} 个有数据窗口无信号；非人工确认正常，证据保持隔离",
                })
                event["status"] = AUTO_CLOSED

        # 人工判断与最新统计是否冲突（关闭/自动关闭事件也要持续检查，绝不覆盖人工结论）
        _conflict_check(event, {entry["window"] for entry in event["evidence"]})

    return store.get("events", [])


def review_event(data, event_id, verdict, reviewed_at, note, reviewer=None):
    """人工复核事件。复核只追加，并驱动参照隔离/放行规则。"""
    if verdict not in TERMINAL_VERDICTS or not note.strip():
        raise ValueError("复核意见不完整")
    event = next((row for row in data.get("events", []) if row["event_id"] == event_id), None)
    if event is None:
        raise ValueError("不存在可复核事件")
    if event["status"] == CLOSED:
        raise ValueError("事件已关闭，需重新复核请开新事件")
    event["reviews"].append({
        "reviewed_at": reviewed_at, "verdict": verdict, "note": note,
        "reviewer": reviewer,
        "evidence_windows": [entry["window"] for entry in event["evidence"]],
        "evidence_fingerprints": [entry["evidence_fingerprint"] for entry in event["evidence"]],
    })
    event["history"].append({"at": reviewed_at, "from": event["status"], "to": CLOSED,
                             "reason": f"人工复核：{verdict}"})
    event["status"] = CLOSED
    event["closed_version"] = len(event["history"])
    event["statistical_conflict"] = None
    return event


def replay_projection(data, as_of, config=None):
    """按有数据的窗口日逐步投影，模拟真实日批处理。

    漂移隔离依赖前序窗口已经形成的事件台账：第 N 日评估时，第 N-1 日确认的信号
    必须已被隔离，才不会污染第 N 日参照。一次性投影无法表达这个因果顺序，
    因此监测视图统一通过本函数重放。返回 (事件台账, 每日信号字典)。
    """
    config = configure(config)
    days = _present_days(data, as_of)
    signals_by_day = {}
    for day in days:
        # 先基于截至当日已知的台账（含前序窗口隔离）计算当日信号，再投影成事件
        signals_by_day[day] = signals(data, day, config)
        project_events(data, day, config, commit=True)
    return data.get("events", []), signals_by_day


def explain_quarantine(data, as_of):
    """列出当前被排除在参照之外的窗口及原因，保证学习护栏可解释。"""
    result = []
    for event in data.get("events", []):
        released = set()
        for review in event.get("reviews", []):
            if review.get("verdict") == "normal_batch":
                released.update(review.get("evidence_windows", []))
        for entry in event["evidence"]:
            if entry["window"] in released:
                continue
            result.append({
                "stratum": event["stratum"], "window": entry["window"],
                "event_id": event["event_id"], "event_status": event["status"],
                "reason": "窗口未经 normal_batch 复核放行，禁止作为正常参照学习",
            })
    return result
