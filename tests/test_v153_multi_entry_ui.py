"""入场规则可多选但各自回测；仅新建隐藏Tk，不访问用户配置或运行进程。"""
import copy
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

from entry_position import POSITION_FILTERS
from selection_config import 入场触发口径选项, 配置统计, 配置签名
from selection_panel import 组合选择面板
from test_v149_fingerprint_ui import single_selection


class MultiEntryPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.summary = tk.StringVar(value=入场触发口径选项["LIVE_01"])
        cls.changed = mock.Mock()
        cls.panel = 组合选择面板(cls.root, cls.changed, entry_mode_var=cls.summary)
        cls.panel.pack(fill="both", expand=True)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.panel.apply_config(single_selection())
        self.changed.reset_mock()

    def test_three_independent_checks_share_only_display_summary(self):
        panel = self.panel
        self.assertIs(panel.entry_mode_var, self.summary)
        self.assertEqual(list(panel.entry_mode_vars), list(入场触发口径选项))
        self.assertEqual(len({str(var) for var in panel.entry_mode_vars.values()}), len(入场触发口径选项))
        for code, widget in panel.entry_mode_widgets.items():
            self.assertIsInstance(widget, ttk.Checkbutton)
            self.assertEqual(str(widget.cget("variable")), str(panel.entry_mode_vars[code]))
        self.assertEqual(list(panel.position_filter_widgets), list(POSITION_FILTERS))
        self.assertFalse(set(panel.entry_mode_widgets) & set(panel.position_filter_widgets))
        self.assertIn("分别回测", panel.entry_trigger_box.cget("text"))

    def test_click_adds_modes_without_changing_other_parameters(self):
        panel = self.panel
        before = panel.get_config()
        panel.entry_mode_widgets["MACD_CYCLE"].invoke()
        after = panel.get_config()
        expected = copy.deepcopy(before)
        expected["入场触发口径"] = ["TF_EVENT", "MACD_CYCLE"]
        self.assertEqual(after, expected)
        self.assertEqual(panel.get_entry_modes(), expected["入场触发口径"])
        self.assertIn("2 种", self.summary.get())
        self.assertIn("TF_EVENT", self.summary.get())
        self.assertIn("MACD_CYCLE", self.summary.get())

    def test_zero_selection_is_visible_error_not_default_fallback(self):
        panel = self.panel
        panel.entry_mode_widgets["TF_EVENT"].invoke()
        self.assertFalse(any(var.get() for var in panel.entry_mode_vars.values()))
        for getter in (panel.get_config, panel.get_entry_modes):
            with self.assertRaisesRegex(ValueError, "至少选择一种"):
                getter()
        self.assertEqual(self.summary.get(), "未选择入场规则")
        self.assertIn("至少选择一种", panel.summary_var.get())
        self.assertIsNone(self.changed.call_args.args[0])

    def test_invalid_setter_and_apply_are_atomic(self):
        panel = self.panel
        before, summary = panel.get_config(), self.summary.get()
        for value in ([], ["LIVE_01", "unknown"], "unknown", None, {"LIVE_01": True}):
            with self.assertRaises(ValueError):
                panel.set_entry_modes(value)
            self.assertEqual(panel.get_config(), before)
            self.assertEqual(self.summary.get(), summary)
        bad = copy.deepcopy(before)
        bad["入场触发口径"] = []
        bad["仓位倍数"] = [100.0]
        with self.assertRaises(ValueError):
            panel.apply_config(bad)
        self.assertEqual(panel.get_config(), before)
        self.changed.assert_not_called()

    def test_single_codes_old_labels_and_one_item_lists_keep_scalar(self):
        panel = self.panel
        values = [(code, value) for code, label in 入场触发口径选项.items() if code != "F5_EVENT"
                  for value in (code, label, [code])]
        values += [("MACD_CYCLE", "MACD柱hist零轴同侧一轮最多一次"),
                   ("LIVE_01", "每个1m指标连续升/降段一次（非零轴整轮/金叉死叉周期）")]
        for code, value in values:
            panel.set_entry_modes(value)
            self.assertEqual(panel.get_config()["入场触发口径"], code)
            self.assertEqual(panel.get_entry_modes(), [code])
            self.assertEqual(self.summary.get(), 入场触发口径选项[code])

    def test_multiple_config_round_trip_and_canonical_signature(self):
        panel = self.panel
        raw = single_selection()
        raw["入场触发口径"] = ["MACD_CYCLE", "LIVE_01", "MACD_CYCLE"]
        panel.apply_config(raw)
        config = panel.get_config()
        self.assertEqual(config["入场触发口径"], ["LIVE_01", "MACD_CYCLE"])
        before = 配置签名(config)
        panel.set_entry_modes("LIVE_01")
        panel.entry_mode_widgets["MACD_CYCLE"].invoke()
        self.assertEqual(配置签名(panel.get_config()), before)
        panel.apply_config(config)
        self.assertEqual(panel.get_config(), config)

    def test_three_modes_times_sixteen_positions_counts_separate_results(self):
        panel = self.panel
        base = 配置统计(panel.get_config())
        self.assertEqual(base["包含仓位完整组合数"], 1)
        panel.set_entry_modes(["LIVE_01", "TF_EVENT", "MACD_CYCLE"])
        panel.position_compare_button.invoke()
        config = panel.get_config()
        counts = 配置统计(config)
        self.assertEqual(counts["入场口径档数"], 3)
        self.assertEqual(counts["基础入场组合数"], base["基础入场组合数"])
        self.assertEqual(counts["开仓位置过滤档数"], 16)
        self.assertEqual(counts["包含仓位完整组合数"], 48)
        self.assertEqual(counts["不含仓位完整组合数"], 48)
        self.assertIn("口径 3种", panel.summary_var.get())
        self.assertIn("位置 16档", panel.summary_var.get())
        self.assertIn("48", panel.pages.tab(0, "text"))
        self.assertEqual(self.changed.call_args.args[0], counts)

    def test_multimode_checks_are_reachable_at_minimum_and_default_width(self):
        panel, root = self.panel, self.root
        root.attributes("-alpha", 0)
        if root.tk.call("tk", "windowingsystem") == "win32":
            root.attributes("-toolwindow", True)
        try:
            panel.set_entry_modes(list(入场触发口径选项))
            for width, height in ((1080, 720), (1280, 850)):
                root.geometry(f"{width}x{height}+30000+30000")
                root.deiconify()
                panel.show_entry_constraints()
                root.update()
                canvas = panel.entry_canvas
                for widget in panel.entry_mode_widgets.values():
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                         canvas.winfo_rootx() + canvas.winfo_width())
                    bounds = canvas.bbox("all")
                    content_y = canvas.canvasy(0) + widget.winfo_rooty() - canvas.winfo_rooty()
                    canvas.yview_moveto(content_y / bounds[3])
                    root.update()
                    top = widget.winfo_rooty() - canvas.winfo_rooty()
                    self.assertGreaterEqual(top, -2)
                    self.assertLessEqual(top + widget.winfo_height(), canvas.winfo_height())
        finally:
            root.withdraw()


if __name__ == "__main__":
    unittest.main()
