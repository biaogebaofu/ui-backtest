"""Finite, auditable server campaign; never an exhaustive Cartesian search.

The validation window is not inspected during selection. Historical data may
already have been viewed by the user: this is a reserved chronological check,
not a claim of genuinely unseen market evidence or statistical significance.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import math
from statistics import median

from account_statistics import account_row
from entry_position import POSITION_FILTERS
from execution_settings import cost_modes
from extended_rules import (ENTRY_RULES, ENTRY_RULE_REQUIREMENTS, FIFTH_SPECS,
                            FOURTH_SPECS, ONE_MINUTE_CASE_CODES, PLACEBO_CODES,
                            entry_supported_timeframes)
from fifth_policy import fifth_retirement_reason
from selection_config import (全选配置, 规范化配置, 入场触发口径选项,
                              成交价格口径选项)
from strategy_space import (仓位列表, 方向列表, 会话列表, 强制时间止损档,
                            生成止损组合, 生成止盈方案, 生成固定止损档,
                            生成叠加止盈档)

VERSION = "family-campaign-20260922-v1"
PHASES = ("coarse", "bucket", "refine", "validation")
TERMINAL = {"done", "completed", "failed", "skipped", "deferred_budget"}
TF_COLUMNS = {"4h": "4小时条件代码", "1h": "1小时条件代码",
              "15m": "15分钟条件代码", "5m": "5分钟条件代码", "1m": "1分钟条件代码"}
SINGLE_KEYS = ("开仓指标", "开仓位置过滤", "止损代码", "固定止损代码", "叠加止盈代码",
               "开仓方向", "交易会话", "强制时间止损分钟", "止盈方案编号", "止盈后等待分钟", "仓位倍数")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()[:20]


def _family(code):
    if code in FIFTH_SPECS:
        return "F5:" + FIFTH_SPECS[code]["family"]
    if code in FOURTH_SPECS:
        return "F4:" + FOURTH_SPECS[code]["family"]
    if code < 5:
        return "original:" + str(code)
    return ("placebo:" if code in PLACEBO_CODES else "rule:") + ENTRY_RULES[code][1]


@lru_cache(maxsize=1)
def _catalog():
    entries = []
    for code in ONE_MINUTE_CASE_CODES:
        if code == 0 or fifth_retirement_reason(code):
            continue
        entries.append({"code": code, "family": _family(code),
                        "name": ENTRY_RULES.get(code, ("情况" + str(code),))[0],
                        "timeframes": list(entry_supported_timeframes(code)),
                        "requires": sorted(ENTRY_RULE_REQUIREMENTS.get(code, {"ohlcv"})),
                        "placebo": code in PLACEBO_CODES})
    stops = [{"code": code, "components": list(atoms), "name": label}
             for code, atoms, label in 生成止损组合()]
    tps = [row.字典() for row in 生成止盈方案()]
    return {"version": VERSION, "entries": entries, "take_profit": tps,
            "stops": stops, "fixed_stops": [list(x) for x in 生成固定止损档()],
            "overlay_take_profit": [list(x) for x in 生成叠加止盈档()],
            "position_filters": list(POSITION_FILTERS), "sizes": list(仓位列表),
            "entry_modes": dict(入场触发口径选项), "cost_modes": ["FEE", "SLIPPAGE"],
            "fill_modes": dict(成交价格口径选项), "directions": list(方向列表),
            "sessions": list(会话列表), "hard_timeouts": list(强制时间止损档),
            "cooldowns": list(range(31)),
            "coverage_contract": {
                "kind": "staged_family_and_one_factor_coverage",
                "not_claimed": ["全部笛卡尔积", "全部指标任意子集", "实盘Maker成交复现", "统计显著盈利"],
                "research": "追势SMA ATR过滤仅为候选；原始订单流/盘口缺失条件不由OHLCV冒充",
                "validation": "冻结时间验证；历史可能已看过，不冒充真正未来样本外",
                "promotion": "仅收线确认口径晋级；理论触碰价作为独立敏感性对照，不用较乐观口径选赢家",
                "quantity_cap": "各杠杆仍受种子配置ETH最大数量约束；名义100倍不代表实际始终开到100倍",
                "fifth_policy": "全部39项保留；不可用/未运行分别登记，不伪造零信号完成"}}


def make_catalog():
    """A JSON-serializable immutable-definition snapshot for the campaign."""
    return deepcopy(_catalog())


def _date(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # UI worker's dates are explicit UTC; never inherit the server timezone.
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _stamp(value):
    return value.isoformat(timespec="seconds")


def _windows(settings):
    start, end = _date(settings["start"]), _date(settings["end"])
    vs, ve = _date(settings["validation_start"]), _date(settings["validation_end"])
    gap = timedelta(hours=float(settings.get("purge_gap_hours", 24)))
    if not start < end <= vs < ve or gap < timedelta(0):
        raise ValueError("训练/验证时间须严格按先后排列，end不可超过validation_start")
    midpoint = start + (end - start) / 2
    # A gap also separates the last tuning observation from validation.
    if midpoint - gap <= start or end - gap <= midpoint:
        raise ValueError("两个训练窗口须长于隔离间隔；请使用更长数据或显式调整purge_gap_hours")
    return [("train_a", _stamp(start), _stamp(midpoint - gap)),
            ("train_b", _stamp(midpoint), _stamp(end - gap)),
            ("validation", _stamp(vs), _stamp(ve))]


def _base(settings):
    supplied = settings.get("seed_selection") or {}
    cfg = 全选配置()
    cfg.update(deepcopy(supplied))
    cfg.update({"开仓指标": ["hist", "dif"],
                "开仓条件": {tf: [0] for tf in TF_COLUMNS},
                "开仓位置过滤": ["OFF"], "止损代码": ["OFF"],
                "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
                "开仓方向": ["BOTH"], "交易会话": ["ALL"],
                "强制时间止损分钟": [180], "止盈方案编号": _coarse_tp_ids(),
                "止盈后等待分钟": [0], "仓位倍数": [1.0],
                "入场触发口径": "MACD_CYCLE", "成交价格口径": "CLOSE_CONFIRMED",
                "成本模式": ["SLIPPAGE", "FEE"]})
    cfg.pop("指标组合", None)
    cfg["资金约束"] = {**全选配置()["资金约束"], "初始资金USDC": 20000.0,
                       **supplied.get("资金约束", {})}
    cfg["手续费"] = {**全选配置()["手续费"], **supplied.get("手续费", {})}
    if not (float(cfg["手续费"]["开仓费率"]) > 0 and float(cfg["手续费"]["平仓费率"]) > 0):
        raise ValueError("研究基准必须显式使用非零开、平手续费，不能默用零费率")
    if float(cfg["手续费"].get("返佣比例", 0)) >= 1:
        raise ValueError("研究基准净手续费不能被100%返佣归零")
    cfg["候选筛选"] = {**cfg["候选筛选"], "启用": False, "自动导出": False, "导出旧排行": False}
    return cfg


def _coarse_tp_ids():
    # Prespecified representatives; no selection from observed returns.
    specs = [("固定比例止盈-周末相同", "", "", .004, 0.),
             ("MACD反转止盈", "1m", "hist", 1., 0.),
             ("ATR倍数止盈", "5m", "ATR14开仓冻结", 1.5, 0.),
             ("时间止盈", "", "持仓分钟", 60., 0.),
             ("保本移动止盈", "", "收盘启动次根生效", .004, .0005),
             ("移动止盈", "", "最高浮盈回吐比例", .004, .15)]
    rows = _catalog()["take_profit"]
    return [next(row["编号"] for row in rows
                 if (row["类别"], row["周期组合"], row["指标"], row["参数一"], row["参数二"]) == spec)
            for spec in specs]


def _representatives(values, count=3):
    values = list(values)
    if len(values) <= count:
        return values
    return [values[i] for i in sorted({round(i * (len(values) - 1) / (count - 1))
                                      for i in range(count)})] if count > 1 else [values[len(values) // 2]]


def _groups():
    groups = defaultdict(list)
    for item in _catalog()["entries"]:
        groups[item["family"]].append(item)
    return dict(sorted(groups.items()))


def _rows(cfg):
    return math.prod([len(cfg[k]) for k in ("开仓指标", "开仓位置过滤", "止损代码", "固定止损代码",
                      "叠加止盈代码", "开仓方向", "交易会话", "强制时间止损分钟", "止盈方案编号",
                      "止盈后等待分钟", "仓位倍数")]) * math.prod(len(x) for x in cfg["开仓条件"].values()) * len(cost_modes(cfg)) * (len(cfg["入场触发口径"]) if isinstance(cfg["入场触发口径"], list) else 1)


def _job(settings, phase, cfg, window, tags):
    requested = cfg
    cfg = 规范化配置(cfg)
    for key in SINGLE_KEYS:
        if set(requested[key]) != set(cfg[key]):
            raise ValueError(f"计划中的{key}含无效或被剔除选项，不允许静默缩小覆盖")
    if any(set(requested["开仓条件"][tf]) != set(cfg["开仓条件"][tf]) for tf in TF_COLUMNS):
        raise ValueError("开仓条件规范化删除了计划选项，不允许静默缩小覆盖")
    count = _rows(cfg)
    if count > int(settings.get("max_rows_per_job", 512)):
        raise ValueError(f"单任务{count}种账户结果超过上限，不允许隐式截断")
    tags = {**tags, "window": window[0], "result_count": count}
    payload = {"phase": phase, "selection": cfg, "start": window[1], "end": window[2], "tags": tags}
    return {"id": _hash({"version": VERSION, **payload}), **payload}


def _pair(settings, phase, cfg, tags):
    return [_job(settings, phase, cfg, win, tags) for win in _windows(settings)[:2]]


def _coarse(settings):
    base, jobs = _base(settings), []
    for family, items in _groups().items():
        # Same family is tested at multiple parameter locations, not one arbitrary winner.
        representatives = items if family.startswith("F5:") else _representatives(items)
        by_tf = defaultdict(list)
        for item in representatives:
            native = "1m" if "1m" in item["timeframes"] else item["timeframes"][0]
            by_tf[native].append(item["code"])
        for tf, codes in by_tf.items():
            chunk = int(settings.get("max_rows_per_job", 512)) // 72
            if chunk < 1:
                raise ValueError("初筛六类止盈代表要求max_rows_per_job至少72")
            for offset in range(0, len(codes), chunk):
                cfg = deepcopy(base)
                cfg["开仓条件"][tf] = codes[offset:offset + chunk]
                cfg["止损代码"] = ["OFF", "OFF+S9_1m", "ATR150_5m"]
                jobs += _pair(settings, "coarse", cfg, {"family": family, "role": "placebo" if all(x["placebo"] for x in items) else "family_screen", "axis": "entry", "exit_representatives": "固定0.4%/1mMACD反转1根/5mATR1.5/60分钟/0.4%启动锁0.05%/0.4%启动回吐15%"})
    return jobs


def initial_jobs(settings):
    jobs = _coarse(settings)
    maximum = int(settings.get("max_jobs", 2000))
    if len(jobs) > maximum:
        raise ValueError(f"max_jobs={maximum}容不下{len(jobs)}个初筛任务；不允许默默删除尾部族")
    return jobs


def _float(value):
    value = float(str(value).replace(",", ""))
    if not math.isfinite(value):
        raise ValueError("非有限指标")
    return value


def _size(value):
    value = str(value)
    if "/" in value:
        numerator, denominator = value.split("/")
        divisor = _float(denominator)
        if divisor <= 0:
            raise ValueError("仓位分母必须为正数")
        return _float(numerator) / divisor
    return _float(value.removesuffix("x"))


def _row_candidates(job):
    """Expand the native CSV's per-leverage arrays without shared-count mistakes."""
    for row in job.get("results", []):
        cfg = deepcopy(job["selection"])
        try:
            for tf, column in TF_COLUMNS.items():
                cfg["开仓条件"][tf] = [int(row[column])]
            cfg["开仓指标"] = ["hist" if str(row["开仓MACD代码"]) == "0" else "dif"]
            for key, column, cast in (("止盈方案编号", "止盈方案编号", int),
                                     ("止盈后等待分钟", "止盈后等待分钟", int),
                                     ("强制时间止损分钟", "强制时间止损（分钟）", int),
                                     ("止损代码", "止损代码", str), ("固定止损代码", "固定止损代码", str),
                                     ("叠加止盈代码", "叠加止盈代码", str), ("开仓方向", "开仓方向", str),
                                     ("交易会话", "交易会话", str), ("开仓位置过滤", "开仓位置过滤代码", str)):
                cfg[key] = [cast(row[column])]
            cfg["入场触发口径"] = row["入场触发口径"]
            cfg["成交价格口径"] = row["成交价格口径"]
            cost = row["成本模式"]
            if cost not in ("FEE", "SLIPPAGE"):
                cost = {"手续费": "FEE", "成交偏移": "SLIPPAGE"}[cost]
            sizes = row["所选仓位顺序"].split(";")
            returns = row["所选仓位累计收益率（%）"].split(";")
            drawdowns = row["所选仓位最大回撤（%）"].split(";")
            liquidations = row["所选仓位全仓强平次数（次）"].split(";")
            stops = row["所选仓位资金性停机标记（0否1是）"].split(";")
            if len({len(x) for x in (sizes, returns, drawdowns, liquidations, stops)}) != 1:
                continue
            for i, size in enumerate(sizes):
                scoped = deepcopy(cfg)
                scoped["仓位倍数"] = [_size(size)]
                scoped["成本模式"] = ["SLIPPAGE", "FEE"]
                metrics = account_row(row, i)
                active = [code for values in scoped["开仓条件"].values() for code in values if code]
                if any(code in PLACEBO_CODES for code in active):
                    continue
                family = job["tags"].get("family", _family(active[0]))
                yield _hash(scoped), scoped, family, cost, {
                    "trades": _float(metrics["交易次数（单）"]), "return": _float(returns[i]),
                    "drawdown": _float(drawdowns[i]), "liquidations": _float(liquidations[i]),
                    "stopped": _float(stops[i])}
        except (KeyError, ValueError, IndexError, TypeError):
            # Bad/missing metrics cannot create a promotable winner.
            continue


