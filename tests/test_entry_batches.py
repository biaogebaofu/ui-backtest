import unittest
from contextlib import ExitStack, contextmanager
from itertools import product, islice
from unittest import mock
from backtest_worker import 开仓批次


class EntryBatchTests(unittest.TestCase):
    @contextmanager
    def _hidden_app(self):
        import tkinter as tk
        from ui import App
        original_init = tk.Tk.__init__

        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(tk.Tk, '__init__', hidden_init))
            stack.enter_context(mock.patch.object(App, 'load_user_settings'))
            stack.enter_context(mock.patch.object(App, 'save_user_settings'))
            stack.enter_context(mock.patch.object(App, 'detect_gpu'))
            yield App()

    def test_all_combinations_once_with_bounded_batches(self):
        options = [[0, 5], [0], [0], [0, 6], [1, 7, 9]]
        batches = list(开仓批次(['hist', 'dif'], options, 5))
        self.assertTrue(all(1 <= len(cases) <= 5 for _, cases in batches))
        actual = [(f, c) for f, cases in batches for c in cases]
        expected = [(f, c) for f in ('hist', 'dif') for c in product(*options)]
        self.assertEqual(actual, expected)

    def test_huge_space_is_lazy(self):
        first = next(开仓批次(['hist'], [range(18)] * 5, 8))
        self.assertEqual(first[1], tuple(islice(product(*([range(18)] * 5)), 8)))

    def test_second_round_master_checkbox_is_scoped(self):
        with self._hidden_app() as app:
            try:
                app.withdraw()
                panel = app.selection_panel
                original = {tf: {c: v.get() for c, v in vs.items()} for tf, vs in panel.case_vars.items()}
                for enabled in (False, True, False):
                    panel.entry_all_vars['4h'].set(enabled)
                    panel._set_second_round('4h')
                    self.assertTrue(all(v.get() == enabled for c, v in panel.case_vars['4h'].items() if 5 <= c < 18))
                    self.assertEqual({c: v.get() for c, v in panel.case_vars['4h'].items() if c >= 18},
                                     {c: v for c, v in original['4h'].items() if c >= 18})
                    self.assertEqual({c: v.get() for c, v in panel.case_vars['4h'].items() if c < 5},
                                     {c: v for c, v in original['4h'].items() if c < 5})
                    self.assertEqual({c: v.get() for c, v in panel.case_vars['1h'].items()}, original['1h'])
                before_fold = {c: v.get() for c, v in panel.case_vars['4h'].items()}
                panel.entry_fold_vars['4h'].set(not panel.entry_fold_vars['4h'].get())
                self.assertEqual({c: v.get() for c, v in panel.case_vars['4h'].items()}, before_fold)
                # 第三批各周期开放，有独立总开关，不会误改第二批。
                self.assertIn(18, panel.case_vars['4h'])
                second_before = {c: panel.case_vars['1m'][c].get() for c in range(5, 18)}
                panel.third_entry_all_vars['1m'].set(True)
                panel._set_third_round('1m')
                self.assertTrue(all(panel.case_vars['1m'][c].get() for c in range(18, 128)))
                self.assertEqual({c: panel.case_vars['1m'][c].get() for c in range(5, 18)}, second_before)
            finally:
                for timer in app.tk.call('after', 'info'):
                    app.after_cancel(timer)
                app.destroy()
    def test_third_round_kline_source_preset_is_scoped(self):
        from extended_rules import KLINE_ONLY_THIRD_CODES, MICROSTRUCTURE_THIRD_CODES
        with self._hidden_app() as app:
            try:
                app.withdraw()
                panel = app.selection_panel
                panel._set_third_source('1m', 'kline')
                self.assertTrue(all(panel.case_vars['1m'][c].get() for c in KLINE_ONLY_THIRD_CODES))
                self.assertTrue(all(not panel.case_vars['1m'][c].get() for c in MICROSTRUCTURE_THIRD_CODES))
                panel._set_third_source('1m', 'micro')
                self.assertTrue(all(panel.case_vars['1m'][c].get() for c in MICROSTRUCTURE_THIRD_CODES))
                self.assertTrue(all(not panel.case_vars['1m'][c].get() for c in KLINE_ONLY_THIRD_CODES))
            finally:
                for timer in app.tk.call('after', 'info'):
                    app.after_cancel(timer)
                app.destroy()
