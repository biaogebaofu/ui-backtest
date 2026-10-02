from __future__ import annotations

import hashlib
import json
import math
from math import prod
from extended_rules import ONE_MINUTE_CASE_CODES, entry_supported_timeframes, FIFTH_ROUND_CODES
from fifth_policy import fifth_retirement_reason
from execution_settings import (DEFAULT_COST_MODE, DEFAULT_FEES, DEFAULT_SLIPPAGE,
                                DEFAULT_MIN_REENTRY_MINUTES, normalize_execution_settings, cost_modes)
from entry_position import normalize_position_filters
from indicator_combinations import DEFAULT_REGISTRY, normalize_combinations, choices_for_config, selection_pools

from strategy_space import (仓位列表, 生成止损组合, 生成止盈方案, 生成固定止损档,
                            固定止损同档代码, 方向列表, 会话列表, 强制时间止损档,
                            生成叠加止盈档, 叠加止盈同档代码, 解析固定比例档)


# 高周期规则在原生已收盘K线上计算，映射为1m决策时的持续许可状态。
时间条件 = {tf: tuple(c for c in ONE_MINUTE_CASE_CODES if tf in entry_supported_timeframes(c))
            for tf in ("4h", "1h", "15m", "5m", "1m")}
默认成交偏移 = dict(DEFAULT_SLIPPAGE)
默认资金约束 = {
    "初始资金USDC": 100.0,
    "最小开仓数量ETH": 0.01,
    "最大开仓数量ETH": 100.0,
    "低于最小数量停止": True,
    # 全仓（Cross）口径：整个账户为持仓担保。
    "保护止损浮亏比例": 0.85,     # ②爆仓保护：浮亏达到权益的这个比例即平仓
    "维持保证金率": 0.005,        # 币安第一档 MMR，用于计算全仓强平线
    "启用全仓强平": True,         # 强平线早于保护线时按强平处理，账户归零并停机
    "资金费率": 0.0,              # 每 8 小时结算一次；双向均按成本计，0 表示不计
}
默认入场约束 = {
    "最小S3距离": 0.001,          # 0.10%，0 表示关闭
    "S3基线周期": "1m",
}
入场触发口径选项 = {
    "LIVE_01": "01实盘兼容：高周期状态持续＋每个1分钟MACD方向段一次",
    "TF_EVENT": "研究事件：最低启用周期收盘最多触发一次",
    # Always 1m hist: slope reversals do not reset; opposite nonzero sign starts
    # a new cycle; zero extends the prior side and leading zero cannot enter.
    "MACD_CYCLE": "DIF/DEA金叉死叉：每次红绿换色后最多开一次（1m）",
    "MACD_FULL_RED": "完整红绿一轮一次：红开始→绿结束（下次红开始，1m）",
    "MACD_FULL_GREEN": "完整绿红一轮一次：绿开始→红结束（下次绿开始，1m）",
    "F5_EVENT": "第五批专用事件（每个组合都含第五批时才选）",
}
默认入场触发口径 = "LIVE_01"
# Known UI labels are display aliases of the same strategy mode.
入场触发显示代码 = {label: code for code, label in 入场触发口径选项.items()}
入场触发显示代码["每个1m指标连续升/降段一次（非零轴整轮/金叉死叉周期）"] = "LIVE_01"
入场触发显示代码["MACD柱hist零轴同侧一轮最多一次"] = "MACD_CYCLE"
入场触发显示代码["第五轮独立事件：按方法方向与确认时刻开仓"] = "F5_EVENT"


def 入场口径列表(config_or_value):
    value = (config_or_value.get("入场触发口径", 默认入场触发口径)
             if isinstance(config_or_value, dict) else config_or_value)
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        raise ValueError("入场触发口径至少选择一个已知模式，必须是模式代码或代码列表")
    selected = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError("入场触发口径必须是已知模式代码，列表中不能包含其他类型")
        code = 入场触发显示代码.get(item, item)
        if code not in 入场触发口径选项:
            raise ValueError(f"未知入场触发口径：{item!r}；请选择已列出的入场模式")
        selected.add(code)
    return [code for code in 入场触发口径选项 if code in selected]


