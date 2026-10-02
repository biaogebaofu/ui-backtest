from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import shutil
import subprocess
import sys
import time
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import islice, product
from pathlib import Path
from threading import Lock

import numpy as np

from feature_builder import build_features, 特征版本
from data_sources import source_bundle_fingerprint
from export_runtime import export_xlsx_atomic
from selection_config import 排行指标选项, 默认排行指标, 全选配置, 配置签名, 配置统计, 规范化配置, 入场口径列表
from strategy_space import 生成止盈方案, 仓位列表, 补全固定比例档
from run_safety import atomic_json, build_run_identity, verify_run_identity, exclusive_output
from account_statistics import ACCOUNT_FIELD, ENGINE_VERSION, encode_accounts, CAPACITY_TIMESTAMP, capacity_timestamp
from execution_settings import cost_modes, effective_fee_rates, effective_slippage
from extended_rules import ENTRY_RULES, ENTRY_RULE_BATCH, stable_base_id, composite_stop_index
from indicator_combinations import (DEFAULT_REGISTRY, choices_for_config, combination_label,
                                    tp_spec, stop_spec)
from entry_position import position_filter_label
from ranking_limits import top_limit


执行成本表头 = [
    "平仓后最小开仓间隔（分钟）", "开仓基础手续费率（%）", "平仓基础手续费率（%）",
    "BNB手续费抵扣（0否1是）", "手续费返佣比例（%）",
    "开仓净手续费率（%）", "平仓净手续费率（%）", "成本模式",
]
开仓位置表头 = ["开仓位置过滤代码", "开仓位置过滤说明"]


中文表头 = [
    "止盈方案编号", "止盈类别", "止盈周期组合", "止盈指标", "止盈参数一", "止盈参数二", "止盈参数三",
    "止盈后等待分钟", "基础策略编号", "开仓MACD口径", "4小时条件", "1小时条件", "15分钟条件", "5分钟条件", "1分钟条件", "入场触发口径",
    "止损代码", "止损说明", "固定止损代码", "固定止损说明", "叠加止盈代码", "叠加止盈说明",
    "开仓方向", "交易会话", "强制时间止损（分钟）", "仓位标签", "名义倍数（倍）", "交易次数（单）", "胜率（%）", "多单占比（%）",
    "平均日完整交易数（次/日）", "平均日成交订单数（笔/日）",
    "平均持仓时间（分钟）", "平均单笔收益率（%）", "毛收益合计（%）", "盈亏比（倍）", "t值", "期末资金（USDC）", "累计收益率（%）",
    "最大回撤（%）", "爆仓保护次数（次）", "全仓强平次数（次）", "2025毛收益（%）", "2026毛收益（%）",
    "开仓成交偏移（%）", "平仓成交偏移（%）", "往返成交偏移（%）",
    "回测计算版本", "初始资金（USDC）", "ETH最小开仓数量（ETH）", "ETH单次最大开仓数量（ETH）", "实际成交次数（单）",
    "资金性停机标记（0否1是）", "期末可开仓数量（ETH）",
    "实际止盈等待总时间（分钟）", "实际容量占用率（%）",
    "ETH最小开仓约束启用（0否1是）", "成交价格口径",
] + 执行成本表头 + 开仓位置表头 + [CAPACITY_TIMESTAMP]

全量CSV表头 = [
    "止盈方案编号", "止盈后等待分钟", "基础策略编号", "开仓MACD代码", "4小时条件代码", "1小时条件代码", "15分钟条件代码",
    "5分钟条件代码", "1分钟条件代码", "入场触发口径", "止损代码", "固定止损代码", "叠加止盈代码",
    "开仓方向", "交易会话", "强制时间止损（分钟）", "交易次数（单）", "胜率（%）", "多单占比（%）",
    "平均日完整交易数（次/日）", "平均日成交订单数（笔/日）",
    "平均持仓时间（分钟）", "平均单笔收益率（%）", "毛收益合计（%）", "盈亏比（倍）", "t值", "2025毛收益（%）", "2026毛收益（%）",
    "所选仓位顺序", "所选仓位期末资金（USDC）", "所选仓位累计收益率（%）", "所选仓位最大回撤（%）", "所选仓位爆仓保护次数（次）",
    "所选仓位全仓强平次数（次）",
    "开仓成交偏移（%）", "平仓成交偏移（%）", "往返成交偏移（%）",
    "回测计算版本", ACCOUNT_FIELD,
    "初始资金（USDC）", "ETH最小开仓数量（ETH）", "ETH单次最大开仓数量（ETH）", "所选仓位实际成交次数（单）",
    "所选仓位资金性停机标记（0否1是）", "所选仓位期末可开仓数量（ETH）",
    "实际止盈等待总时间（分钟）", "实际容量占用率（%）",
    "ETH最小开仓约束启用（0否1是）", "成交价格口径",
] + 执行成本表头 + 开仓位置表头

排行期末资金列 = 中文表头.index("期末资金（USDC）")
_排行状态 = {"列": 排行期末资金列, "越大越好": True, "名称": 默认排行指标}
_排行门槛 = []


def 从命令行读取排行指标(argv):
    """--rank-metric 名称。名字非法就退回期末资金，并打印一行说明。"""
    if "--rank-metric" in argv:
        i = argv.index("--rank-metric")
        if i + 1 < len(argv):
            name = argv[i + 1]
            got = 设置排行指标(name)
            if got != name:
                print("[排行] 未知指标 %r，改用 %s" % (name, got), flush=True)
            else:
                print("[排行] 按 %s 取最优" % got, flush=True)
            del argv[i:i + 2]
    return _排行状态["名称"]


def 从命令行读取排行设置(argv):
    """读取UI写出的主排行指标和多条最低/最高门槛。"""
    if "--ranking-settings" not in argv:
        return 从命令行读取排行指标(argv)
    i = argv.index("--ranking-settings")
    if i + 1 >= len(argv):
        raise ValueError("--ranking-settings 后缺少JSON路径")
    path = Path(argv[i + 1])
    应用排行设置(json.loads(path.read_text("utf-8-sig")))
    del argv[i:i + 2]
    print(f"[排行] 按 {_排行状态['名称']} 取最优；组合门槛{len(_排行门槛)}条", flush=True)
    # 兼容旧UI同时传来的 --rank-metric，设置文件优先。
    if "--rank-metric" in argv:
        j = argv.index("--rank-metric")
        del argv[j:j + 2]
    return _排行状态["名称"]


def 设置排行指标(name):
    """切换排行依据。名字非法就退回期末资金，绝不静默按错的列排。"""
    col, bigger = 排行指标选项.get(name, 排行指标选项[默认排行指标])
    _排行状态["列"] = 中文表头.index(col)
    _排行状态["越大越好"] = bigger
    _排行状态["名称"] = name if name in 排行指标选项 else 默认排行指标
    return _排行状态["名称"]


def 应用排行设置(raw: dict | None) -> dict:
    global 最优排行名额, _最优名额显式
    source = raw or {}
    limit = top_limit(source)
    设置排行指标(str(source.get("排行指标") or 默认排行指标))
    normalized = []
    for item in source.get("门槛", []):
        name = str(item.get("指标") or "")
        condition = str(item.get("条件") or "")
        if name not in 排行指标选项 or condition not in ("最低值", "最高值"):
            raise ValueError(f"排行榜门槛无效：{item}")
        column_name = 排行指标选项[name][0]
        normalized.append({
            "指标": name,
            "列": 中文表头.index(column_name),
            "条件": condition,
            "值": float(item["值"]),
        })
    _排行门槛[:] = normalized
    最优排行名额 = limit
    _最优名额显式 = "最优名额" in source
    return 当前排行设置()


def 当前排行设置() -> dict:
    settings = {"排行指标": _排行状态["名称"], "门槛": [
        {"指标": x["指标"], "条件": x["条件"], "值": x["值"]} for x in _排行门槛
    ]}
    if _最优名额显式:
        settings["最优名额"] = 最优排行名额
    return settings


def 排行行合格(row) -> bool:
    for item in _排行门槛:
        try:
            value = float(row[item["列"]])
        except (TypeError, ValueError, IndexError):
            return False
        if not math.isfinite(value):
            return False
        if item["条件"] == "最低值" and value < item["值"]:
            return False
        if item["条件"] == "最高值" and value > item["值"]:
            return False
    return True


def 排行键(row):
    """统一的排序键：一律"越大越优"，越小越好的指标取负。"""
    try:
        v = float(row[_排行状态["列"]])
    except (TypeError, ValueError, IndexError):
        return float("-inf")
    if not math.isfinite(v):
        return float("-inf")
    return v if _排行状态["越大越好"] else -v


def 排行数值有效(row) -> bool:
    """Validate ranking/account keys, not unrelated metrics or user gates."""
    try:
        return (math.isfinite(float(row[_排行状态["列"]]))
                and math.isfinite(float(row[排行期末资金列]))
                and math.isfinite(float(row[排行最大回撤列]))
                and math.isfinite(float(row[排行爆仓次数列])))
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


排行最大回撤列 = 中文表头.index("最大回撤（%）")
排行爆仓次数列 = 中文表头.index("爆仓保护次数（次）")
最优排行名额 = 5000
_最优名额显式 = False
最差排行名额 = 1000
WORST_SCOPE = "ALL_VALID_COMPLETED_V1"
WORST_SCOPE_LABEL = "全部数值有效的已完成账户结果，不应用最优优选门槛；包含资金停机和归零账户"
GPU一次载入止损上限 = 32
# 止损套数不多时，把展开好的止损数组常驻内存，别每个止盈方案重建一次。
# 一套是 6 条 N 长度的 8 字节数组，N=86万时约 40MB，8套约 320MB。
CPU常驻止损上限 = 8
# 断点和排行JSON的落盘间隔。排行满员时一次要写约250MB，实测3.4秒，
# 而一个止盈方案真正的模拟只有3毫秒，逐个方案落盘等于全程在写盘。
断点保存间隔秒 = 60.0
# 一批开仓信号常驻的入场数组预算。以前写死64MB：位置过滤勾选5档时
# 每组入场约 B.N*9*5≈39MB，算下来每批只剩1组，一批只有5个并行任务，
# 18个线程里13个空转，而每批之前还要串行跑约122毫秒的 build_field。
# i7-12700KF + N=865440 实测（同一份5档位置过滤的勾选，18线程）：
#   每批1组 30.8组/秒｜4组 51.0｜8组 87.1｜16组 93.4｜32组 102.9（约3.3倍）
# 预算必须是定值：它决定分批边界，也就决定CSV行顺序和断点续跑位置，
# 不能跟着当时的可用内存变。要按旧口径复现，设 BT_ENTRY_BATCH_MB=64。
入场批预算MB = 1024


def 入场批预算字节() -> int:
    """开仓批入场数组的内存预算。定值；可用 BT_ENTRY_BATCH_MB 覆盖。"""
    override = os.environ.get("BT_ENTRY_BATCH_MB")
    if override:
        try:
            return max(1, int(override)) * 1024 ** 2
        except ValueError:
            pass
    return 入场批预算MB * 1024 ** 2


