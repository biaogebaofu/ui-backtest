"""第五轮30/34/37/38/73：因果统计模型，参数和求解器版本固定。

每个训练快照只读当时成熟样本；audit记录实际发出的预测与训练参数，
不能使用后来重训的预测回填历史。研究往返成本默认1bp（示例），不代表实盘费率。
"""
from __future__ import annotations

import numpy as np
from numba import njit

VERSION = "fifth-stat-models-20260911-v1"
IMPLEMENTED = (30, 34, 37, 38, 73)
MODEL_PARAMETERS = {
    30: {"window": 120, "lambda_sigma": 3., "segment_tolerance_sigma": 1e-6,
         "solver": "box-dual-active-set", "solver_tolerance": 1e-10},
    34: {"window": 120, "embedding": 3, "delay": 1, "distance": .5,
         "theiler": 2, "minimum_diagonal": 3, "recompute": 5},
    37: {"samples": 1440, "lags": 5, "horizon": 1, "recompute": 60,
         "solver": "OLS-SVD", "rcond": 1e-12},
    38: {"window": 720, "horizon": 10, "recompute": 30,
         "half_life_min": 3., "half_life_max": 120., "rcond": 1e-12},
    73: {"samples": 2880, "flow_lags": 3, "horizon": 3, "recompute": 60,
         "ridge_alpha": 1., "standardization": "train-only-ddof0"},
}


@njit(cache=True, nogil=True)
def tv_denoise(values, penalty):
    """解sum((x-y)^2)+penalty*sum(abs(diff(x)))；自写盒约束对偶活动集。

    对偶三对角 Hessian=(2,-1,-1)，盒半宽penalty/2。自由连续区间用
    Thomas消元精确求解；边界KKT条件不满足才释放约束，不用后验分段回填。
    返回(解, 是否通过KKT收敛)。无迭代容差达标时不生成交易信号。
    """
    n = len(values)
    if n < 2 or penalty == 0:
        return values.copy(), True
    m = n - 1
    bound = penalty / 2.
    p = np.zeros(m)
    active = np.zeros(m, dtype=np.int8)
    rhs = np.diff(values)
    tolerance = 1e-10 * max(1e-15, np.max(np.abs(values)), penalty)
    diagonal = np.empty(m)
    scratch = np.empty(m)
    for _ in range(10 * n + 100):
        target = p.copy()
        i = 0
        while i < m:
            if active[i] != 0:
                i += 1
                continue
            first = i
            while i + 1 < m and active[i + 1] == 0:
                i += 1
            last = i
            diagonal[first] = 2.
            scratch[first] = rhs[first] + (p[first - 1] if first else 0.)
            if first == last:
                scratch[first] += p[last + 1] if last + 1 < m else 0.
            for j in range(first + 1, last + 1):
                diagonal[j] = 2. - 1. / diagonal[j - 1]
                scratch[j] = rhs[j] + scratch[j - 1] / diagonal[j - 1]
                if j == last and last + 1 < m:
                    scratch[j] += p[last + 1]
            target[last] = scratch[last] / diagonal[last]
            for j in range(last - 1, first - 1, -1):
                target[j] = (scratch[j] + target[j + 1]) / diagonal[j]
            i += 1
        step = 1.
        hit = -1
        hit_side = 0
        for j in range(m):
            if active[j] != 0:
                continue
            move = target[j] - p[j]
            if target[j] > bound and move > 0:
                alpha = (bound - p[j]) / move
                if alpha < step:
                    step = max(0., alpha); hit = j; hit_side = 1
            elif target[j] < -bound and move < 0:
                alpha = (-bound - p[j]) / move
                if alpha < step:
                    step = max(0., alpha); hit = j; hit_side = -1
        p += step * (target - p)
        if hit >= 0:
            p[hit] = hit_side * bound
            active[hit] = hit_side
            continue
        release = -1
        worst = tolerance
        for j in range(m):
            gradient = 2*p[j] - rhs[j]
            if j:
                gradient -= p[j-1]
            if j+1 < m:
                gradient -= p[j+1]
            violation = gradient * active[j]
            if violation > worst:
                worst = violation; release = j
        if release < 0:
            out = values.copy()
            out[:-1] += p
            out[1:] -= p
            return out, True
        active[release] = 0
    return np.full(n, np.nan), False


