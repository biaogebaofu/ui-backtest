"""第四批保留、互斥成本与可选等待；UI保存拦截到内存。"""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest import mock

from execution_settings import (DEFAULT_MIN_REENTRY_MINUTES, DEFAULT_SLIPPAGE,
                                effective_fee_rates, effective_slippage,
                                normalize_execution_settings)
from selection_config import 全选配置, 规范化配置
from extended_rules import entry_supported_timeframes


class ExecutionConfigTests(unittest.TestCase):
    def test_public_default_uses_illustrative_slippage(self):
        self.assertAlmostEqual(DEFAULT_SLIPPAGE["开仓"], 0.0001, places=17)
        self.assertAlmostEqual(DEFAULT_SLIPPAGE["平仓"], 0.0001, places=17)
        self.assertEqual(全选配置()["成交偏移"], DEFAULT_SLIPPAGE)

    def test_new_default_and_legacy_migration(self):
        new = 规范化配置(全选配置())
        self.assertEqual(new["成本模式"], "SLIPPAGE")
        self.assertEqual(new["平仓后最小开仓间隔分钟"], DEFAULT_MIN_REENTRY_MINUTES)
        self.assertEqual(effective_fee_rates(new), (0.0, 0.0))
        old = copy.deepcopy(new)
        old["版本"] = 17
        for key in ("成本模式", "手续费", "平仓后最小开仓间隔分钟"):
            old.pop(key)
        restored = 规范化配置(old)
        self.assertEqual(restored["成本模式"], "SLIPPAGE")
        self.assertEqual(restored["平仓后最小开仓间隔分钟"], 0)
        self.assertEqual(restored["成交偏移"], old["成交偏移"])
        self.assertEqual(effective_fee_rates(restored), (0.0, 0.0))

    def test_cost_modes_are_exclusive_and_retain_inactive_values(self):
        raw = 全选配置()
        raw["手续费"].update({"BNB抵扣": True, "返佣比例": 0.2})
        slip_mode = 规范化配置(raw)
        self.assertEqual(effective_slippage(slip_mode), tuple(DEFAULT_SLIPPAGE.values()))
        self.assertEqual(effective_fee_rates(slip_mode), (0.0, 0.0))
        raw["成本模式"] = "FEE"
        fee_mode = 规范化配置(raw)
        self.assertEqual(effective_slippage(fee_mode), (0.0, 0.0))
        for value in effective_fee_rates(fee_mode):
            self.assertAlmostEqual(value, 0.0004 * 0.9 * 0.8)
        self.assertEqual(fee_mode["成交偏移"], slip_mode["成交偏移"])
        self.assertEqual(fee_mode["手续费"], slip_mode["手续费"])

    def test_ambiguous_v18_import_requires_cost_choice(self):
        raw = 全选配置()
        raw["版本"] = 18
        raw.pop("成本模式")
        with self.assertRaisesRegex(ValueError, "明确选择成本模式"):
            规范化配置(raw)
        raw["成本模式"] = "FEE"
        self.assertEqual(规范化配置(raw)["成本模式"], "FEE")

    def test_fee_only_legacy_import_and_zero_fee(self):
        raw = {"手续费": {"开仓费率": 0.0004, "平仓费率": 0.0005},
               "成交偏移": {"开仓": 0.0, "平仓": 0.0}}
        self.assertEqual(normalize_execution_settings(raw)["成本模式"], "FEE")
        self.assertEqual(effective_fee_rates(raw), (0.0004, 0.0005))
        self.assertEqual(effective_fee_rates({"成本模式": "FEE"}), (0.0, 0.0))

    def test_wait_is_optional_integer_and_rejects_invalid_values(self):
        for value in (0, 1, 5, 17, 90, "12"):
            got = normalize_execution_settings({"平仓后最小开仓间隔分钟": value})
            self.assertEqual(got["平仓后最小开仓间隔分钟"], int(value))
        for value in (-1, 0.5, "abc", "nan", float("inf"), True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "整数分钟"):
                normalize_execution_settings({"平仓后最小开仓间隔分钟": value})

    def test_fourth_batch_roundtrip_and_unknown_codes_rejected(self):
        raw = 全选配置()
        raw["开仓条件"]["1m"] = list(range(144, 264))
        self.assertEqual(规范化配置(raw)["开仓条件"]["1m"], list(range(144, 264)))
        raw["开仓条件"]["1m"] = [5, 156, 264]
        self.assertEqual(规范化配置(raw)["开仓条件"]["1m"], [5, 156, 264])
        raw["开仓条件"]["1m"] = [5, 156, 384]
        with self.assertRaisesRegex(ValueError, "未知或不支持.*384"):
            规范化配置(raw)
        raw["开仓条件"]["1m"] = [156]
        raw["开仓条件"]["4h"] = [5, 156]
        self.assertEqual(规范化配置(raw)["开仓条件"]["4h"], [5, 156])
        raw["开仓条件"]["4h"] = [5, 132]
        with self.assertRaisesRegex(ValueError, "4h.*132"):
            规范化配置(raw)


