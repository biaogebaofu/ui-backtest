from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from export_runtime import export_xlsx_atomic
from selection_config import 规范化候选筛选
from result_period import read_period_info, completed_trades_per_day
from account_statistics import ACCOUNT_FIELD, account_row, CAPACITY_TIMESTAMP
from entry_position import position_filter_label
from ranking_view import config_fingerprint
from indicator_combinations import DEFAULT_REGISTRY, combination_label
from strategy_description import PARAMETER_HEADERS, EXTRA_HEADERS, load_context, describe_row, validate_cell_lengths


EXECUTION_COLUMNS = (
    "平仓后最小开仓间隔（分钟）", "开仓基础手续费率（%）", "平仓基础手续费率（%）",
    "BNB手续费抵扣（0否1是）", "手续费返佣比例（%）",
    "开仓净手续费率（%）", "平仓净手续费率（%）", "成本模式",
)
POSITION_COLUMNS = ("开仓位置过滤代码", "开仓位置过滤说明")


高级待验证项 = (
    "样本外Calmar与最大回撤", "样本外年化对数收益", "DSR", "PBO（策略集合级）",
    "Newey-West/区块Bootstrap t值", "正收益月份比例", "参数邻域稳定度",
    "有效独立样本数", "最大回撤持续时间", "最长连续亏损", "95%/99% CVaR",
    "样本内/样本外衰减比例", "Maker成交率",
)

输出表头 = [
    "策略指纹", "名义倍数（倍）", "期末资金（USDC）", "账户净利润（USDC）", "最大回撤（%）",
    "胜率（%）", "平均日完整交易数（单/日）", "利润因子PF", "PF状态", "筛选提示",
    "止盈类别", "止盈方案编号", "止盈周期组合", "止盈指标", "止盈参数一", "止盈参数二", "止盈参数三",
    "止盈后等待分钟", "基础策略编号", "开仓MACD代码", "4小时条件代码", "1小时条件代码", "15分钟条件代码",
    "5分钟条件代码", "1分钟条件代码", "入场触发口径", "回测计算版本", "止损代码", "统一目标杠杆（倍）", "策略指纹", "相似策略簇",
    "交易次数（单）", "平均日完整交易数（单/日）", "胜率（%）", "多单占比（%）", "平均持仓时间（分钟）",
    "容量占用率（%）", "容量计算口径", "盈亏比（倍）", "普通独立样本t值（仅参考）", "目标杠杆期末资金（USDC）",
    "目标杠杆最大回撤（%）", "爆仓保护次数（次）", "全仓强平次数（次）", "固定止损代码",
    "叠加止盈代码", "开仓方向", "交易会话", "强制时间止损（分钟）", "目标杠杆实际成交次数（单）",
    "资金性停机标记（0否1是）", "期末可开仓数量（ETH）", "初始资金（USDC）",
    "ETH最小开仓数量（ETH）", "ETH单次最大开仓数量（ETH）", "最小/最大下单量约束已验证",
    "2025目标杠杆简单收益（%）", "2026目标杠杆简单收益（%）",
    "最差可用年度简单收益（%）", "扣手续费后、未扣成交偏移单笔收益（%）", "平均往返偏移（%）", "平均成本后单笔收益（%）",
    "p95往返偏移（%）", "p95成本后单笔收益（%）", "极端往返偏移（%）", "极端成本后单笔收益（%）",
    "扣手续费后收益÷平均往返偏移（倍）", "研究预筛分", "风险等级", "导出原因", "淘汰原因", "高级待验证项",
    "实盘资格", "样本外Calmar", "样本外年化对数收益（%）", "DSR置信度（%）", "PBO（%）",
    "Newey-West/区块Bootstrap t值", "正收益月份比例（%）", "参数邻域盈利比例（%）", "有效独立样本（区块）",
    "最大回撤持续时间", "最长连续亏损", "95% CVaR（%）", "99% CVaR（%）", "样本内/样本外收益衰减比例（%）",
    "Maker成交率（%）", *EXECUTION_COLUMNS, *POSITION_COLUMNS,
]
输出表头 = list(dict.fromkeys(输出表头))
附加参数表头 = [name for name in dict.fromkeys(PARAMETER_HEADERS + EXTRA_HEADERS) if name not in 输出表头]
输出表头 += 附加参数表头


def emit(kind: str, **data):
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def safe_float(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def parse_size_label(label: str) -> float:
    text = label.strip().lower()
    if text.endswith("x"):
        return float(text[:-1])
    if "/" in text:
        left, right = text.split("/", 1)
        return float(left) / float(right)
    return float(text)


def split_values(text: str, cast=float) -> list:
    return [cast(item) for item in text.split(";") if item != ""]


def calculate_capacity(trades_per_day: float, average_holding_minutes: float, cooldown_minutes: int) -> float:
    return trades_per_day * (average_holding_minutes + cooldown_minutes) / 1440.0


def capacity_for_row(row, trades_per_day, cooldown):
    exact = row.get("实际容量占用率（%）")
    if exact is not None and str(exact).strip():
        capacity = float(exact)
        if not math.isfinite(capacity) or not 0.0 <= capacity <= 1.0 + 1e-9:
            raise ValueError("实际容量占用率无效或超过100%，请核对逐账户数据")
        return capacity, "逐账户实际持仓+原止盈等待（不含新增全退出间隔，非盘口容量）"
    return (calculate_capacity(trades_per_day, safe_float(row["平均持仓时间（分钟）"]), cooldown),
            "旧数据保守估算：每笔均加原止盈等待，可能高估（不含新增全退出间隔，非盘口容量）")


def minimum_order_was_enabled(row):
    # 有最小数量参数不等于启用了资金不足停机；旧数据无法证明时不得标作已验证。
    return str(row.get("ETH最小开仓约束启用（0否1是）", "")).strip() == "1"


def cost_metrics(net_average_return: float, applied_cost: float, p95_cost: float, extreme_cost: float) -> dict:
    # CSV 的平均收益已扣实际模拟手续费；压力情景只替换成交偏移，不能重扣手续费。
    # 历史键名保留兼容调用方，gross_before_cost 是扣手续费后、扣成交偏移前的收益。
    gross_before_cost = net_average_return + applied_cost
    ratio = None if applied_cost <= 0.0 else gross_before_cost / applied_cost
    return {
        "gross_before_cost": gross_before_cost,
        "p95_net": gross_before_cost - p95_cost,
        "extreme_net": gross_before_cost - extreme_cost,
        "cost_ratio": ratio,
    }


def execution_values(row):
    """CSV 费率仍为原始小数；旧 CSV 缺失的执行参数严格按零处理。"""
    values = {name: safe_float(row.get(name)) for name in EXECUTION_COLUMNS[:-1]}
    for name in (EXECUTION_COLUMNS[0], EXECUTION_COLUMNS[3]):
        values[name] = int(values[name])
    # 旧CSV只标注历史口径，不用新版二选一重新计算旧收益。
    mode = row.get("成本模式")
    if not mode:
        has_fees = any(values[k] != 0 for k in EXECUTION_COLUMNS[5:7])
        has_slippage = safe_float(row.get("往返成交偏移（%）")) != 0
        mode = "LEGACY_COMBINED" if has_fees and has_slippage else "FEE" if has_fees else "SLIPPAGE"
    values["成本模式"] = mode
    return values


def original_run_context(directory):
    """Read one verified same-directory run, never infer sweep modes from CSV rows."""
    directory = Path(directory)
    has_context = any((directory / name).is_file() for name in ('组合选择.json', '回测运行身份.json'))
    checkpoint_path = directory / '断点记录.json'
    if not has_context and checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text('utf-8-sig'))
        has_context = any(key in checkpoint for key in ('selection', 'run_identity'))
    if not has_context:
        return None
    from ranking_view import CODE_NAMES
    from strategy_description import load_context
    from execution_settings import cost_modes, effective_fee_rates, effective_slippage
    from selection_config import 入场口径列表
    context = load_context({}, directory, CODE_NAMES)
    if context.get('error'):
        # Older exports never promised a complete runnable context. Preserve their
        # original scalar-CSV path, without authorizing newly declared sweeps.
        archived = []
        for name in ('组合选择.json', '断点记录.json'):
            path = directory / name
            if path.is_file():
                value = json.loads(path.read_text('utf-8-sig'))
                archived.append(value.get('selection') if name == '断点记录.json' else value)
        if archived and all(isinstance(raw, dict) and type(raw.get('版本')) is int and 1 <= raw['版本'] < 19
                            and not any(isinstance(raw.get(key), list) and len(raw[key]) > 1
                                        for key in ('成本模式', '入场触发口径')) for raw in archived):
            return None
        raise ValueError('原运行上下文核验失败，不能放行混合入场/成本扫描：' + context['error'])
    selection = context['selection']
    context['entry_modes'] = 入场口径列表(selection)
    funds, fees = selection['资金约束'], selection['手续费']
    fixed = {'初始资金（USDC）': funds['初始资金USDC'],
             'ETH最小开仓数量（ETH）': funds['最小开仓数量ETH'],
             'ETH单次最大开仓数量（ETH）': funds['最大开仓数量ETH'],
             'ETH最小开仓约束启用（0否1是）': int(funds['低于最小数量停止']),
             '成交价格口径': selection['成交价格口径'],
             '回测计算版本': context['identity']['engine_version'] + ':' + selection['成交价格口径'],
             '平仓后最小开仓间隔（分钟）': selection['平仓后最小开仓间隔分钟'],
             '开仓基础手续费率（%）': fees['开仓费率'], '平仓基础手续费率（%）': fees['平仓费率'],
             'BNB手续费抵扣（0否1是）': int(fees['BNB抵扣']), '手续费返佣比例（%）': fees['返佣比例']}
    context['cost_checks'] = {}
    for mode in cost_modes(selection):
        branch = dict(selection, 成本模式=mode)
        checks = dict(fixed, 成本模式=mode)
        checks.update(zip(('开仓净手续费率（%）', '平仓净手续费率（%）'), effective_fee_rates(branch)))
        slip = effective_slippage(branch)
        checks.update(zip(('开仓成交偏移（%）', '平仓成交偏移（%）'), slip))
        checks['往返成交偏移（%）'] = sum(slip)
        context['cost_checks'][mode] = checks
    return context


