from __future__ import annotations

import hashlib
import json
import re
import zipfile
import shutil
import uuid
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

BASE_REQUIRED = ("openTime", "open", "high", "low", "close", "volume")
COLUMN_ALIASES = {
    "open_time": "openTime", "open_time_ms": "openTime",
    "quoteVolume": "quote_volume", "quoteAssetVolume": "quote_volume",
    "takerBuyBaseVol": "taker_buy_base", "takerBuyBaseVolume": "taker_buy_base",
    "takerBuyQuoteVol": "taker_buy_quote", "takerBuyQuoteVolume": "taker_buy_quote",
    "numberOfTrades": "trades", "trade_count": "trades",
    "fundingRate": "funding_rate", "openInterest": "open_interest",
    "openInterestValue": "open_interest_value",
    "fundingTime": "funding_time", "sumOpenInterest": "open_interest",
    "sumOpenInterestValue": "open_interest_value",
    "isBuyerMaker": "is_buyer_maker", "transact_time": "timestamp",
    "firstTradeId": "first_trade_id", "lastTradeId": "last_trade_id",
    "aggTradeId": "agg_trade_id", "agg_trade_ID": "agg_trade_id",
    "first_trade_ID": "first_trade_id", "last_trade_ID": "last_trade_id",

}
CAPABILITY_COLUMNS = {
    "ohlcv": set(BASE_REQUIRED),
    "quote_volume": {"quote_volume"},
    "trades": {"trades"},
    "taker_base": {"taker_buy_base"},
    "taker_quote": {"taker_buy_quote", "quote_volume"},
    "delta_cvd": {"delta_base"},
    "taker_ratio": {"taker_buy_ratio"},
    "avg_trade_size": {"avg_trade_size"},
    "funding": {"funding_rate"},
    "open_interest": {"open_interest"},
    "open_interest_value": {"open_interest_value"},
}
CAPABILITY_LABELS = {
    "ohlcv": "K线 OHLCV", "quote_volume": "成交额 Quote Volume", "trades": "成交笔数",
    "taker_base": "主动买/卖基础币量", "taker_quote": "主动买成交额", "delta_cvd": "Delta / CVD",
    "taker_ratio": "主动买占比", "avg_trade_size": "平均单笔规模", "funding": "已结算资金费率",
    "open_interest": "持仓量 OI", "open_interest_value": "持仓价值 OI Value",
}
TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
ADDITIVE_FIELDS = (
    "quote_volume", "agg_trades", "trades", "taker_buy_base", "taker_sell_base",
    "taker_buy_quote", "delta_base", "delta_quote",
)
STATE_FIELDS = ("funding_rate", "funding_age_min", "open_interest", "open_interest_value", "oi_age_min")


def resample_closed_frame(df, minutes):
    """Aggregate complete UTC buckets; ratios are derived from totals downstream."""
    if minutes == 1:
        return df
    rule = f"{minutes}min"
    agg_map = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "openTime": "first"}
    agg_map.update({name: "sum" for name in ADDITIVE_FIELDS if name in df})
    agg_map.update({name: "last" for name in STATE_FIELDS if name in df})
    frame = df.resample(rule, origin="epoch", label="left", closed="left").agg(agg_map)
    counts = df["close"].resample(rule, origin="epoch", label="left", closed="left").count()
    frame = frame.loc[counts == minutes].dropna(subset=["open", "high", "low", "close", "volume"])
    optional = [c for c in ADDITIVE_FIELDS if c in df]
    if optional:
        present = df[optional].resample(rule, origin="epoch", label="left", closed="left").count()
        frame[optional] = frame[optional].where(present.reindex(frame.index) == minutes)
    # 'last' skips NaN; use the actual last minute to retain state expiry.
    last_rows = frame.index + pd.Timedelta(minutes=minutes - 1)
    for name in STATE_FIELDS:
        if name in df:
            frame[name] = df[name].reindex(last_rows).to_numpy()
    return frame


def _symbol_from_name(path: str | Path | None) -> str:
    if not path:
        return ""
    m = re.search(r"(?:^|[^A-Z])(ETH(?:USDC|USDT))(?:[^A-Z]|$)", Path(path).name.upper())
    return m.group(1) if m else ""


