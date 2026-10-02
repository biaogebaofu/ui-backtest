"""Run identity and a non-destructive, cross-platform output-directory lock."""
from __future__ import annotations
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

AUDIT_REVISION = 'v1.64-fifth-permanent-39-20260912'
CORE_FILES = ('fourth_batch.py', 'fifth_batch.py', 'fifth_policy.py', 'engine.py', 'backtest_engine.py', 'account_replay.py', 'run_all.py',
              'backtest_worker.py', 'extended_rules.py', 'extended_signals.py',
              'data_sources.py', 'feature_builder.py', 'selection_config.py',
              'strategy_space.py', 'account_statistics.py', 'run_safety.py',
              'execution_settings.py', 'entry_position.py', 'ranking_limits.py', 'indicator_combinations.py',
              'selection_availability.py', 'fifth_model_audit.py', 'fifth_stat_models.py',
              'fifth_learned_models.py', 'fifth_conditional_models.py', 'fifth_precompute.py',
              'gp_parallel.py', 'iso_fast.py')


def atomic_json(path: Path, payload) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         indent=2, allow_nan=False), encoding='utf-8')
        # Windows readers can briefly hold a file without FILE_SHARE_DELETE.
        # Keep the old JSON intact while retrying the atomic replacement.
        for attempt in range(11):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32) or attempt == 10:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def build_run_identity(project_dir: Path, selection_signature: str, feature_meta: dict,
                       engine_version: str, limit_base: int = 0) -> dict:
    digest = hashlib.sha256()
    for name in CORE_FILES:
        path = project_dir / name
        digest.update(name.encode('utf-8'))
        digest.update(path.read_bytes())
    return {'audit_revision': AUDIT_REVISION, 'engine_version': engine_version,
            'code_sha256': digest.hexdigest(), 'selection_signature': selection_signature,
            'feature_request': feature_meta['request'], 'limit_base': int(limit_base)}


def verify_run_identity(output_dir: Path, identity: dict, restart: bool = False) -> None:
    if restart:
        return
    path = output_dir / '回测运行身份.json'
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError) as exc:
            raise RuntimeError('运行身份文件损坏，不能安全续跑；请使用新任务目录') from exc
        if previous != identity:
            raise RuntimeError('数据源、日期范围、策略配置或程序版本已变化，禁止新旧结果混写。'
                               '请新建任务目录；旧CSV/断点保持不变。')
    elif any((output_dir / name).exists() for name in ('断点记录.json', '全部回测结果.csv')):
        raise RuntimeError('旧任务缺少可核验的数据/程序运行身份，不能证明与本次一致。'
                           '请新建任务目录；旧CSV仍可单独导出。')


@contextmanager
def exclusive_output(output_dir: Path):
    """The OS releases the advisory lock on crash; no unsafe stale-PID guessing.

    The lock file stays in place after release: unlinking would allow two
    processes to lock different file objects at the same path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    handle = (output_dir / '.backtest.lock').open('a+b')
    locked = False
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError('该结果目录正在被另一个回测进程使用，请换目录或等待其结束') from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
