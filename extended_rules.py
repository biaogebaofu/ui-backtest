"""已接入引擎的自由组合开仓/止损扩展规则。

批次约定：
- 第一批：0—4（v8 原始情况）
- 第二批：5—17（0904 扩展）
- 第四批：144—263（v1.44R重建版，120条参数化规则）
- 第三批：18—143（2026-09-09 数据挖掘/实验扩展；18—127为K线/成交微结构，128—143为资金费/OI/成交额）
第三/四批支持可正确计算的原生收盘周期；显式分钟窗口按支持矩阵限制。

代码一经导出不可重新分配。第二批既有编号和旧 stable_base_id 保持不变。
"""

# 编号 -> (名称、算法、参数)。同周期多选时分别生成策略，不自动AND。
ENTRY_RULES = {
    # ---------------- 第二批（0904） ----------------
    5: ("零轴同向", "zero", 0.),
    6: ("长影线过滤50%", "wick", .5),
    7: ("长影线过滤60%", "wick", .6),
    8: ("长影线过滤70%", "wick", .7),
    9: ("实体占比至少30%", "body", .3),
    10: ("实体占比至少50%", "body", .5),
    11: ("实体占比至少70%", "body", .7),
    12: ("MA20斜率同向1根", "slope", 1.),
    13: ("MA20斜率同向3根", "slope", 3.),
    14: ("MA20斜率同向5根", "slope", 5.),
    15: ("距MA20不超过0.1%", "distance", .001),
    16: ("距MA20不超过0.2%", "distance", .002),
    17: ("距MA20不超过0.4%", "distance", .004),

    # ---------------- 第三批：MACD/蜡烛/波动/成交量 ----------------
    18: ("MACD加速度同向", "macd_accel", 1.),
    19: ("MACD加速度连续2根同向", "macd_accel2", 2.),
    20: ("MACD幅度≥20根标准差0.5倍", "macd_amp_std", .5),
    21: ("MACD幅度≥20根标准差1.0倍", "macd_amp_std", 1.0),
    22: ("CLV收盘位置强势50%", "clv", .5),
    23: ("CLV收盘位置极强75%", "clv", .75),
    24: ("K线实体方向与MACD同向", "candle_align", 0.),
    25: ("同向实体且实体占比≥50%", "candle_body_align", .5),
    26: ("振幅扩张≥前20均值1.5倍", "range_ratio_min", 1.5),
    27: ("振幅收缩≤前20均值0.6倍", "range_ratio_max", .6),
    28: ("成交量≥前20均量1.5倍", "volume_ratio_min", 1.5),
    29: ("成交量≥前20均量2.5倍", "volume_ratio_min", 2.5),
    30: ("成交量≤前20均量0.6倍", "volume_ratio_max", .6),
    31: ("连续3根价格动量同向", "momentum_run", 3.),
    32: ("连续5根价格动量同向", "momentum_run", 5.),

    # ---------------- 第三批：结构/均值/趋势 ----------------
    33: ("唐奇安20收盘突破", "donchian_break", 20.),
    34: ("唐奇安60收盘突破", "donchian_break", 60.),
    35: ("距唐奇安20边缘≤通道10%", "donchian_near", .10),
    36: ("布林20一倍标准差趋势侧", "boll_trend", 1.0),
    37: ("布林20一倍标准差反向极端", "boll_contra", 1.0),
    38: ("RSI14趋势区55/45", "rsi_trend", 55.),
    39: ("RSI14逆势极端35/65", "rsi_contra", 35.),
    40: ("效率比ER10≥0.50", "efficiency_min", .50),
    41: ("效率比ER10≤0.25", "efficiency_max", .25),
    42: ("距MA20至少0.20%（追势）", "distance_min", .002),
    43: ("距MA20不超过0.05%（贴均线）", "distance", .0005),
    44: ("滚动VWAP20同向", "vwap_side", 20.),
    45: ("距滚动VWAP20不超过0.10%", "vwap_distance", .001),
    46: ("3根收盘新高/新低", "close_break", 3.),
    47: ("10根收盘新高/新低", "close_break", 10.),
    48: ("内包K线母线突破", "inside_break", 0.),
    49: ("扫前高低后收回", "sweep_reclaim", 0.),
    50: ("前一根反向→本根同向微反转", "micro_reversal", 0.),
    51: ("连续3根同色K线", "candle_run", 3.),
    52: ("连续5根同色K线", "candle_run", 5.),

    # ---------------- 第三批：订单流/成交微结构（CSV有可选字段时生效） ----------------
    53: ("主动买占比≥60%/≤40%", "taker_ratio", .60),
    54: ("主动买占比≥70%/≤30%", "taker_ratio", .70),
    55: ("主动买占比Z分数±1", "taker_z", 1.0),
    56: ("主动成交量Delta同向", "delta_side", 0.),
    57: ("5根CVD增量同向", "cvd_slope", 5.),
    58: ("成交笔数≥前20均值1.5倍", "trades_ratio", 1.5),
    59: ("平均单笔规模≥前20均值1.5倍", "avg_trade_ratio", 1.5),
    60: ("价格3根逆向但CVD同向（背离反转）", "price_cvd_div", 3.),

    # ---------------- 第三批：故意无厘头/安慰剂对照 ----------------
    61: ("安慰剂：UTC分钟个位数=7", "minute_tail7", 0.),
    62: ("安慰剂：价格分位末位=7或8", "price_tail78", 0.),
    63: ("安慰剂：UTC分钟处于斐波那契集合", "minute_fib", 0.),

    # ---------------- 第三批追加：MACD状态/拐点 ----------------
    64: ("MACD斜率连续2根同向", "macd_slope_run", 2.),
    65: ("MACD斜率连续3根同向", "macd_slope_run", 3.),
    66: ("MACD同向但加速度反向（减速）", "macd_decel", 0.),
    67: ("MACD相对20根均值Z同向≥0.5", "macd_z", .5),
    68: ("MACD相对20根均值Z同向≥1.5", "macd_z", 1.5),
    69: ("MACD贴近零轴≤0.25σ", "macd_near_zero", .25),
    70: ("MACD刚穿零轴事件", "zero_cross_event", 0.),
    71: ("DIF与MACD柱方向同时同向", "dual_macd_dir", 0.),
    72: ("DIF与MACD柱方向分歧", "macd_dir_disagree", 0.),
    73: ("MACD柱符号与方向同侧", "hist_sign_align", 0.),

    # ---------------- 第三批追加：K线结构/短序列 ----------------
    74: ("吞没K线同向", "engulf", 0.),
    75: ("外包K线且实体同向", "outside_align", 0.),
    76: ("长下/上影拒绝≥60%", "pin_reversal", .60),
    77: ("收盘位于振幅最强20%一侧", "close_extreme", .20),
    78: ("十字星后同向突破", "doji_break", .10),
    79: ("NR4窄幅K线", "nr_n", 4.),
    80: ("NR7窄幅K线", "nr_n", 7.),
    81: ("WR7宽幅K线", "wr_n", 7.),
    82: ("2根累计动量同向≥0.10%", "return2_min", .001),
    83: ("5根累计动量同向≥0.20%", "return5_min", .002),
    84: ("5根价格逆向≥0.20%后顺MACD", "return5_contra", .002),
    85: ("两根K线强反包反转", "two_bar_reversal", 0.),
    86: ("扫前高低并穿回另一端", "failed_break_strong", 0.),
    87: ("最近3根红绿交替且本根同向", "alternating", 3.),
    88: ("最近5根红绿交替且本根同向", "alternating", 5.),

    # ---------------- 第三批追加：波动率状态 ----------------
    89: ("ATR14相对60均值≤0.75低波动", "atr_regime_low", .75),
    90: ("ATR14相对60均值≥1.25高波动", "atr_regime_high", 1.25),
    91: ("10根实现波动相对60均值≤0.75", "rv_regime_low", .75),
    92: ("10根实现波动相对60均值≥1.25", "rv_regime_high", 1.25),
    93: ("布林带宽相对60均值≤0.70挤压", "boll_width_low", .70),
    94: ("布林带宽相对60均值≥1.50扩张", "boll_width_high", 1.50),
    95: ("真实振幅TR相对20根Z≥1.5", "tr_z_high", 1.5),

    # ---------------- 第三批追加：量价/订单流 ----------------
    96: ("成交量Z分数≥1", "volume_z_high", 1.0),
    97: ("成交量Z分数≤-1", "volume_z_low", 1.0),
    98: ("连续3根成交量递减", "volume_falling", 3.),
    99: ("主动买占比≥55%/≤45%", "taker_ratio", .55),
    100: ("主动买占比≥65%/≤35%", "taker_ratio", .65),
    101: ("主动买占比Z分数±2", "taker_z", 2.0),
    102: ("3根CVD增量同向", "cvd_slope", 3.),
    103: ("10根CVD增量同向", "cvd_slope", 10.),
    104: ("成交笔数≤前20均值0.6倍", "trades_ratio_max", .6),
    105: ("平均单笔规模≤前20均值0.6倍", "avg_trade_ratio_max", .6),
    106: ("成交笔数Z分数≥2", "trades_z_high", 2.0),
    107: ("主动成交Delta Z分数同向≥1", "delta_z_side", 1.0),
    108: ("订单流极端逆向≥65%（吸收候选）", "taker_contra", .65),
    109: ("K线实体与主动Delta双重同向", "flow_candle_align", 0.),
    110: ("扫高低+逆向主动量吸收", "flow_absorption", 0.),
    111: ("价格10根逆向但CVD同向（背离）", "price_cvd_div", 10.),

    # ---------------- 第三批追加：故意无厘头/安慰剂对照 ----------------
    112: ("安慰剂：UTC分钟能被5整除", "minute_mod5", 0.),
    113: ("安慰剂：UTC分钟是质数", "minute_prime", 0.),
    114: ("安慰剂：UTC小时为奇数", "hour_odd", 0.),
    115: ("安慰剂：UTC小时在斐波那契集合", "hour_fib", 0.),
    116: ("安慰剂：价格整数个位=7", "price_integer_tail7", 0.),
    117: ("安慰剂：价格小数两位为00或50", "price_cents_0050", 0.),
    118: ("安慰剂：成交量整数个位=7", "volume_tail7", 0.),
    119: ("安慰剂：成交笔数为奇数", "trades_odd", 0.),
    120: ("安慰剂：UTC分钟=价格分位数字模60", "time_price_match", 0.),
    121: ("安慰剂：时间戳伪随机20%抽样", "time_hash20", 0.),
    122: ("安慰剂：UTC分钟能被13整除", "minute_mod13", 0.),
    123: ("安慰剂：UTC分钟=13或37", "minute_13_37", 0.),
    124: ("安慰剂：UTC分钟双数00/11/22/33/44/55", "minute_double", 0.),
    125: ("安慰剂：UTC日序号模24=小时", "day_hour_match", 0.),
    126: ("安慰剂：价格分位尾数13/42/69", "price_cents_special", 0.),
    127: ("安慰剂：平均单笔规模分位末位=7", "avg_size_tail7", 0.),

    # ---------------- 第三批追加：外部衍生品数据 / 成交额 ----------------
    128: ("已结算资金费率逆拥挤≥0.01%", "funding_contra", .0001),
    129: ("已结算资金费率逆拥挤≥0.03%", "funding_contra", .0003),
    130: ("已结算资金费率与MACD同拥挤", "funding_align", 0.),
    131: ("资金费率刚翻符号后逆拥挤", "funding_flip_contra", 0.),
    132: ("OI 15分钟增幅≥0.30%", "oi_change_min", .003),
    133: ("OI 60分钟增幅≥1.00%", "oi_change60_min", .01),
    134: ("OI 15分钟降幅≥0.30%", "oi_change_max", -.003),
    135: ("OI扩张确认MACD方向", "oi_expand_confirm", .002),
    136: ("价格15分钟逆向+OI扩张（挤压候选）", "price_oi_squeeze", .002),
    137: ("OI相对60分钟均值Z≥1.5", "oi_z_high", 1.5),
    138: ("OI价值15分钟增幅≥0.30%", "oi_value_change_min", .003),
    139: ("OI 15分钟下降+价格顺MACD（去杠杆延续）", "oi_delever_trend", .003),
    140: ("成交额≥前20均值1.5倍", "quote_volume_ratio_min", 1.5),
    141: ("成交额≤前20均值0.6倍", "quote_volume_ratio_max", .6),
    142: ("主动买成交额占比≥60%/≤40%", "taker_quote_ratio", .60),
    143: ("主动成交额Delta同向", "quote_delta_side", 0.),
}