def 规范化入场触发口径(value):
    if not isinstance(value, (str, list)):
        raise ValueError("入场触发口径必须是模式代码或非空代码列表")
    modes = 入场口径列表(value)
    # Keep every historical scalar configuration/signature byte-for-byte stable.
    return modes[0] if len(modes) == 1 else modes


def 结果入场口径列表(config, cases):
    for code in cases:
        group = DEFAULT_REGISTRY.resolve("entry", code)
        if any(c in FIFTH_ROUND_CODES for c in (group["members"] if group else [code])):
            return ["F5_EVENT"]
    return 入场口径列表(config)


def 每个组合含第五批(config):
    combinations = config.get("指标组合", {})
    for tf, values in config.get("开仓条件", {}).items():
        spec = combinations.get("开仓", {}).get(tf)
        if spec and spec.get("启用", True) and not spec.get("保留单项", True):
            old_count = sum(c != 0 and c not in FIFTH_ROUND_CODES for c in values)
            if spec.get("组合数量") and min(spec["组合数量"]) > old_count:
                return True
        elif values and all(c in FIFTH_ROUND_CODES for c in values):
            return True
    return False


成交价格口径选项 = {
    "THEORETICAL": "理论触碰价（旧结果对照）",
    "CLOSE_CONFIRMED": "触发K线收盘价（收线确认压力测试）",
}
# 排行按哪个指标取"最优"。原来写死是期末资金。
# 值 = (中文表头里的列名, 是否越大越好)
排行指标选项 = {
    "期末资金（USDC）": ("期末资金（USDC）", True),
    "累计收益率（%）": ("累计收益率（%）", True),
    "盈亏比（倍）": ("盈亏比（倍）", True),
    "t值": ("t值", True),
    "胜率（%）": ("胜率（%）", True),
    "平均日完整交易数（次/日）": ("平均日完整交易数（次/日）", True),
    "平均日成交订单数（笔/日）": ("平均日成交订单数（笔/日）", True),
    "交易次数（单）": ("交易次数（单）", True),
    "平均单笔收益率（%）": ("平均单笔收益率（%）", True),
    "毛收益合计（%）": ("毛收益合计（%）", True),
    "2025毛收益（%）": ("2025毛收益（%）", True),
    "2026毛收益（%）": ("2026毛收益（%）", True),
    "多单占比（%）": ("多单占比（%）", True),
    "实际成交次数（单）": ("实际成交次数（单）", True),
    "期末可开仓数量（ETH）": ("期末可开仓数量（ETH）", True),
    "最大回撤（%）越小越好": ("最大回撤（%）", False),
    "爆仓保护次数（次）越小越好": ("爆仓保护次数（次）", False),
    "全仓强平次数（次）越小越好": ("全仓强平次数（次）", False),
    "平均持仓时间（分钟）越小越好": ("平均持仓时间（分钟）", False),
    "资金性停机标记（0否1是）越小越好": ("资金性停机标记（0否1是）", False),
}
默认排行指标 = "期末资金（USDC）"

S3基线周期选项 = ("1m", "5m", "15m", "1h", "4h")
默认候选筛选 = {
    "启用": True,
    "自动导出": True,
    "筛选方案": "RETURN_DRAWDOWN",
    "比较范围": "ALL_RUN",
    "资金保留比例": 0.90,
    "统一目标杠杆": 5.0,
    "p95往返偏移": 0.0005,
    "极端往返偏移": 0.001,
    "最大回撤优选": 0.30,
    "最大回撤硬上限": 0.40,
    "盈亏比下限": 1.20,
    "容量占用率上限": 0.70,
    "多单占比下限": 0.30,
    "多单占比上限": 0.70,
    "爆仓保护次数上限": 10,
    "最低原始交易数": 200,
    "每类最多": 50,
    "全局实盘最多": 10,
    "全局观察最多": 20,
    "导出旧排行": False,
}


