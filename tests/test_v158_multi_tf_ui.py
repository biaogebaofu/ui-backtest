"""Hidden, isolated Tk checks; no real user settings or workers are touched."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from extended_rules import (ENTRY_RULE_REQUIREMENTS, RESEARCH_CASE_CODES,
                            entry_supported_timeframes)
from selection_config import 配置统计
from fifth_policy import RETIRED_FIFTH_CODES
import fingerprint_lookup as lookup
import test_v149_fingerprint_ui as single
import test_v152_fingerprint_queue_ui as queue


class MultiTimeframeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        queue.FingerprintQueueUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        queue.FingerprintQueueUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        queue.FingerprintQueueUiTests.setUp(self)

    def tearDown(self):
        queue.FingerprintQueueUiTests.tearDown(self)

    snapshot = queue.FingerprintQueueUiTests.snapshot

    def full_caps(self):
        return {key: True for req in ENTRY_RULE_REQUIREMENTS.values() for key in req}

    def test_each_timeframe_exposes_supported_research_codes_without_new_markers(self):
        panel = self.app.selection_panel
        for tf, values in panel.case_vars.items():
            expected = {c for c in RESEARCH_CASE_CODES
                        if tf in entry_supported_timeframes(c) and c not in RETIRED_FIFTH_CODES}
            self.assertEqual(set(values) & set(RESEARCH_CASE_CODES), expected)
            self.assertFalse(set(values) & set(RETIRED_FIFTH_CODES))
            self.assertTrue(expected, tf)
            for code in expected:
                widget = panel.case_widgets[tf][code]
                label = str(widget.cget('text'))
                self.assertFalse(label.startswith('新 '), (tf, code, label))
                self.assertNotIn('【新】', label)
                self.assertNotIn('★新', label)
                self.assertNotEqual(widget.cget('style'), 'New.TCheckbutton')

    def test_native_timeframe_capability_gates_group_selection_and_restore(self):
        panel = self.app.selection_panel
        full = self.full_caps()
        per_tf = {tf: dict(full) for tf in panel.case_vars}
        per_tf['5m'] = {'ohlcv': True}
        panel.set_data_capabilities(full, timeframe_capabilities=per_tf)
        panel._set_third_source('5m', 'available')
        panel._set_fourth_source('5m', 'available')
        missing = [c for c in RESEARCH_CASE_CODES if c in panel.case_vars['5m']
                   and not ENTRY_RULE_REQUIREMENTS[c].issubset({'ohlcv'})]
        self.assertTrue(missing)
        for code in missing:
            self.assertFalse(panel.case_vars['5m'][code].get())
            self.assertTrue(panel.case_widgets['5m'][code].instate(['disabled']))
        config = single.single_selection()
        config['开仓条件']['5m'] = [missing[0]]
        panel.apply_config(config)
        self.assertFalse(panel.case_vars['5m'][missing[0]].get())
        panel.clear_data_capabilities()
        self.assertTrue(panel.case_widgets['5m'][missing[0]].instate(['!disabled']))

    def test_legacy_metadata_does_not_claim_high_timeframe_research_coverage(self):
        panel = self.app.selection_panel
        panel.set_data_capabilities(self.full_caps())
        for tf, values in panel.case_vars.items():
            if tf == '1m':
                continue
            for code in set(values) & set(RESEARCH_CASE_CODES):
                self.assertTrue(panel.case_widgets[tf][code].instate(['disabled']))

    def test_multiple_timeframes_save_and_restore_with_batch_folds(self):
        app, panel = self.app, self.app.selection_panel
        panel.clear_data_capabilities()
        config = single.single_selection()
        for tf in panel.case_vars:
            config['开仓条件'][tf] = [42, 156]
        panel.apply_config(config)
        self.assertEqual(app.current_selection()['开仓条件'], config['开仓条件'])
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        panel.apply_config(single.single_selection())
        path = mock.Mock(is_file=mock.Mock(return_value=True),
                         read_text=mock.Mock(return_value=json.dumps(saved)))
        with mock.patch.object(self.ui, '用户设置文件', path), mock.patch.object(app, '_launch_process') as launch:
            type(self).original_load(app)
        self.assertEqual(app.current_selection()['开仓条件'], config['开仓条件'])
        for tf in panel.case_vars:
            self.assertTrue(panel.third_entry_fold_vars[tf].get())
            self.assertTrue(panel.fourth_entry_fold_vars[tf].get())
        launch.assert_not_called()

    def test_fingerprint_restoration_checks_high_timeframe_data_before_applying(self):
        value = single.restored_payload()
        value['selection']['开仓条件']['1m'] = [0]
        value['selection']['开仓条件']['5m'] = [156]
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '数据字段'):
            self.app.apply_fingerprint_restoration(value)
        self.assertEqual(self.snapshot(), before)
        value['capabilities_by_timeframe'] = {'5m': self.full_caps()}
        self.app.apply_fingerprint_restoration(value)
        self.assertEqual(self.app.current_selection()['开仓条件']['5m'], [156])

    def test_five_persisted_fingerprints_remain_independent_from_editor(self):
        app = self.app
        items = [queue.bound(queue.match(code, leverage=size))
                 for code, size in zip('abcde', (2., 3., 4., 5., 6.))]
        before = self.snapshot()
        app.fingerprint_queue = app._validated_fingerprint_queue(items + [copy.deepcopy(items[0])])
        app._refresh_fingerprint_queue()
        self.assertEqual(len(app.fingerprint_queue), 5)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(app.run_target_var.get(), 'EDITOR')
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        path = mock.Mock(is_file=mock.Mock(return_value=True),
                         read_text=mock.Mock(return_value=json.dumps(saved)))
        app.fingerprint_queue = []
        with mock.patch.object(self.ui, '用户设置文件', path), mock.patch.object(app, '_launch_process') as launch:
            type(self).original_load(app)
        self.assertEqual(app.fingerprint_queue, items)
        self.assertEqual(app.run_target_var.get(), 'EDITOR')
        self.assertEqual(self.snapshot(), before)
        launch.assert_not_called()
        for item in items:
            self.assertEqual(配置统计(item['verified_snapshot']['selection'])['包含仓位完整组合数'], 1)
        with mock.patch.object(Path, 'mkdir'), mock.patch.object(Path, 'exists', return_value=False), \
                mock.patch.object(app, '_launch_process', return_value=True):
            app.start_fingerprint_batch()
        manifest = self.write.call_args.args[1]
        self.assertEqual(manifest, {'version': 1, 'strategies': items})
        self.assertEqual(self.snapshot()[:3], before[:3])

    def test_code_change_only_adds_traceability_display_without_changing_snapshot(self):
        item = queue.match()
        value = queue.restoration(item)
        original = lookup.bind_fingerprint_match(item, value)
        changed = copy.deepcopy(value)
        changed['current_code_sha256'] = 'f' * 64
        changed['current_engine_version'] = 'new-release'
        rebound = lookup.bind_fingerprint_match(original, changed)
        self.assertEqual(rebound['verified_snapshot'], original['verified_snapshot'])
        self.assertEqual(rebound['row'], original['row'])
        self.assertEqual(self.app._queue_display_values(0, rebound)[3], '原定义可追溯')


if __name__ == '__main__':
    unittest.main()