from fourth_batch import (FOURTH_ENTRY_RULES, FOURTH_SPECS, FOURTH_ROUND_CODES,
                          KLINE_ONLY_FOURTH_CODES, MICROSTRUCTURE_FOURTH_CODES)
ENTRY_RULES.update(FOURTH_ENTRY_RULES)
from fifth_batch import (FIFTH_ENTRY_RULES, FIFTH_SPECS, FIFTH_ROUND_CODES,
                         KLINE_ONLY_FIFTH_CODES, MICROSTRUCTURE_FIFTH_CODES,
                         EXTERNAL_FIFTH_CODES, fifth_unavailable_reason)
ENTRY_RULES.update(FIFTH_ENTRY_RULES)

FIRST_ROUND_CODES = tuple(range(5))
SECOND_ROUND_CODES = tuple(range(5, 18))
THIRD_ROUND_CODES = tuple(range(18, 144))
RESEARCH_CASE_CODES = THIRD_ROUND_CODES + FOURTH_ROUND_CODES + FIFTH_ROUND_CODES
ONE_MINUTE_CASE_CODES = FIRST_ROUND_CODES + SECOND_ROUND_CODES + RESEARCH_CASE_CODES
CASE_CODES = ONE_MINUTE_CASE_CODES  # 兼容旧导入；完整全集。
ENTRY_RULE_BATCH = {code: (2 if code in SECOND_ROUND_CODES else (3 if code in THIRD_ROUND_CODES else
                          (4 if code in FOURTH_ROUND_CODES else 5))) for code in ENTRY_RULES}
