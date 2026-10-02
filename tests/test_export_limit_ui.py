import copy
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fingerprint_lookup as F
from ranking_limits import top_limit
from ranking_view import build_view
import test_v147_rank_view as view_fixture
import test_v149_fingerprint_lookup as fingerprint_fixture


class ExportLimitViewTests(unittest.TestCase):
    def test_completion_uses_the_exported_limit(self):
        import ui
        import test_v156_ranking_completion_ui as fixture
        app = fixture.RankingCompletionUiTests().app()
        app.messages.put({'type': 'legacy_completed', 'top_limit': 10000,
                          'top_rows': 10000, 'worst_rows': 1000,
                          'top_path': 'top10000.xlsx', 'worst_path': 'worst1000.xlsx'})
        ui.App.poll_messages(app)
        self.assertIn('最优前10000', app.status_var.set.call_args.args[0])
        self.assertIn('最优前10000 Excel：top10000.xlsx', [call.args[0] for call in app.append_log.call_args_list])

    def test_each_category_and_global_ranking_honor_both_limits(self):
        original = view_fixture.ExecutionRankViewTests().payload()
        headers = original['表头']
        native = dict(zip(headers, original['分类']['移动止盈'][0]))
        rows = []
        for index in reversed(range(10125)):
            row = dict(native, 基础策略编号=index + 1)
            row['期末资金（USDC）'] = 100. + index
            rows.append([row.get(key) for key in headers])
        views = {}
        for limit in (5000, 10000):
            payload = dict(original, 名额=limit, 分类={'移动止盈': rows})
            view = build_view(payload)
            self.assertEqual(len(view['分类']['移动止盈']), limit)
            self.assertEqual(len(view['分类'][f'全局前{limit}']), limit)
            money = view['表头'].index('期末资金（USDC）')
            self.assertEqual([row[money] for row in view['分类'][f'全局前{limit}']],
                             [100. + i for i in reversed(range(10125))][:limit])
            views[limit] = view
            self.assertEqual(len(payload['分类']['移动止盈']), 10125)
        self.assertEqual(views[5000]['分类']['移动止盈'], views[10000]['分类']['移动止盈'][:5000])
        self.assertEqual(len(build_view(dict(original, 名额=10000))['分类']['全局前10000']), 1)

    def test_original_fingerprint_settings_keep_optional_limit_and_old_snapshot_shape(self):
        fixture = fingerprint_fixture.FingerprintLookupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        directory, fingerprint, _ = fixture.make_result()
        path = directory / '排行榜设置.json'
        original = json.loads(path.read_text('utf-8'))
        self.assertEqual(F._ranking_settings(directory), original)
        fixture.write_json(path, dict(original, 最优名额=10000))
        self.assertEqual(F._ranking_settings(directory), dict(original, 最优名额=10000))
        match = F.find_fingerprint_matches(fingerprint, directory)[0]
        restored = F.restore_fingerprint_match(match)
        self.assertEqual(restored['ranking_settings']['最优名额'], 10000)
        pinned = F.bind_fingerprint_match(match, restored)
        self.assertEqual(F.restore_many([pinned])[0], restored)
        fixture.write_json(path, original)
        with self.assertRaisesRegex(ValueError, '已变化'):
            F.restore_many([pinned])
        old = copy.deepcopy(restored)
        old['ranking_settings'].pop('最优名额')
        explicit = copy.deepcopy(old)
        explicit['ranking_settings']['最优名额'] = 5000
        before = copy.deepcopy([old, explicit])
        self.assertEqual(F.merge_fingerprint_restorations([old, explicit])['exact_count'], 2)
        self.assertEqual([old, explicit], before)
        explicit['ranking_settings']['最优名额'] = 10000
        with self.assertRaisesRegex(ValueError, '排行榜设置不同'):
            F.merge_fingerprint_restorations([old, explicit])
        for invalid in (True, '10000', 10000.0, 6000, None):
            fixture.write_json(path, dict(original, 最优名额=invalid))
            with self.assertRaises(ValueError):
                F._ranking_settings(directory)


class ExportLimitUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import ui
        cls.ui = ui
        cls.stack = ExitStack()
        original_init = tk.Tk.__init__
        def hidden(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()
        cls.stack.enter_context(mock.patch.object(tk.Tk, '__init__', hidden))
        cls.stack.enter_context(mock.patch.object(ui.App, 'load_user_settings'))
        cls.stack.enter_context(mock.patch.object(ui.App, 'detect_gpu'))
        cls.writer = cls.stack.enter_context(mock.patch.object(ui, 'atomic_json'))
        cls.app = ui.App()
        if cls.app._poll_after_id:
            cls.app.after_cancel(cls.app._poll_after_id)
            cls.app._poll_after_id = None

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.stack.close()

    def setUp(self):
        self.app.proc = None
        self.app.apply_ranking_settings({'排行指标': self.ui.默认排行指标, '门槛': []})
        self.writer.reset_mock()

    def test_default_and_selection_persist_without_changing_strategy(self):
        app = self.app
        self.assertEqual(app.rank_top_limit_var.get(), '5000')
        self.assertEqual(top_limit(app.current_ranking_settings()), 5000)
        before = copy.deepcopy(app.current_selection())
        app.rank_top_limit_var.set('10000')
        self.assertTrue(app.save_user_settings())
        saved = self.writer.call_args.args[1]
        self.assertEqual(saved['排行榜设置']['最优名额'], 10000)
        app.apply_ranking_settings(saved['排行榜设置'])
        self.assertEqual(app.rank_top_limit_var.get(), '10000')
        self.assertEqual(app.current_selection(), before)
        app.apply_ranking_settings({'排行指标': self.ui.默认排行指标, '门槛': []})
        self.assertEqual(app.rank_top_limit_var.get(), '5000')
        self.assertNotIn('最优名额', app.current_ranking_settings())

    def test_both_pages_share_the_same_readonly_choice(self):
        app = self.app
        choices = []
        def visit(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == 'TCombobox' and str(child.cget('textvariable')) == str(app.rank_top_limit_var):
                    choices.append(child)
                visit(child)
        visit(app)
        self.assertEqual(len(choices), 2)
        app.rank_top_limit_var.set('10000')
        for choice in choices:
            self.assertEqual(str(choice.cget('state')), 'readonly')
            self.assertEqual(tuple(map(str, choice.cget('values'))), ('5000', '10000'))
            self.assertEqual(choice.get(), '10000')

    def test_csv_export_passes_selected_count_without_launching_backtest(self):
        app = self.app
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / '全部回测结果.csv'
            source.write_text('fixture', 'utf-8')
            app.rank_top_limit_var.set('10000')
            with mock.patch.object(self.ui.filedialog, 'askopenfilename', return_value=str(source)), \
                 mock.patch.object(app, '_launch_process', return_value=False) as launch:
                app.start_legacy_export()
            command, output, kind = launch.call_args.args
            self.assertEqual(kind, 'legacy')
            self.assertTrue(any(str(item).endswith('worst_export.py') for item in command))
            settings = json.loads((output / '排行榜设置.json').read_text('utf-8'))
            self.assertEqual(settings['最优名额'], 10000)


if __name__ == '__main__':
    unittest.main()