@njit(cache=True, nogil=True)
def recurrence_determinism(window):
    """三维延迟1递归图：排除|i-j|<=2，只计长度>=3的对角连续点。"""
    std = np.std(window)
    if not np.isfinite(std) or std <= 0:
        return np.nan
    x = (window - np.mean(window)) / std
    size = len(x) - 2
    total = 0
    deterministic = 0
    # 上半图与下半图严格镜像，比例相同，只遍历上半图。
    for offset in range(3, size):
        run = 0
        for i in range(size - offset):
            distance = 0.
            for k in range(3):
                distance += (x[i+k] - x[i+offset+k])**2
            if distance < .25:
                total += 1
                run += 1
            else:
                if run >= 3:
                    deterministic += run
                run = 0
        if run >= 3:
            deterministic += run
    return deterministic / total if total else np.nan


@njit(cache=True, nogil=True)
def _tv_path(returns):
    n = len(returns)
    mean = np.full(n, np.nan)
    duration = np.zeros(n, dtype=np.int64)
    prior = np.full(n, np.nan)
    sigma = np.full(n, np.nan)
    failed = np.zeros(n, dtype=np.bool_)
    for t in range(121, n):
        window = returns[t-120:t]
        if not np.all(np.isfinite(window)):
            continue
        s = np.std(window)
        if s <= 0:
            continue
        fitted, ok = tv_denoise(window, 3*s)
        if not ok:
            failed[t] = True
            continue
        start = 119
        while start > 0 and abs(fitted[start] - fitted[start-1]) < 1e-6*s:
            start -= 1
        mean[t] = fitted[-1]
        duration[t] = 120-start
        prior[t] = fitted[start-1] if start else np.nan
        sigma[t] = s
    return mean, duration, prior, sigma, failed


@njit(cache=True, nogil=True)
def _det_path(returns):
    values = np.full(len(returns), np.nan)
    for t in range(121, len(returns), 5):
        window = returns[t-120:t]
        if np.all(np.isfinite(window)):
            values[t] = recurrence_determinism(window)
    return values


def _first(mask):
    return mask & ~np.r_[False, mask[:-1]]


def _lag(values, periods):
    out = np.full(len(values), np.nan)
    if len(values) > periods:
        out[periods:] = values[:-periods]
    return out


def _fit_regression(features, targets, ridge=0.):
    if not np.all(np.isfinite(features)) or not np.all(np.isfinite(targets)):
        return None
    if ridge:
        center = features.mean(axis=0)
        scale = features.std(axis=0)
        if np.any(scale <= 1e-15):
            return None
        x = (features-center)/scale
        yc = targets-targets.mean()
        slopes = np.linalg.solve(x.T@x + ridge*np.eye(x.shape[1]), x.T@yc) / scale
        return np.r_[targets.mean()-center@slopes, slopes]
    design = np.column_stack((np.ones(len(targets)), features))
    coef, _residual, rank, _s = np.linalg.lstsq(design, targets, rcond=1e-12)
    return coef if rank == design.shape[1] else None


def ar_forecasts(close, horizon=1, training_gap=0):
    """AR(5)共同预测核心；共形版本只改h和显式训练/校准隔离长度。"""
    logc = np.log(np.asarray(close, dtype=float))
    n = len(logc); r = logc-_lag(logc, 1)
    features = np.column_stack([_lag(r, j) if j else r for j in range(5)])
    targets = np.full(n, np.nan)
    if n > horizon:
        targets[:-horizon] = logc[horizon:]-logc[:-horizon]
    predictions = np.full(n, np.nan); cutoff = np.full(n, -1, dtype=np.int64); models = []
    earliest = 5+1440-1+horizon+training_gap
    coef = None; fit_cutoff = -1; next_fit = earliest
    for t in range(earliest, n):
        if not np.all(np.isfinite(features[t])):
            coef = None; next_fit = t+1
            continue
        if t >= next_fit:
            end = t-training_gap
            last = end-horizon; first = last-1440+1
            coef = _fit_regression(features[first:last+1], targets[first:last+1])
            fit_cutoff = end; next_fit = t+60
            models.append(dict(at=t, train_start=first, train_last_anchor=last, label_end=end,
                               samples=1440, coefficients=None if coef is None else coef.tolist(),
                               features=[f"r_lag{j}" for j in range(5)], horizon=horizon,
                               training_gap=training_gap))
        if coef is not None:
            predictions[t] = coef[0]+features[t]@coef[1:]
            cutoff[t] = fit_cutoff
    return predictions, cutoff, models


