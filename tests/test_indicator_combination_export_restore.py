"""Composite results preserve members through reading views and exact replay."""
import copy
import json
import sys
import unittest
from unittest import mock

import candidate_export as C
import fingerprint_lookup as F
from extended_rules import stable_base_id
from indicator_combinations import CombinationRegistry, choices_for_config, combination_label, tp_spec
from ranking_view import build_view, config_fingerprint
from selection_config import 规范化配置, 配置签名, 配置统计
import test_v149_fingerprint_lookup as fixture


class CombinationExportRestoreTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixture.FingerprintLookupTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def result(self):
        from extended_rules import composite_stop_index
        directory, _, selection = self.helper.make_result()
        old = F._read_json(directory / F.RANKING_FILES[0])
        template = dict(zip(old['表头'], next(iter(old['分类'].values()))[0]))
        entry_spec = {'启用': True, '组合数量': [2], '保留单项': True, '逻辑': 'AND'}
        exit_spec = dict(entry_spec, 逻辑='OR', 保留单项=False)
        selection.update({'开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [5, 152],
                                      '1m': [5, 152, 153]},
                          '止损代码': ['S1', 'S3_1m'], '止盈方案编号': [2, 8427],
                          '指标组合': {'开仓': {'5m': entry_spec, '1m': dict(entry_spec, 组合数量=[2, 3])},
                                       '止损': exit_spec, '止盈': exit_spec}})
        selection = 规范化配置(selection)
        registry = CombinationRegistry()
        options = choices_for_config(selection, registry)
        five = next(code for code in options['开仓']['5m'] if code < 0)
        entries = [code for code in options['开仓']['1m'] if code < 0]
        stop, tp_id = next(iter(options['止损'])), next(iter(options['止盈']))
        plan = tp_spec(tp_id, registry)
        self.rows = []
        for code in entries[:2]:
            row = dict(template, **{
                '5分钟条件': combination_label('entry', five, registry),
                '1分钟条件': combination_label('entry', code, registry),
                '止损代码': stop, '止损说明': combination_label('stop', stop, registry),
                '止盈方案编号': tp_id, '止盈类别': plan.类别, '止盈周期组合': plan.周期组合,
                '止盈指标': plan.指标, '止盈参数一': plan.参数一, '止盈参数二': plan.参数二,
                '止盈参数三': plan.参数三,
                '基础策略编号': stable_base_id(1, (0, 0, 0, five, code), composite_stop_index(stop)),
                '期末资金（USDC）': 10001.0, '最大回撤（%）': .12, '胜率（%）': .8,
                '交易次数（单）': 150, '平均日完整交易数（次/日）': 10.0})
            self.rows.append(row)
        self.payload = {'表头': list(self.rows[0]),
                        '分类': {'指标组合止盈': [list(row.values()) for row in self.rows]}}
        identity = F._read_json(directory / '回测运行身份.json')
        identity['selection_signature'] = 配置签名(selection)
        data = F._read_json(directory / '回测数据说明.json')
        data['capabilities_by_timeframe'] = {'5m': {'ohlcv': True}}
        self.helper.write_json(directory / '回测数据说明.json', data)
        for filename in F.RANKING_FILES:
            self.helper.write_json(directory / filename, self.payload)
        self.helper.write_json(directory / '组合选择.json', selection)
        self.helper.write_json(directory / '回测运行身份.json', identity)
        self.helper.write_json(directory / '断点记录.json', {'selection': selection, 'run_identity': identity,
                                                         'selection_signature': 配置签名(selection)})
        registry.save(directory)
        self.options, self.registry, self.selection = options, registry, selection
        return directory

    def test_view_and_candidate_preserve_exact_combo_and_precision(self):
        directory = self.result()
        view = build_view(self.payload, directory)
        exported = dict(zip(view['表头'], view['分类']['指标组合止盈'][0]))
        envelope = json.loads(exported['完整单策略配置JSON'])
        selection = envelope['selection']
        self.assertEqual(配置统计(selection)['包含仓位完整组合数'], 1)
        self.assertEqual(selection['开仓条件']['1m'], [5, 152])
        self.assertEqual(selection['开仓条件']['5m'], [5, 152])
        self.assertEqual(selection['止损代码'], ['S1', 'S3_1m'])
        self.assertEqual(selection['止盈方案编号'], [2, 8427])
        self.assertFalse(selection['指标组合']['开仓']['1m']['保留单项'])
        self.assertIn('且', exported['开仓判据（当前代码解释）'])
        self.assertIn('或', exported['止盈判据（当前代码解释）'])
        self.assertEqual(exported['止盈方案编号'], str(self.rows[0]['止盈方案编号']))
        members = json.loads(exported['指标组合成员JSON'])
        self.assertEqual(members['止盈']['members'], [2, 8427])
        self.assertEqual(len(view['止盈方案字典']), 1)
        self.assertEqual(view['止盈方案字典'][0][0], str(self.rows[0]['止盈方案编号']))
        candidates = [{'策略指纹': config_fingerprint(self.rows[0]),
                       '止盈方案编号': self.rows[0]['止盈方案编号']}]
        C.append_candidate_details(candidates, self.rows, directory)
        self.assertEqual(candidates[0]['止盈方案编号'], str(self.rows[0]['止盈方案编号']))
        self.assertEqual(json.loads(candidates[0]['完整单策略配置JSON'])['selection'], selection)
        C.write_native_catalog(directory, directory / '全部回测结果.csv', self.rows)
        for filename in F.RANKING_FILES:
            (directory / filename).unlink()
        match = F.find_fingerprint_matches(config_fingerprint(self.rows[0]), directory)[0]
        restored = F.restore_fingerprint_match(match)
        self.assertEqual(restored['selection'], selection)

    def test_restore_never_recombines_distinct_groups_in_editor_union(self):
        directory = self.result()
        matches = [F.find_fingerprint_matches(config_fingerprint(row), directory)[0] for row in self.rows]
        restored = F.restore_many(matches)
        self.assertTrue(all(配置统计(item['selection'])['包含仓位完整组合数'] == 1 for item in restored))
        self.assertNotEqual(restored[0]['selection']['开仓条件']['1m'],
                            restored[1]['selection']['开仓条件']['1m'])
        with self.assertRaisesRegex(ValueError, '不同指标组合不能合并'):
            F.merge_fingerprint_restorations(restored)

    def test_csv_rebuild_keeps_compound_tp_and_native_integer_identifiers(self):
        from test_v147_export_execution import sample_row, write_source
        import worst_export
        self.result()
        output = self.helper.root / 'csv_rebuild'
        output.mkdir()
        native = self.rows[0]
        row = sample_row()
        row.update({key: native[key] for key in ('止盈方案编号', '基础策略编号', '止损代码')})
        for timeframe, header in (('5m', '5分钟条件'), ('1m', '1分钟条件')):
            row[header + '代码'] = next(code for code in self.options['开仓'][timeframe]
                if combination_label('entry', code, self.registry) == native[header])
        write_source(output, [row])
        self.registry.save(output)
        with mock.patch.object(sys, 'argv', ['worst_export', '--output', str(output), '--json-only']), \
             mock.patch.object(worst_export, 'emit'):
            worst_export.main()
        for filename in F.RANKING_FILES:
            payload = F._read_json(output / filename)
            exported = dict(zip(payload['表头'], payload['分类']['指标组合止盈'][0]))
            for key in ('止盈方案编号', '基础策略编号', '止损代码', '5分钟条件', '1分钟条件'):
                self.assertEqual(exported[key], native[key])
            self.assertIn('或', exported['止损说明'])

    def test_missing_or_changed_run_dictionary_cannot_use_another_runs_cache(self):
        directory = self.result()
        match = F.find_fingerprint_matches(config_fingerprint(self.rows[0]), directory)[0]
        F.restore_fingerprint_match(match)  # Deliberately leave the shared cache populated.
        path = directory / '指标组合字典.json'
        payload = F._read_json(path)
        path.unlink()
        with self.assertRaisesRegex(ValueError, '缺少原指标组合字典'):
            F.restore_fingerprint_match(match)
        missing_tp = copy.deepcopy(payload)
        missing_tp['组合'] = [row for row in missing_tp['组合'] if row['kind'] != 'tp']
        self.helper.write_json(path, missing_tp)
        with self.assertRaisesRegex(ValueError, '不属于原运行配置'):
            F.restore_fingerprint_match(match)
        changed = copy.deepcopy(payload)
        changed['组合'][0]['members'] = [5, 153, 156]
        self.helper.write_json(path, changed)
        with self.assertRaises(ValueError):
            F.restore_fingerprint_match(match)


if __name__ == '__main__':
    unittest.main()