PLACEBO_CODES = frozenset((61, 62, 63, *range(112, 128)))

# 数据来源分类；高周期使用完整收盘桶的总量或最后已知状态。
# KLINE_ONLY 只依赖 openTime+OHLCV；MICROSTRUCTURE 依赖逐分钟成交统计；EXTERNAL 依赖资金费/OI。
MICROSTRUCTURE_THIRD_CODES = frozenset((*range(53, 61), *range(99, 112), 119, 127, *range(140, 144)))
EXTERNAL_THIRD_CODES = frozenset(range(128, 140))
KLINE_ONLY_THIRD_CODES = tuple(c for c in THIRD_ROUND_CODES if c not in MICROSTRUCTURE_THIRD_CODES and c not in EXTERNAL_THIRD_CODES)

# 每条扩展开仓真正需要的数据能力。UI和工作进程都用它做硬校验，缺数据时不再静默生成0信号。
ENTRY_RULE_REQUIREMENTS = {code: frozenset({"ohlcv"}) for code in THIRD_ROUND_CODES}
for code in (53,54,55,99,100,101,108): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","taker_ratio"})
for code in (56,57,60,102,103,107,109,110,111): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","delta_cvd"})
for code in (58,104,106,119): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","trades"})
for code in (59,105,127): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","avg_trade_size"})
for code in (128,129,130,131): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","funding"})
for code in range(132,138): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","open_interest"})
ENTRY_RULE_REQUIREMENTS[138] = frozenset({"ohlcv","open_interest_value"})
ENTRY_RULE_REQUIREMENTS[139] = frozenset({"ohlcv","open_interest"})
for code in (140,141): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","quote_volume"})
for code in (142,143): ENTRY_RULE_REQUIREMENTS[code] = frozenset({"ohlcv","taker_quote"})

