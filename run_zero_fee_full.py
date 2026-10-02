import csv
import itertools
import os
import time

import numpy as np
import pandas as pd
from numba import njit

import engine as E
import run_all as R


BASE = os.path.dirname(os.path.abspath(__file__))
N = E.n
CLOSE = E.close.astype(np.float64)
HIGH = E.high.astype(np.float64)
LOW = E.low.astype(np.float64)
DAYS = (E.D["ct1"][-1] - E.D["openms"][0]) / 86_400_000.0
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
SIZES = np.array(
    [i / 10 for i in range(1, 11)] + [2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 50, 100],
    dtype=np.float64,
)
SIZE_LABELS = [f"{i}/10" for i in range(1, 11)] + [f"{i}x" for i in range(2, 11)] + ["20x", "50x", "100x"]


def stop_configs():
    configs = [("S1", ("S1",))]
    for tf in TIMEFRAMES:
        s3 = f"S3_{tf}"
        s4 = f"S4_{tf}"
        configs.extend(
            [
                (s3, (s3,)),
                (s4, (s4,)),
                (f"S1+{s3}", ("S1", s3)),
                (f"S1+{s4}", ("S1", s4)),
                (f"{s3}+{s4}", (s3, s4)),
                (f"S1+{s3}+{s4}", ("S1", s3, s4)),
            ]
        )
    return configs


STOP_CONFIGS = stop_configs()


def stop_label(code):
    parts = []
    for part in code.split("+"):
        if part == "S1":
            parts.append("①1分序列")
        elif part.startswith("S3_"):
            parts.append(f"③{part[3:]}前轮极值")
        elif part.startswith("S4_"):
            parts.append(f"④{part[3:]}MACD反转")
    return "+".join(parts)


def direction_run_id(direction):
    changed = np.ones(len(direction), dtype=np.bool_)
    changed[1:] = direction[1:] != direction[:-1]
    return np.cumsum(changed).astype(np.int32)


def build_candidates(S, HI, c4, c1, c15, c5, cm):
    d1 = S["d1"]
    ma = S["ma120"]
    long_trigger = d1 == 1
    short_trigger = d1 == -1
    if cm == 2:
        long_trigger &= CLOSE > ma
        short_trigger &= CLOSE < ma
    long_ok = HI["4h"][c4][0] & HI["1h"][c1][0] & HI["15m"][c15][0] & HI["5m"][c5][0] & long_trigger
    short_ok = HI["4h"][c4][1] & HI["1h"][c1][1] & HI["15m"][c15][1] & HI["5m"][c5][1] & short_trigger
    cand = np.flatnonzero(long_ok | short_ok).astype(np.int64)
    cdir = np.where(long_ok[cand], 1, -1).astype(np.int8)
    return cand, cdir


def component_exit(EX, component, long_side):
    side = 0 if long_side else 1
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


def make_stop_data(EX, components):
    x_long = np.full(N, N, dtype=np.int64)
    x_short = np.full(N, N, dtype=np.int64)
    p_long = np.full(N, CLOSE[-1], dtype=np.float64)
    p_short = np.full(N, CLOSE[-1], dtype=np.float64)
    for component in components:
        for long_side, x, price in (
            (True, x_long, p_long),
            (False, x_short, p_short),
        ):
            idx, px, is_intrabar = component_exit(EX, component, long_side)
            better = idx <= x if is_intrabar else idx < x
            better &= idx < N
            x[better] = idx[better]
            price[better] = px[better]
    x_long = np.minimum(x_long, N - 1)
    x_short = np.minimum(x_short, N - 1)
    return x_long, x_short, p_long, p_short


