"""无人值守的、只调用离线 backtest_worker 的回测调度器。

python campaign_runner.py init --settings settings.json --root /data/backtest/run01
python campaign_runner.py run --root /data/backtest/run01
python campaign_runner.py status --root /data/backtest/run01

init 只接受新目录。源代码、数据、参数变更必须新建任务；run 不使用 --restart。
默认单线程、72 小时壁钟预算、至少保留 15 GiB 空闲和 900 MiB 可用内存。
达到资源边界只暂停自己的 worker，不删除其他数据或停止其他服务。已完成 CSV
经过 gzip 回读 SHA256 校验才移除原 CSV；所有亏损、零交易结果均保留。
时间段复核不是未见过数据的样本外验证。本程序不连接交易所、不下单。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from run_safety import atomic_json, exclusive_output
from platform_support import macos_available_memory

GIB = 1024 ** 3
MIB = 1024 ** 2
TERMINAL = {"completed", "skipped", "failed", "deferred_budget"}
DEFAULTS = {
    "wall_hours": 72, "min_free_gib": 15, "max_root_gib": 12,
    "start_memory_mib": 900, "stop_memory_mib": 700,
    "max_jobs": 2000, "max_retries": 2, "poll_seconds": 5,
    "resource_retry_seconds": 60, "failure_retry_seconds": 60,
    "stop_grace_seconds": 300, "log_max_mib": 4,
}


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def source_identity(source_dir: Path) -> dict:
    paths = sorted(source_dir.glob("*.py"))
    paths += [source_dir / name for name in ("requirements.txt", "AGENTS.md", "rank_color_scales.json", "strategy_display.json")
              if (source_dir / name).is_file()]
    return {path.name: digest_file(path) for path in paths}


def runtime_identity() -> dict:
    packages = {}
    for name in ("numpy", "pandas", "numba", "llvmlite", "scipy", "scikit-learn",
                 "hmmlearn", "pyarrow", "threadpoolctl"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def data_identity(settings: dict) -> tuple[dict, str]:
    """与 data_sources.source_bundle_fingerprint 的真实文件口径一致。"""
    identities, parts = {}, []
    for field, key in (("csv", "kline"), ("micro_csv", "micro"),
                       ("funding", "funding"), ("oi", "oi"), ("bundle", "bundle")):
        raw = settings.get(field, "")
        if not raw:
            parts.append((key, ""))
            continue
        path = Path(raw).resolve(strict=True)
        stat = path.stat()
        if not path.is_file():
            raise ValueError(f"数据源必须是文件：{path}")
        value = (key, str(path), stat.st_size, stat.st_mtime_ns, digest_file(path))
        parts.append(value)
        identities[field] = value[1:]
    # bundle 需要额外解包及解析后的各源身份；不把原 ZIP 身份误传给 worker。
    if settings.get("bundle"):
        raise ValueError("服务器任务请先解包，并明确提供 csv/micro_csv/funding/oi；不接受隐式 bundle")
    fingerprint = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
    return identities, fingerprint


def task_path(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts).resolve()
    if path != root and root not in path.parents:
        raise ValueError("任务路径超出本任务目录")
    return path


def read_json(path: Path):
    # Windows atomic replacement can briefly deny the reader with errno 13.
    for attempt in range(11):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except PermissionError:
            if os.name != "nt" or attempt == 10:
                raise
            time.sleep(.02)


def directory_bytes(root: Path) -> int:
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


def available_memory() -> int:
    if sys.platform == "darwin":
        return macos_available_memory()
    if sys.platform.startswith("linux"):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
        raise RuntimeError("无法读取 MemAvailable，不能安全启动任务")
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in (
                    "total", "available", "total_page", "available_page",
                    "total_virtual", "available_virtual", "extended")]

        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise RuntimeError("无法读取可用内存")
        return memory.available
    raise RuntimeError("本调度器仅支持 Windows/macOS/Linux 的可用内存核验")


def resource_snapshot(root: Path) -> dict:
    return {"free_bytes": shutil.disk_usage(root).free,
            "root_bytes": directory_bytes(root), "memory_bytes": available_memory()}


def resource_reason(snapshot: dict, settings: dict, running=False) -> str:
    if snapshot["free_bytes"] < float(settings["min_free_gib"]) * GIB:
        return "磁盘剩余空间不足，保留其他程序空间"
    if snapshot["root_bytes"] >= float(settings["max_root_gib"]) * GIB:
        return "本任务磁盘预算已满（含缓存），等待人工归档或新任务"
    memory_key = "stop_memory_mib" if running else "start_memory_mib"
    if snapshot["memory_bytes"] < float(settings[memory_key]) * MIB:
        return "可用内存不足，等待其他程序负载下降"
    return ""


class RotatingLog:
    """只轮换此任务内固定的两份日志；不轮换或清理其他程序日志。"""
    def __init__(self, path: Path, maximum: int):
        self.path, self.maximum = path, maximum

    def write(self, text: str):
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > self.maximum:
            encoded = encoded[-self.maximum:]
        size = self.path.stat().st_size if self.path.exists() else 0
        if size + len(encoded) > self.maximum:
            os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        with self.path.open("ab") as handle:
            handle.write(encoded)


def verify_archive(path: Path, expected: str | None = None) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for block in iter(lambda: handle.read(MIB), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if expected is not None and actual != expected:
        raise RuntimeError(f"已完成结果内容校验失败：{path}")
    return actual


def archive_csv(output: Path, event: dict) -> dict:
    """幂等提交：先校验压缩包，写完成凭证，最后只移除本 job 的原 CSV。"""
    raw = output / "全部回测结果.csv"
    archive = output / "全部回测结果.csv.gz"
    receipt_path = output / "完成凭证.json"
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        verify_archive(archive, receipt["content_sha256"])
        if raw.exists():
            if digest_file(raw) != receipt["content_sha256"]:
                raise RuntimeError("原 CSV 与已完成压缩包冲突，保留原文件并停止")
            raw.unlink()
        return receipt
    if not raw.is_file():
        raise RuntimeError("worker 声称 completed 但缺少完整 CSV，拒绝标记完成")
    expected = digest_file(raw)
    temporary = output / ".全部回测结果.csv.gz.pending"
    with raw.open("rb") as source, temporary.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
            shutil.copyfileobj(source, compressed, length=MIB)
        target.flush()
        os.fsync(target.fileno())
    verify_archive(temporary, expected)
    os.replace(temporary, archive)
    rows = 0
    with gzip.open(archive, "rt", encoding="utf-8-sig", newline="") as handle:
        for _ in csv.DictReader(handle):
            rows += 1
    if event.get("rows") is not None and rows != int(event["rows"]):
        raise RuntimeError(f"completed 行数 {event['rows']} 与 CSV 实际 {rows} 不一致")
    receipt = {"archive": archive.name, "content_sha256": expected,
               "archive_sha256": digest_file(archive), "raw_bytes": raw.stat().st_size,
               "archive_bytes": archive.stat().st_size, "row_count": rows,
               "completed_at": time.time()}
    atomic_json(receipt_path, receipt)
    raw.unlink()
    return receipt


def load_results(output: Path, maximum=512) -> list[dict]:
    rows = []
    with gzip.open(output / "全部回测结果.csv.gz", "rt", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(row)
            if len(rows) > maximum:
                raise RuntimeError(f"单 job 超过 {maximum} 行，拒绝截断候选；请新建有界计划")
    return rows


class PlannerJob(dict):
    """只对 planner 真正读取的候选载入全量 CSV；不把全部历史放入常驻内存。"""
    def __init__(self, job, root):
        super().__init__(job)
        self.root = root

    def get(self, key, default=None):
        if key == "results" and self.get("state") == "completed" and key not in self:
            return load_results(task_path(self.root, "jobs", self["id"]))
        return super().get(key, default)

    def __getitem__(self, key):
        if key == "results" and key not in self:
            return self.get(key, [])
        return super().__getitem__(key)


def validate_job(job: dict) -> dict:
    job = json.loads(json.dumps(job, ensure_ascii=False, allow_nan=False))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", str(job.get("id", ""))):
        raise ValueError("job id 仅允许短 ASCII 字母、数字、横线、下划线")
    if not isinstance(job.get("selection"), dict) or not job.get("phase"):
        raise ValueError("job 缺少 phase 或 selection")
    candidate = job["selection"].setdefault("候选筛选", {})
    candidate.update({"启用": False, "自动导出": False, "导出旧排行": False})
    # 输出控制不改变交易公式；固定下来避免每个小任务重复导出巨型 Excel。
    job.update(state="queued", failures=0, launches=0, not_before=0)
    job["spec_sha256"] = job_spec_hash(job)
    return job


def job_spec_hash(job: dict) -> str:
    return canonical_hash({key: job.get(key) for key in ("id", "phase", "selection", "start", "end", "tags")})


def validate_settings(raw: dict, base_dir: Path | None = None) -> dict:
    settings = dict(DEFAULTS)
    settings.update(raw)
    if not settings.get("csv"):
        raise ValueError("settings.csv 必填")
    for field in ("csv", "micro_csv", "funding", "oi", "bundle"):
        if settings.get(field):
            path = Path(settings[field])
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            settings[field] = str(path.resolve(strict=True))
    for key in DEFAULTS:
        value = float(settings[key])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"资源或预算设置无效：{key}")
        if key in ("max_jobs", "max_retries"):
            if value != int(value):
                raise ValueError(f"{key} 必须为整数")
            settings[key] = int(value)
        else:
            settings[key] = value
    if not 1 <= int(settings["max_jobs"]) <= 10000:
        raise ValueError("max_jobs 必须在 1..10000")
    if not 0 <= int(settings["max_retries"]) <= 2:
        raise ValueError("最多允许失败后重试两次")
    if settings["wall_hours"] <= 0 or settings["max_root_gib"] <= 0:
        raise ValueError("wall_hours 和 max_root_gib 必须大于零")
    if settings["poll_seconds"] <= 0 or settings["log_max_mib"] <= 0:
        raise ValueError("poll_seconds 和 log_max_mib 必须大于零")
    settings.setdefault("phase_end_hours", {phase: settings["wall_hours"] * ratio for phase, ratio in
                                            (("coarse", .5), ("bucket", 2 / 3), ("refine", 5 / 6), ("validation", 1))})
    if not isinstance(settings["phase_end_hours"], dict):
        raise ValueError("phase_end_hours 必须为阶段到累计小时数的映射")
    for phase, end_hours in settings["phase_end_hours"].items():
        if not math.isfinite(float(end_hours)) or not 0 < float(end_hours) <= settings["wall_hours"]:
            raise ValueError(f"阶段 {phase} 截止时间必须在整个壁钟预算内")
        settings["phase_end_hours"][phase] = float(end_hours)
    ordered = [settings["phase_end_hours"][phase] for phase in ("coarse", "bucket", "refine", "validation")
               if phase in settings["phase_end_hours"]]
    if ordered != sorted(ordered):
        raise ValueError("阶段截止时间必须递增，给最后时间段复核保留资源")
    return settings


def initialize(settings_path: Path, root: Path, source_dir: Path | None = None, planner=None) -> dict:
    if root.exists():
        raise ValueError("init 只接受不存在的新任务目录；旧任务请使用 run")
    settings = validate_settings(read_json(settings_path), settings_path.resolve().parent)
    source_dir = (source_dir or Path(__file__).parent).resolve()
    data, fingerprint = data_identity(settings)
    if planner is None:
        import campaign_plan as planner
    jobs = [validate_job(job) for job in planner.initial_jobs(settings)]
    if len(jobs) > int(settings["max_jobs"]):
        raise ValueError("初始计划超过 max_jobs，未创建任务")
    if len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("初始计划含重复 job id")
    identity = {"source_dir": str(source_dir), "source_files": source_identity(source_dir),
                "data": data, "source_fingerprint": fingerprint,
                "settings_hash": canonical_hash(settings), "batch_mb": 16, "threads": 1,
                "runtime": runtime_identity()}
    root.mkdir(parents=True)
    for directory in ("jobs", "cache", "tmp"):
        (root / directory).mkdir()
    atomic_json(root / "settings.json", settings)
    atomic_json(root / "identity.json", identity)
    atomic_json(root / "catalog.json", planner.make_catalog())
    state = {"version": 1, "created_at": time.time(), "started_at": None,
             "deadline": None, "status": "ready", "jobs": jobs, "reason": ""}
    atomic_json(root / "state.json", state)
    write_summary(root, state)
    write_analysis(root, settings, state, planner)
    return state


def verify_identity(root: Path, settings: dict, identity: dict):
    if canonical_hash(settings) != identity["settings_hash"]:
        raise RuntimeError("冻结参数已变化，禁止混跑；请新建任务")
    if source_identity(Path(identity["source_dir"])) != identity["source_files"]:
        raise RuntimeError("程序源码已变化，禁止混跑；请使用冻结版本或新建任务")
    if runtime_identity() != identity["runtime"]:
        raise RuntimeError("Python/数值依赖版本已变化，禁止混跑；请恢复冻结环境或新建任务")
    data, fingerprint = data_identity(settings)
    # JSON 把 tuple 转 list；统一 canonical 后比较。
    if canonical_hash(data) != canonical_hash(identity["data"]) or fingerprint != identity["source_fingerprint"]:
        raise RuntimeError("原始数据或文件时间已变化，禁止混跑；请新建任务")


def write_summary(root: Path, state: dict):
    counts = Counter(job["state"] for job in state["jobs"])
    phases = {}
    for job in state["jobs"]:
        phases.setdefault(str(job["phase"]), Counter())[job["state"]] += 1
    summary = {"status": state["status"], "reason": state.get("reason", ""),
               "updated_at": time.time(), "started_at": state.get("started_at"),
               "deadline": state.get("deadline"), "registered_jobs": len(state["jobs"]),
               "counts": dict(counts), "phases": {k: dict(v) for k, v in phases.items()},
               "resources": state.get("resources", {}),
               "result_rows": sum(job.get("row_count", 0) for job in state["jobs"]),
               "coverage_note": "分层覆盖，不是全部笛卡尔积；缺数据/失败/排队不计入实际测过。历史时间段复核不宣称真正样本外。",
               "unmeasured": [{"id": job["id"], "phase": job["phase"], "state": job["state"],
                               "reason": job.get("reason", "")} for job in state["jobs"]
                              if job["state"] != "completed"]}
    atomic_json(root / "summary.json", summary)
    lines = ["# 服务器回测进度", "", f"状态：{state['status']}。{state.get('reason', '')}", "",
             f"登记任务 {len(state['jobs'])}；实际完成 {counts['completed']}；缺数据跳过 {counts['skipped']}；"
             f"失败 {counts['failed']}；阶段预算延期 {counts['deferred_budget']}；待运行 {counts['queued']}；运行中 {counts['running']}。",
             f"已保存完整 CSV 结果 {summary['result_rows']} 行（含亏损和零交易，gzip 无损保存）。", "",
             summary["coverage_note"], "", "| 阶段 | 完成 | 跳过 | 失败 | 未完成 |", "|---|---:|---:|---:|---:|"]
    for phase, values in phases.items():
        lines.append(f"| {phase} | {values['completed']} | {values['skipped']} | {values['failed']} | {values['queued'] + values['running'] + values['deferred_budget']} |")
    lines += ["", "settings.json：冻结参数；identity.json：源码及数据身份；catalog.json：计划目录；",
              "state.json：每项实际状态及跳过原因；jobs/<id>/完成凭证.json：可验证的完成凭证。",
              "达到时间/空间预算时保留断点和未测清单，不把预算到期写成完成。", ""]
    temporary = root / ".进度汇总.md.pending"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, root / "进度汇总.md")


def write_analysis(root: Path, settings: dict, state: dict, planner):
    if hasattr(planner, "analysis_report"):
        analysis = planner.analysis_report(settings, [PlannerJob(job, root) for job in state["jobs"]])
    else:
        analysis = {"conclusion": "当前仅有运行记录，尚未生成研究比较；不宣称找到最佳策略",
                    "training_candidates": [], "validation_candidates": [], "limitations": []}
    analysis.update(campaign_status=state["status"], updated_at=time.time(),
                    campaign_reason=state.get("reason", ""))
    atomic_json(root / "analysis.json", analysis)
    lines = ["# 回测研究结果", "", analysis["conclusion"], "",
             f"调度状态：{state['status']}。{state.get('reason', '')}", "",
             "## 训练阶段候选", "",
             "两训练窗口、两种独立成本情景全部达到预设门槛后才列入；不是实盘推荐。", "",
             "| 候选 | 规则族 | 评分 | 四种情景最差收益 | 最大回撤 |",
             "|---|---|---:|---:|---:|"]
    for item in analysis.get("training_candidates", [])[:10]:
        lines.append(f"| {item['id']} | {item['family']} | {item['score']:.4f} | "
                     f"{item['worst_return']:.2%} | {item['max_drawdown']:.2%} |")
    if not analysis.get("training_candidates"):
        lines += ["", "当前没有符合全部门槛的候选；缺数据、样本不足、未完成或不盈利都不会被强行选成赢家。"]
    lines += ["", "## 冻结时间段复核", "", "| 候选 | 状态 | 预设时间复核门槛 | 未通过原因 |",
              "|---|---|---|---|"]
    for item in analysis.get("validation_candidates", [])[:10]:
        verdict = "通过描述性门槛" if item["meets_prespecified_time_check"] else "未通过/未完成"
        lines.append(f"| {item.get('candidate_id', item['job_id'])} | {item['state']} | {verdict} | "
                     f"{'；'.join(item['reasons']) or '—'} |")
    if not analysis.get("validation_candidates"):
        lines += ["", "尚无冻结时间段复核结果。"]
    lines += ["", "## 口径及限制", ""]
    lines += [f"- {item}" for item in analysis.get("limitations", [])]
    lines += ["", "完整候选参数、各训练/复核情景数据、未通过原因和覆盖清单见 analysis.json；",
              "完整原始结果含所有亏损与零交易记录，保存在 jobs 内经 SHA256 校验的 gzip CSV。",
              "本文仅展示前十项，不代表已经穷举全笛卡尔积，也不证明存在可实盘获利策略。", ""]
    temporary = root / ".研究结果汇总.md.pending"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, root / "研究结果汇总.md")


def worker_command(root: Path, settings: dict, identity: dict, job: dict) -> list[str]:
    if job.get("spec_sha256") != job_spec_hash(job):
        raise RuntimeError("已冻结 job 定义发生变化，禁止混跑")
    output = task_path(root, "jobs", job["id"])
    output.mkdir(exist_ok=True)
    selection_path = output / "selection.json"
    if selection_path.exists() and read_json(selection_path) != job["selection"]:
        raise RuntimeError("已落盘 job 配置发生变化，禁止继续")
    atomic_json(selection_path, job["selection"])
    command = [sys.executable, "-u", str(Path(identity["source_dir"]) / "backtest_worker.py"),
               "--csv", settings["csv"], "--output", str(output), "--threads", "1", "--device", "cpu",
               "--selection", str(selection_path), "--cache-root", str(root / "cache"),
               "--expected-source-fingerprint", identity["source_fingerprint"]]
    for field, flag in (("micro_csv", "--micro-csv"), ("funding", "--funding"), ("oi", "--oi")):
        if settings.get(field):
            command += [flag, settings[field]]
    for field in ("start", "end"):
        if job.get(field):
            command += ["--" + field, str(job[field])]
    return command


def worker_environment(root: Path) -> dict:
    env = dict(os.environ)
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
        env[key] = "1"
    env.update(BT_ENTRY_BATCH_MB="16", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(root / "tmp")
    for key, child in (("NUMBA_CACHE_DIR", "numba"), ("CUPY_CACHE_DIR", "cupy")):
        path = root / "tmp" / child
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    env["JOBLIB_TEMP_FOLDER"] = str(root / "tmp")
    return env


def terminate_worker_tree(process: subprocess.Popen):
    """仅结束本次 Popen 创建的独立进程组，包含模型训练子进程。"""
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # 即使父进程已经退出，模型子进程也可能尚在组内。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    else:
        # 明确 PID 的本次子进程树；绝不按 python.exe 等名字终止进程。
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if process.poll() is None:
            process.kill()
        process.wait()


class CampaignRunner:
    def __init__(self, root: Path, planner=None, resources=resource_snapshot, clock=time.time):
        self.root = root.resolve()
        self.settings = read_json(self.root / "settings.json")
        self.identity = read_json(self.root / "identity.json")
        self.state = read_json(self.root / "state.json")
        if planner is None:
            import campaign_plan as planner
        self.planner, self.resources, self.clock = planner, resources, clock
        self.stop = threading.Event()
        self.log = RotatingLog(self.root / "runner.log", int(self.settings["log_max_mib"] * MIB))

    def save(self):
        atomic_json(self.root / "state.json", self.state)
        write_summary(self.root, self.state)
        if hasattr(self.planner, "coverage_report"):
            atomic_json(self.root / "coverage.json", self.planner.coverage_report(self.settings, self.state["jobs"]))

    def request_stop(self, *_):
        self.stop.set()

    def recover(self):
        for job in self.state["jobs"]:
            if job.get("spec_sha256") != job_spec_hash(job):
                raise RuntimeError("已登记 job 定义校验失败，禁止续跑")
            output = task_path(self.root, "jobs", job["id"])
            receipt_path = output / "完成凭证.json"
            event_path = output / "worker_completed.json"
            if receipt_path.exists() or event_path.exists():
                receipt = archive_csv(output, read_json(event_path) if event_path.exists() else {})
                job.update(state="completed", **receipt, reason="")
            elif job["state"] == "completed":
                raise RuntimeError(f"{job['id']} 缺少可验证完成凭证")
            elif job["state"] == "running":
                job.update(state="queued", reason="调度进程中断，按 worker 断点恢复")
        self.save()

    def expired(self):
        return self.state.get("deadline") is not None and self.clock() >= self.state["deadline"]

    def phase_expired(self, phase):
        hours = self.settings.get("phase_end_hours", {}).get(phase)
        return hours is not None and self.state.get("started_at") is not None and self.clock() >= (
            self.state["started_at"] + float(hours) * 3600)

    def defer_expired_phases(self):
        changed = False
        for job in self.state["jobs"]:
            if job["state"] == "queued" and self.phase_expired(job["phase"]):
                job.update(state="deferred_budget", reason="阶段时间预算已到；保留断点但未完成，为后续筛选/时间复核留时")
                changed = True
        if changed:
            self.save()

    def _read_output(self, pipe, events, log):
        try:
            for line in iter(pipe.readline, ""):
                log.write(line)
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        # 只保留状态事件；进度全文已在有限轮转日志中，不积压无界队列。
                        if event.get("type") in ("completed", "stopped", "skipped"):
                            events.put(event)
                except (ValueError, TypeError):
                    pass
        finally:
            pipe.close()

    def execute(self, job: dict) -> tuple[str, dict]:
        output = task_path(self.root, "jobs", job["id"])
        command = worker_command(self.root, self.settings, self.identity, job)
        events = queue.Queue()
        log = RotatingLog(output / "worker.log", int(self.settings["log_max_mib"] * MIB))
        process = subprocess.Popen(command, cwd=self.identity["source_dir"], env=worker_environment(self.root),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding="utf-8", errors="replace", bufsize=1,
                                   start_new_session=os.name != "nt")
        job["worker_pid"] = process.pid
        self.save()
        reader = threading.Thread(target=self._read_output, args=(process.stdout, events, log), daemon=True)
        reader.start()
        stopping_at, stop_reason, terminal = None, "", None
        try:
            while process.poll() is None:
                while not events.empty():
                    terminal = events.get_nowait()
                reason = "用户或服务请求停止" if self.stop.is_set() else ""
                if self.expired():
                    reason = "72小时/配置壁钟预算已到，保留未完成任务"
                elif self.phase_expired(job["phase"]):
                    reason = "本阶段时间预算已到，保存断点并为后续阶段留时"
                if not reason:
                    snapshot = self.resources(self.root)
                    self.state["resources"] = snapshot
                    reason = resource_reason(snapshot, self.settings, running=True)
                if reason and stopping_at is None:
                    stopping_at, stop_reason = self.clock(), reason
                    job["reason"] = reason
                    self.state.update(status="checkpointing", reason=reason)
                    self.save()
                if stopping_at is not None:
                    # worker 启动阶段会清掉旧 flag，因此直到退出前持续重写自己的 flag。
                    control = output / "控制"
                    control.mkdir(exist_ok=True)
                    (control / "停止.flag").touch()
                    if self.clock() - stopping_at >= self.settings["stop_grace_seconds"]:
                        terminate_worker_tree(process)
                        break
                time.sleep(self.settings["poll_seconds"])
            process.wait()
        except BaseException:
            # 调度器异常时不留下脱管的后台计算，也绝不按名字杀别人的进程。
            if process.poll() is None:
                terminate_worker_tree(process)
            raise
        finally:
            reader.join(timeout=1)
            if reader.is_alive() or (process.poll() is not None and process.returncode != 0):
                # 父 worker 异常结束后，模型子进程可能仍持有 stdout 管道。
                terminate_worker_tree(process)
                reader.join(timeout=10)
            job.pop("worker_pid", None)
        while not events.empty():
            terminal = events.get_nowait()
        if terminal and terminal["type"] == "completed" and process.returncode == 0:
            # 持久化事件后再压缩：崩溃发生于任一步都可安全补交，不重新模拟。
            atomic_json(output / "worker_completed.json", terminal)
            return "completed", archive_csv(output, terminal)
        if terminal and terminal["type"] == "skipped" and process.returncode == 0:
            return "skipped", {"reason": terminal.get("message") or terminal.get("reason", "缺少数据能力")}
        if stopping_at is not None or (terminal and terminal["type"] == "stopped"):
            return "queued", {"reason": stop_reason or "worker 已保存停止断点", "checkpoint_confirmed": bool(
                terminal and terminal["type"] == "stopped"),
                "not_before": self.clock() + self.settings["resource_retry_seconds"]}
        return "failure", {"reason": f"worker 异常退出或缺少完成事件，exit={process.returncode}"}

    def run(self) -> dict:
        with exclusive_output(self.root):
            verify_identity(self.root, self.settings, self.identity)
            self.recover()
            if self.state["started_at"] is None:
                self.state["started_at"] = self.clock()
                self.state["deadline"] = self.clock() + self.settings["wall_hours"] * 3600
            self.save()
            while not self.stop.is_set() and not self.expired():
                self.defer_expired_phases()
                if all(job["state"] in TERMINAL for job in self.state["jobs"]):
                    write_analysis(self.root, self.settings, self.state, self.planner)
                    existing = {job["id"] for job in self.state["jobs"]}
                    proposed = list(self.planner.advance_jobs(self.settings, [
                        PlannerJob(job, self.root) for job in self.state["jobs"]]))
                    new = []
                    for job in proposed:
                        if job["id"] not in existing:
                            new.append(validate_job(job))
                            existing.add(job["id"])
                    if len(self.state["jobs"]) + len(new) > self.settings["max_jobs"]:
                        self.state.update(status="budget_jobs", reason="达到登记任务数量预算，后续阶段未运行")
                        break
                    if not new:
                        if len(self.state["jobs"]) >= self.settings["max_jobs"] and not any(
                                job["phase"] == "validation" for job in self.state["jobs"]):
                            self.state.update(status="budget_jobs", reason="登记任务数达到预算；没有执行的后续阶段仍属未测")
                            break
                        failed = any(job["state"] != "completed" for job in self.state["jobs"])
                        self.state.update(status="finished_with_gaps" if failed else "finished_plan",
                                          reason="分层计划已结束；不是全部笛卡尔积。缺数据和失败见未测清单。")
                        break
                    self.state["jobs"].extend(new)
                    self.save()
                snapshot = self.resources(self.root)
                reason = resource_reason(snapshot, self.settings)
                self.state["resources"] = snapshot
                if reason:
                    self.state.update(status="paused_resource", reason=reason)
                    self.save()
                    self.stop.wait(min(self.settings["resource_retry_seconds"], max(.01, self.state["deadline"] - self.clock())))
                    continue
                job = next((job for job in self.state["jobs"] if job["state"] == "queued"
                            and job.get("not_before", 0) <= self.clock()), None)
                if job is None:
                    self.stop.wait(self.settings["poll_seconds"])
                    continue
                verify_identity(self.root, self.settings, self.identity)
                self.state.update(status="running", reason="")
                job.update(state="running", started_at=self.clock(), launches=job.get("launches", 0) + 1)
                self.save()
                try:
                    status, details = self.execute(job)
                except Exception as exc:
                    status, details = "failure", {"reason": f"{type(exc).__name__}: {exc}"}
                if status == "failure":
                    job["failures"] += 1
                    status = "failed" if job["failures"] > self.settings["max_retries"] else "queued"
                    details["not_before"] = self.clock() + self.settings["failure_retry_seconds"] * 2 ** (job["failures"] - 1)
                job.update(details, state=status, last_finished_at=self.clock())
                self.log.write(json.dumps({"job": job["id"], "state": status, "reason": job.get("reason", ""),
                                           "at": self.clock()}, ensure_ascii=False) + "\n")
                self.save()
            if self.expired():
                self.state.update(status="budget_time", reason="壁钟预算到期，未完成任务保留；没有宣称全部跑完")
            elif self.stop.is_set():
                self.state.update(status="stopped", reason="已停止本任务，保留已提交结果和断点")
            self.save()
            write_analysis(self.root, self.settings, self.state, self.planner)
            return self.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run", "status"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name == "init":
            command.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "init":
        state = initialize(args.settings, root)
        print(json.dumps({"status": state["status"], "jobs": len(state["jobs"])}, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(read_json(root / "summary.json"), ensure_ascii=False, indent=2))
    else:
        runner = CampaignRunner(root)
        for name in ("SIGINT", "SIGTERM"):
            signal.signal(getattr(signal, name), runner.request_stop)
        state = runner.run()
        print(json.dumps({"status": state["status"], "reason": state.get("reason", "")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
