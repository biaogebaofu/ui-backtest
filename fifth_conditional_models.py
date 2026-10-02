"""第五轮条件模型78/85/89/92/93/97/100，全部按已成熟的真实历史统计。

未在研究卡片写死的数值约定在PARAMETERS中固定，并随模型审计归档。
"""
from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque

import numpy as np
import pandas as pd
from numba import njit
from scipy.optimize import minimize

from fifth_stat_models import _first, _lag, _fit_regression, ar_forecasts

VERSION = "fifth-conditional-models-20260911-v1"
IMPLEMENTED = (78, 85, 89, 92, 93, 97, 100)
PARAMETERS = {
    78: {"training": 1440, "recompute": 60, "lagged_activity_mean": 5,
         "solver": "Poisson-LBFGSB", "ftol": 1e-12, "gtol": 1e-7, "maxiter": 500},
    85: {"bucket_minutes": 15, "observed_minutes": 5, "training_buckets": 1000,
         "volume_baseline_buckets": 20, "ridge_alpha": 1., "recompute": "each_bucket",
         "range_definition": "(observed_high-observed_low)/bucket_open"},
    89: {"calendar_days": 60, "slot_minutes": 5, "minimum_slot_samples": 30,
         "shrinkage": 30., "standard_error": "unshrunk_slot_std_ddof0/sqrt(n)"},
    92: {"training_anchors": 10080, "horizon": 10, "atr_barrier": 1.,
         "volatility_quantile_window": 10080, "minimum_cell_samples": 100,
         "classes": "3volatility*3direction*3CLV", "laplace_pseudocount": 1},
    93: {"training_anchors": 10080, "horizon": 5, "sigma_window": 240,
         "word_length": 4, "minimum_word_samples": 50, "thresholds": [-1., -.25, .25, 1.]},
    97: {"training": 10080, "horizon": 10, "ridge_alpha": 10.,
         "recompute": "UTC_day", "trades_relative_mean": 20,
         "evaluation": "last_240_mature_predictions_of_current_frozen_fit"},
    100: {"training": 1440, "AR_lags": 5, "horizon": 5, "recompute": 60,
          "calibration": 1000, "training_gap": 1005, "quantile_rank": 901,
          "calibration_predictions": "stored_at_original_time"},
}


def _rolling(values, window, kind="mean", prior=True):
    series = pd.Series(np.asarray(values, dtype=float))
    if prior:
        series = series.shift(1)
    roll = series.rolling(window, min_periods=window)
    return roll.std(ddof=0).to_numpy() if kind == "std" else getattr(roll, kind)().to_numpy()


def _target(logc, horizon):
    out = np.full(len(logc), np.nan)
    if len(logc) > horizon:
        out[:-horizon] = logc[horizon:]-logc[:-horizon]
    return out


def poisson_fit(features, counts):
    """无额外正则的Poisson对数链接；优化只在本次训练段内标准化。"""
    if not np.all(np.isfinite(features)) or not np.all(np.isfinite(counts)) or np.any(counts < 0) or counts.mean() <= 0:
        return None
    mean = features.mean(axis=0); scale = features.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.)
    x = np.column_stack((np.ones(len(counts)), (features-mean)/scale))
    start = np.r_[np.log(counts.mean()), np.zeros(features.shape[1])]
    def objective(coef):
        eta = x@coef
        with np.errstate(over="ignore", invalid="ignore"):
            expected = np.exp(eta)
            value = np.mean(expected-counts*eta)
            gradient = x.T@(expected-counts)/len(counts)
        return value, gradient
    fit = minimize(objective, start, method="L-BFGS-B", jac=True,
                   options={"ftol": 1e-12, "gtol": 1e-7, "maxiter": 500})
    if not fit.success or not np.all(np.isfinite(fit.x)):
        return None
    slopes = fit.x[1:]/scale
    return np.r_[fit.x[0]-mean@slopes, slopes]