def 选择计算设备(requested: str, gpu_ok: bool) -> bool:
    """读取GPU请求；是否能执行仍由当前逐账户回放口径的兼容检查决定。"""
    if not gpu_ok or requested != "gpu":
        return False
    return True


def 分块(items, size: int):
    """惰性读取任务，防止ThreadPoolExecutor一次提交数十万项。"""
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, max(1, int(size))))
        if not batch:
            return
        yield batch


def CPU分组上限(task_count, threads, stop_count):
    """少量常驻止损拆小组并行回放，其余配置保留64条以复用止损准备。"""
    if stop_count > CPU常驻止损上限 or stop_count >= max(1, threads):
        return 64
    return max(1, min(64, math.ceil(task_count / max(1, threads))))


class 停止检查:
    """共享停止状态；最多每0.1秒读一次文件，检测到后保持停止。"""
    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock or time.monotonic
        self.stopped = False
        self.next_check = 0.0
        self.lock = Lock()

    def __call__(self):
        if self.stopped:
            return True
        now = self.clock()
        if now < self.next_check:
            return False
        with self.lock:
            if self.stopped:
                return True
            now = self.clock()
            if now >= self.next_check:
                self.stopped = self.path.exists()
                self.next_check = now + 0.1
            return self.stopped


def 有界顺序结果(pool, evaluate, tasks, max_pending, stop_requested):
    """跨小组预取有限任务，仍按提交顺序返回，保持CSV和并列排行顺序。"""
    source = iter(tasks)
    pending = deque()
    exhausted = False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < max(1, max_pending):
                if stop_requested():
                    return
                try:
                    task = next(source)
                except StopIteration:
                    exhausted = True
                    break
                if stop_requested():
                    return
                pending.append(pool.submit(evaluate, task))
            if pending:
                yield pending.popleft().result()
    finally:
        for future in pending:
            future.cancel()
        close = getattr(source, "close", None)
        if close is not None:
            close()


def GPU任务索引(cases, stop_code, entry_positions, stop_positions, stop_count: int) -> int:
    """GpuField内核的固定布局：入场为外层、止损为内层。"""
    return entry_positions[cases] * stop_count + stop_positions[stop_code]


def 开仓批次(fields, options, size):
    """只生成当前批次的条件元组，不展开全部条件的行情信号。"""
    def lazy_product(position=0, prefix=()):
        if position == len(options):
            yield prefix
            return
        for value in options[position]:
            yield from lazy_product(position + 1, (*prefix, value))
    for field in fields:
        for cases in 分块(lazy_product(), size):
            yield field, tuple(cases)


def 共享缓存目录(cache_root: str, csv_path: str, micro_path: str | None, funding_path: str | None,
           oi_path: str | None, bundle_path: str | None, start: str | None, end: str | None) -> Path:
    """同一组数据源和日期范围共用缓存。任一补充源变化都会换缓存。"""
    sources = {
        "kline": csv_path or "", "micro": micro_path or "", "funding": funding_path or "",
        "oi": oi_path or "", "bundle": bundle_path or "",
    }
    request = {
        "fingerprint": source_bundle_fingerprint(sources),
        "start": start or "", "end": end or "", "feature_version": 特征版本,
    }
    key = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return Path(cache_root).resolve() / key


def emit(kind: str, **data):
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def size_label(value: float) -> str:
    if value <= 1.0:
        return f"{round(value * 10):d}/10"
    return f"{value:g}x"


def case_label(value: int) -> str:
    if value < 0:
        return combination_label("entry", value)
    if value in ENTRY_RULES:
        return ENTRY_RULES[value][0]
    return {
        0: "不启用",
        1: "情况一",
        2: "情况二",
        3: "情况三（反转量能充足）",
        4: "情况四（量能不足时反向）",
    }[value]


def _sequence_count(values):
    count = getattr(values, "count", None)
    return count if isinstance(count, int) else len(values)


class LazyTakeProfits:
    """随机访问本次止盈选池，不提前生成组合对象或登记整个组合空间。"""
    def __init__(self, choices, limit=0):
        self.choices = choices
        self.count = min(choices.count, limit) if limit > 0 else choices.count

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.count))]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        return tp_spec(self.choices[index])

    def __iter__(self):
        for index in range(self.count):
            yield self[index]


class LazyStops:
    def __init__(self, choices):
        self.choices = choices
        self.count = choices.count

    def __len__(self):
        return self.count

    def __iter__(self):
        for code in self.choices:
            _, _, label = stop_spec(code)
            yield code, label, None


class LazyRowCounts:
    def __init__(self, tps, regular, special):
        self.count = tps.count
        self.special_indices = frozenset(i for i, code in enumerate(tps.choices.singles)
                                        if i < self.count and tp_spec(code).类别 in ("分批止盈", "盈亏比止盈"))
        self.regular = regular
        self.special = special
        self.total = (self.count - len(self.special_indices)) * regular + len(self.special_indices) * special
        self.maximum = max(regular if self.count > len(self.special_indices) else 0,
                           special if self.special_indices else 0)

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if not 0 <= index < self.count:
            raise IndexError(index)
        return self.special if index in self.special_indices else self.regular


def write_dictionary(path: Path, tps):
    def write_to(target: Path):
        with target.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["止盈方案编号", "止盈类别", "周期组合", "指标", "参数一（原始小数）", "参数二（原始小数）", "参数三（原始小数）", "完整说明（参数含义及单位）"])
            for tp in tps:
                w.writerow([tp.编号, tp.类别, tp.周期组合, tp.指标, tp.参数一, tp.参数二, tp.参数三, tp.说明])

    try:
        write_to(path)
        return path
    except PermissionError:
        fallback = path.with_name("止盈方案字典_本次.csv")
        try:
            write_to(fallback)
        except PermissionError:
            fallback = path.with_name(f"止盈方案字典_本次_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            write_to(fallback)
        emit("warning", message=f"{path.name}正被Excel或其他程序占用，本次字典已改存为：{fallback.name}；不影响回测。")
        return fallback


def save_json_atomic(path: Path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def load_heaps(raw: dict) -> tuple[dict, int]:
    heaps = {}
    counter = 0
    for category, rows in raw.items():
        heap = []
        for row in rows:
            if not 排行数值有效(row) or not 排行行合格(row):
                continue
            counter += 1
            key = 排行键(row)
            heap.append((key, counter, row))
        if len(heap) > 最优排行名额:
            heap = heapq.nlargest(最优排行名额, heap)
        heapq.heapify(heap)
        heaps[category] = heap
    return heaps, counter


def heaps_to_rows(heaps: dict) -> dict:
    return {k: [x[2] for x in sorted(v, key=lambda item: (item[0], -float(item[2][排行最大回撤列])), reverse=True)]
            for k, v in heaps.items()}


def update_top(heap, row, counter):
    if not 排行数值有效(row) or not 排行行合格(row):
        return
    item = (排行键(row), counter, row)
    if len(heap) < 最优排行名额:
        heapq.heappush(heap, item)
    elif item[0] > heap[0][0]:
        heapq.heapreplace(heap, item)


def load_worst_heaps(raw: dict) -> tuple[dict, int]:
    heaps = {}
    counter = 0
    for category, rows in raw.items():
        heap = []
        for row in rows:
            if not 排行数值有效(row):
                continue
            counter += 1
            heap.append((-排行键(row), counter, row))
        if len(heap) > 最差排行名额:
            heap = heapq.nlargest(最差排行名额, heap)
        heapq.heapify(heap)
        heaps[category] = heap
    return heaps, counter


def worst_heaps_to_rows(heaps: dict) -> dict:
    return {k: [x[2] for x in sorted(v, key=lambda item: (排行键(item[2]),
                                                           -float(item[2][排行最大回撤列]),
                                                           -int(item[2][排行爆仓次数列])))]
            for k, v in heaps.items()}


def update_worst(heap, row, counter):
    if not 排行数值有效(row):
        return
    key = 排行键(row)
    item = (-key, counter, row)
    if len(heap) < 最差排行名额:
        heapq.heappush(heap, item)
    elif key < -heap[0][0]:
        heapq.heapreplace(heap, item)


def find_node() -> str | None:
    candidates = [
        Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe",
        Path(sys.executable).with_name("node.exe"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return shutil.which("node")


def export_excel(project_dir: Path, output_dir: Path, ranking_json: Path, output_name: str):
    node = find_node()
    script = project_dir / "excel_export.mjs"
    try:
        actual = export_xlsx_atomic(node, script, ranking_json, output_dir / output_name, project_dir)
    except RuntimeError as exc:
        emit("warning", message="Excel导出失败，但CSV和JSON完整保留。", detail=str(exc)[-2000:])
        return
    emit("excel_ready", path=str(actual))


def export_candidates(project_dir: Path, output_dir: Path, settings: dict):
    settings_path = output_dir / "候选筛选设置.json"
    save_json_atomic(settings_path, settings)
    script = project_dir / "candidate_export.py"
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(script), "--output", str(output_dir), "--settings", str(settings_path)],
        cwd=project_dir, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip(), flush=True)
    if process.wait() != 0:
        emit("warning", message="分层候选导出失败；完整CSV不受影响，可在UI中从已有CSV重新生成。")


def rebuild_legacy_rankings(project_dir: Path, output_dir: Path, ranking_settings: Path | None = None,
                            json_only: bool = False):
    script = project_dir / "worst_export.py"
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(script), "--output", str(output_dir)]
        + (["--settings", str(ranking_settings)] if ranking_settings else [])
        + (["--json-only"] if json_only else []),
        cwd=project_dir, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip(), flush=True)
    if process.wait() != 0:
        emit("warning", message=f"最优前{最优排行名额}/最差1000导出失败；完整CSV不受影响，可在UI中从已有CSV重新生成。")
        return False
    return True


def ensure_worst_scope(project_dir: Path, output_dir: Path, checkpoint: dict, ranking_settings: Path):
    """Recover filtered legacy heaps from committed CSV without replaying trades."""
    if checkpoint.get("worst_scope") == WORST_SCOPE:
        return
    if int(checkpoint.get("rows", 0)) == 0:
        checkpoint["worst"] = {}
        checkpoint["worst_scope"] = WORST_SCOPE
        return
    source = output_dir / "全部回测结果.csv"
    committed = int(checkpoint.get("csv_bytes", 0))
    if not source.is_file() or not committed or source.stat().st_size != committed:
        raise RuntimeError("旧最差榜范围不完整，且无法核对已保存CSV范围；请从完整CSV重新生成排行榜，无需重跑交易。")
    emit("stage", message="旧最差榜只覆盖门槛内结果；正在从已保存CSV重建全量最差榜，不重新计算交易")
    if not rebuild_legacy_rankings(project_dir, output_dir, ranking_settings, json_only=True):
        raise RuntimeError("旧最差榜自动重建失败；请从已有完整CSV重新生成排行榜后继续，无需重跑交易。")
    rebuilt = json.loads((output_dir / "断点记录.json").read_text("utf-8-sig"))
    if (rebuilt.get("worst_scope") != WORST_SCOPE
            or rebuilt.get("rows") != checkpoint.get("rows")
            or rebuilt.get("csv_bytes") != committed):
        raise RuntimeError("最差榜重建范围未通过核验；请从已有完整CSV重新生成排行榜，无需重跑交易。")
    checkpoint.update(top=rebuilt["top"], worst=rebuilt["worst"], worst_scope=WORST_SCOPE)
    if "top_limit" in rebuilt:
        checkpoint["top_limit"] = rebuilt["top_limit"]