def 规范化候选筛选(raw: dict | None) -> dict:
    source = dict(默认候选筛选)
    if raw is not None:
        # 显式历史候选配置继续沿用旧筛选，只有全新设置采用收益/回撤方案。
        source.update({"筛选方案": "LEGACY_STRESS", "比较范围": "TARGET", "资金保留比例": 0.90})
    source.update(raw or {})
    result = {
        "启用": bool(source["启用"]),
        "自动导出": bool(source["自动导出"]),
        "筛选方案": source["筛选方案"],
        "比较范围": source["比较范围"],
        "资金保留比例": float(source["资金保留比例"]),
        "统一目标杠杆": float(source["统一目标杠杆"]),
        "p95往返偏移": float(source["p95往返偏移"]),
        "极端往返偏移": float(source["极端往返偏移"]),
        "最大回撤优选": float(source["最大回撤优选"]),
        "最大回撤硬上限": float(source["最大回撤硬上限"]),
        "盈亏比下限": float(source["盈亏比下限"]),
        "容量占用率上限": float(source["容量占用率上限"]),
        "多单占比下限": float(source["多单占比下限"]),
        "多单占比上限": float(source["多单占比上限"]),
        "爆仓保护次数上限": int(source["爆仓保护次数上限"]),
        "最低原始交易数": int(source["最低原始交易数"]),
        "每类最多": int(source["每类最多"]),
        "全局实盘最多": int(source["全局实盘最多"]),
        "全局观察最多": int(source["全局观察最多"]),
        "导出旧排行": bool(source["导出旧排行"]),
    }
    for key, value in result.items():
        if isinstance(value, (float, int)) and not math.isfinite(value):
            raise ValueError(f"{key}必须是有限数字，不能使用NaN或Infinity")
    if result["筛选方案"] not in ("RETURN_DRAWDOWN", "LEGACY_STRESS"):
        raise ValueError("未知候选筛选方案")
    if result["比较范围"] not in ("ALL_RUN", "TARGET"):
        raise ValueError("候选比较范围必须是ALL_RUN或TARGET")
    if not 0.0 < result["资金保留比例"] <= 1.0:
        raise ValueError("资金保留比例必须大于0且不超过100%")
    if result["统一目标杠杆"] not in 仓位列表:
        raise ValueError("候选比较杠杆必须是组合选择中的有效仓位档位")
    for key in ("p95往返偏移", "极端往返偏移"):
        if not 0.0 <= result[key] <= 0.1:
            raise ValueError(f"{key}必须在0%到10%之间")
    if result["极端往返偏移"] < result["p95往返偏移"]:
        raise ValueError("极端往返偏移不能小于p95往返偏移")
    if not 0.0 <= result["最大回撤优选"] <= result["最大回撤硬上限"] <= 1.0:
        raise ValueError("最大回撤门槛必须满足0%≤优选≤硬上限≤100%")
    if result["盈亏比下限"] < 0.0:
        raise ValueError("利润因子PF下限不能为负数")
    if not 0.0 <= result["容量占用率上限"] <= 2.0:
        raise ValueError("持仓+等待占时上限必须在0%到200%之间")
    if not 0.0 <= result["多单占比下限"] <= result["多单占比上限"] <= 1.0:
        raise ValueError("多单占比范围必须在0%到100%之间")
    for key in ("爆仓保护次数上限", "最低原始交易数", "每类最多", "全局实盘最多", "全局观察最多"):
        if result[key] < 0:
            raise ValueError(f"{key}不能为负数")
    return result


def 全选配置() -> dict:
    return {
        "版本": 20,
        "开仓指标": ["hist", "dif"],
        "开仓条件": {周期: [c for c in 取值 if not fifth_retirement_reason(c)]
                   for 周期, 取值 in 时间条件.items()},
        "开仓位置过滤": ["OFF"],
        "止损代码": [row[0] for row in 生成止损组合()],
        # 默认只勾选"工作日与周末同档"的10种＋OFF；周末独立的90种需手动勾选，
        # 否则默认全选会把组合数再乘9倍。
        "固定止损代码": 固定止损同档代码(),
        # 叠加在所选止盈方案之上的固定比例止盈，默认 OFF，不改变默认组合数。
        "叠加止盈代码": ["OFF"],
        # 开仓方向/交易会话仍是分析维度；时间止损已移入正式止损页。
        "开仓方向": ["BOTH"],
        "交易会话": ["ALL"],
        "强制时间止损分钟": [0],
        "止盈方案编号": [row.编号 for row in 生成止盈方案()],
        # 0 = 止盈后不额外等待，与止损退出一致。原来最小只能选1，缺这个基准。
        "止盈后等待分钟": list(range(0, 31)),
        "仓位倍数": list(仓位列表),
        "成交偏移": dict(默认成交偏移),
        "成本模式": DEFAULT_COST_MODE,
        "平仓后最小开仓间隔分钟": DEFAULT_MIN_REENTRY_MINUTES,
        "手续费": dict(DEFAULT_FEES),
        "资金约束": dict(默认资金约束),
        "入场约束": dict(默认入场约束),
        "入场触发口径": 默认入场触发口径,
        "成交价格口径": "CLOSE_CONFIRMED",
        "候选筛选": 规范化候选筛选(None),
    }


