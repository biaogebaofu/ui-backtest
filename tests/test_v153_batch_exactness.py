"""Independent fingerprint rows must never become a Cartesian parameter sweep."""
import copy
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fingerprint_batch as B
import fingerprint_lookup as F
from account_statistics import ENGINE_VERSION
from ranking_view import config_fingerprint
from selection_config import 配置签名, 配置统计
import test_v149_fingerprint_lookup as fixture


class FingerprintBatchExactnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v153_batch_exactness_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = fixture.FingerprintLookupTests()
        self.helper.root = self.root

    def make_original(self, name, leverage, tp, entry_mode, cost_mode, fill_mode, meta=None):
        directory, _, selected = self.helper.make_result(name, mode=cost_mode, s3=0)
        # Each source can have many original options, but each fingerprint fixes one.
        selected['仓位倍数'] = [9., 10.]
        selected['入场触发口径'] = entry_mode
        selected['成交价格口径'] = fill_mode
        selected['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=False)
        identity = json.loads((directory / '回测运行身份.json').read_text('utf-8'))
        identity['selection_signature'] = 配置签名(selected)
        if meta is not None:
            identity['feature_request'] = meta['request']
            self.helper.write_json(directory / '回测数据说明.json', meta)
        self.helper.write_json(directory / '组合选择.json', selected)
        self.helper.write_json(directory / '回测运行身份.json', identity)
        self.helper.write_json(directory / '断点记录.json', {
            'selection': selected, 'run_identity': identity,
            'selection_signature': identity['selection_signature'],
        })
        payload = json.loads((directory / F.RANKING_FILES[0]).read_text('utf-8'))
        values = next(iter(payload['分类'].values()))[0]
        row = dict(zip(payload['表头'], values))
        row.update({'名义倍数（倍）': leverage, '止盈方案编号': tp,
                    '入场触发口径': entry_mode, '成交价格口径': fill_mode,
                    '回测计算版本': ENGINE_VERSION + ':' + fill_mode})
        payload['分类'] = {'分批止盈': [list(row.values())]}
        for filename in F.RANKING_FILES:
            self.helper.write_json(directory / filename, payload)
        return F.find_fingerprint_matches(config_fingerprint(row), directory)[0]

    def manifest(self, matches):
        path = self.root / 'manifest.json'
        self.helper.write_json(path, {'version': 1, 'strategies': matches})
        return path

    def test_two_leverages_from_one_original_remain_two_exact_selections(self):
        match = self.make_original('original', 10., 2421, 'TF_EVENT', 'SLIPPAGE', 'THEORETICAL')
        directory = Path(match['source_dir'])
        payload = json.loads((directory / F.RANKING_FILES[0]).read_text('utf-8'))
        values = next(iter(payload['分类'].values()))[0]
        other = copy.deepcopy(values)
        other[payload['表头'].index('名义倍数（倍）')] = 9.
        next(iter(payload['分类'].values())).append(other)
        for filename in F.RANKING_FILES:
            self.helper.write_json(directory / filename, payload)
        second_fp = config_fingerprint(dict(zip(payload['表头'], other)))
        matches = [match, F.find_fingerprint_matches(second_fp, directory)[0]]
        expected = F.restore_many(matches)
        self.assertEqual([r['selection']['仓位倍数'] for r in expected], [[10.], [9.]])
        self.assertEqual([配置统计(r['selection'])['包含仓位完整组合数'] for r in expected], [1, 1])
        calls = []

        def finish(command, project, control, output, index, total, fingerprint):
            saved = json.loads((output / '组合选择.json').read_text('utf-8'))
            calls.append(saved)
            self.assertEqual(command[command.index('--selection') + 1], str(output / '组合选择.json'))
            restored = expected[index - 1]
            self.assertEqual(saved, restored['selection'])
            self.assertEqual(command[command.index('--expected-source-fingerprint') + 1],
                             restored['source_fingerprint'])
            identity = {'selection_signature': 配置签名(saved), 'feature_request': restored['request'],
                        'engine_version': restored['current_engine_version'],
                        'code_sha256': restored['current_code_sha256']}
            self.helper.write_json(output / '回测运行身份.json', identity)
            self.helper.write_json(output / '回测数据说明.json', {
                'sources': restored['sources'], 'request': restored['request'],
                'start_utc': restored['period']['start_utc'], 'end_utc': restored['period']['end_utc']})
            self.helper.write_json(output / '断点记录.json', {
                'selection': saved, 'selection_signature': identity['selection_signature'],
                'run_identity': identity})
            (output / '全部回测结果.csv').write_text('test\n1\n', 'utf-8')
            return 'completed', 'fixture completed'

        with mock.patch.object(B, '_run_child', side_effect=finish), mock.patch.object(B, 'emit'):
            state = B.run_batch(self.manifest(matches), self.root / 'batch')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(calls), 2)
        self.assertEqual([row['仓位倍数'] for row in calls], [[10.], [9.]])

    def test_real_worker_produces_only_two_exact_rows_with_distinct_tp_mode_cost_and_leverage(self):
        import numpy as np
        import pandas as pd
        from feature_builder import build_features

        x = np.arange(1200)
        close = 2000. + 6 * np.sin(x / 11.) + 2 * np.sin(x / 3.)
        opening = np.r_[close[0], close[:-1]]
        volume = 100. + x % 23
        frame = pd.DataFrame({'openTime': 1735689600000 + x * 60000, 'open': opening,
                              'high': np.maximum(opening, close) + .5,
                              'low': np.minimum(opening, close) - .5, 'close': close,
                              'volume': volume, 'trades': 10 + x % 5,
                              'taker_buy_base': volume * (.5 + .3 * np.sin(x / 7.))})
        specs = [(10., 2, 'TF_EVENT', 'FEE', 'CLOSE_CONFIRMED'),
                 (9., 2421, 'MACD_CYCLE', 'SLIPPAGE', 'THEORETICAL')]
        matches = []
        for index, spec in enumerate(specs, 1):
            data = self.root / f'synthetic_{index}.csv'
            frame.to_csv(data, index=False)
            start = f'2025-01-01T0{index}:00:00Z'
            end = f'2025-01-01T{17 + index}:00:00Z'
            features = build_features(str(data), str(self.root / f'features_{index}'), start=start, end=end)
            meta = json.loads(features.with_name('features_meta.json').read_text('utf-8'))
            matches.append(self.make_original(f'original_{index}', *spec, meta=meta))
        expected = F.restore_many(matches)
        # Exercise the saved queue's JSON round-trip before the batch runner sees it.
        manifest = self.manifest(json.loads(json.dumps(matches)))
        original_bytes = manifest.read_bytes()
        with mock.patch.object(B, 'emit'), mock.patch.dict(os.environ, {
                'OPENBLAS_NUM_THREADS': '1', 'PYTHONDONTWRITEBYTECODE': '1'}):
            state = B.run_batch(manifest, self.root / 'batch', threads=1, device='cpu')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(state['items']), 2)
        self.assertEqual(manifest.read_bytes(), original_bytes)
        actual = []
        for index, (item, restored) in enumerate(zip(state['items'], expected), 1):
            output = Path(item['output'])
            self.assertEqual(output.name, f'{index:03d}_{matches[index - 1]["fingerprint"]}')
            with Path(item['csv']).open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertNotIn(';', row['所选仓位顺序'])
            actual.append((float(row['所选仓位顺序'].removesuffix('x')),
                           int(row['止盈方案编号']), row['入场触发口径'],
                           row['成本模式'], row['成交价格口径']))
            written = json.loads((output / '组合选择.json').read_text('utf-8'))
            checkpoint = json.loads((output / '断点记录.json').read_text('utf-8'))
            self.assertEqual(written, restored['selection'])
            self.assertEqual(checkpoint['selection'], restored['selection'])
            self.assertEqual(配置统计(written)['包含仓位完整组合数'], 1)
            meta = json.loads((output / '回测数据说明.json').read_text('utf-8'))
            self.assertEqual(meta['sources'], restored['sources'])
            self.assertEqual(meta['request'], restored['request'])
            self.assertEqual(json.loads((output / '排行榜设置.json').read_text('utf-8')),
                             restored['ranking_settings'])
        self.assertEqual(actual, specs)
        self.assertEqual(sum(配置统计(r['selection'])['包含仓位完整组合数'] for r in expected), 2)


if __name__ == '__main__':
    unittest.main()