ENTRY_RULE_REQUIREMENTS.update({c: spec["requires"] for c, spec in FOURTH_SPECS.items()})
ENTRY_RULE_REQUIREMENTS.update({c: spec["requires"] for c, spec in FIFTH_SPECS.items()})

ENTRY_TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}


def _timeframe_reason(code, timeframe):
    if timeframe not in ENTRY_TIMEFRAME_MINUTES:
        return "未知开仓周期"
    if code not in ONE_MINUTE_CASE_CODES:
        return "未知开仓策略"
    if code in FIFTH_SPECS:
        supported = FIFTH_SPECS[code]["supported_timeframes"]
        return "" if timeframe in supported else "第五轮原生周期为" + "/".join(supported)
    if timeframe == "1m" or code < 18:
        return ""
    minutes = ENTRY_TIMEFRAME_MINUTES[timeframe]
    kind = ENTRY_RULES[code][1]
    if code in (132, 134, 135, 136, 138, 139) and minutes > 15:
        return "该规则需要15分钟变化，所选周期无法提供完整的15分钟边界"
    if code == 133 and minutes > 60:
        return "该规则需要60分钟变化，所选周期大于60分钟"
    if code == 137 and minutes >= 60:
        return "60分钟OI均值和标准差至少需要两根已收盘K线"
    if code in FOURTH_SPECS and FOURTH_SPECS[code]["family"] == "opening_range":
        window = FOURTH_SPECS[code]["params"][0]
        if minutes > window or window % minutes:
            return f"首{window}分钟区间无法由该周期的完整K线组成"
    if (kind in ("minute_tail7", "minute_13_37") or
            kind == "minute_prime" and minutes >= 15 or
            kind == "hour_odd" and minutes == 240):
        return "该UTC时间条件在所选周期没有可匹配的开盘时刻"
    return ""


def entry_supported_timeframes(code):
    return tuple(tf for tf in ENTRY_TIMEFRAME_MINUTES if not _timeframe_reason(int(code), tf))


HIGH_TF_CASE_CODES = tuple(c for c in ONE_MINUTE_CASE_CODES
                           if any(tf != "1m" for tf in entry_supported_timeframes(c)))


