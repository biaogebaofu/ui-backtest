"""Parameters for the independent, offline two-direction account mode."""
from copy import deepcopy
import math

DEFAULT_CONFIG = {
    "schema": 1, "mode": "scale_in", "initial_equity": 20000.0,
    "max_eth": 20.0, "add_drops": [0.001], "tp_weekday": 0.004,
    "tp_holiday": 0.002, "timeout_hours": [12, 24, 48, 72],
    "entry_gap_minutes": 15, "maker_fee_rate": 0.0,
    "seeds": 100, "seed_start": 0, "compare_entries": False, "paths": [0, 1],
}


def normalize_config(raw=None):
    result = deepcopy(DEFAULT_CONFIG)
    result.update(raw or {})
    rule = result.get("direction_rule")
    if rule in (None, "OFF"):
        result.pop("direction_rule", None)  # Keep pre-calendar fingerprints unchanged when off.
    else:
        from direction_calendar import RULE_ID
        if rule != RULE_ID:
            raise ValueError("方向限制公式版本无法识别，不能静默按无限制回测")
    if result["mode"] not in ("single", "scale_in"):
        raise ValueError("请选择1倍不补仓或0.5倍首仓＋0.5倍补仓")
    for name, label, low, high in (
        ("initial_equity", "本金", 0, 1e12), ("max_eth", "每方向ETH上限", 0, 8000),
        ("tp_weekday", "工作日止盈", 0, 1), ("tp_holiday", "周末节假日止盈", 0, 1),
    ):
        value = float(result[name])
        if not math.isfinite(value) or not low < value <= high:
            raise ValueError(f"{label}超出有效范围")
        result[name] = value
    fee = float(result["maker_fee_rate"])
    if not math.isfinite(fee) or not 0 <= fee < 0.1:
        raise ValueError("Maker手续费率须在0%至10%之间（不含10%）")
    result["maker_fee_rate"] = fee
    for name, label, low, high in (("entry_gap_minutes", "开仓间隔", 1, 10080),
                                   ("seeds", "随机次数", 1, 10000),
                                   ("seed_start", "起始随机种子", 0, 2**32-10000)):
        value = float(result[name])
        if not math.isfinite(value) or value != int(value) or not low <= value <= high:
            raise ValueError(f"{label}须为{low}至{high}之间的整数")
        result[name] = int(value)
    for name, label, low, high in (("add_drops", "补仓逆向幅度", 0, 1),
                                   ("timeout_hours", "时间止损小时", 0, 87600)):
        values = sorted(set(float(x) for x in result[name]))
        if not values or any(not math.isfinite(x) or not low < x < high for x in values):
            raise ValueError(f"{label}必须填写有效的正数")
        result[name] = values
    paths = sorted(set(int(x) for x in result["paths"]))
    if not paths or any(x not in (0, 1) for x in paths):
        raise ValueError("请选择有效的K线内价格路径")
    result["paths"] = paths
    result["compare_entries"] = bool(result["compare_entries"])
    result["schema"] = 1
    # These weights identify the two requested experiments and cannot drift apart.
    result["first_multiple"] = 1.0 if result["mode"] == "single" else 0.5
    result["add_multiple"] = 0.0 if result["mode"] == "single" else 0.5
    return result


def default_config():
    return normalize_config()
