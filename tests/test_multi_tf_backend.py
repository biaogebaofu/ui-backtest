"""Native-timeframe research signals retain closed-bar and legacy identity semantics."""
import hashlib
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from data_sources import (capabilities_from_frame, resample_closed_frame,
                          timeframe_capabilities_from_frame)
from extended_rules import (ENTRY_RULES, ENTRY_TIMEFRAME_MINUTES, entry_supported_timeframes,
                            entry_unavailable_reason, capabilities_for_timeframe, stable_base_id)
from extended_signals import entry_masks
from feature_builder import build_features
from selection_config import 全选配置, 规范化配置, 时间条件

PROJECT = Path(__file__).resolve().parents[1]


def fixture(n=3400, minutes=1):
    rng = np.random.default_rng(158)
    c = 2000 * np.exp(np.cumsum(rng.normal(0, .001, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(c, o) + rng.uniform(.1, 3, n)
    l = np.minimum(c, o) - rng.uniform(.1, 3, n)
    v = rng.lognormal(6, 1, n)
    values = np.sin(np.arange(n) / 5) + rng.normal(0, .2, n)
    d = np.sign(values - np.r_[values[0], values[:-1]])
    ma = pd.Series(c).rolling(20).mean().to_numpy()
    times = 1735689600000 + np.arange(1, n + 1) * minutes * 60000
    ex = {'delta_base': v * rng.uniform(-1, 1, n), 'trades': rng.integers(1, 10000, n).astype(float),
          'taker_buy_ratio': rng.uniform(.1, .9, n), 'avg_trade_size': rng.lognormal(0, 1, n),
          'quote_volume': v * c, 'taker_buy_quote_ratio': rng.uniform(.1, .9, n),
          'delta_quote': v * c * rng.uniform(-1, 1, n), 'funding_rate': rng.normal(0, .0003, n),
          'open_interest': 10000 * np.exp(np.cumsum(rng.normal(0, .001, n))),
          'open_interest_value': 10000 * c, 'hist': values, 'dif': values * .8,
          'dea': values * .3, 'ma120': pd.Series(c).rolling(120).mean().to_numpy()}
    return d, values, o, h, l, c, ma, v, times, ex


def legacy_digest():
    args = fixture()
    digest = hashlib.sha256()
    for code in sorted(code for code in ENTRY_RULES if code < 264):
        for mask in entry_masks(code, *args):
            digest.update(mask.tobytes())
    return digest.hexdigest()


def bars(n=12000):
    x = np.arange(n)
    c = 2000 + 20 * np.sin(x / 70) + 4 * np.sin(x / 9)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({'openTime': 1735689600000 + x * 60000, 'open': o,
                         'high': np.maximum(c, o) + .4, 'low': np.minimum(c, o) - .4,
                         'close': c, 'volume': 1. + x % 5})


class MultiTimeframeBackendTests(unittest.TestCase):
    def test_legacy_one_minute_catalog_is_exact(self):
        # Generated with the pre-expansion baseline, all 259 existing rule masks.
        self.assertEqual(legacy_digest(), '82fbc5d126c1ee4721d180b48a8fa58be4790b32f08048942a77ab3ee7e6a5a6')

    def test_supported_timeframes_and_explicit_minute_limits(self):
        self.assertEqual({tf: sum(code < 264 for code in codes) for tf, codes in 时间条件.items()},
                         {'4h': 248, '1h': 252, '15m': 261, '5m': 262, '1m': 264})
        for code in (18, 42, 144, 232, 263):
            self.assertEqual(entry_supported_timeframes(code), tuple(ENTRY_TIMEFRAME_MINUTES))
        for code in (132, 134, 135, 136, 137, 138, 139, 240, 241):
            self.assertEqual(entry_supported_timeframes(code), ('1m', '5m', '15m'))
        self.assertEqual(entry_supported_timeframes(133), ('1m', '5m', '15m', '1h'))
        self.assertEqual(entry_supported_timeframes(242), ('1m', '5m', '15m', '1h'))
        config = 全选配置()
        config['开仓条件'] = {'4h': [144], '1h': [133], '15m': [132], '5m': [240], '1m': [42]}
        self.assertEqual(规范化配置(config), config)
        config['开仓条件']['4h'] = [240]
        with self.assertRaises(ValueError):
            规范化配置(config)

    def test_id_capacity_all_five_dimensions_and_old_ids(self):
        cases = {tuple([c] * 5) for c in range(264)}
        cases.update(tuple(c if i == pos else 0 for i in range(5))
                     for c in range(264) for pos in range(5))
        ids = [stable_base_id(f, case, s) for f in (0, 1) for case in cases for s in (0, 911, 6665, 8191)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLess(max(ids), np.iinfo(np.int64).max)
        self.assertEqual(stable_base_id(0, (0, 0, 0, 0, 143), 0), 80000000001171457)
        self.assertEqual(stable_base_id(0, (0, 0, 0, 0, 0), 0), 1)

    def test_oi_minutes_do_not_become_native_bar_counts_or_bridge_missing_boundary(self):
        for tf, minutes, code, window in [('5m', 5, 132, 15), ('15m', 15, 132, 15),
                                           ('1h', 60, 133, 60)]:
            args = list(fixture(40, minutes))
            args[0] = np.ones(40)
            oi = np.full(40, 100.)
            oi[10:] = 102.
            args[-1]['open_interest'] = oi
            actual = entry_masks(code, *args, timeframe=tf)[0]
            expected = np.zeros(40, bool)
            expected[10:10 + window // minutes] = True
            np.testing.assert_array_equal(actual, expected)
        args = list(fixture(6, 5))
        # A missing 00:15 close must not substitute the older 00:10 snapshot.
        args[0] = np.ones(6)
        args[-2] = 1735689600000 + np.array([5, 10, 20, 25, 30, 35]) * 60000
        args[-1]['open_interest'] = np.array([100., 100., 100., 100., 102., 102.])
        actual = entry_masks(132, *args, timeframe='5m')[0]
        self.assertFalse(actual[4])
        self.assertTrue(actual[5])

    def test_opening_range_and_previous_day_use_native_utc_bars(self):
        args = list(fixture(600, 5))
        c = np.full(600, 100.)
        c[288 + 3] = 102.
        args[0] = np.ones(600)
        args[2:7] = [c.copy(), np.full(600, 101.), np.full(600, 99.), c, np.full(600, 100.)]
        actual = entry_masks(240, *args, timeframe='5m')[0]
        self.assertTrue(actual[291])  # Day two's first 15 minutes end at native bar 290.
        self.assertFalse(actual[290])
        cut = [a[:292] if isinstance(a, np.ndarray) else {k: v[:292] for k, v in a.items()} for a in args]
        np.testing.assert_array_equal(actual[:292], entry_masks(240, *cut, timeframe='5m')[0])

        args = list(fixture(24, 240))
        c = np.full(24, 100.); c[18] = 99.; c[19] = 105.
        args[0] = np.ones(24)
        args[2:7] = [c.copy(), np.maximum(c, 101.), np.minimum(c, 99.), c, np.full(24, 100.)]
        self.assertTrue(entry_masks(232, *args, timeframe='4h')[0][19])
        keep = np.arange(24) != 12
        gap = [a[keep] if isinstance(a, np.ndarray) else {k: v[keep] for k, v in a.items()} for a in args]
        self.assertFalse(entry_masks(232, *gap, timeframe='4h')[0][18])

    def test_aggregation_capabilities_and_legacy_metadata_are_conservative(self):
        frame = bars(960)
        frame['taker_buy_ratio'] = .7
        frame['avg_trade_size'] = 10.
        old = capabilities_from_frame(frame)
        caps = timeframe_capabilities_from_frame(frame)
        self.assertTrue(caps['1m']['taker_ratio'])
        self.assertTrue(caps['1m']['avg_trade_size'])
        for tf in ('5m', '15m', '1h', '4h'):
            self.assertFalse(caps[tf]['taker_ratio'])
            self.assertFalse(caps[tf]['avg_trade_size'])
            self.assertTrue(entry_unavailable_reason(53, caps[tf], tf))
            self.assertEqual(capabilities_for_timeframe(old, {}, tf), {})
        frame['trades'] = 2.
        frame['taker_buy_base'] = frame.volume * np.where(np.arange(960) % 5 == 0, .9, .1)
        frame['delta_base'] = 2 * frame.taker_buy_base - frame.volume
        frame['funding_rate'] = .001
        frame.loc[4::5, 'funding_rate'] = np.nan
        native = resample_closed_frame(frame.set_index(pd.to_datetime(frame.openTime, unit='ms', utc=True)), 5)
        self.assertAlmostEqual(native.iloc[0].taker_buy_base / native.iloc[0].volume, (0.9 + 1.4) / 15)
        self.assertTrue(native.funding_rate.isna().all())
        caps = timeframe_capabilities_from_frame(frame)
        self.assertTrue(caps['5m']['taker_ratio'])
        self.assertTrue(caps['5m']['avg_trade_size'])
        self.assertFalse(caps['5m']['funding'])
        # Every high-period bucket has an absent input, despite many valid 1m rows.
        frame.loc[::5, 'trades'] = np.nan
        caps = timeframe_capabilities_from_frame(frame)
        self.assertTrue(caps['1m']['trades'])
        self.assertFalse(caps['5m']['trades'])
        self.assertTrue(entry_unavailable_reason(252, caps['5m'], '5m'))

    def test_closed_native_signals_map_to_one_minute_without_lookahead(self):
        with tempfile.TemporaryDirectory(prefix='multi_tf_backend_') as temporary:
            root = Path(temporary)
            csv = root / 'bars.csv'; bars().to_csv(csv, index=False)
            path = build_features(str(csv), str(root / 'cache'))
            # Existing version-7 arrays are reused while missing per-TF metadata is enriched.
            meta_path = path.with_name('features_meta.json')
            meta = json.loads(meta_path.read_text('utf-8'))
            self.assertEqual(meta['request']['feature_version'], 7)
            del meta['capabilities_by_timeframe']
            meta_path.write_text(json.dumps(meta), 'utf-8')
            before = path.read_bytes()
            with mock.patch('feature_builder.load_source', side_effect=AssertionError('cache rebuilt')):
                self.assertEqual(build_features(str(csv), str(root / 'cache')), path)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(json.loads(meta_path.read_text('utf-8'))['capabilities_by_timeframe']['4h']['ohlcv'])
            script = '''import numpy as np
import backtest_engine as B
from extended_signals import entry_masks
d = B.E.D
for tf in ('5m','15m','1h','4h'):
    for code in (18,144):
        native = entry_masks(code, B.E.direction(d[tf+'_hist']), d[tf+'_hist'],
            d[tf+'_open'], d[tf+'_high'], d[tf+'_low'], d[tf+'_close'], d[tf+'_ma20'],
            d[tf+'_volume'], d[tf+'_ct'], timeframe=tf)
        mapped = B.extended_entry_data(tf,'hist',code)
        mp = d[tf+'_map']; valid=(mp>=35)&(np.arange(B.N)>=200)
        for src,actual in zip(native,mapped):
            expected=np.zeros(B.N,bool); expected[valid]=src[mp[valid]]
            np.testing.assert_array_equal(actual,expected)
        assert np.any(mapped[0]) or np.any(mapped[1]), (tf,code)
        assert np.all(d[tf+'_ct'][mp[valid]]<=d['ct1'][valid])
        # Between native closes the latest permission state stays unchanged.
        repeats=np.flatnonzero((mp[1:]==mp[:-1]) & valid[1:] & valid[:-1])+1
        for mask in mapped: assert np.all(mask[repeats]==mask[repeats-1])
        end=39
        shortened=entry_masks(code,B.E.direction(d[tf+'_hist'][:end]),d[tf+'_hist'][:end],
            d[tf+'_open'][:end],d[tf+'_high'][:end],d[tf+'_low'][:end],d[tf+'_close'][:end],
            d[tf+'_ma20'][:end],d[tf+'_volume'][:end],d[tf+'_ct'][:end],timeframe=tf)
        for full,short in zip(native,shortened): np.testing.assert_array_equal(full[:end],short)
cases=((0,0,0,18,1),(0,0,0,144,1))
entries=B.build_field('hist',cases,('S1',),materialize_stops=False)[3]
assert len(entries)==2 and all(len(entry[1])>0 for entry in entries)
'''
            env = os.environ.copy(); env['BT_FEATURES'] = str(path)
            env['OPENBLAS_NUM_THREADS'] = '1'
            run = subprocess.run([sys.executable, '-c', script], cwd=PROJECT, env=env,
                                 capture_output=True, text=True, timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_worker_executes_high_tf_entries_and_removes_missing_microstructure(self):
        with tempfile.TemporaryDirectory(prefix='multi_tf_worker_') as temporary:
            root = Path(temporary)
            source = root / 'bars.csv'; bars(2400).to_csv(source, index=False)
            config = 全选配置()
            config.update({'开仓指标': ['hist'],
                '开仓条件': {'4h': [0], '1h': [0], '15m': [0], '5m': [18, 144], '1m': [1]},
                '止损代码': ['S1'], '固定止损代码': ['OFF'], '叠加止盈代码': ['OFF'],
                '止盈方案编号': [2], '止盈后等待分钟': [0], '仓位倍数': [1.],
                '成本模式': 'FEE', '开仓位置过滤': ['OFF']})
            config['入场约束']['最小S3距离'] = 0.
            config['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=False)
            selection = root / 'selection.json'
            script = 'import backtest_worker as W; W.export_excel=lambda *a: None; W.main()'
            env = os.environ.copy(); env.pop('BT_FEATURES', None)
            env['OPENBLAS_NUM_THREADS'] = '1'

            def run(output):
                selection.write_text(json.dumps(规范化配置(config), ensure_ascii=False), 'utf-8')
                return subprocess.run([sys.executable, '-X', 'utf8', '-c', script,
                    '--csv', str(source), '--output', str(output), '--selection', str(selection),
                    '--threads', '1', '--device', 'cpu', '--cache-root', str(root / 'cache')],
                    cwd=PROJECT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)

            output = root / 'result'
            result = run(output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with (output / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(int(row['交易次数（单）']) > 0 for row in rows))
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            capabilities = next(e for e in events if e['type'] == 'data_capabilities')
            self.assertTrue(capabilities['capabilities_by_timeframe']['5m']['ohlcv'])
            config['开仓条件']['5m'] = [252]
            missing = root / 'missing'
            pruned = run(missing)
            self.assertEqual(pruned.returncode, 0, pruned.stdout + pruned.stderr)
            self.assertIn('5m 252', pruned.stdout + pruned.stderr)
            self.assertIn('成交笔数', pruned.stdout + pruned.stderr)
            audit = json.loads((missing / '数据能力自动剔除记录.json').read_text('utf-8'))
            self.assertEqual([(r['周期'], r['代码']) for r in audit['removed']], [('5m', 252)])
            self.assertEqual(audit['selection']['开仓条件']['5m'], [0])
            self.assertEqual(audit['selection']['开仓条件']['1m'], [1])
            with (missing / '全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
                remaining = list(csv.DictReader(handle))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(int(remaining[0]['5分钟条件代码']), 0)
            self.assertEqual(int(remaining[0]['1分钟条件代码']), 1)


if __name__ == '__main__':
    unittest.main()