def _dimensions(cfg):
    return (tuple(cfg["开仓条件"][tf][0] for tf in TF_COLUMNS),
            tuple(cfg[key][0] for key in SINGLE_KEYS), cfg["入场触发口径"], cfg["成交价格口径"])


def _materialize(template, dimensions):
    cfg = deepcopy(template)
    cases, values, mode, fill = dimensions
    cfg["开仓条件"] = {tf: [code] for tf, code in zip(TF_COLUMNS, cases)}
    cfg.update({key: [value] for key, value in zip(SINGLE_KEYS, values)})
    cfg.update({"入场触发口径": mode, "成交价格口径": fill, "成本模式": ["SLIPPAGE", "FEE"]})
    return cfg


def ranked_candidates(settings, completed_jobs, phases=None, audit=None):
    """Require the same configuration in BOTH cost branches and training windows."""
    observations = {}
    for job in completed_jobs:
        if job.get("state") not in ("done", "completed") or job["phase"] == "validation":
            continue
        if phases and job["phase"] not in phases:
            continue
        window = job["tags"].get("window")
        if window not in ("train_a", "train_b"):
            continue
        for key, cfg, family, cost, metrics in _row_candidates(job):
            if cfg["成交价格口径"] != "CLOSE_CONFIRMED":
                continue
            # Keep shared job constants once, rather than copying a full account
            # configuration for every candidate in the long exit grids.
            item = observations.setdefault(key, {"template": job["selection"], "dimensions": _dimensions(cfg),
                                                  "family": family, "observations": {}})
            item["observations"][(window, cost)] = metrics
    ranked = []
    counts = defaultdict(int)
    expected = {(w, c) for w in ("train_a", "train_b") for c in ("FEE", "SLIPPAGE")}
    for key, item in observations.items():
        obs = item["observations"]
        if set(obs) != expected:
            counts["incomplete_window_or_cost_pair"] += 1
            continue
        values = list(obs.values())
        reasons = []
        if any(x["trades"] < int(settings.get("minimum_trades", 20)) for x in values):
            reasons.append("insufficient_trades")
        if any(x["return"] <= 0 for x in values):
            reasons.append("nonpositive_return_in_at_least_one_window_or_cost")
        if any(x["liquidations"] or x["stopped"] or x["drawdown"] < 0 for x in values):
            reasons.append("liquidation_stoppage_or_invalid_drawdown")
        if reasons:
            for reason in reasons:
                counts[reason] += 1
            continue
        scores = [x["return"] / max(x["drawdown"], .02) for x in values]
        score = .5 * min(scores) + .5 * median(scores)
        ranked.append({"id": key, "selection": _materialize(item["template"], item["dimensions"]), "family": item["family"],
                       "score": score, "worst_return": min(x["return"] for x in values),
                       "max_drawdown": max(x["drawdown"] for x in values),
                       "training_observations": [{"window": w, "cost_mode": c, **obs[(w, c)]}
                                                 for w, c in sorted(obs)]})
    if audit is not None:
        audit.update({"observed_candidate_count": len(observations), "eligible_candidate_count": len(ranked),
                      "exclusion_counts_overlapping": dict(counts)})
    return sorted(ranked, key=lambda x: (-x["score"], x["id"]))