def verify_run_cost_row(row, context):
    """A scalar branch must be explicitly selected and carry only its own cost."""
    if context is None:
        return
    from fingerprint_lookup import _same
    if row.get('入场触发口径') not in context['entry_modes']:
        raise ValueError('结果入场触发口径不属于原运行选择，不能混入另一入场模式')
    mode = row.get('成本模式')
    if not isinstance(mode, str) or mode not in context['cost_checks']:
        raise ValueError('结果成本模式不属于原运行选择，不能混入另一成本模式')
    for key, expected in context['cost_checks'][mode].items():
        _same(row.get(key), expected, key)


def position_values(row):
    code = str(row.get("开仓位置过滤代码") or "OFF").strip() or "OFF"
    return {"开仓位置过滤代码": code, "开仓位置过滤说明": position_filter_label(code)}


@lru_cache(maxsize=1)
def native_labels():
    from strategy_space import 生成止损组合
    return {code: label for code, _, label in 生成止损组合()}


def native_rank_row(source_row, size_index, tp, days=None):
    """Same native schema/values as worst_export's per-account ranking row."""
    from backtest_worker import 中文表头, case_label, size_label
    from strategy_space import 固定比例说明
    actual = account_row(source_row, size_index)
    stop_labels = native_labels()
    def number(name, default=0.0):
        value = actual.get(name)
        return float(value) if value is not None and str(value).strip() else default
    def selected(name, default=0.0):
        values = str(source_row.get(name) or '').split(';')
        return float(values[size_index]) if size_index < len(values) and values[size_index] else default
    leverage = parse_size_label(source_row['所选仓位顺序'].split(';')[size_index])
    fixed = actual.get('固定止损代码', 'OFF')
    overlay = actual.get('叠加止盈代码', 'OFF')
    stop = actual['止损代码']
    category = tp.get('止盈类别', '未知止盈')
    initial = number('初始资金（USDC）', 100.)
    final = selected('所选仓位期末资金（USDC）')
    maximum = number('ETH单次最大开仓数量（ETH）', 100.)
    stats = [number(name) for name in ('交易次数（单）', '胜率（%）', '多单占比（%）')]
    # The old ranking uses stored per-account frequency when that evidence exists.
    daily = (number('平均日完整交易数（次/日）') if source_row.get(ACCOUNT_FIELD)
             else completed_trades_per_day(actual, category, days))
    stats += [daily, number('平均日成交订单数（笔/日）', number('平均日成交单数（单/日）'))]
    stats += [number(name) for name in ('平均持仓时间（分钟）', '平均单笔收益率（%）',
                                      '毛收益合计（%）', '盈亏比（倍）', 't值')]
    entry_slip = number('开仓成交偏移（%）')
    exit_slip = number('平仓成交偏移（%）')
    values = [
        int(actual['止盈方案编号']), category, tp.get('周期组合', ''), tp.get('指标', ''),
        *(safe_float(tp.get(f'参数{name}（原始小数）')) for name in ('一', '二', '三')),
        int(actual.get('止盈后等待分钟') or 0), int(actual['基础策略编号']),
        'MACD柱' if int(actual['开仓MACD代码']) == 0 else 'DIF线',
        *(case_label(int(actual[name])) for name in ('4小时条件代码', '1小时条件代码', '15分钟条件代码', '5分钟条件代码', '1分钟条件代码')),
        actual.get('入场触发口径', 'LEGACY_UNKNOWN'), stop,
        combination_label('stop', stop) if DEFAULT_REGISTRY.resolve('stop', stop) is not None else stop_labels.get(stop, stop),
        fixed, 固定比例说明(fixed, 'FSL'), overlay, 固定比例说明(overlay, 'FTP'),
        actual.get('开仓方向', 'BOTH'), actual.get('交易会话', 'ALL'), int(actual.get('强制时间止损（分钟）') or 0),
        size_label(leverage), leverage, *stats,
        final, selected('所选仓位累计收益率（%）', final / initial - 1 if initial else 0.),
        selected('所选仓位最大回撤（%）'), int(selected('所选仓位爆仓保护次数（次）')),
        int(selected('所选仓位全仓强平次数（次）')), number('2025毛收益（%）'), number('2026毛收益（%）'),
        entry_slip, exit_slip, number('往返成交偏移（%）', entry_slip + exit_slip),
        actual.get('回测计算版本', '旧版（未逐仓重放）'), initial, number('ETH最小开仓数量（ETH）'), maximum,
        int(selected('所选仓位实际成交次数（单）', stats[0])), int(selected('所选仓位资金性停机标记（0否1是）')),
        min(selected('所选仓位期末可开仓数量（ETH）'), maximum),
        number('实际止盈等待总时间（分钟）', None) if source_row.get(ACCOUNT_FIELD) else None,
        number('实际容量占用率（%）', None) if source_row.get(ACCOUNT_FIELD) else None,
        int(number('ETH最小开仓约束启用（0否1是）')) if actual.get('ETH最小开仓约束启用（0否1是）') is not None and str(actual['ETH最小开仓约束启用（0否1是）']).strip() else None,
        actual.get('成交价格口径', 'THEORETICAL'),
        *execution_values({name: actual[name] for name in EXECUTION_COLUMNS if name in actual}).values(),
        *position_values(actual).values(),
        float(actual.get(CAPACITY_TIMESTAMP, -2)),
    ]
    if len(values) != len(中文表头):
        raise ValueError('候选原始行与排行榜表头不一致')
    return dict(zip(中文表头, values))


