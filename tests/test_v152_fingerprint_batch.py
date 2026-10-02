import copy
import csv
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import fingerprint_batch as B
import fingerprint_lookup as F
from ranking_view import config_fingerprint
from selection_config import 配置签名
import test_v149_fingerprint_lookup as fixture


class FingerprintBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v152_batch_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = fixture.FingerprintLookupTests()
        self.fixture.root = self.root

    def originals(self):
        first, fp1, _ = self.fixture.make_result('original1')
        second, _, _ = self.fixture.make_result('original2', mode='SLIPPAGE')
        payload = json.loads((second / F.RANKING_FILES[0]).read_text('utf-8'))
        row = dict(zip(payload['表头'], next(iter(payload['分类'].values()))[0]))
        fp2 = config_fingerprint(row)
        return [F.find_fingerprint_matches(fp1, first)[0], F.find_fingerprint_matches(fp2, second)[0]]

    def manifest(self, matches, inside=False):
        output = self.root / 'batch'
        output.mkdir(exist_ok=True)
        path = output / '指纹批量清单.json' if inside else self.root / 'manifest.json'
        path.write_text(json.dumps({'version': 1, 'strategies': matches}, ensure_ascii=False), 'utf-8')
        return path, output

    def fake_complete(self, command, project, control, output, index, total, fingerprint):
        (output / '全部回测结果.csv').write_text('test\n1\n', 'utf-8')
        restored = json.loads((output / '指纹来源.json').read_text('utf-8'))
        signature = 配置签名(restored['selection'])
        identity = {'selection_signature': signature, 'feature_request': restored['request'],
                    'engine_version': restored['current_engine_version'], 'code_sha256': restored['current_code_sha256']}
        self.fixture.write_json(output / '回测数据说明.json', {
            'sources': restored['sources'], 'request': restored['request'], **restored['period']})
        self.fixture.write_json(output / '回测运行身份.json', identity)
        self.fixture.write_json(output / '断点记录.json', {
            'selection': restored['selection'], 'selection_signature': signature, 'run_identity': identity})
        return 'completed', 'fixture completed'

    def test_parse_whole_tokens_order_duplicates_and_rejections(self):
        a, b = 'A' * 16, 'b' * 16
        self.assertEqual(F.parse_fingerprints(f' {a}，{b};{a.lower()}；\n{b} '), [a.lower(), b])
        for invalid in ('', f'{a}/other', f'prefix{a}', 'a' * 20, f'{a} invalid', f'[{a}]', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): F.parse_fingerprints(invalid)

    def test_group_lookup_reads_each_original_file_once_and_preserves_ambiguous_sources(self):
        matches = self.originals()
        # A second source with the same fingerprint still needs explicit UI choice.
        self.fixture.make_result('same_as_first', s3=.03)
        wanted = [matches[1]['fingerprint'], matches[0]['fingerprint'], '0' * 16]
        with mock.patch.object(F, '_read_json', wraps=F._read_json) as read:
            groups = F.find_fingerprint_groups('\n'.join(wanted), self.root)
        self.assertEqual(list(groups), wanted)
        self.assertEqual(len(groups[wanted[1]]), 2)
        self.assertEqual(groups[wanted[2]], [])
        paths = [call.args[0] for call in read.call_args_list]
        self.assertEqual(len(paths), len(set(paths)))

    def test_restore_many_is_all_or_nothing(self):
        matches = self.originals()
        result = F.restore_many(matches)
        self.assertEqual([r['fingerprint'] for r in result], [r['fingerprint'] for r in matches])
        with mock.patch.object(F, 'restore_fingerprint_match', side_effect=[result[0], ValueError('bad second')]):
            with self.assertRaisesRegex(ValueError, 'bad second'): F.restore_many(matches)

    def test_same_fingerprint_different_sources_allowed_but_exact_pair_duplicate_rejected(self):
        matches = self.originals()
        second, same_fp, _ = self.fixture.make_result('different_period', s3=.03)
        second_match = F.find_fingerprint_matches(same_fp, second)[0]
        path, _ = self.manifest([matches[0], second_match])
        self.assertEqual(len(B.read_manifest(path)['strategies']), 2)
        path.write_text(json.dumps({'version':1,'strategies':[matches[0],dict(matches[0], source_dir=str(Path(matches[0]['source_dir'])/'.'))]}), 'utf-8')
        with self.assertRaisesRegex(ValueError, '相同指纹及相同来源'): B.read_manifest(path)

    def test_batch_discovery_is_bounded_and_ignores_status_paths(self):
        directory, fp, _ = self.fixture.make_result('batch/001_example')
        self.fixture.make_result('unmarked/nested', s3=.02)
        batch = directory.parent
        (batch/'批量状态.json').write_text(json.dumps({'items':[{'output':str(self.root/'unmarked/nested')}]}), 'utf-8')
        for search_root in (self.root, batch):
            groups = F.find_fingerprint_groups(fp, search_root)
            self.assertEqual([item['source_dir'] for item in groups[fp]], [str(directory)])

    def test_research_collection_finds_real_children_without_recursive_search(self):
        directory, fp, _ = self.fixture.make_result('research/strategy01')
        self.fixture.make_result('research/unmarked/deeper', s3=.02)
        self.fixture.make_result('unmarked/nested', s3=.03)
        collection = directory.parent
        # The metadata marks the collection only; referenced external paths
        # are not trusted as discovery targets.
        (collection/'候选完整参数.json').write_text(json.dumps({
            'source_dir': str(self.root/'unmarked/nested')}), 'utf-8')
        for search_root in (self.root, collection):
            groups = F.find_fingerprint_groups(fp, search_root)
            self.assertEqual([item['source_dir'] for item in groups[fp]], [str(directory)])

    def test_discovery_does_not_follow_directory_junctions_or_symlinks(self):
        directory, fp, _ = self.fixture.make_result('research/strategy01')
        collection = directory.parent
        (collection/'候选完整参数.json').write_text('{}', 'utf-8')
        original = Path.is_symlink
        with mock.patch.object(Path, 'is_symlink', lambda path: path == directory or original(path)):
            self.assertEqual(F.find_fingerprint_matches(fp, self.root), [])
        original_stat = Path.lstat
        def junction_stat(path):
            result = original_stat(path)
            return (SimpleNamespace(st_mode=result.st_mode,
                                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    if path == directory else result)
        with mock.patch.object(Path, 'lstat', junction_stat):
            self.assertEqual(F.find_fingerprint_matches(fp, self.root), [])

    def test_discovery_does_not_require_python312_junction_method(self):
        directory, fp, _ = self.fixture.make_result('research/strategy01')
        (directory.parent/'候选完整参数.json').write_text('{}', 'utf-8')
        with mock.patch.object(Path, 'is_junction', create=True,
                               side_effect=AssertionError('Unavailable in Python 3.11')):
            self.assertEqual([row['source_dir'] for row in F.find_fingerprint_matches(fp, self.root)],
                             [str(directory)])

    def test_sequential_commands_preserve_independent_sources_dates_and_configs(self):
        matches = self.originals(); path, output = self.manifest(matches, inside=True)
        (output / '工作进程日志.txt').touch()
        expected = F.restore_many(matches)
        with mock.patch.object(B, '_run_child', side_effect=self.fake_complete) as child, mock.patch.object(B, 'emit'):
            state = B.run_batch(path, output, threads=1, device='cpu')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(child.call_count, 2)
        for index, (call, restored) in enumerate(zip(child.call_args_list, expected), 1):
            command, _, _, folder, *_ = call.args
            self.assertEqual(folder.name, f'{index:03d}_{restored["fingerprint"]}')
            self.assertEqual(command[command.index('--csv') + 1], restored['sources']['kline'])
            self.assertEqual(command[command.index('--start') + 1], restored['start'])
            self.assertEqual(command[command.index('--end') + 1], restored['end'])
            self.assertEqual(command[command.index('--ranking-settings') + 1], str(folder/'排行榜设置.json'))
            self.assertEqual(json.loads((folder / '组合选择.json').read_text('utf-8')), restored['selection'])
            self.assertEqual(json.loads((folder / '排行榜设置.json').read_text('utf-8')), restored['ranking_settings'])
            self.assertTrue((folder / '独立回测状态.json').is_file())
        self.assertNotEqual(expected[0]['selection']['成本模式'], expected[1]['selection']['成本模式'])
        with (output / '批量结果汇总.csv').open(encoding='utf-8-sig', newline='') as handle:
            summary = list(csv.DictReader(handle))
        self.assertEqual(len(summary), 2)
        self.assertTrue(all(Path(row['完整结果CSV']).is_file() for row in summary))

    def test_no_resume_or_different_manifest_can_overwrite_existing_batch(self):
        path, output = self.manifest(self.originals())
        with mock.patch.object(B, '_run_child', side_effect=self.fake_complete), mock.patch.object(B, 'emit'):
            B.run_batch(path, output)
        before = (output / B.STATUS_FILE).read_bytes()
        with mock.patch.object(B, '_run_child') as child, self.assertRaisesRegex(ValueError, '续跑暂不支持'):
            B.run_batch(path, output)
        child.assert_not_called()
        self.assertEqual((output / B.STATUS_FILE).read_bytes(), before)

    def test_preflight_failure_starts_no_child(self):
        matches = self.originals(); path, output = self.manifest(matches)
        with mock.patch.object(B, 'restore_many', side_effect=ValueError('bad second')), mock.patch.object(B, '_run_child') as child, mock.patch.object(B, 'emit'):
            with self.assertRaisesRegex(ValueError, 'bad second'): B.run_batch(path, output)
        child.assert_not_called()
        self.assertEqual(json.loads((output / B.STATUS_FILE).read_text('utf-8'))['status'], 'failed')

    def test_changed_source_before_second_item_aborts_without_starting_it(self):
        matches = self.originals(); path, output = self.manifest(matches)
        restored = F.restore_many(matches)
        changed = copy.deepcopy(restored[1]); changed['selection']['入场约束']['最小S3距离'] += .001
        with mock.patch.object(B, 'restore_many', return_value=restored), \
             mock.patch.object(B, 'restore_fingerprint_match', side_effect=[restored[0], changed]), \
             mock.patch.object(B, '_run_child', side_effect=self.fake_complete) as child, mock.patch.object(B, 'emit'):
            with self.assertRaisesRegex(ValueError, '已变化'): B.run_batch(path, output)
        self.assertEqual(child.call_count, 1)
        self.assertFalse((output / f'002_{matches[1]["fingerprint"]}').exists())

    def test_stop_after_child_or_exception_never_starts_next_item(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                matches = self.originals() if not failure else self._saved_matches
                self._saved_matches = matches
                path = self.root / f'manifest_{failure}.json'
                path.write_text(json.dumps({'version': 1, 'strategies': matches}), 'utf-8')
                output = self.root / f'output_{failure}'
                def stopped(command, project, control, folder, index, total, fingerprint):
                    if failure: raise RuntimeError('worker failed')
                    (control / '停止.flag').touch()
                    return self.fake_complete(command, project, control, folder, index, total, fingerprint)
                with mock.patch.object(B, '_run_child', side_effect=stopped) as child, mock.patch.object(B, 'emit'):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'worker failed'): B.run_batch(path, output)
                    else:
                        self.assertEqual(B.run_batch(path, output)['status'], 'stopped')
                self.assertEqual(child.call_count, 1)
                self.assertFalse((output / f'002_{matches[1]["fingerprint"]}').exists())

    def test_pause_between_items_and_control_forwarding(self):
        parent = self.root / '控制'; parent.mkdir()
        child = self.root / 'child'
        (parent / '暂停.flag').touch()
        self.assertEqual(B._controls(parent, child), (True, False))
        self.assertTrue((child / '暂停.flag').exists())
        def resume(_): (parent / '暂停.flag').unlink()
        with mock.patch.object(B.time, 'sleep', side_effect=resume), mock.patch.object(B, 'emit') as events:
            self.assertTrue(B._wait_between_items(parent, 2, 2))
        self.assertEqual([c.args[0] for c in events.call_args_list], ['batch_paused', 'batch_resumed'])
        (parent / '停止.flag').touch()
        self.assertEqual(B._controls(parent, child), (False, True))
        self.assertTrue(all((child / name).exists() for name in B.STOP_FLAGS))
        self.assertFalse((child / '暂停.flag').exists())

    def test_real_short_child_receives_stop_even_if_it_later_reports_completed(self):
        parent = self.root / '控制'; parent.mkdir()
        child = self.root / 'child'; child.mkdir()
        script = """import json,sys,time
from pathlib import Path
out=Path(sys.argv[1]); print(json.dumps({'type':'stage','message':'ready'}),flush=True)
deadline=time.monotonic()+10
while not (out/'控制'/'停止.flag').exists() and time.monotonic()<deadline: time.sleep(.02)
assert all((out/'控制'/name).exists() for name in ('停止.flag','停止候选导出.flag','停止最差导出.flag'))
(out/'全部回测结果.csv').write_text('x\\n1\\n')
print(json.dumps({'type':'completed'}),flush=True)
"""
        def event(kind, **fields):
            if kind == 'batch_child_event' and fields['event']['type'] == 'stage': (parent / '停止.flag').touch()
        with mock.patch.object(B, 'emit', side_effect=event):
            status, _ = B._run_child([sys.executable, '-u', '-c', script, str(child)], self.root, parent, child, 1, 2, 'a'*16)
        self.assertEqual(status, 'stopped')


class RealWorkerBatchTests(unittest.TestCase):
    def test_two_synthetic_strategies_run_separately_in_the_real_worker(self):
        import numpy as np
        import pandas as pd
        from feature_builder import build_features
        with tempfile.TemporaryDirectory(prefix='v152_real_batch_') as temp:
            root = Path(temp)
            count = 1100; x = np.arange(count)
            close = 2000. + 6*np.sin(x/11.) + 2*np.sin(x/3.)
            opening = np.r_[close[0], close[:-1]]
            volume = 100. + x % 23
            data = root / 'synthetic.csv'
            pd.DataFrame({'openTime': 1735689600000+x*60000, 'open': opening,
                          'high': np.maximum(opening,close)+.5, 'low': np.minimum(opening,close)-.5,
                          'close': close, 'volume': volume, 'trades': 10+x%5,
                          'taker_buy_base': volume*(.5+.3*np.sin(x/7.))}).to_csv(data,index=False)
            features = build_features(str(data), str(root/'features'))
            meta = json.loads(features.with_name('features_meta.json').read_text('utf-8'))
            helper = fixture.FingerprintLookupTests(); helper.root = root
            matches = []
            for mode in ('LIVE_01', 'MACD_CYCLE'):
                directory, _, selected = helper.make_result(mode, s3=0)
                selected['入场触发口径'] = mode
                selected['候选筛选'].update(启用=False, 自动导出=False, 导出旧排行=False)
                identity = json.loads((directory/'回测运行身份.json').read_text('utf-8'))
                identity.update(feature_request=meta['request'], selection_signature=配置签名(selected))
                helper.write_json(directory/'组合选择.json', selected)
                helper.write_json(directory/'断点记录.json', {'selection': selected, 'run_identity': identity,
                                                            'selection_signature': identity['selection_signature']})
                helper.write_json(directory/'回测运行身份.json', identity)
                helper.write_json(directory/'回测数据说明.json', meta)
                payload=json.loads((directory/F.RANKING_FILES[0]).read_text('utf-8'))
                for rows in payload['分类'].values():
                    for row in rows: row[payload['表头'].index('入场触发口径')]=mode
                for filename in F.RANKING_FILES: helper.write_json(directory/filename,payload)
                row=dict(zip(payload['表头'],next(iter(payload['分类'].values()))[0]))
                matches.append(F.find_fingerprint_matches(config_fingerprint(row),directory)[0])
            manifest=root/'manifest.json'; manifest.write_text(json.dumps({'version':1,'strategies':matches}), 'utf-8')
            with mock.patch.object(B,'emit'), mock.patch.dict(os.environ, {'OPENBLAS_NUM_THREADS':'1','PYTHONDONTWRITEBYTECODE':'1'}):
                state=B.run_batch(manifest,root/'batch',threads=1,device='cpu')
            self.assertEqual(state['status'],'completed')
            self.assertEqual(len(state['items']),2)
            for item, mode in zip(state['items'], ('LIVE_01','MACD_CYCLE')):
                with Path(item['csv']).open(encoding='utf-8-sig',newline='') as handle: rows=list(csv.DictReader(handle))
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['入场触发口径'],mode)
                self.assertEqual(rows[0]['所选仓位顺序'],'2x')
                checkpoint=json.loads((Path(item['output'])/'断点记录.json').read_text('utf-8'))
                self.assertEqual(checkpoint['selection']['开仓位置过滤'],['RANGE60_EDGE20'])
                self.assertEqual(checkpoint['selection']['成本模式'],'FEE')
                self.assertEqual(json.loads((Path(item['output'])/'排行榜设置.json').read_text('utf-8'))['排行指标'],'胜率（%）')


if __name__ == '__main__':
    unittest.main()
