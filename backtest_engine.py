from __future__ import annotations

import itertools
from functools import lru_cache

import os
import threading
from collections import OrderedDict

import numpy as np
from numba import njit

import engine as E
import run_all as R
from extended_rules import ENTRY_RULES, FIFTH_SPECS
from extended_signals import entry_masks, atr14
from entry_position import position_masks
from strategy_space import (周期列表, 仓位列表, 生成止损组合, 生成固定止损档,
                            生成叠加止盈档, 止盈方案)


N = E.n
CLOSE = E.close.astype(np.float64)
HIGH = E.high.astype(np.float64)
LOW = E.low.astype(np.float64)
SIZES = np.asarray(仓位列表, dtype=np.float64)
DAYS = (E.D["ct1"][-1] - E.D["openms"][0]) / 86_400_000.0
# 永续资金费每 8 小时结算一次（UTC 00:00 / 08:00 / 16:00）。
# FUNDING_CUM[i] = 截至第 i 根 1m K 线收盘为止，已经历的结算次数。
FUNDING_CUM = np.cumsum(((E.D["ct1"] % 28_800_000) == 0).astype(np.int64))
def entry_calendar(close_times):
    """收盘后开仓：会话与周末以可下单的UTC收盘时刻定档。"""
    hours = ((close_times // 3_600_000) % 24).astype(np.int64)
    # Unix epoch为星期四，Monday=0时偏移3。
    weekend = ((close_times // 86_400_000 + 3) % 7) >= 5
    return hours, weekend


HOUR_UTC, IS_WEEKEND = entry_calendar(E.D["ct1"])


@lru_cache(maxsize=8)
def session_mask(code: str):
    """会话过滤：只在选定的时段允许开仓，已有持仓不受影响。"""
    if code in ("", "ALL"):
        return np.ones(N, dtype=np.bool_)
    if code == "WEEKDAY":
        return ~IS_WEEKEND
    if code == "WEEKEND":
        return IS_WEEKEND.copy()
    if code == "ASIA":
        return (HOUR_UTC >= 0) & (HOUR_UTC < 8)
    if code == "EU":
        return (HOUR_UTC >= 7) & (HOUR_UTC < 16)
    if code == "US":
        return (HOUR_UTC >= 13) & (HOUR_UTC < 22)
    if code == "EX_THIN":
        return ~((HOUR_UTC >= 22) | (HOUR_UTC < 2))
    raise ValueError("未知会话代码: " + str(code))


OVERLAY_TP_DEFINITIONS = tuple(生成叠加止盈档())


@lru_cache(maxsize=4)
def overlay_tp_data(weekday_rate: float, weekend_rate: float):
    """叠加用的固定比例止盈价位。与所选止盈方案并行，谁先触发算谁。"""
    if weekday_rate <= 0.0 and weekend_rate <= 0.0:
        return None
    weekend = IS_WEEKEND
    rates = np.where(weekend, weekend_rate, weekday_rate).astype(np.float64)
    x_l, x_s = first_level_hits(HIGH, LOW, CLOSE, rates, rates)
    return x_l, x_s, CLOSE * (1.0 + rates), CLOSE * (1.0 - rates)


def apply_overlay_tp(tp_data, overlay):
    """把叠加止盈并进止盈数组。

    动态方案（移动/保本）的静态数组是"永不触发"，叠加后引擎里那段
    并行判定才真正生效；静态方案则取两者更早的一个。
    """
    if overlay is None:
        return tp_data
    x_l, x_s, p_l, p_s, mode, a, b = tp_data
    o_l, o_s, op_l, op_s = overlay
    x_l = np.asarray(x_l); x_s = np.asarray(x_s)
    p_l = np.asarray(p_l).copy(); p_s = np.asarray(p_s).copy()
    use_l = o_l < x_l
    use_s = o_s < x_s
    p_l[use_l] = op_l[use_l]
    p_s[use_s] = op_s[use_s]
    return (np.where(use_l, o_l, x_l), np.where(use_s, o_s, x_s), p_l, p_s, mode, a, b)


@lru_cache(maxsize=32)
def time_stop_data(minutes: int):
    """时间止损：持仓到点无论盈亏一律按收盘价离场。

    重要：样本尾部不足完整持仓分钟时必须返回 N=never，不能把目标时间
    截断到最后一根K线；旧实现会让尾部仓位提前退出，产生边界偏差。
    """
    if minutes <= 0:
        return None
    minutes = int(minutes)
    x = np.full(N, N, dtype=np.int64)
    if minutes < N:
        idx = np.arange(N - minutes, dtype=np.int64)
        x[idx] = idx + minutes
    safe = np.minimum(x, N - 1)
    price = CLOSE[safe]
    return x, x.copy(), price, price.copy()


_COMBINED_LOCK = threading.Lock()
_COMBINED_CACHE = OrderedDict()
def _combined_cache_budget():
    """按可用内存决定缓存几份合并后的止损数组。

    一份是 6 条 N 长度数组（int64/float64），N=86万时约 41MB。
    固定 4 份是我之前拍脑袋定的，遇到 combo 轮换就疯狂重算：
    实测缓存命中 2014 组/秒、抖动时只剩 49.6 组/秒，差 40 倍。
    这里按内存自适应，并允许用 BT_STOP_CACHE 覆盖。
    """
    override = os.environ.get("BT_STOP_CACHE")
    if override:
        try:
            return max(2, int(override))
        except ValueError:
            pass
    per_entry = max(1, N * 8 * 6)          # 6 条 N 长度的 8 字节数组
    budget = 1_500_000_000                 # 默认最多拿 1.5GB 来做这个缓存
    try:
        import psutil
        budget = min(budget, int(psutil.virtual_memory().available * 0.25))
    except Exception:
        pass
    return max(4, min(64, budget // per_entry))


_COMBINED_MAX = _combined_cache_budget()


def combined_stop_data(stop_data, fs_weekday, fs_weekend, hard_minutes, cache_key):
    """按需合并"信号止损 + 固定比例止损 + 强制时间止损"，并只保留几份。

    以前是在任务列表里给每个组合各存一份合并后的数组。一份是 6 条 N 长度的
    数组，N≈86万时约 41MB；入场12 × 止损11 就是 132 份 ≈ 5.4GB——正好是
    观察到的 6.3GB 占用，而且大部分时间都花在换页上，所以 CPU 反而跑不满。
    现在任务里只带 key，真正的数组用完就丢。
    """
    # 同名止损在hist和dif下是不同的数组。把来源对象与实际参数纳入键，
    # 并在缓存值中保留来源引用，防止释放后Python复用id导致错误命中。
    cache_key = (cache_key, id(stop_data), fs_weekday, fs_weekend, hard_minutes)
    with _COMBINED_LOCK:
        hit = _COMBINED_CACHE.get(cache_key)
        if hit is not None:
            _COMBINED_CACHE.move_to_end(cache_key)
            return hit[1]
    merged = merge_fixed_stop(stop_data, fixed_stop_data(fs_weekday, fs_weekend))
    merged = merge_extra_stop(merged, time_stop_data(hard_minutes))
    with _COMBINED_LOCK:
        _COMBINED_CACHE[cache_key] = (stop_data, merged)
        while len(_COMBINED_CACHE) > _COMBINED_MAX:
            _COMBINED_CACHE.popitem(last=False)
    return merged


@njit(cache=True, nogil=True)
def _merge_stop_side(x, price, extra_x, extra_price, long_side, set_risk, sentinel):
    merged_x = np.empty(len(x), dtype=np.int64)
    merged_price = np.empty_like(price)
    for i in range(len(x)):
        earlier = extra_x[i] < x[i]
        tie = (set_risk and extra_x[i] == x[i]
               and (extra_price[i] < price[i] if long_side else extra_price[i] > price[i]))
        merged_x[i] = min(extra_x[i] if earlier else x[i], sentinel - 1)
        merged_price[i] = extra_price[i] if earlier or tie else price[i]
    return merged_x, merged_price


def merge_extra_stop(stop_data, extra, set_risk=False):
    """把一条"开仓即知出场点"的止损并入信号型止损，谁先触发算谁。

    set_risk=True 时用它的价位当作 1R（固定比例止损用）；
    强制时间止损没有价位含义，不动 risk。
    """
    if extra is None:
        return stop_data
    x_l, x_s, p_l, p_s, risk_l, risk_s = stop_data
    ex_l, ex_s, ep_l, ep_s = extra
    x_l, p_l = _merge_stop_side(np.asarray(x_l), np.asarray(p_l), ex_l, ep_l, True, set_risk, N)
    x_s, p_s = _merge_stop_side(np.asarray(x_s), np.asarray(p_s), ex_s, ep_s, False, set_risk, N)
    return (x_l, x_s, p_l, p_s,
            ep_l.copy() if set_risk else risk_l,
            ep_s.copy() if set_risk else risk_s)
YEAR = np.asarray([np.datetime64(int(x), "ms").astype("datetime64[Y]").astype(int) + 1970
                   for x in E.D["openms"]], dtype=np.int16)
YEAR_VALUES = np.unique(YEAR)
YEAR_INDEX = np.searchsorted(YEAR_VALUES, YEAR).astype(np.int16)
ENTRY_CASE_SPACE = tuple(itertools.product((0, 1, 2, 3, 4), (0, 1, 2, 3, 4),
                                           (0, 1, 2, 3, 4), (0, 1, 2, 3, 4),
                                           (0, 1, 2, 3, 4)))
ENTRY_INDEX = {cases: index for index, cases in enumerate(ENTRY_CASE_SPACE)}
STOP_DEFINITIONS = tuple(生成止损组合())
STOP_INDEX = {code: index for index, (code, _components, _label) in enumerate(STOP_DEFINITIONS)}
FIXED_STOP_DEFINITIONS = tuple(生成固定止损档())
FIXED_STOP_INDEX = {code: index for index, (code, _wd, _we, _label)
                    in enumerate(FIXED_STOP_DEFINITIONS)}


def direction_run_id(direction: np.ndarray) -> np.ndarray:
    changed = np.ones(len(direction), dtype=np.bool_)
    changed[1:] = direction[1:] != direction[:-1]
    return np.cumsum(changed).astype(np.int32)


def macd_cycle_run_id(hist: np.ndarray) -> np.ndarray:
    """已收盘1m柱在零轴同侧为一轮；零值延续，前导零的轮号为0。"""
    hist = np.asarray(hist, dtype=np.float64)
    side = np.where(np.isfinite(hist), np.where(hist > 0, 1, np.where(hist < 0, -1, 0)), 0)
    if not len(side):
        return np.empty(0, dtype=np.int32)
    previous = np.maximum.accumulate(np.where(side != 0, np.arange(len(side)), 0))
    side = side[previous]
    changed = np.zeros(len(side), dtype=np.bool_)
    changed[0] = side[0] != 0
    changed[1:] = side[1:] != side[:-1]
    return np.cumsum(changed).astype(np.int32)


def macd_full_cycle_run_id(hist: np.ndarray, start_side: int) -> np.ndarray:
    """完整红绿轮次：只在回到固定起点色时重置，多空共享该轮的一次入场。

    红起点为+1、绿起点为-1；零和非有限值延续上一个有效颜色。
    样本首段可能被截断，必须先观察到相反色→起点色才开启第一轮。
    非有限值自身不能入场，由build_field的有效柱闸门排除。
    """
    if start_side not in (-1, 1):
        raise ValueError("完整MACD轮次起点必须为红色(1)或绿色(-1)")
    hist = np.asarray(hist, dtype=np.float64)
    side = np.where(np.isfinite(hist), np.sign(hist), 0).astype(np.int8)
    if not len(side):
        return np.empty(0, dtype=np.int32)
    previous = np.maximum.accumulate(np.where(side != 0, np.arange(len(side)), 0))
    side = side[previous]
    changed = np.zeros(len(side), dtype=np.bool_)
    changed[1:] = (side[:-1] == -start_side) & (side[1:] == start_side)
    return np.cumsum(changed).astype(np.int32)


def s3_baseline_gate(EX, timeframe: str, gap: float):
    """最小S3距离入场闸门。

    多单要求收盘价 >= 基线 x (1+gap)，空单要求收盘价 <= 基线 x (1-gap)。
    基线已被越过、距离不足、基线不可用（尚无已确认的MACD极值）一律禁止开仓。
    gap<=0 表示关闭该闸门。
    """
    if gap <= 0.0:
        ones = np.ones(N, dtype=np.bool_)
        return ones, ones
    level_long, level_short = EX[f"LVL_{timeframe}"]
    available_long = np.isfinite(level_long) & (level_long > 0.0)
    available_short = np.isfinite(level_short) & (level_short < 1e17)
    long_ok = available_long & (CLOSE >= level_long * (1.0 + gap))
    short_ok = available_short & (CLOSE <= level_short * (1.0 - gap))
    return long_ok, short_ok


@lru_cache(maxsize=4096)
def cases_use_fifth(cases):
    from indicator_combinations import entry_members
    return any(member in FIFTH_SPECS for code in cases for member in entry_members(code))


def effective_entry_mode(cases, entry_mode):
    return "F5_EVENT" if cases_use_fifth(tuple(cases)) else entry_mode


@lru_cache(maxsize=1)
def _fifth_event_run_ids(length):
    ids = np.arange(length, dtype=np.int32)
    ids.flags.writeable = False
    return ids


def entry_run_id_for_cases(cases, legacy_run_id):
    """F5事件不共用MACD方向段限次；旧规则继续使用原限次数组。"""
    return _fifth_event_run_ids(len(legacy_run_id)) if cases_use_fifth(tuple(cases)) else legacy_run_id


def build_candidates(S, HI, cases, gate=None, direction="BOTH", session="ALL",
                     entry_mode="LIVE_01"):
    c4, c1, c15, c5, cm = cases
    fifth = cases_use_fifth(tuple(cases))
    entry_mode = effective_entry_mode(cases, entry_mode)
    if cm < 0:
        from indicator_combinations import entry_members, entry_logic
        if entry_logic(cm) != "AND":
            raise ValueError("开仓指标组合必须同时满足")
        long_ok = np.ones(N, dtype=np.bool_)
        short_ok = np.ones(N, dtype=np.bool_)
        for member in entry_members(cm):
            cand, sides = build_candidates(S, HI, (c4, c1, c15, c5, member),
                                          gate, direction, session, entry_mode)
            ml = np.zeros(N, dtype=np.bool_); ms = ml.copy()
            ml[cand[sides > 0]] = True; ms[cand[sides < 0]] = True
            long_ok &= ml; short_ok &= ms
        cand = np.flatnonzero(long_ok | short_ok).astype(np.int64)
        return cand, np.where(long_ok[cand], 1, -1).astype(np.int8)
    d1 = S["d1"]
    ma = S["ma120"]
    long_trigger = d1 == 1
    short_trigger = d1 == -1
    if entry_mode == "TF_EVENT":
        flip = np.zeros(N, dtype=np.bool_)
        flip[1:] = d1[1:] != d1[:-1]
        long_trigger = long_trigger & flip
        short_trigger = short_trigger & flip
    if cm == 0:
        if entry_mode == "TF_EVENT":
            # 研究事件口径：最低的已启用高周期作为唯一触发周期，其余更高周期
            # 只做状态过滤。情况三/四每次原生MACD反转最多触发一次。
            trigger = next(((tf, code) for tf, code in
                            (("5m", c5), ("15m", c15), ("1h", c1), ("4h", c4))
                            if code != 0), None)
            if trigger is None:
                long_trigger = np.zeros(N, dtype=np.bool_)
                short_trigger = np.zeros(N, dtype=np.bool_)
            else:
                tf, code = trigger
                if code < 0:
                    from indicator_combinations import entry_members
                    long_trigger = np.ones(N, dtype=np.bool_)
                    short_trigger = np.ones(N, dtype=np.bool_)
                    for member in entry_members(code):
                        if member in (3, 4):
                            long_trigger &= S[f"{tf}_entry{member}_long"]
                            short_trigger &= S[f"{tf}_entry{member}_short"]
                        else:
                            long_trigger &= S[tf + "_close_event"]
                            short_trigger &= S[tf + "_close_event"]
                elif code == 3:
                    long_trigger = S[tf + "_entry3_long"]
                    short_trigger = S[tf + "_entry3_short"]
                elif code == 4:
                    long_trigger = S[tf + "_entry4_long"]
                    short_trigger = S[tf + "_entry4_short"]
                else:
                    long_trigger = S[tf + "_close_event"]
                    short_trigger = long_trigger
        else:
            # 01实盘兼容口径：高周期情况三/四是锁存状态；1分钟不启用时，
            # 方向由高周期给出，再由run_id限制每个1分钟MACD方向段最多开一次。
            long_trigger = np.ones(N, dtype=np.bool_)
            short_trigger = np.ones(N, dtype=np.bool_)
    elif cm == 2:
        long_trigger = long_trigger & (CLOSE > ma)
        short_trigger = short_trigger & (CLOSE < ma)
    elif cm == 3:
        long_trigger = S["1m_highvol_long"]
        short_trigger = S["1m_highvol_short"]
    elif cm == 4:
        # 量能充足顺反转方向 + 量能不足反向，四个分句都要覆盖。
        long_trigger = S["1m_highvol_long"] | S["1m_lowvol_short"]
        short_trigger = S["1m_highvol_short"] | S["1m_lowvol_long"]
    elif cm in ENTRY_RULES:
        long_trigger, short_trigger = S[f"extra_{cm}"]
        if entry_mode == "TF_EVENT":
            flip = np.zeros(N, dtype=np.bool_)
            flip[1:] = d1[1:] != d1[:-1]
            long_trigger = long_trigger & flip
            short_trigger = short_trigger & flip
    if fifth:
        # OFF是中性条件，不能借用旧MACD的200根预热门卫压掉F5自主事件。
        # 非零旧规则仍使用其原掩码，保留用户明确勾选的MACD/预热过滤。
        long_ok, short_ok = long_trigger.copy(), short_trigger.copy()
        for tf, code in (("4h", c4), ("1h", c1), ("15m", c15), ("5m", c5)):
            if code != 0:
                long_ok &= HI[tf][code][0]
                short_ok &= HI[tf][code][1]
    else:
        long_ok = HI["4h"][c4][0] & HI["1h"][c1][0] & HI["15m"][c15][0] & HI["5m"][c5][0] & long_trigger
        short_ok = HI["4h"][c4][1] & HI["1h"][c1][1] & HI["15m"][c15][1] & HI["5m"][c5][1] & short_trigger
    if cm == 0 or fifth:
        # 方向只能来自高周期；同一触发时刻若多空同时被允许，等于没有方向。
        ambiguous = long_ok & short_ok
        long_ok = long_ok & ~ambiguous
        short_ok = short_ok & ~ambiguous
    if gate is not None:
        long_ok = long_ok & gate[0]
        short_ok = short_ok & gate[1]
    if direction == "LONG":
        short_ok = np.zeros(N, dtype=np.bool_)
    elif direction == "SHORT":
        long_ok = np.zeros(N, dtype=np.bool_)
    if session not in ("", "ALL"):
        allow = session_mask(session)
        long_ok = long_ok & allow
        short_ok = short_ok & allow
    cand = np.flatnonzero(long_ok | short_ok).astype(np.int64)
    cdir = np.where(long_ok[cand], 1, -1).astype(np.int8)
    return cand, cdir


def component_exit(EX, component, long_side):
    side = 0 if long_side else 1
    if component.startswith(("ATR", "MA", "BAD")):
        data = extended_stop_data(component)
        return data[side], data[side + 2], component.startswith("ATR")
    if component.startswith("S3_"):
        tf = component[3:]
        raw = EX[component][side].astype(np.int64)
        level = EX[f"LVL_{tf}"][side].astype(np.float64)
        valid = level < CLOSE if long_side else level > CLOSE
        idx = np.where(valid, raw, N).astype(np.int64)
        return idx, level, True
    idx = EX[component][side].astype(np.int64)
    price = CLOSE[np.minimum(idx, N - 1)]
    return idx, price, False


@njit(cache=True, nogil=True)
def _update_stop_component(x, price, idx, component_price, intrabar, sentinel):
    for i in range(len(x)):
        if idx[i] < sentinel and (idx[i] <= x[i] if intrabar else idx[i] < x[i]):
            x[i] = idx[i]
            price[i] = component_price[i]


def make_stop_data(EX, components):
    x_long = np.full(N, N, dtype=np.int64)
    x_short = np.full(N, N, dtype=np.int64)
    p_long = np.full(N, CLOSE[-1], dtype=np.float64)
    p_short = np.full(N, CLOSE[-1], dtype=np.float64)
    risk_long = np.full(N, np.nan, dtype=np.float64)
    risk_short = np.full(N, np.nan, dtype=np.float64)
    for component in components:
        if component.startswith("ATR"):
            data = extended_stop_data(component)
            risk_long, risk_short = data[2], data[3]
        if component.startswith("S3_"):
            level_long, level_short = EX[f"LVL_{component[3:]}"]
            valid_long = (level_long > 0) & (level_long < CLOSE)
            valid_short = np.isfinite(level_short) & (level_short > CLOSE)
            risk_long = np.where(valid_long, level_long, risk_long)
            risk_short = np.where(valid_short, level_short, risk_short)
        for long_side, x, price in ((True, x_long, p_long), (False, x_short, p_short)):
            idx, px, intrabar = component_exit(EX, component, long_side)
            _update_stop_component(x, price, idx, px, intrabar, N)
    x_long = np.minimum(x_long, N - 1)
    x_short = np.minimum(x_short, N - 1)
    return x_long, x_short, p_long, p_short, risk_long, risk_short


@lru_cache(maxsize=8)
def cached_signal_stop(field, components):
    return make_stop_data(R.build(field)[1], components)


@lru_cache(maxsize=2)
def build_field(field: str, selected_cases: tuple = (), selected_stop_codes: tuple = (),
                s3_gap: float = 0.0, s3_timeframe: str = "1m",
                direction: str = "BOTH", session: str = "ALL",
                materialize_stops: bool = True, entry_mode: str = "LIVE_01",
                position_filter: str = "OFF"):
    S, EX, HI, _ = R.build(field)
    # 只计算本次勾选项；不要为全笛卡尔积提前生成几百万组信号。
    wanted = selected_cases or ENTRY_CASE_SPACE
    if any(code < 0 for cases in wanted for code in cases[:4]):
        # 原生规则缓存可共享；本批组合掩码不能写回R.build的常驻字典。
        HI = {tf: dict(values) for tf, values in HI.items()}
    for pos, tf in enumerate(("4h", "1h", "15m", "5m", "1m")):
        from indicator_combinations import entry_members, entry_logic
        requested = {cases[pos] for cases in wanted}
        atoms = {member for code in requested for member in entry_members(code)}
        for code in atoms & ENTRY_RULES.keys():
            masks = extended_entry_data(tf, field, code)
            if tf == "1m":
                S[f"extra_{code}"] = masks
            else:
                HI[tf][code] = masks
        if tf != "1m":
            for code in requested:
                if code >= 0:
                    continue
                if entry_logic(code) != "AND":
                    raise ValueError("开仓指标组合必须同时满足")
                members = entry_members(code)
                HI[tf][code] = tuple(np.logical_and.reduce([HI[tf][member][side]
                                                          for member in members])
                                     for side in (0, 1))
    if entry_mode == "MACD_CYCLE":
        # 周期边界始终取hist=2*(DIF-DEA)的零轴侧，不随开仓指标hist/dif改变。
        run_id = macd_cycle_run_id(E.D["m1_hist"])
    elif entry_mode in ("MACD_FULL_RED", "MACD_FULL_GREEN"):
        run_id = macd_full_cycle_run_id(E.D["m1_hist"], 1 if entry_mode == "MACD_FULL_RED" else -1)
    else:
        run_id = (np.arange(N, dtype=np.int32) if entry_mode == "TF_EVENT"
                  else direction_run_id(S["d1"]))
    if wanted and all(cases_use_fifth(tuple(cases)) for cases in wanted):
        run_id = _fifth_event_run_ids(N)
    gate = s3_baseline_gate(EX, s3_timeframe, s3_gap)
    if position_filter != "OFF":
        position_gate = entry_position_gate(position_filter)
        gate = (position_gate if gate is None else
                (gate[0] & position_gate[0], gate[1] & position_gate[1]))
    case_filter = set(selected_cases)
    entries = []
    for cases in wanted:
        if case_filter and cases not in case_filter:
            continue
        case_gate = gate
        if entry_mode in ("MACD_CYCLE", "MACD_FULL_RED", "MACD_FULL_GREEN") and not cases_use_fifth(tuple(cases)):
            valid_cycle = (run_id > 0) & np.isfinite(E.D["m1_hist"])
            case_gate = ((valid_cycle, valid_cycle) if gate is None else
                         (gate[0] & valid_cycle, gate[1] & valid_cycle))
        cand, cdir = build_candidates(S, HI, cases, case_gate, direction, session, entry_mode)
        entries.append((cases, cand, cdir))
    stop_filter = set(selected_stop_codes)
    stops = [(code, label, cached_signal_stop(field, components) if materialize_stops else None)
             for code, components, label in STOP_DEFINITIONS
             if not stop_filter or code in stop_filter]
    return S, EX, run_id, entries, stops


@lru_cache(maxsize=13)
def entry_position_gate(code: str):
    masks = position_masks(code, E.D)
    for mask in masks:
        mask.flags.writeable = False
    return masks


_fifth_signal_cache = None


def set_fifth_signal_cache(path):
    global _fifth_signal_cache
    _fifth_signal_cache = path
    extended_entry_data.cache_clear()
    build_field.cache_clear()


def _map_fifth_signals(tf, masks):
    values = E.D[f"{tf}_close"]
    if not len(values):
        return np.zeros(N, dtype=np.bool_), np.zeros(N, dtype=np.bool_)
    mp = E.D[f"{tf}_map"]
    safe = np.clip(mp, 0, len(values) - 1)
    valid = ((mp >= 0) & (mp < len(values)) &
             (E.D[f"{tf}_ct"][safe] == E.D["ct1"]))
    return tuple(np.asarray(mask, dtype=np.bool_)[safe] & valid for mask in masks)


@lru_cache(maxsize=32)
def extended_entry_data(tf, field, code):
    from fifth_policy import require_entry_allowed
    require_entry_allowed(code)
    if code in FIFTH_SPECS and field != "hist":
        # F5独立版不取MACD口径；双口径比较只影响明确选择的旧过滤/退出。
        return extended_entry_data(tf, "hist", code)
    d = E.D
    values = d[f"{tf}_{field}"]
    if code in FIFTH_SPECS and _fifth_signal_cache is not None:
        from fifth_precompute import load_signals
        return _map_fifth_signals(tf, load_signals(_fifth_signal_cache, tf, code, len(values)))
    extras = {
        "taker_buy_ratio": d.get(f"{tf}_taker_buy_ratio", np.full(len(values), np.nan)),
        "delta_base": d.get(f"{tf}_delta_base", np.full(len(values), np.nan)),
        "trades": d.get(f"{tf}_trades", np.full(len(values), np.nan)),
        "avg_trade_size": d.get(f"{tf}_avg_trade_size", np.full(len(values), np.nan)),
        "quote_volume": d.get(f"{tf}_quote_volume", np.full(len(values), np.nan)),
        "taker_buy_quote_ratio": d.get(f"{tf}_taker_buy_quote_ratio", np.full(len(values), np.nan)),
        "delta_quote": d.get(f"{tf}_delta_quote", np.full(len(values), np.nan)),
        "taker_buy_base": d.get(f"{tf}_taker_buy_base", np.full(len(values), np.nan)),
        "taker_buy_quote": d.get(f"{tf}_taker_buy_quote", np.full(len(values), np.nan)),
        "funding_rate": d.get(f"{tf}_funding_rate", np.full(len(values), np.nan)),
        "open_interest": d.get(f"{tf}_open_interest", np.full(len(values), np.nan)),
        "open_interest_value": d.get(f"{tf}_open_interest_value", np.full(len(values), np.nan)),
        "hist": d.get(f"{tf}_hist", np.full(len(values), np.nan)),
        "dif": d.get(f"{tf}_dif", np.full(len(values), np.nan)),
        "dea": d.get(f"{tf}_dea", np.full(len(values), np.nan)),
        "ma120": d.get(f"{tf}_ma120", np.full(len(values), np.nan)),
    }
    masks = entry_masks(code, E.direction(values), values, d[f"{tf}_open"], d[f"{tf}_high"],
                        d[f"{tf}_low"], d[f"{tf}_close"], d[f"{tf}_ma20"],
                        volume=d[f"{tf}_volume"], close_times=d[f"{tf}_ct"], extras=extras, timeframe=tf)
    mp = d[f"{tf}_map"]
    if code in FIFTH_SPECS:
        # 原生信号只在该桶收盘时可见；后续1m不能复用同一事件。
        return _map_fifth_signals(tf, masks)
    # 同步现有MACD预热下限；缺失均线、未完成高周期桶一律不放行。
    valid = (mp >= 35) & (np.arange(N) >= 200)
    safe = np.maximum(mp, 0)
    return tuple(mask[safe] & valid for mask in masks)


@lru_cache(maxsize=5)
def mapped_atr(tf):
    d = E.D
    native = atr14(d[f"{tf}_high"], d[f"{tf}_low"], d[f"{tf}_close"])
    mp = d[f"{tf}_map"]
    return np.where(mp >= 13, native[np.maximum(mp, 0)], np.nan)


# All 84 component definitions are reused by thousands of composite stops.
# Bound their four int64/float64 arrays to 2.4 GB, also on larger datasets.
@lru_cache(maxsize=min(84, 2_400_000_000 // max(1, N * 4 * 8)))
def extended_stop_data(component):
    rule, tf = component.split("_")
    if rule.startswith("ATR"):
        distance = mapped_atr(tf) * (int(rule[3:]) / 100.)
        p_l, p_s = CLOSE - distance, CLOSE + distance
        valid = np.isfinite(distance) & (distance > 0)
        x_l = np.where(valid & (p_l > 0), E.first_below(LOW, np.where(valid, p_l, -np.inf)), N)
        x_s = np.where(valid, E.first_above(HIGH, np.where(valid, p_s, np.inf)), N)
        return x_l, x_s, p_l, p_s
    if rule.startswith("BAD"):
        import pandas as pd
        count = int(rule[3:])
        if N <= count:
            never = np.full(N, N, dtype=np.int64)
            return never, never.copy(), CLOSE.copy(), CLOSE.copy()
        # 将窗口尾位置平移，使first_*的严格下一根搜索等价于j>=entry+count。
        # 比较的是开仓价，不是与前一根涨跌；所有确认收盘都在入场之后。
        roll_high = pd.Series(CLOSE).rolling(count).max().to_numpy()
        roll_low = pd.Series(CLOSE).rolling(count).min().to_numpy()
        window_high = np.full(N, np.inf)
        window_low = np.full(N, -np.inf)
        window_high[:N-count+1] = roll_high[count-1:]
        window_low[:N-count+1] = roll_low[count-1:]
        x_l = np.minimum(E.first_below(window_high, np.nextafter(CLOSE, -np.inf)) + count - 1, N)
        x_s = np.minimum(E.first_above(window_low, np.nextafter(CLOSE, np.inf)) + count - 1, N)
        return x_l, x_s, CLOSE[np.minimum(x_l, N-1)], CLOSE[np.minimum(x_s, N-1)]
    from research_signals import ma_cross
    import pandas as pd
    close_tf = E.D[f"{tf}_close"]
    period, confirm = map(int, rule[2:].split("C"))
    key = f"{tf}_ma{period}"
    ma = E.D[key] if key in E.D else pd.Series(close_tf).rolling(period).mean().to_numpy()
    events = ma_cross(close_tf, ma, confirm)
    mp = E.D[f"{tf}_map"]
    out = []
    for event in events:
        # 连续不利收盘所需的N根确认均须在入场之后收盘。
        native_i = next_true(event)[np.minimum(np.maximum(mp + confirm, 0), len(event))]
        valid = native_i < len(event)
        ix = np.full(N, N, dtype=np.int64)
        ix[valid] = np.searchsorted(E.D["ct1"], E.D[f"{tf}_ct"][native_i[valid]])
        out.append(ix)
    return out[0], out[1], CLOSE[np.minimum(out[0], N-1)], CLOSE[np.minimum(out[1], N-1)]


def _tf_to_minute_events(tf: str, mask: np.ndarray) -> np.ndarray:
    events = np.zeros(N, dtype=np.bool_)
    if tf == "1m":
        events[:len(mask)] = mask
        return events
    close_times = E.D[f"{tf}_ct"]
    one_i = np.searchsorted(E.D["ct1"], close_times, side="left")
    valid = (one_i >= 0) & (one_i < N) & mask
    events[one_i[valid]] = True
    return events


def _consecutive(mask: np.ndarray, count: int) -> np.ndarray:
    out = mask.copy()
    for shift in range(1, count):
        tmp = np.zeros(len(mask), dtype=np.bool_)
        tmp[shift:] = mask[:-shift]
        out &= tmp
    return out


@njit(cache=True, nogil=True)
def _excursion_kernel(deviation, threshold):
    out = np.zeros(len(deviation), dtype=np.bool_)
    peak = 0.0
    for i in range(len(deviation)):
        value = deviation[i]
        if not np.isfinite(value) or value <= 0.0:
            peak = 0.0
        else:
            if value > peak:
                peak = value
            out[i] = peak >= threshold
    return out


def _excursion_active(deviation: np.ndarray, threshold: float) -> np.ndarray:
    """只在当前离开均线的同一次偏离段内记忆峰值；回到均线即重置。

    原来是 86 万次的纯Python循环，实测 0.62 秒一次，MA偏离回归止盈每个
    方案要调两次；搬进 njit 后是毫秒级，逻辑一字未改。
    """
    return _excursion_kernel(np.ascontiguousarray(deviation, dtype=np.float64),
                             float(threshold))


@njit(cache=True, nogil=True)
def _divergence_flags(values, hi_tf, lo_tf, lookback):
    """回看窗口内的顶背离/底背离标记。

    原来在Python里对每根K线切片调用 np.argmax/np.argmin，一个方案要调
    260万次，实测 1.8 秒。这里用等价的显式循环，取第一个极值，
    与 np.argmax/np.argmin 的口径一致。
    """
    n = len(values)
    bear = np.zeros(n, dtype=np.bool_)
    bull = np.zeros(n, dtype=np.bool_)
    for j in range(lookback, n):
        hi_idx = j - lookback
        lo_idx = j - lookback
        for k in range(j - lookback + 1, j):
            if hi_tf[k] > hi_tf[hi_idx]:
                hi_idx = k
            if lo_tf[k] < lo_tf[lo_idx]:
                lo_idx = k
        bear[j] = hi_tf[j] >= hi_tf[hi_idx] and values[j] < values[hi_idx]
        bull[j] = lo_tf[j] <= lo_tf[lo_idx] and values[j] > values[lo_idx]
    return bear, bull


@lru_cache(maxsize=256)
def signal_events(periods: str, indicator: str, confirm: int, kind: str,
                  activation: float = 0.0, lookback: int = 0):
    long_events = np.zeros(N, dtype=np.bool_)
    short_events = np.zeros(N, dtype=np.bool_)
    for tf in periods.split("+"):
        if not tf:
            continue
        values = E.D[f"{tf}_{indicator}"] if indicator in ("hist", "dif") else None
        if kind == "macd":
            down = np.zeros(len(values), dtype=np.bool_)
            up = np.zeros(len(values), dtype=np.bool_)
            down[1:] = values[1:] < values[:-1]
            up[1:] = values[1:] > values[:-1]
            e_long, e_short = _consecutive(down, confirm), _consecutive(up, confirm)
        elif kind == "ma":
            close_tf = E.D[f"{tf}_close"]
            ma = E.D[f"{tf}_{indicator.lower()}"]
            long_dev = close_tf / ma - 1.0
            short_dev = ma / close_tf - 1.0
            shrink_long = np.zeros(len(ma), dtype=np.bool_)
            shrink_short = np.zeros(len(ma), dtype=np.bool_)
            shrink_long[1:] = long_dev[1:] < long_dev[:-1]
            shrink_short[1:] = short_dev[1:] < short_dev[:-1]
            active_long = _excursion_active(long_dev, activation)
            active_short = _excursion_active(short_dev, activation)
            e_long = _consecutive(shrink_long, confirm) & active_long & (close_tf > ma)
            e_short = _consecutive(shrink_short, confirm) & active_short & (close_tf < ma)
        elif kind == "divergence":
            close_tf = E.D[f"{tf}_close"]
            hi_tf = E.D[f"{tf}_high"]
            lo_tf = E.D[f"{tf}_low"]
            bear, bull = _divergence_flags(
                np.ascontiguousarray(values, dtype=np.float64),
                np.ascontiguousarray(hi_tf, dtype=np.float64),
                np.ascontiguousarray(lo_tf, dtype=np.float64),
                int(lookback))
            down = np.zeros(len(values), dtype=np.bool_); down[1:] = values[1:] < values[:-1]
            up = np.zeros(len(values), dtype=np.bool_); up[1:] = values[1:] > values[:-1]
            e_long = bear & _consecutive(down, confirm)
            e_short = bull & _consecutive(up, confirm)
        else:
            raise ValueError(kind)
        long_events |= _tf_to_minute_events(tf, e_long)
        short_events |= _tf_to_minute_events(tf, e_short)
    return long_events, short_events


@njit(cache=True)
def next_true(mask):
    n = len(mask)
    out = np.full(n + 1, n, dtype=np.int64)
    nxt = n
    for i in range(n - 1, -1, -1):
        if mask[i]:
            nxt = i
        out[i] = nxt
    return out


def first_level_hits(high, low, close, long_rate, short_rate):
    target_l = close * (1.0 + long_rate)
    target_s = close * (1.0 - short_rate)
    return E.first_above(high, target_l), E.first_below(low, target_s)


@lru_cache(maxsize=10)
def fixed_rate_data(rate: float):
    rates = np.full(N, rate, dtype=np.float64)
    x_l, x_s = first_level_hits(HIGH, LOW, CLOSE, rates, rates)
    return x_l, x_s, CLOSE * (1.0 + rate), CLOSE * (1.0 - rate)


@njit(cache=True, nogil=True)
def profitable_signal_exits(close, next_l, next_s):
    """每个时刻之后第一个"有浮盈"的信号出场点。

    原来顺着 next_true 链一步步往后跳，信号密时每根K线都要走很远，
    实测一个 MACD反转止盈方案要 2.8 秒，8280 个方案里这一步就占半小时。
    改成从右往左维护单调栈：栈里只留"往后看的严格前缀极值"——多单留
    前缀最大值、空单留前缀最小值，其余的位置更靠后且价格更差，永远不
    可能先被选中。栈内价格随下标单调，查询时二分找最靠上（位置最小）
    的那个即可。输出与逐步跳链逐元素完全一致。
    """
    n = len(close)
    x_l = np.full(n, n, dtype=np.int64)
    x_s = np.full(n, n, dtype=np.int64)
    lp = np.empty(n, dtype=np.int64); lc = np.empty(n, dtype=np.float64); ltop = -1
    sp = np.empty(n, dtype=np.int64); sc = np.empty(n, dtype=np.float64); stop = -1
    for i in range(n - 1, -1, -1):
        price = close[i]
        # 多单：栈内价格随下标递减，找最大的下标使价格仍高于当前收盘价。
        lo = 0; hi = ltop; found = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if lc[mid] > price:
                found = mid; lo = mid + 1
            else:
                hi = mid - 1
        if found >= 0:
            x_l[i] = lp[found]
        # 空单：栈内价格随下标递增，找最大的下标使价格仍低于当前收盘价。
        lo = 0; hi = stop; found = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if sc[mid] < price:
                found = mid; lo = mid + 1
            else:
                hi = mid - 1
        if found >= 0:
            x_s[i] = sp[found]
        if next_l[i] == i:
            while ltop >= 0 and lc[ltop] <= price:
                ltop -= 1
            ltop += 1; lp[ltop] = i; lc[ltop] = price
        if next_s[i] == i:
            while stop >= 0 and sc[stop] >= price:
                stop -= 1
            stop += 1; sp[stop] = i; sc[stop] = price
    return x_l, x_s


@lru_cache(maxsize=16)
def fixed_stop_data(weekday_rate: float, weekend_rate: float):
    """固定比例止损：按开仓时刻的UTC星期定档，整笔订单不中途换档。

    多单止损价 = 开仓价 x (1 - 比例)，空单 = 开仓价 x (1 + 比例)。
    用1分钟盘中最低/最高价触碰判定，与③前轮极值止损同口径。
    """
    if weekday_rate <= 0.0 and weekend_rate <= 0.0:
        return None
    weekend = IS_WEEKEND
    rates = np.where(weekend, weekend_rate, weekday_rate).astype(np.float64)
    level_l = CLOSE * (1.0 - rates)
    level_s = CLOSE * (1.0 + rates)
    x_l = E.first_below(LOW, level_l)
    x_s = E.first_above(HIGH, level_s)
    return x_l, x_s, level_l, level_s


def merge_fixed_stop(stop_data, fixed):
    """兼容旧调用名，等价于 merge_extra_stop(..., set_risk=True)。"""
    """把固定比例止损并入信号型止损，谁先触发算谁。

    同一根K线两者都命中时取对交易者更不利的价位（保守）。
    1R 改用固定止损距离，因为它开仓即确定，比结构位更可靠。
    """
    return merge_extra_stop(stop_data, fixed, set_risk=True)


def simple_tp_data(tp: 止盈方案):
    """返回(xL,xS,pL,pS,mode,p1,p2)。mode 0=数组退出，2=移动，3=保本。"""
    n_arr = np.full(N, N, dtype=np.int64)
    last = np.full(N, CLOSE[-1], dtype=np.float64)
    if tp.类别 == "ATR倍数止盈":
        distance = mapped_atr(tp.周期组合) * tp.参数一
        valid = np.isfinite(distance) & (distance > 0)
        p_l, p_s = CLOSE + distance, CLOSE - distance
        x_l = np.where(valid, E.first_above(HIGH, np.where(valid, p_l, np.inf)), N)
        x_s = np.where(valid & (p_s > 0), E.first_below(LOW, np.where(valid, p_s, -np.inf)), N)
        return x_l, x_s, p_l, p_s, 0, 0., 0.
    if tp.类别 in ("均线穿越止盈", "缩量止盈"):
        from research_signals import ma_cross, volume_fade
        tf = tp.周期组合
        if tp.类别 == "均线穿越止盈":
            ev_l, ev_s = ma_cross(E.D[f"{tf}_close"], E.D[f"{tf}_{tp.指标.lower()}"], int(tp.参数一))
        else:
            ev_l = volume_fade(E.D[f"{tf}_volume"], tp.参数一, int(tp.参数二))
            ev_s = ev_l
        # 高周期必须在其收盘后才产生事件；盈利判据沿用信号止盈的毛浮盈口径。
        x_l, x_s = profitable_signal_exits(CLOSE, next_true(_tf_to_minute_events(tf, ev_l)),
                                         next_true(_tf_to_minute_events(tf, ev_s)))
        return x_l, x_s, CLOSE[np.minimum(x_l, N - 1)], CLOSE[np.minimum(x_s, N - 1)], 0, 0.0, 0.0
    if tp.类别 == "不使用止盈":
        return n_arr, n_arr.copy(), last, last.copy(), 0, 0.0, 0.0
    if tp.类别.startswith("固定比例"):
        if tp.类别.endswith("周末相同"):
            x_l, x_s, p_l, p_s = fixed_rate_data(tp.参数一)
        else:
            w_l, w_s, wp_l, wp_s = fixed_rate_data(tp.参数一)
            e_l, e_s, ep_l, ep_s = fixed_rate_data(tp.参数二)
            weekend = IS_WEEKEND
            x_l = np.where(weekend, e_l, w_l); x_s = np.where(weekend, e_s, w_s)
            p_l = np.where(weekend, ep_l, wp_l); p_s = np.where(weekend, ep_s, wp_s)
        return x_l, x_s, p_l, p_s, 0, 0.0, 0.0
    if tp.类别 in ("MACD反转止盈", "MA偏离回归止盈", "经典背离止盈"):
        if tp.类别 == "MACD反转止盈":
            ev_l, ev_s = signal_events(tp.周期组合, tp.指标, int(tp.参数一), "macd")
        elif tp.类别 == "MA偏离回归止盈":
            ev_l, ev_s = signal_events(tp.周期组合, tp.指标, int(tp.参数一), "ma", tp.参数二)
        else:
            ev_l, ev_s = signal_events(tp.周期组合, tp.指标, int(tp.参数二), "divergence", lookback=int(tp.参数一))
        x_l, x_s = profitable_signal_exits(CLOSE, next_true(ev_l), next_true(ev_s))
        return x_l, x_s, CLOSE[np.minimum(x_l, N - 1)], CLOSE[np.minimum(x_s, N - 1)], 0, 0.0, 0.0
    if tp.类别 == "时间止盈":
        minutes = int(tp.参数一)
        x_l = np.full(N, N, dtype=np.int64); x_s = x_l.copy()
        idx = np.arange(N - minutes)
        target = idx + minutes
        x_l[idx[CLOSE[target] > CLOSE[idx]]] = target[CLOSE[target] > CLOSE[idx]]
        x_s[idx[CLOSE[target] < CLOSE[idx]]] = target[CLOSE[target] < CLOSE[idx]]
        return x_l, x_s, CLOSE[np.minimum(x_l, N - 1)], CLOSE[np.minimum(x_s, N - 1)], 0, 0.0, 0.0
    if tp.类别 == "移动止盈":
        mode = 4 if tp.指标 == "最高价固定回撤" else 2
        return n_arr, n_arr.copy(), last, last.copy(), mode, tp.参数一, tp.参数二
    if tp.类别 == "保本移动止盈":
        return n_arr, n_arr.copy(), last, last.copy(), 3, tp.参数一, tp.参数二
    return None


def structure_tp_data(tp: 止盈方案):
    x_l = np.full(N, N, dtype=np.int64); x_s = x_l.copy()
    p_l = np.full(N, CLOSE[-1], dtype=np.float64); p_s = p_l.copy()
    advance = tp.参数一
    for tf in tp.周期组合.split("+"):
        values = E.D[f"{tf}_hist"]
        direction = E.direction(values)
        trough, peak = E.prev_pivot(direction)
        if tf == "1m":
            mp = np.arange(N)
        else:
            mp = E.D[f"{tf}_map"]
        safe = np.maximum(mp, 0)
        tr = trough[safe]; pk = peak[safe]
        hi = E.D[f"{tf}_high"]; lo = E.D[f"{tf}_low"]
        level_l = np.where((mp >= 0) & (pk >= 0), hi[np.maximum(pk, 0)] * (1.0 - advance), np.inf)
        level_s = np.where((mp >= 0) & (tr >= 0), lo[np.maximum(tr, 0)] * (1.0 + advance), -np.inf)
        valid_l = level_l > CLOSE; valid_s = level_s < CLOSE
        hit_l = np.where(valid_l, E.first_above(HIGH, level_l), N)
        hit_s = np.where(valid_s, E.first_below(LOW, level_s), N)
        better_l = (hit_l < x_l) | ((hit_l == x_l) & (level_l < p_l))
        better_s = (hit_s < x_s) | ((hit_s == x_s) & (level_s > p_s))
        x_l[better_l] = hit_l[better_l]; p_l[better_l] = level_l[better_l]
        x_s[better_s] = hit_s[better_s]; p_s[better_s] = level_s[better_s]
    return x_l, x_s, p_l, p_s, 0, 0.0, 0.0


def risk_reward_tp_data(stop_data, r_multiple: float):
    _, _, _, _, risk_l, risk_s = stop_data
    target_l = CLOSE + (CLOSE - risk_l) * r_multiple
    target_s = CLOSE - (risk_s - CLOSE) * r_multiple
    valid_l = np.isfinite(target_l) & (target_l > CLOSE)
    valid_s = np.isfinite(target_s) & (target_s < CLOSE)
    x_l = np.where(valid_l, E.first_above(HIGH, np.where(valid_l, target_l, np.inf)), N)
    x_s = np.where(valid_s, E.first_below(LOW, np.where(valid_s, target_s, -np.inf)), N)
    return x_l, x_s, np.where(valid_l, target_l, CLOSE[-1]), np.where(valid_s, target_s, CLOSE[-1]), 0, 0.0, 0.0


@njit(cache=True, nogil=True)
def _combined_tp_plans(cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s,
                       plans, close, high, low, year_index, year_count):
    """复用原单笔退出判据，保留动态止盈的激活和保守OHLC顺序。"""
    n = len(close)
    out_l = np.full(n, n, dtype=np.int64); out_s = out_l.copy()
    price_l = np.full(n, close[-1]); price_s = price_l.copy()
    no_sizes = np.empty(0, dtype=np.float64)
    for k in range(len(cand)):
        i = int(cand[k]); side = int(cdir[k])
        stop_i = sx_l[i] if side > 0 else sx_s[i]
        best_i = n
        best_return = np.inf
        for tx_l, tx_s, tp_l, tp_s, mode, p1, p2 in plans:
            result = simulate_all_sizes(
                cand[k:k + 1], cdir[k:k + 1], run_id,
                sx_l, sx_s, sp_l, sp_s, tx_l, tx_s, tp_l, tp_s,
                mode, p1, p2, close, high, low, no_sizes, year_index, year_count)
            if not result[0]:
                continue
            exit_i = i + int(result[4]); raw_return = result[5]
            # 止损与止盈同根时仍由原账户层按止损优先处理。
            if exit_i >= stop_i:
                continue
            if exit_i < best_i or (exit_i == best_i and raw_return < best_return):
                best_i = exit_i; best_return = raw_return
        if best_i < n:
            if side > 0:
                out_l[i] = best_i; price_l[i] = close[i] * (1.0 + best_return)
            else:
                out_s[i] = best_i; price_s[i] = close[i] * (1.0 - best_return)
    return out_l, out_s, price_l, price_s, 0, 0.0, 0.0


def combined_tp_data(cand, cdir, run_id, stop_data, plans):
    """多个全仓止盈并行：实际先触发者退出，同根取保守成交价。"""
    from numba.typed import List
    typed_plans = List()
    for plan in plans:
        if plan is None or len(plan) != 7:
            raise ValueError("组合只支持完整全仓止盈，分批止盈须独立回测")
        typed_plans.append((np.asarray(plan[0], dtype=np.int64), np.asarray(plan[1], dtype=np.int64),
                            np.asarray(plan[2], dtype=np.float64), np.asarray(plan[3], dtype=np.float64),
                            int(plan[4]), float(plan[5]), float(plan[6])))
    if not len(typed_plans):
        raise ValueError("组合止盈至少包含一项有效方案")
    return _combined_tp_plans(cand, cdir, run_id, *stop_data[:4], typed_plans,
                              CLOSE, HIGH, LOW, YEAR_INDEX, len(YEAR_VALUES))


def partial_remainder_data(remainder: 止盈方案):
    if remainder.类别 == "MACD反转止盈":
        ev_l, ev_s = signal_events(remainder.周期组合, remainder.指标,
                                   int(remainder.参数一), "macd")
        return 1, next_true(ev_l), next_true(ev_s), 0.0, 0.0
    if remainder.类别 == "移动止盈":
        empty = np.full(N + 1, N, dtype=np.int64)
        mode = 4 if remainder.指标 == "最高价固定回撤" else 2
        return mode, empty, empty.copy(), remainder.参数一, remainder.参数二
    raise ValueError(f"分批余仓类型不支持：{remainder.类别}")


@njit(cache=True, nogil=True)
def simulate_partial_sizes(cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s,
                           first_x_l, first_x_s, first_price_l, first_price_s,
                           fraction, remainder_mode, next_l, next_s, rem_p1, rem_p2,
                           close, high, low, sizes, year_index, year_count, cooldown_minutes=0,
                           roundtrip_slippage=0.0, initial_capital=100.0,
                           minimum_order_eth=0.0, enforce_minimum_order=False,
                           maximum_order_eth=100.0, funding_cum=FUNDING_CUM, funding_rate=0.0,
                           protect_ratio=0.85, maintenance_rate=0.005, cross_liquidation=True):
    eq = np.ones(len(sizes), dtype=np.float64); peak = eq.copy()
    mdd = np.zeros(len(sizes), dtype=np.float64); liquidations = np.zeros(len(sizes), dtype=np.int64)
    forced = np.zeros(len(sizes), dtype=np.int64)
    executed = np.zeros(len(sizes), dtype=np.int64)
    capital_stops = np.zeros(len(sizes), dtype=np.int64)
    active = np.ones(len(sizes), dtype=np.int8)
    nt = wins = nlong = orders = hold = 0
    gross = gross2 = profit_sum = loss_sum = 0.0
    yearly = np.zeros(year_count, dtype=np.float64)
    last_run = -1; p = 0
    while True:
        k = np.searchsorted(cand, p)
        if k >= len(cand): break
        i = int(cand[k])
        if run_id[i] == last_run:
            p = i + 1; continue
        side = int(cdir[k]); entry = close[i]
        stop_x = int(sx_l[i] if side > 0 else sx_s[i])
        stop_price = sp_l[i] if side > 0 else sp_s[i]
        f_x = int(first_x_l[i] if side > 0 else first_x_s[i])
        f_price = first_price_l[i] if side > 0 else first_price_s[i]
        partial = f_x < stop_x
        if not partial:
            exit_x = stop_x; ret = (stop_price / entry - 1.0) * side - roundtrip_slippage
            adverse_before = 0.0; adverse_after = 0.0; orders_trade = 2
            if side > 0:
                worst = entry
                for j in range(i + 1, exit_x + 1):
                    if low[j] < worst: worst = low[j]
                adverse_before = max(0.0, 1.0 - worst / entry)
            else:
                worst = entry
                for j in range(i + 1, exit_x + 1):
                    if high[j] > worst: worst = high[j]
                adverse_before = max(0.0, worst / entry - 1.0)
            first_ret = 0.0
        else:
            first_ret = (f_price / entry - 1.0) * side
            exit_x = stop_x; rem_price = stop_price; orders_trade = 3
            if remainder_mode == 1:
                j = next_l[f_x + 1] if side > 0 else next_s[f_x + 1]
                while j < stop_x and ((side > 0 and close[j] <= entry) or (side < 0 and close[j] >= entry)):
                    j = next_l[j + 1] if side > 0 else next_s[j + 1]
                if j < stop_x:
                    exit_x = j; rem_price = close[j]
            elif remainder_mode == 2:
                activated = False; activation_bar = -1; best = entry
                for j in range(f_x + 1, stop_x):
                    if side > 0:
                        # 1m OHLC不知道先高后低还是先低后高。先用上一根结束时
                        # 已知的极值检查旧止盈线，未退出后才吸收本根新极值。
                        if activated and j > activation_bar:
                            trail = entry + (best - entry) * (1.0 - rem_p2)
                            if low[j] <= trail:
                                exit_x = j; rem_price = trail; break
                        if high[j] > best: best = high[j]
                        if not activated and best >= entry * (1.0 + rem_p1):
                            activated = True; activation_bar = j
                    else:
                        if activated and j > activation_bar:
                            trail = entry - (entry - best) * (1.0 - rem_p2)
                            if high[j] >= trail:
                                exit_x = j; rem_price = trail; break
                        if low[j] < best: best = low[j]
                        if not activated and best <= entry * (1.0 - rem_p1):
                            activated = True; activation_bar = j
            else:
                activated = False; activation_bar = -1; best = entry
                for j in range(f_x + 1, stop_x):
                    if side > 0:
                        if activated and j > activation_bar:
                            trail = best * (1.0 - rem_p2)
                            if low[j] <= trail:
                                exit_x = j; rem_price = trail; break
                        if high[j] > best: best = high[j]
                        if not activated and best >= entry * (1.0 + rem_p1):
                            activated = True; activation_bar = j
                    else:
                        if activated and j > activation_bar:
                            trail = best * (1.0 + rem_p2)
                            if high[j] >= trail:
                                exit_x = j; rem_price = trail; break
                        if low[j] < best: best = low[j]
                        if not activated and best <= entry * (1.0 - rem_p1):
                            activated = True; activation_bar = j
            remainder_ret = (rem_price / entry - 1.0) * side
            ret = fraction * first_ret + (1.0 - fraction) * remainder_ret - roundtrip_slippage
            adverse_before = 0.0; adverse_after = 0.0
            if side > 0:
                worst = entry
                for j in range(i + 1, f_x + 1):
                    if low[j] < worst: worst = low[j]
                adverse_before = max(0.0, 1.0 - worst / entry)
                worst = low[f_x]
                for j in range(f_x + 1, exit_x + 1):
                    if low[j] < worst: worst = low[j]
                adverse_after = max(0.0, 1.0 - worst / entry)
            else:
                worst = entry
                for j in range(i + 1, f_x + 1):
                    if high[j] > worst: worst = high[j]
                adverse_before = max(0.0, worst / entry - 1.0)
                worst = high[f_x]
                for j in range(f_x + 1, exit_x + 1):
                    if high[j] > worst: worst = high[j]
                adverse_after = max(0.0, worst / entry - 1.0)
        if funding_rate != 0.0:
            ret -= funding_rate * (funding_cum[exit_x] - funding_cum[i])
        nt += 1; orders += orders_trade; hold += exit_x - i
        gross += ret; gross2 += ret * ret
        if ret > 0: wins += 1; profit_sum += ret
        elif ret < 0: loss_sum -= ret
        if side > 0: nlong += 1
        yearly[year_index[i]] += ret
        for z in range(len(sizes)):
            mult = sizes[z]
            if active[z] == 0:
                continue
            requested_qty = initial_capital * eq[z] * mult / entry
            actual_qty = min(requested_qty, maximum_order_eth)
            if enforce_minimum_order and actual_qty < minimum_order_eth:
                active[z] = 0; capital_stops[z] = 1
                continue
            effective_mult = actual_qty * entry / (initial_capital * eq[z])
            executed[z] += 1
            before_loss = effective_mult * adverse_before
            after_loss = effective_mult * ((1.0 - fraction) * adverse_after - fraction * first_ret) if partial else before_loss
            worst_loss = max(before_loss, after_loss)
            liq_ratio = 1.0 - effective_mult * maintenance_rate
            if liq_ratio < 0.0: liq_ratio = 0.0
            use_liq = cross_liquidation and liq_ratio <= protect_ratio
            floor = 0.0 if use_liq else 1.0 - protect_ratio
            intra_eq = eq[z] * max(floor, 1.0 - worst_loss)
            dd_intra = 1.0 - intra_eq / peak[z]
            if dd_intra > mdd[z]: mdd[z] = dd_intra
            if use_liq and worst_loss >= liq_ratio:
                trade_ret = -1.0
                forced[z] += 1
                active[z] = 0
                capital_stops[z] = 1
            elif worst_loss >= protect_ratio:
                trade_ret = -protect_ratio; liquidations[z] += 1
            else:
                trade_ret = effective_mult * ret
            eq[z] = min(1e300, eq[z] * max(1e-12, 1.0 + trade_ret))
            if eq[z] > peak[z]: peak[z] = eq[z]
            dd = 1.0 - eq[z] / peak[z]
            if dd > mdd[z]: mdd[z] = dd
        last_run = run_id[i]
        p = exit_x + cooldown_minutes + 1 if partial else exit_x + 1
    if enforce_minimum_order and len(close) > 0:
        final_price = close[len(close) - 1]
        for z in range(len(sizes)):
            final_qty = min(initial_capital * eq[z] * sizes[z] / final_price, maximum_order_eth)
            if final_qty < minimum_order_eth:
                capital_stops[z] = 1
    return (nt, wins, nlong, orders, hold, gross, gross2, profit_sum, loss_sum, yearly,
            eq, mdd, liquidations, executed, capital_stops, forced)


@njit(cache=True, nogil=True)
def simulate_all_sizes(cand, cdir, run_id, sx_l, sx_s, sp_l, sp_s,
                       tx_l, tx_s, tp_l, tp_s, dynamic_mode, param1, param2,
                       close, high, low, sizes, year_index, year_count, cooldown_minutes=0,
                       roundtrip_slippage=0.0, initial_capital=100.0,
                       minimum_order_eth=0.0, enforce_minimum_order=False,
                       maximum_order_eth=100.0, funding_cum=FUNDING_CUM, funding_rate=0.0,
                       protect_ratio=0.85, maintenance_rate=0.005, cross_liquidation=True):
    eq = np.ones(len(sizes), dtype=np.float64)
    peak = np.ones(len(sizes), dtype=np.float64)
    mdd = np.zeros(len(sizes), dtype=np.float64)
    liquidations = np.zeros(len(sizes), dtype=np.int64)
    forced = np.zeros(len(sizes), dtype=np.int64)
    executed = np.zeros(len(sizes), dtype=np.int64)
    capital_stops = np.zeros(len(sizes), dtype=np.int64)
    active = np.ones(len(sizes), dtype=np.int8)
    nt = wins = nlong = orders = hold = 0
    gross = gross2 = profit_sum = loss_sum = 0.0
    yearly = np.zeros(year_count, dtype=np.float64)
    last_run = -1
    p = 0
    while True:
        k = np.searchsorted(cand, p)
        if k >= len(cand):
            break
        i = int(cand[k])
        if run_id[i] == last_run:
            p = i + 1
            continue
        side = int(cdir[k])
        stop_x = int(sx_l[i] if side > 0 else sx_s[i])
        exit_x = stop_x
        exit_price = sp_l[i] if side > 0 else sp_s[i]
        took_profit = False
        if dynamic_mode == 0:
            candidate_x = int(tx_l[i] if side > 0 else tx_s[i])
            # 同一分钟触发止损和止盈时，止损优先，所以只能严格早于止损。
            if candidate_x < stop_x:
                exit_x = candidate_x
                exit_price = tp_l[i] if side > 0 else tp_s[i]
                took_profit = True
        elif dynamic_mode == 2:
            entry = close[i]
            activated = False
            activation_bar = -1
            best = entry
            for j in range(i + 1, stop_x):
                if side > 0:
                    if activated and j > activation_bar:
                        trail = entry + (best - entry) * (1.0 - param2)
                        if low[j] <= trail:
                            exit_x = j; exit_price = trail; took_profit = True; break
                    if high[j] > best: best = high[j]
                    if not activated and best >= entry * (1.0 + param1):
                        activated = True; activation_bar = j
                else:
                    if activated and j > activation_bar:
                        trail = entry - (entry - best) * (1.0 - param2)
                        if high[j] >= trail:
                            exit_x = j; exit_price = trail; took_profit = True; break
                    if low[j] < best: best = low[j]
                    if not activated and best <= entry * (1.0 - param1):
                        activated = True; activation_bar = j
        elif dynamic_mode == 3:
            entry = close[i]
            activation_bar = -1
            protect = entry * (1.0 + param2 * side)
            for j in range(i + 1, stop_x):
                if activation_bar < 0:
                    favorable_close = close[j] >= entry * (1.0 + param1) if side > 0 else close[j] <= entry * (1.0 - param1)
                    if favorable_close: activation_bar = j
                elif j > activation_bar:
                    touched = low[j] <= protect if side > 0 else high[j] >= protect
                    if touched:
                        exit_x = j; exit_price = protect; took_profit = True; break
        elif dynamic_mode == 4:
            entry = close[i]
            activated = False
            activation_bar = -1
            best = entry
            for j in range(i + 1, stop_x):
                if side > 0:
                    if activated and j > activation_bar:
                        trail = best * (1.0 - param2)
                        if low[j] <= trail:
                            exit_x = j; exit_price = trail; took_profit = True; break
                    if high[j] > best: best = high[j]
                    if not activated and best >= entry * (1.0 + param1):
                        activated = True; activation_bar = j
                else:
                    if activated and j > activation_bar:
                        trail = best * (1.0 + param2)
                        if high[j] >= trail:
                            exit_x = j; exit_price = trail; took_profit = True; break
                    if low[j] < best: best = low[j]
                    if not activated and best <= entry * (1.0 - param1):
                        activated = True; activation_bar = j
        if dynamic_mode != 0:
            # 动态止盈（移动/保本）与固定比例止盈并行，谁先触发算谁。
            # 原来是 if/elif：只要开了动态止盈，静态止盈数组就被整个忽略，
            # 于是回测根本没法还原实盘"固定止盈 + 移动止盈 两条并行"的配置。
            # 纯动态方案传进来的是"永不触发"数组，所以这段对它们是空操作。
            static_x = int(tx_l[i] if side > 0 else tx_s[i])
            if static_x < exit_x and static_x < stop_x:
                exit_x = static_x
                exit_price = tp_l[i] if side > 0 else tp_s[i]
                took_profit = True
        if exit_x <= i:
            p = i + 1
            continue
        entry = close[i]
        ret = (exit_price / entry - 1.0) * side - roundtrip_slippage
        adverse = 0.0
        if side > 0:
            worst = entry
            for j in range(i + 1, exit_x + 1):
                if low[j] < worst: worst = low[j]
            adverse = max(0.0, 1.0 - worst / entry)
        else:
            worst = entry
            for j in range(i + 1, exit_x + 1):
                if high[j] > worst: worst = high[j]
            adverse = max(0.0, worst / entry - 1.0)
        if funding_rate != 0.0:
            ret -= funding_rate * (funding_cum[exit_x] - funding_cum[i])
        nt += 1; orders += 2; hold += exit_x - i
        gross += ret; gross2 += ret * ret
        if ret > 0: wins += 1; profit_sum += ret
        elif ret < 0: loss_sum -= ret
        if side > 0: nlong += 1
        yearly[year_index[i]] += ret
        for z in range(len(sizes)):
            mult = sizes[z]
            if active[z] == 0:
                continue
            requested_qty = initial_capital * eq[z] * mult / entry
            actual_qty = min(requested_qty, maximum_order_eth)
            if enforce_minimum_order and actual_qty < minimum_order_eth:
                active[z] = 0
                capital_stops[z] = 1
                continue
            effective_mult = actual_qty * entry / (initial_capital * eq[z])
            executed[z] += 1
            # 全仓口径：整个账户为该仓位担保，浮亏占权益比例 = 名义倍数 x 不利波动。
            loss_ratio = effective_mult * adverse
            liq_ratio = 1.0 - effective_mult * maintenance_rate
            if liq_ratio < 0.0: liq_ratio = 0.0
            use_liq = cross_liquidation and liq_ratio <= protect_ratio
            floor = 0.0 if use_liq else 1.0 - protect_ratio
            intra_factor = 1.0 - loss_ratio
            if intra_factor < floor: intra_factor = floor
            intra_eq = eq[z] * intra_factor
            dd_intra = 1.0 - intra_eq / peak[z]
            if dd_intra > mdd[z]: mdd[z] = dd_intra
            if use_liq and loss_ratio >= liq_ratio:
                trade_ret = -1.0
                forced[z] += 1
                active[z] = 0
                capital_stops[z] = 1
            elif loss_ratio >= protect_ratio:
                trade_ret = -protect_ratio
                liquidations[z] += 1
            else:
                trade_ret = effective_mult * ret
            factor = max(1e-12, 1.0 + trade_ret)
            eq[z] = min(1e300, eq[z] * factor)
            if eq[z] > peak[z]: peak[z] = eq[z]
            dd = 1.0 - eq[z] / peak[z]
            if dd > mdd[z]: mdd[z] = dd
        last_run = run_id[i]
        p = exit_x + cooldown_minutes + 1 if took_profit else exit_x + 1
    if enforce_minimum_order and len(close) > 0:
        final_price = close[len(close) - 1]
        for z in range(len(sizes)):
            final_qty = min(initial_capital * eq[z] * sizes[z] / final_price, maximum_order_eth)
            if final_qty < minimum_order_eth:
                capital_stops[z] = 1
    return (nt, wins, nlong, orders, hold, gross, gross2, profit_sum, loss_sum, yearly,
            eq, mdd, liquidations, executed, capital_stops, forced)


def metrics_dict(result):
    (nt, wins, nlong, orders, hold, gross, gross2, profit_sum, loss_sum, yearly,
     eq, mdd, liq, executed, capital_stops, forced) = result[:16]
    variance = max(0.0, (gross2 - gross * gross / nt) / (nt - 1)) if nt > 1 else 0.0
    t0 = (gross / nt) / np.sqrt(variance / nt) if variance > 0 else 0.0
    metrics = {
        "交易次数": int(nt), "盈利次数": int(wins), "胜率": wins / nt if nt else 0.0,
        "多单占比": nlong / nt if nt else 0.0,
        "平均日完整交易数": nt / DAYS if DAYS else 0.0,
        "平均日成交订单数": orders / DAYS if DAYS else 0.0,
        # 兼容内部旧调用；新导出不再使用这个含义不清的名字。
        "平均日成交单数": orders / DAYS if DAYS else 0.0,
        "平均持仓分钟": hold / nt if nt else 0.0, "平均单笔收益率": gross / nt if nt else 0.0,
        "毛收益合计": gross, "盈亏比": profit_sum / loss_sum if loss_sum > 0 else 0.0, "t值": t0,
        "年度收益": yearly, "期末资金倍数": eq, "最大回撤": mdd, "爆仓保护次数": liq,
        "实际成交次数": executed, "资金性停机标记": capital_stops, "全仓强平次数": forced,
    }
    if len(result) > 16:
        metrics["逐仓统计"] = result[16]
    if len(result) > 17:
        metrics["逐仓止盈等待总分钟"] = result[17]
    if len(result) > 18:
        metrics["逐仓首次达到开仓上限索引"] = result[18]
    return metrics


def metrics_for_size(metrics, index):
    if "逐仓统计" not in metrics:
        return metrics
    values = metrics["逐仓统计"][index]
    result = (*[int(x) for x in values[:5]], *values[5:9], values[9:],
              *[metrics[name][index:index+1] for name in (
                  "期末资金倍数", "最大回撤", "爆仓保护次数", "实际成交次数",
                  "资金性停机标记", "全仓强平次数")])
    account = metrics_dict(result)
    if "逐仓首次达到开仓上限索引" in metrics:
        account["首次达到开仓上限索引"] = int(metrics["逐仓首次达到开仓上限索引"][index])
    if "逐仓止盈等待总分钟" in metrics:
        waiting = float(metrics["逐仓止盈等待总分钟"][index])
        account["实际止盈等待总分钟"] = waiting
        account["实际容量占用率"] = (values[4] + waiting) / (DAYS * 1440.0) if DAYS else 0.0
    return account
