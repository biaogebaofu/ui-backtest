"""Multi-entry selection is a mode dimension, not a shared entry/account state."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ranking_view import config_fingerprint
from selection_config import (全选配置, 入场口径列表, 入场触发口径选项, 入场触发显示代码,
                              规范化入场触发口径, 规范化配置, 配置签名, 配置统计)


PROJECT = Path(__file__).resolve().parents[1]
MODES = ['LIVE_01', 'TF_EVENT', 'MACD_CYCLE']


def small_config(mode='LIVE_01'):
    raw = 全选配置()
    raw.update({'开仓指标': ['hist'],
                '开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [0], '1m': [1]},
                '止损代码': ['S1'], '固定止损代码': ['OFF'], '叠加止盈代码': ['OFF'],
                '止盈方案编号': [1], '止盈后等待分钟': [0], '仓位倍数': [1.],
                '入场触发口径': mode})
    return raw


class MultiEntryConfigTests(unittest.TestCase):
    def test_single_values_and_public_example_signatures_remain_exact(self):
        golden = {'LIVE_01': '3c77d471083414322d2ebc0f910bb791149b5db57f485ae31599814253421a0f',
                  'TF_EVENT': '736625fdd50f0c135371d89a362a3443c93f6c7ffb3fa4a60b378a6e0358a825'}
        self.assertEqual(全选配置()['入场触发口径'], 'LIVE_01')
        for mode in MODES:
            self.assertEqual(规范化配置(small_config([mode])), small_config(mode))
            self.assertEqual(配置签名(small_config([mode])), 配置签名(small_config(mode)))
        for mode, signature in golden.items():
            self.assertEqual(配置签名(small_config(mode)), signature)

    def test_modes_are_validated_deduplicated_and_canonicalized(self):
        self.assertEqual(入场口径列表({'入场触发口径': MODES}), MODES)
        self.assertEqual(入场口径列表('TF_EVENT'), ['TF_EVENT'])
        self.assertEqual(规范化入场触发口径(['MACD_CYCLE', 'LIVE_01', 'MACD_CYCLE']),
                         ['LIVE_01', 'MACD_CYCLE'])
        labels = [入场触发口径选项[m] for m in reversed(MODES)]
        self.assertEqual(规范化入场触发口径(labels), MODES)
        self.assertEqual(配置签名(small_config(list(reversed(MODES)))), 配置签名(small_config(MODES)))
        for label, code in 入场触发显示代码.items():
            self.assertEqual(规范化入场触发口径([label]), code)
        for bad in ([], ['LIVE_01', 'UNKNOWN'], [None], [['LIVE_01']], '', None, True, {}, 1):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                规范化配置(small_config(bad))

    def test_mode_dimension_multiplies_rows_accounts_but_not_base_ids(self):
        one = small_config()
        one['开仓指标'] = ['hist', 'dif']
        one['开仓位置过滤'] = ['OFF', 'RANGE60_EDGE20']
        one['止盈方案编号'] = [1, 2]
        one['仓位倍数'] = [9., 10.]
        original = 配置统计(one)
        several = 配置统计(dict(one, 入场触发口径=MODES))
        self.assertEqual(several['入场口径档数'], 3)
        self.assertEqual(several['基础入场组合数'], original['基础入场组合数'])
        self.assertEqual(several['开仓位置过滤档数'], 2)
        for key in ('入场组合数', '不含仓位完整组合数', '包含仓位完整组合数'):
            self.assertEqual(several[key], original[key] * 3)
        self.assertEqual(规范化配置(dict(one, 入场触发口径=MODES))['版本'], 20)

    def test_cli_rejects_malformed_expected_source_before_loading_data(self):
        import backtest_worker as worker
        for value in ('', 'a' * 63, 'g' * 64, ' ' + 'a' * 64):
            with self.subTest(value=value), tempfile.TemporaryDirectory(prefix='v153_bad_source_') as temporary:
                argv = ['worker', '--csv', 'unused.csv', '--output', temporary,
                        '--expected-source-fingerprint', value, '--threads', '1']
                with mock.patch.object(sys, 'argv', argv), mock.patch.object(worker, 'build_features') as build:
                    with self.assertRaisesRegex(ValueError, '64位十六进制'):
                        worker._main()
                build.assert_not_called()


class MultiEntryRealWorkerTests(unittest.TestCase):
    def test_multi_mode_matches_independent_single_runs_and_resumes_at_mode_boundary(self):
        import numpy as np
        import pandas as pd

        with tempfile.TemporaryDirectory(prefix='v153_multi_worker_') as temporary:
            root = Path(temporary)
            x = np.arange(1200)
            close = 2000. + 6 * np.sin(x / 11.) + 2 * np.sin(x / 3.)
            opening = np.r_[close[0], close[:-1]]
            data = root / 'synthetic.csv'
            pd.DataFrame({'openTime': 1735689600000 + x * 60000, 'open': opening,
                          'high': np.maximum(opening, close) + .5,
                          'low': np.minimum(opening, close) - .5, 'close': close,
                          'volume': 100. + x % 23}).to_csv(data, index=False)
            config = small_config(MODES)
            config['开仓指标'] = ['hist', 'dif']
            config['止盈方案编号'] = [1, 2]
            config['仓位倍数'] = [9., 10.]
            config['入场约束']['最小S3距离'] = 0.
            config['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=True)
            env = os.environ.copy()
            env.pop('BT_FEATURES', None)
            env.update(OPENBLAS_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
            script = """import sys