def stat_model_signals(number, close, atr, cost=0.0001, volume=None, delta=None, audit=None):
    """返回独立首次信号；audit可存到该数据指纹对应缓存，供导出逐事件复查。

    audit预测与模型数组的下标就是当时原生K线；模型37/73训练标签允许
    成熟到当前收盘，38的“前720根”严格截至t-1。输入缺失会重置模型。
    """
    number = int(number)
    if number not in IMPLEMENTED:
        raise ValueError("不是已实现的第五轮统计模型")
    c = np.asarray(close, dtype=float)
    a = np.asarray(atr, dtype=float)
    if c.ndim != 1 or a.shape != c.shape or not np.isfinite(cost) or cost < 0:
        raise ValueError("统计模型输入长度或成本无效")
    n = len(c)
    logc = np.log(np.where(c > 0, c, np.nan))
    r = logc-_lag(logc, 1)
    up = np.zeros(n, dtype=bool); down = up.copy()
    prediction = np.full(n, np.nan)
    cutoff = np.full(n, -1, dtype=np.int64)
    models = []
    details = {}
    if number == 30:
        mean, duration, prior, sigma, failed = _tv_path(r)
        up = _first((duration >= 5) & (prior <= 0) & (mean > .2*sigma))
        down = _first((duration >= 5) & (prior >= 0) & (mean < -.2*sigma))
        details = dict(segment_mean=mean, segment_duration=duration, prior_segment_mean=prior,
                       sigma=sigma, solver_failed=failed)
        cutoff[np.isfinite(mean)] = np.flatnonzero(np.isfinite(mean))-1
    elif number == 34:
        det = _det_path(r)
        previous = np.nan
        for t in range(121, n, 5):
            current = det[t]
            if np.isfinite(current) and np.isfinite(previous) and previous <= .6 < current:
                direction = logc[t]-logc[t-10]
                up[t] = direction > 0; down[t] = direction < 0
            previous = current
        details = dict(determinism=det)
        cutoff[np.isfinite(det)] = np.flatnonzero(np.isfinite(det))-1
    elif number == 37:
        prediction, cutoff, models = ar_forecasts(c)
        up = _first(prediction > cost+.0001)
        down = _first(prediction < -(cost+.0001))
    elif number == 73:
        horizon, count, stride = 3, 2880, 60
        v = np.asarray(volume, dtype=float); flow = np.asarray(delta, dtype=float)
        if v.shape != c.shape or flow.shape != c.shape:
            raise ValueError("F5-073需要同长度真实成交量和净主动量")
        ratio = np.divide(flow, v, out=np.full(n, np.nan), where=(v > 0)&(np.abs(flow)<=v))
        features = np.column_stack((ratio, _lag(ratio, 1), _lag(ratio, 2), r))
        first_anchor = 2
        names = ["delta_ratio", "delta_ratio_lag1", "delta_ratio_lag2", "r"]
        earliest = first_anchor+count-1+horizon
        targets = np.full(n, np.nan)
        if n > horizon:
            targets[:-horizon] = logc[horizon:]-logc[:-horizon]
        coef = None; model_cutoff = -1; next_fit = earliest
        for t in range(earliest, n):
            if not np.all(np.isfinite(features[t])):
                coef = None; next_fit = t+1
                continue
            if t >= next_fit:
                last_anchor = t-horizon
                first = last_anchor-count+1
                coef = _fit_regression(features[first:last_anchor+1], targets[first:last_anchor+1], 1.)
                model_cutoff = t
                next_fit = t+stride
                models.append(dict(at=t, train_start=first, train_last_anchor=last_anchor,
                                   label_end=t, samples=count, coefficients=None if coef is None else coef.tolist(),
                                   features=names, horizon=horizon))
            if coef is None:
                continue
            prediction[t] = coef[0]+features[t]@coef[1:]
            cutoff[t] = model_cutoff
            enabled = (coef[1:4].sum() > 0 and abs(logc[t]-logc[t-3]) < .3*a[t]/c[t])
            up[t] = enabled and prediction[t] > cost+.0001
            down[t] = enabled and prediction[t] < -(cost+.0001)
        up = _first(up); down = _first(down)
    else:
        coef = None; model_cutoff = -1; mu = scale = phi = np.nan
        for t in range(720, n):
            if not np.isfinite(r[t]):
                coef = None; mu = scale = phi = np.nan
                continue
            if (t-720) % 30 == 0:
                x = logc[t-720:t]
                coef = _fit_regression(x[:-1, None], np.diff(x))
                model_cutoff = t-1
                mu = scale = phi = np.nan
                if coef is not None:
                    intercept, slope = coef
                    phi = 1+slope
                    if slope < 0 and 0 < phi < 1:
                        half_life = -np.log(2)/np.log(phi)
                        if 3 <= half_life <= 120:
                            mu = -intercept/slope
                            residual = np.diff(x)-(intercept+slope*x[:-1])
                            scale = residual.std(ddof=0)/np.sqrt(1-phi*phi)
                models.append(dict(at=t, train_start=t-720, train_last_anchor=t-2,
                                   label_end=t-1, samples=719,
                                   coefficients=None if coef is None else coef.tolist(),
                                   equilibrium=mu, stationary_scale=scale, phi=phi))
            if not np.isfinite(mu) or not np.isfinite(scale) or scale <= 0 or not np.isfinite(r[t]):
                continue
            prediction[t] = (mu-logc[t])*(1-phi**10)
            cutoff[t] = model_cutoff
            up[t] = logc[t] < mu-2*scale and r[t] > 0 and prediction[t] > cost+.0001
            down[t] = logc[t] > mu+2*scale and r[t] < 0 and prediction[t] < -(cost+.0001)
        up = _first(up); down = _first(down)
    if audit is not None:
        audit.update(version=VERSION, number=number, parameters=dict(MODEL_PARAMETERS[number]),
                     cost=float(cost), prediction=prediction, training_cutoff=cutoff, models=models,
                     **details)
    return up, down


