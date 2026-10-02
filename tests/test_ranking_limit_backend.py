"""Both supported quotas retain real rows and recover old truncated history from CSV."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import backtest_worker as worker
import worst_export as exporter


PROJECT = Path(__file__).resolve().parents[1]


class RankingLimitSettingsTests(unittest.TestCase):
    def tearDown(self):
        worker.应用排行设置(None)

    def test_default_shape_and_strict_optional_limit(self):
        original = {'排行指标': worker.默认排行指标, '门槛': []}
        self.assertEqual(worker.应用排行设置(original), original)
        self.assertEqual(worker.最优排行名额, 5000)
        for limit in (5000, 10000):
            selected = dict(original, 最优名额=limit)
            self.assertEqual(worker.应用排行设置(selected), selected)
            self.assertEqual(worker.当前排行设置(), selected)
        for value in (True, False, 10000., '10000', 0, 9999, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.应用排行设置(dict(original, 最优名额=value))
        self.assertEqual(worker.应用排行设置(original), original)
        self.assertEqual(worker.最优排行名额, 5000)

    def test_live_heap_and_resumed_heap_use_the_selected_capacity(self):
        rows = []
        for value in range(10020):
            row = [0] * len(worker.中文表头)
            row[worker.排行期末资金列] = value + 1
            rows.append(row)
        for limit in (5000, 10000):
            worker.应用排行设置({'最优名额': limit})
            heap = []
            for counter, row in enumerate(rows):
                worker.update_top(heap, row, counter)
            expected = list(range(10020, 10020 - limit, -1))
            self.assertEqual([r[worker.排行期末资金列] for r in worker.heaps_to_rows({'测试': heap})['测试']], expected)
            loaded, _ = worker.load_heaps({'测试': rows})
            self.assertEqual([r[worker.排行期末资金列] for r in worker.heaps_to_rows(loaded)['测试']], expected)
        self.assertEqual(worker.最差排行名额, 1000)


class RankingLimitCsvTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ranking_limit_')
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(worker.应用排行设置, None)
        self.root = Path(self.temporary.name)
        self.source = self.root / '全部回测结果.csv'
        template = {name: '' for name in worker.全量CSV表头}
        template.update({
            '止盈方案编号': 2, '止盈后等待分钟': 0, '基础策略编号': 1,
            '开仓MACD代码': 0, '4小时条件代码': 0, '1小时条件代码': 0,
            '15分钟条件代码': 0, '5分钟条件代码': 0, '1分钟条件代码': 1,
            '止损代码': 'S1', '交易次数（单）': 10, '胜率（%）': .6, '多单占比（%）': .5,
            '平均日完整交易数（次/日）': .1, '平均日成交订单数（笔/日）': .2,
            '平均持仓时间（分钟）': 10, '平均单笔收益率（%）': .001,
            '毛收益合计（%）': .01, '盈亏比（倍）': 1.5, 't值': 2,
            '2025毛收益（%）': .01, '2026毛收益（%）': .02, '所选仓位顺序': '5x',
            '所选仓位期末资金（USDC）': '200', '所选仓位累计收益率（%）': '1',
            '所选仓位最大回撤（%）': '.2', '所选仓位爆仓保护次数（次）': '0',
            '开仓成交偏移（%）': .0001, '平仓成交偏移（%）': .0001, '往返成交偏移（%）': .0002,
            '初始资金（USDC）': 100, 'ETH最小开仓数量（ETH）': .01, 'ETH单次最大开仓数量（ETH）': 100,
            '所选仓位实际成交次数（单）': '10', '所选仓位资金性停机标记（0否1是）': '0',
            '所选仓位期末可开仓数量（ETH）': '.25',
        })
        with self.source.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=worker.全量CSV表头)
            writer.writeheader()
            for value in range(1, 10021):
                writer.writerow(dict(template, 基础策略编号=value,
                                     **{'所选仓位期末资金（USDC）': str(100 + value)}))
        self.checkpoint_path = self.root / '断点记录.json'
        self.initial_checkpoint = {'rows': 10020, 'csv_bytes': self.source.stat().st_size,
                                   'next_tp': 5, 'entry_batch_next': 2, 'entry_base_done': 1,
                                   'top': {}, 'worst': {}, 'worst_scope': worker.WORST_SCOPE}
        self.checkpoint_path.write_text(json.dumps(self.initial_checkpoint), 'utf-8')
        self.settings = self.root / 'requested_settings.json'

    def configure(self, limit):
        settings = {'排行指标': worker.默认排行指标, '门槛': [], '最优名额': limit}
        self.settings.write_text(json.dumps(settings, ensure_ascii=False), 'utf-8')
        worker.应用排行设置(settings)

    def rebuild(self, limit, *options):
        self.configure(limit)
        captured = []
        def export(_project, source, output):
            captured.append((json.loads(source.read_text('utf-8')), output.name))
            return output
        with mock.patch.object(sys, 'argv', ['worst_export', '--output', str(self.root),
                                            '--settings', str(self.settings), *options]), \
             mock.patch.object(exporter, 'export_excel', side_effect=export), \
             mock.patch.object(exporter, 'emit') as emit:
            exporter.main()
        top = json.loads((self.root / '各类止盈前5000名.json').read_text('utf-8'))
        worst = json.loads((self.root / '各类止盈最差1000名.json').read_text('utf-8'))
        return top, worst, captured, emit

    def test_existing_5000_is_rebuilt_to_real_10000_and_smaller_export_keeps_native_capacity(self):
        before_csv = self.source.read_bytes()
        first, worst, _, _ = self.rebuild(5000, '--json-only')
        self.assertEqual(sum(map(len, first['分类'].values())), 5000)
        expanded, worst, exports, emit = self.rebuild(10000, '--export-existing')
        self.assertEqual(expanded['名额'], 10000)
        self.assertEqual(sum(map(len, expanded['分类'].values())), 10000)
        self.assertEqual(sum(map(len, worst['分类'].values())), 1000)
        category = next(iter(first['分类']))
        self.assertEqual(first['分类'][category], expanded['分类'][category][:5000])
        self.assertEqual(exports[0][1], '各类止盈最优前10000名.xlsx')
        completed = next(call.kwargs for call in emit.call_args_list if call.args[0] == 'legacy_completed')
        self.assertEqual((completed['top_limit'], completed['worst_limit']), (10000, 1000))
        self.assertEqual(self.source.read_bytes(), before_csv)
        native_before = (self.root / '各类止盈前5000名.json').read_bytes()
        _, _, exports, _ = self.rebuild(5000, '--export-existing')
        self.assertEqual(exports[0][0]['名额'], 5000)
        self.assertEqual(sum(map(len, exports[0][0]['分类'].values())), 5000)
        self.assertEqual(exports[0][1], '各类止盈最优前5000名.xlsx')
        self.assertEqual((self.root / '各类止盈前5000名.json').read_bytes(), native_before)
        self.assertEqual(list(self.root.glob('.top_export.*.json')), [])

    def test_capacity_expansion_rebuilds_committed_history_without_advancing_the_checkpoint(self):
        self.rebuild(5000, '--json-only')
        checkpoint = json.loads(self.checkpoint_path.read_text('utf-8'))
        checkpoint.pop('top_limit')  # Old default checkpoints had no capacity marker.
        self.checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False), 'utf-8')
        self.configure(10000)
        before_csv = self.source.read_bytes()
        with mock.patch.object(worker, 'emit'):
            worker.ensure_top_limit(PROJECT, self.root, checkpoint, self.settings)
        self.assertEqual(checkpoint['top_limit'], 10000)
        self.assertEqual(sum(map(len, checkpoint['top'].values())), 10000)
        for name in ('rows', 'csv_bytes', 'next_tp', 'entry_batch_next', 'entry_base_done'):
            self.assertEqual(checkpoint[name], self.initial_checkpoint[name])
        self.assertEqual(self.source.read_bytes(), before_csv)
        with mock.patch.object(worker, 'rebuild_legacy_rankings', side_effect=AssertionError('already recovered')):
            worker.ensure_top_limit(PROJECT, self.root, checkpoint, self.settings)

    def test_missing_full_csv_and_uncommitted_tail_cannot_claim_expanded_capacity(self):
        self.rebuild(5000, '--json-only')
        checkpoint = json.loads(self.checkpoint_path.read_text('utf-8'))
        self.configure(10000)
        with self.source.open('ab') as handle:
            handle.write(b'uncommitted')
        with mock.patch.object(worker, 'rebuild_legacy_rankings') as rebuild, self.assertRaises(RuntimeError):
            worker.ensure_top_limit(PROJECT, self.root, checkpoint, self.settings)
        rebuild.assert_not_called()
        self.assertEqual(checkpoint['top_limit'], 5000)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, '缺少完整CSV'):
            self.rebuild(10000, '--export-existing')
        with self.assertRaisesRegex(ValueError, '先从完整CSV重建'):
            self.rebuild(10000, '--attach-existing')


if __name__ == '__main__':
    unittest.main()