class ExecutionUiTests(unittest.TestCase):
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
        cls.error = cls.stack.enter_context(mock.patch.object(ui.messagebox, "showerror"))
        cls.app = ui.App()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.stack.close()

    def setUp(self):
        self.write.reset_mock()
        self.info.reset_mock()
        self.error.reset_mock()
        self.app._settings_restore_error = False
        self.app.data_report = None
        self.app.selection_panel.clear_data_capabilities()
        self.app.selection_panel.apply_config(全选配置())
        self.app.apply_execution_settings(全选配置())

    def test_cost_switch_disables_inactive_controls_and_keeps_values(self):
        app = self.app
        original_slip = (app.entry_slippage_var.get(), app.exit_slippage_var.get())
        app.set_cost_modes("FEE")
        self.assertTrue(all(w.instate(["disabled"]) for w in app.slippage_controls))
        self.assertTrue(all(not w.instate(["disabled"]) for w in app.fee_controls))
        self.assertEqual(effective_slippage(app.current_selection()), (0.0, 0.0))
        app.set_cost_modes("SLIPPAGE")
        self.assertTrue(all(w.instate(["disabled"]) for w in app.fee_controls))
        self.assertEqual((app.entry_slippage_var.get(), app.exit_slippage_var.get()), original_slip)
        self.assertEqual(effective_fee_rates(app.current_selection()), (0.0, 0.0))

    def test_no_data_available_selection_keeps_fourth_and_third_choices(self):
        panel = self.app.selection_panel
        before = panel.get_config()["开仓条件"]
        self.app.select_available_fourth()
        panel._set_fourth_source("1m", "available")
        panel._set_third_source("1m", "available")
        self.assertEqual(panel.get_config()["开仓条件"], before)
        self.assertEqual(self.info.call_count, 3)

    def test_fourth_controls_expand_and_data_capabilities(self):
        panel = self.app.selection_panel
        fourth = set(range(144, 264))
        self.assertEqual(fourth & set(panel.case_widgets["1m"]), fourth)
        for tf, values in panel.case_vars.items():
            expected = {c for c in fourth if tf in entry_supported_timeframes(c)}
            self.assertEqual(fourth & set(values), expected)
            self.assertIn(154, values)
        panel.fourth_entry_fold_vars["1m"].set(True)
        self.assertEqual(panel.case_widgets["1m"][144].master.winfo_manager(), "pack")
        panel.set_data_capabilities({"ohlcv": True})
        panel.fourth_entry_all_vars["1m"].set(True)
        panel._set_fourth_round("1m")
        self.assertEqual(len(fourth & set(panel.get_config()["开仓条件"]["1m"])), 108)
        self.assertEqual(sum(panel.case_widgets["1m"][c].instate(["disabled"]) for c in fourth), 12)

    def test_save_load_preserves_120_rules_cost_mode_and_custom_wait(self):
        app = self.app
        raw = 全选配置()
        raw["开仓条件"]["1m"] = list(range(144, 264))
        app.selection_panel.apply_config(raw)
        app.set_cost_modes("FEE")
        app.min_reentry_minutes_var.set("17")
        self.assertNotEqual(str(app.min_reentry_minutes_box.cget("state")), "readonly")
        self.assertTrue(app.save_user_settings())
        payload = copy.deepcopy(self.write.call_args.args[1])
        path = mock.Mock()
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps(payload, ensure_ascii=False)
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(app)
        restored = app.current_selection()
        self.assertEqual(restored["开仓条件"]["1m"], list(range(144, 264)))
        self.assertEqual(restored["成本模式"], "FEE")
        self.assertEqual(restored["平仓后最小开仓间隔分钟"], 17)
        self.assertEqual(restored["成交偏移"], payload["组合选择"]["成交偏移"])
        self.assertEqual(restored["手续费"], payload["组合选择"]["手续费"])

    def test_failed_restore_does_not_overwrite_old_settings(self):
        raw = 全选配置()
        # Retired fifth-batch codes are intentionally removed by migration.
        # An unknown cost model is still an invalid, non-migratable setting.
        raw["成本模式"] = "UNKNOWN_COST_MODE"
        path = mock.Mock()
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps({"组合选择": raw}, ensure_ascii=False)
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(self.app)
        self.assertTrue(self.app._settings_restore_error)
        self.assertFalse(self.app.save_user_settings())
        self.write.assert_not_called()
        self.error.assert_called_once()

    def test_old_default_slippage_migrates_but_custom_slippage_is_retained(self):
        new_default = {"开仓": 0.00012, "平仓": 0.00023}
        for slippage, expected in ((self.ui.LEGACY_DEFAULT_SLIPPAGE, new_default),
                                   ({"开仓": 0.00011, "平仓": 0.00022}, {"开仓": 0.00011, "平仓": 0.00022})):
            with self.subTest(slippage=slippage):
                raw = 全选配置()
                raw["版本"] = 17
                raw["成交偏移"] = dict(slippage)
                for key in ("成本模式", "手续费", "平仓后最小开仓间隔分钟"):
                    raw.pop(key)
                path = mock.Mock()
                path.is_file.return_value = True
                path.read_text.return_value = json.dumps({"组合选择": raw, "成交偏移预设": "Maker均值"}, ensure_ascii=False)
                with mock.patch.object(self.ui, "用户设置文件", path), mock.patch.object(self.ui, "DEFAULT_SLIPPAGE", new_default):
                    type(self).original_load(self.app)
                restored = self.app.current_selection()
                for key, value in expected.items():
                    self.assertAlmostEqual(restored["成交偏移"][key], value)
                    self.assertAlmostEqual(self.write.call_args.args[1]["组合选择"]["成交偏移"][key], value)
                self.assertEqual(restored["平仓后最小开仓间隔分钟"], 0)


if __name__ == "__main__":
    unittest.main()