def ensure_top_limit(project_dir: Path, output_dir: Path, checkpoint: dict, ranking_settings: Path):
    """Rebuild committed history when changing the saved leaderboard capacity."""
    previous = top_limit({"最优名额": checkpoint.get("top_limit", 5000)})
    if previous == 最优排行名额:
        return
    if int(checkpoint.get("rows", 0)) == 0:
        checkpoint["top_limit"] = 最优排行名额
        return
    source = output_dir / "全部回测结果.csv"
    committed = int(checkpoint.get("csv_bytes", 0))
    if not source.is_file() or not committed or source.stat().st_size != committed:
        raise RuntimeError("排行榜名额已变化，但无法核对已保存CSV范围；请从完整CSV重建排行榜后继续。")
    emit("stage", message=f"最优榜名额由{previous}调整为{最优排行名额}；正在从已保存CSV重建历史排行，不重新回测")
    if not rebuild_legacy_rankings(project_dir, output_dir, ranking_settings, json_only=True):
        raise RuntimeError("历史排行榜名额重建失败；请从完整CSV重建排行榜后继续，不能只追加后续结果。")
    rebuilt = json.loads((output_dir / "断点记录.json").read_text("utf-8-sig"))
    if (rebuilt.get("top_limit") != 最优排行名额 or rebuilt.get("rows") != checkpoint.get("rows")
            or rebuilt.get("csv_bytes") != committed or rebuilt.get("worst_scope") != WORST_SCOPE):
        raise RuntimeError("历史排行榜重建范围未通过核验；请从完整CSV重建排行榜后继续。")
    checkpoint.update(top=rebuilt["top"], worst=rebuilt["worst"],
                      worst_scope=WORST_SCOPE, top_limit=最优排行名额)


def scan_row_counts(selection, tps, limit_base=0):
    """Count actual CSV tasks, including TP categories that ignore overlays."""
    options = choices_for_config(selection)
    entries = (len(selection["开仓指标"]) * len(selection["开仓方向"])
               * len(selection["交易会话"]) * len(selection["开仓位置过滤"])
               * len(入场口径列表(selection)) * math.prod(
                   options["开仓"][tf].count for tf in ("4h", "1h", "15m", "5m", "1m")))
    base_rows = (entries * options["止损"].count * len(selection["固定止损代码"])
                 * len(selection["强制时间止损分钟"]) * len(selection["止盈后等待分钟"])
                 * len(cost_modes(selection)))
    if isinstance(tps, LazyTakeProfits):
        regular = base_rows * len(selection["叠加止盈代码"])
        return LazyRowCounts(tps, min(regular, limit_base) if limit_base > 0 else regular,
                             min(base_rows, limit_base) if limit_base > 0 else base_rows)
    rows = [base_rows * (1 if tp.类别 in ("分批止盈", "盈亏比止盈")
                         else len(selection["叠加止盈代码"])) for tp in tps]
    return [min(count, limit_base) for count in rows] if limit_base > 0 else rows


class ScanProgress:
    """Estimate remaining work from newly completed rows and active run time."""
    def __init__(self, total_rows, resumed_rows=0, clock=None):
        self.total_rows = total_rows
        self.resumed_rows = resumed_rows
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.paused_at = None
        self.paused_seconds = 0.0
        self.last_emit = None

    def pause(self):
        if self.paused_at is None:
            self.paused_at = self.clock()

    def resume(self):
        if self.paused_at is not None:
            self.paused_seconds += self.clock() - self.paused_at
            self.paused_at = None
            return True
        return False

    def snapshot(self, rows, force=False):
        now = self.clock()
        if not force and self.last_emit is not None and now - self.last_emit < 1.0:
            return None
        self.last_emit = now
        elapsed = max(0.0, (self.paused_at if self.paused_at is not None else now)
                      - self.started - self.paused_seconds)
        new_rows = max(0, rows - self.resumed_rows)
        remaining = max(0, self.total_rows - rows)
        try:
            eta = (0.0 if remaining == 0 else
                   elapsed * (remaining / new_rows) if new_rows and elapsed > 0 else None)
            if eta is not None and not math.isfinite(eta):
                eta = None
        except OverflowError:
            eta = None
        return {"rows": rows, "total_rows": self.total_rows,
                "percent": min(100.0, rows / self.total_rows * 100) if self.total_rows else 100.0,
                "elapsed_seconds": elapsed, "eta_seconds": eta}