def 规范化配置(raw: dict | None) -> dict:
    raw = raw or 全选配置()
    raw_version = int(raw.get("版本", 0))
    fields = [x for x in ("hist", "dif") if x in set(raw.get("开仓指标", []))]
    if not fields:
        raise ValueError("至少选择一种开仓MACD口径")

    raw_cases = raw.get("开仓条件", {})
    cases = {}
    for timeframe, allowed in 时间条件.items():
        selected = {int(x) for x in raw_cases.get(timeframe, [])}
        # v5及更早只有一个含义错误的“代码3”。升级时同时开放新的情况三、四，
        # 避免原来的“全选”设置悄悄漏掉情况四。
        if raw_version < 6 and 3 in selected:
            selected.add(4)
        unknown = sorted(selected - set(allowed))
        if unknown:
            raise ValueError(f"开仓条件“{timeframe}”包含未知或不支持的规则代码：{unknown}；请核对版本和周期，原选择未被过滤")
        cases[timeframe] = [x for x in allowed if x in selected]
        if not cases[timeframe]:
            raise ValueError(f"开仓条件“{timeframe}”至少选择一种情况")

    allowed_stops = {row[0] for row in 生成止损组合()}
    selected_stops = set(raw.get("止损代码", []))
    if raw_version < 6:
        # 旧S4实际执行的是普通MACD反转，对应新版S6；迁移后保持原交易语义。
        selected_stops = {
            "+".join("S6_" + part[3:] if part.startswith("S4_") else part for part in code.split("+"))
            for code in selected_stops
        }
    stops = [row[0] for row in 生成止损组合() if row[0] in selected_stops]
    def fixed_codes(key, prefix, presets):
        known = {row[0] for row in presets}
        result = []
        for code in dict.fromkeys(raw.get(key, []) or []):
            # 旧设置中未知名称仍按原规则忽略；自定义P代码损坏则明确报错，不能静默退回OFF。
            custom = isinstance(code, str) and code.startswith(prefix) and any(
                part.startswith("P") for part in code[len(prefix):].split("_"))
            if code in known or custom:
                result.append(解析固定比例档(code, prefix)[0])
        return result
    overlay_tps = fixed_codes("叠加止盈代码", "FTP", 生成叠加止盈档()) or ["OFF"]
    fixed_stops = fixed_codes("固定止损代码", "FSL", 生成固定止损档())
    if "固定止损代码" in raw and raw["固定止损代码"] == []:
        raise ValueError("固定止损至少勾选一项；不使用固定止损时请明确勾选OFF")
    if not fixed_stops:
        fixed_stops = 固定止损同档代码()
    # 只勾固定比例止损、一条信号止损都不选，是完全合法的配置：
    # 那就是"只靠⑧固定比例止损和②爆仓保护"。自动挂上 OFF 基座即可，
    # 不该拦着说"至少选择一种止损方式"。
    has_time_stop = any(int(x) in 强制时间止损档 and int(x) > 0
                        for x in raw.get("强制时间止损分钟", []))
    if not stops and (any(x != "OFF" for x in fixed_stops) or has_time_stop):
        stops = ["OFF"]
    if not stops:
        raise ValueError("至少选择一种止损方式：信号止损①③④⑤⑥⑨任选，"
                         "或在固定比例止损/时间止损里选择一档非 OFF 的")

    allowed_tps = {row.编号 for row in 生成止盈方案()}
    selected_tps = {int(x) for x in raw.get("止盈方案编号", [])}
    tps = sorted(allowed_tps & selected_tps)
    if not tps:
        raise ValueError("至少选择一种止盈方案")

    selected_cooldowns = {int(x) for x in raw.get("止盈后等待分钟", [])}
    cooldowns = [x for x in range(0, 31) if x in selected_cooldowns]
    if not cooldowns:
        raise ValueError("至少选择一种止盈后等待时间")


    def _pick(key, allowed, fallback):
        got = [x for x in dict.fromkeys(raw.get(key, []) or []) if x in allowed]
        return got or list(fallback)

    directions = _pick("开仓方向", {c for c, _ in 方向列表}, ["BOTH"])
    sessions = _pick("交易会话", {c for c, _ in 会话列表}, ["ALL"])
    hard_time = [int(x) for x in dict.fromkeys(raw.get("强制时间止损分钟", []) or [])
                 if int(x) in 强制时间止损档] or [0]

    selected_sizes = {round(float(x), 10) for x in raw.get("仓位倍数", [])}
    sizes = [float(x) for x in 仓位列表 if round(float(x), 10) in selected_sizes]
    if not sizes:
        raise ValueError("至少选择一档仓位/杠杆")

    raw_slippage = raw.get("成交偏移", 默认成交偏移)
    entry_slippage = float(raw_slippage.get("开仓", 默认成交偏移["开仓"]))
    exit_slippage = float(raw_slippage.get("平仓", 默认成交偏移["平仓"]))
    if not (0.0 <= entry_slippage <= 0.1 and 0.0 <= exit_slippage <= 0.1):
        raise ValueError("成交偏移必须在0%到10%之间")

    raw_funds = raw.get("资金约束", 默认资金约束)
    initial_capital = float(raw_funds.get("初始资金USDC", 默认资金约束["初始资金USDC"]))
    minimum_eth = float(raw_funds.get("最小开仓数量ETH", 默认资金约束["最小开仓数量ETH"]))
    maximum_eth = float(raw_funds.get("最大开仓数量ETH", 默认资金约束["最大开仓数量ETH"]))
    stop_when_too_small = bool(raw_funds.get("低于最小数量停止", True))
    if not math.isfinite(initial_capital) or initial_capital <= 0.0:
        raise ValueError("初始资金必须是大于0的有限数字 USDC")
    if not 0.0 <= minimum_eth <= 1_000_000.0:
        raise ValueError("ETH最小开仓数量必须在0到1,000,000之间")
    if not minimum_eth <= maximum_eth <= 100.0:
        raise ValueError("ETH单次最大开仓数量必须不小于最小开仓数量，且不能超过100 ETH")
    protect_ratio = float(raw_funds.get("保护止损浮亏比例", 默认资金约束["保护止损浮亏比例"]))
    maintenance_rate = float(raw_funds.get("维持保证金率", 默认资金约束["维持保证金率"]))
    cross_liquidation = bool(raw_funds.get("启用全仓强平", 默认资金约束["启用全仓强平"]))
    funding_rate = float(raw_funds.get("资金费率", 默认资金约束["资金费率"]))
    if not 0.05 <= protect_ratio <= 1.0:
        raise ValueError("保护止损浮亏比例必须在5%到100%之间")
    if not 0.0 <= maintenance_rate <= 0.2:
        raise ValueError("维持保证金率必须在0%到20%之间")
    if not 0.0 <= funding_rate <= 0.01:
        raise ValueError("资金费率必须在0%到1%之间")

    raw_entry = raw.get("入场约束", 默认入场约束)
    s3_gap = float(raw_entry.get("最小S3距离", 默认入场约束["最小S3距离"]))
    s3_timeframe = str(raw_entry.get("S3基线周期", 默认入场约束["S3基线周期"]))
    if not 0.0 <= s3_gap <= 0.1:
        raise ValueError("最小S3距离必须在0%到10%之间")
    if s3_timeframe not in S3基线周期选项:
        raise ValueError("S3基线周期必须是1m/5m/15m/1h/4h之一")
    entry_mode = 规范化入场触发口径(raw.get("入场触发口径", 默认入场触发口径))
    fill_mode = raw.get("成交价格口径", "THEORETICAL")
    if fill_mode not in 成交价格口径选项:
        raise ValueError("未知成交价格口径")
    position_filters = raw.get("开仓位置过滤", ["OFF"])
    if not isinstance(position_filters, list):
        raise ValueError("开仓位置过滤必须是方案代码列表；不限制时明确选择OFF")

    result = {
        "版本": 20,
        "开仓指标": fields,
        "开仓条件": cases,
        "开仓位置过滤": normalize_position_filters(position_filters),
        "止损代码": stops,
        "固定止损代码": fixed_stops,
        "叠加止盈代码": overlay_tps,
        "开仓方向": directions,
        "交易会话": sessions,
        "强制时间止损分钟": hard_time,
        "止盈方案编号": tps,
        "止盈后等待分钟": cooldowns,
        "仓位倍数": sizes,
        "成交偏移": {"开仓": entry_slippage, "平仓": exit_slippage},
        **normalize_execution_settings(raw),
        "资金约束": {
            "初始资金USDC": initial_capital,
            "最小开仓数量ETH": minimum_eth,
            "最大开仓数量ETH": maximum_eth,
            "低于最小数量停止": stop_when_too_small,
            "保护止损浮亏比例": protect_ratio,
            "维持保证金率": maintenance_rate,
            "启用全仓强平": cross_liquidation,
            "资金费率": funding_rate,
        },
        "入场约束": {"最小S3距离": s3_gap, "S3基线周期": s3_timeframe},
        "入场触发口径": entry_mode,
        "成交价格口径": fill_mode,
        "候选筛选": 规范化候选筛选(raw.get("候选筛选")),
    }
    combinations = normalize_combinations(raw.get("指标组合"), selection_pools(result))
    if combinations:
        result["指标组合"] = combinations
    modes = 入场口径列表(result)
    if any(c in FIFTH_ROUND_CODES for values in cases.values() for c in values) and len(modes) > 1:
        raise ValueError("第五轮使用自己的事件口径；包含第五轮时只选一种入场次数规则，避免将相同事件重复回测")
    if "F5_EVENT" in modes and not 每个组合含第五批(result):
        raise ValueError("不必选择第五批。当前误用了“第五批专用事件”；点击“使用普通入场模式”即可回测前几批。混合扫描时，第五批组合仍自动按自身事件运行")
    return result


