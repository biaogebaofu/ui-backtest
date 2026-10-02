import copy
import json
import unittest

import fingerprint_lookup as F
import test_v149_fingerprint_lookup as fixture


class CandidateFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.FingerprintLookupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_candidate_not_in_top_or_worst_is_searchable_and_restorable(self):
        directory, fp, _ = self.fixture.make_result()
        native = json.loads((directory / F.RANKING_FILES[0]).read_text('utf-8'))
        native['原始结果目录'] = str(directory)
        for name in F.RANKING_FILES:
            self.fixture.write_json(directory / name, {'表头': native['表头'], '分类': {}})
        self.fixture.write_json(directory / F.CANDIDATE_FILES[0], native)
        matches = F.find_fingerprint_matches(fp, directory)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['ranking_files'], ['候选原始记录.json'])
        self.assertEqual(F.restore_fingerprint_match(matches[0])['fingerprint'], fp)

    def test_candidate_cannot_bind_to_another_complete_run_context(self):
        origin, fp, _ = self.fixture.make_result('original', s3=.0025)
        wrong, other_fp, _ = self.fixture.make_result('other', s3=.004)
        self.assertEqual(fp, other_fp)  # S3 is deliberately outside the row hash.
        native = json.loads((origin / F.RANKING_FILES[0]).read_text('utf-8'))
        native['原始结果目录'] = str(origin)
        for name in F.RANKING_FILES:
            self.fixture.write_json(wrong / name, {'表头': native['表头'], '分类': {}})
        self.fixture.write_json(wrong / F.CANDIDATE_FILES[0], native)
        with self.assertRaisesRegex(ValueError, '来源目录'):
            F.find_fingerprint_matches(fp, wrong)

    def test_old_candidate_settings_are_preserved_with_explicit_legacy_semantics(self):
        directory, fp, _ = self.fixture.make_result()
        for name in ('组合选择.json', '断点记录.json'):
            content = json.loads((directory / name).read_text('utf-8'))
            raw = content if name == '组合选择.json' else content['selection']
            for key in ('筛选方案', '比较范围', '资金保留比例'):
                raw['候选筛选'].pop(key, None)
            self.fixture.write_json(directory / name, content)
        restored = F.restore_fingerprint_match(F.find_fingerprint_matches(fp, directory)[0])
        self.assertEqual(restored['selection']['候选筛选']['筛选方案'], 'LEGACY_STRESS')
        self.assertEqual(restored['selection']['候选筛选']['比较范围'], 'TARGET')

    def test_all_run_scope_does_not_replace_candidate_target_or_trading_size(self):
        directory, fp, _ = self.fixture.make_result()
        for name in ('组合选择.json', '断点记录.json'):
            content = json.loads((directory / name).read_text('utf-8'))
            raw = content if name == '组合选择.json' else content['selection']
            raw['候选筛选'].update({'启用': True, '筛选方案': 'RETURN_DRAWDOWN',
                                  '比较范围': 'ALL_RUN', '资金保留比例': .9, '统一目标杠杆': 10.})
            self.fixture.write_json(directory / name, content)
        restored = F.restore_fingerprint_match(F.find_fingerprint_matches(fp, directory)[0])
        self.assertEqual(restored['selection']['候选筛选']['统一目标杠杆'], 10.)
        self.assertEqual(restored['selection']['仓位倍数'], [2.])

    def test_invalid_new_screen_setting_is_not_repaired_silently(self):
        _, _, original = self.fixture.make_result()
        for key, invalid in (('筛选方案', 'made_up'), ('比较范围', 'made_up'), ('资金保留比例', 2.0)):
            raw = copy.deepcopy(original)
            raw['候选筛选'][key] = invalid
            with self.assertRaises(ValueError):
                F._complete_selection(raw)


if __name__ == '__main__':
    unittest.main()
