"""三种入场规则与16档位置过滤独立组合；保留旧单项回填，隐藏Tk不写配置。"""
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest import mock

import test_v149_fingerprint_ui as fingerprint_tests
from entry_position import POSITION_FILTERS
from selection_config import 配置统计, 配置签名


class CycleModeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import ui
        cls.ui = ui
        cls.original_load = ui.App.load_user_settings
        cls.stack = ExitStack()
        original_init = tk.Tk.__init__
        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()
        cls.stack.enter_context(mock.patch.object(tk.Tk, "__init__", hidden_init))
        cls.stack.enter_context(mock.patch.object(ui.App, "load_user_settings"))
        cls.stack.enter_context(mock.patch.object(ui.App, "detect_gpu"))
        cls.write = cls.stack.enter_context(mock.patch.object(ui, "atomic_json"))
        cls.info = cls.stack.enter_context(mock.patch.object(ui.messagebox, "showinfo"))
        cls.stack.enter_context(mock.patch.object(ui.messagebox, "showerror"))
        cls.app = ui.App()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.stack.close()

    def setUp(self):
        app = self.app
        app.proc = None
        app._active_job_id = None
        app._closing = False
        app._settings_restore_error = False
        app.apply_fingerprint_restoration(fingerprint_tests.restored_payload())
        self.write.reset_mock()
        self.info.reset_mock()

    def load_memory(self, config):
        path = mock.Mock()
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps({"组合选择": config}, ensure_ascii=False)
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(self.app)

    @staticmethod
    def walk(widget):
        for child in widget.winfo_children():
            yield child
            yield from CycleModeUiTests.walk(child)

    def test_shared_summary_three_checks_and_separate_position_group(self):
        app, panel = self.app, self.app.selection_panel
        self.assertIs(panel.entry_mode_var, app.entry_mode_var)
        self.assertEqual(set(panel.entry_mode_widgets), {"LIVE_01", "TF_EVENT", "MACD_CYCLE", "MACD_FULL_RED", "MACD_FULL_GREEN", "F5_EVENT"})
        self.assertTrue(all(widget.winfo_class() == "TCheckbutton" for widget in panel.entry_mode_widgets.values()))
        self.assertEqual(list(panel.position_filter_widgets), list(POSITION_FILTERS))
        self.assertNotIn("MACD_CYCLE", panel.position_filter_widgets)
        self.assertIs(panel.entry_trigger_box.master, panel.position_box.master)
        self.assertIsNot(panel.entry_trigger_box, panel.position_box)
        self.assertEqual(str(app.entry_mode_summary.cget("textvariable")), str(app.entry_mode_var))
        self.assertFalse(any(w.winfo_class() == "TCombobox" and "textvariable" in w.keys()
                             and str(w.cget("textvariable")) == str(app.entry_mode_var)
                             for w in self.walk(app.run_tab)))

    def test_mode_only_switch_marks_new_task_and_preserves_all_other_parameters(self):
        app, panel = self.app, self.app.selection_panel
        before = app.current_selection()
        app.current_output_dir = Path.cwd() / "_synthetic_fixture" / "previous-result-not-created"
        with mock.patch.object(app, "start_run") as start, mock.patch.object(app, "start_candidate_export") as export:
            panel.entry_mode_widgets["MACD_CYCLE"].invoke()
        after = app.current_selection()
        expected = copy.deepcopy(before)
        expected["入场触发口径"] = ["TF_EVENT", "MACD_CYCLE"]
        self.assertEqual(after, expected)
        self.assertIsNone(app.current_output_dir)
        self.assertEqual(after["开仓位置过滤"], ["EMA5_ATR100_R60_E20"])
        self.assertEqual(str(app.entry_mode_summary.cget("text")), app.entry_mode_var.get())
        start.assert_not_called(); export.assert_not_called(); self.write.assert_not_called()

    def test_single_mode_configs_remain_scalar_with_no_extra_combinations(self):
        app, panel = self.app, self.app.selection_panel
        panel.position_compare_button.invoke()
        counts = 配置统计(app.current_selection())
        self.assertEqual(counts["开仓位置过滤档数"], 16)
        signatures = set()
        for code in ("LIVE_01", "TF_EVENT", "MACD_CYCLE"):
            panel.set_entry_modes(code)
            config = app.current_selection()
            self.assertEqual(config["入场触发口径"], code)
            self.assertEqual(配置统计(config), counts)
            self.assertEqual(config["开仓位置过滤"], list(POSITION_FILTERS))
            self.assertEqual(sum(w.instate(["selected"]) for w in panel.entry_mode_widgets.values()), 1)
            signatures.add(配置签名(config))
        self.assertEqual(len(signatures), 3)
        panel.position_baseline_button.invoke()
        self.assertEqual(app.current_selection()["入场触发口径"], "MACD_CYCLE")

    def test_save_load_and_known_old_labels_keep_mode_and_position(self):
        app = self.app
        for code in ("LIVE_01", "TF_EVENT", "MACD_CYCLE"):
            app.selection_panel.set_entry_modes(code)
            before = app.current_selection()
            app.save_user_settings()
            saved = copy.deepcopy(self.write.call_args.args[1]["组合选择"])
            self.assertEqual(saved["入场触发口径"], code)
            self.load_memory(saved)
            self.assertEqual(app.current_selection(), before)
            saved["入场触发口径"] = self.ui.原入场触发口径选项[code]
            self.load_memory(saved)
            self.assertEqual(app.current_selection(), before)
        before = app.current_selection()
        with self.assertRaises(ValueError):
            app.selection_panel.set_entry_modes("unknown-mode")
        self.assertEqual(app.current_selection(), before)

    def test_cycle_fingerprint_restores_full_configuration_without_start(self):
        app = self.app
        payload = fingerprint_tests.restored_payload()
        payload["selection"]["入场触发口径"] = "MACD_CYCLE"
        with mock.patch.object(app, "_launch_process") as launch:
            app.apply_fingerprint_restoration(payload)
        fingerprint_tests.FingerprintUiTests.assert_nested_equal(self, app.current_selection(), payload["selection"])
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 1)
        self.assertTrue(app.selection_panel.entry_mode_widgets["MACD_CYCLE"].instate(["selected"]))
        launch.assert_not_called(); self.write.assert_not_called()

    assert_nested_equal = fingerprint_tests.FingerprintUiTests.assert_nested_equal

    def test_panel_apply_config_preserves_cycle_without_app_side_assignment(self):
        panel = self.app.selection_panel
        raw = fingerprint_tests.single_selection()
        raw["入场触发口径"] = "MACD_CYCLE"
        raw["开仓位置过滤"] = ["EMA5_ATR075", "OFF"]
        panel.apply_config(raw)
        self.assertEqual(panel.get_config()["入场触发口径"], "MACD_CYCLE")
        self.assertEqual(panel.get_config()["开仓位置过滤"], raw["开仓位置过滤"])
        self.assertEqual(self.app.current_selection()["入场触发口径"], "MACD_CYCLE")

    def test_run_summary_jump_and_running_guard(self):
        app = self.app
        app.entry_rules_jump_button.invoke()
        self.assertEqual(app.notebook.select(), str(app.select_tab))
        self.assertEqual(app.selection_panel.pages.select(), str(app.selection_panel.entry_tab))
        app.notebook.select(app.run_tab)
        app._active_job_id = 7
        app.entry_rules_jump_button.invoke()
        self.assertEqual(app.notebook.select(), str(app.run_tab))
        self.info.assert_called_once()
        app._active_job_id = None

    def test_three_modes_and_sixteen_positions_are_reachable_at_minimum_width(self):
        app, panel = self.app, self.app.selection_panel
        app.attributes("-alpha", 0)
        app.attributes("-toolwindow", True)
        try:
            for width, height in ((1080, 720), (1280, 850)):
                app.geometry(f"{width}x{height}+30000+30000")
                app.deiconify()
                app.show_entry_rules()
                app.update()
                canvas = panel.entry_canvas
                for widget in [*panel.entry_mode_widgets.values(), *panel.position_filter_widgets.values(),
                               panel.position_baseline_button, panel.position_compare_button]:
                    right = widget.winfo_rootx() + widget.winfo_width() - canvas.winfo_rootx()
                    self.assertLessEqual(right, canvas.winfo_width())
                    bounds = canvas.bbox("all")
                    content_y = canvas.canvasy(0) + widget.winfo_rooty() - canvas.winfo_rooty()
                    canvas.yview_moveto(content_y / bounds[3])
                    app.update()
                    top = widget.winfo_rooty() - canvas.winfo_rooty()
                    self.assertGreaterEqual(top, -2)
                    self.assertLessEqual(top + widget.winfo_height(), canvas.winfo_height())
                app.notebook.select(app.run_tab)
                app.update()
                for widget in (app.entry_mode_summary, app.entry_rules_jump_button):
                    right = widget.winfo_rootx() + widget.winfo_width() - app.run_tab.winfo_rootx()
                    self.assertLessEqual(right, app.run_tab.winfo_width())
        finally:
            app.withdraw()


if __name__ == "__main__":
    unittest.main()