def 配置签名(config: dict) -> str:
    normalized = 规范化配置(config)
    # 候选导出只影响回测后的筛选，不改变交易结果，允许续跑时调整。
    normalized.pop("候选筛选", None)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def 配置统计(config: dict) -> dict[str, int]:
    normalized = 规范化配置(config)
    options = choices_for_config(normalized)
    base_entries = (len(normalized["开仓指标"]) * len(normalized["开仓方向"])
               * len(normalized["交易会话"])) * prod(
        options["开仓"][timeframe].count for timeframe in 时间条件
    )
    position_filters = len(normalized["开仓位置过滤"])
    entry_modes = len(入场口径列表(normalized))
    costs = len(cost_modes(normalized))
    entries = base_entries * position_filters * entry_modes
    stops = (options["止损"].count * len(normalized["固定止损代码"])
             * len(normalized["强制时间止损分钟"]))
    tps = options["止盈"].count * len(normalized["叠加止盈代码"])
    cooldowns = len(normalized["止盈后等待分钟"])
    sizes = len(normalized["仓位倍数"])
    rows = entries * stops * tps * cooldowns * costs
    return {
        "入场组合数": entries,
        "基础入场组合数": base_entries,
        "入场口径档数": entry_modes,
        "成本模式档数": costs,
        "开仓位置过滤档数": position_filters,
        # 止损页的信号规则、固定比例档和强制时间档是相互组合的。
        # 同时返回三个分项，供 UI 把“选了几条规则”和“实际会跑几种组合”分开显示。
        "信号止损数": options["止损"].count,
        "固定止损数": len(normalized["固定止损代码"]),
        "时间止损数": len(normalized["强制时间止损分钟"]),
        "止损组合数": stops,
        "止盈方案数": tps,
        "等待时间档数": cooldowns,
        "仓位档数": sizes,
        "不含仓位完整组合数": rows,
        "包含仓位完整组合数": rows * sizes,
    }
