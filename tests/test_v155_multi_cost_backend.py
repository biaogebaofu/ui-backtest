"""Independent cost modes preserve scalar history and exact account replay."""
import copy
import csv
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from execution_settings import (cost_modes, normalize_cost_mode, normalize_execution_settings,
                                effective_fee_rates, effective_slippage)
from ranking_view import config_fingerprint
from selection_config import 全选配置, 规范化配置, 配置签名, 配置统计


PROJECT = Path(__file__).resolve().parents[1]
COSTS = ['SLIPPAGE', 'FEE']


def small_config():
    raw = 全选配置()
    raw.update({'开仓指标': ['hist'],
                '开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [0], '1m': [1]},
                '止损代码': ['S1'], '固定止损代码': ['OFF'], '叠加止盈代码': ['OFF'],
                '止盈方案编号': [1], '止盈后等待分钟': [0], '仓位倍数': [1.],
                '入场触发口径': 'LIVE_01'})
    return raw


class MultiCostConfigTests(unittest.TestCase):
    def test_single_modes_preserve_public_example_configuration_and_signature(self):
        raw = small_config()
        self.assertEqual(配置签名(raw), '3c77d471083414322d2ebc0f910bb791149b5db57f485ae31599814253421a0f')
        for mode in COSTS:
            scalar = dict(raw, 成本模式=mode)
            self.assertEqual(规范化配置(dict(raw, 成本模式=[mode])), scalar)
            self.assertEqual(配置签名(dict(raw, 成本模式=[mode])), 配置签名(scalar))
        legacy = copy.deepcopy(raw)
        for name in ('成本模式', '手续费', '平仓后最小开仓间隔分钟'):
            legacy.pop(name)
        self.assertEqual(cost_modes(legacy), ['SLIPPAGE'])
        self.assertEqual(规范化配置(legacy)['平仓后最小开仓间隔分钟'], 0)
        fee_only = {'成交偏移': {'开仓': 0., '平仓': 0.}, '手续费': {'开仓费率': .0004}}
        self.assertEqual(cost_modes(fee_only), ['FEE'])
        legacy['手续费'] = {'开仓费率': .0004}
        with self.assertRaisesRegex(ValueError, '明确选择成本模式'):
            规范化配置(legacy)

    def test_cost_modes_canonical_and_invalid_values_rejected(self):
        self.assertEqual(cost_modes('FEE'), ['FEE'])
        self.assertEqual(cost_modes({'成本模式': COSTS}), COSTS)
        self.assertEqual(normalize_cost_mode(['FEE', 'SLIPPAGE', 'FEE']), COSTS)
        self.assertEqual(normalize_cost_mode(['FEE', 'FEE']), 'FEE')
        for bad in ([], '', None, False, 7, {}, ['FEE', None], ['UNKNOWN'], [['FEE']], ('FEE',)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_cost_mode(bad)
        for bad in ([], '', False, 7, {}, ['UNKNOWN'], [['FEE']]):
            with self.subTest(config_bad=bad), self.assertRaises(ValueError):
                规范化配置(dict(small_config(), 成本模式=bad))
        self.assertEqual(配置签名(dict(small_config(), 成本模式=COSTS)),
                         配置签名(dict(small_config(), 成本模式=list(reversed(COSTS)))))

    def test_effective_cost_helpers_require_a_single_branch(self):
        raw = dict(small_config(), 成本模式=COSTS)
        raw['手续费'] = {'开仓费率': .0004, '平仓费率': .0005, 'BNB抵扣': True, '返佣比例': .3}
        for helper in (effective_slippage, effective_fee_rates):
            with self.assertRaisesRegex(ValueError, '多成本模式必须分别回测'):
                helper(raw)
        slippage = dict(raw, 成本模式='SLIPPAGE')
        fee = dict(raw, 成本模式='FEE')
        self.assertEqual(effective_slippage(slippage), tuple(raw['成交偏移'].values()))
        self.assertEqual(effective_fee_rates(slippage), (0., 0.))
        self.assertEqual(effective_slippage(fee), (0., 0.))
        self.assertEqual(effective_fee_rates(fee), (.0004 * .9 * .7, .0005 * .9 * .7))
        fee['手续费'] = {'开仓费率': 0., '平仓费率': 0., 'BNB抵扣': False, '返佣比例': 0.}
        self.assertEqual(effective_fee_rates(fee), (0., 0.))
        self.assertEqual(normalize_execution_settings(fee)['成本模式'], 'FEE')

    def test_counts_multiply_costs_once_independently_of_entry_modes_and_sizes(self):
        raw = small_config()
        raw.update({'入场触发口径': ['TF_EVENT', 'MACD_CYCLE'],
                    '开仓位置过滤': ['OFF', 'RANGE60_EDGE20'], '仓位倍数': [9., 10.]})
        single = 配置统计(raw)
        multi = 配置统计(dict(raw, 成本模式=COSTS))
        self.assertEqual((multi['成本模式档数'], multi['入场口径档数']), (2, 2))
        self.assertEqual(multi['入场组合数'], single['入场组合数'])
        self.assertEqual(multi['基础入场组合数'], single['基础入场组合数'])
        for name in ('不含仓位完整组合数', '包含仓位完整组合数'):
            self.assertEqual(multi[name], single[name] * 2)


class MultiCostRealWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        import pandas as pd

        cls.temp = tempfile.TemporaryDirectory(prefix='v155_multi_cost_')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        rng = np.random.default_rng(20260909)
        count = 4600
        close = 2000 * np.exp(np.cumsum(rng.normal(0, .0007, count)))
        opening = np.r_[close[0], close[:-1]]
        volume = rng.lognormal(6, 1, count)
        cls.data = cls.root / 'synthetic.csv'
        pd.DataFrame({'openTime': 1735689600000 + np.arange(count) * 60000,
                      'open': opening, 'high': np.maximum(opening, close) + rng.uniform(.1, 2, count),
                      'low': np.minimum(opening, close) - rng.uniform(.1, 2, count),
                      'close': close, 'volume': volume, 'trades': rng.integers(1, 10000, count),
                      'taker_buy_base': volume * rng.uniform(.01, .99, count)}).to_csv(cls.data, index=False)
        config = small_config()
        config['开仓条件']['1m'] = [154, 156, 263]
        config.update({'开仓位置过滤': ['OFF', 'RANGE60_EDGE20'],
                       '入场触发口径': ['TF_EVENT', 'MACD_CYCLE'], '成本模式': COSTS,
                       '止盈方案编号': [2, 2421], '止盈后等待分钟': [2], '仓位倍数': [1., 2.],
                       '平仓后最小开仓间隔分钟': 5, '成交价格口径': 'CLOSE_CONFIRMED',
                       '成交偏移': {'开仓': 0.00005, '平仓': 0.00005},
                       '手续费': {'开仓费率': .0004, '平仓费率': .0005, 'BNB抵扣': True, '返佣比例': .3}})
        config['入场约束']['最小S3距离'] = 0.
        config['资金约束'].update(初始资金USDC=1000., 最大开仓数量ETH=20.)
        config['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=True)
        cls.config = 规范化配置(config)
        cls.runs = {'combined': cls.run_worker('combined', cls.config)}
        for mode in COSTS:
            cls.runs[mode] = cls.run_worker(mode, dict(cls.config, 成本模式=mode))

    @classmethod
    def run_worker(cls, name, config, stop_at=0):
        output = cls.root / name
        selection = cls.root / (name + '.json')
        selection.write_text(json.dumps(config, ensure_ascii=False), 'utf-8')
        driver = '''import sys
from pathlib import Path
import backtest_worker as W
from itertools import count
stop_clock = count()
StopCheck = W.停止检查
W.停止检查 = lambda path: StopCheck(path, clock=lambda: float(next(stop_clock)))
stop_at = int(sys.argv.pop(1))
output = Path(sys.argv[sys.argv.index('--output') + 1])
original_emit = W.emit
def emit(kind, **values):
    original_emit(kind, **values)
    if stop_at and kind == 'inner_progress' and values.get('base_done') == stop_at:
        (output / '控制' / '停止.flag').touch()
W.emit = emit
W.export_excel = lambda *args: None
W.main()
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1',
                   TEMP=str(cls.root), TMP=str(cls.root), NUMBA_CACHE_DIR=str(cls.root / 'numba'))
        env.pop('BT_FEATURES', None)
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', driver, str(stop_at),
                                 '--csv', str(cls.data), '--output', str(output), '--selection', str(selection),
                                 '--threads', '2', '--device', 'cpu', '--cache-root', str(cls.root / 'features')],
                                cwd=PROJECT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
        if result.returncode:
            raise AssertionError(result.stdout[-4000:] + result.stderr[-4000:])
        with (output / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        ranking = json.loads((output / '各类止盈前5000名.json').read_text('utf-8'))
        records = [dict(zip(ranking['表头'], row)) for values in ranking['分类'].values() for row in values]
        return {'output': output, 'rows': rows, 'ranking': records,
                'ranking_context': ranking['运行上下文'],
                'checkpoint': json.loads((output / '断点记录.json').read_text('utf-8')),
                'events': [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]}

    def test_combined_equals_both_single_runs_for_every_csv_and_ranking_field(self):
        combined = self.runs['combined']
        self.assertEqual(len(combined['rows']), 48)
        self.assertEqual(len(combined['ranking']), 96)
        self.assertEqual(len({config_fingerprint(row) for row in combined['ranking']}), 96)
        for mode in COSTS:
            separate = self.runs[mode]
            self.assertEqual([row for row in combined['rows'] if row['成本模式'] == mode], separate['rows'])
            self.assertEqual({config_fingerprint(row): row for row in combined['ranking'] if row['成本模式'] == mode},
                             {config_fingerprint(row): row for row in separate['ranking']})
            for tp in ('2', '2421'):
                for code in ('154', '156', '263'):
                    active = [row for row in separate['rows'] if row['止盈方案编号'] == tp and row['1分钟条件代码'] == code]
                    self.assertGreater(sum(int(row['交易次数（单）']) for row in active), 0)
        key = lambda row: (row['止盈方案编号'], row['入场触发口径'], row['1分钟条件代码'], row['开仓位置过滤代码'])
        fee_rows = {key(row): row for row in self.runs['FEE']['rows']}
        self.assertTrue(any(row['所选仓位期末资金（USDC）'] != fee_rows[key(row)]['所选仓位期末资金（USDC）']
                            for row in self.runs['SLIPPAGE']['rows']))

    def test_scalar_cost_and_effective_rates_exported_without_double_charging(self):
        for row in self.runs['combined']['rows']:
            mode = row['成本模式']
            self.assertEqual(float(row['平仓后最小开仓间隔（分钟）']), 5)
            self.assertEqual(float(row['开仓基础手续费率（%）']), .0004)
            self.assertEqual(float(row['平仓基础手续费率（%）']), .0005)
            self.assertEqual(float(row['开仓净手续费率（%）']), .0004 * .9 * .7 if mode == 'FEE' else 0.)
            self.assertEqual(float(row['平仓净手续费率（%）']), .0005 * .9 * .7 if mode == 'FEE' else 0.)
            self.assertEqual(float(row['开仓成交偏移（%）']), 0.00005 if mode == 'SLIPPAGE' else 0.)
            self.assertEqual(float(row['平仓成交偏移（%）']), 0.00005 if mode == 'SLIPPAGE' else 0.)
        start = next(event for event in self.runs['combined']['events'] if event['type'] == 'start')
        self.assertEqual((start['csv_rows'], start['total_combinations']), (48, 96))
        progress = [event for event in self.runs['combined']['events'] if event['type'] == 'inner_progress']
        self.assertTrue(all(event['base_total'] == 24 for event in progress))
        self.assertEqual(progress[-1]['base_done'], 24)
        self.assertEqual(self.runs['combined']['checkpoint']['selection']['成本模式'], COSTS)
        context = self.runs['combined']['ranking_context']
        self.assertEqual(context['schema'], 1)
        self.assertEqual(context['selection'], self.config)
        self.assertEqual(context['identity'], self.runs['combined']['checkpoint']['run_identity'])
        output = self.runs['combined']['output']
        self.assertEqual(context['data'], json.loads((output / '回测数据说明.json').read_text('utf-8')))
        self.assertEqual(context, json.loads((output / '各类止盈最差1000名.json').read_text('utf-8'))['运行上下文'])
        self.assertEqual(self.runs['combined']['checkpoint']['worst_scope'], 'ALL_VALID_COMPLETED_V1')
        for name in ('各类止盈前5000名.json', '各类止盈最差1000名.json'):
            payload = json.loads((output / name).read_text('utf-8'))
            self.assertEqual(payload['worst_scope'], 'ALL_VALID_COMPLETED_V1')
            self.assertEqual(payload['排行指标'], '期末资金（USDC）')
            self.assertEqual(payload['组合门槛'], [])

    def test_resume_at_cost_boundary_is_byte_identical_and_keeps_heaps(self):
        stopped = self.run_worker('resume', self.config, stop_at=12)
        self.assertEqual({row['成本模式'] for row in stopped['rows']}, {'SLIPPAGE'})
        self.assertEqual((stopped['checkpoint']['next_tp'], stopped['checkpoint']['entry_batch_next'],
                          stopped['checkpoint']['rows']), (0, 2, 12))
        self.assertIn('stopped', [event['type'] for event in stopped['events']])
        resumed = self.run_worker('resume', self.config)
        self.assertEqual(resumed['rows'], self.runs['combined']['rows'])
        self.assertEqual(resumed['ranking'], self.runs['combined']['ranking'])
        self.assertEqual((resumed['output'] / '全部回测结果.csv').read_bytes(),
                         (self.runs['combined']['output'] / '全部回测结果.csv').read_bytes())

    def test_legacy_filtered_scope_resume_rebuilds_only_committed_csv(self):
        stopped = self.run_worker('legacy_resume', self.config, stop_at=12)
        checkpoint = stopped['checkpoint']
        checkpoint.pop('worst_scope')
        checkpoint['worst'] = {}
        path = stopped['output'] / '断点记录.json'
        path.write_text(json.dumps(checkpoint, ensure_ascii=False), 'utf-8')
        with (stopped['output'] / '全部回测结果.csv').open('ab') as handle:
            handle.write(b'uncommitted partial row\n')
        resumed = self.run_worker('legacy_resume', self.config)
        self.assertEqual(resumed['rows'], self.runs['combined']['rows'])
        def assert_rebuilt_rows(actual, expected):
            actual = {config_fingerprint(row): row for row in actual}
            expected = {config_fingerprint(row): row for row in expected}
            self.assertEqual(actual.keys(), expected.keys())
            for fingerprint, old in expected.items():
                for name, value in old.items():
                    got = actual[fingerprint][name]
                    if isinstance(value, (float, int)):
                        # CSV financial arrays use .8e; encoded account statistics use .16g.
                        self.assertTrue(math.isclose(got, value, rel_tol=5e-9, abs_tol=1e-12),
                                        (fingerprint, name, got, value))
                    else:
                        self.assertEqual(got, value, (fingerprint, name))
        assert_rebuilt_rows(resumed['ranking'], self.runs['combined']['ranking'])
        headers = json.loads((resumed['output'] / '各类止盈最差1000名.json').read_text('utf-8'))['表头']
        flatten = lambda categories: [dict(zip(headers, row)) for rows in categories.values() for row in rows]
        assert_rebuilt_rows(flatten(resumed['checkpoint']['worst']),
                            flatten(self.runs['combined']['checkpoint']['worst']))
        self.assertEqual(resumed['checkpoint']['worst_scope'], 'ALL_VALID_COMPLETED_V1')
        stages = [event.get('message', '') for event in resumed['events'] if event['type'] == 'stage']
        self.assertEqual(sum('正在从已保存CSV重建' in message for message in stages), 1)
        self.assertEqual((resumed['output'] / '全部回测结果.csv').read_bytes(),
                         (self.runs['combined']['output'] / '全部回测结果.csv').read_bytes())

    def test_zero_fees_are_a_legal_independent_actual_worker_branch(self):
        config = copy.deepcopy(self.config)
        config.update({'入场触发口径': 'TF_EVENT', '开仓位置过滤': ['OFF'],
                       '成交偏移': {'开仓': 0., '平仓': 0.},
                       '手续费': {'开仓费率': 0., '平仓费率': 0., 'BNB抵扣': False, '返佣比例': 0.}})
        config['开仓条件']['1m'] = [156]
        zero = self.run_worker('zero', config)
        self.assertEqual(len(zero['rows']), 4)
        by_key = {}
        for row in zero['rows']:
            self.assertEqual(float(row['开仓净手续费率（%）']), 0.)
            self.assertEqual(float(row['平仓净手续费率（%）']), 0.)
            self.assertGreater(int(row['交易次数（单）']), 0)
            without_mode = {key: value for key, value in row.items() if key != '成本模式'}
            if row['止盈方案编号'] in by_key:
                self.assertEqual(without_mode, by_key[row['止盈方案编号']])
            else:
                by_key[row['止盈方案编号']] = without_mode


if __name__ == '__main__':
    unittest.main()
