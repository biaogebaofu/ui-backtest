"""Reuse existing closed-candle entry definitions for independent hedge comparisons."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")
BASE_LABELS = {1: "情况一", 2: "情况二", 3: "情况三（量能充足）", 4: "情况四（不足时反向）"}


def strategy_id(definition):
    return hashlib.sha256(json.dumps(definition, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def plan_entries(selection, meta, compare_entries=False):
    """Enumerate selected rules, never the Cartesian product of timeframes."""
    random = {"id": "RANDOM", "label": "随机开仓基线", "timeframe": "", "code": 0,
              "field": "", "entry_mode": "UNLIMITED", "cases": (), "s3_gap": 0., "s3_timeframe": "1m"}
    if not compare_entries:
        return [random], []
    from extended_rules import ENTRY_RULES, FIFTH_SPECS, capabilities_for_timeframe, entry_unavailable_reason
    from fifth_policy import require_entry_allowed
    from indicator_combinations import entry_members, combination_label, choices_for_config
    from selection_config import 入场口径列表

    fields = [field for field in ("hist", "dif") if field in selection.get("开仓指标", ["hist"])]
    if not fields:
        raise ValueError("比较开仓条件时至少勾选一种MACD口径")
    modes = 入场口径列表(selection)
    constraints = selection.get("入场约束", {})
    entry_choices = selection.get("开仓条件", {})
    if selection.get("指标组合"):
        # Independent means separate timeframes, not discarding AND groups
        # that the user explicitly selected within each timeframe.
        choice_config = dict(selection, 开仓条件={tf: entry_choices.get(tf, []) for tf in TIMEFRAMES},
                             止损代码=selection.get("止损代码", ["OFF"]),
                             止盈方案编号=selection.get("止盈方案编号", [1]))
        entry_choices = choices_for_config(choice_config)["开仓"]
    descriptors, skipped, seen = [random], [], set()
    for tf in TIMEFRAMES:
        caps = capabilities_for_timeframe(meta.get("capabilities", {}), meta.get("capabilities_by_timeframe", {}), tf)
        for code in dict.fromkeys(map(int, entry_choices.get(tf, []))):
            if code == 0:
                continue
            label = ENTRY_RULES.get(code, (BASE_LABELS.get(code, str(code)),))[0]
            try:
                require_entry_allowed(code)
                members = entry_members(code)
                if code < 0:
                    label = combination_label("entry", code)
                reasons = [entry_unavailable_reason(member, caps, tf) for member in members]
                reason = "；".join(dict.fromkeys(reason for reason in reasons if reason))
                if reason:
                    raise ValueError(reason)
            except ValueError as exc:
                skipped.append({"周期": tf, "开仓代码": code, "开仓规则": label, "跳过原因": str(exc)})
                continue
            cases = [0] * 5
            cases[TIMEFRAMES.index(tf)] = code
            fifth = any(member in FIFTH_SPECS for member in members)
            for field in fields:
                for mode in modes:
                    if mode == "F5_EVENT" and not fifth:
                        skipped.append({"周期": tf, "开仓代码": code, "开仓规则": label,
                                        "跳过原因": "所选第五批专用事件口径不适用于该旧规则；未擅自替换入场口径"})
                        continue
                    definition = {"timeframe": tf, "code": code, "field": field,
                                  "entry_mode": "F5_EVENT" if fifth else mode, "cases": tuple(cases),
                                  "s3_gap": float(constraints.get("最小S3距离", .001)),
                                  "s3_timeframe": constraints.get("S3基线周期", "1m")}
                    identity = strategy_id(definition)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    descriptors.append(dict(definition, id=identity,
                                            label=f"{tf} {label} / {'MACD柱' if field == 'hist' else 'DIF线'} / {definition['entry_mode']}"))
    return descriptors, skipped


def fifth_tasks(descriptors):
    from extended_rules import FIFTH_SPECS
    from indicator_combinations import entry_members
    return sorted({(row["timeframe"], member) for row in descriptors if row["id"] != "RANDOM"
                   for member in entry_members(row["code"]) if member in FIFTH_SPECS})


class SignalAdapter:
    def __init__(self, feature_path):
        path = str(Path(feature_path).resolve())
        loaded = sys.modules.get("engine")
        if loaded is not None and Path(loaded.FEATURE_PATH).resolve() != Path(path):
            raise RuntimeError("同一个工作进程不能混用两组行情缓存，请启动新的回测工作进程")
        os.environ["BT_FEATURES"] = path
        import backtest_engine
        self.engine = backtest_engine

    def build(self, descriptor, warmup=200):
        engine = self.engine
        if descriptor["id"] == "RANDOM":
            signals = np.ones((engine.N, 2), dtype=np.bool_)
            ids = np.full(engine.N, -1, dtype=np.int64)
        else:
            cases = tuple(descriptor["cases"])
            _, _, ids, entries, _ = engine.build_field(
                descriptor["field"], (cases,), ("OFF",), descriptor["s3_gap"], descriptor["s3_timeframe"],
                direction="BOTH", session="ALL", materialize_stops=False,
                entry_mode=descriptor["entry_mode"], position_filter="OFF")
            ids = np.asarray(engine.entry_run_id_for_cases(cases, ids), dtype=np.int64)
            _, candidates, directions = entries[0]
            signals = np.zeros((engine.N, 2), dtype=np.bool_)
            signals[candidates[directions > 0], 0] = True
            signals[candidates[directions < 0], 1] = True
        signals[:warmup] = False
        signals.flags.writeable = False
        ids.flags.writeable = False
        return signals, ids
