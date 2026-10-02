"""金叉死叉标签保留同一模式；第五轮另有独立事件口径。"""
import copy
import unittest
from contextlib import ExitStack
from unittest import mock

import selection_config as config
from entry_position import POSITION_FILTERS


OLD_LABEL = "MACD柱hist零轴同侧一轮最多一次"
NEW_LABEL = "DIF/DEA金叉死叉：每次红绿换色后最多开一次（1m）"


class MacdLabelConfigTests(unittest.TestCase):
    def test_old_and_new_names_keep_the_same_code_signature_and_dimensions(self):
        baseline = config.全选配置()
        baseline["入场触发口径"] = "MACD_CYCLE"
        self.assertEqual(config.入场触发口径选项["MACD_CYCLE"], NEW_LABEL)
        self.assertEqual(set(config.入场触发口径选项), {"LIVE_01", "TF_EVENT", "MACD_CYCLE", "MACD_FULL_RED", "MACD_FULL_GREEN", "F5_EVENT"})
        self.assertEqual(config.默认入场触发口径, "LIVE_01")
        self.assertEqual(len(POSITION_FILTERS), 16)
        for label in (OLD_LABEL, NEW_LABEL, "MACD_CYCLE"):
            raw = copy.deepcopy(baseline)
            raw["入场触发口径"] = label
            self.assertEqual(config.规范化配置(raw), config.规范化配置(baseline))
            self.assertEqual(config.配置签名(raw), config.配置签名(baseline))
            self.assertEqual(config.配置统计(raw), config.配置统计(baseline))

    def test_fingerprint_configuration_accepts_both_historical_and_new_labels(self):
        from fingerprint_lookup import _complete_selection
        for label in (OLD_LABEL, NEW_LABEL):
            raw = config.全选配置()
            raw["入场触发口径"] = label
            self.assertEqual(_complete_selection(raw)["入场触发口径"], "MACD_CYCLE")


class MacdLabelPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        from tkinter import ttk
        from selection_panel import 组合选择面板
        cls.stack = ExitStack()
        original_init = tk.Tk.__init__
        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()
        cls.stack.enter_context(mock.patch.object(tk.Tk, "__init__", hidden_init))
        cls.root = tk.Tk()
        # 18px等效App外边距及外层Notebook边框，1080窗口内开仓画布为975px。
        outer = ttk.Frame(cls.root, padding=18)
        outer.pack(fill="both", expand=True)
        cls.panel = 组合选择面板(outer)
        cls.panel.pack(fill="both", expand=True)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        cls.stack.close()

    def test_old_label_loads_as_new_display_and_description_distinguishes_events(self):
        raw = config.全选配置()
        raw["入场触发口径"] = OLD_LABEL
        self.panel.apply_config(raw)
        self.assertEqual(self.panel.entry_mode_var.get(), NEW_LABEL)
        self.assertEqual(self.panel.get_config()["入场触发口径"], "MACD_CYCLE")
        self.assertTrue(self.panel.entry_mode_widgets["MACD_CYCLE"].instate(["selected"]))
        text = " ".join(str(w.cget("text")) for w in self.panel.entry_trigger_box.winfo_children()
                        if "text" in w.keys())
        for phrase in ("hist=2×(DIF−DEA)", "hist穿0等于两线交叉", "并非DIF本身穿0", "空心/实心变化不另计轮"):
            self.assertIn(phrase, text)

    def test_new_label_fits_minimum_and_default_content_width(self):
        root, panel = self.root, self.panel
        root.attributes("-alpha", 0)
        if root.tk.call("tk", "windowingsystem") == "win32":
            root.attributes("-toolwindow", True)
        try:
            for width in (1080, 1280):
                root.geometry(f"{width}x720+30000+30000")
                root.deiconify()
                panel.show_entry_constraints()
                root.update()
                if width == 1080:
                    self.assertLessEqual(panel.entry_canvas.winfo_width(), 980)
                for widget in panel.entry_mode_widgets.values():
                    right = widget.winfo_rootx() + widget.winfo_width() - panel.entry_canvas.winfo_rootx()
                    self.assertLessEqual(right, panel.entry_canvas.winfo_width())
                    self.assertEqual(widget.winfo_width(), widget.winfo_reqwidth())
        finally:
            root.withdraw()


if __name__ == "__main__":
    unittest.main()
