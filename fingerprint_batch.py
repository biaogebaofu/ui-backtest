"""Sequential, isolated backtests from verified original fingerprint records.

Batch resume is deliberately unsupported. A stopped/failed batch remains intact;
start a new batch directory. Individual worker checkpoints retain their old rules.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from fingerprint_lookup import RANKING_FILES, bind_fingerprint_match, parse_fingerprints, restore_fingerprint_match, restore_many
from ranking_view import config_fingerprint
from run_safety import atomic_json, exclusive_output
from selection_config import 配置签名


STATUS_FILE = '批量状态.json'
STOP_FLAGS = ('停止.flag', '停止候选导出.flag', '停止最差导出.flag')
SUMMARY_HEADERS = ('序号', '策略指纹', '状态', '原始结果目录', '独立输出目录', '完整结果CSV', '说明')
RESULT_SUMMARY_HEADERS = ('原指纹', '本次结果指纹', '开仓条件', '名义倍数（倍）', '交易次数（单）',
                          '胜率（比例，1=100%）', '期末资金（USDC）', '最大回撤（比例，1=100%）',
                          '原结果资金（USDC）', '资金差额（USDC）', '数值对账说明')


def emit(kind, **data):
    print(json.dumps({'type': kind, **data}, ensure_ascii=False, allow_nan=False), flush=True)


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def read_manifest(path):
    value = json.loads(Path(path).read_text('utf-8-sig'))
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('strategies'), list) or not value['strategies']):
        raise ValueError('批量清单必须是version=1且strategies为非空原始匹配记录列表')
    seen = set()
    for match in value['strategies']:
        if not isinstance(match, dict) or any(key not in match for key in ('fingerprint', 'source_dir', 'row')):
            raise ValueError('每条策略必须保留原match的fingerprint、source_dir、row')
        fingerprints = parse_fingerprints(match['fingerprint'])
        if len(fingerprints) != 1 or len(match['fingerprint'].strip()) != 16:
            raise ValueError('每条原始记录只能含一个完整16位指纹')
        if not isinstance(match['row'], dict):
            raise ValueError('清单原始行必须是完整对象')
        actual = match['row'].get('策略标识') if match.get('kind') == 'hedge' else config_fingerprint(match['row'])
        if actual != fingerprints[0]:
            raise ValueError('清单原始行与策略指纹不匹配，请移除后重新核验')
        if not isinstance(match['source_dir'], str) or not match['source_dir'].strip():
            raise ValueError('每条策略必须有明确的原始来源目录')
        key = (fingerprints[0], Path(match['source_dir']).resolve())
        if key in seen:
            raise ValueError('清单包含相同指纹及相同来源的重复记录')
        seen.add(key)
    _digest(value)  # Reject non-finite/non-JSON manifests before creating results.
    return value


def _restoration_identity(restored):
    return _digest({key: restored[key] for key in (
        'selection', 'sources', 'request', 'source_dir', 'fingerprint', 'engine_version',
        'source_fingerprint', 'code_sha256', 'current_code_sha256', 'ranking_settings', 'origin_identity')})


def _save_status(output, state):
    state['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    atomic_json(output / STATUS_FILE, state)
    # This summary belongs solely to this newly created batch.
    with (output / '批量结果汇总.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(SUMMARY_HEADERS + RESULT_SUMMARY_HEADERS)
        for item in state['items']:
            summary = dict(item.get('result_summary', {}), 原指纹=item['fingerprint'])
            writer.writerow((item['index'], item['fingerprint'], item['status'], item['source_dir'],
                             item['output'], item.get('csv', ''), item.get('message', ''),
                             *(summary.get(key, '') for key in RESULT_SUMMARY_HEADERS)))


def _result_summary(output, match, restored):
    """Use only this verified child's single native ranking row, never source metrics."""
    if restored.get('kind') == 'hedge':
        from hedge_fingerprints import result_summary
        return result_summary(output, match, restored)
    original = match['row']
    summary = {'原指纹': match['fingerprint']}
    finite = lambda value: type(value) in (int, float) and math.isfinite(value)
    original_money = original.get('期末资金（USDC）')
    if finite(original_money): summary['原结果资金（USDC）'] = original_money
    expected = dict(original, 回测计算版本=restored['current_engine_version'] + ':' + restored['selection']['成交价格口径'])
    expected_fingerprint = config_fingerprint(expected)
    records = {}
    try:
        for name in RANKING_FILES:
            path = output / name
            if not path.is_file(): continue
            payload = json.loads(path.read_text('utf-8-sig'))
            headers, categories = payload.get('表头'), payload.get('分类')
            if (not isinstance(headers, list) or not all(isinstance(h, str) for h in headers)
                    or len(headers) != len(set(headers)) or not isinstance(categories, dict)):
                raise ValueError('原生排行榜格式无效')
            for rows in categories.values():
                if not isinstance(rows, list): raise ValueError('原生排行榜分类无效')
                for values in rows:
                    if not isinstance(values, list) or len(values) != len(headers):
                        raise ValueError('原生排行榜行宽无效')
                    row = dict(zip(headers, values))
                    if config_fingerprint(row) != expected_fingerprint:
                        raise ValueError('结果行参数与本次单一策略不一致')
                    records[_digest(row)] = row
        if not records:
            summary['数值对账说明'] = '未导出可用原生排行（可能未启用或被排行门槛过滤）；本次数值留空，请查看完整结果CSV'
            return summary
        if len(records) != 1: raise ValueError('原生排行包含多个不同结果，无法作为单条结果汇总')
        row = next(iter(records.values()))
        for key in ('名义倍数（倍）', '交易次数（单）', '胜率（%）', '期末资金（USDC）', '最大回撤（%）'):
            if not finite(row.get(key)): raise ValueError(f'{key}缺失或不是有限数值')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        summary['数值对账说明'] = f'本次排行未通过汇总核验：{exc}；本次数值留空，请查看完整结果CSV'
        return summary
    summary.update({
        '本次结果指纹': config_fingerprint(row),
        '开仓条件': '；'.join(f'{tf}={row.get(key, "")}' for tf, key in
                            (('4h', '4小时条件'), ('1h', '1小时条件'), ('15m', '15分钟条件'),
                             ('5m', '5分钟条件'), ('1m', '1分钟条件'))) + f'；{row.get("入场触发口径", "")}',
        '名义倍数（倍）': row['名义倍数（倍）'], '交易次数（单）': row['交易次数（单）'],
        '胜率（比例，1=100%）': row['胜率（%）'], '期末资金（USDC）': row['期末资金（USDC）'],
        '最大回撤（比例，1=100%）': row['最大回撤（%）'],
    })
    if finite(original_money): summary['资金差额（USDC）'] = row['期末资金（USDC）'] - original_money
    numeric_keys = [key for key, value in original.items() if type(value) in (int, float)]
    incomplete = any(not finite(original.get(key)) for key in
                     ('交易次数（单）', '胜率（%）', '期末资金（USDC）', '最大回撤（%）'))
    changed = [key for key in numeric_keys if not finite(row.get(key)) or row[key] != original[key]]
    if incomplete:
        note = '本次结果已核验；原记录数值不完整，未作完整数值对账'
    elif changed:
        note = '数值存在差异：' + '、'.join(changed)
    elif row.get('回测计算版本') != original.get('回测计算版本'):
        note = '数值一致，版本标识不同'
    else:
        note = '数值一致'
    summary['数值对账说明'] = note
    return summary


