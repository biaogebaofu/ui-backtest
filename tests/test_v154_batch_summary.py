"""Batch summaries expose verified new results without inventing missing metrics."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fingerprint_batch as B
import fingerprint_lookup as F
from ranking_view import config_fingerprint
import test_v149_fingerprint_lookup as fixture
import test_v152_fingerprint_batch as batch_fixture


class BatchSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v154_batch_summary_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = fixture.FingerprintLookupTests()
        self.helper.root = self.root
        directory, _, _ = self.helper.make_result('original')
        payload = json.loads((directory / F.RANKING_FILES[0]).read_text('utf-8'))
        row = dict(zip(payload['表头'], next(iter(payload['分类'].values()))[0]))
        row.update({'回测计算版本': 'v1.48-fixture:CLOSE_CONFIRMED',
                    '期末资金（USDC）': 930864.3960747208, '最大回撤（%）': .40716796853815074,
                    '交易次数（单）': 61127, '胜率（%）': .8399888756196117})
        self.original_row = row
        for filename in F.RANKING_FILES:
            self.write_ranking(directory, row, filename)
        identity = json.loads((directory / '回测运行身份.json').read_text('utf-8'))
        identity['engine_version'] = 'v1.48-fixture'
        checkpoint = json.loads((directory / '断点记录.json').read_text('utf-8'))
        checkpoint['run_identity'] = identity
        self.helper.write_json(directory / '回测运行身份.json', identity)
        self.helper.write_json(directory / '断点记录.json', checkpoint)
        self.match = F.find_fingerprint_matches(config_fingerprint(row), directory)[0]
        self.restored = F.restore_fingerprint_match(self.match)
        self.current_row = dict(row, 回测计算版本=self.restored['current_engine_version'] + ':CLOSE_CONFIRMED')
        self.output = self.root / 'child'
        self.output.mkdir()

    def write_ranking(self, output, row, name=None):
        self.helper.write_json(output / (name or F.RANKING_FILES[0]),
                               {'表头': list(row), '分类': {'分批止盈': [list(row.values())]}})

    def summary(self):
        return B._result_summary(self.output, self.match, self.restored)

    def test_single_actual_result_keeps_precision_and_version_only_fingerprint_mapping(self):
        for name in F.RANKING_FILES:
            self.write_ranking(self.output, self.current_row, name)
        actual = self.summary()
        self.assertEqual(actual['原指纹'], self.match['fingerprint'])
        self.assertEqual(actual['本次结果指纹'], config_fingerprint(self.current_row))
        self.assertNotEqual(actual['本次结果指纹'], actual['原指纹'])
        self.assertEqual(actual['数值对账说明'], '数值一致，版本标识不同')
        self.assertEqual(actual['名义倍数（倍）'], 2.)
        self.assertEqual(actual['期末资金（USDC）'], self.current_row['期末资金（USDC）'])
        self.assertEqual(actual['原结果资金（USDC）'], self.original_row['期末资金（USDC）'])
        self.assertEqual(actual['资金差额（USDC）'], 0.)
        self.assertEqual(actual['胜率（比例，1=100%）'], self.current_row['胜率（%）'])
        self.assertEqual(actual['最大回撤（比例，1=100%）'], self.current_row['最大回撤（%）'])
        self.assertIn(self.current_row['1分钟条件'], actual['开仓条件'])

    def test_metrics_are_taken_from_child_and_differences_are_not_hidden(self):
        changed = dict(self.current_row, **{'期末资金（USDC）': 12.25, '交易次数（单）': 17,
                                           '胜率（%）': .25, '最大回撤（%）': .6})
        self.write_ranking(self.output, changed)
        actual = self.summary()
        self.assertEqual(actual['期末资金（USDC）'], 12.25)
        self.assertEqual(actual['交易次数（单）'], 17)
        self.assertEqual(actual['资金差额（USDC）'], 12.25 - self.original_row['期末资金（USDC）'])
        self.assertIn('数值存在差异', actual['数值对账说明'])
        self.assertIn('期末资金', actual['数值对账说明'])

    def test_absent_empty_or_invalid_rankings_never_copy_original_performance(self):
        for kind in ('absent', 'empty', 'mismatched', 'nonfinite', 'conflicting'):
            with self.subTest(kind=kind):
                output = self.root / kind
                output.mkdir()
                if kind == 'empty':
                    self.helper.write_json(output / F.RANKING_FILES[0], {'表头': list(self.current_row), '分类': {}})
                elif kind == 'mismatched':
                    self.write_ranking(output, dict(self.current_row, **{'名义倍数（倍）': 9.}))
                elif kind == 'nonfinite':
                    self.write_ranking(output, dict(self.current_row, **{'期末资金（USDC）': float('nan')}))
                elif kind == 'conflicting':
                    self.write_ranking(output, self.current_row)
                    self.write_ranking(output, dict(self.current_row, **{'期末资金（USDC）': 123.}), F.RANKING_FILES[1])
                actual = B._result_summary(output, self.match, self.restored)
                for key in ('本次结果指纹', '名义倍数（倍）', '期末资金（USDC）', '资金差额（USDC）', '交易次数（单）'):
                    self.assertNotIn(key, actual)
                self.assertEqual(actual['原结果资金（USDC）'], self.original_row['期末资金（USDC）'])
                self.assertIn('本次数值留空', actual['数值对账说明'])

    def test_original_missing_metrics_does_not_claim_full_numeric_agreement(self):
        self.write_ranking(self.output, self.current_row)
        match = copy.deepcopy(self.match)
        match['row'].pop('胜率（%）')
        actual = B._result_summary(self.output, match, self.restored)
        self.assertEqual(actual['期末资金（USDC）'], self.current_row['期末资金（USDC）'])
        self.assertIn('原记录数值不完整', actual['数值对账说明'])

    def test_completed_batch_persists_summary_and_preserves_old_seven_columns(self):
        path = self.root / 'manifest.json'
        self.helper.write_json(path, {'version': 1, 'strategies': [self.match]})
        prior_files = {p: p.read_bytes() for p in Path(self.match['source_dir']).iterdir() if p.is_file()}
        runner = batch_fixture.FingerprintBatchTests()
        runner.fixture = self.helper

        def finish(*args):
            status = runner.fake_complete(*args)
            self.write_ranking(args[3], self.current_row)
            return status

        target = self.root / 'batch'
        with mock.patch.object(B, '_run_child', side_effect=finish), mock.patch.object(B, 'emit'):
            state = B.run_batch(path, target)
        self.assertEqual(state['status'], 'completed')
        self.assertTrue(state['items'][0]['identity_verified'])
        self.assertEqual(state['items'][0]['result_summary']['本次结果指纹'], config_fingerprint(self.current_row))
        with (target / '批量结果汇总.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            self.assertEqual(tuple(reader.fieldnames[:7]), B.SUMMARY_HEADERS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['策略指纹'], self.match['fingerprint'])
        self.assertEqual(rows[0]['原指纹'], self.match['fingerprint'])
        self.assertEqual(rows[0]['本次结果指纹'], config_fingerprint(self.current_row))
        self.assertEqual(float(rows[0]['期末资金（USDC）']), self.current_row['期末资金（USDC）'])
        saved = json.loads((target / B.STATUS_FILE).read_text('utf-8'))
        self.assertEqual(saved['items'][0]['result_summary'], state['items'][0]['result_summary'])
        self.assertEqual({p: p.read_bytes() for p in prior_files}, prior_files)


if __name__ == '__main__':
    unittest.main()