def _poisson(c, v, trades, delta, audit):
    n = len(c)
    features = np.column_stack((np.log(_rolling(trades, 5)), np.log(_rolling(v, 5))))
    prediction = np.full(n, np.nan); residual = prediction.copy()
    cutoff = np.full(n, -1, dtype=np.int64); models = []
    coef = None; fitted_at = -1
    for t in range(1445, n):
        if (t-1445) % 60 == 0:
            coef = poisson_fit(features[t-1440:t], trades[t-1440:t])
            fitted_at = t-1
            models.append(dict(at=t, train_start=t-1440, train_last_anchor=t-1, label_end=t-1,
                               coefficients=None if coef is None else coef.tolist()))
        if coef is None or not np.all(np.isfinite(features[t])) or trades[t] < 0:
            continue
        eta = coef[0]+features[t]@coef[1:]
        if not np.isfinite(eta) or eta > 700:
            continue
        expected = np.exp(eta)
        if expected <= 0:
            continue
        prediction[t] = expected; cutoff[t] = fitted_at
        residual[t] = (trades[t]-expected)/np.sqrt(expected)
    flow = np.divide(delta, v, out=np.full(n, np.nan), where=v > 0)
    up = _first((residual > 3)&(flow > .2)&(c > _lag(c, 1)))
    down = _first((residual > 3)&(flow < -.2)&(c < _lag(c, 1)))
    audit.update(prediction=prediction, residual=residual, training_cutoff=cutoff, models=models)
    return up, down


def _partial_bucket(o, h, l, c, v, ct, cost, audit):
    n = len(c); prediction = np.full(n, np.nan); cutoff = np.full(n, -1, dtype=np.int64)
    up = np.zeros(n, dtype=bool); down = up.copy(); models = []
    bucket = (ct-60000)//900000
    starts = np.r_[0, np.flatnonzero(bucket[1:] != bucket[:-1])+1] if n else np.array([], dtype=int)
    ends = np.r_[starts[1:], n]
    past_volumes = deque(maxlen=20); training = deque(maxlen=1000)
    previous_close = None; atr15 = np.nan; true_ranges = deque(maxlen=14)
    for start, end in zip(starts, ends):
        aligned = ct[start]-60000 == bucket[start]*900000
        if not aligned or end-start < 5:
            continue
        at = start+4
        baseline = np.mean(past_volumes) if len(past_volumes) == 20 else np.nan
        features = np.array([np.log(c[at]/o[start]),
                             (np.max(h[start:at+1])-np.min(l[start:at+1]))/o[start],
                             np.sum(v[start:at+1])/baseline if baseline > 0 else np.nan])
        if len(training) == 1000 and np.all(np.isfinite(features)):
            x = np.asarray([item[0] for item in training]); y = np.asarray([item[1] for item in training])
            coef = _fit_regression(x, y, 1.)
            models.append(dict(at=at, train_start=training[0][2], train_last_anchor=training[-1][2],
                               label_end=training[-1][3], coefficients=None if coef is None else coef.tolist(),
                               samples=1000))
            if coef is not None:
                prediction[at] = coef[0]+features@coef[1:]
                cutoff[at] = training[-1][3]
                quiet = np.isfinite(atr15) and abs(c[at]-o[start]) < .5*atr15
                up[at] = quiet and prediction[at] > cost+.0001
                down[at] = quiet and prediction[at] < -(cost+.0001)
        # 本桶标签在桶结束才可成熟，下一个桶的第j分钟才可能使用它。
        if end-start == 15 and ct[end-1] == (bucket[start]+1)*900000:
            if np.all(np.isfinite(features)):
                training.append((features.copy(), np.log(c[end-1]/c[at]), at, end-1))
            past_volumes.append(float(np.sum(v[start:at+1])))
            high = float(np.max(h[start:end])); low = float(np.min(l[start:end]))
            tr = high-low if previous_close is None else max(high-low, abs(high-previous_close), abs(low-previous_close))
            true_ranges.append(tr)
            if np.isfinite(atr15):
                atr15 = (13*atr15+tr)/14
            elif len(true_ranges) == 14:
                atr15 = np.mean(true_ranges)
            previous_close = c[end-1]
    audit.update(prediction=prediction, training_cutoff=cutoff, models=models)
    return up, down


