"""Portable, credential-free export for the headless family campaign."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import zipfile

from selection_config import 规范化配置
from run_safety import CORE_FILES

# Frozen allowlist: never package arbitrary local diagnostics or credential scripts.
EXPORT_SOURCE_FILES = tuple(sorted(set(CORE_FILES) | {
    'campaign_plan.py', 'campaign_runner.py', 'campaign_export.py',
    'candidate_export.py', 'export_runtime.py', 'export_names.py', 'features.py',
    'fingerprint_lookup.py', 'gpu_engine.py', 'research_signals.py',
    'strategy_description.py', 'strategy_display.py', 'ranking_view.py',
    'rank_color_scales.py', 'portable_rank_export.py', 'streaming_excel_export.py',
    'artifact_rank_export.py', 'worst_export.py', 'xlsx_export_metadata.py',
    'result_period.py', 'second_round_catalog.py', 'second_round.py',
    'requirements.txt', 'AGENTS.md', 'platform_support.py', 'rank_color_scales.json', 'strategy_display.json',
    'README.md', 'README.en.md', 'LICENSE',
}))


def data_dates(path):
    """Inspect actual UTC bounds; do not infer them from a filename."""
    first = last = None
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            value = int(row['openTime'])
            first = value if first is None else min(first, value)
            last = value if last is None else max(last, value)
    if first is None:
        raise ValueError('数据为空')
    return (datetime.fromtimestamp(first / 1000, timezone.utc),
            datetime.fromtimestamp((last + 60000) / 1000, timezone.utc))


def export_bundle(target, data, selection, start='', end=''):
    target, data = Path(target), Path(data)
    if target.exists():
        raise ValueError('目标已存在，请选择新的文件名；不会覆盖已有任务')
    if not data.is_file():
        raise ValueError('请先选择有效的1分钟主K线CSV')
    first, last = data_dates(data)
    def parse(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    begin = parse(start) if start else max(first, last - timedelta(days=120))
    finish = parse(end) if end else last
    if begin < first or finish > last or finish - begin < timedelta(days=21):
        raise ValueError('服务器任务需在实际数据范围内选择至少21天；末日为UTC不包含边界')
    boundary = begin + (finish - begin) * .75
    boundary = boundary.replace(hour=0, minute=0, second=0, microsecond=0)
    iso = lambda date: date.isoformat()
    settings = {
        'csv': 'data/market.csv', 'start': iso(begin), 'end': iso(boundary),
        'validation_start': iso(boundary), 'validation_end': iso(finish),
        'seed_selection': 规范化配置(selection), 'max_jobs': 2000,
        'top_families': 8, 'wall_hours': 72,
        'notes': ['按族分层覆盖，不是任意参数笛卡尔积穷举',
                  '仅打包主K线；资金费/OI/逐笔等缺数据方法单列缺失，不能替代',
                  '历史时间段复核不代表真正未来样本外，不接入交易API'],
    }
    project = Path(__file__).resolve().parent
    source_files = [project / name for name in EXPORT_SOURCE_FILES]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    digest = hashlib.sha256()
    with data.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    manifest = {'code_sha256': hashes, 'data_sha256': digest.hexdigest(),
                'data_bytes': data.stat().st_size, 'settings': settings}
    # Exclusive output creation: never partially overwrite an old export.
    created = False
    try:
        with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            created = True
            for path in source_files:
                archive.write(path, 'app/' + path.name)
            archive.write(data, 'data/market.csv')
            archive.writestr('settings.json', json.dumps(settings, ensure_ascii=False, indent=2))
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr('运行说明.txt',
                '在独立目录解压，在独立Python环境安装app/requirements.txt。\n'
                'python app/campaign_runner.py init --settings settings.json --root campaign\n'
                'python app/campaign_runner.py run --root campaign\n'
                'python app/campaign_runner.py status --root campaign\n'
                '需用systemd设置CPU/内存限制。导出不等于已部署或启动。\n'
                '研究条件是待验证候选。原UI资金、成本参数保存在seed_selection内。\n')
        with zipfile.ZipFile(target) as archive:
            if archive.testzip() is not None:
                raise ValueError('导出包校验失败')
    except Exception:
        if created:
            target.unlink(missing_ok=True)  # Only the new exclusive output created above.
        raise
    return manifest