@njit(cache=True)
def simulate_all_sizes(cand, cdir, run_id, x_long, x_short, p_long, p_short, year_bin, close, high, low, sizes):
    n = len(close)
    eq = np.ones(len(sizes), dtype=np.float64)
    peak = np.ones(len(sizes), dtype=np.float64)
    mdd = np.zeros(len(sizes), dtype=np.float64)
    liquidations = np.zeros(len(sizes), dtype=np.int64)
    nt = 0
    gross = 0.0
    gross2 = 0.0
    hold = 0
    nlong = 0
    half1 = 0.0
    half2 = 0.0
    yearly = np.zeros(2, dtype=np.float64)
    last_run = -1
    p = 0
    m = len(cand)
    while True:
        k = np.searchsorted(cand, p)
        if k >= m:
            break
        i = int(cand[k])
        if run_id[i] == last_run:
            p = i + 1
            continue
        side = int(cdir[k])
        x = int(x_long[i] if side > 0 else x_short[i])
        exit_price = p_long[i] if side > 0 else p_short[i]
        if x <= i:
            p = i + 1
            continue
        entry = close[i]
        ret = (exit_price / entry - 1.0) * side
        adverse = 0.0
        if side > 0:
            worst = low[i]
            for j in range(i + 1, x + 1):
                if low[j] < worst:
                    worst = low[j]
            adverse = 1.0 - worst / entry
        else:
            worst = high[i]
            for j in range(i + 1, x + 1):
                if high[j] > worst:
                    worst = high[j]
            adverse = worst / entry - 1.0
        nt += 1
        gross += ret
        gross2 += ret * ret
        hold += x - i
        if side > 0:
            nlong += 1
        if i < n // 2:
            half1 += ret
        else:
            half2 += ret
        yearly[year_bin[i]] += ret
        for z in range(len(sizes)):
            mult = sizes[z]
            leverage = mult if mult > 1.0 else 1.0
            if adverse >= 0.85 / leverage:
                trade_ret = -0.85
                liquidations[z] += 1
            else:
                trade_ret = mult * ret
            factor = 1.0 + trade_ret
            if factor < 1e-12:
                factor = 1e-12
            eq[z] *= factor
            if eq[z] > 1e300:
                eq[z] = 1e300
            if eq[z] > peak[z]:
                peak[z] = eq[z]
            dd = 1.0 - eq[z] / peak[z]
            if dd > mdd[z]:
                mdd[z] = dd
        last_run = run_id[i]
        p = x + 1
    return nt, gross, gross2, hold, nlong, half1, half2, yearly, eq, mdd, liquidations


def build_field(field):
    S, EX, HI, _ = R.build(field)
    run_id = direction_run_id(S["d1"])
    entries = []
    for c4, c1, c15, c5, cm in itertools.product([0, 1, 2], [0, 1, 2], [0, 1, 2], [0, 1, 2], [1, 2]):
        cand, cdir = build_candidates(S, HI, c4, c1, c15, c5, cm)
        entries.append((c4, c1, c15, c5, cm, cand, cdir))
    return S, EX, run_id, entries


def base_row(strategy_id, field, cases, stop_code, result):
    c4, c1, c15, c5, cm = cases
    nt, gross, gross2, hold, nlong, half1, half2, yearly, eq, mdd, liq = result
    var = max(0.0, (gross2 - gross * gross / nt) / (nt - 1)) if nt > 1 else 0.0
    sd = np.sqrt(var)
    t0 = gross / nt / np.sqrt(var / nt) if nt > 1 and var > 0 else np.nan
    one_x = 9
    return {
        "strategy_id": strategy_id,
        "field": field,
        "c4h": c4,
        "c1h": c1,
        "c15m": c15,
        "c5m": c5,
        "c1m": cm,
        "nper": 1 + (c4 > 0) + (c1 > 0) + (c15 > 0) + (c5 > 0),
        "stop": stop_code,
        "stop_label": stop_label(stop_code),
        "nt": nt,
        "tpd": nt / DAYS,
        "orders_per_day": nt * 2 / DAYS,
        "hold": hold / nt,
        "long_ratio": nlong / nt,
        "gross_bp": gross / nt * 1e4,
        "sd_bp": sd * 1e4,
        "t0": t0,
        "gross_return": gross,
        "half1": half1,
        "half2": half2,
        "y2025": yearly[0],
        "y2026": yearly[1],
        "final_1x": eq[one_x] * 100.0,
        "return_1x": eq[one_x] - 1.0,
        "mdd_1x": mdd[one_x],
        "liq_1x": int(liq[one_x]),
    }


def record_recommended(row):
    field = row["field"]
    S, EX, HI, _ = R.build(field)
    run_id = direction_run_id(S["d1"])
    cand, cdir = build_candidates(S, HI, int(row.c4h), int(row.c1h), int(row.c15m), int(row.c5m), int(row.c1m))
    components = dict(STOP_CONFIGS)[row.stop]
    x_long, x_short, p_long, p_short = make_stop_data(EX, components)
    rows = []
    last_run = -1
    p = 0
    while True:
        k = cand.searchsorted(p)
        if k >= len(cand):
            break
        i = int(cand[k])
        if run_id[i] == last_run:
            p = i + 1
            continue
        side = int(cdir[k])
        x = int(x_long[i] if side > 0 else x_short[i])
        px = float(p_long[i] if side > 0 else p_short[i])
        if x <= i:
            p = i + 1
            continue
        entry = CLOSE[i]
        ret = (px / entry - 1.0) * side
        adverse = 1.0 - LOW[i : x + 1].min() / entry if side > 0 else HIGH[i : x + 1].max() / entry - 1.0
        rows.append((i, x, side, entry, px, x - i, ret, adverse))
        last_run = run_id[i]
        p = x + 1
    out = pd.DataFrame(rows, columns=["entry_i", "exit_i", "side", "entry", "exit", "hold_min", "return", "mae"])
    out["entry_time_utc"] = pd.to_datetime(E.D["openms"][out.entry_i], unit="ms", utc=True).tz_localize(None)
    out["exit_time_utc"] = pd.to_datetime(E.D["openms"][out.exit_i], unit="ms", utc=True).tz_localize(None)
    out["direction"] = np.where(out.side > 0, "多", "空")
    return out[["entry_time_utc", "exit_time_utc", "direction", "entry", "exit", "hold_min", "return", "mae"]]


