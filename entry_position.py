"""独立入场位置过滤；只约束追单位置，不产生方向或改变退出。

多个代码表示分别回测；只有组合代码内部两项同时满足。
EMA使用最新已收5m的EMA20，距离按同周期Wilder ATR14归一化。
区间使用信号K之前的30/60根完整1m，不把当前突破K计入区间。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from extended_signals import atr14


POSITION_FILTERS = {
    "OFF": {"label": "关闭位置过滤（原策略基线）", "ema_atr_max": None,
            "range_minutes": 0, "edge": 0.0},
}
for _bucket in (75, 100, 150, 200):
    POSITION_FILTERS[f"EMA5_ATR{_bucket:03d}"] = {
        "label": f"距5m EMA20顺向偏离≤{_bucket / 100:g}倍ATR14",
        "ema_atr_max": _bucket / 100, "range_minutes": 0, "edge": 0.0,
    }
for _minutes in (30, 60):
    for _edge in (10, 20):
        POSITION_FILTERS[f"RANGE{_minutes}_EDGE{_edge}"] = {
            "label": f"前{_minutes}分钟区间：多≤{100-_edge}%位、空≥{_edge}%位",
            "ema_atr_max": None, "range_minutes": _minutes, "edge": _edge / 100,
        }
for _bucket in (100, 150):
    for _edge in (10, 20):
        POSITION_FILTERS[f"EMA5_ATR{_bucket}_R60_E{_edge}"] = {
            "label": f"5m EMA偏离≤{_bucket / 100:g}ATR 且前60分钟多≤{100-_edge}%位/空≥{_edge}%位",
            "ema_atr_max": _bucket / 100, "range_minutes": 60, "edge": _edge / 100,
        }


# 研究候选，不是已确认优势。B04 原定义使用 1m TR 的 SMA14，不是 Wilder ATR。
for _threshold in (100, 150, 200):
    POSITION_FILTERS[f"RESEARCH_CHASE5_SMAATR{_threshold}"] = {
        "label": f"研究对照：排除近5根顺向追价≥{_threshold / 100:g}倍1m SMA-ATR14（24根预热）",
        "chase_limit": _threshold / 100,
    }


def position_filter_label(code: str) -> str:
    if code not in POSITION_FILTERS:
        raise ValueError(f"未知开仓位置过滤：{code}")
    return POSITION_FILTERS[code]["label"]


def normalize_position_filters(raw) -> list[str]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("开仓位置过滤至少选择一档；不限制时明确选择OFF")
    result = []
    for code in raw:
        if not isinstance(code, str) or code not in POSITION_FILTERS:
            raise ValueError(f"未知开仓位置过滤：{code}")
        if code not in result:
            result.append(code)
    return result


def _mapped_ema_atr(data):
    close = np.asarray(data["close"], dtype=np.float64)
    native = np.asarray(data["5m_close"], dtype=np.float64)
    if not len(native):
        return np.full(len(close), np.nan), np.full(len(close), np.nan)
    ema = pd.Series(native).ewm(span=20, adjust=False, min_periods=20).mean().to_numpy()
    atr = atr14(np.asarray(data["5m_high"], dtype=np.float64),
                np.asarray(data["5m_low"], dtype=np.float64), native)
    mapping = np.asarray(data["5m_map"], dtype=np.int64)
    safe = np.clip(mapping, 0, len(native) - 1)
    native_ct = np.asarray(data["5m_ct"], dtype=np.int64)
    age = np.asarray(data["ct1"], dtype=np.int64) - native_ct[safe]
    consecutive = np.zeros(len(native), dtype=bool)
    consecutive[19:] = native_ct[19:] - native_ct[:-19] == 19 * 300_000
    valid = ((mapping >= 19) & (mapping < len(native)) & consecutive[safe]
             & (age >= 0) & (age < 300_000))
    return np.where(valid, ema[safe], np.nan), np.where(valid, atr[safe], np.nan)


def position_masks(code: str, data: dict) -> tuple[np.ndarray, np.ndarray]:
    position_filter_label(code)  # 明确拒绝未知代码，不能退回不限制。
    spec = POSITION_FILTERS[code]
    close = np.asarray(data["close"], dtype=np.float64)
    if code == "OFF":
        return np.ones(len(close), dtype=bool), np.ones(len(close), dtype=bool)
    if "chase_limit" in spec:
        high = np.asarray(data["high"], dtype=float)
        low = np.asarray(data["low"], dtype=float)
        ct = np.asarray(data["ct1"], dtype=np.int64)
        prior = pd.Series(close).shift(1).to_numpy()
        tr = np.maximum(high - low, np.maximum(abs(high - prior), abs(low - prior)))
        atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
        change = close - pd.Series(close).shift(5).to_numpy()
        # 缺K线/无效价格一律不放行，不能把缺数据解释为没有追价。
        valid_bar = (np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
                     & (high >= close) & (close >= low) & (low > 0))
        valid = pd.Series(valid_bar.astype(int)).rolling(24, min_periods=24).sum().eq(24).to_numpy()
        steps = pd.Series(ct).diff().eq(60_000)
        consecutive = steps.rolling(23, min_periods=23).sum().eq(23).to_numpy()
        valid = valid & consecutive & np.isfinite(atr) & (atr > 0) & np.isfinite(change)
        return (valid & (change < spec["chase_limit"] * atr),
                valid & (-change < spec["chase_limit"] * atr))
    long_ok = np.isfinite(close)
    short_ok = long_ok.copy()
    if spec["ema_atr_max"] is not None:
        ema, atr = _mapped_ema_atr(data)
        valid = np.isfinite(ema) & np.isfinite(atr) & (atr > 0)
        distance = atr * spec["ema_atr_max"]
        # 仅约束本方向追价：多在均线上方过远禁开，空在均线下方过远禁开。
        long_ok &= valid & (close <= ema + distance)
        short_ok &= valid & (close >= ema - distance)
    window = spec["range_minutes"]
    if window:
        high = pd.Series(np.asarray(data["high"], dtype=np.float64))
        low = pd.Series(np.asarray(data["low"], dtype=np.float64))
        previous_high = high.shift(1).rolling(window, min_periods=window).max().to_numpy()
        previous_low = low.shift(1).rolling(window, min_periods=window).min().to_numpy()
        width = previous_high - previous_low
        ct = np.asarray(data["ct1"], dtype=np.int64)
        consecutive = np.zeros(len(close), dtype=bool)
        consecutive[window:] = ct[window:] - ct[:-window] == window * 60_000
        valid = np.isfinite(width) & (width > 0) & consecutive
        long_ok &= valid & (close <= previous_high - spec["edge"] * width)
        short_ok &= valid & (close >= previous_low + spec["edge"] * width)
    return long_ok, short_ok
