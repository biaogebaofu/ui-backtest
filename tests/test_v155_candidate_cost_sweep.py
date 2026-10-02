"""A declared cost/entry sweep survives candidate screening and CSV rebuild."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import candidate_export as C
import worst_export as W
from ranking_view import CODE_NAMES, config_fingerprint
from selection_config import 规范化配置, 规范化候选筛选
from strategy_description import load_context
import test_candidate_return_drawdown as candidate_fixtures
from test_v155_multi_cost_backend import small_config


class DeclaredCostSweepExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v155_cost_export_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = candidate_fixtures.ReturnDrawdownTests()
        self.fixture.root = self.root
        config = small_config()
        config.update({'成本模式': ['SLIPPAGE', 'FEE'], '入场触发口径': ['LIVE_01', 'TF_EVENT'],
                       '止盈后等待分钟': [1], '仓位倍数': [1., 2., 5.],
                       '成交偏移': {'开仓': 0.00005, '平仓': 0.00005},
                       '手续费': {'开仓费率': .0004, '平仓费率': .0004, 'BNB抵扣': True, '返佣比例': .3},
                       '平仓后最小开仓间隔分钟': 5, '成交价格口径': 'CLOSE_CONFIRMED'})
        config['资金约束']['最大开仓数量ETH'] = 20.
        self.selection = 规范化配置(config)
        self.rows = []
        for entry in ('LIVE_01', 'TF_EVENT'):
            for cost in ('SLIPPAGE', 'FEE'):
                row = self.fixture.sample()
                row.update({'入场触发口径': entry, '成本模式': cost,
                            '回测计算版本': 'v155-fixture:CLOSE_CONFIRMED'})
                if cost == 'FEE':
                    row.update({'开仓成交偏移（%）': 0., '平仓成交偏移（%）': 0., '往返成交偏移（%）': 0.,
                                '开仓净手续费率（%）': .0004 * .9 * .7,
                                '平仓净手续费率（%）': .0004 * .9 * .7})
                self.rows.append(row)

    def prepare(self, rows=None, selection=None, context=True):
        source = self.fixture.write_source(self.rows if rows is None else rows)
        if context:
            raw = copy.deepcopy(self.selection if selection is None else selection)
            data = {'start_utc': '2025-01-01T00:00:00Z', 'end_utc': '2026-02-01T00:00:00Z',
                    'sources': {'kline': 'synthetic.csv', 'micro': '', 'funding': '', 'oi': '', 'bundle': ''},
                    'request': {'start': '', 'end': '', 'fingerprint': '0' * 64, 'feature_version': 7}}
            trade = copy.deepcopy(raw)
            trade.pop('候选筛选')
            signature = hashlib.sha256(json.dumps(trade, ensure_ascii=False, sort_keys=True,
                                                  separators=(',', ':')).encode()).hexdigest()
            identity = {'selection_signature': signature, 'feature_request': data['request'],
                        'engine_version': 'v155-fixture', 'code_sha256': '1' * 64}
            self.expected_context = {'schema': 1, 'selection': raw, 'data': data, 'identity': identity}
            for name, value in [('组合选择.json', raw), ('回测数据说明.json', data),
                                ('回测运行身份.json', identity),
                                ('断点记录.json', {'selection': raw, 'run_identity': identity})]:
                (self.root / name).write_text(json.dumps(value, ensure_ascii=False), 'utf-8')
        return source

    def screen(self):
        with mock.patch.object(C, 'export_excel'), mock.patch.object(C, 'emit'):
            C.run_return_drawdown(self.root, self.root, self.root / '全部回测结果.csv',
                                  规范化候选筛选(None), C.read_tp_dictionary(self.root / '止盈方案字典.csv'))
        return json.loads((self.root / '分层候选数据_全部实际倍数.json').read_text('utf-8'))

    def rebuild(self):
        tp = SimpleNamespace(编号=1, 类别='类别1', 周期组合='1m', 指标='测试', 参数一=.001, 参数二=.01, 参数三=0)
        with mock.patch.object(sys, 'argv', ['worst_export', '--output', str(self.root), '--json-only']), \
             mock.patch.object(W, '生成止盈方案', return_value=[tp]), mock.patch.object(W, 'emit'):
            W.main()
        return json.loads((self.root / '各类止盈前5000名.json').read_text('utf-8'))

    def test_declared_two_costs_two_entry_modes_keep_all_accounts_and_fingerprints(self):
        self.prepare()
        result = self.screen()
        self.assertEqual(result['筛选阶段统计']['比较账户数'], 12)
        self.assertEqual(result['筛选阶段统计']['数据有效账户数'], 12)
        self.assertEqual(result['研究候选数'], 12)
        records = result['研究候选']
        self.assertEqual(len({row['策略指纹'] for row in records}), 12)
        for mode in ('SLIPPAGE', 'FEE'):
            self.assertEqual(sum(row['成本模式'] == mode for row in records), 6)
        self.assertEqual({row['入场触发口径'] for row in records}, {'LIVE_01', 'TF_EVENT'})

    def test_unselected_modes_double_charging_and_wrong_global_parameters_are_rejected(self):
        for changes in ({'入场触发口径': 'MACD_CYCLE'}, {'开仓净手续费率（%）': .0004},
                        {'往返成交偏移（%）': .1}, {'初始资金（USDC）': 200.},
                        {'回测计算版本': 'wrong'}, {'平仓后最小开仓间隔（分钟）': 0}):
            with self.subTest(changes=changes):
                self.prepare([dict(self.rows[0], **changes)])
                with self.assertRaises(ValueError):
                    self.screen()
        self.prepare([self.rows[1]], dict(self.selection, 成本模式='SLIPPAGE'))
        with self.assertRaisesRegex(ValueError, '成本模式不属于原运行选择'):
            self.screen()
        self.prepare([dict(self.rows[1], **{'开仓成交偏移（%）': 0.00005})])
        with self.assertRaisesRegex(ValueError, '开仓成交偏移'):
            self.screen()

    def test_unknown_csv_cannot_enable_sweep_by_showing_two_mode_rows(self):
        self.prepare(self.rows[:2], context=False)
        with self.assertRaisesRegex(ValueError, '混合'):
            self.screen()

    def test_old_progress_only_checkpoint_does_not_pretend_to_be_run_context(self):
        self.prepare([self.rows[0]], context=False)
        (self.root / '断点记录.json').write_text(json.dumps({'rows': 1, 'next_tp': 1}), 'utf-8')
        self.assertIsNone(C.original_run_context(self.root))
        self.assertNotIn('运行上下文', self.rebuild())

    def test_data_request_and_original_selection_must_match_the_run_identity(self):
        self.prepare()
        path = self.root / '回测数据说明.json'
        data = json.loads(path.read_text('utf-8'))
        data['request']['end'] = '2025-01-02'
        path.write_text(json.dumps(data), 'utf-8')
        with self.assertRaisesRegex(ValueError, '上下文核验失败'):
            self.screen()
        self.prepare()
        path = self.root / '组合选择.json'
        selection = json.loads(path.read_text('utf-8'))
        selection['资金约束']['保护止损浮亏比例'] = .5
        path.write_text(json.dumps(selection), 'utf-8')
        with self.assertRaisesRegex(ValueError, '上下文核验失败'):
            self.rebuild()

    def test_csv_rebuild_preserves_context_and_both_cost_heaps(self):
        self.prepare()
        screened = self.screen()
        top = self.rebuild()
        worst = json.loads((self.root / '各类止盈最差1000名.json').read_text('utf-8'))
        self.assertEqual(top['运行上下文'], self.expected_context)
        self.assertEqual(worst['运行上下文'], self.expected_context)
        self.assertNotIn('error', load_context(top, self.root, CODE_NAMES))
        native = [dict(zip(top['表头'], row)) for rows in top['分类'].values() for row in rows]
        self.assertEqual(len(native), 12)
        self.assertEqual({row['成本模式'] for row in native}, {'SLIPPAGE', 'FEE'})
        self.assertEqual({config_fingerprint(row) for row in native},
                         {row['策略指纹'] for row in screened['研究候选']})

    def test_rebuild_rejects_inconsistent_csv_before_overwriting_rankings(self):
        self.prepare()
        top = self.rebuild()
        path = self.root / '各类止盈前5000名.json'
        before = path.read_bytes()
        self.prepare([dict(self.rows[1], **{'开仓成交偏移（%）': 0.00005})])
        with self.assertRaisesRegex(ValueError, '开仓成交偏移'):
            self.rebuild()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(top['运行上下文'], self.expected_context)

    def test_old_schema_context_keeps_raw_signed_selection_when_rebuilt(self):
        old = dict(self.selection, 版本=19, 成本模式='SLIPPAGE', 入场触发口径='LIVE_01')
        old.pop('开仓位置过滤')
        self.prepare([self.rows[0]], old)
        payload = self.rebuild()
        self.assertEqual(payload['运行上下文'], self.expected_context)
        self.assertEqual(payload['运行上下文']['selection']['版本'], 19)
        self.assertNotIn('error', load_context(payload, None, CODE_NAMES))

    def test_older_scalar_export_still_works_but_cannot_authorize_new_sweeps(self):
        old = dict(self.selection, 版本=18, 成本模式='SLIPPAGE', 入场触发口径='LIVE_01')
        self.prepare([self.rows[0]], old)
        payload = self.rebuild()
        self.assertNotIn('运行上下文', payload)
        self.assertEqual(len(payload['分类']['类别1']), 3)
        self.prepare(self.rows[:2], old)
        with self.assertRaisesRegex(ValueError, '混合'):
            self.screen()
        self.prepare(self.rows[:2], dict(old, 成本模式=['SLIPPAGE', 'FEE']))
        with self.assertRaisesRegex(ValueError, '上下文核验失败'):
            self.screen()


if __name__ == '__main__':
    unittest.main()
