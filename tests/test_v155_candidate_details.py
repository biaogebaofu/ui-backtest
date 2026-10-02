"""Candidate descriptions append verified parameters without changing screening."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import candidate_export as C
from account_statistics import ACCOUNT_COLUMNS, ACCOUNT_FIELD, encode_accounts
from extended_rules import stable_base_id
from ranking_view import config_fingerprint
from selection_config import 配置统计, 规范化候选筛选
from strategy_space import 生成止损组合
import test_v155_candidate_cost_sweep as fixtures


class CandidateDetailsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeclaredCostSweepExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.selection['开仓条件']['1m'] = [152]
        stop_index = next(index for index, spec in enumerate(生成止损组合()) if spec[0] == 'S1')
        self.base_id = stable_base_id(0, (0, 0, 0, 0, 152), stop_index)
        account = dict(zip(ACCOUNT_COLUMNS, [300, .6, .5, .5, 1., 10., .001, .3, 1.5, 4., .1, .2, 300., .3]))
        for row in self.fixture.rows:
            row['1分钟条件代码'] = 152
            row['基础策略编号'] = self.base_id
            row[ACCOUNT_FIELD] = encode_accounts([account, account, account])

    def export(self, scheme, details=True):
        settings = 规范化候选筛选(None)
        settings.update(筛选方案=scheme, 比较范围='ALL_RUN')
        settings_path = self.root / 'settings.json'
        settings_path.write_text(json.dumps(settings), 'utf-8')
        with mock.patch.object(sys, 'argv', ['candidate', '--output', str(self.root), '--settings', str(settings_path)]), \
             mock.patch.object(C, 'export_excel'), mock.patch.object(C, 'emit'):
            if details:
                C.main()
            else:
                with mock.patch.object(C, 'append_candidate_details'):
                    C.main()
        return json.loads((self.root / '分层候选数据_全部实际倍数.json').read_text('utf-8'))

    def test_return_and_legacy_keep_every_old_value_and_append_exact_single_configs(self):
        self.fixture.prepare()
        old_headers = [name for name in C.输出表头 if name not in C.附加参数表头]
        for scheme in ('RETURN_DRAWDOWN', 'LEGACY_STRESS'):
            with self.subTest(scheme=scheme):
                baseline = self.export(scheme, details=False)
                current = self.export(scheme)
                self.assertEqual(current['研究候选数'], 12)
                self.assertEqual(current['筛选阶段统计'], baseline['筛选阶段统计'])
                self.assertEqual(current['淘汰统计'], baseline['淘汰统计'])
                for group in ('研究候选', '观察候选'):
                    self.assertEqual(len(current[group]), len(baseline[group]))
                    for before, after in zip(baseline[group], current[group]):
                        for key in old_headers:
                            self.assertEqual(after[key], str(before[key]) if key == '基础策略编号' else before[key], key)
                        self.assertIn('已按原运行身份核对', after['参数完整性'])
                        envelope = json.loads(after['完整单策略配置JSON'])
                        selected = envelope['selection']
                        self.assertEqual(配置统计(selected)['包含仓位完整组合数'], 1)
                        self.assertEqual(selected['仓位倍数'], [after['名义倍数（倍）']])
                        self.assertEqual(selected['成本模式'], after['成本模式'])
                        self.assertEqual(selected['入场触发口径'], after['入场触发口径'])
                        self.assertEqual(selected['开仓条件']['1m'], [152])
                        self.assertEqual(selected['资金约束'], self.fixture.selection['资金约束'])
                        self.assertEqual(selected['手续费'], self.fixture.selection['手续费'])
                        self.assertEqual(selected['成交偏移'], self.fixture.selection['成交偏移'])
                        self.assertEqual(envelope['request'], self.fixture.expected_context['data']['request'])
                        self.assertIn('真实参数=[9]', after['开仓判据（当前代码解释）'])
                        self.assertEqual(after['基础策略编号'], str(self.base_id))
                native_payload = json.loads((self.root / '候选原始记录.json').read_text('utf-8'))
                natives = [dict(zip(native_payload['表头'], row)) for rows in native_payload['分类'].values() for row in rows]
                self.assertTrue(all(type(row['基础策略编号']) is int for row in natives))
                self.assertEqual({config_fingerprint(row) for row in natives}, {row['策略指纹'] for row in current['研究候选']})
                self.assertNotIn('完整单策略配置JSON', native_payload['表头'])

    def test_no_source_context_is_explicitly_incomplete_without_guessed_json(self):
        self.fixture.prepare([self.fixture.rows[0]], context=False)
        result = self.export('RETURN_DRAWDOWN')
        self.assertEqual(result['研究候选数'], 3)
        for row in result['研究候选'] + result['观察候选']:
            self.assertIn('不完整', row['参数完整性'])
            self.assertEqual(row['完整单策略配置JSON'], '')
            self.assertEqual(row['保护止损浮亏比例（%）'], '未记录')
            self.assertEqual(row['原核心代码SHA256'], '未记录')

    def test_missing_native_does_not_create_a_strategy_from_candidate_metric_fields(self):
        self.fixture.prepare()
        context = C.original_run_context(self.root)
        candidate = {'策略指纹': 'f' * 16, '期末资金（USDC）': 999., '基础策略编号': 1}
        C.append_candidate_details([candidate], [], self.root, context)
        self.assertEqual(candidate['期末资金（USDC）'], 999.)
        self.assertEqual(candidate['基础策略编号'], 1)
        self.assertIn('缺少匹配', candidate['参数完整性'])
        self.assertEqual(candidate['完整单策略配置JSON'], '')
        self.assertEqual(candidate['1m条件代码'], '未记录')

    def test_overlong_full_json_is_rejected_not_truncated(self):
        self.fixture.prepare()
        result = self.export('RETURN_DRAWDOWN')
        native_payload = json.loads((self.root / '候选原始记录.json').read_text('utf-8'))
        native = dict(zip(native_payload['表头'], next(iter(native_payload['分类'].values()))[0]))
        candidate = next(copy.deepcopy(row) for row in result['研究候选'] if row['策略指纹'] == config_fingerprint(native))
        with mock.patch.object(C, 'describe_row', return_value={'完整单策略配置JSON': 'a' * 32768}):
            with self.assertRaisesRegex(ValueError, '32767'):
                C.append_candidate_details([candidate], [native], self.root)


if __name__ == '__main__':
    unittest.main()
