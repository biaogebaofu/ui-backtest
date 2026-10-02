"""第二轮收盘信号。仅使用当根及历史已收盘数据，不对缺失值放行。"""
import numpy as np


def consecutive(mask, count):
    out = np.asarray(mask, dtype=bool).copy()
    for lag in range(1, count):
        out[lag:] &= mask[:-lag]
        out[:lag] = False
    return out


def ma_cross(close, ma, confirm):
    close, ma = np.asarray(close), np.asarray(ma)
    below = np.isfinite(ma) & (close < ma)
    above = np.isfinite(ma) & (close > ma)
    # 确认段前一根必须位于另一侧；只发出穿越事件，不把同一段状态重复触发。
    was_above = np.zeros(len(close), dtype=bool)
    was_below = was_above.copy()
    was_above[confirm:] = (close[:-confirm] >= ma[:-confirm])
    was_below[confirm:] = (close[:-confirm] <= ma[:-confirm])
    return consecutive(below, confirm) & was_above, consecutive(above, confirm) & was_below


def volume_fade(volume, ratio, confirm, window=20):
    volume = np.asarray(volume, dtype=float)
    valid = np.isfinite(volume) & (volume >= 0)
    sums = np.r_[0.0, np.cumsum(np.where(valid, volume, 0.0))]
    counts = np.r_[0, np.cumsum(valid)]
    mean = np.full(len(volume), np.nan)
    if len(volume) > window:
        mean[window:] = (sums[window:-1] - sums[:-window-1]) / window
        mean[window:][(counts[window:-1] - counts[:-window-1]) != window] = np.nan
    return consecutive(valid & (mean > 0) & (volume <= ratio * mean), confirm)