def _controls(parent, child=None):
    stopped = (parent / '停止.flag').exists()
    paused = (parent / '暂停.flag').exists() and not stopped
    if child is not None:
        child.mkdir(parents=True, exist_ok=True)
        if stopped:
            for name in STOP_FLAGS: (child / name).touch()
        pause = child / '暂停.flag'
        if paused: pause.touch()
        elif pause.exists(): pause.unlink()
    return paused, stopped


def _wait_between_items(control, index, total):
    announced = False
    while True:
        paused, stopped = _controls(control)
        if stopped: return False
        if not paused:
            if announced: emit('batch_resumed', index=index, total=total, message='继续顺序处理')
            return True
        if not announced:
            emit('batch_paused', index=index, total=total, message='批次已暂停，尚未启动下一条')
            announced = True
        time.sleep(.1)


def worker_command(project_dir, restored, output, threads, device, cache_root):
    from fifth_policy import require_selection_allowed
    require_selection_allowed(restored['selection'])
    if restored.get('kind') == 'hedge':
        request = dict(restored['origin_identity']['hedge_request'], cache_root=str(cache_root))
        path = output / '双向回测请求.json'
        atomic_json(path, request)
        return [sys.executable, '-X', 'utf8', str(project_dir / 'hedge_worker.py'),
                '--request', str(path), '--output', str(output), '--threads', str(threads)]
    sources = restored['sources']
    command = [sys.executable, '-X', 'utf8', str(project_dir / 'backtest_worker.py'),
               '--csv', sources['kline'], '--output', str(output), '--threads', str(threads),
               '--device', device, '--selection', str(output / '组合选择.json'), '--cache-root', str(cache_root),
               '--expected-source-fingerprint', restored['source_fingerprint']]
    for key, flag in (('micro', '--micro-csv'), ('funding', '--funding'), ('oi', '--oi'), ('bundle', '--bundle')):
        if sources[key]: command.extend((flag, sources[key]))
    for key in ('start', 'end'):
        if restored[key]: command.extend(('--' + key, restored[key]))
    if restored['ranking_settings'] is not None:
        command.extend(('--ranking-settings', str(output / '排行榜设置.json')))
    return command


