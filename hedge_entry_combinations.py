"""Build the selected multi-timeframe entries without altering legacy hedge cases."""
from __future__ import annotations

import itertools

from hedge_signals import BASE_LABELS, TIMEFRAMES, plan_entries, strategy_id


def plan_request_entries(request, meta):
    selection = request.get("selection", {})
    compare = request["hedge"]["compare_entries"]
    mode = request.get("entry_plan", "independent")
    if mode == "independent" or not compare:
        return plan_entries(selection, meta, compare)
    if mode != "combined":
        raise ValueError("未知双向开仓组合方式，不能替换为其他口径")
    return combined_entries(selection, meta)


def combined_entries(selection, meta):
    from extended_rules import ENTRY_RULES, FIFTH_SPECS, capabilities_for_timeframe, entry_unavailable_reason
    from fifth_policy import require_entry_allowed
    from indicator_combinations import choices_for_config, entry_members, combination_label
    from selection_config import 入场口径列表

    random = plan_entries({}, {}, False)[0][0]
    fields = [field for field in ("hist", "dif") if field in selection.get("开仓指标", ["hist"])]
    if not fields:
        raise ValueError("比较开仓条件时至少勾选一种MACD口径")
    choices = choices_for_config(selection)["开仓"]
    pools, skipped, labels = [], [], {}
    for tf in TIMEFRAMES:
        caps = capabilities_for_timeframe(meta.get("capabilities", {}), meta.get("capabilities_by_timeframe", {}), tf)
        valid = []
        for code in choices[tf]:
            if not code:
                valid.append(0)
                continue
            label = combination_label("entry", code) if code < 0 else ENTRY_RULES.get(code, (BASE_LABELS.get(code, str(code)),))[0]
            labels[tf, code] = label
            try:
                require_entry_allowed(code)
                reasons = [entry_unavailable_reason(member, caps, tf) for member in entry_members(code)]
                reason = "；".join(dict.fromkeys(reason for reason in reasons if reason))
                if reason:
                    raise ValueError(reason)
            except ValueError as exc:
                skipped.append({"周期": tf, "开仓代码": code, "开仓规则": label, "跳过原因": str(exc)})
                continue
            valid.append(code)
        # Empty means this required timeframe cannot run; never silently disable it.
        pools.append(valid)
    constraints = selection.get("入场约束", {})
    descriptors, seen = [random], set()
    for cases in itertools.product(*pools):
        active = [(tf, code) for tf, code in zip(TIMEFRAMES, cases) if code]
        if not active:
            continue
        fifth = any(member in FIFTH_SPECS for _, code in active for member in entry_members(code))
        for field, mode in itertools.product(fields, 入场口径列表(selection)):
            if mode == "F5_EVENT" and not fifth:
                continue
            definition = dict(timeframe="+".join(tf for tf, _ in active),
                code=active[0][1] if len(active) == 1 else "+".join(f"{tf}:{code}" for tf, code in active),
                field=field, entry_mode="F5_EVENT" if fifth else mode, cases=tuple(cases),
                s3_gap=float(constraints.get("最小S3距离", .001)), s3_timeframe=constraints.get("S3基线周期", "1m"))
            identity = strategy_id(definition)
            if identity in seen:
                continue
            seen.add(identity)
            label = " ＋ ".join(f"{tf} {labels[tf, code]}" for tf, code in active)
            label += f" / {'MACD柱' if field == 'hist' else 'DIF线'} / {definition['entry_mode']}"
            descriptors.append(dict(definition, id=identity, label=label))
    return descriptors, skipped


def fifth_tasks(descriptors):
    from extended_rules import FIFTH_SPECS
    from indicator_combinations import entry_members
    return sorted({(tf, member) for row in descriptors if row["id"] != "RANDOM"
                   for tf, code in zip(TIMEFRAMES, row["cases"])
                   for member in entry_members(code) if member in FIFTH_SPECS})
