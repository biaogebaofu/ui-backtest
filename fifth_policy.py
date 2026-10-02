"""用户确认的第五轮永久保留清单；历史定义可以读取，执行必须过此白名单。"""
from __future__ import annotations


POLICY_VERSION = "20260912-fifth-permanent-39"
ALLOWED_FIFTH_NUMBERS = frozenset((
    15, 22, 23, 25, 26, 27, 28, 29, 31, 34, 35, 36, 37, 38, 39, 40, 41,
    51, 55, 59, 70, 71, 72, 73, 77, 79, 81, 84, 85, 86, 87, 88, 90,
    96, 97, 98, 99, 101, 102,
))
ACTIVE_FIFTH_CODES = tuple(263 + number for number in sorted(ALLOWED_FIFTH_NUMBERS))
RETIRED_FIFTH_CODES = tuple(code for code in range(264, 384) if code not in ACTIVE_FIFTH_CODES)


def fifth_retirement_reason(code):
    code = int(code)
    if code >= 264 and code not in ACTIVE_FIFTH_CODES:
        return (f"F5-{code - 263:03d} 已永久删除：不在用户确认的39种保留清单内；"
                "不能重新勾选、导入或运行，历史结果仅供查阅")
    return ""


def require_entry_allowed(code, registry=None):
    """Validate an atomic rule or every member of a stored indicator group."""
    from indicator_combinations import entry_members

    code = int(code)
    for member in entry_members(code, registry):
        if member < 0 and member != code:
            require_entry_allowed(member, registry)
        reason = fifth_retirement_reason(member)
        if reason:
            raise ValueError(reason)


def require_selection_allowed(selection, registry=None):
    """Check an execution selection without changing its parameters or identity."""
    for timeframe, codes in selection.get("开仓条件", {}).items():
        for code in codes:
            try:
                require_entry_allowed(code, registry)
            except ValueError as exc:
                raise ValueError(f"{timeframe}：{exc}") from exc
