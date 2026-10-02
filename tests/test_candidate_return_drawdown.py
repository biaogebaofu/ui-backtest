import csv
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import candidate_export as C
import worst_export as W
from account_statistics import ACCOUNT_COLUMNS, ACCOUNT_FIELD, encode_accounts
from backtest_worker import 全量CSV表头
from ranking_view import config_fingerprint
from selection_config import 规范化候选筛选, 全选配置, 配置签名
from test_v147_export_execution import sample_row


class ReturnDrawdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sample(self, sizes=(1, 2, 5), finals=(1000, 950, 900), drawdowns=(.2, .1, .3), trades=None):
        row = sample_row()
        n = len(sizes)
        row.update({'成本模式': 'SLIPPAGE', '开仓净手续费率（%）': 0, '平仓净手续费率（%）': 0,
                    '往返成交偏移（%）': 0.0001, '成交价格口径': 'CLOSE_CONFIRMED',
                    '固定止损代码': 'OFF', '叠加止盈代码': 'OFF', '开仓方向': 'BOTH',
                    '交易会话': 'ALL', '强制时间止损（分钟）': 0, '止损代码': 'S1',
                    '开仓位置过滤代码': 'OFF', '开仓位置过滤说明': '关闭位置过滤（原策略基线）',
                    '所选仓位顺序': ';'.join(f'{x}x' for x in sizes),
                    '所选仓位期末资金（USDC）': ';'.join(map(str, finals)),
                    '所选仓位累计收益率（%）': ';'.join(str(x/100-1) for x in finals),
                    '所选仓位最大回撤（%）': ';'.join(map(str, drawdowns)),
                    '所选仓位爆仓保护次数（次）': ';'.join(['0']*n),
                    '所选仓位全仓强平次数（次）': ';'.join(['0']*n),
                    '所选仓位实际成交次数（单）': ';'.join(map(str, trades or [300]*n)),
                    '所选仓位资金性停机标记（0否1是）': ';'.join(['0']*n),
                    '所选仓位期末可开仓数量（ETH）': ';'.join(['.25']*n)})
        accounts = []
        for i in range(n):
            accounts.append(dict(zip(ACCOUNT_COLUMNS, [(trades or [300]*n)[i], .6, .95, .5, 1., 10.,
                                                       .001, .3, 1.5, 4., -.1, .2, 300., .9])))
        row[ACCOUNT_FIELD] = encode_accounts(accounts)
        return row

    def write_source(self, rows):
        source = self.root / '全部回测结果.csv'
        with source.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=全量CSV表头, extrasaction='ignore')
            writer.writeheader(); writer.writerows(rows)
        with (self.root / '止盈方案字典.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['止盈方案编号', '止盈类别', '周期组合', '指标', '参数一（原始小数）', '参数二（原始小数）', '参数三（原始小数）'])
            writer.writeheader()
            for tp_id in (1, 2): writer.writerow({'止盈方案编号': tp_id, '止盈类别': f'类别{tp_id}', '周期组合': '1m', '指标': '测试', '参数一（原始小数）': .001, '参数二（原始小数）': .01, '参数三（原始小数）': 0})
        (self.root / '回测数据说明.json').write_text(json.dumps({'start_utc': '2025-01-01T00:00:00Z', 'end_utc': '2026-02-01T00:00:00Z'}), 'utf-8')
        return source

    def run_export(self, rows, **changes):
        self.write_source(rows)
        settings = 规范化候选筛选(None)
        settings.update(changes)
        settings_path = self.root / '候选设置.json'
        settings_path.write_text(json.dumps(settings), 'utf-8')
        with mock.patch.object(sys, 'argv', ['candidate_export', '--output', str(self.root), '--settings', str(settings_path)]), mock.patch.object(C, 'export_excel'):
            C.main()
        tag = '全部实际倍数' if settings['比较范围'] == 'ALL_RUN' else f'{settings["统一目标杠杆"]:g}x'
        return json.loads((self.root / f'分层候选数据_{tag}.json').read_text('utf-8'))

    def test_new_defaults_and_explicit_history_keep_different_contracts(self):
        new = 规范化候选筛选(None)
        old = 规范化候选筛选({'每类最多': 7})
        self.assertEqual((new['筛选方案'], new['比较范围'], new['资金保留比例']), ('RETURN_DRAWDOWN', 'ALL_RUN', .9))
        self.assertEqual((old['筛选方案'], old['比较范围'], old['资金保留比例']), ('LEGACY_STRESS', 'TARGET', .9))
        config = 全选配置(); signature = 配置签名(config)
        config['候选筛选'] = old
        self.assertEqual(配置签名(config), signature)
        for patch in ({'筛选方案': 'bad'}, {'比较范围': 'bad'}, {'资金保留比例': 0}, {'资金保留比例': 1.1}, {'资金保留比例': float('nan')}):
            with self.assertRaises(ValueError): 规范化候选筛选(patch)

    def test_every_real_account_uses_own_trades_and_global_qualified_maximum(self):
        row = self.sample((1, 2, 5, 10), (1000, 950, 900, 2000), (.2, .1, .3, .01), [300, 300, 300, 10])
        result = self.run_export([row, row], **{'p95往返偏移': .002, '极端往返偏移': .003})
        self.assertEqual([r['名义倍数（倍）'] for r in result['研究候选']], [2, 1, 5])
        self.assertEqual(result['全局合格最高期末资金（USDC）'], 1000)
        self.assertEqual(result['资金保留门槛（USDC）'], 900)
        self.assertEqual(result['筛选阶段统计']['比较账户数'], 8)
        self.assertEqual(result['筛选阶段统计']['硬门槛通过账户数'], 6)
        self.assertEqual(result['筛选阶段统计']['去重合格账户数'], 3)
        self.assertTrue(all(r['研究预筛分'] is None for r in result['研究候选']))
        self.assertTrue(all(r['p95成本后单笔收益（%）'] < 0 for r in result['研究候选']))
        self.assertTrue(all('仅提示' in r['筛选提示'] for r in result['研究候选']))
        self.assertEqual(result['实盘候选'], [])
        self.assertEqual(len({r['策略指纹'] for r in result['研究候选']}), 3)
        self.assertTrue(all(len(r['策略指纹']) == 16 for r in result['研究候选']))

    def test_retention_category_and_watch_limits_do_not_pad(self):
        first = self.sample(finals=(1000, 950, 899), drawdowns=(.2, .1, .01))
        second = self.sample((1,), (930,), (.05,)); second['止盈方案编号'] = 2
        result = self.run_export([first, second], **{'每类最多': 1, '全局观察最多': 1})
        self.assertEqual([(r['止盈方案编号'], r['名义倍数（倍）']) for r in result['研究候选']], [(2, 1), (1, 2)])
        self.assertEqual(len(result['观察候选']), 1)
        self.assertEqual(result['筛选阶段统计']['资金门槛内账户数'], 3)

    def test_target_scope_reads_only_the_selected_actual_account(self):
        result = self.run_export([self.sample()], **{'比较范围': 'TARGET', '统一目标杠杆': 5})
        self.assertEqual([r['名义倍数（倍）'] for r in result['研究候选']], [5])
        self.assertEqual(result['筛选阶段统计']['比较账户数'], 1)

    def test_hard_gates_and_no_loss_exception_require_positive_sufficient_trades(self):
        settings = 规范化候选筛选(None)
        record = dict(forced_liquidations=0, capital_stop=0, liquidations=0, mdd=.1,
                      profit_factor=2, win_rate=.6, average_net=.001, trades=300,
                      final_money=200, initial_capital=100)
        self.assertEqual(C.return_drawdown_failures(record, settings), ([], False))
        for changes in (dict(forced_liquidations=1), dict(capital_stop=1), dict(liquidations=11),
                        dict(mdd=.41), dict(profit_factor=1), dict(trades=199),
                        dict(final_money=100), dict(average_net=0),
                        dict(profit_factor=0, win_rate=1, trades=0),
                        dict(profit_factor=0, win_rate=1, average_net=0)):
            with self.subTest(changes=changes):
                self.assertTrue(C.return_drawdown_failures(dict(record, **changes), settings)[0])

    def test_pf_then_leverage_then_fingerprint_break_ties_deterministically(self):
        row = self.sample((1, 2, 5), (1000, 1000, 1000), (.1, .1, .1))
        groups = [part.split('|') for part in row[ACCOUNT_FIELD].split(';')]
        groups[0][8] = '1.5'; groups[1][8] = '2'; groups[2][8] = '2'
        row[ACCOUNT_FIELD] = ';'.join('|'.join(part) for part in groups)
        result = self.run_export([row])
        self.assertEqual([r['名义倍数（倍）'] for r in result['研究候选']], [2, 5, 1])
        first = self.sample((1,), (1000,), (.1,)); second = dict(first, **{'基础策略编号': 2})
        result = self.run_export([second, first])
        self.assertEqual([r['策略指纹'] for r in result['研究候选']], sorted(r['策略指纹'] for r in result['研究候选']))

    def test_legacy_stress_all_run_keeps_each_leverage_and_legacy_gates(self):
        row = self.sample()
        groups = [part.split('|') for part in row[ACCOUNT_FIELD].split(';')]
        for group in groups: group[2] = '.5'; group[10] = '.1'; group[13] = '.3'
        row[ACCOUNT_FIELD] = ';'.join('|'.join(part) for part in groups)
        result = self.run_export([row], **{'筛选方案': 'LEGACY_STRESS', '比较范围': 'ALL_RUN'})
        self.assertEqual({r['名义倍数（倍）'] for r in result['研究候选']}, {1, 2, 5})
        self.assertTrue(all(len(r['策略指纹']) == 16 for r in result['研究候选']))
        groups[0][10] = '-.1'
        row[ACCOUNT_FIELD] = ';'.join('|'.join(part) for part in groups)
        result = self.run_export([row], **{'筛选方案': 'LEGACY_STRESS', '比较范围': 'ALL_RUN'})
        self.assertEqual({r['名义倍数（倍）'] for r in result['研究候选']}, {2, 5})
        row[ACCOUNT_FIELD] = ''
        result = self.run_export([row], **{'筛选方案': 'LEGACY_STRESS', '比较范围': 'ALL_RUN'})
        self.assertEqual(result['研究候选'], [])
        self.assertTrue(any('缺少逐账户' in item['淘汰原因'] for item in result['淘汰统计']))

    def test_no_loss_zero_pf_is_marked_not_forged_and_nan_is_not_exempt(self):
        row = self.sample((1,), (1000,), (.2,))
        parts = row[ACCOUNT_FIELD].split('|'); parts[1] = '1'; parts[8] = '0'; row[ACCOUNT_FIELD] = '|'.join(parts)
        result = self.run_export([row])
        self.assertEqual(result['研究候选'][0]['利润因子PF'], 0)
        self.assertIn('未定义', result['研究候选'][0]['PF状态'])
        parts[8] = 'nan'; row[ACCOUNT_FIELD] = '|'.join(parts)
        result = self.run_export([row])
        self.assertEqual(result['研究候选'], [])

    def test_nonfinite_critical_values_and_missing_multi_account_evidence_are_excluded(self):
        for field, value in [('所选仓位期末资金（USDC）', 'nan'), ('所选仓位最大回撤（%）', 'nan'),
                             ('所选仓位全仓强平次数（次）', ''), ('所选仓位资金性停机标记（0否1是）', '')]:
            with self.subTest(field=field):
                row = self.sample((1,), (1000,), (.2,)); row[field] = value
                result = self.run_export([row])
                self.assertEqual(result['研究候选'], [])
                self.assertTrue(result['淘汰统计'])
        row = self.sample(); row[ACCOUNT_FIELD] = ''
        result = self.run_export([row])
        self.assertEqual(result['研究候选'], [])
        self.assertEqual(result['筛选阶段统计']['比较账户数'], 3)

    def test_mixed_funds_or_cost_context_is_rejected_before_export(self):
        for changes in ({'初始资金（USDC）': 200}, {'成本模式': 'FEE'}, {'回测计算版本': 'other'}):
            with self.subTest(changes=changes):
                first = self.sample((1,), (1000,), (.2,)); second = dict(first, **changes)
                with self.assertRaisesRegex(ValueError, '混合'):
                    self.run_export([first, second])

    def test_native_catalog_matches_existing_worst_export_fingerprint_fields(self):
        row = self.sample(); result = self.run_export([row])
        native_payload = json.loads((self.root / '候选原始记录.json').read_text('utf-8'))
        native = [dict(zip(native_payload['表头'], values)) for values in native_payload['分类']['类别1']]
        tp = SimpleNamespace(编号=1, 类别='类别1', 周期组合='1m', 指标='测试', 参数一=.001, 参数二=.01, 参数三=0)
        with mock.patch.object(sys, 'argv', ['worst_export', '--output', str(self.root), '--json-only']), mock.patch.object(W, '生成止盈方案', return_value=[tp]):
            W.main()
        old_payload = json.loads((self.root / '各类止盈前5000名.json').read_text('utf-8'))
        old = [dict(zip(old_payload['表头'], values)) for values in old_payload['分类']['类别1']]
        self.assertEqual({config_fingerprint(r) for r in old}, {r['策略指纹'] for r in result['研究候选']})
        old_by_size = {r['名义倍数（倍）']: r for r in old}
        # Compare every native field, not only hash length; Python number equality
        # is supplemented by the fingerprints' JSON-sensitive type comparison.
        for record in native:
            self.assertEqual(record, old_by_size[record['名义倍数（倍）']])

    def test_preexisting_stop_flag_cancels_without_candidate_output(self):
        self.write_source([self.sample()])
        control = self.root / '控制'; control.mkdir()
        (control / '停止候选导出.flag').touch()
        with mock.patch.object(C, 'export_excel') as export:
            C.run_return_drawdown(self.root, self.root, self.root / '全部回测结果.csv', 规范化候选筛选(None), C.read_tp_dictionary(self.root / '止盈方案字典.csv'))
        export.assert_not_called()
        self.assertFalse((self.root / '候选原始记录.json').exists())

    def test_native_present_blank_text_fields_match_legacy_rank_not_missing_defaults(self):
        row = self.sample((1,), (1000,), (.2,))
        for name in ('固定止损代码', '叠加止盈代码', '开仓方向', '交易会话', '入场触发口径', '成交价格口径', '回测计算版本'):
            row[name] = ''
        source = self.write_source([row])
        tp = SimpleNamespace(编号=1, 类别='类别1', 周期组合='1m', 指标='测试', 参数一=.001, 参数二=.01, 参数三=0)
        with mock.patch.object(sys, 'argv', ['worst_export', '--output', str(self.root), '--json-only']), mock.patch.object(W, '生成止盈方案', return_value=[tp]):
            W.main()
        payload = json.loads((self.root / '各类止盈前5000名.json').read_text('utf-8'))
        expected = dict(zip(payload['表头'], payload['分类']['类别1'][0]))
        with source.open(encoding='utf-8-sig', newline='') as handle: raw = next(csv.DictReader(handle))
        native = C.native_rank_row(raw, 0, C.read_tp_dictionary(self.root / '止盈方案字典.csv')[1])
        self.assertEqual(native, expected)
        self.assertEqual(config_fingerprint(native), config_fingerprint(expected))


if __name__ == '__main__':
    unittest.main()
