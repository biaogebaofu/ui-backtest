"""成交成本分别回测、不叠加；比例存小数，界面转换成百分数。"""
from __future__ import annotations

import math


COST_MODE_LABELS = {"SLIPPAGE": "成交偏移", "FEE": "手续费"}
DEFAULT_COST_MODE = "SLIPPAGE"
LEGACY_DEFAULT_SLIPPAGE = {"开仓": 0.00005, "平仓": 0.00005}
# Illustrative assumptions only; calibrate to your own execution data.
DEFAULT_SLIPPAGE = {"开仓": 0.0001, "平仓": 0.0001}
DEFAULT_SLIPPAGE_PRESET = "示例偏移 · 开平各0.01%"
FEE_PRESETS = {
    "USDC普通用户 · 双边吃单": (0.0004, 0.0004),
    "USDT普通用户 · 双边吃单": (0.0005, 0.0005),
    "零费Maker（账户适用时）": (0.0, 0.0),
}
DEFAULT_FEE_PRESET = "USDC普通用户 · 双边吃单"
DEFAULT_FEES = {"开仓费率": 0.0004, "平仓费率": 0.0004,
                "BNB抵扣": False, "返佣比例": 0.0}
DEFAULT_MIN_REENTRY_MINUTES = 5


def cost_modes(config_or_value) -> list[str]:
    """Return canonical independent modes; retain the old missing-mode inference."""
    if isinstance(config_or_value, dict):
        value = normalize_execution_settings(config_or_value)["成本模式"]
    else:
        value = config_or_value
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        raise ValueError("成本模式至少选择一项：SLIPPAGE（偏移）或FEE（手续费）")
    if any(not isinstance(mode, str) or mode not in COST_MODE_LABELS for mode in values):
        raise ValueError("成本模式必须是SLIPPAGE（偏移）或FEE（手续费）的代码或非空代码列表")
    return [mode for mode in COST_MODE_LABELS if mode in values]


def normalize_cost_mode(value):
    if not isinstance(value, (str, list)):
        raise ValueError("成本模式必须是模式代码或非空代码列表")
    modes = cost_modes(value)
    # Historical scalar configurations and their signatures remain unchanged.
    return modes[0] if len(modes) == 1 else modes


def _single_cost_mode(config) -> str:
    modes = cost_modes(config)
    if len(modes) != 1:
        raise ValueError("多成本模式必须分别回测；请先选择当前单一成本分支，不能叠加或默认取第一项")
    return modes[0]


def normalize_fees(raw: dict | None) -> dict:
    """旧配置没有手续费时保持零费率。"""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("手续费必须是配置对象")
    source = raw or {}
    result = {}
    for key in ("开仓费率", "平仓费率", "返佣比例"):
        try:
            value = float(source.get(key, 0.0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{key}必须是0%到100%之间的有限数字") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{key}必须是0%到100%之间的有限数字")
        result[key] = value
    bnb = source.get("BNB抵扣", False)
    if not isinstance(bnb, bool):
        raise ValueError("BNB抵扣必须为true或false")
    result["BNB抵扣"] = bnb
    return result


def normalize_execution_settings(selection: dict | None) -> dict:
    source = selection or {}
    raw_minutes = source.get("平仓后最小开仓间隔分钟", 0)
    try:
        value = float(raw_minutes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("平仓后最小开仓间隔必须是大于等于0的整数分钟") from exc
    if (isinstance(raw_minutes, bool) or not math.isfinite(value)
            or value < 0 or not value.is_integer()):
        raise ValueError("平仓后最小开仓间隔必须是大于等于0的整数分钟")
    fees = normalize_fees(source.get("手续费"))
    mode = source.get("成本模式")
    if mode is None:
        has_fees = any(fees[key] > 0 for key in ("开仓费率", "平仓费率"))
        slip = source.get("成交偏移") or {}
        has_slippage = any(float(slip.get(key, 0.0)) != 0 for key in ("开仓", "平仓"))
        if has_fees and has_slippage:
            raise ValueError("旧配置同时包含成交偏移和手续费，请明确选择成本模式：SLIPPAGE（偏移）或FEE（手续费）")
        mode = "FEE" if has_fees else DEFAULT_COST_MODE
    mode = normalize_cost_mode(mode)
    return {"成本模式": mode, "平仓后最小开仓间隔分钟": int(value), "手续费": fees}


def effective_fee_rates(selection: dict | None) -> tuple[float, float]:
    config = normalize_execution_settings(selection)
    if _single_cost_mode(config) != "FEE":
        return 0.0, 0.0
    fees = config["手续费"]
    factor = (0.9 if fees["BNB抵扣"] else 1.0) * (1.0 - fees["返佣比例"])
    return fees["开仓费率"] * factor, fees["平仓费率"] * factor


def effective_slippage(selection: dict | None) -> tuple[float, float]:
    if _single_cost_mode(normalize_execution_settings(selection)) != "SLIPPAGE":
        return 0.0, 0.0
    slip = (selection or {}).get("成交偏移") or DEFAULT_SLIPPAGE
    values = tuple(float(slip.get(key, DEFAULT_SLIPPAGE[key])) for key in ("开仓", "平仓"))
    if any(not math.isfinite(value) or not 0.0 <= value <= 0.1 for value in values):
        raise ValueError("成交偏移必须在0%到10%之间")
    return values