def masks(number, o, h, l, c, v, ct, extras, timeframe="1m"):
    """第五轮分段路由适配；真实时间戳用于模型及预测归档。"""
    from fifth_batch import _atr
    from fifth_model_audit import record_fit, record_predictions
    close = np.asarray(c, dtype=float)
    times = np.asarray(ct, dtype=np.int64)
    if times.shape != close.shape:
        raise ValueError("统计模型归档时间长度不一致")
    atr, _tr = _atr(np.asarray(h, dtype=float), np.asarray(l, dtype=float), close)
    extras = extras or {}
    delta = extras.get("delta_base")
    if delta is None and "taker_buy_base" in extras:
        delta = 2*np.asarray(extras["taker_buy_base"])-np.asarray(v)
    evidence = {}
    up, down = stat_model_signals(number, close, atr, volume=v, delta=delta, audit=evidence)
    settings = dict(evidence["parameters"], version=VERSION, timeframe=timeframe,
                    cost=evidence["cost"], label="log(C[t+h]/C[t])")
    for model in evidence["models"]:
        snapshot = {key: (None if isinstance(value, float) and not np.isfinite(value) else value)
                    for key, value in model.items()}
        for key in ("train_start", "train_last_anchor", "label_end"):
            snapshot[key+"_utc_ms"] = int(times[model[key]])
        record_fit(number, int(times[model["at"]]), snapshot, settings)
    if len(times) and not evidence["models"]:
        record_fit(number, int(times[0]), {"kind": "rolling_statistic"}, settings)
    extra = {key: value for key, value in evidence.items()
             if isinstance(value, np.ndarray) and key != "prediction"}
    cutoff = evidence["training_cutoff"]
    extra["training_cutoff_utc_ms"] = np.where(cutoff >= 0, times[np.maximum(cutoff, 0)], -1) if len(times) else cutoff.copy()
    extra.update(long_signal=up, short_signal=down)
    record_predictions(number, times, evidence["prediction"], **extra)
    return up, down
