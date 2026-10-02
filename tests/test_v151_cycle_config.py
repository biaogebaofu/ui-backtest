"""Scalar cycle mode, unchanged legacy identities, and complete lookup restoration."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import backtest_worker as worker
import fingerprint_lookup as lookup
from ranking_view import config_fingerprint
from selection_config import (全选配置, 入场触发口径选项, 入场触发显示代码,
                              规范化配置, 配置签名, 配置统计, 默认入场触发口径)
import test_v149_fingerprint_lookup as lookup_fixture


def small_config(mode='LIVE_01'):
    raw = 全选配置()
    raw.update({'开仓指标': ['hist'], '开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [0], '1m': [1]},
                '止损代码': ['S1'], '固定止损代码': ['OFF'], '叠加止盈代码': ['OFF'],
                '止盈方案编号': [1], '止盈后等待分钟': [0], '仓位倍数': [1.], '入场触发口径': mode})
    return raw


class CycleConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v151_cycle_config_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = lookup_fixture.FingerprintLookupTests()
        self.fixture.root = self.root

    def save_mode_result(self, mode, name='result', candidate=False, cost='FEE', old_engine=None):
        directory, _, selection = self.fixture.make_result(name, mode=cost)
        selection['入场触发口径'] = mode
        trade = copy.deepcopy(selection); trade.pop('候选筛选')
        signature = hashlib.sha256(json.dumps(trade, ensure_ascii=False, sort_keys=True,
                                              separators=(',', ':')).encode('utf-8')).hexdigest()
        identity = json.loads((directory / '回测运行身份.json').read_text('utf-8'))
        identity['selection_signature'] = signature
        if old_engine is not None: identity['engine_version'] = old_engine
        payload = json.loads((directory / lookup.RANKING_FILES[0]).read_text('utf-8'))
        for rows in payload['分类'].values():
            for row in rows:
                row[payload['表头'].index('入场触发口径')] = mode
                row[payload['表头'].index('回测计算版本')] = identity['engine_version'] + ':' + selection['成交价格口径']
        record = dict(zip(payload['表头'], next(iter(payload['分类'].values()))[0]))
        for filename in lookup.RANKING_FILES:
            self.fixture.write_json(directory / filename, dict(payload, 分类={}) if candidate else payload)
        if candidate:
            self.fixture.write_json(directory / lookup.CANDIDATE_FILES[0], dict(payload, 原始结果目录=str(directory)))
        self.fixture.write_json(directory / '组合选择.json', selection)
        self.fixture.write_json(directory / '断点记录.json', {'selection': selection, 'run_identity': identity,
                                                              'selection_signature': signature})
        self.fixture.write_json(directory / '回测运行身份.json', identity)
        return directory, config_fingerprint(record), selection

    def test_cycle_is_scalar_with_schema_twenty_and_default_unchanged(self):
        self.assertEqual(默认入场触发口径, 'LIVE_01')
        self.assertEqual(全选配置()['入场触发口径'], 'LIVE_01')
        self.assertEqual(入场触发口径选项['MACD_CYCLE'], 'DIF/DEA金叉死叉：每次红绿换色后最多开一次（1m）')
        raw = small_config('MACD_CYCLE')
        raw['开仓指标'] = ['dif']
        raw['开仓位置过滤'] = ['OFF', 'RANGE60_EDGE20']
        normalized = 规范化配置(raw)
        self.assertEqual(normalized['版本'], 20)
        self.assertEqual(normalized['入场触发口径'], 'MACD_CYCLE')
        self.assertEqual(normalized['开仓指标'], ['dif'])
        self.assertEqual(normalized['开仓位置过滤'], raw['开仓位置过滤'])
        baseline = dict(raw, 入场触发口径='LIVE_01')
        self.assertEqual(配置统计(raw), 配置统计(baseline))
        self.assertEqual(配置统计(raw)['包含仓位完整组合数'], 2)

    def test_public_example_configuration_signatures_are_stable(self):
        golden = {'LIVE_01': '3c77d471083414322d2ebc0f910bb791149b5db57f485ae31599814253421a0f',
                  'TF_EVENT': '736625fdd50f0c135371d89a362a3443c93f6c7ffb3fa4a60b378a6e0358a825'}
        for mode, expected in golden.items():
            with self.subTest(mode=mode):
                config = small_config(mode)
                self.assertEqual(规范化配置(config), config)
                self.assertEqual(配置签名(config), expected)
                self.assertNotEqual(配置签名(small_config('MACD_CYCLE')), expected)
        missing = small_config(); missing.pop('入场触发口径')
        self.assertEqual(规范化配置(missing)['入场触发口径'], 'LIVE_01')

    def test_fingerprint_old_golden_unchanged_and_each_mode_distinct(self):
        row = {'基础策略编号': 1, '止盈方案编号': 1, '止盈后等待分钟': 0, '名义倍数（倍）': 1.,
               '入场触发口径': 'LIVE_01', '开仓位置过滤代码': 'OFF',
               '回测计算版本': 'v1.50-fixture:CLOSE_CONFIRMED', '成交价格口径': 'CLOSE_CONFIRMED'}
        self.assertEqual(config_fingerprint(row), '62860a7862b16a20')
        fingerprints = {config_fingerprint(dict(row, 入场触发口径=mode)) for mode in 入场触发口径选项}
        self.assertEqual(len(fingerprints), len(入场触发口径选项))
        self.assertTrue(all(len(value) == 16 for value in fingerprints))

    def test_known_ui_labels_and_single_mode_lists_normalize_but_unknown_modes_are_rejected(self):
        for label, code in 入场触发显示代码.items():
            with self.subTest(label=label):
                config = small_config(label)
                if code == 'F5_EVENT':
                    config['开仓条件']['1m'] = [269]
                self.assertEqual(规范化配置(config)['入场触发口径'], code)
                self.assertEqual(lookup._complete_selection(config)['入场触发口径'], code)
        self.assertEqual(规范化配置(small_config(['MACD_CYCLE']))['入场触发口径'], 'MACD_CYCLE')
        for unknown in ('UNKNOWN', '', 'macd_cycle', ' LIVE_01', None, 0, True, [], ['UNKNOWN']):
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                规范化配置(small_config(unknown))

    def test_worker_cli_selection_accepts_cycle_before_any_market_computation(self):
        selection_path = self.root / 'selection.json'
        selection_path.write_text(json.dumps(small_config('MACD_CYCLE')), 'utf-8')
        argv = ['backtest_worker', '--csv', str(self.root / 'unused.csv'), '--output', str(self.root / 'output'),
                '--threads', '1', '--selection', str(selection_path)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(worker.tempfile, 'gettempdir', return_value=str(self.root)), \
             mock.patch.dict(os.environ, {}), mock.patch.object(worker, '规范化配置', wraps=规范化配置) as normalize, \
             mock.patch.object(worker, 'build_features', side_effect=RuntimeError('test boundary before market computation')) as build:
            with self.assertRaisesRegex(RuntimeError, 'test boundary'): worker._main()
        self.assertEqual(normalize.call_args.args[0]['入场触发口径'], 'MACD_CYCLE')
        build.assert_called_once()

    def test_cycle_restores_old_rank_and_candidate_with_dates_costs_and_hidden_settings(self):
        for candidate, cost in ((False, 'FEE'), (True, 'SLIPPAGE')):
            with self.subTest(candidate=candidate, cost=cost):
                directory, fp, original = self.save_mode_result('MACD_CYCLE', str(candidate), candidate, cost)
                matches = lookup.find_fingerprint_matches(fp, directory)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]['ranking_files'], list(lookup.CANDIDATE_FILES if candidate else lookup.RANKING_FILES))
                before = {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
                restored = lookup.restore_fingerprint_match(matches[0])
                config = restored['selection']
                self.assertEqual(config['版本'], 20)
                self.assertEqual(config['入场触发口径'], 'MACD_CYCLE')
                self.assertEqual(配置统计(config)['包含仓位完整组合数'], 1)
                for field in ('资金约束', '入场约束', '手续费', '成交偏移', '候选筛选', '成本模式', '成交价格口径', '平仓后最小开仓间隔分钟'):
                    self.assertEqual(config[field], original[field])
                self.assertEqual(config['开仓位置过滤'], ['RANGE60_EDGE20'])
                self.assertEqual(config['止盈后等待分钟'], [5])
                self.assertEqual(restored['start'], '1970-01-01T00:00:00Z')
                self.assertEqual(restored['end'], '1970-01-01T00:01:00Z')
                self.assertEqual(set(restored['sources']), set(lookup.SOURCE_KEYS))
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_old_modes_different_engine_warn_without_guessing_new_mode(self):
        for mode in ('LIVE_01', 'TF_EVENT'):
            with self.subTest(mode=mode):
                directory, fp, _ = self.save_mode_result(mode, mode, old_engine='v1.50-historical-fixture')
                match = lookup.find_fingerprint_matches(fp, directory)[0]
                restored = lookup.restore_fingerprint_match(match)
                self.assertEqual(restored['selection']['入场触发口径'], mode)
                self.assertTrue(any('原计算版本' in warning for warning in restored['warnings']))
                self.assertTrue(any('核心计算代码指纹' in warning for warning in restored['warnings']))

    def test_signed_unknown_mode_cannot_be_silently_restored_as_live(self):
        directory, fp, _ = self.save_mode_result('UNKNOWN')
        match = lookup.find_fingerprint_matches(fp, directory)[0]
        with self.assertRaisesRegex(ValueError, '未知入场触发口径'):
            lookup.restore_fingerprint_match(match)


if __name__ == '__main__':
    unittest.main()
