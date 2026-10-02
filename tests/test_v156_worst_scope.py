"""Worst ranks use all valid accounts; old filtered heaps recover from committed CSV."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import backtest_worker as B
import worst_export as W
import test_v155_candidate_cost_sweep as fixtures


PROJECT = Path(__file__).resolve().parents[1]
SETTINGS = {'排行指标': B.默认排行指标, '门槛': [
    {'指标': '期末资金（USDC）', '条件': '最低值', '值': 10000},
    {'指标': '最大回撤（%）越小越好', '条件': '最高值', '值': .5}]}


class WorstScopeHeapTests(unittest.TestCase):
    def tearDown(self):
        B.应用排行设置({'排行指标': B.默认排行指标, '门槛': []})

    def row(self, funds=0., drawdown=1., halted=1):
        row = [0] * len(B.中文表头)
        for name, value in {'期末资金（USDC）': funds, '最大回撤（%）': drawdown,
                            '资金性停机标记（0否1是）': halted, '成本模式': 'FEE'}.items():
            row[B.中文表头.index(name)] = value
        return row

    def test_top_gates_do_not_hide_halted_or_zero_capital_worst_accounts(self):
        B.应用排行设置(SETTINGS)
        rows = [self.row(), self.row(4., .996), self.row(11000., .1, 0)]
        top, worst = [], []
        for counter, row in enumerate(rows):
            B.update_top(top, row, counter)
            B.update_worst(worst, row, counter)
        self.assertEqual([item[2] for item in top], [rows[2]])
        self.assertEqual(B.worst_heaps_to_rows({'x': worst})['x'], rows)
        loaded_top, _ = B.load_heaps({'x': rows})
        loaded_worst, _ = B.load_worst_heaps({'x': rows})
        self.assertEqual(len(loaded_top['x']), 1)
        self.assertEqual(B.worst_heaps_to_rows(loaded_worst)['x'], rows)

    def test_invalid_ranking_account_and_tie_numbers_never_enter_either_heap(self):
        B.应用排行设置({'排行指标': '胜率（%）', '门槛': []})
        for column in ('胜率（%）', '期末资金（USDC）', '最大回撤（%）', '爆仓保护次数（次）'):
            for value in (float('nan'), float('inf'), float('-inf'), '无数据'):
                with self.subTest(column=column, value=value):
                    row = self.row()
                    row[B.中文表头.index(column)] = value
                    top, worst = [], []
                    B.update_top(top, row, 1)
                    B.update_worst(worst, row, 1)
                    self.assertEqual((top, worst), ([], []))

    def test_unrelated_metric_is_not_an_extra_gate(self):
        row = self.row()
        row[B.中文表头.index('盈亏比（倍）')] = float('inf')
        row[B.中文表头.index('t值')] = float('nan')
        self.assertTrue(B.排行数值有效(row))
        B.应用排行设置({'排行指标': 't值', '门槛': []})
        self.assertFalse(B.排行数值有效(row))

    def test_smaller_is_better_and_worst_capacity_are_preserved(self):
        B.应用排行设置({'排行指标': '最大回撤（%）越小越好', '门槛': [
            {'指标': '最大回撤（%）越小越好', '条件': '最高值', '值': .1}]})
        rows = [self.row(100., x) for x in (.1, .8, .4, 1.)]
        with mock.patch.object(B, '最差排行名额', 2):
            heap = []
            for counter, row in enumerate(rows):
                B.update_worst(heap, row, counter)
            expected = [rows[3], rows[1]]
            self.assertEqual(B.worst_heaps_to_rows({'x': heap})['x'], expected)
            loaded, _ = B.load_worst_heaps({'x': rows})
            self.assertEqual(B.worst_heaps_to_rows(loaded)['x'], expected)


class WorstScopeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeclaredCostSweepExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        for row in self.fixture.rows:
            if row['成本模式'] == 'FEE':
                row['所选仓位期末资金（USDC）'] = '0;3.23688292;4.1242883'
                row['所选仓位累计收益率（%）'] = '-1;-.9676311708;-.958757117'
                row['所选仓位最大回撤（%）'] = '1;.997;.996'
                row['所选仓位资金性停机标记（0否1是）'] = '1;1;1'
        self.source = self.fixture.prepare()
        self.settings = self.root / '排行榜设置.json'
        self.settings.write_text(json.dumps(SETTINGS, ensure_ascii=False), 'utf-8')
        self.settings_before = self.settings.read_bytes()
        self.checkpoint_path = self.root / '断点记录.json'
        self.checkpoint = json.loads(self.checkpoint_path.read_text('utf-8'))
        self.checkpoint.update(rows=4, csv_bytes=self.source.stat().st_size, next_tp=7,
                               entry_batch_next=2, top={}, worst={})
        self.checkpoint_path.write_text(json.dumps(self.checkpoint, ensure_ascii=False), 'utf-8')
        self.addCleanup(B.应用排行设置, {'排行指标': B.默认排行指标, '门槛': []})

    def rebuild(self, *options):
        with mock.patch.object(sys, 'argv', ['worst_export', '--output', str(self.root),
                                            '--settings', str(self.settings), '--json-only', *options]), \
             mock.patch.object(W, 'emit'):
            W.main()
        return [json.loads((self.root / name).read_text('utf-8')) for name in
                ('各类止盈前5000名.json', '各类止盈最差1000名.json')]

    def test_csv_rebuild_preserves_gates_context_and_each_cost_account(self):
        top, worst = self.rebuild()
        self.assertEqual(sum(map(len, top['分类'].values())), 0)
        rows = [dict(zip(worst['表头'], row)) for values in worst['分类'].values() for row in values]
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(row['成本模式'] == 'FEE' for row in rows), 6)
        self.assertEqual(sum(row['资金性停机标记（0否1是）'] == 1 for row in rows), 6)
        self.assertEqual(sum(row['期末资金（USDC）'] == 0 for row in rows), 2)
        for payload in (top, worst):
            self.assertEqual(payload['worst_scope'], B.WORST_SCOPE)
            self.assertEqual(payload['组合门槛'], SETTINGS['门槛'])
            self.assertEqual(payload['运行上下文'], self.fixture.expected_context)
        self.assertEqual(self.settings.read_bytes(), self.settings_before)

    def test_old_scope_recovers_once_from_committed_csv_without_replaying(self):
        before_csv = self.source.read_bytes()
        with mock.patch.dict(os.environ, {'PYTHONDONTWRITEBYTECODE': '1'}), mock.patch.object(B, 'emit'):
            B.ensure_worst_scope(PROJECT, self.root, self.checkpoint, self.settings)
        self.assertEqual(self.checkpoint['worst_scope'], B.WORST_SCOPE)
        self.assertEqual(sum(map(len, self.checkpoint['worst'].values())), 12)
        self.assertEqual(sum(map(len, self.checkpoint['top'].values())), 0)
        saved = json.loads(self.checkpoint_path.read_text('utf-8'))
        for name in ('selection', 'run_identity', 'rows', 'csv_bytes', 'next_tp', 'entry_batch_next'):
            self.assertEqual(saved[name], self.checkpoint[name])
        with mock.patch.object(B, 'rebuild_legacy_rankings', side_effect=AssertionError('must only rebuild once')):
            B.ensure_worst_scope(PROJECT, self.root, self.checkpoint, self.settings)
        self.assertEqual(self.source.read_bytes(), before_csv)
        self.assertEqual(self.settings.read_bytes(), self.settings_before)

    def test_uncommitted_csv_or_failed_rebuild_does_not_claim_full_scope(self):
        self.checkpoint['csv_bytes'] -= 1
        with mock.patch.object(B, 'rebuild_legacy_rankings') as rebuild, \
             self.assertRaisesRegex(RuntimeError, '完整CSV.*无需重跑交易'):
            B.ensure_worst_scope(PROJECT, self.root, self.checkpoint, self.settings)
        rebuild.assert_not_called()
        self.checkpoint['csv_bytes'] += 1
        with mock.patch.object(B, 'rebuild_legacy_rankings', return_value=False), mock.patch.object(B, 'emit'), \
             self.assertRaisesRegex(RuntimeError, '重建失败.*无需重跑交易'):
            B.ensure_worst_scope(PROJECT, self.root, self.checkpoint, self.settings)
        self.assertNotIn('worst_scope', self.checkpoint)

    def test_attach_requires_current_scope_and_does_not_modify_checkpoint_on_failure(self):
        top, worst = self.rebuild()
        path = self.root / '各类止盈最差1000名.json'
        old = dict(worst)
        old.pop('worst_scope')
        path.write_text(json.dumps(old, ensure_ascii=False), 'utf-8')
        before = self.checkpoint_path.read_bytes()
        with self.assertRaisesRegex(ValueError, '全量有效结果范围'):
            self.rebuild('--attach-existing')
        self.assertEqual(self.checkpoint_path.read_bytes(), before)
        path.write_text(json.dumps(worst, ensure_ascii=False), 'utf-8')
        self.rebuild('--attach-existing')
        self.assertEqual(json.loads(self.checkpoint_path.read_text('utf-8'))['worst_scope'], B.WORST_SCOPE)


if __name__ == '__main__':
    unittest.main()
