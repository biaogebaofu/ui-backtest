import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from backtest_worker import 有界顺序结果
from selection_config import 全选配置, 规范化配置


class DeferredFuture:
    def __init__(self, evaluate, task):
        self.evaluate = evaluate
        self.task = task
        self.done = False
        self.cancelled = False

    def result(self):
        self.done = True
        return self.evaluate(self.task)

    def cancel(self):
        self.cancelled = not self.done
        return self.cancelled


class RecordingPool:
    def __init__(self):
        self.futures = []
        self.peak = 0

    def submit(self, evaluate, task):
        future = DeferredFuture(evaluate, task)
        self.futures.append(future)
        self.peak = max(self.peak, sum(not f.done for f in self.futures))
        return future


class BoundedCpuSchedulerTests(unittest.TestCase):
    def test_prefetch_is_bounded_and_results_keep_submission_order(self):
        pool = RecordingPool()
        produced = []

        def tasks():
            for value in range(37):
                produced.append(value)
                yield value

        results = 有界顺序结果(pool, lambda value: value * 2, tasks(), 4, lambda: False)
        self.assertEqual(next(results), 0)
        self.assertEqual(produced, list(range(4)))
        self.assertEqual([0, *results], [value * 2 for value in range(37)])
        self.assertEqual(pool.peak, 4)

    def test_small_groups_run_together_and_hold_their_prepared_data(self):
        barrier = threading.Barrier(4)
        live_group = {}

        def tasks():
            for group in range(4):
                live_group[group] = [group + 10]
                yield group, live_group[group]
                del live_group[group]

        def evaluate(prepared):
            group, data = prepared
            barrier.wait(timeout=10)
            return group, data[0]

        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(有界顺序结果(pool, evaluate, tasks(), 4, lambda: False))
        self.assertEqual(actual, [(group, group + 10) for group in range(4)])
        self.assertEqual(live_group, {})

    def test_stop_cancels_prefetch_and_closes_source_without_more_tasks(self):
        pool = RecordingPool()
        state = {'stop': False, 'closed': False, 'produced': 0}

        def tasks():
            try:
                for value in range(100):
                    state['produced'] += 1
                    yield value
            finally:
                state['closed'] = True

        results = 有界顺序结果(pool, lambda value: value, tasks(), 4, lambda: state['stop'])
        self.assertEqual(next(results), 0)
        state['stop'] = True
        self.assertEqual(list(results), [])
        self.assertEqual(state['produced'], 4)
        self.assertTrue(state['closed'])
        self.assertTrue(all(f.cancelled for f in pool.futures[1:]))

    def test_consumer_close_and_worker_failure_cancel_unconsumed_tasks(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                pool = RecordingPool()

                def evaluate(value):
                    if fails:
                        raise ValueError('worker failed')
                    return value

                results = 有界顺序结果(pool, evaluate, range(100), 4, lambda: False)
                if fails:
                    with self.assertRaisesRegex(ValueError, 'worker failed'):
                        next(results)
                else:
                    self.assertEqual(next(results), 0)
                    results.close()
                self.assertEqual(len(pool.futures), 4)
                self.assertTrue(all(f.cancelled for f in pool.futures[1:]))

    def test_stop_during_preparation_does_not_submit_prepared_task(self):
        pool = RecordingPool()
        state = {'stop': False}

        def tasks():
            yield 0
            state['stop'] = True
            yield 1

        results = 有界顺序结果(pool, lambda value: value, tasks(), 4, lambda: state['stop'])
        self.assertEqual(list(results), [])
        self.assertEqual(len(pool.futures), 1)
        self.assertTrue(pool.futures[0].cancelled)


class CrossStopWorkerTests(unittest.TestCase):
    def test_parallel_rows_rank_ties_and_mid_batch_resume_match_serial(self):
        self.verify_worker(wide=False)

    def test_wide_group_chunk_boundary_limit70_and_mid_batch_resume(self):
        self.verify_worker(wide=True)

    def verify_worker(self, wide):
        import numpy as np
        import pandas as pd

        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='bounded_cpu_worker_') as temporary:
            root = Path(temporary)
            x = np.arange(1200)
            close = 2000. + 6 * np.sin(x / 11.) + 2 * np.sin(x / 3.)
            opening = np.r_[close[0], close[:-1]]
            data = root / 'synthetic.csv'
            pd.DataFrame({'openTime': 1735689600000 + x * 60000, 'open': opening,
                          'high': np.maximum(opening, close) + .5,
                          'low': np.minimum(opening, close) - .5, 'close': close,
                          'volume': 100. + x % 23}).to_csv(data, index=False)
            config = 全选配置()
            config.update({'开仓指标': ['hist'],
                           '开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [0],
                                      '1m': [1, 2, 3, 4]},
                           '止损代码': config['止损代码'][:30], '固定止损代码': ['OFF'],
                           '强制时间止损分钟': [30], '叠加止盈代码': ['OFF'],
                           '止盈方案编号': [2], '止盈后等待分钟': [0, 2],
                           '仓位倍数': [1., 2.], '开仓方向': ['BOTH'],
                           '交易会话': ['ALL'], '开仓位置过滤': ['OFF']})
            if wide:
                config.update({'止损代码': config['止损代码'][:1],
                               '强制时间止损分钟': [0, 30, 60],
                               '止盈后等待分钟': list(range(18))})
            config['入场约束']['最小S3距离'] = 0.
            config['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=True)
            selection = root / 'selection.json'
            selection.write_text(json.dumps(规范化配置(config), ensure_ascii=False), 'utf-8')
            driver = '''import sys
from pathlib import Path
import backtest_worker as W
from itertools import count
# Advance the injected polling clock at every check; stop at the exact test
# boundary instead of racing the production 100ms filesystem throttle.
stop_clock = count()
StopCheck = W.停止检查
W.停止检查 = lambda path: StopCheck(path, clock=lambda: float(next(stop_clock)))
stop_at = int(sys.argv.pop(1))
output = Path(sys.argv[sys.argv.index('--output') + 1])
original_encode = W.encode_accounts
encoded_rows = 0
def encode_accounts(records):
    global encoded_rows
    encoded = original_encode(records)
    encoded_rows += 1
    # Progress messages are throttled; encoding is the exact per-row boundary.
    if stop_at and encoded_rows == stop_at:
        (output / '控制' / '停止.flag').touch()
    return encoded
W.encode_accounts = encode_accounts
W.export_excel = lambda *args: None
W.main()
'''
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1',
                       TEMP=str(root), TMP=str(root))
            env.pop('BT_FEATURES', None)

            def run(name, threads, stop_at=0, limit=0):
                output = root / name
                result = subprocess.run(
                    [sys.executable, '-B', '-X', 'utf8', '-c', driver, str(stop_at),
                     '--csv', str(data), '--output', str(output), '--selection', str(selection),
                     '--threads', str(threads), '--device', 'cpu', '--cache-root', str(root / 'cache'),
                     '--limit-base', str(limit)],
                    cwd=project, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
                return output, result.stdout

            serial, _ = run('serial', 1)
            parallel, _ = run('parallel', min(4, os.cpu_count() or 1))
            _, stopped_log = run('resumed', min(4, os.cpu_count() or 1), 100)
            self.assertIn('"stopped"', stopped_log)
            resumed, _ = run('resumed', min(4, os.cpu_count() or 1))
            expected = (serial / '全部回测结果.csv').read_bytes()
            with (serial / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 216 if wide else 240)
            for output in (parallel, resumed):
                self.assertEqual((output / '全部回测结果.csv').read_bytes(), expected)
                for filename in ('各类止盈前5000名.json', '各类止盈最差1000名.json'):
                    baseline = json.loads((serial / filename).read_text('utf-8'))
                    actual = json.loads((output / filename).read_text('utf-8'))
                    self.assertEqual(actual['分类'], baseline['分类'])
            if wide:
                _, first_group_stop_log = run('first_group_resumed', min(4, os.cpu_count() or 1), 1)
                self.assertIn('"stopped"', first_group_stop_log)
                first_group_resumed, _ = run('first_group_resumed', min(4, os.cpu_count() or 1))
                self.assertEqual((first_group_resumed / '全部回测结果.csv').read_bytes(), expected)
                for filename in ('各类止盈前5000名.json', '各类止盈最差1000名.json'):
                    baseline = json.loads((serial / filename).read_text('utf-8'))
                    actual = json.loads((first_group_resumed / filename).read_text('utf-8'))
                    self.assertEqual(actual['分类'], baseline['分类'])
                limited_serial, _ = run('limited_serial', 1, limit=70)
                limited_parallel, _ = run('limited_parallel', min(4, os.cpu_count() or 1), limit=70)
                actual = (limited_parallel / '全部回测结果.csv').read_bytes()
                self.assertEqual(actual, (limited_serial / '全部回测结果.csv').read_bytes())
                with (limited_parallel / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), 70)
                for filename in ('各类止盈前5000名.json', '各类止盈最差1000名.json'):
                    baseline = json.loads((limited_serial / filename).read_text('utf-8'))
                    actual = json.loads((limited_parallel / filename).read_text('utf-8'))
                    self.assertEqual(actual['分类'], baseline['分类'])


if __name__ == '__main__':
    unittest.main()
