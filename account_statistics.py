"""每个杠杆档的实际交易统计，紧凑地保存在同一CSV行中。"""
CAPACITY_TIMESTAMP = "首次达到最大开仓数量时间戳（毫秒）"
CAPACITY_DATE = "首次达到开仓上限日期（UTC+8）"
ACCOUNT_COLUMNS = (
    "交易次数（单）", "胜率（%）", "多单占比（%）", "平均日完整交易数（次/日）",
    "平均日成交订单数（笔/日）", "平均持仓时间（分钟）", "平均单笔收益率（%）",
    "毛收益合计（%）", "盈亏比（倍）", "t值", "2025毛收益（%）", "2026毛收益（%）",
    "实际止盈等待总时间（分钟）", "实际容量占用率（%）",
    CAPACITY_TIMESTAMP,
)
ACCOUNT_FIELD = "所选仓位实际交易统计"
ENGINE_VERSION = "v1.63-independent-training-batches:v1.30-free-combination"


def encode_accounts(accounts):
    return ";".join("|".join(format(float(row.get(name, -2) if name == CAPACITY_TIMESTAMP else row[name]), ".16g") for name in ACCOUNT_COLUMNS)
                    for row in accounts)


def account_row(row, index):
    raw = row.get(ACCOUNT_FIELD)
    if not raw:
        return row
    groups = raw.split(";")
    if index >= len(groups):
        raise ValueError("逐仓统计与所选仓位顺序不一致，无法导出")
    values = groups[index].split("|")
    if len(values) not in (12, 14, len(ACCOUNT_COLUMNS)):
        raise ValueError("逐仓统计字段不完整，无法导出")
    result = dict(row)
    result.update(zip(ACCOUNT_COLUMNS, values))
    result["交易次数（单）"] = str(int(float(result["交易次数（单）"])))
    return result


def capacity_timestamp(entry_index, close_times):
    """Actual accepted opening at candle close; -1 never, -2 not recorded."""
    return int(close_times[entry_index]) if entry_index >= 0 else int(entry_index)


def capacity_date(timestamp):
    """Excel date serial in UTC+8, retaining unknown versus never reached."""
    if timestamp is None or timestamp == "":
        return "未记录（旧结果）"
    value = float(timestamp)
    if value == -1:
        return "未达到"
    if value < 0:
        return "未记录（旧结果）"
    return int((value + 8 * 3600000) // 86400000) + 25569