def _winning_families(settings, ranked):
    scores = defaultdict(list)
    for item in ranked:
        scores[item["family"]].append(item["score"])
    # Family median, not its single most profitable configuration.
    return sorted(scores, key=lambda f: (-median(scores[f]), f))[:int(settings.get("top_families", 8))]


def _bucket(settings, completed):
    winners = set(_winning_families(settings, ranked_candidates(settings, completed, {"coarse"})))
    base, jobs = _base(settings), []
    tp_by_family = defaultdict(list)
    for row in _catalog()["take_profit"]:
        tp_by_family[row["类别"]].append(row["编号"])
    rescue_exits = [_representatives(v, 1)[0] for _, v in sorted(tp_by_family.items())]
    for i, (family, items) in enumerate(_groups().items()):
        if all(x["placebo"] for x in items):
            continue
        if family in winners:
            groups = defaultdict(list)
            for item in items:
                for tf in item["timeframes"]:
                    groups[tf].append(item["code"])
            role = "expanded_family"
        else:
            # Revisit every losing family with an alternate scale and exit family.
            item = items[len(items) // 2]
            tf = item["timeframes"][min(1, len(item["timeframes"]) - 1)]
            groups = {tf: [item["code"]]}
            role = "rescue_representative"
        for tf, codes in groups.items():
            for offset in range(0, len(codes), 8):
                cfg = deepcopy(base)
                cfg["开仓条件"][tf] = codes[offset:offset + 8]
                cfg["止盈方案编号"] = list(dict.fromkeys([5, rescue_exits[i % len(rescue_exits)]]))
                cfg["止损代码"] = ["OFF", "ATR150_5m"]
                jobs += _pair(settings, "bucket", cfg, {"family": family, "role": role, "axis": "entry"})
    return jobs


def _anchors(settings, ranked, count):
    families = _winning_families(settings, ranked)
    result = []
    for family in families:
        result.append(next(x for x in ranked if x["family"] == family))
    return result[:count]


def _refine(settings, completed):
    ranked = ranked_candidates(settings, completed, {"coarse", "bucket"})
    anchors = _anchors(settings, ranked, int(settings.get("refine_anchors", 2)))
    jobs, anchor_batches = [], []
    catalog = _catalog()
    axes = {"开仓位置过滤": catalog["position_filters"],
            "强制时间止损分钟": catalog["hard_timeouts"], "仓位倍数": catalog["sizes"],
            "止盈后等待分钟": catalog["cooldowns"],
            "开仓方向": [x[0] for x in catalog["directions"]],
            "交易会话": [x[0] for x in catalog["sessions"]]}
    long_axes = {"止盈方案编号": [x["编号"] for x in catalog["take_profit"]],
                 "止损代码": [x["code"] for x in catalog["stops"]],
                 "固定止损代码": [x[0] for x in catalog["fixed_stops"]],
                 "叠加止盈代码": [x[0] for x in catalog["overlay_take_profit"]]}
    for anchor in anchors:
        batches = []
        for axis, values in axes.items():
            # 128 x two cost branches, with all other dimensions fixed.
            chunk = min(128, int(settings.get("max_rows_per_job", 512)) // 2)
            if chunk < 1:
                raise ValueError("max_rows_per_job至少为2，才能公平比较双成本")
            for offset in range(0, len(values), chunk):
                cfg = deepcopy(anchor["selection"])
                cfg[axis] = values[offset:offset + chunk]
                batches.append(_pair(settings, "refine", cfg, {"family": anchor["family"], "anchor": anchor["id"], "role": "one_factor", "axis": axis}))
        cfg = deepcopy(anchor["selection"])
        active = [c for vv in cfg["开仓条件"].values() for c in vv if c]
        if not any(c in FIFTH_SPECS for c in active):
            for mode in catalog["entry_modes"]:
                if mode == "F5_EVENT":
                    continue
                changed = deepcopy(cfg)
                changed["入场触发口径"] = mode
                batches.append(_pair(settings, "refine", changed, {"family": anchor["family"], "anchor": anchor["id"], "role": "one_factor", "axis": "入场触发口径"}))
        for mode in catalog["fill_modes"]:
            changed = deepcopy(cfg)
            changed["成交价格口径"] = mode
            batches.append(_pair(settings, "refine", changed, {"family": anchor["family"], "anchor": anchor["id"], "role": "execution_sensitivity", "axis": "成交价格口径"}))
        # Broad, cheap axes precede long grids. TP and stop buckets alternate;
        # one expensive axis or one anchor cannot consume the entire budget.
        for offset in range(0, max(map(len, long_axes.values())), chunk):
            for axis, values in long_axes.items():
                if offset >= len(values):
                    continue
                cfg = deepcopy(anchor["selection"])
                cfg[axis] = values[offset:offset + chunk]
                batches.append(_pair(settings, "refine", cfg, {"family": anchor["family"], "anchor": anchor["id"], "role": "one_factor", "axis": axis}))
        anchor_batches.append(deque(batches))
    while any(anchor_batches):
        for batches in anchor_batches:
            if batches:
                jobs.extend(batches.popleft())
    # A small, declared cross-timeframe interaction test, not arbitrary subsets.
    for left in anchors:
        for right in anchors:
            if left["id"] >= right["id"]:
                continue
            cfg = deepcopy(left["selection"])
            other = right["selection"]
            if cfg["开仓指标"] != other["开仓指标"]:
                continue
            a = {tf for tf, v in cfg["开仓条件"].items() if v != [0]}
            b = {tf for tf, v in other["开仓条件"].items() if v != [0]}
            if a & b:
                continue
            for tf in b:
                cfg["开仓条件"][tf] = other["开仓条件"][tf]
            if any(c in FIFTH_SPECS for v in cfg["开仓条件"].values() for c in v):
                cfg["入场触发口径"] = "F5_EVENT"
            jobs += _pair(settings, "refine", cfg, {"family": left["family"], "role": "cross_timeframe", "axis": "two_timeframes"})
    return jobs


def _validation(settings, completed):
    ranked = ranked_candidates(settings, completed)
    # Freeze multiple candidates per family, not just a lucky maximum.
    selected, counts = [], defaultdict(int)
    families = set(_winning_families(settings, ranked))
    for item in ranked:
        family = item["family"]
        if family in families and counts[family] < int(settings.get("validation_per_family", 2)):
            selected.append(item)
            counts[family] += 1
    return [_job(settings, "validation", item["selection"], _windows(settings)[2],
                 {"family": item["family"], "role": "reserved_validation", "candidate": item["id"],
                  "training_score": item["score"], "axis": "frozen_candidate"}) for item in selected]


def validation_jobs(settings, all_jobs):
    """Freeze once, including when a scheduler reserves the last budget window."""
    jobs = list(all_jobs)
    if any(job["phase"] == "validation" for job in jobs):
        return []
    remaining = max(0, int(settings.get("max_jobs", 2000)) - len(jobs))
    return _validation(settings, jobs)[:remaining]


def advance_jobs(settings, completed_jobs):
    """Call with ALL registered jobs, including queued/running; dedupe by ID."""
    jobs = list(completed_jobs)
    if not jobs:
        return initial_jobs(settings)
    if any(job.get("state") not in TERMINAL for job in jobs):
        return []
    latest = max(PHASES.index(job["phase"]) for job in jobs)
    if latest == len(PHASES) - 1:
        return []
    builders = (_bucket, _refine, _validation)
    # Empty eligible refinement goes directly to the validation decision; it
    # cannot turn losing or missing-data candidates into a fabricated winner.
    generated = builders[latest](settings, jobs)
    if not generated and latest == 1:
        generated = _validation(settings, jobs)
    known = {job["id"] for job in jobs}
    unique = {job["id"]: job for job in generated if job["id"] not in known}
    remaining = max(0, int(settings.get("max_jobs", 2000)) - len(jobs))
    # Keep training pairs intact when the declared queue capacity is reached.
    reserve = int(settings.get("top_families", 8)) * int(settings.get("validation_per_family", 2))
    validation_phase = bool(unique) and next(iter(unique.values()))["phase"] == "validation"
    capacity = remaining if validation_phase else max(0, remaining - reserve) // 2 * 2
    selected = list(unique.values())[:capacity]
    for job in selected:
        job["phase_plan"] = {"planned_jobs": len(unique), "enqueued_jobs": len(selected),
                             "capacity_truncated": len(selected) < len(unique)}
    return selected


def analysis_report(settings, completed_jobs):
    """Small result artifact with full rerunnable candidates and rejection evidence."""
    jobs = list(completed_jobs)
    audit = {}
    ranked = ranked_candidates(settings, jobs, audit=audit)
    validation = []
    for job in jobs:
        if job["phase"] != "validation":
            continue
        observed = []
        if job.get("state") in ("done", "completed"):
            for _, _, _, cost, metrics in _row_candidates(job):
                observed.append({"cost_mode": cost, **metrics})
        reasons = []
        if job.get("state") not in ("done", "completed"):
            reasons.append("验证尚未完成或未执行")
        if {x["cost_mode"] for x in observed} != {"FEE", "SLIPPAGE"}:
            reasons.append("两种成本结果不完整")
        if any(x["trades"] < int(settings.get("minimum_trades", 20)) for x in observed):
            reasons.append("验证交易数不足预设门槛")
        if any(x["return"] <= 0 for x in observed):
            reasons.append("至少一种成本情景在验证段不盈利")
        if any(x["liquidations"] or x["stopped"] for x in observed):
            reasons.append("验证出现强平或资金停机")
        validation.append({"job_id": job["id"], "candidate_id": job["tags"].get("candidate"),
                           "state": job.get("state", "queued"), "selection": job["selection"],
                           "start": job["start"], "end": job["end"], "observations": observed,
                           "meets_prespecified_time_check": not reasons, "reasons": reasons})
    return {"version": VERSION, "windows": _windows(settings), "selection_audit": audit,
            "training_candidates": ranked[:int(settings.get("report_candidates", 50))],
            "training_candidates_truncated_in_report": len(ranked) > int(settings.get("report_candidates", 50)),
            "validation_candidates": validation,
            "conclusion": ("已冻结候选完成情况见时间复核；不得仅按训练最高收益选实盘策略" if validation else
                           "已有训练候选，尚未完成冻结时间复核" if ranked else
                           "当前没有同时通过两训练窗、两成本及交易数门槛的候选；不宣称找到最佳策略"),
            "metric_units": {"return": "收益小数，0.01表示1%", "drawdown": "回撤小数，0.01表示1%", "trades": "完整交易次数"},
            "limitations": ["只是单个ETHUSDC历史场景，不能直接外推到BTC或多币组合",
                            "验证时间段可能曾被人查看，不是真正未来样本外",
                            "门槛检验是描述性筛选，不是多重比较校正后的显著性或亏损因果证明",
                            "固定费率与偏移是两个独立成本情景，不代表已核验的实际账户收费",
                            "理论触碰价仅作敏感性对照，收盘价压力回测也不等于真实Maker排队成交",
                            "名义杠杆受ETH数量上限及账户风险规则约束，数据不足/延后任务不能算已测"],
            "coverage": coverage_report(settings, jobs)}


def coverage_report(settings, jobs):
    """Actual completions, assigned pending jobs, and the untouched catalog."""
    jobs = list(jobs)
    defined = _catalog()
    attempted, finished = defaultdict(set), defaultdict(set)
    for job in jobs:
        cfg = job["selection"]
        values = {"entry": [f"{tf}:{field}:{code}" for tf, cc in cfg["开仓条件"].items() for code in cc if code for field in cfg["开仓指标"]],
                  "take_profit": cfg["止盈方案编号"], "stop": cfg["止损代码"],
                  "fixed_stop": cfg["固定止损代码"], "overlay": cfg["叠加止盈代码"],
                  "position_filter": cfg["开仓位置过滤"], "size": cfg["仓位倍数"],
                  "cost_mode": cost_modes(cfg), "fill_mode": [cfg["成交价格口径"]],
                  "entry_mode": cfg["入场触发口径"] if isinstance(cfg["入场触发口径"], list) else [cfg["入场触发口径"]],
                  "direction": cfg["开仓方向"], "session": cfg["交易会话"],
                  "hard_timeout": cfg["强制时间止损分钟"], "cooldown": cfg["止盈后等待分钟"]}
        for key, items in values.items():
            attempted[key].update(map(str, items))
            if job.get("state") in ("done", "completed"):
                finished[key].update(map(str, items))
    universe = {"entry": [f"{tf}:{field}:{x['code']}" for x in defined["entries"] for tf in x["timeframes"] for field in ("hist", "dif")],
                "take_profit": [x["编号"] for x in defined["take_profit"]],
                "stop": [x["code"] for x in defined["stops"]],
                "fixed_stop": [x[0] for x in defined["fixed_stops"]],
                "overlay": [x[0] for x in defined["overlay_take_profit"]],
                "position_filter": defined["position_filters"], "size": defined["sizes"],
                "cost_mode": defined["cost_modes"], "fill_mode": list(defined["fill_modes"]),
                "entry_mode": list(defined["entry_modes"]), "direction": [x[0] for x in defined["directions"]],
                "session": [x[0] for x in defined["sessions"]], "hard_timeout": defined["hard_timeouts"],
                "cooldown": defined["cooldowns"]}
    coverage = {}
    for key, values in universe.items():
        all_values = set(map(str, values))
        coverage[key] = {"defined_count": len(all_values), "completed_option_count": len(finished[key]),
                         "assigned_not_completed": sorted(attempted[key] - finished[key]),
                         "not_assigned": sorted(all_values - attempted[key])}
    states = defaultdict(int)
    for job in jobs:
        states[job.get("state", "queued")] += 1
    return {"version": VERSION, "contract": defined["coverage_contract"], "job_states": dict(states),
            "max_jobs": int(settings.get("max_jobs", 2000)), "coverage": coverage,
            "capacity_truncated": any(j.get("phase_plan", {}).get("capacity_truncated", False) for j in jobs),
            "budget_deferred_jobs": sum(j.get("state") == "deferred_budget" for j in jobs),
            "future_phases": [phase for phase in PHASES if not any(j["phase"] == phase for j in jobs)],
            "complete_cartesian_product": False,
            "note": "选项覆盖只表示至少一次背景下完成；不等于所有交互已覆盖，失败/缺数据不算已测。"}