def finite_value(value, label):
    if isinstance(value, bool) or value is None or not str(value).strip():
        raise ValueError(f'{label}缺少有效数值')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'{label}含非有限数值')
    return result


def return_drawdown_failures(record, settings):
    failures = []
    if record['forced_liquidations'] != 0: failures.append('发生全仓强平')
    if record['capital_stop'] != 0: failures.append('资金不足ETH最小开仓数量')
    if record['liquidations'] > settings['爆仓保护次数上限']: failures.append('爆仓保护次数超过上限')
    if record['mdd'] > settings['最大回撤硬上限']: failures.append('最大回撤超过硬上限')
    no_losses = (record['profit_factor'] == 0 and record['win_rate'] == 1
                 and record['average_net'] > 0 and record['trades'] > 0
                 and record['trades'] >= settings['最低原始交易数'])
    if record['profit_factor'] < settings['盈亏比下限'] and not no_losses: failures.append('利润因子不足或不可判定')
    if record['trades'] < settings['最低原始交易数']: failures.append('原始交易次数不足')
    if record['final_money'] <= record['initial_capital']: failures.append('实际期末资金未超过初始资金')
    if record['average_net'] <= 0: failures.append('平均净收益不为正')
    return failures, no_losses


def account_comparisons(reader, settings):
    for source_number, row in enumerate(reader, 1):
        sizes = [parse_size_label(item) for item in split_values(row['所选仓位顺序'], str)]
        indices = list(range(len(sizes))) if settings['比较范围'] == 'ALL_RUN' else [
            i for i, value in enumerate(sizes) if abs(value - settings['统一目标杠杆']) < 1e-9]
        if not indices:
            yield source_number, row, None, None
        for index in indices:
            yield source_number, row, index, sizes[index]


def preliminary_failures(record: dict, settings: dict, available_years: set[int]) -> list[str]:
    failed = []
    if record["liquidations"] > settings["爆仓保护次数上限"]:
        failed.append("爆仓保护次数超过上限")
    if record["forced_liquidations"] != 0: failed.append("发生全仓强平")
    if record.get("capital_stop", 0) != 0:
        failed.append("资金不足ETH最小开仓数量")
    if record["mdd"] > settings["最大回撤硬上限"]: failed.append("最大回撤超过硬上限")
    if 2025 in available_years and record["y2025"] <= 0.0: failed.append("2025收益不为正")
    if 2026 in available_years and record["y2026"] <= 0.0: failed.append("2026收益不为正")
    if record["profit_factor"] < settings["盈亏比下限"]: failed.append("盈亏比不足")
    if record["p95_net"] <= 0.0: failed.append("p95成本后单笔收益不为正")
    if record["cost_ratio"] is not None and record["cost_ratio"] < 2.0: failed.append("扣手续费后收益/平均往返偏移不足2倍")
    if record["gross_before_cost"] <= 0.0: failed.append("扣手续费后、未扣成交偏移收益不为正")
    if record["capacity"] > settings["容量占用率上限"]: failed.append("容量占用率超过上限")
    if not settings["多单占比下限"] <= record["long_share"] <= settings["多单占比上限"]:
        failed.append("多单占比超出范围")
    if record["trades"] < settings["最低原始交易数"]: failed.append("原始交易次数不足")
    return failed


def read_tp_dictionary(path: Path) -> dict[int, dict]:
    registry_path = path.parent / '指标组合字典.json'
    if registry_path.is_file():
        DEFAULT_REGISTRY.load(registry_path)
    result = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            result[int(row["止盈方案编号"])] = row
    return result


