"""Preparing later TPs concurrently preserves results and safe TP-boundary resume."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from selection_config import 全选配置, 规范化配置


PROJECT = Path(__file__).resolve().parents[1]


class TpPrefetchResumeTests(unittest.TestCase):
    @unittest.skipUnless((os.cpu_count() or 1) >= 4, 'Four CPU threads are required to exercise TP prefetch')
    def test_prefetch_matches_serial_and_resumes_after_first_completed_tp(self):
        import numpy as np
        import pandas as pd

        with tempfile.TemporaryDirectory(prefix='tp_prefetch_resume_', dir=PROJECT.parent) as temporary:
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
            config.update({
                '开仓指标': ['hist'],
                '开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [0], '1m': [1]},
                '止损代码': ['S1'], '固定止损代码': ['OFF'], '叠加止盈代码': ['OFF'],
                '止盈方案编号': [2, 3, 1358, 1758, 2421, 2422],
                '止盈后等待分钟': [0], '仓位倍数': [1., 10.],
                '入场触发口径': 'LIVE_01', '开仓位置过滤': ['OFF'], '成本模式': 'SLIPPAGE',
            })
            config['入场约束']['最小S3距离'] = 0.
            config['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=True)
            selection_path = root / 'selection.json'
            selection_path.write_text(json.dumps(规范化配置(config), ensure_ascii=False), 'utf-8')
            env = os.environ.copy()
            env.pop('BT_FEATURES', None)
            env.update(OPENBLAS_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
            script = '''import sys
from pathlib import Path
import backtest_worker as worker
from itertools import count
stop_clock = count()
StopCheck = worker.停止检查
worker.停止检查 = lambda path: StopCheck(path, clock=lambda: float(next(stop_clock)))
stop = sys.argv.pop(1) == "stop"
output = Path(sys.argv[sys.argv.index("--output")+1])
original_emit = worker.emit
def emit(kind, **fields):
    original_emit(kind, **fields)
    if stop and kind == "progress" and fields.get("tp") == 1:
        (output / "控制" / "停止.flag").touch()
worker.emit = emit
worker.export_excel = lambda *args: None
worker.main()
'''

            def run(name, threads, stop=False):
                output = root / name
                result = subprocess.run(
                    [sys.executable, '-X', 'utf8', '-c', script, 'stop' if stop else 'run',
                     '--csv', str(data), '--output', str(output), '--selection', str(selection_path),
                     '--threads', str(threads), '--device', 'cpu', '--cache-root', str(root / 'cache')],
                    cwd=PROJECT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
                with (output / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
                    rows = list(csv.DictReader(handle))
                return output, rows, events

            def rankings(output):
                return [json.loads((output / name).read_text('utf-8')) for name in
                        ('各类止盈前5000名.json', '各类止盈最差1000名.json')]

            def prefetched(events):
                return any('提前准备止盈方案' in event.get('message', '') for event in events)

            serial, serial_rows, serial_events = run('serial', 1)
            parallel, parallel_rows, parallel_events = run('parallel', 4)
            self.assertFalse(prefetched(serial_events))
            self.assertTrue(prefetched(parallel_events), 'The fixture must exercise the prefetch branch')
            self.assertEqual(len(serial_rows), 6)
            self.assertGreater(sum(int(row['交易次数（单）']) for row in serial_rows), 0)
            self.assertEqual(parallel_rows, serial_rows)
            self.assertEqual((parallel / '全部回测结果.csv').read_bytes(),
                             (serial / '全部回测结果.csv').read_bytes())
            self.assertEqual(rankings(parallel), rankings(serial))

            resumed, partial_rows, stopped_events = run('resumed', 4, stop=True)
            self.assertTrue(prefetched(stopped_events))
            self.assertIn('stopped', [event['type'] for event in stopped_events])
            saved = json.loads((resumed / '断点记录.json').read_text('utf-8'))
            self.assertEqual((saved['next_tp'], saved['entry_batch_next'], saved['rows']), (1, 0, 1))
            self.assertEqual(partial_rows, serial_rows[:1])
            resumed, resumed_rows, resumed_events = run('resumed', 4)
            self.assertTrue(prefetched(resumed_events))
            self.assertEqual(resumed_rows, serial_rows)
            self.assertEqual((resumed / '全部回测结果.csv').read_bytes(),
                             (serial / '全部回测结果.csv').read_bytes())
            self.assertEqual(rankings(resumed), rankings(serial))
            start = next(event for event in resumed_events if event['type'] == 'start')
            self.assertEqual(start['start_tp'], 1)
            initial = next(event for event in resumed_events if 'total_rows' in event)
            self.assertEqual((initial['rows'], initial['total_rows']), (1, 6))
            self.assertIsNone(initial['eta_seconds'])


if __name__ == '__main__':
    unittest.main()