def _seasonality(c, ct, cost, audit):
    n = len(c); logc = np.log(c); future = _target(logc, 5)
    prediction = np.full(n, np.nan); stderr = prediction.copy(); sample_count = np.zeros(n, dtype=np.int64)
    cutoff = np.full(n, -1, dtype=np.int64); up = np.zeros(n, dtype=bool); down = up.copy()
    slots = np.flatnonzero(ct % 300000 == 0)
    days = {}
    for i in slots:
        day = int(ct[i]//86400000); slot = int((ct[i]%86400000)//300000)
        if day not in days:
            days[day] = np.full(288, -1, dtype=np.int64)
        days[day][slot] = i
    complete = {}
    for day, indices in days.items():
        if np.all(indices >= 0) and indices[-1]+5 < n and np.all(np.isfinite(future[indices])):
            complete[day] = indices
    first_day = min(complete) if complete else None
    models = []
    for day, indices in sorted(days.items()):
        if first_day is None or day-first_day < 60 or indices[0] < 0:
            continue
        history = [complete[d] for d in range(day-60, day) if d in complete]
        if len(history) < 30:
            continue
        source = np.asarray(history); targets = future[source]
        count = len(history); all_mean = float(targets.mean()); slot_mean = targets.mean(axis=0)
        mean = (count*slot_mean+30*all_mean)/(count+30)
        error = targets.std(axis=0, ddof=0)/np.sqrt(count)
        label_end = int(source[-1, -1]+5)
        models.append(dict(at=int(indices[0]), train_start=int(source[0, 0]),
                           train_last_anchor=int(source[-1, -1]), label_end=label_end,
                           mean=mean.tolist(), standard_error=error.tolist(), samples=count))
        for slot, i in enumerate(indices):
            if i < 0:
                continue
            prediction[i] = mean[slot]; stderr[i] = error[slot]; sample_count[i] = count; cutoff[i] = label_end
            current_day_return = logc[i]-logc[indices[0]]
            up[i] = mean[slot]-1.5*error[slot] > cost and current_day_return >= np.log(.99)
            down[i] = mean[slot]+1.5*error[slot] < -cost and current_day_return <= np.log(1.01)
    audit.update(prediction=prediction, standard_error=stderr, sample_count=sample_count,
                 training_cutoff=cutoff, models=models)
    return up, down


@njit(cache=True, nogil=True)
def barrier_labels(close, atr):
    n = len(close); labels = np.full(n, -1, dtype=np.int8); maturity = np.full(n, -1, dtype=np.int64)
    for i in range(n):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        for j in range(i+1, min(n, i+11)):
            if not np.isfinite(close[j]):
                break
            if close[j] > close[i]+atr[i]:
                labels[i] = 2; maturity[i] = j; break
            if close[j] < close[i]-atr[i]:
                labels[i] = 0; maturity[i] = j; break
            if j == i+10:
                labels[i] = 1; maturity[i] = j
    return labels, maturity


@njit(cache=True, nogil=True)
def barrier_probabilities(cells, labels, maturity):
    n = len(cells); counts = np.zeros((27, 3), dtype=np.int64)
    probabilities = np.full((n, 3), np.nan); sample_count = np.zeros(n, dtype=np.int64)
    for t in range(n):
        leaving = t-10081
        if leaving >= 0 and cells[leaving] >= 0 and 0 <= maturity[leaving] < t:
            counts[cells[leaving], labels[leaving]] -= 1
        for anchor in range(max(0, t-10), t):
            if maturity[anchor] == t and cells[anchor] >= 0:
                counts[cells[anchor], labels[anchor]] += 1
        if cells[t] >= 0:
            cell = counts[cells[t]]; total = np.sum(cell)
            sample_count[t] = total
            if total >= 100:
                probabilities[t] = (cell+1)/(total+3)
    return probabilities, sample_count


def _barriers(h, l, c, atr, audit):
    n = len(c); volatility = atr/c
    roll = pd.Series(volatility).shift(1).rolling(10080, min_periods=10080)
    q1 = roll.quantile(1/3).to_numpy(); q2 = roll.quantile(2/3).to_numpy()
    vol = np.where(volatility < q1, 0, np.where(volatility > q2, 2, 1))
    movement = np.sign(c-_lag(c, 5)).astype(float)
    clv = np.divide(2*c-h-l, h-l, out=np.full(n, np.nan), where=h > l)
    body = np.where(clv < -1/3, 0, np.where(clv > 1/3, 2, 1))
    valid = np.isfinite(q1)&np.isfinite(q2)&np.isfinite(movement)&np.isfinite(clv)&(atr > 0)
    cells = np.full(n, -1, dtype=np.int64)
    cells[valid] = vol[valid]*9+(movement[valid].astype(int)+1)*3+body[valid]
    labels, maturity = barrier_labels(c, atr)
    probability, count = barrier_probabilities(cells, labels, maturity)
    new_cell = (cells >= 0)&(cells != np.r_[-1, cells[:-1]]) if n else np.zeros(0, dtype=bool)
    up = new_cell&(probability[:, 2] > .6)&(probability[:, 2]-probability[:, 0] > .2)
    down = new_cell&(probability[:, 0] > .6)&(probability[:, 0]-probability[:, 2] > .2)
    audit.update(prediction=probability[:, 2]-probability[:, 0], probabilities=probability,
                 sample_count=count, cell=cells, training_cutoff=np.where(count >= 100, np.arange(n), -1), models=[])
    return up, down


def _words(c, cost, audit):
    n = len(c); logc = np.log(c); r = logc-_lag(logc, 1); sigma = _rolling(r, 240, "std")
    z = np.divide(r, sigma, out=np.full(n, np.nan), where=sigma > 0)
    symbol = np.searchsorted(np.array([-1., -.25, .25, 1.]), z, side="right")
    words = np.full(n, -1, dtype=np.int64)
    if n >= 4:
        valid = np.isfinite(z)
        for j in range(1, 4):
            valid &= np.isfinite(_lag(z, j))
        for i in np.flatnonzero(valid):
            words[i] = int(symbol[i-3]*125+symbol[i-2]*25+symbol[i-1]*5+symbol[i])
    targets = _target(logc, 5); samples = [[] for _ in range(625)]
    prediction = np.full(n, np.nan); count = np.zeros(n, dtype=np.int64)
    for t in range(n):
        old = t-10081
        if old >= 0 and words[old] >= 0 and np.isfinite(targets[old]):
            bucket = samples[words[old]]; bucket.pop(bisect_left(bucket, targets[old]))
        anchor = t-5
        if anchor >= 0 and words[anchor] >= 0 and np.isfinite(targets[anchor]):
            insort(samples[words[anchor]], targets[anchor])
        if words[t] >= 0:
            bucket = samples[words[t]]; count[t] = len(bucket)
            if len(bucket) >= 50:
                middle = len(bucket)//2
                prediction[t] = bucket[middle] if len(bucket)%2 else (bucket[middle-1]+bucket[middle])/2
    up = _first(prediction > cost+.0001); down = _first(prediction < -(cost+.0001))
    audit.update(prediction=prediction, sample_count=count, word=words,
                 training_cutoff=np.where(count >= 50, np.arange(n), -1), models=[])
    return up, down


def _flow_ridge(c, v, trades, delta, ct, cost, audit):
    n = len(c); logc = np.log(c); r = logc-_lag(logc, 1)
    flow = np.divide(delta, v, out=np.full(n, np.nan), where=(v > 0)&(np.abs(delta)<=v))
    base = _rolling(trades, 20)
    activity = np.divide(trades, base, out=np.full(n, np.nan), where=base > 0)
    features = np.column_stack([_lag(flow, j) if j else flow for j in range(10)]+
                               [_lag(activity, j) if j else activity for j in range(10)]+
                               [r, _lag(r, 1), _lag(r, 2)])
    targets = _target(logc, 10); prediction = np.full(n, np.nan)
    cutoff = np.full(n, -1, dtype=np.int64); fit_id = cutoff.copy(); errors = deque(maxlen=240)
    mae = np.full(n, np.nan); outcome_std = mae.copy(); up = np.zeros(n, dtype=bool); down = up.copy()
    models = []; coef = None; day = None; model_at = -1
    for t in range(10118, n):
        current_day = int(ct[t]//86400000)
        if current_day != day:
            first = t-10-10080+1; last = t-10
            coef = _fit_regression(features[first:last+1], targets[first:last+1], 10.)
            day = current_day; model_at = t; errors.clear()
            models.append(dict(at=t, train_start=first, train_last_anchor=last, label_end=t,
                               samples=10080, coefficients=None if coef is None else coef.tolist()))
        matured = t-10
        if fit_id[matured] == model_at and np.isfinite(prediction[matured]) and np.isfinite(targets[matured]):
            errors.append((abs(prediction[matured]-targets[matured]), targets[matured]))
        if coef is None or not np.all(np.isfinite(features[t])):
            continue
        prediction[t] = coef[0]+features[t]@coef[1:]; cutoff[t] = model_at; fit_id[t] = model_at
        if len(errors) == 240:
            values = np.asarray(errors); mae[t] = values[:, 0].mean(); outcome_std[t] = values[:, 1].std(ddof=0)
            if mae[t] < outcome_std[t]:
                up[t] = prediction[t] > cost+.0001; down[t] = prediction[t] < -(cost+.0001)
    audit.update(prediction=prediction, training_cutoff=cutoff, model_id=fit_id,
                 mature_error_mae=mae, mature_outcome_std=outcome_std, models=models)
    return _first(up), _first(down)


def _conformal(c, cost, audit):
    n = len(c); logc = np.log(c); targets = _target(logc, 5)
    prediction, cutoff, models = ar_forecasts(c, horizon=5, training_gap=1005)
    calibration = deque(maxlen=1000)
    q = np.full(n, np.nan); first_anchor = np.full(n, -1, dtype=np.int64)
    count = np.zeros(n, dtype=np.int64)
    for t in range(n):
        anchor = t-5
        if anchor >= 0 and np.isfinite(prediction[anchor]) and np.isfinite(targets[anchor]):
            calibration.append((anchor, abs(prediction[anchor]-targets[anchor])))
        count[t] = len(calibration)
        if len(calibration) == 1000 and np.isfinite(prediction[t]) and cutoff[t] < calibration[0][0]:
            q[t] = np.partition(np.fromiter((x[1] for x in calibration), dtype=float), 900)[900]
            first_anchor[t] = calibration[0][0]
    up = _first(prediction-q > cost); down = _first(prediction+q < -cost)
    audit.update(prediction=prediction, training_cutoff=cutoff, calibration_quantile=q,
                 calibration_first_anchor=first_anchor, calibration_sample_count=count, models=models)
    return up, down


def conditional_signals(number, o, h, l, c, v, ct, extras=None, atr=None, cost=0.0001, audit=None):
    """纯数组计算接口，audit只含当时模型与真正输出的预测。"""
    number = int(number); arrays = [np.asarray(x, dtype=float) for x in (o, h, l, c, v)]
    o, h, l, c, v = arrays; n = len(c); ct = np.asarray(ct, dtype=np.int64)
    if any(x.shape != (n,) for x in arrays) or ct.shape != (n,) or not np.isfinite(cost) or cost < 0:
        raise ValueError("条件模型输入长度或成本无效")
    extras = extras or {}; evidence = {}
    if atr is None:
        from fifth_batch import _atr
        atr, _ = _atr(h, l, c)
    atr = np.asarray(atr, dtype=float)
    if atr.shape != c.shape:
        raise ValueError("条件模型ATR长度不一致")
    if number in (78, 97):
        trades = np.asarray(extras.get("trades"), dtype=float)
        delta = extras.get("delta_base")
        if delta is None and "taker_buy_base" in extras:
            delta = 2*np.asarray(extras["taker_buy_base"])-v
        delta = np.asarray(delta, dtype=float)
        if trades.shape != c.shape or delta.shape != c.shape:
            raise ValueError(f"F5-{number:03d}需要同长度真实成交笔数和净主动量")
        signals = _poisson(c, v, trades, delta, evidence) if number == 78 else _flow_ridge(c, v, trades, delta, ct, cost, evidence)
    elif number == 85:
        signals = _partial_bucket(o, h, l, c, v, ct, cost, evidence)
    elif number == 89:
        signals = _seasonality(c, ct, cost, evidence)
    elif number == 92:
        signals = _barriers(h, l, c, atr, evidence)
    elif number == 93:
        signals = _words(c, cost, evidence)
    elif number == 100:
        signals = _conformal(c, cost, evidence)
    else:
        raise ValueError("不是已实现的第五轮条件模型")
    if audit is not None:
        audit.update(version=VERSION, number=number, parameters=dict(PARAMETERS[number]), cost=float(cost), **evidence)
    return signals


def masks(number, o, h, l, c, v, ct, extras, timeframe="1m"):
    from fifth_model_audit import record_fit, record_predictions
    if number in (85, 89, 92, 93) and timeframe != "1m":
        raise ValueError("该条件模型固定使用1m/UTC日或部分15m桶")
    times = np.asarray(ct, dtype=np.int64); audit = {}
    up, down = conditional_signals(number, o, h, l, c, v, times, extras, audit=audit)
    settings = dict(audit["parameters"], version=VERSION, timeframe=timeframe, cost=audit["cost"])
    for model in audit["models"]:
        snapshot = dict(model)
        for key in ("train_start", "train_last_anchor", "label_end"):
            snapshot[key+"_utc_ms"] = int(times[model[key]])
        record_fit(number, int(times[model["at"]]), snapshot, settings)
    if len(times) and not audit["models"]:
        record_fit(number, int(times[0]), {"kind": "rolling_conditional_statistics"}, settings)
    extra = {key: value for key, value in audit.items() if isinstance(value, np.ndarray) and key != "prediction"}
    cutoff = audit["training_cutoff"]
    extra["training_cutoff_utc_ms"] = np.where(cutoff >= 0, times[np.maximum(cutoff, 0)], -1) if len(times) else cutoff.copy()
    extra.update(long_signal=up, short_signal=down)
    record_predictions(number, times, audit["prediction"], **extra)
    return up, down