def fingerprint(parts) -> str:
    raw = "|".join(str(x) for x in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def find_node() -> str | None:
    candidates = [
        Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe",
        Path(sys.executable).with_name("node.exe"),
    ]
    for path in candidates:
        if path.exists(): return str(path)
    return shutil.which("node")


def export_excel(project_dir: Path, payload_path: Path, output_path: Path):
    node = find_node()
    script = project_dir / "candidate_excel_export.mjs"
    try:
        return export_xlsx_atomic(node, script, payload_path, output_path, project_dir)
    except RuntimeError as exc:
        raise RuntimeError("候选Excel导出失败：" + str(exc)[-2000:]) from exc


def write_csv(path: Path, rows: list[dict]):
    def write_to(target: Path):
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=输出表头, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
    try:
        write_to(path)
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_本次_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
        write_to(fallback)
        emit("warning", message=f"{path.name}正被Excel占用，已改存为：{fallback.name}")
        return fallback


def row_to_output(row: sqlite3.Row, tp: dict, reason: str) -> dict:
    pending_items = list(高级待验证项)
    if not row["minimum_order_verified"]:
        pending_items.append("ETH最小/最大开仓数量约束（未启用或旧CSV缺少启用证据，需重跑验证）")
    pending = "；".join(pending_items)
    ratio = row["cost_ratio"]
    risk = "研究级A（待高级验证）" if row["mdd"] <= row["preferred_mdd"] else "研究级B（待高级验证）"
    values = {
        "名义倍数（倍）": row["leverage"], "期末资金（USDC）": row["final_money"],
        "最大回撤（%）": row["mdd"], "账户净利润（USDC）": row["final_money"] - row["initial_capital"],
        "利润因子PF": row["profit_factor"], "PF状态": "沿用原始统计（旧压力筛选）",
        "筛选提示": "旧压力筛选口径；不同初始资金/成本条件不可视为同一收益回撤比较",
        "止盈类别": row["category"], "止盈方案编号": row["tp_id"], "止盈周期组合": tp.get("周期组合", ""),
        "止盈指标": tp.get("指标", ""), "止盈参数一": safe_float(tp.get("参数一（原始小数）")),
        "止盈参数二": safe_float(tp.get("参数二（原始小数）")), "止盈参数三": safe_float(tp.get("参数三（原始小数）")),
        "止盈后等待分钟": row["cooldown"], "基础策略编号": row["base_id"], "开仓MACD代码": row["macd"],
        "4小时条件代码": row["c4"], "1小时条件代码": row["c1"], "15分钟条件代码": row["c15"],
        "5分钟条件代码": row["c5"], "1分钟条件代码": row["c1m"],
        "入场触发口径": row["entry_mode"], "止损代码": row["stop_code"],
        "回测计算版本": row["engine_version"],
        "统一目标杠杆（倍）": row["leverage"], "策略指纹": row["strategy_fp"], "相似策略簇": row["cluster_fp"],
        "交易次数（单）": row["trades"], "平均日完整交易数（单/日）": row["trades_per_day"],
        "胜率（%）": row["win_rate"], "多单占比（%）": row["long_share"], "平均持仓时间（分钟）": row["holding"],
        "容量占用率（%）": row["capacity"], "容量计算口径": row["capacity_basis"],
        "盈亏比（倍）": row["profit_factor"],
        "普通独立样本t值（仅参考）": row["naive_t"], "目标杠杆期末资金（USDC）": row["final_money"],
        "目标杠杆最大回撤（%）": row["mdd"], "爆仓保护次数（次）": row["liquidations"],
        "全仓强平次数（次）": row["forced_liquidations"],
        "固定止损代码": row["fixed_stop_code"],
        "叠加止盈代码": row["overlay_code"], "开仓方向": row["direction"],
        "交易会话": row["session"], "强制时间止损（分钟）": row["hard_minutes"],
        "目标杠杆实际成交次数（单）": row["executed"], "资金性停机标记（0否1是）": row["capital_stop"],
        "期末可开仓数量（ETH）": row["end_quantity"], "初始资金（USDC）": row["initial_capital"],
        "ETH最小开仓数量（ETH）": row["minimum_order_eth"],
        "ETH单次最大开仓数量（ETH）": row["maximum_order_eth"],
        "最小/最大下单量约束已验证": "是" if row["minimum_order_verified"] else "否（最小/最大数量约束需重跑）",
        "2025目标杠杆简单收益（%）": row["y2025"] * row["leverage"],
        "2026目标杠杆简单收益（%）": row["y2026"] * row["leverage"],
        "最差可用年度简单收益（%）": row["worst_year"] * row["leverage"],
        "扣手续费后、未扣成交偏移单笔收益（%）": row["gross_before_cost"], "平均往返偏移（%）": row["average_cost"],
        "平均成本后单笔收益（%）": row["average_net"], "p95往返偏移（%）": row["p95_cost"],
        "p95成本后单笔收益（%）": row["p95_net"], "极端往返偏移（%）": row["extreme_cost"],
        "极端成本后单笔收益（%）": row["extreme_net"], "扣手续费后收益÷平均往返偏移（倍）": ratio,
        "研究预筛分": row["score"], "风险等级": risk, "导出原因": reason, "淘汰原因": "",
        "高级待验证项": pending, "实盘资格": "否（高级门槛尚未验证）",
    }
    values.update(json.loads(row["execution_json"]))
    for name in 输出表头:
        values.setdefault(name, None)
    return values


def write_native_catalog(output_dir, source, records):
    from backtest_worker import 中文表头
    categories = defaultdict(list)
    seen = set()
    for record in records:
        fingerprint_value = config_fingerprint(record)
        if fingerprint_value in seen:
            continue
        seen.add(fingerprint_value)
        categories[record['止盈类别']].append([None if isinstance(record.get(name), float) and not math.isfinite(record[name])
                                               else record.get(name) for name in 中文表头])
    payload = {'表头': 中文表头, '分类': dict(categories), '源文件': str(source),
               '原始结果目录': str(source.parent), '说明': '仅保存本次导出候选的原始排行榜行；交易参数及数据身份仍从原始结果目录核验'}
    (output_dir / '候选原始记录.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), 'utf-8')


def append_candidate_details(rows, natives, source_dir, context=None):
    """Append descriptions from matching native rows; never alter ranking metrics."""
    from ranking_view import CODE_NAMES
    if context is None:
        context = load_context({}, source_dir, CODE_NAMES)
    by_fingerprint = {config_fingerprint(native): native for native in natives}
    for row in rows:
        native = by_fingerprint.get(row.get('策略指纹'))
        if native is None:
            extra = {'参数完整性': '不完整：缺少匹配的原始排行榜行；未猜参数', '完整单策略配置JSON': ''}
        else:
            extra = {**native, **describe_row(native, context)}
        for name in 附加参数表头:
            row[name] = extra.get(name, '未记录')
        # JSON numbers would be rounded by the JS XLSX renderer before cell
        # formatting. Keep long identifiers exact without changing native hashes.
        for name in ('基础策略编号', '止盈方案编号', '4小时条件代码', '1小时条件代码',
                     '15分钟条件代码', '5分钟条件代码', '1分钟条件代码'):
            identifier = row.get(name)
            if type(identifier) is int and abs(identifier) >= 10**15:
                row[name] = str(identifier)
    validate_cell_lengths({'表头': 附加参数表头, '分类': {
        '候选附加信息': [[row.get(name) for name in 附加参数表头] for row in rows]}})


RETURN_ORDER = 'mdd ASC, final_money DESC, profit_factor DESC, leverage ASC, strategy_fp ASC'


def return_candidate_output(source_row, index, native, record, settings, available_years, no_losses):
    actual = account_row(source_row, index)
    leverage = native['名义倍数（倍）']
    hints = ['样本内研究候选，尚无实盘资格；年度、成本压力、多空占比及持仓占比仅提示']
    if no_losses: hints.append('无亏损样本，PF未定义；保留原始PF=0，不伪造无限大')
    optional = lambda value: safe_float(value, None)
    avg_cost = native['往返成交偏移（%）']
    costs = cost_metrics(record['average_net'], avg_cost, settings['p95往返偏移'], settings['极端往返偏移'])
    if costs['p95_net'] <= 0: hints.append('p95偏移压力下平均单笔不为正')
    if not settings['多单占比下限'] <= native['多单占比（%）'] <= settings['多单占比上限']:
        hints.append('多单占比超出旧压力模式范围')
    try:
        capacity, basis = capacity_for_row(actual, native['平均日完整交易数（次/日）'], native['止盈后等待分钟'])
        if capacity > settings['容量占用率上限']: hints.append('持仓/等待占比超过旧压力模式上限')
    except (ValueError, TypeError):
        capacity, basis = None, '缺少有效持仓/等待占比，仅提示'
    years = {year: optional(actual.get(f'{year}毛收益（%）')) for year in (2025, 2026)}
    for year in available_years & years.keys():
        if years[year] is None or years[year] <= 0: hints.append(f'{year}年度收益非正或缺少有效数据')
    fp = config_fingerprint(native)
    output = {name: None for name in 输出表头}
    output.update(native)
    output.update({name: int(source_row[name]) for name in ('开仓MACD代码', '4小时条件代码', '1小时条件代码', '15分钟条件代码', '5分钟条件代码', '1分钟条件代码')})
    output.update({'账户净利润（USDC）': record['final_money'] - record['initial_capital'],
                   '利润因子PF': record['profit_factor'], 'PF状态': '无亏损样本，PF未定义（原值0）' if no_losses else '可用',
                   '筛选提示': '；'.join(hints), '策略指纹': fp, '相似策略簇': fp,
                   '统一目标杠杆（倍）': leverage, '目标杠杆期末资金（USDC）': record['final_money'],
                   '目标杠杆最大回撤（%）': record['mdd'], '平均日完整交易数（单/日）': native['平均日完整交易数（次/日）'],
                   '普通独立样本t值（仅参考）': optional(actual.get('t值')), '容量占用率（%）': capacity, '容量计算口径': basis,
                   '目标杠杆实际成交次数（单）': native['实际成交次数（单）'], '最小/最大下单量约束已验证': '是' if minimum_order_was_enabled(actual) else '否（需核验）',
                   '平均成本后单笔收益（%）': record['average_net'], '平均往返偏移（%）': avg_cost,
                   '扣手续费后、未扣成交偏移单笔收益（%）': costs['gross_before_cost'], 'p95往返偏移（%）': settings['p95往返偏移'],
                   'p95成本后单笔收益（%）': costs['p95_net'], '极端往返偏移（%）': settings['极端往返偏移'],
                   '极端成本后单笔收益（%）': costs['extreme_net'], '扣手续费后收益÷平均往返偏移（倍）': costs['cost_ratio'],
                   '风险等级': '研究级A（待高级验证）' if record['mdd'] <= settings['最大回撤优选'] else '研究级B（待高级验证）',
                   '高级待验证项': '；'.join(高级待验证项), '实盘资格': '否（高级门槛尚未验证）'})
    for year in (2025, 2026): output[f'{year}目标杠杆简单收益（%）'] = None if years[year] is None else years[year] * leverage
    available = [years[y] for y in available_years & years.keys() if years[y] is not None]
    output['最差可用年度简单收益（%）'] = min(available) * leverage if available else None
    # Optional non-finite statistics remain blank, never become attractive zeroes.
    native = {key: None if isinstance(value, float) and not math.isfinite(value) else value for key, value in native.items()}
    output = {key: None if isinstance(value, float) and not math.isfinite(value) else value for key, value in output.items()}
    return output, native


def run_return_drawdown(project_dir, output_dir, source, settings, tp_lookup):
    run_context = original_run_context(source.parent)
    """Stream actual accounts into SQLite; only final bounded candidate lists enter memory."""
    days, available_years = read_period_info(source.parent)
    source_stat = source.stat()
    source_identity = (source_stat.st_size, source_stat.st_mtime_ns)
    stop_flag = output_dir / '控制' / '停止候选导出.flag'
    cache = output_dir / '缓存'
    cache.mkdir(parents=True, exist_ok=True)
    db_fd, db_name = tempfile.mkstemp(prefix='收益回撤候选_', suffix='.sqlite', dir=cache)
    os.close(db_fd)
    db_path = Path(db_name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA temp_store=FILE')
    conn.execute('CREATE TABLE eligible (strategy_fp TEXT PRIMARY KEY, category TEXT, mdd REAL, final_money REAL, profit_factor REAL, leverage REAL, source_number INTEGER, size_index INTEGER)')
    counts = Counter({name: 0 for name in ('原始CSV行数', '比较账户数', '数据有效账户数', '硬门槛通过账户数',
                                         '去重合格账户数', '资金门槛内账户数', '缺少目标倍数行数')})
    rejected = Counter()
    context = None
    context_headers = ('初始资金（USDC）', '回测计算版本', '成交价格口径', '入场触发口径',
                       '开仓成交偏移（%）', '平仓成交偏移（%）', '往返成交偏移（%）',
                       'ETH最小开仓数量（ETH）', 'ETH单次最大开仓数量（ETH）', 'ETH最小开仓约束启用（0否1是）',
                       *EXECUTION_COLUMNS)
    sweep_headers = {'入场触发口径', '成本模式', '开仓成交偏移（%）', '平仓成交偏移（%）',
                     '往返成交偏移（%）', '开仓净手续费率（%）', '平仓净手续费率（%）'}
    comparison_headers = tuple(name for name in context_headers
                               if run_context is None or name not in sweep_headers)
    try:
        with source.open('r', encoding='utf-8-sig', newline='') as handle:
            for row_number, source_row in enumerate(csv.DictReader(handle), 1):
                counts['原始CSV行数'] += 1
                if (row_number == 1 or row_number % 1000 == 0) and stop_flag.exists():
                    emit('candidate_stopped', rows=row_number - 1)
                    return
                try:
                    sizes = [parse_size_label(label) for label in source_row['所选仓位顺序'].split(';')]
                    if not sizes or any(not math.isfinite(size) or size <= 0 for size in sizes):
                        raise ValueError('无效回放倍数')
                except (KeyError, TypeError, ValueError):
                    rejected['仓位顺序无效'] += 1
                    continue
                indices = list(range(len(sizes))) if settings['比较范围'] == 'ALL_RUN' else [
                    index for index, size in enumerate(sizes) if math.isclose(size, settings['统一目标杠杆'], abs_tol=1e-9)]
                if not indices:
                    counts['缺少目标倍数行数'] += 1
                    continue
                for index in indices:
                    counts['比较账户数'] += 1
                    try:
                        if len(sizes) > 1 and not source_row.get(ACCOUNT_FIELD):
                            raise ValueError('多倍数缺少逐账户交易统计，不能套用首档统计')
                        actual = account_row(source_row, index)
                        tp_id = int(source_row['止盈方案编号'])
                        if tp_id not in tp_lookup: raise ValueError('缺少原止盈字典')
                        def required(name): return finite_value(actual.get(name), name)
                        def account_value(name):
                            values = str(source_row.get(name) or '').split(';')
                            return finite_value(values[index] if index < len(values) else None, name)
                        record = {'initial_capital': required('初始资金（USDC）'),
                                  'final_money': account_value('所选仓位期末资金（USDC）'),
                                  'mdd': account_value('所选仓位最大回撤（%）'),
                                  'liquidations': account_value('所选仓位爆仓保护次数（次）'),
                                  'forced_liquidations': account_value('所选仓位全仓强平次数（次）'),
                                  'capital_stop': account_value('所选仓位资金性停机标记（0否1是）'),
                                  'trades': required('交易次数（单）'), 'win_rate': required('胜率（%）'),
                                  'profit_factor': required('盈亏比（倍）'), 'average_net': required('平均单笔收益率（%）')}
                        if (record['initial_capital'] <= 0 or record['final_money'] < 0 or not 0 <= record['mdd'] <= 1
                                or not 0 <= record['win_rate'] <= 1 or record['profit_factor'] < 0):
                            raise ValueError('账户结果数值范围无效')
                        for key in ('liquidations', 'forced_liquidations', 'capital_stop', 'trades'):
                            if record[key] < 0 or not record[key].is_integer(): raise ValueError('交易或风险计数无效')
                        if record['capital_stop'] not in (0, 1): raise ValueError('资金停机标记无效')
                        native = native_rank_row(source_row, index, tp_lookup[tp_id], days)
                        # Mandatory context must not pass through native legacy-default handling.
                        for name in context_headers:
                            if source_row.get(name) is None or not str(source_row[name]).strip(): raise ValueError('缺少原始比较上下文：' + name)
                            if name not in ('成本模式', '回测计算版本', '成交价格口径', '入场触发口径'):
                                finite_value(source_row[name], name)
                        if source_row['成本模式'] not in ('SLIPPAGE', 'FEE', 'LEGACY_COMBINED'):
                            raise ValueError('成本模式未知')
                        if source_row['成交价格口径'] not in ('THEORETICAL', 'CLOSE_CONFIRMED'):
                            raise ValueError('成交价格口径未知')
                        current_context = tuple(native[name] for name in comparison_headers)
                    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
                        rejected['数据无效或缺少逐账户证据：' + str(exc)] += 1
                        continue
                    verify_run_cost_row(native, run_context)
                    if context is None: context = current_context
                    elif context != current_context:
                        raise ValueError('收益/回撤比较发现混合初始资金、成本、执行或计算版本上下文；请按原运行拆分结果，不能直接比较绝对期末资金')
                    counts['数据有效账户数'] += 1
                    failures, no_losses = return_drawdown_failures(record, settings)
                    if failures:
                        rejected.update(failures)
                        continue
                    counts['硬门槛通过账户数'] += 1
                    conn.execute('INSERT OR IGNORE INTO eligible VALUES (?,?,?,?,?,?,?,?)',
                                 (config_fingerprint(native), native['止盈类别'], record['mdd'], record['final_money'],
                                  record['profit_factor'], sizes[index], row_number, index))
                if row_number % 1000 == 0:
                    conn.commit()
                    emit('candidate_progress', rows=row_number, eligible=counts['硬门槛通过账户数'])
        conn.commit()
        if stop_flag.exists():
            emit('candidate_stopped', rows=counts['原始CSV行数'])
            return
        counts['去重合格账户数'] = conn.execute('SELECT COUNT(*) FROM eligible').fetchone()[0]
        best = conn.execute('SELECT MAX(final_money) FROM eligible').fetchone()[0]
        threshold = None if best is None else best * settings['资金保留比例']
        research, natives = [], []
        if threshold is not None:
            counts['资金门槛内账户数'] = conn.execute('SELECT COUNT(*) FROM eligible WHERE final_money>=?', (threshold,)).fetchone()[0]
            query = f'''SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY category ORDER BY {RETURN_ORDER}) AS category_rank
                        FROM eligible WHERE final_money>=?) WHERE category_rank<=? ORDER BY {RETURN_ORDER}'''
            selected = list(conn.execute(query, (threshold, settings['每类最多'])))
            by_source = defaultdict(list)
            for rank, row in enumerate(selected): by_source[row['source_number']].append((rank, row))
            retained = {}
            # A second streaming pass only reconstructs the bounded final candidates.
            current_stat = source.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != source_identity:
                raise ValueError('候选扫描期间原CSV已变化，请在回测保存完成后重新导出')
            with source.open('r', encoding='utf-8-sig', newline='') as handle:
                for source_number, source_row in enumerate(csv.DictReader(handle), 1):
                    if source_number % 1000 == 0 and stop_flag.exists():
                        emit('candidate_stopped', rows=counts['原始CSV行数'])
                        return
                    for rank, selected_row in by_source.pop(source_number, []):
                        index = selected_row['size_index']
                        native = native_rank_row(source_row, index, tp_lookup[int(source_row['止盈方案编号'])], days)
                        record = {'initial_capital': native['初始资金（USDC）'], 'final_money': native['期末资金（USDC）'],
                                  'mdd': native['最大回撤（%）'], 'profit_factor': native['盈亏比（倍）'],
                                  'average_net': native['平均单笔收益率（%）']}
                        no_losses = native['盈亏比（倍）'] == 0 and native['胜率（%）'] == 1
                        output, native = return_candidate_output(source_row, index, native, record, settings, available_years, no_losses)
                        if output['策略指纹'] != selected_row['strategy_fp']:
                            raise ValueError('候选原始行在扫描后发生变化，停止导出')
                        output['导出原因'] = f'期末资金≥全局合格最高资金的{settings["资金保留比例"]:.1%}；按回撤升序、资金降序、PF降序、倍数升序、指纹排序'
                        retained[rank] = (output, native)
                    if not by_source: break
            current_stat = source.stat()
            if by_source or (current_stat.st_size, current_stat.st_mtime_ns) != source_identity:
                raise ValueError('候选扫描期间原CSV已变化，停止导出')
            for rank in range(len(selected)):
                output, native = retained[rank]
                research.append(output); natives.append(native)
        append_candidate_details(research, natives, source.parent, run_context)
        counts['每类限额后研究数'] = len(research)
        watch = [dict(row) for row in research[:settings['全局观察最多']]]
        counts['全局观察数'] = len(watch)
        counts['实盘候选数'] = 0
        for rank, row in enumerate(watch, 1): row['导出原因'] = f'全局观察第{rank}名；' + row['导出原因']
        payload = {'生成时间': time.strftime('%Y-%m-%d %H:%M:%S'), '源文件': str(source), '设置': settings,
                   '总行数': counts['原始CSV行数'], '初筛通过行数': counts['硬门槛通过账户数'], '研究候选数': len(research),
                   '观察候选数': len(watch), '实盘候选数': 0, '可用年份': sorted(available_years),
                   '表头': 输出表头, '研究候选': research, '观察候选': watch, '实盘候选': [],
                   '阶段数量': dict(counts), '全局合格最高期末资金（USDC）': best, '资金保留门槛（USDC）': threshold,
                   '筛选阶段统计': dict(counts),
                   '淘汰统计': [{'淘汰原因': reason, '涉及行数': count} for reason, count in rejected.most_common()],
                   '高级待验证项': list(高级待验证项),
                   '研究预筛分说明': '本方案不计算加权分或类别百分位；全局收益门槛后按真实回撤/资金/PF/倍数/指纹确定性排序。不足不补。',
                   '阶段说明': '先核验逐账户数据与同一运行上下文，再进行硬门槛、相同指纹去重、全局资金比例门槛、每类限额及全局观察限额；统计以实际账户而非CSV行计。',
                   '成交成本说明': '全部实际资金与净收益沿用原回放，不重新加减成本。压力列只提示；不是实盘验证。旧目标杠杆列兼容保留，其值等于该行实际回放倍数。',
                   '容量口径说明': '持仓及原等待占比仅提示，不作为收益/回撤方案硬门槛。'}
        tag = '全部实际倍数' if settings['比较范围'] == 'ALL_RUN' else f'{settings["统一目标杠杆"]:g}x'
        payload_path = output_dir / f'分层候选数据_{tag}.json'
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), 'utf-8')
        write_native_catalog(output_dir, source, natives)
        write_csv(output_dir / f'每类研究候选_{tag}.csv', research)
        write_csv(output_dir / f'全局观察候选_{tag}.csv', watch)
        write_csv(output_dir / f'实盘候选_{tag}.csv', [])
        output_path = export_excel(project_dir, payload_path, output_dir / f'分层候选分析_{tag}.xlsx')
        emit('candidate_completed', rows=counts['原始CSV行数'], eligible=counts['硬门槛通过账户数'], research=len(research), watch=len(watch), live=0, path=str(output_path))
    finally:
        conn.close()
        if db_path.exists(): db_path.unlink()


def main():
    parser = argparse.ArgumentParser(description="从完整CSV生成分层研究候选")
    parser.add_argument("--output", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--source", default="", help="实际选中的历史结果CSV；默认输出目录中的全部回测结果.csv")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    output_dir = Path(args.output).resolve()
    source = Path(args.source).resolve() if args.source else output_dir / "全部回测结果.csv"
    dictionary_path = source.parent / "止盈方案字典.csv"
    if not source.is_file(): raise FileNotFoundError(source)
    if not dictionary_path.is_file(): raise FileNotFoundError(dictionary_path)
    settings = 规范化候选筛选(json.loads(Path(args.settings).read_text("utf-8-sig")))
    tp_lookup = read_tp_dictionary(dictionary_path)
    if settings['筛选方案'] == 'RETURN_DRAWDOWN':
        return run_return_drawdown(project_dir, output_dir, source, settings, tp_lookup)
    days, available_years = read_period_info(source.parent)
    # SQLite窗口排序和Node导出都可能创建大量临时文件。强制放到结果目录所在磁盘，
    # 避免系统盘空间不足时出现“database or disk is full”。
    runtime_temp = output_dir / "缓存" / "候选临时文件"
    runtime_temp.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(runtime_temp)
    os.environ["TMP"] = str(runtime_temp)
    stop_flag = output_dir / "控制" / "停止候选导出.flag"
    stop_flag.parent.mkdir(exist_ok=True)

    db_path = output_dir / "缓存" / "候选筛选临时.sqlite"
    db_path.parent.mkdir(exist_ok=True)
    if db_path.exists(): db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if source.stat().st_size <= 2 * 1024 ** 3:
        conn.execute("PRAGMA temp_store=MEMORY")
    else:
        conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-262144")
    conn.execute("""
        CREATE TABLE eligible (
            id INTEGER PRIMARY KEY, category TEXT, tp_id INTEGER, cooldown INTEGER, base_id INTEGER,
            macd INTEGER, c4 INTEGER, c1 INTEGER, c15 INTEGER, c5 INTEGER, c1m INTEGER,
            entry_mode TEXT, engine_version TEXT, stop_code TEXT,
            leverage REAL, trades INTEGER, trades_per_day REAL, win_rate REAL, long_share REAL, holding REAL,
            fixed_stop_code TEXT, overlay_code TEXT, direction TEXT, session TEXT, hard_minutes INTEGER,
            capacity REAL, profit_factor REAL, naive_t REAL, final_money REAL, mdd REAL, liquidations INTEGER,
            forced_liquidations INTEGER,
            executed INTEGER, capital_stop INTEGER, end_quantity REAL, initial_capital REAL,
            minimum_order_eth REAL, maximum_order_eth REAL, minimum_order_verified INTEGER,
            y2025 REAL, y2026 REAL, worst_year REAL, gross_before_cost REAL, average_cost REAL, average_net REAL,
            p95_cost REAL, p95_net REAL, extreme_cost REAL, extreme_net REAL, cost_ratio REAL,
            preferred_mdd REAL, strategy_fp TEXT, cluster_fp TEXT, capacity_basis TEXT, execution_json TEXT, native_json TEXT
        )
    """)
    insert_sql = "INSERT INTO eligible VALUES (" + ",".join("?" for _ in range(56)) + ")"
    batch = []
    total = eligible_count = missing_leverage = 0
    reject_counts = Counter()
    file_size = max(1, source.stat().st_size)

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_num, (source_number, row, size_index, leverage) in enumerate(account_comparisons(reader, settings), 1):
            total = source_number
            if (row_num == 1 or row_num % 1000 == 0) and stop_flag.exists():
                conn.close(); emit("candidate_stopped", rows=total); return
            if size_index is None:
                missing_leverage += 1
                continue
            if (settings['比较范围'] == 'ALL_RUN' and len(row['所选仓位顺序'].split(';')) > 1
                    and not row.get(ACCOUNT_FIELD)):
                reject_counts['ALL_RUN多倍数缺少逐账户交易统计，不能套用首档统计'] += 1
                continue
            source_row = row
            row = account_row(row, size_index)
            finals = split_values(row["所选仓位期末资金（USDC）"])
            mdds = split_values(row["所选仓位最大回撤（%）"])
            liquidations = split_values(row["所选仓位爆仓保护次数（次）"], int)
            forced_raw = row.get("所选仓位全仓强平次数（次）", "").strip()
            forced_values = split_values(forced_raw, int) if forced_raw else []
            if size_index >= min(len(finals), len(mdds), len(liquidations)):
                missing_leverage += 1
                continue
            fixed_stop_code = (row.get("固定止损代码") or "OFF").strip() or "OFF"
            overlay_code = (row.get("叠加止盈代码") or "OFF").strip() or "OFF"
            direction = (row.get("开仓方向") or "BOTH").strip() or "BOTH"
            session = (row.get("交易会话") or "ALL").strip() or "ALL"
            hard_minutes = int(row.get("强制时间止损（分钟）") or 0)
            entry_mode = (row.get("入场触发口径") or "LEGACY_UNKNOWN").strip() or "LEGACY_UNKNOWN"
            position = position_values(row)
            engine_version = (row.get("回测计算版本") or "旧版（未逐仓重放）").strip()
            tp_id = int(row["止盈方案编号"]); tp = tp_lookup.get(tp_id, {})
            category = tp.get("止盈类别", "未知止盈")
            cooldown = int(row["止盈后等待分钟"]); trades = int(row["交易次数（单）"])
            order_data_present = (
                bool(row.get("所选仓位资金性停机标记（0否1是）", "").strip())
                and bool(row.get("ETH单次最大开仓数量（ETH）", "").strip())
            )
            if order_data_present:
                executed_values = split_values(row["所选仓位实际成交次数（单）"], int)
                capital_stop_values = split_values(row["所选仓位资金性停机标记（0否1是）"], int)
                end_quantity_values = split_values(row["所选仓位期末可开仓数量（ETH）"])
                if size_index >= min(len(executed_values), len(capital_stop_values), len(end_quantity_values)):
                    order_data_present = False
            minimum_order_verified = order_data_present and minimum_order_was_enabled(row)
            initial_capital = safe_float(row.get("初始资金（USDC）"), 100.0)
            minimum_order_eth = safe_float(row.get("ETH最小开仓数量（ETH）"), 0.0)
            maximum_order_eth = safe_float(row.get("ETH单次最大开仓数量（ETH）"), 100.0)
            if order_data_present:
                executed = executed_values[size_index]
                capital_stop = capital_stop_values[size_index]
                end_quantity = end_quantity_values[size_index]
            else:
                executed = trades
                capital_stop = 0
                end_quantity = 0.0
            trades_per_day = completed_trades_per_day(row, category, days)
            capacity, capacity_basis = capacity_for_row(row, trades_per_day, cooldown)
            average_net = safe_float(row["平均单笔收益率（%）"])
            average_cost = safe_float(row.get("往返成交偏移（%）"))
            costs = cost_metrics(average_net, average_cost, settings["p95往返偏移"], settings["极端往返偏移"])
            execution = execution_values(row)
            year_values = {2025: safe_float(row.get("2025毛收益（%）")), 2026: safe_float(row.get("2026毛收益（%）"))}
            available_values = [year_values[year] for year in (2025, 2026) if year in available_years]
            worst_year = min(available_values) if available_values else min(year_values.values())
            record = {
                "trades": trades, "long_share": safe_float(row["多单占比（%）"]),
                "holding": safe_float(row["平均持仓时间（分钟）"]),
                "capacity": capacity,
                "profit_factor": safe_float(row["盈亏比（倍）"]), "mdd": mdds[size_index],
                "liquidations": liquidations[size_index],
                "forced_liquidations": (forced_values[size_index]
                                        if size_index < len(forced_values) else 0),
                "y2025": year_values[2025], "y2026": year_values[2026],
                "minimum_order_verified": minimum_order_verified, "capital_stop": capital_stop,
                **costs,
            }
            failures = preliminary_failures(record, settings, available_years)
            if failures:
                reject_counts.update(failures)
            else:
                execution_parts = [entry_mode, engine_version, row["止损代码"], fixed_stop_code,
                                   overlay_code, direction, session, hard_minutes, *execution.values(),
                                   position["开仓位置过滤代码"]]
                native = native_rank_row(source_row, size_index, tp, days)
                cluster_parts = [category, tp.get("周期组合", ""), tp.get("指标", ""), row["基础策略编号"],
                                 row["开仓MACD代码"], row["4小时条件代码"], row["1小时条件代码"],
                                 row["15分钟条件代码"], row["5分钟条件代码"], row["1分钟条件代码"],
                                 *execution_parts, leverage]
                if tp_id < 0:
                    cluster_parts.append(tp_id)
                eligible_count += 1
                batch.append((
                    eligible_count, category, tp_id, cooldown, int(row["基础策略编号"]), int(row["开仓MACD代码"]),
                    int(row["4小时条件代码"]), int(row["1小时条件代码"]), int(row["15分钟条件代码"]),
                    int(row["5分钟条件代码"]), int(row["1分钟条件代码"]), entry_mode, engine_version,
                    row["止损代码"], leverage,
                    trades, trades_per_day, safe_float(row["胜率（%）"]), record["long_share"], record["holding"],
                    fixed_stop_code, overlay_code, direction, session, hard_minutes,
                    record["capacity"], record["profit_factor"], safe_float(row["t值"]), finals[size_index], record["mdd"],
                    record["liquidations"], record["forced_liquidations"],
                    executed, capital_stop, end_quantity, initial_capital,
                    minimum_order_eth, maximum_order_eth, int(minimum_order_verified),
                    record["y2025"], record["y2026"], worst_year, costs["gross_before_cost"],
                    average_cost, average_net, settings["p95往返偏移"], costs["p95_net"], settings["极端往返偏移"],
                    costs["extreme_net"], costs["cost_ratio"], settings["最大回撤优选"], config_fingerprint(native),
                    fingerprint(cluster_parts), capacity_basis,
                    json.dumps({**execution, **position}, ensure_ascii=False),
                    json.dumps(native, ensure_ascii=False),
                ))
            if len(batch) >= 10_000:
                conn.executemany(insert_sql, batch); conn.commit(); batch.clear()
            if row_num % 100_000 == 0:
                try: position = handle.buffer.tell()
                except OSError: position = 0
                emit("candidate_progress", rows=row_num, eligible=eligible_count,
                     percent=min(99.0, position / file_size * 100.0))
                if stop_flag.exists():
                    if batch: conn.executemany(insert_sql, batch); conn.commit()
                    conn.close(); emit("candidate_stopped", rows=row_num); return
    if batch: conn.executemany(insert_sql, batch); conn.commit()

    conn.row_factory = sqlite3.Row
    conn.execute("CREATE INDEX idx_eligible_category ON eligible(category)")
    conn.execute("""
        CREATE TABLE scored AS
        WITH distinct_strategies AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY strategy_fp ORDER BY id) AS duplicate_rank
            FROM eligible
        ), ranked AS (
            SELECT *,
                CASE WHEN COUNT(*) OVER (PARTITION BY category)=1 THEN 0.5
                     ELSE PERCENT_RANK() OVER (PARTITION BY category ORDER BY mdd DESC) END AS p_mdd,
                CASE WHEN COUNT(*) OVER (PARTITION BY category)=1 THEN 0.5
                     ELSE PERCENT_RANK() OVER (PARTITION BY category ORDER BY p95_net) END AS p_p95,
                CASE WHEN COUNT(*) OVER (PARTITION BY category)=1 THEN 0.5
                     ELSE PERCENT_RANK() OVER (PARTITION BY category ORDER BY worst_year) END AS p_worst_year,
                CASE WHEN COUNT(*) OVER (PARTITION BY category)=1 THEN 0.5
                     ELSE PERCENT_RANK() OVER (PARTITION BY category ORDER BY capacity DESC) END AS p_capacity,
                CASE WHEN COUNT(*) OVER (PARTITION BY category)=1 THEN 0.5
                     ELSE PERCENT_RANK() OVER (PARTITION BY category ORDER BY profit_factor) END AS p_pf
            FROM distinct_strategies WHERE duplicate_rank=1
        )
        SELECT *, 100.0 * (
            0.35 * ((MIN(0.975, MAX(0.025, p_mdd)) - 0.025) / 0.95) +
            0.25 * ((MIN(0.975, MAX(0.025, p_p95)) - 0.025) / 0.95) +
            0.20 * ((MIN(0.975, MAX(0.025, p_worst_year)) - 0.025) / 0.95) +
            0.10 * ((MIN(0.975, MAX(0.025, p_capacity)) - 0.025) / 0.95) +
            0.10 * ((MIN(0.975, MAX(0.025, p_pf)) - 0.025) / 0.95)
        ) AS score
        FROM ranked
    """)
    conn.execute("CREATE INDEX idx_scored_category ON scored(category)")
    categories = [row[0] for row in conn.execute("SELECT DISTINCT category FROM scored ORDER BY category")]
    research = []
    for category in categories:
        selected: dict[int, tuple[sqlite3.Row, set[str]]] = {}
        queries = [
            ("综合预筛分前20", "score DESC, mdd ASC, p95_net DESC", 20),
            ("最大回撤最低前10", "mdd ASC, score DESC", 10),
            ("p95成本后收益前10", "p95_net DESC, score DESC", 10),
        ]
        for label, order, limit in queries:
            for row in conn.execute(f"SELECT * FROM scored WHERE category=? ORDER BY {order} LIMIT ?", (category, limit)):
                if row["id"] not in selected: selected[row["id"]] = (row, set())
                selected[row["id"]][1].add(label)
        clusters = defaultdict(list)
        for row, reasons in selected.values(): clusters[row["cluster_fp"]].append((row, reasons))
        retained = []
        for items in clusters.values():
            best = max(items, key=lambda item: (item[0]["score"], -item[0]["mdd"], item[0]["p95_net"]))
            retained.append(best)
            remaining = [item for item in items if item[0]["id"] != best[0]["id"]]
            if remaining:
                retained.append(min(remaining, key=lambda item: (item[0]["mdd"], -item[0]["p95_net"], -item[0]["score"])))
        retained.sort(key=lambda item: (-item[0]["score"], item[0]["mdd"], -item[0]["p95_net"]))
        for row, reasons in retained[:settings["每类最多"]]:
            research.append(row_to_output(row, tp_lookup.get(row["tp_id"], {}), "；".join(sorted(reasons))))

    research.sort(key=lambda row: (-safe_float(row["研究预筛分"]), safe_float(row["目标杠杆最大回撤（%）"])))
    natives = [json.loads(conn.execute('SELECT native_json FROM scored WHERE strategy_fp=?',
                                      (row['策略指纹'],)).fetchone()[0]) for row in research]
    append_candidate_details(research, natives, source.parent)
    watch = [dict(row) for row in research[:settings["全局观察最多"]]]
    for rank, row in enumerate(watch, 1): row["导出原因"] = f"全局观察候选第{rank}名；" + row["导出原因"]
    live = []
    rejection_rows = [{"淘汰原因": reason, "涉及行数": count} for reason, count in reject_counts.most_common()]
    if missing_leverage:
        rejection_rows.append({"淘汰原因": "完整CSV不包含统一目标杠杆", "涉及行数": missing_leverage})
    rejection_rows.append({"淘汰原因": "实盘候选为空：高级验证数据尚未生成", "涉及行数": len(research)})

    payload = {
        "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"), "源文件": str(source), "设置": settings,
        "总行数": total, "初筛通过行数": eligible_count, "研究候选数": len(research),
        "观察候选数": len(watch), "实盘候选数": 0, "可用年份": sorted(available_years),
        "表头": 输出表头, "研究候选": research, "观察候选": watch, "实盘候选": live,
        "淘汰统计": rejection_rows, "高级待验证项": list(高级待验证项),
        "研究预筛分说明": "同止盈类别内2.5%/97.5%截尾百分位：35%低回撤+25%高p95成本后收益+20%最差年度+10%低容量占用+10%盈亏比。该分数不是用户完整综合评分。",
        "筛选阶段统计": {"原始CSV行数": total, "硬门槛通过账户数": eligible_count,
                         "去重合格账户数": conn.execute('SELECT COUNT(*) FROM scored').fetchone()[0],
                         "每类限额后研究数": len(research), "全局观察数": len(watch), "实盘候选数": 0},
        "成交成本说明": "期末资金、平均收益与胜率沿用原CSV逐账户结果，已含该次回测手续费及开仓间隔。新版原始回测按成本模式二选一：SLIPPAGE只扣偏移，FEE只扣手续费；缺字段的旧CSV费用/间隔按0显示，旧双成本结果标LEGACY_COMBINED。p95/极端列另作偏移压力假设，保留已扣手续费，不是二选一回测结果或逐笔重放，不能据此声称实盘收益。不会用当前UI参数改写旧结果。",
        "容量口径说明": "容量占用率沿用持仓时间加原止盈等待口径，不含新增全退出开仓间隔，不代表盘口深度或市场承载量。",
        "资料来源": [
            "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551",
            "https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253",
            "https://doi.org/10.1111/1468-0262.00152",
            "https://www.nber.org/papers/t0055",
        ],
    }
    leverage_tag = '全部实际倍数' if settings['比较范围'] == 'ALL_RUN' else f"{settings['统一目标杠杆']:g}x"
    payload_path = output_dir / f"分层候选数据_{leverage_tag}.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    write_native_catalog(output_dir, source, natives)
    write_csv(output_dir / f"每类研究候选_{leverage_tag}.csv", research)
    write_csv(output_dir / f"全局观察候选_{leverage_tag}.csv", watch)
    write_csv(output_dir / f"实盘候选_{leverage_tag}.csv", live)
    output_path = output_dir / f"分层候选分析_{leverage_tag}.xlsx"
    output_path = export_excel(project_dir, payload_path, output_path)
    conn.close()
    try: db_path.unlink()
    except OSError: pass
    emit("candidate_completed", rows=total, eligible=eligible_count, research=len(research),
         watch=len(watch), live=0, path=str(output_path))


if __name__ == "__main__":
    main()
