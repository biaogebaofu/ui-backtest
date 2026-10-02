"""多成本档独立回测；隐藏Tk、模拟保存和工作进程，不修改用户设置。"""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from execution_settings import effective_fee_rates, effective_slippage
from selection_config import 配置统计
import test_v149_fingerprint_ui as single
import test_v152_fingerprint_queue_ui as queue


class CostModesUiTests(unittest.TestCase):
    assert_nested_equal = queue.FingerprintQueueUiTests.assert_nested_equal

    @classmethod
    def setUpClass(cls):
        queue.FingerprintQueueUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        queue.FingerprintQueueUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        queue.FingerprintQueueUiTests.setUp(self)
        self.app._close_fingerprint_queue()

    def tearDown(self):
        queue.FingerprintQueueUiTests.tearDown(self)

    def test_two_independent_checks_share_panel_source_not_display_string(self):
        app = self.app
        self.assertIs(app.selection_panel.cost_mode_vars, app.cost_mode_vars)
        self.assertEqual(set(app.cost_mode_widgets), {"SLIPPAGE", "FEE"})
        self.assertEqual(len({str(var) for var in app.cost_mode_vars.values()}), 2)
        for code, widget in app.cost_mode_widgets.items():
            self.assertEqual(widget.winfo_class(), "TCheckbutton")
            self.assertEqual(str(widget.cget("variable")), str(app.cost_mode_vars[code]))
        before = app.current_selection()
        app.cost_mode_var.set("SLIPPAGE")
        self.assertEqual(app.current_selection(), before)
        self.assertEqual(app.get_cost_modes(), ["FEE"])
        app.update_cost_mode()
        self.assertEqual(app.cost_mode_var.get(), "FEE")

    def test_both_modes_enable_both_controls_and_preserve_nonzero_branch_costs(self):
        app = self.app
        before = app.current_selection()
        app.cost_mode_widgets["SLIPPAGE"].invoke()
        config = app.current_selection()
        self.assertEqual(config["成本模式"], ["SLIPPAGE", "FEE"])
        self.assertTrue(all(not widget.instate(["disabled"]) for widget in (*app.slippage_controls, *app.fee_controls)))
        for key in ("手续费", "成交偏移", "资金约束", "平仓后最小开仓间隔分钟"):
            self.assertEqual(config[key], before[key])
        self.assertIn("分别回测", app.cost_mode_hint_var.get())
        self.assertIn("不叠加", app.cost_mode_hint_var.get())
        slip = dict(config, 成本模式="SLIPPAGE")
        fee = dict(config, 成本模式="FEE")
        self.assertEqual(effective_fee_rates(slip), (0., 0.))
        self.assertEqual(effective_slippage(fee), (0., 0.))
        self.assertEqual(effective_slippage(slip), (config["成交偏移"]["开仓"], config["成交偏移"]["平仓"]))
        factor = .9 * (1 - config["手续费"]["返佣比例"])
        rates = effective_fee_rates(fee)
        self.assertAlmostEqual(rates[0], config["手续费"]["开仓费率"] * factor)
        self.assertAlmostEqual(rates[1], config["手续费"]["平仓费率"] * factor)
        self.assertGreater(rates[0], 0)
        self.assertIn(f"{rates[0] * 100:.5f}%", app.net_fee_label_var.get())
        for helper in (effective_fee_rates, effective_slippage):
            with self.assertRaises(ValueError):
                helper(config)

    def test_single_mode_only_disables_other_controls_without_losing_parameters(self):
        app = self.app
        before = app.current_selection()
        for mode, disabled, enabled in (("SLIPPAGE", app.fee_controls, app.slippage_controls),
                                        ("FEE", app.slippage_controls, app.fee_controls)):
            app.set_cost_modes(mode)
            self.assertTrue(all(widget.instate(["disabled"]) for widget in disabled))
            self.assertTrue(all(not widget.instate(["disabled"]) for widget in enabled))
            self.assertEqual(app.current_selection()["成本模式"], mode)
            for key in ("手续费", "成交偏移"):
                self.assertEqual(app.current_selection()[key], before[key])

    def test_selected_zero_net_fee_has_explicit_warning_without_changing_rates_or_discounts(self):
        app = self.app
        app.set_cost_modes("SLIPPAGE")
        app.entry_fee_var.set(0); app.exit_fee_var.set(0)
        app.bnb_discount_var.set(True); app.fee_rebate_var.set(20)
        original = app.current_execution_settings()["手续费"]
        self.assertNotIn("当前净费率为0", app.net_fee_label_var.get())
        app.set_cost_modes(["SLIPPAGE", "FEE"])
        self.assertIn("当前净费率为0", app.net_fee_label_var.get())
        self.assertIn("不会自动补吃单费", app.net_fee_label_var.get())
        self.assertEqual(app.current_execution_settings()["手续费"], original)
        app.entry_fee_var.set(.04)
        self.assertNotIn("当前净费率为0", app.net_fee_label_var.get())
        self.assertTrue(app.bnb_discount_var.get())
        self.assertEqual(app.fee_rebate_var.get(), 20)
        app.fee_rebate_var.set(100)
        self.assertIn("当前净费率为0", app.net_fee_label_var.get())
        self.assertEqual(app.entry_fee_var.get(), .04)
        self.assertEqual(app.fee_rebate_var.get(), 100)
        self.write.assert_not_called()

    def test_zero_selection_blocks_configuration_save_and_start_without_fallback(self):
        app = self.app
        app.cost_mode_widgets["FEE"].invoke()
        self.assertFalse(any(var.get() for var in app.cost_mode_vars.values()))
        self.assertIn("至少", app.cost_mode_hint_var.get())
        for getter in (app.get_cost_modes, app.current_execution_settings, app.current_selection,
                       app.selection_panel.get_config):
            with self.assertRaises(ValueError):
                getter()
        self.assertIn("不完整", app.selection_summary_var.get())
        self.assertFalse(app.save_user_settings())
        with mock.patch.object(app, "resolve_output_dir", return_value=Path.cwd()), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(Path, "write_text") as write, mock.patch.object(app, "_launch_process") as launch:
            app.start_run()
        launch.assert_not_called(); write.assert_not_called(); self.write.assert_not_called()
        self.assertIn("成本", self.error.call_args.args[1])

    def test_invalid_setter_is_atomic_and_valid_order_is_canonical(self):
        app = self.app
        before = app.current_selection()
        for value in ([], ["SLIPPAGE", "unknown"], "unknown", None, 1):
            with self.assertRaises(ValueError):
                app.set_cost_modes(value)
            self.assertEqual(app.current_selection(), before)
        app.set_cost_modes(["FEE", "SLIPPAGE", "FEE"])
        self.assertEqual(app.get_cost_modes(), ["SLIPPAGE", "FEE"])
        app.set_cost_modes(["FEE"])
        self.assertEqual(app.current_selection()["成本模式"], "FEE")

    def test_mode_only_change_marks_new_task_and_updates_panel_and_main_counts(self):
        app = self.app
        before = app.current_selection()
        baseline = 配置统计(before)
        app.current_output_dir = Path.cwd() / "uncreated-previous-result"
        with mock.patch.object(app, "_launch_process") as launch:
            app.cost_mode_widgets["SLIPPAGE"].invoke()
        after = app.current_selection()
        self.assertIsNone(app.current_output_dir)
        expected = copy.deepcopy(before); expected["成本模式"] = ["SLIPPAGE", "FEE"]
        self.assertEqual(after, expected)
        counts = 配置统计(after)
        self.assertEqual(counts["成本模式档数"], 2)
        self.assertEqual(counts["入场组合数"], baseline["入场组合数"])
        self.assertEqual(counts["包含仓位完整组合数"], baseline["包含仓位完整组合数"] * 2)
        self.assertEqual(counts["不含仓位完整组合数"], baseline["不含仓位完整组合数"] * 2)
        for variable in (app.selection_panel.summary_var, app.selection_summary_var):
            self.assertIn("成本 2档", variable.get())
        launch.assert_not_called(); self.write.assert_not_called()

    def test_three_entry_modes_sixteen_positions_and_two_costs_produce_96(self):
        app = self.app
        app.selection_panel.set_entry_modes(["LIVE_01", "TF_EVENT", "MACD_CYCLE"])
        app.selection_panel.position_compare_button.invoke()
        app.set_cost_modes(["SLIPPAGE", "FEE"])
        counts = 配置统计(app.current_selection())
        self.assertEqual((counts["入场口径档数"], counts["开仓位置过滤档数"], counts["成本模式档数"]), (3, 16, 2))
        self.assertEqual(counts["包含仓位完整组合数"], 96)
        self.assertIn("96", app.selection_summary_var.get())

    def test_save_load_restores_both_costs_dates_wait_and_inactive_parameters(self):
        app = self.app
        app.set_cost_modes(["FEE", "SLIPPAGE"])
        before, dates = app.current_selection(), app.current_date_range()
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        self.assertEqual(saved["组合选择"]["成本模式"], ["SLIPPAGE", "FEE"])
        app.set_cost_modes("SLIPPAGE")
        app.entry_fee_var.set(.99)
        path = mock.Mock(is_file=mock.Mock(return_value=True),
                         read_text=mock.Mock(return_value=json.dumps(saved)))
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(app)
        self.assert_nested_equal(app.current_selection(), before)
        self.assertEqual(app.current_date_range(), dates)
        self.assertTrue(all(var.get() for var in app.cost_mode_vars.values()))
        self.assertTrue(all(not widget.instate(["disabled"]) for widget in app.fee_controls))

    def test_single_fingerprint_apply_replaces_dual_mode_with_real_scalar_mode(self):
        app = self.app
        for mode in ("SLIPPAGE", "FEE"):
            app.set_cost_modes(["SLIPPAGE", "FEE"])
            payload = single.restored_payload()
            payload["selection"]["成本模式"] = mode
            with mock.patch.object(app, "_launch_process") as launch:
                app.apply_fingerprint_restoration(payload)
            self.assert_nested_equal(app.current_selection(), payload["selection"])
            self.assertEqual(app.get_cost_modes(), [mode])
            self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 1)
            launch.assert_not_called()

    def test_adding_two_cost_fingerprints_merges_editor_but_keeps_scalar_exact_snapshots(self):
        from ranking_view import config_fingerprint
        app = self.app
        items, restored = [], []
        for mode in ("SLIPPAGE", "FEE"):
            item = queue.match()
            value = queue.restoration(item)
            value["selection"]["成本模式"] = mode
            slip = effective_slippage(value["selection"])
            fees = effective_fee_rates(value["selection"])
            item["row"].update({"成本模式": mode, "开仓成交偏移（%）": slip[0], "平仓成交偏移（%）": slip[1],
                                "开仓净手续费率（%）": fees[0], "平仓净手续费率（%）": fees[1]})
            item["fingerprint"] = config_fingerprint(item["row"])
            value["fingerprint"] = item["fingerprint"]
            items.append(item); restored.append(value)
        with mock.patch.object(app, "_start_fingerprint_job"):
            queue.FingerprintQueueUiTests.deliver(self, "queue_find", {item["fingerprint"]: [item] for item in items})
        queue.FingerprintQueueUiTests.deliver(self, "queue_restore", restored)
        self.assertEqual(app.run_target_var.get(), "EDITOR")
        self.assertEqual(app.current_selection()["成本模式"], ["SLIPPAGE", "FEE"])
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 2)
        self.assertEqual([item["verified_snapshot"]["selection"]["成本模式"] for item in app.fingerprint_queue],
                         ["SLIPPAGE", "FEE"])
        self.assertEqual([app.batch_tree.set(i, "成本") for i in app.batch_tree.get_children()], ["成交偏移", "手续费"])
        self.assertEqual(len(app.fingerprint_queue), 2)
        self.write.assert_not_called()

    def test_editor_run_passes_both_modes_in_one_selection_without_altering_dates(self):
        app = self.app
        app.set_cost_modes(["SLIPPAGE", "FEE"])
        expected = app.current_selection()
        out = Path.cwd() / "uncreated-dual-cost-result"
        with mock.patch.object(app, "resolve_output_dir", return_value=out), \
                mock.patch.object(app, "validate_paths", return_value=True), \
                mock.patch.object(Path, "write_text") as write, \
                mock.patch.object(app, "_launch_process", return_value=False) as launch:
            app.start_run()
        self.assertEqual(json.loads(write.call_args_list[0].args[0]), expected)
        self.assertEqual(launch.call_args.args[2], "backtest")
        cmd = launch.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--start") + 1], app.start_date_var.get())
        self.assertEqual(cmd[cmd.index("--end") + 1], app.end_date_var.get())

    def test_cost_checkboxes_fit_minimum_width_and_both_are_accessible(self):
        app = self.app
        app.set_cost_modes(["SLIPPAGE", "FEE"])
        app.attributes("-alpha", 0)
        if app.tk.call("tk", "windowingsystem") == "win32":
            app.attributes("-toolwindow", True)
        try:
            for width, height in ((1080, 720), (1280, 850)):
                app.geometry(f"{width}x{height}+30000+30000"); app.deiconify()
                app.notebook.select(app.run_tab)
                app.fold_vars["运行设置"].set(False); app._apply_folds(); app.update()
                canvas = app.run_scroll.canvas
                for widget in app.cost_mode_widgets.values():
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), canvas.winfo_rootx() + canvas.winfo_width())
                    bounds = canvas.bbox("all")
                    content_y = canvas.canvasy(0) + widget.winfo_rooty() - canvas.winfo_rooty()
                    canvas.yview_moveto(content_y / bounds[3]); app.update()
                    top = widget.winfo_rooty() - canvas.winfo_rooty()
                    self.assertGreaterEqual(top, -2)
                    self.assertLessEqual(top + widget.winfo_height(), canvas.winfo_height())
        finally:
            app.withdraw()


if __name__ == "__main__":
    unittest.main()
