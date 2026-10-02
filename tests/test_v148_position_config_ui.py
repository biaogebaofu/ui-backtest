"""开仓位置作为独立方案维度；保存加载只写入测试内存。"""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest import mock

from entry_position import POSITION_FILTERS
from selection_config import 全选配置, 规范化配置, 配置统计, 配置签名


class PositionConfigTests(unittest.TestCase):
    def test_new_and_old_config_default_to_off(self):
        raw = 全选配置()
        self.assertEqual(raw["开仓位置过滤"], ["OFF"])
        self.assertEqual(raw["版本"], 20)
        old = copy.deepcopy(raw)
        old["版本"] = 19
        old.pop("开仓位置过滤")
        expected = 规范化配置(raw)
        self.assertEqual(规范化配置(old), expected)

    def test_position_choices_multiply_combination_counts_once(self):
        raw = 全选配置()
        raw["开仓条件"] = {tf: [c for c in codes if c < 264]
                         for tf, codes in raw["开仓条件"].items()}
        baseline = 配置统计(raw)
        self.assertEqual(baseline["开仓位置过滤档数"], 1)
        self.assertEqual(baseline["基础入场组合数"], 2 * 248 * 252 * 261 * 262 * 264)
        raw["开仓位置过滤"] = list(POSITION_FILTERS)
        counts = 配置统计(raw)
        self.assertEqual(len(POSITION_FILTERS), 16)
        self.assertEqual(len([code for code in POSITION_FILTERS if not code.startswith("RESEARCH_")]), 13)
        self.assertEqual(counts["开仓位置过滤档数"], len(POSITION_FILTERS))
        for key in ("入场组合数", "不含仓位完整组合数", "包含仓位完整组合数"):
            self.assertEqual(counts[key], baseline[key] * len(POSITION_FILTERS))
        for key in ("基础入场组合数", "止损组合数", "止盈方案数", "等待时间档数", "仓位档数"):
            self.assertEqual(counts[key], baseline[key])

    def test_combined_filter_is_one_choice_and_changes_signature(self):
        raw = 全选配置()
        baseline = 配置签名(raw)
        base_counts = 配置统计(raw)
        raw["开仓位置过滤"] = ["EMA5_ATR100_R60_E20"]
        self.assertNotEqual(配置签名(raw), baseline)
        self.assertEqual(配置统计(raw), base_counts)

    def test_normalization_preserves_order_deduplicates_and_rejects_invalid(self):
        raw = 全选配置()
        raw["开仓位置过滤"] = ["RANGE60_EDGE20", "OFF", "RANGE60_EDGE20", "EMA5_ATR075"]
        self.assertEqual(规范化配置(raw)["开仓位置过滤"], ["RANGE60_EDGE20", "OFF", "EMA5_ATR075"])
        for invalid in ([], None, "OFF", ["UNKNOWN"], ["OFF", "UNKNOWN"], {"OFF"}, ("OFF",)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                raw["开仓位置过滤"] = invalid
                规范化配置(raw)


class PositionUiTests(unittest.TestCase):
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
        cls.stack.enter_context(mock.patch.object(ui.messagebox, "showerror"))
        cls.app = ui.App()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.stack.close()

    def setUp(self):
        self.write.reset_mock()
        self.app._settings_restore_error = False
        self.app.selection_panel.clear_data_capabilities()
        self.app.selection_panel.apply_config(全选配置())
        self.app.apply_execution_settings(全选配置())

    def test_catalog_controls_and_comparison_actions_only_change_position(self):
        panel = self.app.selection_panel
        self.assertEqual(list(panel.position_filter_widgets), list(POSITION_FILTERS))
        before = panel.get_config()
        panel.position_compare_button.invoke()
        after = panel.get_config()
        self.assertEqual(after["开仓位置过滤"], list(POSITION_FILTERS))
        self.assertEqual({k: v for k, v in after.items() if k != "开仓位置过滤"},
                         {k: v for k, v in before.items() if k != "开仓位置过滤"})
        self.assertIn(f"位置 {len(POSITION_FILTERS)}档", panel.summary_var.get())
        self.assertIn(f"位置 {len(POSITION_FILTERS)}档", self.app.selection_summary_var.get())
        self.assertEqual(配置统计(after)["入场组合数"], 配置统计(before)["入场组合数"] * len(POSITION_FILTERS))
        panel.position_baseline_button.invoke()
        self.assertEqual(panel.get_config(), before)

    def test_save_load_preserves_position_order_fourth_batch_cost_and_wait(self):
        app = self.app
        raw = 全选配置()
        raw["开仓位置过滤"] = ["EMA5_ATR100_R60_E20", "OFF", "EMA5_ATR075"]
        raw["开仓条件"]["1m"] = list(range(144, 264))
        app.selection_panel.apply_config(raw)
        app.set_cost_modes("FEE")
        app.min_reentry_minutes_var.set("17")
        before = app.current_selection()
        self.assertTrue(app.save_user_settings())
        payload = copy.deepcopy(self.write.call_args.args[1])
        app.selection_panel.position_baseline_button.invoke()
        path = mock.Mock()
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps(payload, ensure_ascii=False)
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(app)
        self.assertEqual(app.current_selection(), before)
        self.assertEqual(app.current_selection()["开仓位置过滤"], raw["开仓位置过滤"])

    def test_empty_selection_is_rejected_and_existing_rounds_keep_off(self):
        panel = self.app.selection_panel
        panel._set_position_filters([])
        with self.assertRaises(ValueError):
            panel.get_config()
        self.assertIn("当前选择不完整", panel.summary_var.get())
        panel.apply_round("基线")
        self.assertEqual(panel.get_config()["开仓位置过滤"], ["OFF"])
        self.assertEqual(配置统计(panel.get_config())["包含仓位完整组合数"], 1)
        panel.apply_round("A")
        self.assertEqual(panel.detect_round(), "A")
        panel.position_compare_button.invoke()
        self.assertIsNone(panel.detect_round())


if __name__ == "__main__":
    unittest.main()
