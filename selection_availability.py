"""按每个原生周期的真实数据能力收敛编辑组合，精确指纹保持原定义。"""
from __future__ import annotations

import copy

from extended_rules import ENTRY_RULES, capabilities_for_timeframe, entry_unavailable_reason
from indicator_combinations import combination_pool


def prepare_editor_selection(selection):
    """只迁移编辑配置；精确指纹必须先经永久删除策略校验，禁止替换成员。"""
    from fifth_policy import require_entry_allowed
    from selection_config import 入场口径列表, 每个组合含第五批, 默认入场触发口径

    result = copy.deepcopy(selection)
    removed, messages = [], []
    for tf, codes in result.get("开仓条件", {}).items():
        keep = []
        for code in codes:
            try:
                require_entry_allowed(code)
            except ValueError as exc:
                if "永久删除" not in str(exc):
                    raise
                removed.append({"周期": tf, "代码": code, "原因": str(exc)})
            else:
                keep.append(code)
        result["开仓条件"][tf] = keep or [0]
    if removed:
        messages.append(f"已移除旧配置中永久删除的第五批选项，共{len(removed)}个周期选项；不再恢复这些方法")
        combos = result.get("指标组合", {})
        for tf, spec in list(combos.get("开仓", {}).items()):
            count = len(combination_pool("entry", result["开仓条件"][tf]))
            if spec.get("启用") and any(n > count for n in spec.get("组合数量", [])):
                del combos["开仓"][tf]
                messages.append(f"{tf}剔除后成员不足，已关闭该周期的指标组合，请重新选择组合数量")
        if "开仓" in combos and not combos["开仓"]:
            del combos["开仓"]
        if not combos:
            result.pop("指标组合", None)
    modes = 入场口径列表(result)
    if "F5_EVENT" in modes and not 每个组合含第五批(result):
        modes = [mode for mode in modes if mode != "F5_EVENT"] or [默认入场触发口径]
        result["入场触发口径"] = modes[0] if len(modes) == 1 else modes
        messages.append("原选择包含不含第五批的组合，已退出第五批专用事件模式；普通组合按所选入场规则运行，第五批组合仍自动使用自身事件")
    return {"selection": result, "removed": removed, "message": "；".join(messages)}


def prune_unavailable_selection(selection, capabilities, timeframe_capabilities=None, *, exact=False):
    result = copy.deepcopy(selection)
    removed = []
    disabled = []
    for tf, codes in selection["开仓条件"].items():
        caps = capabilities_for_timeframe(capabilities, timeframe_capabilities or {}, tf)
        keep = []
        for code in codes:
            reason = entry_unavailable_reason(code, caps, tf)
            if reason:
                removed.append({"周期": tf, "代码": code,
                                "名称": ENTRY_RULES.get(code, (str(code),))[0], "原因": reason})
            else:
                keep.append(code)
        if not exact:
            # 单周期全空只关闭该周期；不补入任何其他开仓规则。
            result["开仓条件"][tf] = keep or [0]
    if not exact:
        combos = result.get("指标组合", {})
        for tf, spec in list(combos.get("开仓", {}).items()):
            available = len(combination_pool("entry", result["开仓条件"][tf]))
            if spec.get("启用") and any(size > available for size in spec.get("组合数量", [])):
                del combos["开仓"][tf]
                disabled.append({"周期": tf, "原因": f"剔除后仅余{available}项，原组合数量不再有效，已关闭指标组合"})
        if "开仓" in combos and not combos["开仓"]:
            del combos["开仓"]
        if not combos:
            result.pop("指标组合", None)
    has_rule = any(code != 0 for codes in result["开仓条件"].values() for code in codes)
    runnable = has_rule and not (exact and removed)
    pieces = []
    if removed:
        verb = "精确指纹将跳过，保留原定义" if exact else "已自动取消缺数据或当前未支持的开仓选项"
        examples = "；".join(f"{row['周期']} {row['代码']}（{row['原因']}）" for row in removed[:12])
        pieces.append(f"{verb}，共{len(removed)}项：{examples}" + ("……" if len(removed) > 12 else ""))
    pieces.extend(f"{row['周期']}：{row['原因']}" for row in disabled)
    if not has_rule:
        pieces.append("没有可运行的开仓规则；各周期已关闭，没有替换成其他策略")
    return {"selection": result, "removed": removed, "disabled_combinations": disabled,
            "runnable": runnable, "exact": exact, "message": "；".join(pieces)}
