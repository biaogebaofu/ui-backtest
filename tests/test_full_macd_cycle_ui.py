"""Full-cycle choices survive UI, settings and fingerprint-list presentation."""
import copy
import json
from contextlib import ExitStack
import tkinter as tk
import unittest
from unittest import mock

import ui
from selection_config import 入场触发口径选项, 规范化配置, 配置签名, 配置统计
from test_v149_fingerprint_ui import restored_payload
from test_v151_cycle_config import small_config


MODES = ("MACD_FULL_RED", "MACD_FULL_GREEN")


class FullCycleConfigTests(unittest.TestCase):
    def test_labels_and_single_item_lists_round_trip_without_other_changes(self):
        for mode in MODES:
            expected = small_config(mode)
            for selected in (mode, [mode], 入场触发口径选项[mode]):
                self.assertEqual(规范化配置(small_config(selected)), expected)

    def test_each_anchor_has_distinct_identity_and_multi_select_is_independent(self):
        choices = ("LIVE_01", "TF_EVENT", "MACD_CYCLE", *MODES)
        self.assertEqual(len({配置签名(small_config(m)) for m in choices}), len(choices))
        one = 配置统计(small_config(MODES[0]))
        both = 配置统计(small_config(list(reversed(MODES))))
        self.assertEqual(both["入场口径档数"], 2)
        self.assertEqual(both["基础入场组合数"], one["基础入场组合数"])
        for key in ("入场组合数", "不含仓位完整组合数", "包含仓位完整组合数"):
            self.assertEqual(both[key], one[key] * 2)
        self.assertEqual(规范化配置(small_config(list(reversed(MODES))))["入场触发口径"], list(MODES))


class FullCycleUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        original_init = tk.Tk.__init__

        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()

        cls.stack.enter_context(mock.patch.object(tk.Tk, "__init__", hidden_init))
        cls.original_load = ui.App.load_user_settings
        cls.stack.enter_context(mock.patch.object(ui.App, "load_user_settings"))
        cls.stack.enter_context(mock.patch.object(ui.App, "detect_gpu"))
        cls.write = cls.stack.enter_context(mock.patch.object(ui, "atomic_json"))
        cls.stack.enter_context(mock.patch.object(ui.messagebox, "showinfo"))
        cls.stack.enter_context(mock.patch.object(ui.messagebox, "showerror"))
        cls.app = ui.App()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.stack.close()

    def setUp(self):
        self.app.proc = None
        self.app._active_job_id = None
        self.app._closing = False
        self.app._settings_restore_error = False
        self.app.apply_fingerprint_restoration(restored_payload())
        self.write.reset_mock()

    def test_both_choices_are_visible_checkbuttons_and_do_not_start_a_run(self):
        panel = self.app.selection_panel
        before = self.app.current_selection()
        with mock.patch.object(self.app, "_launch_process") as launch:
            panel.set_entry_modes(MODES[0])
            panel.entry_mode_widgets[MODES[1]].invoke()
        expected = copy.deepcopy(before)
        expected["入场触发口径"] = list(MODES)
        self.assertEqual(self.app.current_selection(), expected)
        for mode in MODES:
            widget = panel.entry_mode_widgets[mode]
            self.assertEqual(widget.winfo_class(), "TCheckbutton")
            self.assertTrue(widget.instate(["selected"]))
            self.assertIn("完整", widget.cget("text"))
        self.assertIn("2 种", panel.entry_mode_var.get())
        launch.assert_not_called()
        self.write.assert_not_called()

    def test_settings_restore_preserves_each_anchor_and_multiple_choices(self):
        for selected in (*MODES, list(MODES)):
            self.app.selection_panel.set_entry_modes(selected)
            before = self.app.current_selection()
            self.app.save_user_settings()
            payload = copy.deepcopy(self.write.call_args.args[1])
            self.assertEqual(payload["组合选择"]["入场触发口径"], selected)
            path = mock.Mock()
            path.is_file.return_value = True
            path.read_text.return_value = json.dumps(payload, ensure_ascii=False)
            self.app.selection_panel.set_entry_modes("LIVE_01")
            with mock.patch.object(ui, "用户设置文件", path):
                type(self).original_load(self.app)
            self.assertEqual(self.app.current_selection(), before)

    def test_fingerprint_restore_selects_only_its_exact_anchor(self):
        for mode in MODES:
            payload = restored_payload()
            payload["selection"]["入场触发口径"] = mode
            with mock.patch.object(self.app, "_launch_process") as launch:
                self.app.apply_fingerprint_restoration(payload)
            self.assertEqual(self.app.current_selection(), payload["selection"])
            self.assertEqual(self.app.selection_panel.get_entry_modes(), [mode])
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
