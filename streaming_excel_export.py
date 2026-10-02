from __future__ import annotations

import json
import math
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from rank_color_scales import add_openpyxl_scales
from strategy_display import candidate_display_payload


NAVY = "1F4E78"
NOTE = "FFF2CC"


def _safe_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\\/?*\[\]:]", "_", str(name))[:31] or "未分类"
    value = base
    number = 2
    while value in used:
        suffix = f"_{number}"
        value = (base[:31 - len(suffix)] + suffix)
        number += 1
    used.add(value)
    return value


def _cells(sheet, values, *, fill=None, color=None, bold=False, size=None,
           number_formats=None):
    row = []
    for index, value in enumerate(values):
        cell = WriteOnlyCell(sheet, value=_safe_value(value))
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        if color or bold or size:
            cell.font = Font(color=color, bold=bold, size=size)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        if number_formats and index < len(number_formats) and number_formats[index]:
            cell.number_format = number_formats[index]
        row.append(cell)
    sheet.append(row)


def _formats(headers):
    result = []
    for header in headers:
        if header in ("基础策略编号", "入场次数规则", "成本模式", "成本模式代码") or "JSON" in header or "指纹" in header:
            result.append("@")
        elif any(word in header for word in ("费率", "偏移", "返佣")):
            result.append("0.000000%")
        elif "（%）" in header:
            result.append("0.000%")
        elif "（USDC）" in header:
            result.append("#,##0.00")
        elif "（倍）" in header:
            result.append("0.000")
        elif "（单/日）" in header or "（次/日）" in header:
            result.append("0.00")
        elif any(unit in header for unit in ("（分钟）", "（单）", "（次）")):
            result.append("0")
        elif header == "t值":
            result.append("0.000")
        else:
            result.append(None)
    return result


def _export_candidates(payload, output_path):
    """Candidate payloads use named lists, not the old leaderboard's 分类 map."""
    payload = candidate_display_payload(payload)
    headers = payload["表头"]
    for key in ("研究候选", "观察候选", "实盘候选"):
        for number, row in enumerate(payload.get(key, []), 5):
            for header in headers:
                value = row.get(header)
                if isinstance(value, str) and len(value.encode('utf-16-le')) // 2 > 32767:
                    raise ValueError(f'Excel单元格超过32767字符：{key} 第{number}行 {header}；拒绝截断完整配置')
    workbook = Workbook(write_only=True)
    settings = payload.get("设置", {})
    new_mode = settings.get("筛选方案") == "RETURN_DRAWDOWN"
    scope = ("全部已回测倍数" if settings.get("比较范围") == "ALL_RUN"
             else f"指定已回测倍数 {settings.get('统一目标杠杆', '')}x")
    rule = (f"先过硬门槛，保留合格最高期末资金的{settings.get('资金保留比例', .9):.1%}及以上，按回撤升序"
            if new_mode else "原严格成本预筛；按原类别内预筛分选研究候选")
    guide = workbook.create_sheet("0_说明与检查")
    guide.freeze_panes = "A4"
    guide.column_dimensions["A"].width = 36
    guide.column_dimensions["B"].width = 110
    _cells(guide, ["本次回测候选筛选", scope], fill=NAVY, color="FFFFFF", bold=True)
    _cells(guide, ["排序方法", rule], fill=NOTE)
    _cells(guide, ["项目", "内容"], fill=NAVY, color="FFFFFF", bold=True)
    for key in ("源文件", "总行数", "初筛通过行数", "研究候选数", "观察候选数", "实盘候选数",
                "研究预筛分说明", "成交成本说明", "容量口径说明"):
        if key in payload:
            _cells(guide, [key, payload[key]])
    for key, value in payload.get("筛选阶段统计", {}).items():
        _cells(guide, [key, value])
    for item in payload.get("高级待验证项", []):
        _cells(guide, ["未验证", item])
    _cells(guide, ["资格边界", "本轮优选尚未通过实盘验证；不等于未来最佳或实盘盈利保证。"], fill=NOTE)
    formats = _formats(headers)
    last = get_column_letter(max(1, len(headers)))
    for key, name in (("研究候选", "每类研究候选"), ("观察候选", "全局观察候选"), ("实盘候选", "实盘候选")):
        rows = payload.get(key, [])
        sheet = workbook.create_sheet(name)
        sheet.freeze_panes = "A5"
        for index, header in enumerate(headers, 1):
            sheet.column_dimensions[get_column_letter(index)].width = (44 if header == "入场次数规则" else 18 if header == "成本模式" else 60 if "JSON" in header else 55 if "判据" in header or header in ("成交与成本说明", "定义来源与版本核对", "参数完整性")
                else 24 if header == "基础策略编号" or any(k in header for k in ("指纹", "原因", "说明")) else 17)
        _cells(sheet, [name, scope], fill=NAVY, color="FFFFFF", bold=True)
        _cells(sheet, [rule], fill=NOTE)
        sheet.append([])
        _cells(sheet, headers, fill=NAVY, color="FFFFFF", bold=True)
        for number, row in enumerate(rows, 5):
            if "完整单策略配置JSON" in headers:sheet.row_dimensions[number].height = 44
            _cells(sheet, [str(row[header]) if header == "基础策略编号" and row.get(header) is not None else row.get(header) for header in headers], number_formats=formats)
        if not rows:
            _cells(sheet, ["尚未通过实盘验证，不生成实盘合格项。" if key == "实盘候选"
                           else "没有满足当前条件的候选；未放宽门槛凑数。"], fill=NOTE)
        add_openpyxl_scales(sheet, headers, 5, len(rows))
        sheet.auto_filter.ref = f"A4:{last}{4 + len(rows)}"
    rejected = workbook.create_sheet("淘汰统计")
    rejected.column_dimensions["A"].width = 65
    rejected.column_dimensions["B"].width = 20
    _cells(rejected, ["淘汰原因", "涉及账户数（可重叠）"], fill=NAVY, color="FFFFFF", bold=True)
    for row in payload.get("淘汰统计", []):
        _cells(rejected, [row["淘汰原因"], row["涉及行数"]])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)


