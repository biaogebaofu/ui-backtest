"""读取结果自身的回测区间；缺少区间时不能把数百天默认为一天。"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path


def read_period_info(output_dir: Path):
    for path in (output_dir / "回测数据说明.json", output_dir / "缓存" / "features_meta.json"):
        if not path.is_file():
            continue
        meta = json.loads(path.read_text("utf-8-sig"))
        start = datetime.fromisoformat(meta["start_utc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(meta["end_utc"].replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        days = (end - start).total_seconds() / 86400.0
        if not math.isfinite(days) or days <= 0:
            raise ValueError(f"回测区间无效：{path}")
        # end为右开边界。2026-01-01 00:00结束的数据没有2026年的交易。
        last = end - timedelta(microseconds=1)
        return days, set(range(start.year, last.year + 1))
    return None, set()


def completed_trades_per_day(row, category, days=None):
    trades = float(row.get("交易次数（单）", row.get("交易次数", 0)))
    if days is not None:
        return trades / days
    for name in ("平均日完整交易数（次/日）", "平均日完整交易数（单/日）"):
        value = row.get(name)
        if value is not None and str(value).strip():
            daily = float(value)
            if not math.isfinite(daily) or daily < 0:
                raise ValueError(f"{name}不是有效非负数")
            return daily
    if trades == 0:
        return 0.0
    if category != "分批止盈":
        for name in ("平均日成交订单数（笔/日）", "平均日成交单数（单/日）", "平均日成交单数"):
            value = row.get(name)
            if value is not None and str(value).strip():
                daily = float(value) / 2.0
                if math.isfinite(daily) and daily > 0:
                    return daily
    raise ValueError("无法确定每日完整交易数：请保留该回测的“回测数据说明.json”或“缓存/features_meta.json”。"
                     "分批策略有的交易在分批前就止损，不能把订单总笔数一律除以3。")