def capabilities_for_timeframe(capabilities, capabilities_by_timeframe, timeframe):
    if timeframe == "1m":
        return capabilities or {}
    # Old metadata has no evidence that optional columns survive aggregation.
    return (capabilities_by_timeframe or {}).get(timeframe, {})


def entry_unavailable_reason(code, capabilities, timeframe="1m", *, allow_retired=False):
    code = int(code)
    if not allow_retired:
        from fifth_policy import fifth_retirement_reason
        reason = fifth_retirement_reason(code)
        if reason:
            return reason
    reason = _timeframe_reason(code, timeframe)
    if reason:
        return reason
    if code in FIFTH_SPECS:
        reason = fifth_unavailable_reason(code, timeframe, allow_retired=allow_retired)
        if reason:
            return reason
    missing = ENTRY_RULE_REQUIREMENTS.get(code, frozenset()) - {
        k for k, v in (capabilities or {}).items() if v}
    if missing:
        from data_sources import CAPABILITY_LABELS
        return "缺少该周期数据：" + "、".join(CAPABILITY_LABELS.get(k, k) for k in sorted(missing))
    return ""


def unavailable_entry_codes(codes, capabilities, timeframe="1m", *, allow_retired=False):
    return [int(c) for c in codes if entry_unavailable_reason(
        c, capabilities, timeframe, allow_retired=allow_retired)]



def extra_stop_components():
    rows = []
    for tf in ("1m", "5m", "15m", "1h", "4h"):
        for k in (.5, .75, 1., 1.5, 2., 2.5, 3.):
            rows.append((f"ATR{int(k*100)}_{tf}", f"ATR止损 {tf} {k:g}倍ATR14"))
        for ma in (20, 60, 120):
            for n in (1, 2, 3):
                rows.append((f"MA{ma}C{n}_{tf}", f"均线止损 {tf} 收盘反穿MA{ma}确认{n}根"))
    for n in (5, 10, 15, 30):
        rows.append((f"BAD{n}_1m", f"连续不利收盘止损 1m {n}根处于开仓价不利侧"))
    return rows


def stable_base_id(field_index, cases, stop_index):
    """稳定策略ID。

    0—4 继续使用第一批旧编号；所有case<32继续使用原base32扩展编号；
    含32—63进入原第三批base64命名空间；含64—127进入base128命名空间；含128—143进入新的base256命名空间。
    """
    if any(c < 0 for c in cases) or stop_index < 0:
        import hashlib
        import json
        if field_index not in (0, 1) or len(cases) != 5:
            raise ValueError("组合策略编号参数无效")
        payload = json.dumps(["indicator-combination-v1", field_index, list(cases), stop_index],
                             separators=(",", ":")).encode("utf-8")
        return -(int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 63) - 1) or 1)
    if all(0 <= c <= 4 for c in cases) and stop_index < 912:
        code = 0
        for value in cases:
            code = code * 5 + value
        return field_index * 3125 * 912 + code * 912 + stop_index + 1
    if not (field_index in (0, 1) and len(cases) == 5 and all(0 <= c < 512 for c in cases) and 0 <= stop_index < 8192):
        raise ValueError("策略编号超出预留范围")
    if all(c < 32 for c in cases):
        code = field_index
        for value in cases:
            code = code * 32 + value
        return 5_700_000 + code * 8192 + stop_index + 1
    if all(c < 64 for c in cases):
        code = field_index
        for value in cases:
            code = code * 64 + value
        # 与旧命名空间彻底隔离。
        return 20_000_000_000_000 + code * 8192 + stop_index + 1
    if all(c < 128 for c in cases):
        code = field_index
        for value in cases:
            code = code * 128 + value
        # 第三批追加64—127使用更高命名空间，不改写任何旧ID。
        return 40_000_000_000_000_000 + code * 8192 + stop_index + 1
    if any(c >= 256 for c in cases):
        code = field_index
        for value in cases:
            code = code * 512 + value
        return 200_000_000_000_000_000 + code * 8192 + stop_index + 1
    code = field_index
    for value in cases:
        code = code * 256 + value
    # 128+外部数据规则进入全新base256空间；所有旧策略ID保持不变。
    return 80_000_000_000_000_000 + code * 8192 + stop_index + 1


def composite_stop_index(code):
    """组合止损使用独立负编号；旧止损仍由冻结词典取得原索引。"""
    import hashlib
    if not str(code).startswith("@COMBO:"):
        raise ValueError("不是组合止损代码")
    return -(int.from_bytes(hashlib.sha256(str(code).encode("utf-8")).digest()[:8], "big")
             & ((1 << 63) - 1) or 1)