def _main():
    parser = argparse.ArgumentParser(description="ETHUSDC本地全组合回测工作进程")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--micro-csv", default="", help="可选：分钟微结构或原始aggTrades CSV/Parquet")
    parser.add_argument("--funding", default="", help="可选：历史资金费率 CSV/Parquet")
    parser.add_argument("--oi", default="", help="可选：历史Open Interest CSV/Parquet")
    parser.add_argument("--bundle", default="", help="可选：含klines/agg_trades/funding/open_interest的数据包ZIP")
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    parser.add_argument("--device", choices=("auto", "gpu", "cpu"), default="auto")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--limit-tp", type=int, default=0, help="测试用；0代表全部")
    parser.add_argument("--limit-base", type=int, default=0, help="测试用；0代表全部")
    parser.add_argument("--tp-ids", default="", help="测试用；逗号分隔的原始止盈编号")
    parser.add_argument("--selection", default="", help="UI生成的组合选择JSON")
    parser.add_argument("--expected-source-fingerprint", default=None,
                        help="精确回测原来源64位SHA256；实际数据不匹配则在交易计算前拒绝")
    parser.add_argument("--cache-root", default="", help="跨结果目录复用的指标缓存根目录")
    args = parser.parse_args()
    expected_source = args.expected_source_fingerprint
    if expected_source is not None:
        if len(expected_source) != 64 or any(char not in '0123456789abcdefABCDEF' for char in expected_source):
            raise ValueError("--expected-source-fingerprint必须是64位十六进制指纹")
        expected_source = expected_source.lower()
    if not 1 <= args.threads <= max(1, os.cpu_count() or 1):
        raise ValueError(f"CPU线程数必须在1到{os.cpu_count() or 1}之间")

    project_dir = Path(__file__).resolve().parent
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_settings_path = output_dir / "排行榜设置.json"
    # CUDA/NVRTC临时文件使用纯英文路径；同时避免系统盘空间不足影响长任务。
    runtime_cache = Path(tempfile.gettempdir()) / "eth_backtest_runtime"
    for name in ("temp", "cupy", "numba"):
        (runtime_cache / name).mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(runtime_cache / "temp")
    os.environ["TMP"] = str(runtime_cache / "temp")
    os.environ["CUPY_CACHE_DIR"] = str(runtime_cache / "cupy")
    os.environ["NUMBA_CACHE_DIR"] = str(runtime_cache / "numba")
    cache_dir = (共享缓存目录(args.cache_root, args.csv, args.micro_csv, args.funding, args.oi, args.bundle, args.start, args.end)
                 if args.cache_root else output_dir / "缓存")
    control_dir = output_dir / "控制"
    control_dir.mkdir(exist_ok=True)
    for flag in (control_dir / "暂停.flag", control_dir / "停止.flag"):
        if flag.exists(): flag.unlink()
    stop_requested = 停止检查(control_dir / "停止.flag")

    raw_selection = 全选配置()
    if args.selection:
        raw_selection = json.loads(Path(args.selection).read_text("utf-8-sig"))
    selection = 规范化配置(raw_selection)
    if not args.restart:
        DEFAULT_REGISTRY.load(output_dir)

    # 永久删除的规则不能被数据能力筛选静默丢弃后继续跑另一套组合。
    from fifth_policy import require_selection_allowed
    require_selection_allowed(selection)

    emit("stage", message="正在检查多数据源共享指标缓存；首次使用该数据组合时才会生成")
    feature_path = build_features(args.csv, str(cache_dir), args.start, args.end, False,
                                  args.micro_csv, args.funding, args.oi, args.bundle)
    # 共享指标缓存不在结果目录内；候选导出仍需要本次实际起止时间。
    feature_meta = json.loads(feature_path.with_name("features_meta.json").read_text("utf-8"))
    if expected_source is not None and feature_meta.get("request", {}).get("fingerprint") != expected_source:
        raise RuntimeError("原来源数据指纹已变化，精确回测已在交易计算前拒绝；未生成交易结果CSV，请重新核验来源")
    from selection_availability import prune_unavailable_selection
    availability = prune_unavailable_selection(selection, feature_meta.get("capabilities", {}),
        feature_meta.get("capabilities_by_timeframe", {}), exact=expected_source is not None)
    if expected_source is not None and not availability["runnable"]:
        emit("skipped", reason="data_unavailable", message=availability["message"])
        return
    if not availability["runnable"]:
        raise RuntimeError(availability["message"])
    selection = 规范化配置(availability["selection"])
    if availability["message"]:
        emit("warning", message=availability["message"])
    emit("data_capabilities", capabilities=feature_meta.get("capabilities", {}),
         capabilities_by_timeframe=feature_meta.get("capabilities_by_timeframe", {}),
         coverage=feature_meta.get("coverage", {}), sources=feature_meta.get("sources", {}),
         selection=selection, availability=availability)
    emit("stage", message=f"指标缓存已就绪：{feature_path}")
    os.environ["BT_FEATURES"] = str(feature_path)
    import backtest_engine as B
    import account_replay as A
    from gpu_engine import GpuField, gpu_status, result_at

    gpu_ok, gpu_name = gpu_status()
    if args.device == "gpu" and not gpu_ok:
        raise RuntimeError(f"已指定GPU模式，但CUDA不可用：{gpu_name}")

    candidate_settings = selection["候选筛选"]
    legacy_rankings = candidate_settings["导出旧排行"]
    tp_by_id = {x.编号: x for x in 生成止盈方案()}
    if args.tp_ids:
        selected = {int(x.strip()) for x in args.tp_ids.split(",") if x.strip()}
        selection["止盈方案编号"] = [code for code in selection["止盈方案编号"] if code in selected]
    selection = 规范化配置(selection)
    combination_options = choices_for_config(selection)
    combined_tp = bool(selection.get("指标组合", {}).get("止盈"))
    combined_stops = bool(selection.get("指标组合", {}).get("止损"))
    if combined_tp:
        tps = LazyTakeProfits(combination_options["止盈"], args.limit_tp)
    else:
        tps = [tp_by_id[code] for code in selection["止盈方案编号"]]
        if args.limit_tp > 0:
            tps = tps[:args.limit_tp]
        selection["止盈方案编号"] = [x.编号 for x in tps]
        selection = 规范化配置(selection)
    total_space = 配置统计(selection)
    use_gpu = 选择计算设备(args.device, gpu_ok)
    if use_gpu:
        if selection["开仓位置过滤"] != ["OFF"]:
            emit("warning", message="本次包含开仓位置过滤，CUDA内核尚未支持该独立筛选维度；本次使用CPU多线程执行全部位置档。")
        else:
            emit("warning", message="本版逐仓重放需要按实际保护退出时间更新各杠杆路径，当前CUDA内核不支持此口径；本次使用CPU多线程。")
        use_gpu = False
    if args.device == "auto" and gpu_ok:
        acceleration_message = (
            f"自动=CPU最多{args.threads}个并行工作线程（本次{total_space['不含仓位完整组合数']:,}行）；"
            f"检测到GPU：{gpu_name}，当前CUDA内核尚未支持本版逐账户回放"
        )
    else:
        acceleration_message = f"使用CPU最多{args.threads}个并行工作线程；检测到GPU：{gpu_name}"
    emit("acceleration", requested=args.device, active="gpu" if use_gpu else "cpu",
         gpu=gpu_name, message=acceleration_message)
    selection_signature = 配置签名(selection)
    run_identity = build_run_identity(project_dir, selection_signature, feature_meta,
                                     ENGINE_VERSION, args.limit_base)
    if combined_tp and args.limit_tp > 0:
        run_identity["combination_tp_limit"] = args.limit_tp
    verify_run_identity(output_dir, run_identity, restart=args.restart)
    # 实际选择与运行身份使用同一份已收敛配置；不覆盖不兼容的旧断点。
    atomic_json(output_dir / "组合选择.json", selection)
    if availability["removed"] or availability["disabled_combinations"]:
        atomic_json(output_dir / "数据能力自动剔除记录.json", availability)
    selected_sizes = np.asarray(selection["仓位倍数"], dtype=np.float64)
    selected_cooldowns = selection["止盈后等待分钟"]
    selected_fields = selection["开仓指标"]
    selected_positions = selection["开仓位置过滤"]
    selected_cost_modes = cost_modes(selection)
    min_entry_gap = selection["平仓后最小开仓间隔分钟"]
    fees = selection["手续费"]
    cost_parameters = {}
    cost_notes = []
    for cost_mode in selected_cost_modes:
        cost_selection = dict(selection, 成本模式=cost_mode)
        slip_in, slip_out = effective_slippage(cost_selection)
        fee_in, fee_out = effective_fee_rates(cost_selection)
        cost_parameters[cost_mode] = (slip_in, slip_out, fee_in, fee_out)
        cost_label = "仅手续费" if cost_mode == "FEE" else "仅成交偏移"
        cost_notes.append(f"{cost_label}：开仓偏移{slip_in:.6%}、平仓偏移{slip_out:.6%}；"
                          f"开仓净手续费{fee_in:.6%}、平仓净手续费{fee_out:.6%}")
    funds = selection["资金约束"]
    initial_capital = float(funds["初始资金USDC"])
    minimum_order_eth = float(funds["最小开仓数量ETH"])
    maximum_order_eth = float(funds["最大开仓数量ETH"])
    enforce_minimum_order = bool(funds["低于最小数量停止"])
    protect_ratio = float(funds["保护止损浮亏比例"])
    maintenance_rate = float(funds["维持保证金率"])
    cross_liquidation = bool(funds["启用全仓强平"])
    fill_mode = selection["成交价格口径"]
    close_confirmed = fill_mode == "CLOSE_CONFIRMED"
    calculation_version = ENGINE_VERSION + ":" + fill_mode
    emit("stage", message=f"成交价格口径：{fill_mode}；开仓取信号收盘价并应用所选成本模式，不模拟Maker排队成交")
    emit("stage", message=(f"所有平仓后最小开仓间隔：{min_entry_gap}分钟，与止盈等待取较晚时间；"
                           "成本模式分别回测、不叠加扣费：" + "；".join(cost_notes) +
                           "；返佣按即时冲减成本近似"))
    emit("stage", message="开仓位置过滤独立扫参：" + "；".join(
        f"{code}={position_filter_label(code)}" for code in selected_positions))
    funding_rate = float(funds["资金费率"])
    entry_limits = selection["入场约束"]
    s3_gap = float(entry_limits["最小S3距离"])
    s3_timeframe = str(entry_limits["S3基线周期"])
    selected_entry_modes = 入场口径列表(selection)
    entry_mode_notes = {
        "F5_EVENT": "第五轮独立事件；方向和重置由所选方法定义，每次已确认事件最多实际开仓一次",
        "LIVE_01": "高周期状态持续；每个1m所选指标连续升/降方向段最多开一次",
        "TF_EVENT": "研究事件；最低启用周期收盘触发一次",
        "MACD_CYCLE": "高周期状态持续；DIF/DEA每次金叉死叉（hist柱红绿换色）后的一轮最多开一次（1m，多空共用名额）；柱增减不重置，hist穿零才换轮，不是DIF自身穿零；零值延续，前导零不开仓",
        "MACD_FULL_RED": "1m MACD柱红→绿→红为完整一轮，固定红起点；观察到绿转红的真实边界才开始首轮，初始截断色段不开仓；中间红转绿、平仓均不恢复次数，多空共用一次实际开仓名额；hist为零延续前色，柱增减不换轮",
        "MACD_FULL_GREEN": "1m MACD柱绿→红→绿为完整一轮，固定绿起点；观察到红转绿的真实边界才开始首轮，初始截断色段不开仓；中间绿转红、平仓均不恢复次数，多空共用一次实际开仓名额；hist为零延续前色，柱增减不换轮",
    }
    for entry_mode in selected_entry_modes:
        emit("stage", message=f"入场触发口径（独立回测）：{entry_mode}｜{entry_mode_notes[entry_mode]}；可与开仓位置过滤叠加，只有实际建仓才消耗本段/轮名额")
    case_options = [combination_options["开仓"][tf] for tf in ("4h", "1h", "15m", "5m", "1m")]
    entry_bytes_per_case = max(1, B.N * 9 * len(selection["开仓方向"]) *
                               len(selection["交易会话"]) * len(selected_positions))
    entry_batch_size = max(1, min(32, 入场批预算字节() // entry_bytes_per_case))
    entry_batch_count = ((math.prod(option.count for option in case_options) + entry_batch_size - 1) // entry_batch_size
                         * len(selected_fields) * len(selected_entry_modes) * len(selected_cost_modes))
    emit("stage", message=(f"开仓信号分批：每批最多{entry_batch_size}组，共{entry_batch_count:,}批；不一次加载全部组合。"
                           f"每批同时可并行的任务数按“每批组数×方向×会话×位置档×止损×叠加止盈×固定止损×时间止损×等待档”计算；"
                           f"该数小于并发数时线程一定跑不满。入场数组约{entry_bytes_per_case * entry_batch_size / 1024**2:.0f}MB／批，"
                           f"预算{入场批预算字节() // 1024**2}MB（BT_ENTRY_BATCH_MB 可改）"))
    # 预算是定值，不随内存变，否则分批边界会漂、断点续不上。内存不够时只提醒，
    # 不偷偷改小：让使用者自己用 BT_ENTRY_BATCH_MB 决定，跑出来的行顺序才可复现。
    from fifth_precompute import available_memory as _可用内存
    _批内存 = entry_bytes_per_case * entry_batch_size
    _空闲 = _可用内存()
    if _批内存 > _空闲 * 0.5:
        emit("warning", message=(
            f"开仓批入场数组约{_批内存 / 1024**2:.0f}MB，当前可用内存约{_空闲 / 1024**2:.0f}MB，"
            f"占比偏高。如果开始换页反而更慢，请设 BT_ENTRY_BATCH_MB 调小后新建任务目录重跑；"
            f"分批大小变了不能续用旧断点。"))
    selected_stops = combination_options["止损"] if combined_stops else set(selection["止损代码"])
    selected_fixed_stops = 补全固定比例档(selection["固定止损代码"], "FSL")
    selected_overlay_tps = 补全固定比例档(selection["叠加止盈代码"], "FTP")
    selected_directions = list(selection["开仓方向"])
    selected_sessions = list(selection["交易会话"])
    selected_hard_time = list(selection["强制时间止损分钟"])
    result_csv = output_dir / "全部回测结果.csv"
    checkpoint_path = output_dir / "断点记录.json"
    top_json = output_dir / "各类止盈前5000名.json"
    worst_json = output_dir / "各类止盈最差1000名.json"
    if args.restart:
        generated = [
            result_csv, checkpoint_path, top_json, worst_json, output_dir / "候选筛选设置.json",
            output_dir / "分层候选数据.json", output_dir / "每类研究候选.csv",
            output_dir / "全局观察候选.csv", output_dir / "实盘候选.csv", output_dir / "分层候选分析.xlsx",
        ]
        for leverage_tag in (f"{value:g}x" for value in 仓位列表):
            generated.extend([
                output_dir / f"分层候选数据_{leverage_tag}.json", output_dir / f"每类研究候选_{leverage_tag}.csv",
                output_dir / f"全局观察候选_{leverage_tag}.csv", output_dir / f"实盘候选_{leverage_tag}.csv",
                output_dir / f"分层候选分析_{leverage_tag}.xlsx",
            ])
        for path in generated:
            if path.exists(): path.unlink()

    checkpoint = {"next_tp": 0, "rows": 0, "csv_bytes": 0, "top": {}, "worst": {}}
    if not checkpoint_path.exists() and result_csv.exists() and result_csv.stat().st_size:
        with result_csv.open(encoding="utf-8-sig", newline="") as existing:
            reader = csv.reader(existing)
            header_only = next(reader, None) == 全量CSV表头 and next(reader, None) is None
        if not header_only:
            raise RuntimeError("结果CSV已存在但断点记录缺失，无法证明已完成范围。请使用新结果目录，避免重复追加。")
        emit("stage", message="上次只写入CSV表头，尚无结果行；安全从第一批开始")
    if checkpoint_path.exists():
        checkpoint.update(json.loads(checkpoint_path.read_text("utf-8")))
        checkpoint_signature = checkpoint.get("selection_signature")
        if checkpoint_signature is None:
            raise RuntimeError("该目录是旧版断点，不包含止盈后等待时间，不能继续混写。请更换结果目录，或从头重新计算。")
        if checkpoint_signature != selection_signature:
            raise RuntimeError("当前组合选择与该目录的已有断点不一致。请更换结果目录，或勾选“删除原断点并从头重新计算”。")
        committed_bytes = int(checkpoint.get("csv_bytes", 0))
        if committed_bytes and (not result_csv.is_file() or result_csv.stat().st_size < committed_bytes):
            raise RuntimeError("结果CSV缺失或小于断点已保存长度，无法安全续跑；请恢复完整CSV或使用新目录重新计算。")
    checkpoint["selection_signature"] = selection_signature
    checkpoint["selection"] = selection
    checkpoint["run_identity"] = run_identity
    atomic_json(output_dir / "回测运行身份.json", run_identity)
    save_json_atomic(output_dir / "回测数据说明.json", feature_meta)
    save_json_atomic(ranking_settings_path, 当前排行设置())
    dictionary_path = output_dir / "止盈方案字典.csv"
    if combined_tp:
        if not dictionary_path.exists() or args.restart:
            write_dictionary(dictionary_path, [tp_by_id[code] for code in selection["止盈方案编号"]])
        with dictionary_path.open(encoding="utf-8-sig", newline="") as source:
            written_tp_ids = {int(row["止盈方案编号"]) for row in csv.DictReader(source)}
    else:
        write_dictionary(dictionary_path, tps)
        written_tp_ids = {tp.编号 for tp in tps}
    (output_dir / "开仓扩展规则字典.json").write_text(json.dumps(
        {code: {"名称": value[0], "算法": value[1], "参数": value[2], "批次": ENTRY_RULE_BATCH.get(code, 1)}
         for code, value in ENTRY_RULES.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "中文字段说明.txt").write_text(
        "MACD柱=2×(DIF-DEA)；DIF线=EMA12-EMA26；t值=平均单笔收益÷均值标准误；\n"
        "平均日完整交易数与成交订单数分开统计；普通交易每次2笔订单，分批策略实际触发分批时3笔、分批前止损时2笔；UTC周六、周日为周末。\n"
        "手续费按各次开仓、平仓成交名义金额扣除。成本模式分别回测、不叠加扣费：" + "；".join(cost_notes) + "。"
        "每行只有一种成本；已计入单笔收益、账户资金及回撤。"
        "返佣按即时冲减成本近似，不模拟延迟到账。表内历史字段‘毛收益’为扣所选成本后的非复利收益合计。\n"
        "入场触发口径（各模式独立回测）=" + "；".join(
            f"{mode}：{entry_mode_notes[mode]}" for mode in selected_entry_modes) + "。"
        "LIVE_01按所选hist/DIF的连续升降方向段去重；MACD_CYCLE始终按1m hist（DIF-DEA）的零轴同侧轮次去重，不是DIF自身过零，也不是一次金叉到下一次金叉的双向完整循环；"
        "MACD_FULL_RED固定从绿转红边界到下一次绿转红边界；MACD_FULL_GREEN固定从红转绿边界到下一次红转绿边界；"
        "完整周期均含连续红绿两段，中间换色和平仓不恢复次数，只有实际开仓消耗多空共用名额；初始截断色段不开仓，等待真实起点边界。"
        "TF_EVENT为最低启用周期收盘触发一次。各口径分别回测，不能在同一策略内叠加名额；不代表与生产撮合完全一致；同一分钟止损和止盈同时触发时按止损优先。\n"
        "移动止盈使用保守OHLC顺序：先检查上一时刻已确定的止盈线，未退出后才用本根新高/新低更新下一时刻止盈线。\n"
        "多周期K线由连续1分钟K线按UTC自然边界合成；首尾不足5m/15m/1h/4h的周期桶丢弃，非零高周期MACD条件预热至少35根原生K线。\n"
        f"所有完全平仓后至少间隔{min_entry_gap}分钟才可再次开仓，与原止盈冷却取较晚时间、不相加；0关闭新增间隔。"
        "原止盈冷却为跳过所选完整分钟K线；分批止盈只要首段成交，余仓结束后执行原止盈冷却。"
        "‘实际止盈等待总时间’仍只统计原止盈冷却；‘实际容量占用率’不含新增全退出间隔。\n"
        f"初始资金{initial_capital:g} USDC；ETH最小开仓数量{minimum_order_eth:g}；单次最大开仓数量{maximum_order_eth:g} ETH；"
        f"低于最小数量停止={'启用' if enforce_minimum_order else '关闭'}。计划数量超过上限时按上限成交。\n"
        "开仓MACD代码：0=MACD柱，1=DIF线；条件0—4=第一批v8，5—17=第二批0904，18—143=第三批20260909；144—263=第四批v1.44R。高周期按已收盘原生K线计算、作为1m持续许可；标注分钟的OI/首段区间窗口保留原时长。详见开仓扩展规则字典.json。\n"
        "开仓位置过滤是独立扫参维度，在原开仓信号与方向确认后筛选；未通过不会消耗MACD方向段或零轴轮次。OFF不增加过滤。"
        "基础策略编号保持原编号，使用开仓位置过滤代码区分位置方案；不同位置档分别统计交易与账户结果。\n"
        "情况三：向上严格反转且成交量大于上一根时做多；向下严格反转且成交量大于上一根时做空；量能不足不开户。\n"
        "情况四：量能充足时顺反转方向开仓；量能不足（成交量小于或等于上一根）时反向开仓。\n"
        "止损④=持仓方向的反转量能不足；止损⑤=后续相反方向量能充足反转的成交量大于前一次反转K线；止损⑥=严格MACD反转（前一根必须同向）。\n"
        "注意：止盈的MACD反转是\"连续N根反向\"口径，不要求前一根同向，与止损⑥不是同一判据。\n"
        f"成交价格口径={fill_mode}；CLOSE_CONFIRMED按各次触发K线收盘价记账，THEORETICAL沿用旧理论触碰价；全仓风险退出仍沿用逐K线风险模型。\n"
        "SLIPPAGE分支的成交偏移见上文；每笔完整交易扣除一次，分批止盈不重复扣除；FEE分支偏移为0。\n"
        f"最小S3距离：{s3_gap:.4%}（基线周期{s3_timeframe}）。多单必须在基线上方、空单必须在基线下方至少该距离；"
        "已越过、距离不足、基线尚不可用一律禁止开仓；设为0表示关闭该闸门。\n"
        "保证金口径为全仓：整个账户为持仓担保，浮亏占权益比例=名义倍数×不利波动。\n"
        f"②爆仓保护在浮亏达到权益{protect_ratio:.0%}时平仓；"
        f"全仓强平线={'启用' if cross_liquidation else '关闭'}，维持保证金率{maintenance_rate:.3%}，"
        "强平线早于保护线时按强平处理：该仓位档权益归零并停机。两条线谁先到算谁。\n"
        "资金费当前不计入：付出还是收取取决于持仓方向与当期费率符号，需接入历史费率序列后才能建模。\n"
        "实盘差异边界：本回测仅处理ETH单品种，不模拟01程序的ETH/BTC共享单仓；固定成交偏移不能代替Maker未成交、撤单追价、网络延迟、订单簿冲击和下单竞态。\n"
        "各杠杆账户独立重放：保护退出在首次触发的分钟平仓，不套用其他杠杆的交易次数；资金不足或归零后停止累计。最大回撤包含已观测到的分钟收盘浮盈峰值。\n"
        "全量CSV每行代表一个所选入场+止损+止盈+等待时间组合；普通统计列展示首个仓位档，所选仓位实际交易统计保存所有仓位各自的数据，Excel按对应仓位读取。\n"
        "所选仓位实际交易统计：档位之间用分号，档内用竖线，依次为交易次数、胜率、多单占比、完整交易/日、订单/日、平均持仓、平均单笔、收益合计、利润因子、普通t值、2025收益、2026收益、实际止盈等待总分钟、实际容量占用率。\n"
        "实际容量占用率=(总持仓分钟+数据区间内实际止盈等待分钟)/总区间分钟；止损、保护退出不增加止盈等待，区间结束后的等待不计。此项不包括网络/挂单等待。\n"
        "沿用历史列名的“毛收益”实际已扣所选成本；年度收益按实际平仓年份统计。“盈亏比”列计算总盈利/总亏损，属于利润因子，不是平均盈利/平均亏损。\n",
        "utf-8",
    )
    with (output_dir / "中文字段说明.txt").open("a", encoding="utf-8") as guide:
        guide.write(
            "默认导出采用：硬门槛→同止盈类别内研究预筛分→相似策略簇最多2条→每类/全局分层候选。\n"
            "研究预筛分只使用当前汇总CSV可可靠计算的指标；DSR、PBO、稳健t值、样本外Calmar等缺少逐笔分窗矩阵时保持待验证，实盘候选为空。\n"
        )

    if result_csv.exists() and checkpoint["csv_bytes"]:
        with result_csv.open("rb+") as f:
            f.truncate(int(checkpoint["csv_bytes"]))
    if legacy_rankings:
        ensure_worst_scope(project_dir, output_dir, checkpoint, ranking_settings_path)
        ensure_top_limit(project_dir, output_dir, checkpoint, ranking_settings_path)
    heaps, heap_counter = load_heaps(checkpoint.get("top", {})) if legacy_rankings else ({}, 0)
    worst_heaps, worst_counter = load_worst_heaps(checkpoint.get("worst", {})) if legacy_rankings else ({}, 0)

    new_file = not result_csv.exists() or result_csv.stat().st_size == 0
    text_mode = "w" if new_file else "a"
    csv_file = result_csv.open(text_mode, encoding="utf-8-sig" if new_file else "utf-8", newline="", buffering=1024 * 1024 * 8)
    writer = csv.writer(csv_file, lineterminator="\n")
    if new_file: writer.writerow(全量CSV表头)

    上次保存时刻 = time.time()
    # 待写入CSV的缓冲行。落盘改成按时间节流后，它必须跨止盈方案保留，
    # 不能再在每个方案开头重建，否则没来得及写出的行会被丢掉。
    batch_rows = []
    ranking_context = {"schema": 1, "selection": selection, "data": feature_meta, "identity": run_identity}
    ranking_metadata = {
        "运行上下文": ranking_context, "worst_scope": WORST_SCOPE, "最差排行范围": WORST_SCOPE_LABEL,
        "排行指标": _排行状态["名称"],
        "组合门槛": [{"指标": x["指标"], "条件": x["条件"], "值": x["值"]} for x in _排行门槛],
    }

    def 保存断点(完成到止盈位置: int, 强制: bool = False, 开仓批位置=None, 已算基础数=0) -> None:
        """把CSV、断点和排行一起落盘，按时间节流。

        以前每个止盈方案跑完都要重写断点和两份排行JSON。排行满员时
        （12类×5000行）仍会产生较大的排行文件，因此不能在每个止盈方案后重写。
        现在默认每60秒落一次；中途崩溃最多重算这60秒，续跑时按 csv_bytes
        把CSV截回上次落盘点，不会写出重复行。停止和跑完时一定强制落盘。
        """
        nonlocal 上次保存时刻
        if not 强制 and time.time() - 上次保存时刻 < 断点保存间隔秒:
            return
        if 完成到止盈位置 < start_tp - 1:
            return
        if batch_rows:
            writer.writerows(batch_rows); checkpoint["rows"] += len(batch_rows); batch_rows.clear()
        csv_file.flush(); os.fsync(csv_file.fileno())
        if selection.get("指标组合"):
            DEFAULT_REGISTRY.save(output_dir)
        checkpoint.update({
            "next_tp": 完成到止盈位置 + 1,
            "csv_bytes": result_csv.stat().st_size,
            "top": heaps_to_rows(heaps) if legacy_rankings else {},
            "top_limit": 最优排行名额,
            "worst": worst_heaps_to_rows(worst_heaps) if legacy_rankings else {},
            "worst_scope": WORST_SCOPE if legacy_rankings else None,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_batch_next": 开仓批位置 or 0,
            "entry_batch_size": entry_batch_size,
            "entry_base_done": 已算基础数 if 开仓批位置 is not None else 0,
        })
        save_json_atomic(checkpoint_path, checkpoint)
        if legacy_rankings:
            save_json_atomic(top_json, {"表头": 中文表头, "分类": checkpoint["top"], "排行": "最优",
                                        "名额": 最优排行名额, "初始资金": initial_capital, **ranking_metadata})
            save_json_atomic(worst_json, {"表头": 中文表头, "分类": checkpoint["worst"], "排行": "最差",
                                          "名额": 最差排行名额,
                                          "初始资金": initial_capital, **ranking_metadata})
        上次保存时刻 = time.time()

    gpu_fields = {}
    start_tp = min(int(checkpoint["next_tp"]), _sequence_count(tps))
    if not checkpoint_path.exists():
        保存断点(start_tp - 1, 强制=True)
    tp_row_counts = scan_row_counts(selection, tps, args.limit_base)
    total_scan_rows = tp_row_counts.total if isinstance(tp_row_counts, LazyRowCounts) else sum(tp_row_counts)
    maximum_tp_rows = tp_row_counts.maximum if isinstance(tp_row_counts, LazyRowCounts) else max(tp_row_counts, default=0)
    scan_progress = ScanProgress(total_scan_rows, int(checkpoint["rows"]))

    def report_scan_progress(kind, tp_pos, base_done, force=False, **details):
        # Completed work includes buffered rows; flushing must not move progress backwards.
        fields = scan_progress.snapshot(checkpoint["rows"] + len(batch_rows), force)
        if fields is not None:
            emit(kind, tp=tp_pos + 1, total_tp=_sequence_count(tps), category=tps[tp_pos].类别,
                 base_done=base_done, base_total=tp_row_counts[tp_pos], **fields, **details)

    emit("start", total_tp=_sequence_count(tps), start_tp=start_tp, threads=args.threads,
         total_combinations=total_scan_rows * len(selected_sizes),
         csv_rows=total_scan_rows, output=str(result_csv))

    def prepare_tp(tp):
        if tp.编号 < 0:
            from indicator_combinations import tp_members, tp_logic
            if tp_logic(tp.编号) != "OR":
                raise ValueError("组合止盈目前要求任一触发退出")
            members = []
            for code in tp_members(tp.编号):
                member = tp_by_id[code]
                if member.类别 == "分批止盈":
                    raise ValueError("分批止盈必须独立回测，不能加入全仓退出组合")
                data, _ = prepare_tp(member)
                members.append((member, data))
            return None, members
        if tp.类别 == "前高前低结构止盈":
            return B.structure_tp_data(tp), None
        if tp.类别 == "分批止盈":
            first = B.fixed_rate_data(tp.参数一)
            remainder = tp_by_id[int(tp.参数三)]
            return None, (first, B.partial_remainder_data(remainder), tp.参数二)
        return (None if tp.类别 == "盈亏比止盈" else B.simple_tp_data(tp)), None

    # 仅每个止盈方案任务很少时，使用空闲线程提前准备后续止盈。
    # 最多4个准备线程、5套待消费数据；计算和断点仍按原方案顺序推进。
    prepare_workers = (min(4, max(0, args.threads - maximum_tp_rows))
                       if _sequence_count(tps) - start_tp > 1 else 0)
    tp_executor = None
    prepared_tps = None
    if prepare_workers:
        tp_executor = ThreadPoolExecutor(max_workers=prepare_workers)
        prepared_tps = 有界顺序结果(
            tp_executor, prepare_tp, (tps[i] for i in range(start_tp, _sequence_count(tps))), prepare_workers + 1,
            lambda: False)
        emit("stage", message=f"使用{prepare_workers}个空闲线程提前准备止盈方案；按原顺序保存结果")

    try:
        if start_tp < _sequence_count(tps):
            from fifth_precompute import prepare_fifth_signals, selected_tasks, PrecomputeStopped
            method_tasks = selected_tasks(case_options)
            if args.limit_base > 0:
                # A limited diagnostic run must not train unrelated selected models.
                from indicator_combinations import entry_members, TIMEFRAMES
                wanted = set()
                for _, cases in islice(开仓批次(selected_fields, case_options, 1), args.limit_base):
                    for tf, code in zip(TIMEFRAMES, cases[0]):
                        wanted.update((tf, member) for member in entry_members(code))
                method_tasks = [task for task in method_tasks if task in wanted]
            if method_tasks:
                try:
                    signal_cache = prepare_fifth_signals(
                        B.E.D, method_tasks, output_dir, run_identity, args.threads,
                        lambda details: emit("entry_precompute", **details), control_dir,
                        feature_path.parent / "第五轮信号缓存")
                except PrecomputeStopped:
                    emit("stopped", next_tp=start_tp)
                    return
                B.set_fifth_signal_cache(signal_cache)
                # Preparation has its own progress; estimate replay from replay work.
                scan_progress = ScanProgress(total_scan_rows, int(checkpoint["rows"]))
        for tp_pos in range(start_tp, _sequence_count(tps)):
            tp = tps[tp_pos]
            if tp.编号 not in written_tp_ids:
                with dictionary_path.open("a", encoding="utf-8", newline="") as target:
                    csv.writer(target).writerow([tp.编号, tp.类别, tp.周期组合, tp.指标,
                                                tp.参数一, tp.参数二, tp.参数三, tp.说明])
                written_tp_ids.add(tp.编号)
            existing_batch = checkpoint.get("entry_batch_next", 0) if checkpoint["next_tp"] == tp_pos else None
            existing_done = checkpoint.get("entry_base_done", 0) if existing_batch else 0
            if stop_requested():
                保存断点(tp_pos - 1, 强制=True, 开仓批位置=existing_batch, 已算基础数=existing_done)
                emit("stopped", next_tp=tp_pos); break
            while (control_dir / "暂停.flag").exists():
                scan_progress.pause()
                保存断点(tp_pos - 1, 强制=True, 开仓批位置=existing_batch, 已算基础数=existing_done)
                emit("paused", tp=tp_pos + 1)
                time.sleep(1)
                if stop_requested(): break
            scan_progress.resume()
            if stop_requested():
                保存断点(tp_pos - 1, 强制=True, 开仓批位置=existing_batch, 已算基础数=existing_done); break

            report_scan_progress("tp_start", tp_pos, existing_done, force=True, description=tp.说明)
            shared_tp, partial_spec = (next(prepared_tps) if prepared_tps is not None
                                       else prepare_tp(tp))

            # 叠加固定比例止盈：与所选止盈方案并行，谁先触发算谁。
            # 分批止盈和盈亏比止盈自己就带价位逻辑，不参与叠加。
            overlay_variants = ([(code, label, B.overlay_tp_data(wd, we))
                                 for code, wd, we, label in selected_overlay_tps] if tp.编号 < 0 else
                                [("OFF", "不叠加固定比例止盈", None)]
                                if shared_tp is None else
                                [(code, label,
                                  B.apply_overlay_tp(shared_tp,
                                                     B.overlay_tp_data(wd, we)))
                                 for code, wd, we, label in selected_overlay_tps])

            # 中途停止时，这个方案的行只写了一半，绝不能当成已完成落盘，
            # 否则续跑会从下一个方案开始，半截的行就重复留在CSV里了。
            方案已跑完 = True
            category_heap = heaps.setdefault(tp.类别, []) if legacy_rankings else None
            category_worst_heap = worst_heaps.setdefault(tp.类别, []) if legacy_rankings else None
            resume_batch = int(checkpoint.get("entry_batch_next", 0)) if tp_pos == start_tp else 0
            if resume_batch and checkpoint.get("entry_batch_size") != entry_batch_size:
                raise RuntimeError(
                    f"开仓分批大小与断点不同（断点{checkpoint.get('entry_batch_size')}组／本次{entry_batch_size}组），"
                    "分批边界变了就不能证明续跑位置对得上。要继续这份断点，请设环境变量 "
                    f"BT_ENTRY_BATCH_MB={max(1, entry_bytes_per_case * int(checkpoint.get('entry_batch_size') or 1) // 1024**2)} "
                    "后重新开始；否则请新建任务目录。")
            base_done = int(checkpoint.get("entry_base_done", 0)) if resume_batch else 0

            mode_batches = ((cost, mode, field, cases) for cost in selected_cost_modes for mode in selected_entry_modes
                            for field, cases in 开仓批次(selected_fields, case_options, entry_batch_size))
            for batch_index, (cost_mode, entry_mode, field, case_batch) in enumerate(mode_batches):
                if batch_index < resume_batch:
                    continue
                entry_slippage, exit_slippage, entry_fee_rate, exit_fee_rate = cost_parameters[cost_mode]
                roundtrip_slippage = entry_slippage + exit_slippage
                execution_columns = [min_entry_gap, fees["开仓费率"], fees["平仓费率"],
                                     int(fees["BNB抵扣"]), fees["返佣比例"],
                                     entry_fee_rate, exit_fee_rate, cost_mode]
                if (control_dir / "暂停.flag").exists():
                    scan_progress.pause()
                if (control_dir / "暂停.flag").exists() or stop_requested():
                    保存断点(tp_pos - 1, 强制=True, 开仓批位置=batch_index, 已算基础数=base_done)
                while (control_dir / "暂停.flag").exists() and not stop_requested():
                    emit("paused", tp=tp_pos + 1)
                    time.sleep(1)
                if scan_progress.resume():
                    report_scan_progress("inner_progress", tp_pos, base_done, force=True)
                if stop_requested():
                    方案已跑完 = False
                    break
                if batch_index == resume_batch or batch_index % 100 == 0:
                    report_scan_progress("stage", tp_pos, base_done, force=True,
                                         message=f"生成开仓批次 {batch_index+1:,}/{entry_batch_count:,}：{cost_mode}/{entry_mode}/{field}，{len(case_batch)}组")
                field_index = 0 if field == "hist" else 1
                # 一套信号止损包含6条与1分钟K线等长的数组。一次展开数百套会
                # 耗尽内存；超过显存安全上限时也改走CPU小批量路径。
                field_gpu_enabled = use_gpu and _sequence_count(selected_stops) <= GPU一次载入止损上限
                # 止损数组和止盈方案无关。以前 materialize_stops 跟GPU开关绑死，
                # CPU路径每个止盈方案都要 make_stop_data 重建一次再丢掉；
                # 套数不多时直接常驻，8280个方案能省掉上万次重建。
                materialize_stops = (field_gpu_enabled
                                     or _sequence_count(selected_stops) <= CPU常驻止损上限)
                if use_gpu and not field_gpu_enabled:
                    emit("warning", message=(
                        f"已选择{_sequence_count(selected_stops)}套信号止损，超过GPU单批"
                        f"{GPU一次载入止损上限}套的安全上限；本周期自动改用CPU分批计算，"
                        "避免耗尽内存"
                    ))
                entry_variants = []
                field_stop_codes = (("__COMBINATION_STOPS__",) if combined_stops
                                    else tuple(sorted(selected_stops)))
                # 循环第一轮的参数就是 (方向[0], 会话[0], 位置[0])，和后面那次
                # 单独取 run_id/stops 的调用完全一样。build_field 的 lru_cache 只有
                # 2 格，位置过滤勾到 3 档以上时那次重复调用必然落空要整个重算：
                # 本机实测 5 档位置过滤每批 135.1 毫秒 → 去掉后 122.4 毫秒。
                # 这里直接留住第一轮的结果，参数一致、返回值一致，口径不变。
                run_id = entries = stops = None
                for direction in selected_directions:
                    for session in selected_sessions:
                        for position_filter in selected_positions:
                            _, _, variant_run_id, variant_entries, variant_stops = B.build_field(
                                field, case_batch,
                                field_stop_codes, s3_gap, s3_timeframe,
                                direction, session, materialize_stops, entry_mode,
                                position_filter)
                            if run_id is None:
                                run_id, entries, stops = variant_run_id, variant_entries, variant_stops
                            entry_variants.append((direction, session, position_filter,
                                                   position_filter_label(position_filter), variant_entries))
                # 止损放外层、入场放内层：同一份合并后的止损数组会被连续用完，
                # B.combined_stop_data 的小缓存才命中得上，内存才压得住。
                if combined_stops:
                    stops = LazyStops(selected_stops)
                stop_map = {} if combined_stops else {code: data for code, _label, data in stops}
                stop_components = {} if combined_stops else {
                    code: components for code, components, _label in B.STOP_DEFINITIONS
                    if code in selected_stops
                }
                def combo_tasks(stop, overlay, fixed_stop, hard_min):
                    stop_code, stop_label, _sd = stop
                    ov_code, ov_label, ov_tp = overlay
                    fs_code, fs_weekday, fs_weekend, fs_label = fixed_stop
                    stop_index = (composite_stop_index(stop_code) if stop_code.startswith("@COMBO:")
                                  else B.STOP_INDEX[stop_code])
                    combo = (stop_code, fs_weekday, fs_weekend, hard_min)
                    for direction, session, position_filter, position_label, variant_entries in entry_variants:
                        for cases, cand, cdir in variant_entries:
                            base_id = stable_base_id(field_index, cases, stop_index)
                            yield (base_id, cases, cand, cdir, stop_code, stop_label,
                                   combo, fs_code, fs_label, direction, session, hard_min,
                                   ov_code, ov_label, ov_tp, position_filter, position_label)

                # CPU只保留本批入场数组和有限的在途任务，不展开完整参数笛卡尔积。
                base_task_count = (_sequence_count(stops) * len(overlay_variants) * len(selected_fixed_stops)
                                   * len(selected_hard_time)
                                   * sum(len(variant[-1]) for variant in entry_variants))
                task_count = base_task_count * len(selected_cooldowns)
                if args.limit_base > 0:
                    task_count = min(task_count, max(0, args.limit_base - base_done))
                if task_count <= 0:
                    continue

                def evaluate(prepared):
                    task, stop_data = prepared
                    (base_id, cases, cand, cdir, stop_code, stop_label, combo,
                     fs_code, fs_label, direction, session, hard_min,
                     ov_code, ov_label, ov_tp, position_filter, position_label, cooldown) = task
                    actual_run_id = B.entry_run_id_for_cases(cases, run_id)
                    if ov_tp is not None:
                        shared_tp_local = ov_tp
                    else:
                        shared_tp_local = shared_tp
                    sx_l, sx_s, sp_l, sp_s, _, _ = stop_data
                    if tp.编号 < 0:
                        plans = [B.risk_reward_tp_data(stop_data, member.参数一)
                                 if member.类别 == "盈亏比止盈" else member_data
                                 for member, member_data in partial_spec]
                        if ov_tp is not None:
                            plans.append((*ov_tp, 0, 0.0, 0.0))
                        data = B.combined_tp_data(cand, cdir, actual_run_id, stop_data, plans)
                    elif tp.类别 == "盈亏比止盈":
                        data = B.risk_reward_tp_data(stop_data, tp.参数一)
                    else:
                        data = shared_tp_local
                    if tp.类别 == "分批止盈":
                        first, remainder_data, fraction = partial_spec
                        mode, next_l, next_s, rp1, rp2 = remainder_data
                        result = A.simulate_partial_sizes(
                            cand, cdir, actual_run_id, sx_l, sx_s, sp_l, sp_s,
                            first[0], first[1], first[2], first[3], fraction,
                            mode, next_l, next_s, rp1, rp2,
                            B.CLOSE, B.HIGH, B.LOW, selected_sizes, B.YEAR_INDEX, len(B.YEAR_VALUES), cooldown,
                            roundtrip_slippage, initial_capital, minimum_order_eth, enforce_minimum_order,
                            maximum_order_eth, B.FUNDING_CUM, funding_rate,
                            protect_ratio, maintenance_rate, cross_liquidation, close_confirmed,
                            min_entry_gap=min_entry_gap,
                            entry_fee_rate=entry_fee_rate, exit_fee_rate=exit_fee_rate)
                    else:
                        tx_l, tx_s, p_l, p_s, mode, p1, p2 = data
                        result = A.simulate_all_sizes(
                            cand, cdir, actual_run_id, sx_l, sx_s, sp_l, sp_s,
                            tx_l, tx_s, p_l, p_s, mode, p1, p2,
                            B.CLOSE, B.HIGH, B.LOW, selected_sizes, B.YEAR_INDEX, len(B.YEAR_VALUES), cooldown,
                            roundtrip_slippage, initial_capital, minimum_order_eth, enforce_minimum_order,
                            maximum_order_eth, B.FUNDING_CUM, funding_rate,
                            protect_ratio, maintenance_rate, cross_liquidation, close_confirmed,
                            min_entry_gap=min_entry_gap,
                            entry_fee_rate=entry_fee_rate, exit_fee_rate=exit_fee_rate)
                    return (base_id, cases, stop_code, stop_label, fs_code, fs_label,
                            direction, session, hard_min, ov_code, ov_label, cooldown,
                            position_filter, position_label,
                            B.metrics_dict(result))

                executor = None
                cpu_group_size = CPU分组上限(task_count, args.threads, _sequence_count(stops))
                # 小组共用常驻base_stop；首次合并同一套大数组只做一次。
                group_stop_lock = Lock() if cpu_group_size < 64 else None

                def cpu_groups():
                    remaining = task_count
                    # 保持原stop/combo/cooldown/case顺序，描述符不持有新建的大数组。
                    for stop in stops:
                        if remaining <= 0:
                            return
                        code = stop[0]
                        group = []
                        combinations = ((overlay, fixed_stop, hard_min)
                                        for overlay in overlay_variants
                                        for fixed_stop in selected_fixed_stops
                                        for hard_min in selected_hard_time)
                        for overlay, fixed_stop, hard_min in combinations:
                            for cooldown in selected_cooldowns:
                                for task in combo_tasks(stop, overlay, fixed_stop, hard_min):
                                    if remaining <= 0:
                                        break
                                    group.append((*task, cooldown))
                                    remaining -= 1
                                    if len(group) == cpu_group_size:
                                        yield code, group
                                        group = []
                                if remaining <= 0:
                                    break
                            if remaining <= 0:
                                break
                        if group:
                            yield code, group

                def evaluate_group(group):
                    code, tasks = group
                    if stop_requested():
                        return []
                    # 同一组共用止损准备；不修改其他线程也会读取的stop_map。
                    # 宽配置超过64条会分组重建，以限制待消费结果和在途数组的内存。
                    base_stop = stop_map.get(code)
                    if base_stop is None:
                        base_stop = (B.cached_signal_stop(field, stop_spec(code)[1]) if combined_stops else
                                     B.cached_signal_stop(field, stop_components[code]))
                    current_combo = None
                    results = []
                    for task in tasks:
                        if stop_requested():
                            break
                        combo = task[6]
                        if combo != current_combo:
                            if group_stop_lock is None:
                                stop_data = B.combined_stop_data(
                                    base_stop, combo[1], combo[2], combo[3], combo)
                            else:
                                with group_stop_lock:
                                    stop_data = B.combined_stop_data(
                                        base_stop, combo[1], combo[2], combo[3], combo)
                            current_combo = combo
                        results.append(evaluate((task, stop_data)))
                    return results

                def cpu_results(pool):
                    groups = cpu_groups()
                    if cpu_group_size == 64:
                        first = next(groups, None)
                        if first is None:
                            return
                        # 大组保持原预热流程；少止损小组直接在线程池执行。
                        yield from evaluate_group(first)
                    prepared_results = 有界顺序结果(
                        pool, evaluate_group, groups, max(1, args.threads * 2),
                        stop_requested)
                    try:
                        for results in prepared_results:
                            yield from results
                    finally:
                        prepared_results.close()

                # 当前CUDA批处理按一套方向/会话构造入场矩阵；多方向或多会话时回到CPU，
                # 避免把不同入场集合错误套用到同一个GPU索引上。
                gpu_eligible = (
                    field_gpu_enabled
                    and min_entry_gap == 0 and entry_fee_rate == 0.0 and exit_fee_rate == 0.0
                    and not close_confirmed
                    and args.limit_base <= 0
                    and tp.类别 not in ("分批止盈", "盈亏比止盈")
                    and len(selected_directions) == 1
                    and len(selected_sessions) == 1
                    and selected_positions == ["OFF"]
                )
                result_iter = None
                try:
                    if gpu_eligible:
                        def gpu_results():
                            task_groups = {}
                            for overlay in overlay_variants:
                                for stop in stops:
                                    for fixed_stop in selected_fixed_stops:
                                        for hard_min in selected_hard_time:
                                            key = (fixed_stop[0], hard_min, overlay[0])
                                            task_groups.setdefault(key, []).extend(
                                                combo_tasks(stop, overlay, fixed_stop, hard_min))
                            # 每次只处理一个固定止损/时间止损/叠加止盈组，不再把全部等待档
                            # 展开成几十万个Python任务。GPU输出顺序固定为“入场×止损”。
                            stop_labels = {code: label for code, label, _ in stops}
                            entry_positions = {cases: index for index, (cases, _, _) in enumerate(entries)}
                            for (fs_code, hard_min, ov_code), group in task_groups.items():
                                combos = {}
                                for task in group:
                                    combos.setdefault(task[4], task[6])
                                grouped_stops = [
                                    (code, stop_labels[code],
                                     B.combined_stop_data(stop_map[code], combo[1],
                                                          combo[2], combo[3], combo))
                                    for code, combo in combos.items()
                                ]
                                stop_positions = {
                                    code: index for index, (code, _, _) in enumerate(grouped_stops)
                                }
                                gkey = (cost_mode, entry_mode, field, fs_code, hard_min, ov_code)
                                if gkey not in gpu_fields:
                                    gpu_fields.clear()
                                    emit("stage", message=f"上传{field}/{fs_code}/{ov_code}到GPU")
                                    gpu_fields[gkey] = GpuField(
                                        entries, grouped_stops, run_id, B.CLOSE, B.HIGH, B.LOW,
                                        selected_sizes, B.YEAR_INDEX, len(B.YEAR_VALUES))
                                group_tp = next((task[14] for task in group if task[14] is not None),
                                                shared_tp)
                                for cooldown in selected_cooldowns:
                                    emit("stage", message=(
                                        f"GPU正在计算：{field}/{fs_code}/{ov_code}，"
                                        f"止盈后等待{cooldown}分钟"
                                    ))
                                    gpu_batch = gpu_fields[gkey].simulate(
                                        group_tp, cooldown, roundtrip_slippage,
                                        initial_capital, minimum_order_eth, enforce_minimum_order,
                                        maximum_order_eth, B.FUNDING_CUM, funding_rate,
                                        protect_ratio, maintenance_rate, cross_liquidation)
                                    for task in group:
                                        gpu_index = GPU任务索引(
                                            task[1], task[4], entry_positions,
                                            stop_positions, len(grouped_stops))
                                        yield (task[0], task[1], task[4], task[5], task[7],
                                               task[8], task[9], task[10], task[11],
                                               task[12], task[13], cooldown, task[15], task[16],
                                               B.metrics_dict(result_at(gpu_batch, gpu_index)))

                        def guarded_gpu_results():
                            yielded = False
                            try:
                                for result in gpu_results():
                                    yielded = True
                                    yield result
                            except Exception as exc:
                                if args.device == "gpu" or yielded:
                                    raise
                                gpu_fields.clear()
                                try:
                                    import cupy as cp
                                    cp.get_default_memory_pool().free_all_blocks()
                                except Exception:
                                    pass
                                emit("warning", message=f"GPU启动失败，自动切换CPU继续：{type(exc).__name__}: {exc}")
                                with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
                                    yield from cpu_results(pool)

                        result_iter = guarded_gpu_results()
                    else:
                        executor = ThreadPoolExecutor(max_workers=max(1, args.threads))
                        result_iter = cpu_results(executor)
                    for (base_id, cases, stop_code, stop_label, fs_code, fs_label,
                         direction, session, hard_min, ov_code, ov_label,
                         cooldown, position_filter, position_label, metrics) in result_iter:
                        base_done += 1
                        c4, c1, c15, c5, c1m = cases
                        actual_entry_mode = B.effective_entry_mode(cases, entry_mode)
                        y = metrics["年度收益"]
                        y2025 = float(y[list(B.YEAR_VALUES).index(2025)]) if 2025 in B.YEAR_VALUES else 0.0
                        y2026 = float(y[list(B.YEAR_VALUES).index(2026)]) if 2026 in B.YEAR_VALUES else 0.0
                        finals = metrics["期末资金倍数"] * initial_capital
                        returns = metrics["期末资金倍数"] - 1.0
                        end_quantities = np.minimum(
                            initial_capital * metrics["期末资金倍数"] * selected_sizes / float(B.CLOSE[-1]),
                            maximum_order_eth,
                        )
                        per_size = [B.metrics_for_size(metrics, i) for i in range(len(selected_sizes))]
                        account_records = []
                        for account in per_size:
                            year_returns = dict(zip(B.YEAR_VALUES, account["年度收益"]))
                            account_records.append({
                                "交易次数（单）": account["交易次数"], "胜率（%）": account["胜率"],
                                "多单占比（%）": account["多单占比"],
                                "平均日完整交易数（次/日）": account["平均日完整交易数"],
                                "平均日成交订单数（笔/日）": account["平均日成交订单数"],
                                "平均持仓时间（分钟）": account["平均持仓分钟"],
                                "平均单笔收益率（%）": account["平均单笔收益率"],
                                "毛收益合计（%）": account["毛收益合计"], "盈亏比（倍）": account["盈亏比"],
                                "t值": account["t值"], "2025毛收益（%）": year_returns.get(2025, 0.0),
                                "2026毛收益（%）": year_returns.get(2026, 0.0),
                                "实际止盈等待总时间（分钟）": account["实际止盈等待总分钟"],
                                "实际容量占用率（%）": account["实际容量占用率"],
                                CAPACITY_TIMESTAMP: capacity_timestamp(account.get("首次达到开仓上限索引", -2), B.E.D["ct1"]),
                            })
                        csv_row = [
                            tp.编号, cooldown, base_id, 0 if field == "hist" else 1, c4, c1, c15, c5, c1m, actual_entry_mode, stop_code, fs_code, ov_code, direction, session, hard_min,
                            metrics["交易次数"], metrics["胜率"], metrics["多单占比"],
                            metrics["平均日完整交易数"], metrics["平均日成交订单数"],
                            metrics["平均持仓分钟"], metrics["平均单笔收益率"], metrics["毛收益合计"],
                            metrics["盈亏比"], metrics["t值"], y2025, y2026,
                            ";".join(size_label(x) for x in selection["仓位倍数"]),
                            ";".join(f"{float(x):.8e}" for x in finals),
                            ";".join(f"{float(x):.8e}" for x in returns),
                            ";".join(f"{float(x):.8e}" for x in metrics["最大回撤"]),
                            ";".join(str(int(x)) for x in metrics["爆仓保护次数"]),
                            ";".join(str(int(x)) for x in metrics["全仓强平次数"]),
                            entry_slippage, exit_slippage, roundtrip_slippage,
                            calculation_version, encode_accounts(account_records),
                            initial_capital, minimum_order_eth, maximum_order_eth,
                            ";".join(str(int(x)) for x in metrics["实际成交次数"]),
                            ";".join(str(int(x)) for x in metrics["资金性停机标记"]),
                            ";".join(f"{float(x):.8e}" for x in end_quantities),
                            per_size[0]["实际止盈等待总分钟"], per_size[0]["实际容量占用率"],
                            int(enforce_minimum_order), fill_mode, *execution_columns,
                            position_filter, position_label,
                        ]
                        batch_rows.append(csv_row)
                        for size_i, mult in enumerate(selection["仓位倍数"]):
                            if not legacy_rankings:
                                break
                            actual = per_size[size_i]
                            actual_years = dict(zip(B.YEAR_VALUES, actual["年度收益"]))
                            final_money = float(metrics["期末资金倍数"][size_i] * initial_capital)
                            row = [
                                tp.编号, tp.类别, tp.周期组合, tp.指标, tp.参数一, tp.参数二, tp.参数三,
                                cooldown, base_id, "MACD柱" if field == "hist" else "DIF线",
                                case_label(c4), case_label(c1), case_label(c15), case_label(c5), case_label(c1m), actual_entry_mode,
                                stop_code, stop_label, fs_code, fs_label, ov_code, ov_label,
                                direction, session, hard_min,
                                size_label(mult), mult, actual["交易次数"], actual["胜率"],
                                actual["多单占比"], actual["平均日完整交易数"], actual["平均日成交订单数"], actual["平均持仓分钟"],
                                actual["平均单笔收益率"], actual["毛收益合计"], actual["盈亏比"], actual["t值"],
                                final_money, final_money / initial_capital - 1.0, float(metrics["最大回撤"][size_i]),
                                int(metrics["爆仓保护次数"][size_i]),
                                int(metrics["全仓强平次数"][size_i]), actual_years.get(2025, 0.0), actual_years.get(2026, 0.0),
                                entry_slippage, exit_slippage, roundtrip_slippage,
                                calculation_version,
                                initial_capital, minimum_order_eth, maximum_order_eth,
                                int(metrics["实际成交次数"][size_i]),
                                int(metrics["资金性停机标记"][size_i]), float(end_quantities[size_i]),
                                actual["实际止盈等待总分钟"], actual["实际容量占用率"],
                                int(enforce_minimum_order), fill_mode, *execution_columns,
                                position_filter, position_label,
                                account_records[size_i][CAPACITY_TIMESTAMP],
                            ]
                            heap_counter += 1
                            update_top(category_heap, row, heap_counter)
                            worst_counter += 1
                            update_worst(category_worst_heap, row, worst_counter)
                        if len(batch_rows) >= 4400:
                            writer.writerows(batch_rows); checkpoint["rows"] += len(batch_rows); batch_rows.clear()
                        report_scan_progress("inner_progress", tp_pos, base_done)
                        if stop_requested(): break
                finally:
                    if result_iter is not None:
                        result_iter.close()
                    if executor is not None:
                        executor.shutdown(wait=True, cancel_futures=True)
                if args.limit_base > 0 and base_done >= args.limit_base: break
                if stop_requested():
                    方案已跑完 = False
                    break

                保存断点(tp_pos - 1, 开仓批位置=batch_index+1, 已算基础数=base_done)
                report_scan_progress("inner_progress", tp_pos, base_done, force=True)

            if stop_requested():
                # 跑完了才落盘；没跑完就整块丢掉，续跑时按 csv_bytes 截回上次
                # 落盘点重算这个方案，不会写出半截或重复的行。
                if 方案已跑完:
                    保存断点(tp_pos, 强制=True)
                emit("stopped", next_tp=tp_pos + (1 if 方案已跑完 else 0)); break
            保存断点(tp_pos, 强制=(tp_pos == _sequence_count(tps) - 1))
            report_scan_progress("progress", tp_pos, base_done, force=True)
        else:
            if candidate_settings["启用"] and candidate_settings["自动导出"]:
                emit("stage", message="回测完成，开始按硬门槛、评分、去重和分层规则生成候选")
                export_candidates(project_dir, output_dir, candidate_settings)
            if legacy_rankings:
                if top_json.exists() and worst_json.exists():
                    export_excel(project_dir, output_dir, top_json, f"各类止盈最优前{最优排行名额}名.xlsx")
                    export_excel(project_dir, output_dir, worst_json, "各类止盈最差1000名.xlsx")
                else:
                    emit("stage", message=f"断点中没有历史排行，直接扫描已有CSV补建最优前{最优排行名额}/最差1000，不重新回测")
                    rebuild_legacy_rankings(project_dir, output_dir, ranking_settings_path)
            emit("completed", rows=checkpoint["rows"], csv=str(result_csv))
    finally:
        if prepared_tps is not None:
            prepared_tps.close()
        if tp_executor is not None:
            tp_executor.shutdown(wait=True, cancel_futures=True)
        csv_file.close()


def main():
    from fifth_model_audit import set_output_directory, close_archives
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output")
    known, _ = parser.parse_known_args()
    if known.output:
        with exclusive_output(Path(known.output).resolve()):
            set_output_directory(Path(known.output).resolve() / "第五轮模型审计")
            try:
                return _main()
            finally:
                close_archives()
    return _main()


if __name__ == "__main__":
    从命令行读取排行设置(sys.argv)
    main()