def _verify_child_result(output, restored):
    """Only declare completion for the exact pinned account, period and data request."""
    if restored.get('kind') == 'hedge':
        from hedge_fingerprints import verify_child
        return verify_child(output, restored)
    def read(name):
        try:
            value = json.loads((output / name).read_text('utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'子任务完成核验缺少有效{name}，不能标记完成') from exc
        if not isinstance(value, dict):
            raise ValueError(f'子任务完成核验发现{name}格式错误')
        return value
    def same(actual, expected, label):
        if _digest(actual) != _digest(expected):
            raise ValueError(f'子任务实际{label}与已绑定原参数不一致，停止后续列表')
    selection = read('组合选择.json')
    checkpoint = read('断点记录.json')
    meta = read('回测数据说明.json')
    identity = read('回测运行身份.json')
    same(selection, restored['selection'], '组合选择')
    same(checkpoint.get('selection'), restored['selection'], '断点组合选择')
    signature = 配置签名(restored['selection'])
    same(checkpoint.get('selection_signature'), signature, '断点配置签名')
    same(identity.get('selection_signature'), signature, '运行配置签名')
    same(checkpoint.get('run_identity'), identity, '断点运行身份')
    same(meta.get('sources'), restored['sources'], '数据来源')
    same(meta.get('request'), restored['request'], '数据请求和期间')
    same(identity.get('feature_request'), restored['request'], '运行数据身份')
    for key in ('start_utc', 'end_utc'):
        same(meta.get(key), restored['period'][key], '实际数据期间')
    same(identity.get('engine_version'), restored['current_engine_version'], '引擎版本')
    same(identity.get('code_sha256'), restored['current_code_sha256'], '运行代码身份')
    if restored['ranking_settings'] is not None:
        same(read('排行榜设置.json'), restored['ranking_settings'], '排行榜设置')


def _run_child(command, project_dir, parent_control, output, index, total, fingerprint):
    hedge = str(project_dir / 'hedge_worker.py') in command
    child_control = output / '控制'
    child_control.mkdir(exist_ok=True)
    process = subprocess.Popen(command, cwd=str(project_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', bufsize=1,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()
    def reader():
        try:
            for line in process.stdout: messages.put(line)
        finally:
            messages.put(None)
    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    completed = False
    child_stopped = False
    stop_requested = False
    reader_done = False
    pause_state = False
    skipped_message = None
    try:
        while True:
            paused, stopped = _controls(parent_control, child_control)
            stop_requested = stop_requested or stopped
            if stop_requested:
                if hedge: (output / '停止请求.flag').touch()
                # The worker clears startup flags; keep forwarding until it exits.
                for name in STOP_FLAGS: (child_control / name).touch()
            if paused != pause_state:
                emit('batch_paused' if paused else 'batch_resumed', index=index, total=total,
                     fingerprint=fingerprint, message='暂停请求已转发；当前阶段在安全点响应' if paused else '继续请求已转发')
                pause_state = paused
            try:
                line = messages.get(timeout=.1)
            except queue.Empty:
                line = ''
            if line is None: reader_done = True
            elif line:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if isinstance(event, dict) and isinstance(event.get('type'), str):
                    completed = completed or event['type'] == ('done' if hedge else 'completed')
                    child_stopped = child_stopped or event['type'] in ('stopped', 'candidate_stopped', 'legacy_stopped')
                    if event['type'] == 'skipped' and event.get('reason') == 'data_unavailable':
                        skipped_message = event.get('message', '缺少策略所需数据；保留原定义并跳过')
                    emit('batch_child_event', index=index, total=total, fingerprint=fingerprint, event=event)
                else:
                    emit('batch_child_log', index=index, total=total, fingerprint=fingerprint, message=line.rstrip())
            code = process.poll()
            if code is not None and reader_done: break
        if stop_requested or child_stopped: return 'stopped', '停止请求已转发，本批次不再启动下一条'
        if code != 0: raise RuntimeError(f'第{index}条工作进程异常退出，退出码{code}')
        if skipped_message is not None:
            return 'skipped', skipped_message
        if not completed or not (output / ('全部独立试验.csv' if hedge else '全部回测结果.csv')).is_file():
            raise RuntimeError(f'第{index}条未报告完整完成或缺少完整CSV，不视为成功')
        return 'completed', '独立策略回测完成'
    except BaseException:
        if hedge: (output / '停止请求.flag').touch()
        for name in STOP_FLAGS: (child_control / name).touch()
        # Never launch another child while this one is still running.
        process.wait()
        raise
    finally:
        if process.stdout is not None: process.stdout.close()
        reader_thread.join(timeout=1)


def _restore_available_matches(matches):
    """通常仍批量核验；仅明确的数据能力不足允许逐条跳过，参数/来源错误仍拒绝。"""
    try:
        return restore_many(matches), {}
    except ValueError as exc:
        if not str(exc).startswith('原数据能力不足以运行该策略：'):
            raise
    verified = []
    skipped = {}
    for index, match in enumerate(matches):
        try:
            verified.append(restore_fingerprint_match(match))
        except ValueError as exc:
            if not str(exc).startswith('原数据能力不足以运行该策略：'):
                raise
            verified.append(None)
            skipped[index] = str(exc) + '；原指纹定义保留，未替换参数'
    return verified, skipped


def run_batch(manifest_path, output_dir, threads=1, device='cpu', cache_root=None, project_dir=None):
    if type(threads) is not int or not 1 <= threads <= max(1, os.cpu_count() or 1):
        raise ValueError('批量CPU线程数超出当前机器可用范围')
    if device not in ('cpu', 'auto'): raise ValueError('批量设备只支持cpu或auto')
    project_dir = Path(project_dir or Path(__file__).resolve().parent).resolve()
    output = Path(output_dir).resolve()
    manifest = read_manifest(manifest_path)
    output.mkdir(parents=True, exist_ok=True)
    with exclusive_output(output):
        allowed = {'控制', '指纹批量清单.json', '工作进程日志.txt', '.backtest.lock'}
        if (output / STATUS_FILE).exists() or any(path.name not in allowed for path in output.iterdir()):
            raise ValueError('批量续跑暂不支持；输出目录已有内容，请新建批次目录，原结果未改动')
        control = output / '控制'
        control.mkdir(exist_ok=True)
        cache = Path(cache_root).resolve() if cache_root else output / '共享指标缓存'
        total = len(manifest['strategies'])
        state = {'version': 1, 'manifest_sha256': _digest(manifest), 'status': 'validating',
                 'resume_supported': False, 'items': [
                     {'index': index, 'fingerprint': match['fingerprint'].strip().lower(),
                      'source_dir': str(Path(match['source_dir']).resolve()), 'status': 'pending',
                      'output': str(output / f'{index:03d}_{match["fingerprint"].strip().lower()}')}
                     for index, match in enumerate(manifest['strategies'], 1)]}
        _save_status(output, state)
        atomic_json(output / '批量输入快照.json', manifest)
        active_item = None
        try:
            # 完整核验原身份；仅数据不足的条目跳过，绝不改成另一条策略。
            if any('verified_snapshot' not in match for match in manifest['strategies']):
                emit('warning', message='旧列表没有加入时的核验快照；正在重新核验全部原记录，通过后保存本批次快照，不改写旧匹配行')
            verified, skipped = _restore_available_matches(manifest['strategies'])
            pinned = [bind_fingerprint_match(match, restored) if restored is not None else match
                      for match, restored in zip(manifest['strategies'], verified)]
            verified_manifest = dict(manifest, strategies=pinned)
            atomic_json(output / '批量输入快照.json', verified_manifest)
            state['verified_manifest_sha256'] = _digest(verified_manifest)
            identities = [_restoration_identity(restored) if restored is not None else None for restored in verified]
            state['status'] = 'running'; _save_status(output, state)
            emit('batch_started', total=total, output=str(output), resume_supported=False)
            for index, match in enumerate(pinned, 1):
                if index - 1 in skipped:
                    item = state['items'][index - 1]
                    item.update(status='skipped', message=skipped[index - 1])
                    _save_status(output, state)
                    emit('batch_item_skipped', index=index, total=total, fingerprint=item['fingerprint'],
                         message=item['message'], output=item['output'])
                    continue
                if not _wait_between_items(control, index, total):
                    state['status'] = 'stopped'; _save_status(output, state)
                    emit('batch_stopped', index=index, total=total, output=str(output)); return state
                restored = restore_fingerprint_match(match)
                if _restoration_identity(restored) != identities[index - 1]:
                    raise ValueError(f'第{index}条原来源、参数或代码在批次验证后已变化；停止以避免混用')
                if not _wait_between_items(control, index, total):
                    state['status'] = 'stopped'; _save_status(output, state)
                    emit('batch_stopped', index=index, total=total, output=str(output)); return state
                active_item = state['items'][index - 1]
                child = Path(active_item['output'])
                child.mkdir(exist_ok=False)
                atomic_json(child / '组合选择.json', restored['selection'])
                if restored['ranking_settings'] is not None:
                    atomic_json(child / '排行榜设置.json', restored['ranking_settings'])
                atomic_json(child / '指纹来源.json', restored)
                active_item.update(status='running', warnings=restored['warnings'])
                _save_status(output, state)
                emit('batch_item_started', index=index, total=total, fingerprint=active_item['fingerprint'],
                     output=str(child), warnings=restored['warnings'])
                command = worker_command(project_dir, restored, child, threads, device, cache)
                status, message = _run_child(command, project_dir, control, child, index, total, active_item['fingerprint'])
                if status == 'completed':
                    _verify_child_result(child, restored)
                    active_item['identity_verified'] = True
                    active_item['result_summary'] = _result_summary(child, match, restored)
                active_item.update(status=status, message=message)
                csv_path = child / ('全部独立试验.csv' if restored.get('kind') == 'hedge' else '全部回测结果.csv')
                active_item['csv'] = str(csv_path) if csv_path.is_file() else ''
                atomic_json(child / '独立回测状态.json', active_item)
                _save_status(output, state)
                if status == 'stopped' or (control / '停止.flag').exists():
                    state['status'] = 'stopped'; _save_status(output, state)
                    emit('batch_stopped', index=index, total=total, output=str(output)); return state
                emit('batch_item_skipped' if status == 'skipped' else 'batch_item_completed',
                     index=index, total=total, fingerprint=active_item['fingerprint'],
                     output=str(child), csv=active_item['csv'], message=message)
                active_item = None
            state['status'] = 'completed'
            state['completed_count'] = sum(item['status'] == 'completed' for item in state['items'])
            state['skipped_count'] = sum(item['status'] == 'skipped' for item in state['items'])
            _save_status(output, state)
            emit('batch_completed', total=total, completed_count=state['completed_count'],
                 skipped_count=state['skipped_count'], output=str(output), summary=str(output / '批量结果汇总.csv'))
            return state
        except BaseException as exc:
            state.update(status='failed', message=str(exc))
            if active_item is not None:
                active_item.update(status='failed', message=str(exc))
                atomic_json(Path(active_item['output']) / '独立回测状态.json', active_item)
            _save_status(output, state)
            emit('batch_failed', index=active_item['index'] if active_item else None, total=total, message=str(exc), output=str(output))
            raise


def main():
    parser = argparse.ArgumentParser(description='多指纹独立顺序回测（不支持批量续跑）')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--device', choices=('cpu', 'auto'), default='cpu')
    parser.add_argument('--cache-root', default='')
    args = parser.parse_args()
    run_batch(args.manifest, args.output, args.threads, args.device, args.cache_root or None)


if __name__ == '__main__':
    main()
