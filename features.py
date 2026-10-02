import os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("BT_CSV", "")

def ema(x, n):
    a = 2.0 / (n + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out

def macd(close, f=12, s=26, sig=9):
    dif = ema(close, f) - ema(close, s)
    dea = ema(dif, sig)
    return dif, dea, 2.0 * (dif - dea)

def load():
    if not SRC:
        raise ValueError("Set BT_CSV to your local 1-minute OHLCV CSV path")
    df = pd.read_csv(SRC)
    df["dt"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    start = os.environ.get("BT_START")
    end = os.environ.get("BT_END")
    if start:
        df = df[df["dt"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["dt"] < pd.Timestamp(end, tz="UTC")]
    df = df.sort_values("openTime").drop_duplicates("openTime")
    expected = np.arange(df["openTime"].iloc[0], df["openTime"].iloc[-1] + 60000, 60000, dtype=np.int64)
    if len(expected) != len(df) or not np.array_equal(expected, df["openTime"].to_numpy(np.int64)):
        raise ValueError("1分钟数据存在缺口或重复时间戳")
    ms = df["openTime"].to_numpy(np.int64)
    return df.set_index("dt")[["open", "high", "low", "close"]].assign(ms=ms)

TF = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h"}
MIN = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}

def build():
    df = load()
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    ct1 = df["ms"].to_numpy(np.int64) + 60000
    out = dict(close=close, high=high, low=low, ct1=ct1,
               openms=df["ms"].to_numpy(np.int64))
    dif, dea, hist = macd(close)
    out.update(m1_dif=dif, m1_dea=dea, m1_hist=hist,
               ma120=pd.Series(close).rolling(120).mean().to_numpy())
    for k, rule in TF.items():
        g = df.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "ms": "first"}).dropna()
        d, e, h = macd(g["close"].to_numpy(float))
        out[k + "_dif"] = d
        out[k + "_dea"] = e
        out[k + "_hist"] = h
        out[k + "_high"] = g["high"].to_numpy(float)
        out[k + "_low"] = g["low"].to_numpy(float)
        out[k + "_ct"] = g["ms"].to_numpy(np.int64) + MIN[k] * 60000
        out[k + "_map"] = (np.searchsorted(out[k + "_ct"], ct1, side="right") - 1).astype(np.int64)
    np.savez_compressed(os.path.join(BASE, "feat.npz"), **out)
    print("1m bars", len(close), pd.to_datetime(out["openms"][0], unit="ms", utc=True),
          pd.to_datetime(out["ct1"][-1], unit="ms", utc=True))
    for k in TF:
        print(k, len(out[k + "_dif"]))

if __name__ == "__main__":
    build()
