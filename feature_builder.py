from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from data_sources import (capabilities_from_frame, load_merged_source, resolve_sources,
                          source_bundle_fingerprint, validate_base_frame, resample_closed_frame,
                          timeframe_capabilities_from_features,
                          TIMEFRAME_MINUTES, ADDITIVE_FIELDS, STATE_FIELDS)

周期分钟 = TIMEFRAME_MINUTES
特征版本 = 7
可选可加总字段 = ADDITIVE_FIELDS
可选状态字段 = STATE_FIELDS


def ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    out = np.empty(len(values), dtype=np.float64)
    if not len(values):
        return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def macd(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    dif = ema(values, 12) - ema(values, 26)
    dea = ema(dif, 9)
    hist = 2.0 * (dif - dea)
    return dif, dea, hist


def rolling_mean(values: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(values).rolling(period, min_periods=period).mean().to_numpy(np.float64)


def source_fingerprint(path: Path) -> str:
    """兼容旧调用；单文件时仍返回稳定指纹。"""
    return source_bundle_fingerprint({"kline": str(path), "micro": "", "funding": "", "oi": "", "bundle": ""})


def _validate_frame(df: pd.DataFrame) -> pd.DataFrame:
    return validate_base_frame(df, minimum_rows=500)


def load_source(csv_path: Path, start: str | None = None, end: str | None = None,
                micro_path: str | None = None, funding_path: str | None = None,
                oi_path: str | None = None) -> pd.DataFrame:
    df = load_merged_source(csv_path, micro_path or None, funding_path or None, oi_path or None, start, end)
    df = _validate_frame(df)
    times = df.openTime.to_numpy(np.int64)
    dt = pd.to_datetime(times, unit="ms", utc=True)
    return df.set_index(dt)


def build_features(csv_path: str, cache_dir: str, start: str | None = None,
                   end: str | None = None, force: bool = False,
                   micro_path: str | None = None, funding_path: str | None = None,
                   oi_path: str | None = None, bundle_path: str | None = None) -> Path:
    out_dir = Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved = resolve_sources(csv_path or None, micro_path or None, funding_path or None,
                               oi_path or None, bundle_path or None, out_dir / "bundle_sources")
    data_path = out_dir / "features.npz"
    meta_path = out_dir / "features_meta.json"
    fingerprint = source_bundle_fingerprint(resolved)
    requested = {"fingerprint": fingerprint, "start": start or "", "end": end or "", "feature_version": 特征版本}
    if not force and data_path.exists() and meta_path.exists():
        try:
            cached_meta = json.loads(meta_path.read_text("utf-8"))
            if cached_meta.get("request") == requested:
                if "capabilities_by_timeframe" not in cached_meta:
                    with np.load(data_path) as data:
                        cached_meta["capabilities_by_timeframe"] = timeframe_capabilities_from_features(data)
                    meta_tmp = meta_path.with_name(f".features_meta.{uuid.uuid4().hex}.tmp")
                    try:
                        meta_tmp.write_text(json.dumps(cached_meta, ensure_ascii=False, indent=2), "utf-8")
                        os.replace(meta_tmp, meta_path)
                    finally:
                        meta_tmp.unlink(missing_ok=True)
                return data_path
        except Exception:
            pass

    df = load_source(Path(resolved["kline"]), start, end, resolved["micro"], resolved["funding"], resolved["oi"])
    times = df.openTime.to_numpy(np.int64)
    one_close_time = times + 60_000
    output: dict[str, np.ndarray] = {
        "openms": times,
        "ct1": one_close_time,
        "open": df.open.to_numpy(np.float64),
        "high": df.high.to_numpy(np.float64),
        "low": df.low.to_numpy(np.float64),
        "close": df.close.to_numpy(np.float64),
        "volume": df.volume.to_numpy(np.float64),
        "weekend": (pd.to_datetime(times, unit="ms", utc=True).dayofweek >= 5).astype(np.int8),
    }

    for tf, minutes in 周期分钟.items():
        frame = resample_closed_frame(df, minutes)
        values = frame.close.to_numpy(np.float64)
        dif, dea, hist = macd(values)
        prefix = tf + "_"
        for base in ("open", "high", "low", "close", "volume"):
            output[prefix + base] = frame[base].to_numpy(np.float64)
        for name in (*可选可加总字段, *可选状态字段):
            output[prefix + name] = (frame[name].to_numpy(np.float64) if name in frame.columns
                                     else np.full(len(frame), np.nan, dtype=np.float64))
        v = output[prefix + "volume"]
        buy = output[prefix + "taker_buy_base"]
        trades = output[prefix + "trades"]
        qv = output[prefix + "quote_volume"]
        buyq = output[prefix + "taker_buy_quote"]
        with np.errstate(divide="ignore", invalid="ignore"):
            if tf == "1m" and "taker_buy_ratio" in frame.columns:
                output[prefix + "taker_buy_ratio"] = frame["taker_buy_ratio"].to_numpy(np.float64)
            else:
                output[prefix + "taker_buy_ratio"] = np.divide(buy, v, out=np.full(len(frame), np.nan), where=np.isfinite(buy) & (v > 0))
            if tf == "1m" and "avg_trade_size" in frame.columns:
                output[prefix + "avg_trade_size"] = frame["avg_trade_size"].to_numpy(np.float64)
            else:
                output[prefix + "avg_trade_size"] = np.divide(v, trades, out=np.full(len(frame), np.nan), where=np.isfinite(trades) & (trades > 0))
            output[prefix + "taker_buy_quote_ratio"] = np.divide(buyq, qv, out=np.full(len(frame), np.nan), where=np.isfinite(buyq) & (qv > 0))
            output[prefix + "delta_quote"] = np.where(np.isfinite(buyq) & np.isfinite(qv), 2.0 * buyq - qv, np.nan)
        output[prefix + "dif"] = dif
        output[prefix + "dea"] = dea
        output[prefix + "hist"] = hist
        output[prefix + "ma20"] = rolling_mean(values, 20)
        output[prefix + "ma120"] = rolling_mean(values, 120)
        tf_open_ms = frame.index.to_numpy(dtype="datetime64[ms]").astype(np.int64)
        tf_close_ms = tf_open_ms + minutes * 60_000
        output[prefix + "ct"] = tf_close_ms
        output[prefix + "map"] = (np.arange(len(df), dtype=np.int64) if tf == "1m"
                                   else (np.searchsorted(tf_close_ms, one_close_time, side="right") - 1).astype(np.int64))

    output["m1_dif"] = output["1m_dif"]
    output["m1_dea"] = output["1m_dea"]
    output["m1_hist"] = output["1m_hist"]
    output["ma120"] = output["1m_ma120"]
    if source_bundle_fingerprint(resolved) != fingerprint:
        raise RuntimeError("生成指标时数据文件发生变化，已拒绝缓存；请等数据写入完成后重试")
    temporary = data_path.with_name(f".features.{uuid.uuid4().hex}.tmp.npz")
    try:
        np.savez_compressed(temporary, **output)
        os.replace(temporary, data_path)
    finally:
        temporary.unlink(missing_ok=True)

    caps = capabilities_from_frame(df.reset_index(drop=True))
    coverage = {}
    for name in ("quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "delta_base", "taker_buy_ratio",
                 "avg_trade_size", "funding_rate", "open_interest", "open_interest_value"):
        coverage[name] = float(df[name].notna().mean()) if name in df.columns else 0.0
    meta = {
        "request": requested, "sources": resolved, "bars": int(len(df)),
        "start_utc": pd.to_datetime(times[0], unit="ms", utc=True).isoformat(),
        "end_utc": pd.to_datetime(one_close_time[-1], unit="ms", utc=True).isoformat(),
        "price_min": float(output["low"].min()), "price_max": float(output["high"].max()),
        "source_columns": list(df.columns), "capabilities": caps, "coverage": coverage,
        "capabilities_by_timeframe": timeframe_capabilities_from_features(output),
        "macd": "12/26/9，MACD柱=2×(DIF-DEA)",
        "external_alignment": "funding/OI只使用<=当前1m收盘时刻的最近已知值；资金费最多回看24h，OI最多回看60m；不使用未来值",
    }
    meta_tmp = meta_path.with_name(f".features_meta.{uuid.uuid4().hex}.tmp")
    try:
        meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        os.replace(meta_tmp, meta_path)
    finally:
        meta_tmp.unlink(missing_ok=True)
    return data_path


def main():
    parser = argparse.ArgumentParser(description="生成多数据源回测特征缓存")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--micro-csv", default="")
    parser.add_argument("--funding", default="")
    parser.add_argument("--oi", default="")
    parser.add_argument("--bundle", default="")
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    path = build_features(args.csv, args.cache_dir, args.start, args.end, args.force,
                          args.micro_csv, args.funding, args.oi, args.bundle)
    print(json.dumps({"type": "features_ready", "path": str(path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