def main():
    if len(STOP_CONFIGS) != 31 or len({x[0] for x in STOP_CONFIGS}) != 31:
        raise AssertionError("止损组合必须是31种且不能重复")
    years = pd.to_datetime(E.D["openms"], unit="ms", utc=True).year.values
    year_bin = (years >= 2026).astype(np.int8)
    base_rows = []
    leverage_path = os.path.join(BASE, "zero_fee_leverage_all.csv")
    strategy_id = 1
    started = time.time()
    with open(leverage_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["strategy_id", "size", "mult", "final_equity", "return", "max_drawdown", "liquidations"])
        for field in ["dif", "hist"]:
            _, EX, run_id, entries = build_field(field)
            for stop_index, (stop_code, components) in enumerate(STOP_CONFIGS, 1):
                stop_data = make_stop_data(EX, components)
                for c4, c1, c15, c5, cm, cand, cdir in entries:
                    if len(cand) == 0:
                        continue
                    result = simulate_all_sizes(cand, cdir, run_id, *stop_data, year_bin, CLOSE, HIGH, LOW, SIZES)
                    if result[0] == 0:
                        continue
                    row = base_row(strategy_id, field, (c4, c1, c15, c5, cm), stop_code, result)
                    base_rows.append(row)
                    eq, mdd, liq = result[8], result[9], result[10]
                    for z, label in enumerate(SIZE_LABELS):
                        writer.writerow([strategy_id, label, SIZES[z], eq[z] * 100.0, eq[z] - 1.0, mdd[z], int(liq[z])])
                    strategy_id += 1
                print(field, f"stop {stop_index:02d}/31", stop_code, "elapsed", round(time.time() - started, 1), "s", flush=True)
    base = pd.DataFrame(base_rows)
    base.to_csv(os.path.join(BASE, "zero_fee_base_all.csv"), index=False, encoding="utf-8-sig")
    base.to_pickle(os.path.join(BASE, "zero_fee_base_all.pkl"))
    lev = pd.read_csv(leverage_path)
    eligible = base[(base.t0 >= 2) & (base.y2025 > 0) & (base.y2026 > 0)]
    safe = lev[(lev.liquidations == 0) & (lev.max_drawdown <= 0.30)].merge(
        eligible[["strategy_id", "t0", "gross_bp", "nt"]], on="strategy_id", how="inner"
    )
    if safe.empty:
        safe = lev[(lev.liquidations == 0) & (lev.max_drawdown <= 0.50)].merge(
            base[["strategy_id", "t0", "gross_bp", "nt"]], on="strategy_id", how="inner"
        )
    ranking = safe.sort_values(["final_equity", "t0"], ascending=False)
    ranking.to_csv(os.path.join(BASE, "zero_fee_safe_ranking.csv"), index=False, encoding="utf-8-sig")
    recommended_id = int(ranking.iloc[0].strategy_id)
    recommended = base.loc[base.strategy_id == recommended_id].iloc[0]
    trades = record_recommended(recommended)
    trades.to_csv(os.path.join(BASE, "zero_fee_recommended_trades.csv"), index=False, encoding="utf-8-sig")
    monthly = trades.assign(month=trades.entry_time_utc.dt.strftime("%Y-%m")).groupby("month").agg(
        trades=("return", "size"), gross_return=("return", "sum"), avg_bp=("return", lambda x: x.mean() * 1e4)
    ).reset_index()
    monthly.to_csv(os.path.join(BASE, "zero_fee_recommended_monthly.csv"), index=False, encoding="utf-8-sig")
    print("base strategies", len(base_rows), "leverage rows", len(lev))
    print("recommended strategy", recommended_id, "size", ranking.iloc[0]["size"], "final", ranking.iloc[0].final_equity)


if __name__ == "__main__":
    main()
