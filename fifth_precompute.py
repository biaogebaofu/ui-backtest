"""Compute independent fifth-round methods in isolated, bounded processes."""
from __future__ import annotations

import ctypes
import hashlib
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from platform_support import macos_available_memory

EXTRA_KEYS = ("taker_buy_ratio", "delta_base", "trades", "avg_trade_size",
              "quote_volume", "taker_buy_quote_ratio", "delta_quote", "taker_buy_base",
              "taker_buy_quote", "funding_rate", "open_interest", "open_interest_value",
              "hist", "dif", "dea", "ma120")


class PrecomputeStopped(Exception):
    pass


def available_memory():
    if sys.platform == "darwin":
        return macos_available_memory()
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total_phys", "avail_phys", "total_page", "avail_page",
                 "total_virtual", "avail_virtual", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            # DLL imports and model work need commit headroom even when Windows
            # still reports plenty of unused physical RAM.
            return min(status.avail_phys, status.avail_page)
    else:
        try:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            pass
    return 4 * 1024**3


def worker_limit(requested, task_count, native_rows, free_bytes=None):
    # Shared input files avoid duplicating the full feature bundle. Models still
    # need private work arrays, prediction archives and a Python/Numba runtime.
    free_bytes = available_memory() if free_bytes is None else free_bytes
    per_worker = 768 * 1024**2 + max(0, native_rows) * 512
    budget = max(0, min(int(free_bytes * .75), free_bytes - 2 * 1024**3))
    return max(1, min(max(1, int(requested)), max(1, task_count),
                      os.cpu_count() or 1, max(1, budget // per_worker)))


def load_signals(cache_dir, timeframe, code, native_rows):
    from fifth_policy import require_entry_allowed
    require_entry_allowed(code)
    path = Path(cache_dir) / f"{timeframe}_{code}.npy"
    value = np.load(path, mmap_mode="r", allow_pickle=False)
    if value.dtype != np.bool_ or value.shape != (2, native_rows):
        raise ValueError(f"开仓信号缓存不完整：{timeframe}/{code}")
    return value


def _copy_atomic(source, target):
    """Publish complete files, including when two runs share a cache."""
    target = Path(target)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.unlink()
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_shared(shared, cache_dir, audit_dir, tf, code, rows):
    if shared is None or not (shared / "complete.json").is_file():
        return False
    try:
        value = load_signals(shared, tf, code, rows)
        del value
        files = json.loads((shared / "complete.json").read_text("utf-8"))["audit"]
        if any(not (shared / "audit" / name).is_file() for name in files):
            return False
    except (OSError, ValueError, KeyError, EOFError):
        return False
    for name in files:
        _copy_atomic(shared / "audit" / name, audit_dir / name)
    _copy_atomic(shared / f"{tf}_{code}.npy", cache_dir / f"{tf}_{code}.npy")
    return True


def _publish_shared(shared, task_dir, tf, code):
    if shared is None or shared.exists():
        return
    shared.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publish_", dir=shared.parent) as temporary:
        scratch = Path(temporary).resolve()
        if not scratch.is_relative_to(shared.parent.resolve()):
            raise RuntimeError("共享信号临时目录越界")
        payload = scratch / "task"
        payload.mkdir()
        _copy_atomic(task_dir / "signals.npy", payload / f"{tf}_{code}.npy")
        audit = payload / "audit"
        audit.mkdir()
        names = []
        for source in (task_dir / "audit").glob("*"):
            _copy_atomic(source, audit / source.name)
            names.append(source.name)
        (payload / "complete.json").write_text(json.dumps({"audit": names}), "utf-8")
        try:
            os.rename(payload, shared)
        except OSError:
            if not shared.exists():
                raise


def selected_tasks(options):
    """Expand only method members, never the Cartesian product of strategies."""
    from fifth_batch import FIFTH_SPECS
    from indicator_combinations import TIMEFRAMES, entry_members
    from fifth_policy import require_entry_allowed
    tasks = set()
    for tf, choices in zip(TIMEFRAMES, options):
        codes = set(choices.singles)
        if choices.sizes:
            codes.update(choices.members)
        for code in codes:
            require_entry_allowed(code)
            tasks.update((tf, member) for member in entry_members(code) if member in FIFTH_SPECS)
    return sorted(tasks)


def _initialize_worker():
    # Also limit libraries imported after the worker starts. No nested 18x18 pool.
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ[key] = "1"
    from threadpoolctl import threadpool_limits
    global _thread_limits
    _thread_limits = threadpool_limits(limits=1)


def _compute_task(input_dir, task_dir, timeframe, code, gp_options=None):
    from fifth_batch import fifth_masks
    from fifth_model_audit import set_output_directory, close_archives
    from fifth_learned_models import parallel_gp_context
    inputs = {path.stem: np.load(path, mmap_mode="r", allow_pickle=False)
              for path in (Path(input_dir) / timeframe).glob("*.npy")}
    task_dir = Path(task_dir)
    set_output_directory(task_dir / "audit")
    try:
        # Match extended_entry_data, including missing-key NaN semantics.
        missing = np.full(len(inputs["close"]), np.nan)
        extras = {key: inputs[key] if key in inputs else missing for key in EXTRA_KEYS}
        options = dict(gp_options, audit_dir=str(task_dir / "audit"), timeframe=timeframe) if gp_options else None
        with parallel_gp_context(options):
            masks = fifth_masks(code, None, None, inputs["open"], inputs["high"],
                                inputs["low"], inputs["close"], inputs["volume"],
                                inputs["ct"], extras, timeframe)
        np.save(task_dir / "signals.npy", np.asarray(masks, dtype=np.bool_), allow_pickle=False)
    finally:
        close_archives()
        # The parent also executes GP tasks. Exception tracebacks can retain
        # array views; explicitly release Windows mappings before scratch cleanup.
        for value in inputs.values():
            value._mmap.close()
    return os.getpid()


def prepare_fifth_signals(data, tasks, output_dir, identity, max_workers,
                          report, control_dir=None, shared_cache_dir=None):
    """Parallelize methods, then independent GP refit windows in the parent.

    Cache files are per run and identity. A task is committed only after both
    signal generation and model archive closing succeed. Stop kills this pool
    only; completed methods and completed GP blocks survive for resume.
    """
    from fifth_batch import FIFTH_SPECS
    tasks = sorted(set(tasks))
    from fifth_policy import require_entry_allowed
    for _, code in tasks:
        require_entry_allowed(code)
    # F5 entry signals do not depend on exit/position/cost selections. Reuse only
    # when the exact data request, algorithm code and numerical runtime match.
    keys = ("feature_request", "code_sha256", "engine_version")
    cache_identity = {key: identity[key] for key in keys} if all(key in identity for key in keys) else identity
    from importlib.metadata import version, PackageNotFoundError
    runtime = {}
    for name in ("numpy", "numba", "scipy", "scikit-learn", "hmmlearn"):
        try:
            runtime[name] = version(name)
        except PackageNotFoundError:
            runtime[name] = None
    signature = hashlib.sha256(json.dumps([cache_identity, runtime], sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
    output_dir = Path(output_dir).resolve()
    cache_dir = output_dir / "开仓预计算" / signature
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not tasks:
        return cache_dir
    audit_dir = output_dir / "第五轮模型审计"
    audit_dir.mkdir(exist_ok=True)
    shared_root = Path(shared_cache_dir).resolve() / signature if shared_cache_dir is not None else None
    sizes = {tf: len(data[f"{tf}_close"]) for tf, _ in tasks}
    missing = []
    for tf, code in tasks:
        try:
            cached = load_signals(cache_dir, tf, code, sizes[tf])
            del cached
        except (OSError, ValueError, EOFError):
            shared = shared_root / f"{tf}_{code}" if shared_root is not None else None
            if not _restore_shared(shared, cache_dir, audit_dir, tf, code, sizes[tf]):
                missing.append((tf, code))
    completed = len(tasks) - len(missing)
    workers = worker_limit(max_workers, len(missing), max(sizes.values()))
    started = time.monotonic()
    running = {}
    pool = None
    last_report = 0.0

    def send(state="running", force=False):
        nonlocal last_report
        now = time.monotonic()
        if not force and now - last_report < 1.0:
            return
        last_report = now
        names = [f"{tf} {FIFTH_SPECS[code]['name']}" for tf, code in running]
        message = (f"开仓预计算：已完成 {completed}/{len(tasks)} 种；"
                   f"同时计算 {len(running)} 种，上限 {workers}；已用 {int(now-started)} 秒")
        if state == "paused":
            message += "；暂停派发，正在计算的方法完成后暂停"
        if state == "done":
            message = f"开仓预计算完成：{len(tasks)} 种；后续止盈、止损和仓位比较直接复用信号"
        report(dict(completed=completed, total=len(tasks), running=names,
                    workers=workers, elapsed_seconds=now-started, message=message, state=state))

    def flag(name):
        return control_dir is not None and (Path(control_dir) / name).exists()

    send(force=True)
    if not missing:
        send("done", True)
        return cache_dir
    # Start expensive models first so inexpensive methods fill free slots later.
    heavy = {98: 100, 22: 90, 40: 80, 99: 70, 101: 60, 102: 60, 17: 50,
             20: 40, 76: 40, 18: 35, 19: 35, 21: 30, 35: 35, 42: 30}
    missing.sort(key=lambda task: (-heavy.get(task[1]-263, 0) * sizes[task[0]], task))
    # A multiprocessing.Pool worker is a daemon and cannot own GP children.
    # Finish ordinary methods first, then give each GP timeframe the full budget.
    gp_tasks = [task for task in missing if task[1] == 263 + 98]
    ordinary = [task for task in missing if task not in gp_tasks]

    def publish(task, task_dir):
        nonlocal completed
        tf, code = task
        value = np.load(task_dir / "signals.npy", mmap_mode="r", allow_pickle=False)
        if value.shape != (2, sizes[tf]) or value.dtype != np.bool_:
            raise ValueError(f"开仓预计算结果形状错误：{tf}/{code}")
        del value
        shared = shared_root / f"{tf}_{code}" if shared_root is not None else None
        _publish_shared(shared, task_dir, tf, code)
        for source in (task_dir / "audit").glob("*"):
            os.replace(source, audit_dir / source.name)
        os.replace(task_dir / "signals.npy", cache_dir / f"{tf}_{code}.npy")
        completed += 1
    # TemporaryDirectory owns only this invocation's input and incomplete output.
    # Its resolved path is constrained to this run's cache before recursive cleanup.
    with tempfile.TemporaryDirectory(prefix="work_", dir=cache_dir) as scratch_name:
        scratch = Path(scratch_name).resolve()
        if not scratch.is_relative_to(cache_dir.resolve()):
            raise RuntimeError("预计算临时目录超出本次结果目录")
        input_dir = scratch / "input"
        for tf in sorted({tf for tf, _ in missing}):
            folder = input_dir / tf
            folder.mkdir(parents=True)
            # Native-timeframe arrays only. Workers map the same read-only files.
            for key, value in data.items():
                if key.startswith(tf + "_"):
                    array = np.asarray(value)
                    if array.shape == (sizes[tf],) and key != tf + "_map":
                        np.save(folder / (key[len(tf)+1:] + ".npy"), array, allow_pickle=False)
        queue = iter(ordinary)
        exhausted = False
        try:
            while completed < len(tasks) - len(gp_tasks):
                if flag("停止.flag"):
                    raise PrecomputeStopped()
                paused = flag("暂停.flag")
                while not paused and not exhausted and len(running) < workers:
                    task = next(queue, None)
                    if task is None:
                        exhausted = True
                        break
                    if pool is None:
                        from gp_parallel import single_threaded_children
                        with single_threaded_children():
                            pool = mp.get_context("spawn").Pool(workers, initializer=_initialize_worker)
                    tf, code = task
                    task_dir = scratch / f"{tf}_{code}"
                    task_dir.mkdir()
                    result = pool.apply_async(_compute_task, (str(input_dir), str(task_dir), tf, code))
                    running[task] = (result, task_dir)
                for task, (result, task_dir) in list(running.items()):
                    if not result.ready():
                        continue
                    tf, code = task
                    try:
                        result.get()
                    except Exception as exc:
                        raise RuntimeError(f"开仓预计算失败：{tf} {FIFTH_SPECS[code]['name']}：{exc}") from exc
                    # Retried incomplete archives remain inside scratch, never in
                    # the public audit directory. Only a completed task publishes.
                    publish(task, task_dir)
                    del running[task]
                    send("paused" if paused else "running", True)
                send("paused" if paused else "running")
                if completed < len(tasks):
                    time.sleep(.1)
            if pool is not None:
                pool.close()
                pool.join()
                pool = None
            for tf, code in gp_tasks:
                if flag("停止.flag"):
                    raise PrecomputeStopped()
                # GP private work is bounded by one small block; the historical
                # feature arrays are read-only shared mappings, not per-process copies.
                gp_workers = worker_limit(max_workers, max(1, (sizes[tf]-1025+479)//480), 1505)
                task_dir = scratch / f"{tf}_{code}"
                task_dir.mkdir()

                def gp_report(progress):
                    now = time.monotonic()
                    paused = progress['paused']
                    done, total = progress['done_blocks'], progress['total_blocks']
                    actual_workers = progress['active_workers']
                    message = (f"开仓预计算：已完成 {completed}/{len(tasks)} 种；{tf} 高斯过程当前数据段训练 "
                               f"{done}/{total} 批；并行上限 {actual_workers}；已用 {int(now-started)} 秒")
                    if paused:
                        message += "；当前训练批次完成后暂停"
                    report(dict(completed=completed, total=len(tasks),
                                running=[f"{tf} {FIFTH_SPECS[code]['name']}"],
                                workers=actual_workers, elapsed_seconds=now-started,
                                message=message, state="paused" if paused else "running",
                                training=progress))

                options = dict(work_dir=str(cache_dir / "GP持久训练块"), workers=gp_workers,
                               progress=gp_report,
                               stop_path=str(Path(control_dir)/"停止.flag") if control_dir else None,
                               pause_path=str(Path(control_dir)/"暂停.flag") if control_dir else None)
                try:
                    _compute_task(str(input_dir), str(task_dir), tf, code, options)
                except InterruptedError as exc:
                    raise PrecomputeStopped() from exc
                except Exception as exc:
                    raise RuntimeError(f"开仓预计算失败：{tf} {FIFTH_SPECS[code]['name']}：{exc}") from exc
                publish((tf, code), task_dir)
                send("paused" if flag("暂停.flag") else "running", True)
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()
    send("done", True)
    return cache_dir
