"""OHLCV/微结构扩展开仓规则：只读取已收盘原生周期数据，由调用者映射到1m。

第二批规则保持原义；第三批在此集中实现。滚动基准默认只用当前K线之前的数据，
避免把当前异常值同时放进“历史均值/标准差”造成阈值自稀释。
"""
import numpy as np
import pandas as pd
from numba import njit
from extended_rules import ENTRY_RULES


@njit(cache=True)
def atr14(high, low, close):
    out = np.full(len(close), np.nan)
    total = 0.
    for i in range(len(close)):
        tr = high[i] - low[i]
        if i:
            tr = max(tr, abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        if i < 14:
            total += tr
            if i == 13:
                out[i] = total / 14.
        else:
            out[i] = (out[i-1] * 13. + tr) / 14.
    return out


def _shift(values, n=1, fill=np.nan):
    a = np.asarray(values)
    if np.issubdtype(a.dtype, np.bool_):
        out = np.full(len(a), False, dtype=bool)
    else:
        out = np.full(len(a), fill, dtype=np.float64)
    if n < len(a):
        out[n:] = a[:-n]
    return out


def _prev_roll(values, window, kind="mean"):
    s = pd.Series(np.asarray(values, dtype=np.float64)).shift(1).rolling(window, min_periods=window)
    if kind == "mean": return s.mean().to_numpy(np.float64)
    if kind == "std": return s.std(ddof=0).to_numpy(np.float64)
    if kind == "max": return s.max().to_numpy(np.float64)
    if kind == "min": return s.min().to_numpy(np.float64)
    if kind == "sum": return s.sum().to_numpy(np.float64)
    raise ValueError(kind)


def _rsi14(close):
    d = pd.Series(np.asarray(close, dtype=np.float64)).diff()
    gain = d.clip(lower=0.0)
    loss = (-d.clip(upper=0.0))
    # Wilder递推近似：alpha=1/14；只使用截至本根收盘的数据。
    ag = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    al = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = ag / al.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi.to_numpy(np.float64)


def _run_mask(condition, n):
    condition = np.asarray(condition, dtype=bool)
    if n <= 1: return condition.copy()
    # rolling sum only uses已收盘布尔条件，当前根允许参与信号。
    return pd.Series(condition.astype(np.int8)).rolling(n, min_periods=n).sum().to_numpy() >= n


def _extra(extras, key, n):
    if extras is None or key not in extras:
        return np.full(n, np.nan, dtype=np.float64)
    a = np.asarray(extras[key], dtype=np.float64)
    return a if len(a) == n else np.full(n, np.nan, dtype=np.float64)


def _elapsed_shift(values, minutes, timeframe, close_times):
    from extended_rules import ENTRY_TIMEFRAME_MINUTES
    if timeframe == "1m" or close_times is None:
        return _shift(values, minutes // ENTRY_TIMEFRAME_MINUTES[timeframe])
    times = np.asarray(close_times, dtype=np.int64)
    target = times - minutes * 60000
    index = np.searchsorted(times, target)
    safe = np.minimum(index, len(times) - 1)
    return np.where((index < len(times)) & (times[safe] == target), np.asarray(values)[safe], np.nan)


def entry_masks(code, direction, values, open_, high, low, close, ma, volume=None,
                close_times=None, extras=None, timeframe="1m"):
    from extended_rules import ENTRY_TIMEFRAME_MINUTES, entry_supported_timeframes
    if timeframe not in entry_supported_timeframes(code):
        raise ValueError(f"开仓策略{code}不支持周期{timeframe}")
    minutes = ENTRY_TIMEFRAME_MINUTES[timeframe]
    if code >= 264:
        from fifth_batch import fifth_masks
        return fifth_masks(code, direction, values, open_, high, low, close,
                           volume if volume is not None else np.full(len(close), np.nan),
                           close_times, extras, timeframe)
    if code >= 144:
        from fourth_batch import fourth_masks
        return fourth_masks(code, direction, values, open_, high, low, close,
                            volume if volume is not None else np.full(len(close),np.nan), close_times, extras, timeframe)
    _, kind, arg = ENTRY_RULES[code]
    values = np.asarray(values, dtype=np.float64)
    open_ = np.asarray(open_, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    ma = np.asarray(ma, dtype=np.float64)
    n = len(close)
    volume = np.asarray(volume if volume is not None else np.full(n, np.nan), dtype=np.float64)
    long = np.asarray(direction) == 1
    short = np.asarray(direction) == -1
    span = high - low

    # ---------- 第二批 ----------
    if kind == "zero":
        return long & (values > 0), short & (values < 0)
    if kind in ("wick", "body"):
        good = span > 0
        safe = np.where(good, span, 1.)
        if kind == "wick":
            return (long & good & ((high - np.maximum(close, open_)) / safe <= arg),
                    short & good & ((np.minimum(close, open_) - low) / safe <= arg))
        valid = good & (np.abs(close - open_) / safe >= arg)
        return long & valid, short & valid
    if kind == "slope":
        lag = int(arg)
        up = np.zeros(n, dtype=bool); down = np.zeros(n, dtype=bool)
        if lag < n:
            up[lag:] = ma[lag:] > ma[:-lag]
            down[lag:] = ma[lag:] < ma[:-lag]
        return long & up, short & down
    if kind == "distance":
        with np.errstate(divide="ignore", invalid="ignore"):
            dev = close / ma - 1.
        return long & (dev >= 0) & (dev <= arg), short & (dev <= 0) & (dev >= -arg)

    # ---------- 第三批：MACD/蜡烛/波动/量 ----------
    delta = np.full(n, np.nan); delta[1:] = values[1:] - values[:-1]
    accel = np.full(n, np.nan); accel[2:] = delta[2:] - delta[1:-1]
    if kind == "macd_accel":
        return long & (accel > 0), short & (accel < 0)
    if kind == "macd_accel2":
        return long & (accel > 0) & (_shift(accel) > 0), short & (accel < 0) & (_shift(accel) < 0)
    if kind == "macd_amp_std":
        sd = _prev_roll(values, 20, "std")
        good = np.isfinite(sd) & (sd > 0) & (np.abs(values) >= arg * sd)
        return long & good, short & good
    if kind == "clv":
        safe = np.where(span > 0, span, np.nan)
        clv = (2.0 * close - high - low) / safe
        return long & (clv >= arg), short & (clv <= -arg)
    if kind == "candle_align":
        return long & (close > open_), short & (close < open_)
    if kind == "candle_body_align":
        safe = np.where(span > 0, span, np.nan)
        body = np.abs(close-open_) / safe
        return long & (close > open_) & (body >= arg), short & (close < open_) & (body >= arg)
    if kind in ("range_ratio_min", "range_ratio_max"):
        base = _prev_roll(span, 20, "mean")
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = span / base
        good = ratio >= arg if kind.endswith("min") else ratio <= arg
        good &= np.isfinite(ratio)
        return long & good, short & good
    if kind in ("volume_ratio_min", "volume_ratio_max"):
        base = _prev_roll(volume, 20, "mean")
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = volume / base
        good = ratio >= arg if kind.endswith("min") else ratio <= arg
        good &= np.isfinite(ratio)
        return long & good, short & good
    if kind == "momentum_run":
        ret_up = close > _shift(close)
        ret_dn = close < _shift(close)
        k = int(arg)
        return long & _run_mask(ret_up, k), short & _run_mask(ret_dn, k)
    if kind == "macd_slope_run":
        k = int(arg)
        return long & _run_mask(delta > 0, k), short & _run_mask(delta < 0, k)
    if kind == "macd_decel":
        return long & (accel < 0), short & (accel > 0)
    if kind in ("macd_z", "macd_near_zero"):
        mu = _prev_roll(values, 20, "mean"); sd = _prev_roll(values, 20, "std")
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (values - mu) / sd
        if kind == "macd_z":
            return long & (z >= arg), short & (z <= -arg)
        good = np.isfinite(z) & (np.abs(z) <= arg)
        return long & good, short & good
    if kind == "zero_cross_event":
        prev = _shift(values)
        return long & (values > 0) & (prev <= 0), short & (values < 0) & (prev >= 0)
    if kind in ("dual_macd_dir", "macd_dir_disagree", "hist_sign_align"):
        hist = _extra(extras, "hist", n); dif = _extra(extras, "dif", n)
        dh = np.zeros(n, dtype=np.int8); dd = np.zeros(n, dtype=np.int8)
        hdelta = hist[1:] - hist[:-1]; ddelta = dif[1:] - dif[:-1]
        dh[1:] = np.where(np.isfinite(hdelta), np.sign(hdelta), 0).astype(np.int8)
        dd[1:] = np.where(np.isfinite(ddelta), np.sign(ddelta), 0).astype(np.int8)
        if kind == "dual_macd_dir":
            return long & (dh > 0) & (dd > 0), short & (dh < 0) & (dd < 0)
        if kind == "macd_dir_disagree":
            good = (dh * dd) < 0
            return long & good, short & good
        return long & (hist > 0), short & (hist < 0)

    # ---------- 第三批：结构/均值/趋势 ----------
    if kind == "donchian_break":
        w = int(arg)
        upper = _prev_roll(high, w, "max"); lower = _prev_roll(low, w, "min")
        return long & (close > upper), short & (close < lower)
    if kind == "donchian_near":
        w = 20
        upper = _prev_roll(high, w, "max"); lower = _prev_roll(low, w, "min")
        width = upper-lower
        good = np.isfinite(width) & (width > 0)
        return (long & good & (close <= upper) & ((upper-close)/width <= arg),
                short & good & (close >= lower) & ((close-lower)/width <= arg))
    if kind in ("boll_trend", "boll_contra"):
        mean = pd.Series(close).rolling(20, min_periods=20).mean().to_numpy(np.float64)
        sd = pd.Series(close).rolling(20, min_periods=20).std(ddof=0).to_numpy(np.float64)
        upper = mean + arg*sd; lower = mean - arg*sd
        if kind == "boll_trend": return long & (close >= upper), short & (close <= lower)
        return long & (close <= lower), short & (close >= upper)
    if kind in ("rsi_trend", "rsi_contra"):
        rsi = _rsi14(close)
        if kind == "rsi_trend":
            level = arg
            return long & (rsi >= level), short & (rsi <= 100-level)
        low_level = arg
        return long & (rsi <= low_level), short & (rsi >= 100-low_level)
    if kind in ("efficiency_min", "efficiency_max"):
        w = 10
        chg = np.abs(close - _shift(close, w))
        step = np.abs(close - _shift(close))
        den = pd.Series(step).rolling(w, min_periods=w).sum().to_numpy(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            er = chg/den
        good = er >= arg if kind.endswith("min") else er <= arg
        good &= np.isfinite(er)
        return long & good, short & good
    if kind == "distance_min":
        with np.errstate(divide="ignore", invalid="ignore"):
            dev = close/ma - 1.0
        return long & (dev >= arg), short & (dev <= -arg)
    if kind in ("vwap_side", "vwap_distance"):
        w = 20 if kind == "vwap_distance" else int(arg)
        typical = (high+low+close)/3.0
        pv = pd.Series(typical*volume).rolling(w, min_periods=w).sum().to_numpy(np.float64)
        vv = pd.Series(volume).rolling(w, min_periods=w).sum().to_numpy(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            vwap = pv/vv; dev = close/vwap-1.0
        if kind == "vwap_side": return long & (dev >= 0), short & (dev <= 0)
        return long & (dev >= 0) & (dev <= arg), short & (dev <= 0) & (dev >= -arg)
    if kind == "close_break":
        w = int(arg)
        up = _prev_roll(close, w, "max"); dn = _prev_roll(close, w, "min")
        return long & (close > up), short & (close < dn)
    if kind == "inside_break":
        ph=_shift(high); pl=_shift(low); mh=_shift(high,2); ml=_shift(low,2)
        inside=(ph<=mh)&(pl>=ml)&np.isfinite(mh)&np.isfinite(ml)
        return long & inside & (close>mh), short & inside & (close<ml)
    if kind == "sweep_reclaim":
        ph=_shift(high); pl=_shift(low)
        return long & (low<pl) & (close>pl), short & (high>ph) & (close<ph)
    if kind == "micro_reversal":
        po=_shift(open_); pc=_shift(close)
        return long & (pc<po) & (close>open_), short & (pc>po) & (close<open_)
    if kind == "candle_run":
        k=int(arg); green=close>open_; red=close<open_
        return long & _run_mask(green,k), short & _run_mask(red,k)
    if kind == "engulf":
        po=_shift(open_); pc=_shift(close)
        bull=(pc<po)&(close>open_)&(open_<=pc)&(close>=po)
        bear=(pc>po)&(close<open_)&(open_>=pc)&(close<=po)
        return long & bull, short & bear
    if kind == "outside_align":
        ph=_shift(high); pl=_shift(low)
        outside=(high>ph)&(low<pl)
        return long & outside & (close>open_), short & outside & (close<open_)
    if kind == "pin_reversal":
        safe=np.where(span>0,span,np.nan)
        lower=(np.minimum(open_,close)-low)/safe; upper=(high-np.maximum(open_,close))/safe
        return long & (lower>=arg) & (close>=open_), short & (upper>=arg) & (close<=open_)
    if kind == "close_extreme":
        safe=np.where(span>0,span,np.nan)
        from_high=(high-close)/safe; from_low=(close-low)/safe
        return long & (from_high<=arg), short & (from_low<=arg)
    if kind == "doji_break":
        ps=_shift(span); po=_shift(open_); pc=_shift(close); ph=_shift(high); pl=_shift(low)
        doji=np.isfinite(ps)&(ps>0)&(np.abs(pc-po)/ps<=arg)
        return long & doji & (close>ph), short & doji & (close<pl)
    if kind in ("nr_n","wr_n"):
        w=int(arg); prev_min=_prev_roll(span,w-1,"min"); prev_max=_prev_roll(span,w-1,"max")
        good=(span<=prev_min) if kind=="nr_n" else (span>=prev_max)
        good &= np.isfinite(prev_min if kind=="nr_n" else prev_max)
        return long & good, short & good
    if kind in ("return2_min","return5_min","return5_contra"):
        k=2 if kind=="return2_min" else 5
        past=_shift(close,k)
        with np.errstate(divide="ignore",invalid="ignore"):
            r=close/past-1.0
        if kind=="return5_contra":
            return long & (r<=-arg), short & (r>=arg)
        return long & (r>=arg), short & (r<=-arg)
    if kind == "two_bar_reversal":
        po=_shift(open_); pc=_shift(close); ph=_shift(high); pl=_shift(low)
        mid=(ph+pl)/2.0
        return long & (pc<po)&(close>open_)&(close>mid), short & (pc>po)&(close<open_)&(close<mid)
    if kind == "failed_break_strong":
        ph=_shift(high); pl=_shift(low)
        return long & (low<pl)&(close>ph), short & (high>ph)&(close<pl)
    if kind == "alternating":
        k=int(arg); sign=np.sign(close-open_).astype(np.int8)
        good=np.ones(n,dtype=bool)
        for lag in range(1,k):
            good &= (sign != _shift(sign,lag,fill=0)) & (sign != 0) & (_shift(sign,lag,fill=0) != 0)
        return long & good & (sign>0), short & good & (sign<0)

    # ---------- 第三批：波动率状态 ----------
    if kind in ("atr_regime_low","atr_regime_high"):
        atr=atr14(high,low,close); base=_prev_roll(atr,60,"mean")
        with np.errstate(divide="ignore",invalid="ignore"): rr=atr/base
        good=(rr<=arg) if kind.endswith("low") else (rr>=arg)
        return long & good, short & good
    if kind in ("rv_regime_low","rv_regime_high"):
        r=np.full(n,np.nan); r[1:]=np.log(close[1:]/close[:-1])
        rv=pd.Series(r).rolling(10,min_periods=10).std(ddof=0).to_numpy(np.float64)
        base=_prev_roll(rv,60,"mean")
        with np.errstate(divide="ignore",invalid="ignore"): rr=rv/base
        good=(rr<=arg) if kind.endswith("low") else (rr>=arg)
        return long & good, short & good
    if kind in ("boll_width_low","boll_width_high"):
        m=pd.Series(close).rolling(20,min_periods=20).mean().to_numpy(np.float64)
        sd=pd.Series(close).rolling(20,min_periods=20).std(ddof=0).to_numpy(np.float64)
        with np.errstate(divide="ignore",invalid="ignore"): width=4.0*sd/m
        base=_prev_roll(width,60,"mean")
        with np.errstate(divide="ignore",invalid="ignore"): rr=width/base
        good=(rr<=arg) if kind.endswith("low") else (rr>=arg)
        return long & good, short & good
    if kind == "tr_z_high":
        prevc=_shift(close); tr=np.maximum(span,np.maximum(np.abs(high-prevc),np.abs(low-prevc)))
        mu=_prev_roll(tr,20,"mean"); sd=_prev_roll(tr,20,"std")
        with np.errstate(divide="ignore",invalid="ignore"): z=(tr-mu)/sd
        good=z>=arg
        return long & good, short & good

    # ---------- 第三批：订单流/成交微结构 ----------
    ratio = _extra(extras, "taker_buy_ratio", n)
    delta_base = _extra(extras, "delta_base", n)
    trades = _extra(extras, "trades", n)
    avg_size = _extra(extras, "avg_trade_size", n)
    quote_volume = _extra(extras, "quote_volume", n)
    taker_buy_quote_ratio = _extra(extras, "taker_buy_quote_ratio", n)
    delta_quote = _extra(extras, "delta_quote", n)
    funding_rate = _extra(extras, "funding_rate", n)
    open_interest = _extra(extras, "open_interest", n)
    open_interest_value = _extra(extras, "open_interest_value", n)
    if kind == "taker_ratio":
        return long & (ratio >= arg), short & (ratio <= 1.0-arg)
    if kind == "taker_z":
        mu=_prev_roll(ratio,60,"mean"); sd=_prev_roll(ratio,60,"std")
        with np.errstate(divide="ignore",invalid="ignore"): z=(ratio-mu)/sd
        return long & (z>=arg), short & (z<=-arg)
    if kind == "delta_side":
        return long & (delta_base>0), short & (delta_base<0)
    if kind == "cvd_slope":
        s=pd.Series(delta_base).rolling(int(arg),min_periods=int(arg)).sum().to_numpy(np.float64)
        return long & (s>0), short & (s<0)
    if kind in ("trades_ratio", "avg_trade_ratio", "trades_ratio_max", "avg_trade_ratio_max"):
        if kind.startswith("trades"):
            src=trades
        else:
            src=avg_size
        base=_prev_roll(src,20,"mean")
        with np.errstate(divide="ignore",invalid="ignore"): rr=src/base
        if kind.endswith("max"):
            good=rr<=arg
        else:
            good=rr>=arg
        return long & good, short & good
    if kind == "price_cvd_div":
        k=int(arg); past=_shift(close,k)
        cvd=pd.Series(delta_base).rolling(k,min_periods=k).sum().to_numpy(np.float64)
        return long & (close<past) & (cvd>0), short & (close>past) & (cvd<0)
    if kind in ("volume_z_high","volume_z_low"):
        mu=_prev_roll(volume,20,"mean"); sd=_prev_roll(volume,20,"std")
        with np.errstate(divide="ignore",invalid="ignore"): z=(volume-mu)/sd
        good=(z>=arg) if kind.endswith("high") else (z<=-arg)
        return long & good, short & good
    if kind == "volume_falling":
        falling=volume<_shift(volume)
        good=_run_mask(falling,int(arg))
        return long & good, short & good
    if kind == "trades_z_high":
        mu=_prev_roll(trades,20,"mean"); sd=_prev_roll(trades,20,"std")
        with np.errstate(divide="ignore",invalid="ignore"): z=(trades-mu)/sd
        good=z>=arg
        return long & good, short & good
    if kind == "delta_z_side":
        mu=_prev_roll(delta_base,60,"mean"); sd=_prev_roll(delta_base,60,"std")
        with np.errstate(divide="ignore",invalid="ignore"): z=(delta_base-mu)/sd
        return long & (z>=arg), short & (z<=-arg)
    if kind == "taker_contra":
        return long & (ratio<=1.0-arg), short & (ratio>=arg)
    if kind == "flow_candle_align":
        return long & (close>open_) & (delta_base>0), short & (close<open_) & (delta_base<0)
    if kind == "flow_absorption":
        ph=_shift(high); pl=_shift(low)
        return long & (low<pl)&(close>pl)&(delta_base<0), short & (high>ph)&(close<ph)&(delta_base>0)

    # ---------- 第三批：外部衍生品数据 / 成交额 ----------
    if kind == "funding_contra":
        return long & (funding_rate <= -arg), short & (funding_rate >= arg)
    if kind == "funding_align":
        return long & (funding_rate > 0), short & (funding_rate < 0)
    if kind == "funding_flip_contra":
        prev = _shift(funding_rate)
        flip_pos = (prev <= 0) & (funding_rate > 0)
        flip_neg = (prev >= 0) & (funding_rate < 0)
        return long & flip_neg, short & flip_pos
    if kind in ("oi_change_min", "oi_change60_min", "oi_change_max", "oi_expand_confirm",
                "price_oi_squeeze", "oi_delever_trend"):
        k = 60 if kind == "oi_change60_min" else 15
        past = _elapsed_shift(open_interest, k, timeframe, close_times)
        with np.errstate(divide="ignore", invalid="ignore"):
            chg = open_interest / past - 1.0
        if kind in ("oi_change_min", "oi_change60_min", "oi_expand_confirm"):
            return long & (chg >= arg), short & (chg >= arg)
        if kind == "oi_change_max":
            return long & (chg <= arg), short & (chg <= arg)
        if kind == "price_oi_squeeze":
            price_past = _elapsed_shift(close, k, timeframe, close_times)
            return long & (close < price_past) & (chg >= arg), short & (close > price_past) & (chg >= arg)
        price_past = _elapsed_shift(close, k, timeframe, close_times)
        return long & (close > price_past) & (chg <= -arg), short & (close < price_past) & (chg <= -arg)
    if kind == "oi_z_high":
        bars = 60 // minutes
        mu=_prev_roll(open_interest,bars,"mean"); sd=_prev_roll(open_interest,bars,"std")
        with np.errstate(divide="ignore",invalid="ignore"): z=(open_interest-mu)/sd
        good=z>=arg
        if timeframe != "1m" and close_times is not None:
            times = np.asarray(close_times, dtype=np.int64)
            good &= times - _shift(times, bars) == 60 * 60000
        return long & good, short & good
    if kind == "oi_value_change_min":
        past=_elapsed_shift(open_interest_value,15,timeframe,close_times)
        with np.errstate(divide="ignore",invalid="ignore"): chg=open_interest_value/past-1.0
        good=chg>=arg
        return long & good, short & good
    if kind in ("quote_volume_ratio_min", "quote_volume_ratio_max"):
        base=_prev_roll(quote_volume,20,"mean")
        with np.errstate(divide="ignore",invalid="ignore"): rr=quote_volume/base
        good=(rr>=arg) if kind.endswith("min") else (rr<=arg)
        return long & good, short & good
    if kind == "taker_quote_ratio":
        return long & (taker_buy_quote_ratio >= arg), short & (taker_buy_quote_ratio <= 1.0-arg)
    if kind == "quote_delta_side":
        return long & (delta_quote > 0), short & (delta_quote < 0)

    # ---------- 第三批：安慰剂/噪声对照 ----------
    if close_times is None:
        minute=(np.arange(n,dtype=np.int64)*minutes)%60
    else:
        # 以该根K线开盘分钟为标签；1m时就是自然UTC分钟。
        minute=((np.asarray(close_times,dtype=np.int64)//60000)-minutes)%60
    if kind == "minute_tail7":
        good=(minute%10)==7
        return long & good, short & good
    if kind == "price_tail78":
        tail=(np.floor(np.abs(close)*100+1e-9).astype(np.int64)%10)
        good=(tail==7)|(tail==8)
        return long & good, short & good
    if kind == "minute_fib":
        fib=np.isin(minute,np.array([0,1,2,3,5,8,13,21,34,55],dtype=np.int64))
        return long & fib, short & fib
    # 追加安慰剂：故意没有市场机制，只用于检验数据挖掘/过拟合。
    if close_times is None:
        minute_epoch=np.arange(n,dtype=np.int64)*minutes
        hour=(minute_epoch//60)%24; day_index=minute_epoch//1440
    else:
        open_ms=np.asarray(close_times,dtype=np.int64)-minutes*60000
        minute_epoch=open_ms//60000; hour=(open_ms//3600000)%24; day_index=open_ms//86400000
    cents=(np.floor(np.abs(close)*100+1e-9).astype(np.int64)%100)
    if kind == "minute_mod5": good=(minute%5)==0
    elif kind == "minute_prime": good=np.isin(minute,np.array([2,3,5,7,11,13,17,19,23,29,31,37,41,43,47,53,59],dtype=np.int64))
    elif kind == "hour_odd": good=(hour%2)==1
    elif kind == "hour_fib": good=np.isin(hour,np.array([0,1,2,3,5,8,13,21],dtype=np.int64))
    elif kind == "price_integer_tail7": good=(np.floor(np.abs(close)).astype(np.int64)%10)==7
    elif kind == "price_cents_0050": good=(cents==0)|(cents==50)
    elif kind == "volume_tail7":
        finite = np.isfinite(volume)
        ivol = np.where(finite, np.floor(np.abs(volume)), 0).astype(np.int64)
        good = finite & ((ivol % 10) == 7)
    elif kind == "trades_odd": good=np.isfinite(trades)&((np.nan_to_num(trades).astype(np.int64)%2)==1)
    elif kind == "time_price_match": good=(minute==(cents%60))
    elif kind == "time_hash20": good=((minute_epoch*1103515245+12345)%100)<20
    elif kind == "minute_mod13": good=(minute%13)==0
    elif kind == "minute_13_37": good=(minute==13)|(minute==37)
    elif kind == "minute_double": good=np.isin(minute,np.array([0,11,22,33,44,55],dtype=np.int64))
    elif kind == "day_hour_match": good=((day_index%24)==hour)
    elif kind == "price_cents_special": good=np.isin(cents,np.array([13,42,69],dtype=np.int64))
    elif kind == "avg_size_tail7":
        scaled=np.floor(np.abs(np.nan_to_num(avg_size))*1_000_000+1e-9).astype(np.int64)
        good=np.isfinite(avg_size)&((scaled%10)==7)
    else:
        raise ValueError(kind)
    return long & good, short & good