def export_streaming_xlsx(payload_path: Path, output_path: Path) -> None:
    with payload_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if "研究候选" in payload:
        _export_candidates(payload, output_path)
        return
    headers = payload.get("表头", [])
    categories = payload.get("分类", {})
    ranking = payload.get("排行", "最优")
    limit = int(payload.get("名额", 5000))
    workbook = Workbook(write_only=True)
    used: set[str] = set()

    summary = workbook.create_sheet("0_说明与检查")
    used.add("0_说明与检查")
    summary.freeze_panes = "A4"
    _cells(summary, [f"ETHUSDC 各类止盈{ranking}前{limit}名"], fill=NAVY,
           color="FFFFFF", bold=True, size=16)
    _cells(summary, ["大排行榜采用流式写入，避免内存溢出；完整数据和中文表头均保留。"],
           fill=NOTE, color="7F6000", bold=True)
    _cells(summary, ["工作表", "实际行数", "最多行数"], fill=NAVY,
           color="FFFFFF", bold=True)
    if categories:
        for name, rows in categories.items():
            _cells(summary, [name, len(rows), limit])
    else:
        _cells(summary, ["没有符合当前排行榜门槛的策略", 0, limit],
               fill=NOTE, color="C00000", bold=True)
    summary.column_dimensions["A"].width = 34
    summary.column_dimensions["B"].width = 16
    summary.column_dimensions["C"].width = 16

    sheet_names = {}
    number_formats = _formats(headers)
    last_column = get_column_letter(max(1, len(headers)))
    for category, rows in categories.items():
        name = _sheet_name(category, used)
        sheet_names[category] = name
        sheet = workbook.create_sheet(name)
        sheet.freeze_panes = "A5"
        _cells(sheet, [f"{category}｜{ranking}前{len(rows)}名"], fill=NAVY,
               color="FFFFFF", bold=True, size=15)
        _cells(sheet, [f"排行指标：{payload.get('排行指标', '期末资金（USDC）')}"],
               fill=NOTE, color="7F6000", bold=True)
        sheet.append([])
        _cells(sheet, headers, fill=NAVY, color="FFFFFF", bold=True)
        for row in rows:
            _cells(sheet, row, number_formats=number_formats)
        add_openpyxl_scales(sheet, headers, 5, len(rows))
        if headers:
            sheet.auto_filter.ref = f"A4:{last_column}{max(4, len(rows) + 4)}"
        for index, header in enumerate(headers, 1):
            width = 22 if any(key in header for key in ("条件", "方案", "原因", "指纹", "策略")) else 15
            sheet.column_dimensions[get_column_letter(index)].width = width

    if ranking != "最差":
        daily = workbook.create_sheet("每日收益测算")
        used.add("每日收益测算")
        _cells(daily, ["各类止盈每日收益——成交成本校正"], fill=NAVY,
               color="FFFFFF", bold=True, size=16)
        _cells(daily, ["按各类第1行完整交易数与名义倍数估算，保留已扣手续费，仅替换成交偏移；不是逐日权益收益，不反映数量上限及复利。交易序列固定；改变手续费或间隔须回UI重跑。旧分批缺少完整交易数时留空。"],
               fill=NOTE, color="7F6000", bold=True)
        daily_headers = ["止盈类别", "扣费后未扣偏移日估算", "偏移0.01%日估算", "偏移0.05%日估算",
                         "偏移0.072%日估算", "初始资金", "情景一日估算金额", "情景二日估算金额",
                         "情景三日估算金额", "平均日完整交易数", "名义倍数", "胜率（仅展示）",
                         "回测单笔净收益率", "回测已计往返偏移"]
        _cells(daily, daily_headers, fill=NAVY, color="FFFFFF", bold=True)
        column = {name: index for index, name in enumerate(headers)}
        def value(row, *names, default=0.0):
            for key in names:
                if key in column:
                    try:
                        return float(row[column[key]])
                    except (TypeError, ValueError):
                        return default
            return default
        daily_formats = [None] + ["0.0000%"] * 4 + ["0.000000"] * 4 + ["0.00", "0.0"] + ["0.0000%"] * 3
        for category, rows in categories.items():
            if not rows:
                continue
            row = rows[0]
            if "平均日完整交易数（次/日）" in column or "平均日完整交易数（单/日）" in column:
                trades = value(row, "平均日完整交易数（次/日）", "平均日完整交易数（单/日）")
            else:
                legacy_orders = value(row, "平均日成交订单数（笔/日）", "平均日成交单数（单/日）", "平均日成交单数")
                trades = None if category == "分批止盈" else legacy_orders / 2.0
            multiple = value(row, "名义倍数（倍）", "名义倍数")
            win_rate = value(row, "胜率（%）", "胜率")
            average_return = value(row, "平均单笔收益率（%）", "平均单笔收益率")
            slippage = value(row, "往返成交偏移（%）")
            raw = maker = specified = taker = None
            if trades is not None:
                raw = trades * (average_return + slippage) * multiple
                maker = raw - trades * 0.0001 * multiple
                specified = raw - trades * 0.0005 * multiple
                taker = raw - trades * 0.001 * multiple
            initial = value(row, "初始资金（USDC）", default=payload.get("初始资金"))
            amounts = [x * initial if x is not None and initial is not None else None
                       for x in (maker, specified, taker)]
            _cells(daily, [category, raw, maker, specified, taker, initial,
                           *amounts,
                           trades, multiple, win_rate, average_return, slippage],
                   number_formats=daily_formats)
        for index in range(1, len(daily_headers) + 1):
            daily.column_dimensions[get_column_letter(index)].width = 18
        daily.column_dimensions["A"].width = 24
        daily.freeze_panes = "A4"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
