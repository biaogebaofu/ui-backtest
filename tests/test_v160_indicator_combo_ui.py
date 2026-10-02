"""隔离的窗口回归：组合设置、实际数量、保存恢复与关闭兼容。"""
import copy
import json
import unittest
from unittest import mock

import test_v152_fingerprint_queue_ui as fixture
from indicator_combo_picker import parse_sizes
from selection_config import 配置统计


class IndicatorCombinationUiTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FingerprintQueueUiTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FingerprintQueueUiTests.tearDownClass.__func__)
    setUp = fixture.FingerprintQueueUiTests.setUp
    tearDown = fixture.FingerprintQueueUiTests.tearDown
    snapshot = fixture.FingerprintQueueUiTests.snapshot

    def test_same_button_at_all_five_timeframes_and_exit_stages(self):
        panel = self.app.selection_panel
        self.assertEqual(set(panel.combo_buttons), {
            ("开仓", tf) for tf in ("4h", "1h", "15m", "5m", "1m")
        } | {("止损",), ("止盈",)})
        for button in panel.combo_buttons.values():
            self.assertEqual(button.cget("text"), "指标组合")

    def test_each_button_opens_the_correct_pool_and_logic(self):
        panel = self.app.selection_panel
        for path in panel.combo_buttons:
            with mock.patch("selection_panel.IndicatorComboPicker") as picker:
                panel.combo_buttons[path].invoke()
            args = picker.call_args.args
            self.assertEqual(args[2]["逻辑"], "AND" if path[0] == "开仓" else "OR")
            self.assertEqual(args[3:], (*panel._combo_pool(path), args[-1]))

    def test_multiple_sizes_count_and_persist_in_user_settings(self):
        app = self.app
        panel = app.selection_panel
        config = panel.get_config()
        config["开仓条件"]["1m"] = [0, 1, 2, 3, 4]
        panel.apply_config(config)
        panel._apply_combo_spec(("开仓", "1m"), {
            "启用": True, "组合数量": [2, 3], "保留单项": False, "逻辑": "AND"})
        chosen = app.current_selection()
        self.assertEqual(配置统计(chosen)["入场组合数"], 10)
        self.assertIn("2/3 项组合", panel.combo_status_vars[("开仓", "1m")].get())
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        self.assertEqual(saved["组合选择"]["指标组合"], chosen["指标组合"])
        panel.apply_config(config)
        path = mock.Mock(is_file=mock.Mock(return_value=True),
                         read_text=mock.Mock(return_value=json.dumps(saved)))
        with mock.patch.object(self.ui, "用户设置文件", path), mock.patch.object(app, "_launch_process") as launch:
            type(self).original_load(app)
        self.assertEqual(app.current_selection()["指标组合"], chosen["指标组合"])
        self.assertEqual(配置统计(app.current_selection())["入场组合数"], 10)
        launch.assert_not_called()

    def test_cancel_is_inert_and_apply_records_exact_sizes(self):
        panel = self.app.selection_panel
        config = panel.get_config()
        config["开仓条件"]["1m"] = [1, 2, 3, 4]
        panel.apply_config(config)
        before = panel.get_config()
        picker = panel._show_combo_picker(("开仓", "1m"), "1m 开仓")
        picker.enabled.set(True)
        picker.size_vars[3].set(True)
        picker.destroy()
        self.assertEqual(panel.get_config(), before)
        picker = panel._show_combo_picker(("开仓", "1m"), "1m 开仓")
        picker.enabled.set(True)
        picker.singles.set(False)
        picker.size_vars[4].set(True)
        picker.apply()
        chosen = panel.get_config()["指标组合"]["开仓"]["1m"]
        self.assertEqual(chosen["组合数量"], [2, 4])
        self.assertEqual(配置统计(panel.get_config())["入场组合数"], 7)

    def test_off_and_partial_do_not_enter_combination_pool(self):
        panel = self.app.selection_panel
        pool, excluded = panel._combo_pool(("止盈",))
        expected = {tp.编号 for tp in panel.all_tps if tp.类别 in ("分批止盈", "不使用止盈")}
        self.assertEqual(excluded, expected)
        self.assertIn(0, panel._combo_pool(("开仓", "1m"))[1])
        self.assertEqual(panel._combo_pool(("止损",))[1], {"OFF"})

    def test_switch_off_restores_original_configuration(self):
        panel = self.app.selection_panel
        config = panel.get_config()
        config["开仓条件"]["1m"] = [1, 2, 3]
        panel.apply_config(config)
        before = panel.get_config()
        spec = {"启用": True, "组合数量": [2], "保留单项": True, "逻辑": "AND"}
        panel._apply_combo_spec(("开仓", "1m"), spec)
        self.assertIn("指标组合", panel.get_config())
        panel._apply_combo_spec(("开仓", "1m"), dict(spec, 启用=False))
        self.assertEqual(panel.get_config(), before)

    def test_large_space_storage_estimate_does_not_overflow_float(self):
        counts = 配置统计(self.app.current_selection())
        counts["不含仓位完整组合数"] = 10**500
        counts["包含仓位完整组合数"] = 10**500
        self.app.update_selection_summary(counts, "")
        self.assertIn("E+", self.app.storage_summary_var.get())


class CombinationSizeInputTests(unittest.TestCase):
    def test_more_than_four_indicators_and_duplicates(self):
        self.assertEqual(parse_sizes([2, 3], "7,8，10 8"), [2, 3, 7, 8, 10])

    def test_invalid_sizes_are_rejected(self):
        for value in ("0", "1", "-3", "2.5", "two"):
            with self.assertRaises(ValueError):
                parse_sizes([2], value)


if __name__ == "__main__":
    unittest.main()