def _check_symbol(primary, *others):
    base = _symbol_from_name(primary)
    for other in others:
        sym = _symbol_from_name(other)
        if base and sym and base != sym:
            raise ValueError(f"数据品种不一致：主K线是 {base}，补充数据是 {sym}。禁止跨品种静默合并。")


def _read_table(path: str | Path, nrows: int | None = None) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, nrows=nrows)
    if suffix in (".parquet", ".pq"):
        try:
            df = pd.read_parquet(path)
        except ImportError as exc:
            raise RuntimeError("读取Parquet需要 pyarrow；请运行 pip install -r requirements.txt") from exc
        return df.head(nrows) if nrows else df
    raise ValueError(f"不支持的数据格式：{path.name}；请使用 CSV / Parquet，或在UI里选择数据包ZIP。")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize aliases without creating duplicate column names.

    Co-existing aliases are allowed only when their non-missing values agree.
    The short REST/stream aggTrade names are recognized only as a complete
    aggTrade schema; a generic K-line column named `T` is not reinterpreted.
    """
    work = df.copy()
    work.columns = [str(c).strip().lstrip("\ufeff") for c in work.columns]
    if work.columns.duplicated().any():
        raise ValueError("文件包含重复列名，请先去除歧义")
    aliases = dict(COLUMN_ALIASES)
    if {"p", "q", "T", "m"}.issubset(work.columns):
        aliases.update({"a": "agg_trade_id", "p": "price", "q": "quantity",
                        "T": "timestamp", "m": "is_buyer_maker",
                        "f": "first_trade_id", "l": "last_trade_id", "s": "symbol"})
    for old, new in aliases.items():
        if old not in work.columns or old == new:
            continue
        if new not in work.columns:
            work = work.rename(columns={old: new})
            continue
        left, right = work[new], work[old]
        overlap = left.notna() & right.notna()
        equal = left.astype(str).str.strip().eq(right.astype(str).str.strip())
        ln, rn = pd.to_numeric(left, errors="coerce"), pd.to_numeric(right, errors="coerce")
        equal |= ln.notna() & rn.notna() & ln.eq(rn)
        if (overlap & ~equal).any():
            raise ValueError(f"别名字段内容冲突：{old} 与 {new}")
        work[new] = left.combine_first(right)
        work = work.drop(columns=[old])
    return work


def _normalize_timestamp(series: pd.Series) -> pd.Series:
    """UTC milliseconds, accepting modern epoch s/ms/us/ns and ISO timestamps.

    Values below 1e9 are treated as milliseconds (also supports synthetic epoch
    fixtures). Invalid supplied values are errors, never silently dropped rows.
    Sub-millisecond events are floored so a future event cannot round backwards
    into the next decision boundary. Unit inference must be homogeneous.
    """
    raw = pd.to_numeric(series, errors="coerce")
    present = series.notna()
    if not present.any():
        return pd.Series(pd.NA, index=series.index, dtype="Int64")
    if raw[present].notna().all():
        values = raw[present]
        arr = values.to_numpy(dtype=float)
        if not np.isfinite(arr).all() or (arr < 0).any():
            raise ValueError("时间戳包含负数、NaN或无穷大")
        def unit(v):
            if v >= 1e17: return "ns"
            if v >= 1e14: return "us"
            if 1e9 <= v < 1e11: return "s"
            return "ms"
        units = {unit(float(v)) for v in values}
        if len(units) != 1:
            raise ValueError("同一时间列混用了秒/毫秒/微秒/纳秒单位")
        scale = {"ns": 1_000_000, "us": 1000, "ms": 1, "s": .001}[units.pop()]
        # Integer division avoids float loss for modern nanosecond timestamps.
        converted = values // int(scale) if scale >= 1 else values * 1000
        result = pd.Series(pd.NA, index=series.index, dtype="Int64")
        result.loc[present] = np.floor(converted).astype("int64")
        return result
    if raw[present].notna().any():
        raise ValueError("时间列混用了数值时间戳与日期文本，或含无效时间戳")
    parsed = pd.to_datetime(series, utc=True, errors="coerce", format="mixed")
    if parsed[present].isna().any():
        raise ValueError("时间戳无法解析；请使用UTC秒/毫秒/微秒/纳秒或ISO日期")
    result = pd.Series(pd.NA, index=series.index, dtype="Int64")
    valid = parsed.notna()
    result.loc[valid] = (parsed[valid].dt.as_unit("ns").astype("int64") // 1_000_000).astype("int64")
    return result


def _check_frame_symbol(df: pd.DataFrame, path: str | Path, expected: str = "") -> str:
    """Use the content symbol as well as the filename; reject mixed contracts."""
    from_name = _symbol_from_name(path)
    supplied = set()
    for name in ("symbol", "Symbol", "instrument"):
        if name in df:
            supplied.update(str(x).strip().upper().replace("/", "").replace("-", "")
                            for x in df[name].dropna() if str(x).strip())
    if len(supplied) > 1:
        raise ValueError(f"文件包含多个品种，不能合并：{Path(path).name}: {sorted(supplied)}")
    actual = next(iter(supplied), from_name)
    if supplied and from_name and actual != from_name:
        raise ValueError(f"文件名与symbol字段品种不一致：{from_name} / {actual}")
    if expected and actual and actual != expected:
        raise ValueError(f"数据品种不一致：主K线是 {expected}，补充数据是 {actual}")
    return actual or expected


def _numeric_columns(df: pd.DataFrame, names, *, allow_missing: bool = True) -> pd.DataFrame:
    for c in names:
        if c not in df:
            continue
        old = df[c]
        value = pd.to_numeric(old, errors="coerce")
        if (old.notna() & value.isna()).any():
            raise ValueError(f"字段 {c} 包含非数值内容")
        if np.isinf(value.to_numpy(dtype=float)).any():
            raise ValueError(f"字段 {c} 包含无穷大")
        if not allow_missing and value.isna().any():
            raise ValueError(f"字段 {c} 包含NaN或空值")
        df[c] = value
    return df


def _unique_time_rows(df: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    if df[key].isna().any():
        raise ValueError(f"{label}存在空时间戳")
    result = df.drop_duplicates()
    if result[key].duplicated().any():
        raise ValueError(f"{label}同一时间戳存在内容冲突的重复数据")
    return result.sort_values(key).reset_index(drop=True)


def validate_base_frame(df: pd.DataFrame, minimum_rows: int = 500) -> pd.DataFrame:
    """Shared by the inspector and the worker so an invalid file is not green."""
    missing = set(BASE_REQUIRED) - set(df.columns)
    if missing:
        raise ValueError(f"主K线缺少必要字段：{sorted(missing)}")
    df = _numeric_columns(df.copy(), BASE_REQUIRED, allow_missing=False)
    t = df.openTime.to_numpy(dtype=np.float64)
    if not np.isfinite(t).all() or (t % 60000 != 0).any():
        raise ValueError("openTime必须是UTC整分钟的毫秒时间戳")
    df["openTime"] = df["openTime"].astype("int64")
    values = df[["open", "high", "low", "close", "volume"]].to_numpy(float)
    if (values[:, :4] <= 0).any() or (values[:, 4] < 0).any():
        raise ValueError("开高低收价格必须大于0，成交量不能为负数")
    if ((df.high < df[["open", "low", "close"]].max(axis=1)) |
        (df.low > df[["open", "high", "close"]].min(axis=1))).any():
        raise ValueError("OHLC价格关系无效")
    df = _unique_time_rows(df, "openTime", "K线")
    if len(df) < minimum_rows:
        raise ValueError(f"有效K线不足{minimum_rows}根，无法进行多周期回测")
    if len(df) > 1 and (np.diff(df.openTime.to_numpy(np.int64)) != 60000).any():
        raise ValueError("1分钟数据存在缺口或重复时间戳")
    return df


def resolve_bundle(bundle_zip: str | Path | None, cache_root: str | Path, skip_kinds=()) -> dict[str, str]:
    if not bundle_zip:
        return {}
    zpath = Path(bundle_zip)
    if not zpath.is_file():
        raise FileNotFoundError(zpath)
    stat = zpath.stat()
    key = source_bundle_fingerprint({"bundle": str(zpath)})[:16]
    out = Path(cache_root) / f"bundle_{key}"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        members = [n for n in zf.namelist() if not n.endswith("/")]
        chosen = {}
        patterns = {
            "kline": ("klines_1m", "kline_1m", "1m_kline"),
            "micro": ("agg_trades", "aggtrades"),
            "funding": ("funding_rates", "funding_rate"),
            "oi": ("open_interest", "openinterest"),
        }
        for kind, pats in patterns.items():
            if kind in skip_kinds:
                continue
            matches = [name for name in members if
                       any(p in Path(name).name.lower() for p in pats) and
                       Path(name).suffix.lower() in (".csv", ".parquet", ".pq")]
            if len(matches) > 1:
                raise ValueError(f"ZIP中有多个{kind}文件，不能静默只读第一个：{matches}。请先合并同类分片或显式选择单文件。")
            for name in matches:
                low = Path(name).name.lower()
                if any(p in low for p in pats) and Path(low).suffix in (".csv", ".parquet", ".pq"):
                    target = out / f"{kind}_{Path(name).name}"
                    info = zf.getinfo(name)
                    if not target.exists() or target.stat().st_size != info.file_size:
                        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
                        try:
                            with zf.open(name) as src, temporary.open("wb") as dst:
                                shutil.copyfileobj(src, dst, length=1024 * 1024)
                            temporary.replace(target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    chosen[kind] = str(target)
                    break
    return chosen


def resolve_sources(primary: str | None = None, micro: str | None = None,
                    funding: str | None = None, oi: str | None = None,
                    bundle: str | None = None, cache_root: str | Path = ".") -> dict[str, str]:
    bundled = resolve_bundle(bundle, cache_root, {k for k, v in (("kline", primary), ("micro", micro), ("funding", funding), ("oi", oi)) if v}) if bundle else {}
    result = {
        "kline": str(primary or bundled.get("kline") or ""),
        "micro": str(micro or bundled.get("micro") or ""),
        "funding": str(funding or bundled.get("funding") or ""),
        "oi": str(oi or bundled.get("oi") or ""),
        "bundle": str(bundle or ""),
    }
    if not result["kline"]:
        raise ValueError("缺少1分钟K线主数据。可直接选择K线CSV/Parquet，或选择含klines_1m的数据包ZIP。")
    _check_symbol(result["kline"], result["micro"], result["funding"], result["oi"])
    return result


def _aggregate_raw_aggtrades(df: pd.DataFrame) -> pd.DataFrame:
    need = {"timestamp", "price", "quantity", "is_buyer_maker"}
    if not need.issubset(df.columns):
        raise ValueError(f"aggTrades缺少字段：{sorted(need - set(df.columns))}")
    work = df.copy()
    if "agg_trade_id" in work:
        work = work.drop_duplicates()
        if work["agg_trade_id"].duplicated().any():
            raise ValueError("aggTrades相同ID存在内容冲突，禁止重复累计成交量")
    work["timestamp"] = _normalize_timestamp(work["timestamp"])
    if work["timestamp"].isna().any():
        raise ValueError("aggTrades存在空时间戳")
    work = work.sort_values("timestamp")
    work["openTime"] = (work["timestamp"].astype("int64") // 60000) * 60000
    work = _numeric_columns(work, ("price", "quantity"), allow_missing=False)
    if (work.price <= 0).any() or (work.quantity < 0).any():
        raise ValueError("aggTrades价格须为正，数量须非负")
    flags = work["is_buyer_maker"].astype(str).str.strip().str.lower()
    flags = flags.map({"true": True, "false": False, "1": True, "0": False,
                       "1.0": True, "0.0": False})
    if flags.isna().any():
        raise ValueError("is_buyer_maker必须是true/false或1/0，不能用文字真值强转")
    maker = flags.astype(bool)
    work["quote"] = work["price"] * work["quantity"]
    work["taker_buy_base"] = np.where(~maker, work["quantity"], 0.0)
    work["taker_buy_quote"] = np.where(~maker, work["quote"], 0.0)
    if {"first_trade_id", "last_trade_id"}.issubset(work.columns):
        work = _numeric_columns(work, ("first_trade_id", "last_trade_id"), allow_missing=False)
        work["trade_count"] = work.last_trade_id - work.first_trade_id + 1
        if (work.trade_count < 1).any() or (work.trade_count % 1 != 0).any():
            raise ValueError("aggTrades的first/last_trade_id范围无效")
    else:
        # Without raw trade ID ranges, aggregate count is not actual trade count.
        work["trade_count"] = np.nan
    g = work.groupby("openTime", sort=True)
    out = g.agg(
        quote_volume=("quote", "sum"), agg_trades=("price", "size"),
        taker_buy_base=("taker_buy_base", "sum"), taker_buy_quote=("taker_buy_quote", "sum"),
    )
    if work["trade_count"].notna().all():
        out["trades"] = g["trade_count"].sum()
    return out.reset_index()


def _normalize_micro(path: str | Path, expected_symbol: str = "") -> pd.DataFrame:
    df = _normalize_columns(_read_table(path))
    _check_frame_symbol(df, path, expected_symbol)
    if {"timestamp", "price", "quantity", "is_buyer_maker"}.issubset(df.columns):
        return _aggregate_raw_aggtrades(df)
    if "openTime" not in df.columns:
        raise ValueError("微结构文件既不是原始aggTrades，也没有openTime/open_time分钟键。")
    df["openTime"] = _normalize_timestamp(df["openTime"])
    keep = [c for c in ("openTime", "quote_volume", "agg_trades", "trades", "taker_buy_base",
                        "taker_sell_base", "taker_buy_quote", "delta_base", "taker_buy_ratio",
                        "avg_trade_size") if c in df.columns]
    df = _numeric_columns(df[keep].copy(), keep[1:])
    if (df["openTime"].dropna() % 60000 != 0).any():
        raise ValueError("分钟微结构openTime必须是UTC整分钟")
    return _unique_time_rows(df, "openTime", "分钟微结构")


def _merge_sparse(base: pd.DataFrame, path: str | Path, kind: str, expected_symbol: str = "") -> pd.DataFrame:
    ext = _normalize_columns(_read_table(path))
    _check_frame_symbol(ext, path, expected_symbol)
    if kind == "funding":
        value_cols = [c for c in ("funding_rate",) if c in ext.columns]
        time_col = "funding_time" if "funding_time" in ext.columns else "timestamp"
        tolerance_ms = 24 * 60 * 60 * 1000
        age_name = "funding_age_min"
    else:
        value_cols = [c for c in ("open_interest", "open_interest_value") if c in ext.columns]
        time_col = "timestamp"
        tolerance_ms = 60 * 60 * 1000
        age_name = "oi_age_min"
    if not value_cols:
        if ext.empty:
            return base
        raise ValueError(f"{kind}数据缺少必要数值字段")
    if time_col not in ext.columns:
        raise ValueError(f"{kind}数据缺少时间字段 {time_col}")
    ext = ext[[time_col] + value_cols].copy()
    ext[time_col] = _normalize_timestamp(ext[time_col])
    ext = _numeric_columns(ext, value_cols)
    ext = _unique_time_rows(ext, time_col, kind)
    if kind != "funding" and any((ext[c].dropna() < 0).any() for c in value_cols):
        raise ValueError("Open Interest不能为负数")
    if ext.empty:
        return base
    ext["_src_ms"] = ext[time_col].astype("int64")
    left = base.drop(columns=[c for c in (*value_cols, age_name) if c in base.columns]).sort_values("_close_ms")
    merged = pd.merge_asof(left, ext.drop(columns=[time_col]), left_on="_close_ms", right_on="_src_ms",
                           direction="backward", tolerance=tolerance_ms)
    merged[age_name] = (merged["_close_ms"] - merged["_src_ms"]) / 60000.0
    return merged.drop(columns=["_src_ms"])


def load_merged_source(primary: str | Path, micro: str | Path | None = None,
                       funding: str | Path | None = None, oi: str | Path | None = None,
                       start: str | None = None, end: str | None = None) -> pd.DataFrame:
    _check_symbol(primary, micro, funding, oi)
    df = _normalize_columns(_read_table(primary))
    expected_symbol = _check_frame_symbol(df, primary)
    missing = [x for x in BASE_REQUIRED if x not in df.columns]
    if missing:
        raise ValueError(f"主K线缺少必要字段：{missing}")
    df = df.copy()
    df["openTime"] = _normalize_timestamp(df["openTime"])
    if df["openTime"].isna().any():
        raise ValueError("主K线存在空时间戳，不允许静默丢行")
    df = _numeric_columns(df, ("open", "high", "low", "close", "volume"), allow_missing=False)
    # 完全重复行可去重；同一分钟内容不同必须报错，不能“最后一行覆盖前一行”。
    unique_rows = df.drop_duplicates(list(df.columns))
    if unique_rows["openTime"].duplicated().any():
        raise ValueError("同一openTime存在内容冲突的重复K线，请先核对数据来源")
    df = unique_rows.sort_values("openTime")
    df["openTime"] = df["openTime"].astype("int64")
    if micro:
        ext = _normalize_micro(micro, expected_symbol)
        overlap = [c for c in ext.columns if c != "openTime" and c in df.columns]
        df = df.merge(ext, how="left", on="openTime", suffixes=("", "__external"), validate="one_to_one")
        for name in overlap:
            df[name] = df.pop(name + "__external").combine_first(df[name])
        # Explicit minute data can replace a primitive; recompute derived fields
        # below rather than retaining a conflicting precomputed delta/ratio.
        if "taker_buy_base" in ext:
            df = df.drop(columns=[c for c in ("taker_sell_base", "delta_base", "taker_buy_ratio") if c in df])
        if "trades" in ext:
            df = df.drop(columns=["avg_trade_size"], errors="ignore")
    optional_numeric = set().union(*CAPABILITY_COLUMNS.values()) - set(BASE_REQUIRED)
    optional_numeric |= {"agg_trades", "taker_sell_base", "delta_quote"}
    df = _numeric_columns(df, optional_numeric)
    for name in ("trades", "agg_trades", "quote_volume", "taker_buy_base", "taker_buy_quote",
                 "open_interest", "open_interest_value", "avg_trade_size"):
        if name in df and (df[name].dropna() < 0).any():
            raise ValueError(f"字段{name}不能为负数")
    for name, total in (("taker_buy_base", "volume"), ("taker_buy_quote", "quote_volume")):
        if name in df and total in df:
            tolerance = df[total].abs() * 1e-8 + 1e-10
            if (df[name] > df[total] + tolerance).any():
                raise ValueError(f"{name}大于{total}，微结构与K线来源或单位可能不一致")
    # Derive microstructure columns whenever possible.
    if "taker_buy_base" in df.columns:
        df["taker_sell_base"] = df.get("taker_sell_base", df["volume"] - df["taker_buy_base"])
        df["delta_base"] = df.get("delta_base", 2.0 * df["taker_buy_base"] - df["volume"])
        with np.errstate(divide="ignore", invalid="ignore"):
            df["taker_buy_ratio"] = df.get("taker_buy_ratio", df["taker_buy_base"] / df["volume"])
    if "trades" in df.columns:
        with np.errstate(divide="ignore", invalid="ignore"):
            df["avg_trade_size"] = df.get("avg_trade_size", df["volume"] / df["trades"])
    if "quote_volume" in df.columns and "taker_buy_quote" in df.columns:
        with np.errstate(divide="ignore", invalid="ignore"):
            df["taker_buy_quote_ratio"] = df["taker_buy_quote"] / df["quote_volume"]
            df["delta_quote"] = 2.0 * df["taker_buy_quote"] - df["quote_volume"]
    df["_close_ms"] = df["openTime"] + 60000
    if funding:
        df = _merge_sparse(df, funding, "funding", expected_symbol)
    if oi:
        df = _merge_sparse(df, oi, "oi", expected_symbol)
    if start:
        start_ms = int(pd.to_datetime(start, utc=True).timestamp() * 1000)
        df = df[df.openTime >= start_ms]
    if end:
        end_ms = int(pd.to_datetime(end, utc=True).timestamp() * 1000)
        df = df[df.openTime < end_ms]
    df = df.drop(columns=["_close_ms"], errors="ignore")
    for name in ("taker_buy_ratio", "taker_buy_quote_ratio", "avg_trade_size"):
        if name in df:
            df[name] = df[name].replace([np.inf, -np.inf], np.nan)
    return df


def capabilities_from_frame(df: pd.DataFrame) -> dict[str, bool]:
    cols = set(df.columns)
    result = {}
    for cap, need in CAPABILITY_COLUMNS.items():
        result[cap] = bool(need.issubset(cols) and
                           np.logical_and.reduce([np.isfinite(pd.to_numeric(df[c], errors="coerce")) for c in need]).any())
    return result


def timeframe_capabilities_from_frame(df):
    indexed = df.set_index(pd.to_datetime(df.openTime, unit="ms", utc=True))
    result = {}
    for tf, minutes in TIMEFRAME_MINUTES.items():
        frame = resample_closed_frame(indexed, minutes)
        caps = capabilities_from_frame(frame)
        if minutes != 1:
            # A one-minute ratio or average alone cannot reconstruct totals.
            caps["taker_ratio"] = bool("taker_buy_base" in frame and
                (np.isfinite(frame.taker_buy_base) & (frame.volume > 0)).any())
            caps["avg_trade_size"] = bool("trades" in frame and
                (np.isfinite(frame.trades) & (frame.trades > 0)).any())
        result[tf] = caps
    return result


def timeframe_capabilities_from_features(data):
    result = {}
    for tf in TIMEFRAME_MINUTES:
        caps = {}
        for cap, columns in CAPABILITY_COLUMNS.items():
            keys = [f"{tf}_{'ct' if c == 'openTime' else c}" for c in columns]
            caps[cap] = bool(all(k in data for k in keys) and
                             np.logical_and.reduce([np.isfinite(data[k]) for k in keys]).any())
        result[tf] = caps
    return result


def inspect_sources(primary: str | None = None, micro: str | None = None, funding: str | None = None,
                    oi: str | None = None, bundle: str | None = None, cache_root: str | Path = ".") -> dict:
    src = resolve_sources(primary, micro, funding, oi, bundle, cache_root)
    df = load_merged_source(src["kline"], src["micro"] or None, src["funding"] or None, src["oi"] or None)
    df = validate_base_frame(df)
    caps = capabilities_from_frame(df)
    times = df["openTime"].to_numpy(np.int64)
    gaps = int(np.sum(np.diff(times) != 60000)) if len(times) > 1 else 0
    coverage = {}
    for key, cols in CAPABILITY_COLUMNS.items():
        present = [c for c in cols if c in df.columns]
        if len(present) != len(cols) or len(df) == 0:
            coverage[key] = 0.0
        else:
            coverage[key] = float(np.logical_and.reduce([
                np.isfinite(pd.to_numeric(df[c], errors="coerce")) for c in present]).mean())
    return {
        "sources": src, "rows": int(len(df)), "start_ms": int(times[0]) if len(times) else None,
        "end_ms": int(times[-1] + 60000) if len(times) else None, "gaps": gaps,
        "columns": list(df.columns), "capabilities": caps, "coverage": coverage,
        "capabilities_by_timeframe": timeframe_capabilities_from_frame(df),
        "symbol": _check_frame_symbol(df, src["kline"]),
    }


def source_bundle_fingerprint(sources: dict[str, str]) -> str:
    parts = []
    for key in ("kline", "micro", "funding", "oi", "bundle"):
        raw = sources.get(key) or ""
        if not raw:
            parts.append((key, "")); continue
        p = Path(raw)
        if p.exists():
            st = p.stat()
            digest = hashlib.sha256()
            with p.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            parts.append((key, str(p.resolve()), st.st_size, st.st_mtime_ns, digest.hexdigest()))
        else:
            parts.append((key, raw))
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
