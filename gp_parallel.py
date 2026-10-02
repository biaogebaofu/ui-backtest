"""Exact F5-098 refit-window parallelism; the original estimator stays unchanged.

Each job starts on the original 60-bar refit grid and retains its 1025-bar
history. Only predictions are returned between processes. Full fitted models
remain in each job's normal fifth_model_audit archive. A manifest lists those
archives in chronological order for the caller to concatenate as gzip members.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import importlib.metadata
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import traceback

import numpy as np

WARMUP = 1025
REFIT = 60
VERSION = 'gp-original-window-parallel-2'


@contextmanager
def single_threaded_children():
    # Spawn reimports the entry point before running a worker initializer.
    # Inherit these limits before NumPy/SciPy load their numerical DLLs.
    keys = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
            'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update(dict.fromkeys(keys, '1'))
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _limit_accelerate_threads():
    if sys.platform != 'darwin':
        return
    import ctypes
    import platform
    accelerate = ctypes.CDLL('/System/Library/Frameworks/Accelerate.framework/Accelerate')
    set_threading = getattr(accelerate, 'BLASSetThreading', None)
    if set_threading is None:
        # macOS before 15 uses the inherited VECLIB_MAXIMUM_THREADS limit.
        version = platform.mac_ver()[0]
        if not version or int(version.split('.')[0]) >= 15:
            raise RuntimeError('Accelerate does not expose its BLAS threading API.')
        return
    set_threading.argtypes = [ctypes.c_uint]
    set_threading.restype = ctypes.c_int
    if set_threading(1) != 0:  # BLAS_THREADING_SINGLE_THREADED
        raise RuntimeError('Accelerate rejected the single-threaded BLAS limit.')


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _atomic_npy(path, values):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as stream:
        np.save(stream, values, allow_pickle=False)
    os.replace(temporary, path)


def _identity(x, logc, ct, source_dir, chunk_fits, timeframe):
    source_dir = Path(source_dir).resolve()
    identity = dict(version=VERSION, helper_sha256=_sha(__file__),
                    chunk_fits=chunk_fits, timeframe=timeframe,
                    sources={name: _sha(source_dir / name) for name in
                             ('fifth_learned_models.py', 'fifth_model_audit.py')},
                    packages={name: importlib.metadata.version(name) for name in
                              ('numpy', 'scipy', 'scikit-learn', 'threadpoolctl')})
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode())
    for values in (x, logc, ct):
        contiguous = np.ascontiguousarray(values)
        digest.update(str(contiguous.shape).encode())
        digest.update(contiguous.dtype.str.encode())
        digest.update(memoryview(contiguous).cast('B'))
    identity['key'] = digest.hexdigest()
    return identity


def _cached_job(directory, first, end):
    path = directory / f'block_{first:09d}' / 'done.json'
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding='utf-8'))
        prediction_path = Path(info['predictions'])
        prediction = np.load(prediction_path, mmap_mode='r', allow_pickle=False)
        if (info['first'] != first or info['end'] != end
                or prediction.shape != (end - first, 3)
                or _sha(prediction_path) != info['prediction_sha256']
                or not Path(info['audit_archive']).is_file()):
            return None
        return info
    except (OSError, ValueError, KeyError):
        return None


def _worker(source_dir, directory, tasks, results, timeframe, source_hash, pause_path):
    try:
        _limit_accelerate_threads()
        sys.path.insert(0, source_dir)
        import fifth_learned_models as original
        import fifth_model_audit as audit
        from threadpoolctl import threadpool_limits
        if (_sha(original.__file__) != source_hash
                or Path(original.__file__).resolve().parent != Path(source_dir).resolve()):
            raise RuntimeError('The GP worker did not load the requested original source.')
        directory = Path(directory)
        x = np.load(directory / 'x.npy', mmap_mode='r', allow_pickle=False)
        logc = np.load(directory / 'logc.npy', mmap_mode='r', allow_pickle=False)
        ct = np.load(directory / 'ct.npy', mmap_mode='r', allow_pickle=False)
        with threadpool_limits(limits=1):
            while True:
                task = tasks.get()
                if task is None:
                    return
                while pause_path is not None and Path(pause_path).exists():
                    time.sleep(0.25)
                first, end = task
                block_dir = directory / f'block_{first:09d}'
                block_dir.mkdir(parents=True, exist_ok=True)
                attempt = Path(tempfile.mkdtemp(prefix='attempt_', dir=block_dir))
                audit_dir = attempt / 'audit'
                audit.set_output_directory(audit_dir)
                audit.set_timeframe(timeframe)
                try:
                    begin = first - WARMUP
                    serial = getattr(original, '_supervised_serial', original._supervised)
                    _, _, prediction, _ = serial(
                        98, x[begin:end], logc[begin:end], ct[begin:end])
                finally:
                    audit.close_archives()
                prediction_path = attempt / 'predictions.npy'
                _atomic_npy(prediction_path, prediction[WARMUP:])
                archive = audit_dir / f'F5-098_{timeframe}_训练快照.pkl.gz'
                info = dict(first=first, end=end, predictions=str(prediction_path),
                            prediction_sha256=_sha(prediction_path),
                            audit_archive=str(archive),
                            audit_settings=str(audit_dir / f'F5-098_{timeframe}_模型说明.json'),
                            first_close_time=int(ct[first]), last_close_time=int(ct[end-1]))
                _atomic_json(block_dir / 'done.json', info)
                results.put(('done', info))
    except BaseException:
        results.put(('error', traceback.format_exc()))


def signals_from_predictions(prediction, cost):
    up = prediction[:, 0] - 1.645 * prediction[:, 1] > cost
    down = prediction[:, 0] + 1.645 * prediction[:, 1] < -cost
    return (up & ~np.r_[False, up[:-1]], down & ~np.r_[False, down[:-1]])


def parallel_gp(x, logc, ct, *, source_dir, work_dir, workers, progress=None,
                chunk_fits=8, timeframe='1m', stop_path=None, pause_path=None,
                manifest_callback=None):
    """Return the original (up, down, predictions, {}) with persistent blocks.

    progress receives a dict at least once each second while jobs run. A present
    stop_path cancels this invocation and terminates only its own children; any
    finished blocks remain reusable. pause_path pauses at the next block boundary.
    manifest_callback receives the manifest's
    Path as soon as this invocation's immutable input identity has been saved.
    Calling code must use the normal multiprocessing ``__main__`` guard.
    """
    x = np.asarray(x, dtype=float)
    logc = np.asarray(logc, dtype=float)
    ct = np.asarray(ct, dtype=np.int64)
    if x.ndim != 2 or x.shape[1] < 5 or len(x) != len(logc) or len(ct) != len(logc):
        raise ValueError('GP feature, close and timestamp arrays must have matching lengths.')
    if workers < 1 or chunk_fits < 1:
        raise ValueError('workers and chunk_fits must be positive.')
    source_dir = str(Path(source_dir).resolve())
    identity = _identity(x, logc, ct, source_dir, chunk_fits, timeframe)
    directory = Path(work_dir).resolve() / identity['key']
    directory.mkdir(parents=True, exist_ok=True)
    for name, values in (('x', x), ('logc', logc), ('ct', ct)):
        path = directory / (name + '.npy')
        if not path.exists():
            _atomic_npy(path, values)
    jobs = [(first, min(first + REFIT * chunk_fits, len(ct)))
            for first in range(WARMUP, len(ct), REFIT * chunk_fits)]
    completed = {}
    pending = []
    for first, end in jobs:
        info = _cached_job(directory, first, end)
        if info is None:
            pending.append((first, end))
        else:
            completed[first] = info
    manifest_path = directory / 'manifest.json'
    manifest = dict(identity=identity, samples=len(ct), total_blocks=len(jobs),
                    complete=False, blocks=[completed[key] for key in sorted(completed)])
    _atomic_json(manifest_path, manifest)
    if manifest_callback is not None:
        manifest_callback(manifest_path)
    started = time.monotonic()
    initial_cached = len(completed)
    # Recheck after input preparation, when the parent's feature/model arrays
    # have been allocated, including calls outside the usual precompute route.
    from fifth_precompute import worker_limit
    workers = worker_limit(workers, max(1, len(pending)), WARMUP + REFIT * chunk_fits)

    def report():
        if progress is not None:
            contiguous_end = WARMUP
            for first, end in jobs:
                if first not in completed:
                    break
                contiguous_end = end
            progress(dict(done_blocks=len(completed), total_blocks=len(jobs),
                          cached_blocks=initial_cached, active_workers=min(workers, len(pending)),
                          completed_samples=sum(info['end']-info['first'] for info in completed.values()),
                          total_samples=max(0, len(ct)-WARMUP),
                          contiguous_end=min(contiguous_end, len(ct)),
                          paused=pause_path is not None and Path(pause_path).exists(),
                          elapsed_seconds=time.monotonic()-started,
                          manifest=str(manifest_path)))

    report()
    processes = []
    if pending:
        context = mp.get_context('spawn')
        tasks = context.Queue()
        results = context.Queue()
        count = min(workers, len(pending))
        try:
            with single_threaded_children():
                for _ in range(count):
                    process = context.Process(target=_worker,
                        args=(source_dir, str(directory), tasks, results, timeframe,
                              identity['sources']['fifth_learned_models.py'], pause_path))
                    process.start()
                    processes.append(process)
            for task in pending:
                tasks.put(task)
            for _ in processes:
                tasks.put(None)
            received = 0
            while received < len(pending):
                if stop_path is not None and Path(stop_path).exists():
                    raise InterruptedError('GP calculation stopped; completed blocks are saved.')
                try:
                    kind, info = results.get(timeout=1)
                except queue.Empty:
                    if any(p.exitcode not in (None, 0) for p in processes):
                        raise RuntimeError('A GP worker exited unexpectedly; completed blocks are saved.')
                    if all(p.exitcode is not None for p in processes):
                        raise RuntimeError('GP workers ended before reporting every block.')
                    report()
                    continue
                if kind == 'error':
                    raise RuntimeError(info)
                completed[info['first']] = info
                received += 1
                manifest['blocks'] = [completed[key] for key in sorted(completed)]
                _atomic_json(manifest_path, manifest)
                report()
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=5)
            tasks.cancel_join_thread()
            tasks.close()
            results.close()
    prediction = np.full((len(ct), 3), np.nan)
    for first, end in jobs:
        prediction[first:end] = np.load(completed[first]['predictions'], allow_pickle=False)
    # The constants are loaded from the explicitly selected, unchanged source.
    import ast
    tree = ast.parse((Path(source_dir) / 'fifth_learned_models.py').read_text(encoding='utf-8'))
    cost = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'COST' for target in node.targets))
    up, down = signals_from_predictions(prediction, cost)
    manifest['complete'] = True
    _atomic_json(manifest_path, manifest)
    report()
    return up, down, prediction, {}