from pathlib import Path
import backtest_worker as worker
from itertools import count
stop_clock = count()
StopCheck = worker.停止检查
worker.停止检查 = lambda path: StopCheck(path, clock=lambda: float(next(stop_clock)))
pause = sys.argv.pop(1) == 'stop'
target = Path(sys.argv[sys.argv.index('--output')+1])
original_emit = worker.emit
def emit(kind, **fields):
    original_emit(kind, **fields)
    if pause and kind == 'inner_progress' and fields.get('base_done') == 2:
        (target/'控制'/'停止.flag').touch()
worker.emit = emit
worker.export_excel = lambda *args: None
worker.main()
"""

            def run(name, selected, stop=False, expected_source=None, reject=False):
                selection_path = root / f'{name}.json'
                selection_path.write_text(json.dumps(规范化配置(selected), ensure_ascii=False), 'utf-8')
                output = root / name
                command = [sys.executable, '-X', 'utf8', '-c', script, 'stop' if stop else 'run',
                           '--csv', str(data), '--output', str(output), '--selection', str(selection_path),
                           '--threads', '1', '--device', 'cpu', '--cache-root', str(root / 'cache')]
                if expected_source is not None:
                    command.extend(('--expected-source-fingerprint', expected_source))
                result = subprocess.run(command, cwd=PROJECT, env=env, capture_output=True,
                                        text=True, encoding='utf-8', timeout=90)
                if reject:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('原来源数据指纹已变化', result.stderr)
                    self.assertFalse((output / '全部回测结果.csv').exists())
                    self.assertFalse((output / '断点记录.json').exists())
                    return
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
                with (output / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
                    rows = list(csv.DictReader(handle))
                return output, rows, events

            from data_sources import source_bundle_fingerprint
            fingerprint = source_bundle_fingerprint({'kline': str(data), 'micro': '', 'funding': '', 'oi': '', 'bundle': ''})
            run('wrong_source', config, expected_source='0' * 64, reject=True)
            output, combined, events = run('continuous', config, expected_source=fingerprint.upper())
            self.assertEqual(len(combined), 12)
            start = next(event for event in events if event['type'] == 'start')
            self.assertEqual((start['csv_rows'], start['total_combinations']), (12, 24))
            self.assertEqual({row['入场触发口径'] for row in combined}, set(MODES))
            for mode in MODES:
                _, separate, _ = run(mode, dict(config, 入场触发口径=mode))
                self.assertEqual([row for row in combined if row['入场触发口径'] == mode], separate)
                self.assertGreater(sum(int(row['交易次数（单）']) for row in separate), 0,
                                   'Mode equivalence must exercise actual completed trades')
            checkpoint = json.loads((output / '断点记录.json').read_text('utf-8'))
            self.assertEqual(checkpoint['selection']['入场触发口径'], MODES)
            payload = json.loads((output / '各类止盈前5000名.json').read_text('utf-8'))
            ranking = [dict(zip(payload['表头'], values)) for rows in payload['分类'].values() for values in rows]
            self.assertEqual(len(ranking), 24)
            self.assertEqual(len({config_fingerprint(row) for row in ranking}), 24)
            for field in ('hist', 'dif'):
                ids = {row['基础策略编号'] for row in combined
                       if row['开仓MACD代码'] == ('0' if field == 'hist' else '1')}
                self.assertEqual(len(ids), 1)
            stopped, partial, stop_events = run('resumed', config, stop=True)
            saved = json.loads((stopped / '断点记录.json').read_text('utf-8'))
            self.assertEqual((saved['next_tp'], saved['entry_batch_next'], saved['rows']), (0, 2, 2))
            self.assertEqual({row['入场触发口径'] for row in partial}, {'LIVE_01'})
            self.assertIn('stopped', [event['type'] for event in stop_events])
            resumed, rows, _ = run('resumed', config)
            self.assertEqual(rows, combined)
            self.assertEqual((resumed / '全部回测结果.csv').read_bytes(),
                             (output / '全部回测结果.csv').read_bytes())


if __name__ == '__main__':
    unittest.main()
