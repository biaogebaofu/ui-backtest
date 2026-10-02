"""UI integration tests use hidden Tk windows and fake worker processes."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hedge_panel import DEFAULT_FIELDS, HedgePanel, fields_to_config, holding_summary, number_list, selection_for_hedge
from indicator_combinations import DEFAULT_REGISTRY


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def editor(code=42):
    return SimpleNamespace(
        field_vars={"hist": Variable(True), "dif": Variable(False)},
        case_vars={tf: {0: Variable(tf != "1m"), code: Variable(tf == "1m")}
                   for tf in ("4h", "1h", "15m", "5m", "1m")},
        indicator_combos={}, get_entry_modes=lambda: ["LIVE_01"],
    )


class ParameterTests(unittest.TestCase):
    def test_holding_summary_reports_actual_page_settings(self):
        config = fields_to_config(dict(DEFAULT_FIELDS, mode="single", initial_equity="20000",
                                       tp_weekday_percent="1.5", tp_holiday_percent="0.75",
                                       timeout_hours="6", maker_fee_percent="0.02"))
        text = holding_summary(config)
        for expected in ("20,000U", "各方向1倍", "20 ETH", "1.5%", "0.75%", "6小时", "0.02%",
                         "不限制多空方向", "没有独立价格止损"):
            self.assertIn(expected, text)

    def test_percentage_inputs_convert_once_and_keep_multiple_values(self):
        raw = dict(DEFAULT_FIELDS, add_drops_percent="0.1，0.2 0.1", maker_fee_percent="0.02")
        got = fields_to_config(raw)
        self.assertEqual(got["add_drops"], [0.001, 0.002])
        self.assertEqual(got["tp_weekday"], 0.004)
        self.assertEqual(got["maker_fee_rate"], 0.0002)
        self.assertEqual(got["paths"], [0, 1])
        self.assertEqual((got["first_multiple"], got["add_multiple"]), (0.5, 0.5))

    def test_single_mode_fixed_weights_and_fractional_seed_rejected(self):
        got = fields_to_config(dict(DEFAULT_FIELDS, mode="single", paths="1"))
        self.assertEqual((got["first_multiple"], got["add_multiple"]), (1.0, 0.0))
        self.assertEqual(got["paths"], [1])
        with self.assertRaises(ValueError):
            fields_to_config(dict(DEFAULT_FIELDS, seeds="1.5"))

    def test_bad_numeric_lists_and_config_are_rejected(self):
        for text in ("", "nan", "inf", "1,word"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                number_list(text, "测试")
        for key, value in (("initial_equity", "0"), ("max_eth", "-1"), ("timeout_hours", "0"),
                           ("add_drops_percent", "-0.1"), ("maker_fee_percent", "10")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                fields_to_config(dict(DEFAULT_FIELDS, **{key: value}))

    def test_random_does_not_read_old_editor_exit_or_size_controls(self):
        got = selection_for_hedge(object(), False)
        self.assertEqual(got["开仓方向"], ["BOTH"])
        self.assertEqual(got["交易会话"], ["ALL"])
        self.assertEqual(got["开仓位置过滤"], ["OFF"])
        self.assertTrue(all(codes == [0] for codes in got["开仓条件"].values()))

    def test_selected_entries_preserved_and_old_exit_settings_unused(self):
        source = editor()
        before = copy.deepcopy(source.indicator_combos)
        app = SimpleNamespace(selection_panel=source, s3_gap_var=Variable("0.12"),
                              s3_timeframe_var=Variable("5m"))
        got = selection_for_hedge(app, True)
        self.assertEqual(got["开仓条件"]["1m"], [42])
        self.assertEqual(got["开仓条件"]["4h"], [0])
        self.assertEqual(got["入场约束"]["最小S3距离"], 0.0012)
        self.assertEqual(source.indicator_combos, before)

    def test_permanently_deleted_fifth_method_is_rejected(self):
        app = SimpleNamespace(selection_panel=editor(264), s3_gap_var=Variable("0.1"),
                              s3_timeframe_var=Variable("1m"))
        with self.assertRaisesRegex(ValueError, "永久删除"):
            selection_for_hedge(app, True)

    def test_explicit_compound_code_and_entry_mode_preserved(self):
        code = DEFAULT_REGISTRY.register("entry", [278, 288])
        source = editor(code)
        source.get_entry_modes = lambda: ["F5_EVENT"]
        app = SimpleNamespace(selection_panel=source, s3_gap_var=Variable("0.1"),
                              s3_timeframe_var=Variable("1m"))
        got = selection_for_hedge(app, True)
        self.assertEqual(got["开仓条件"]["1m"], [code])
        self.assertEqual(got["入场触发口径"], "F5_EVENT")

    def test_old_auto_exit_combinations_do_not_block_independent_page(self):
        source = editor()
        source.indicator_combos = {"止损": {"启用": True, "组合数量": [99]}}
        app = SimpleNamespace(selection_panel=source, s3_gap_var=Variable("0.1"),
                              s3_timeframe_var=Variable("1m"))
        got = selection_for_hedge(app, True)
        self.assertFalse(got.get("指标组合"))

    def test_entry_group_controls_are_preserved_for_multiperiod_run(self):
        source = editor()
        source.case_vars["1m"][1] = Variable(True)
        spec = {"启用": True, "组合数量": [2], "保留单项": False, "逻辑": "AND"}
        source.indicator_combos = {"开仓": {"1m": spec}, "止损": {"启用": True, "组合数量": [99]}}
        app = SimpleNamespace(selection_panel=source, s3_gap_var=Variable("0.1"), s3_timeframe_var=Variable("1m"))
        got = selection_for_hedge(app, True)
        self.assertEqual(got["指标组合"], {"开仓": {"1m": spec}})


class HiddenPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.source = self.directory / "sample.csv"
        self.source.write_text("openTime,open,high,low,close\n0,100,101,99,100\n", encoding="utf-8")
        names = dict(csv_var=str(self.source), micro_csv_var="", funding_var="", oi_var="",
                     bundle_var="", start_date_var="", end_date_var="", thread_var="1",
                     out_var=str(self.directory), s3_gap_var="0.1", s3_timeframe_var="1m")
        self.app = SimpleNamespace(**{name: tk.StringVar(self.root, value=value) for name, value in names.items()},
                                   selection_panel=editor(), current_date_range=lambda: ("", ""),
                                   _fingerprint_run_active=Mock(return_value=False),
                                   _launch_process=Mock(return_value=True), proc_kind="",
                                   proc=None, _active_output_dir=None)
        self.app.notebook = ttk.Notebook(self.root)
        for i in range(5):
            self.app.notebook.add(ttk.Frame(self.app.notebook), text=str(i))
        (self.directory / "hedge_worker.py").write_text("# fake worker is never executed\n", encoding="utf-8")
        self.settings = self.directory / "independent_settings.json"
        self.panel = HedgePanel(self.root, self.app, self.directory, settings_path=self.settings)

    def tearDown(self):
        self.root.destroy()
        self.temp.cleanup()

    def test_settings_do_not_overwrite_old_settings_and_restore_independently(self):
        old = self.directory / "用户设置.json"
        old.write_text('{"旧设置":"保持"}', encoding="utf-8")
        self.panel.vars["initial_equity"].set("34567")
        self.panel.vars["compare_entries"].set(True)
        self.panel.save_settings()
        self.assertEqual(old.read_text("utf-8"), '{"旧设置":"保持"}')
        self.panel.vars["initial_equity"].set("1")
        self.panel.load_settings()
        self.assertEqual(self.panel.vars["initial_equity"].get(), "34567")
        self.assertTrue(self.panel.vars["compare_entries"].get())

    def test_corrupt_settings_fall_back_without_callback_failure(self):
        self.settings.write_text("[]", encoding="utf-8")
        self.panel.load_settings()
        self.assertIn("设置未能恢复", self.panel.log.get("1.0", "end"))

    def test_editing_trial_parameters_refreshes_summary_without_manual_button(self):
        self.panel.vars["compare_entries"].set(True)
        self.panel.vars["mode"].set("single")
        self.panel.vars["timeout_hours"].set("6")
        self.panel.vars["seeds"].set("2")
        self.root.update_idletasks()
        self.assertIn("最多8次试验", self.panel.scope_var.get())
        self.assertIn("超时6小时", self.panel.model_var.get())
        self.panel.vars["timeout_hours"].set("6,12")
        self.panel.vars["maker_fee_percent"].set("0.03")
        self.root.update_idletasks()
        self.assertIn("最多16次试验", self.panel.scope_var.get())
        self.assertIn("0.03%", self.panel.model_var.get())
        self.panel.vars["timeout_hours"].set("")
        self.root.update_idletasks()
        self.assertIn("待调整", self.panel.model_var.get())

    def test_pending_summary_refresh_is_cancelled_when_panel_closes(self):
        self.panel.vars["seeds"].set("3")
        self.assertIsNotNone(self.panel._context_after_id)
        self.panel.destroy()
        self.root.update_idletasks()
        self.assertIsNone(self.panel._context_after_id)

    def test_independent_summary_counts_compounds_instead_of_raw_checkboxes(self):
        self.app.selection_panel.case_vars['1m'][1] = Variable(True)
        self.app.selection_panel.indicator_combos = {'开仓': {'1m': {
            '启用': True, '组合数量': [2], '保留单项': False, '逻辑': 'AND'}}}
        for key, value in {'compare_entries': True, 'entry_plan': 'independent',
                           'mode': 'single', 'timeout_hours': '6', 'seeds': '2'}.items():
            self.panel.vars[key].set(value)
        self.root.update_idletasks()
        self.assertIn('最多1条开仓组合', self.panel.scope_var.get())
        self.assertIn('最多8次试验', self.panel.scope_var.get())

    def test_direction_dialog_apply_persists_and_request_carries_rule(self):
        from direction_calendar import RULE_ID
        window=self.panel.show_direction_limit()
        def widgets(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from widgets(widget)
        radios=[w for w in widgets(window) if isinstance(w,ttk.Radiobutton)]
        radios[1].invoke()
        next(w for w in widgets(window) if isinstance(w,ttk.Button) and w.cget('text')=='应用').invoke()
        self.assertTrue(self.panel.vars['direction_limit'].get())
        self.assertEqual(self.panel.build_request()['hedge']['direction_rule'],RULE_ID)
        self.panel.vars['direction_limit'].set(False)
        self.panel.load_settings()
        self.assertTrue(self.panel.vars['direction_limit'].get())
        self.panel.vars['direction_limit'].set(False)
        self.assertNotIn('direction_rule',self.panel.build_request()['hedge'])

    def test_start_builds_cli_snapshot_without_old_run_directory_mutation(self):
        self.panel.start()
        args = self.app._launch_process.call_args.args
        command, out, kind = args
        self.assertEqual(kind, "hedge")
        self.assertEqual(command[command.index("--threads") + 1], "1")
        self.assertEqual(Path(command[command.index("--output") + 1]), out)
        request = json.loads((out / "双向回测请求.json").read_text("utf-8"))
        self.assertEqual(request["sources"]["kline"], str(self.source))
        self.assertEqual(request["hedge"]["initial_equity"], 20000)
        self.assertEqual(request["entry_plan"], "combined")
        self.assertTrue(self.panel.running)
        self.assertEqual(str(self.panel.start_btn.cget("state")), "disabled")
        self.assertFalse(hasattr(self.app, "current_output_dir"))

    def test_running_old_job_prevents_new_output_or_process(self):
        self.app._fingerprint_run_active.return_value = True
        self.panel.start()
        self.app._launch_process.assert_not_called()
        self.assertFalse(list(self.directory.glob("双向持仓对照_*")))

    def test_stop_flag_and_progress_lifecycle(self):
        self.panel.start()
        self.app.proc_kind = "hedge"
        self.app.proc = SimpleNamespace(poll=lambda: None)
        self.panel.request_stop()
        self.assertTrue((self.panel.output / "停止请求.flag").is_file())
        self.panel.handle_message({"type": "progress", "completed": 3, "total": 5, "eta_seconds": 61})
        self.assertEqual(float(self.panel.progress["value"]), 60)
        self.assertIn("1分1秒", self.panel.eta_var.get())
        self.panel.handle_message({"type": "done", "output": str(self.panel.output)})
        self.assertTrue(self.panel.running)
        self.panel.handle_message({"type": "process_exit", "code": 0})
        self.assertEqual(float(self.panel.progress["value"]), 100)
        self.assertFalse(self.panel.running)
        self.assertIn("完成", self.panel.status_var.get())

    def test_missing_source_stops_before_launch(self):
        self.app.csv_var.set(str(self.directory / "missing.csv"))
        with patch("hedge_panel.messagebox.showerror") as error:
            self.panel.start()
        error.assert_called_once()
        self.app._launch_process.assert_not_called()


class AppLifecycleTests(unittest.TestCase):
    def test_hidden_full_app_uses_one_worker_and_restores_all_tabs(self):
        from ui import App, 工具版本
        with TemporaryDirectory() as directory, patch.object(App, "load_user_settings"), patch.object(App, "detect_gpu"):
            app = App()
            app.withdraw()
            app.hedge_panel.settings_path = Path(directory) / "settings.json"
            try:
                app.update_idletasks()
                self.assertEqual(app.notebook.index("end"), 5)
                self.assertIn("v1.72", 工具版本)
                self.assertIn("双向持仓", app.notebook.tab(4, "text"))
                fake_process = SimpleNamespace(poll=lambda: None)
                with patch("ui.subprocess.Popen", return_value=fake_process), patch("ui.threading.Thread"):
                    self.assertTrue(app._launch_process(["fake"], Path(directory), "hedge"))
                    self.assertFalse(app._launch_process(["fake"], Path(directory), "backtest"))
                self.assertEqual([app.notebook.tab(i, "state") for i in range(5)],
                                 ["disabled", "disabled", "disabled", "disabled", "normal"])
                app.hedge_panel.output = Path(directory)
                app.hedge_panel.running = True
                app.stop_run()
                self.assertTrue((Path(directory) / "停止请求.flag").is_file())
                job = app._active_job_id
                for message in ({"type": "progress", "completed": 1, "total": 2, "eta_seconds": 30},
                                {"type": "done", "output": directory}, {"type": "process_exit", "code": 0}):
                    app.messages.put(dict(message, job_id=job))
                fake_process.poll = lambda: 0
                app.after_cancel(app._poll_after_id)
                app._poll_after_id = None
                app.poll_messages()
                self.assertEqual([app.notebook.tab(i, "state") for i in range(5)], ["normal"] * 5)
                self.assertIsNone(app._active_job_id)
                self.assertEqual(app.proc_kind, "")
                self.assertIn("完成", app.hedge_panel.status_var.get())
                next_process = SimpleNamespace(poll=lambda: None)
                with patch("ui.subprocess.Popen", return_value=next_process), patch("ui.threading.Thread"):
                    self.assertTrue(app._launch_process(["fake"], Path(directory), "hedge"))
                with patch("ui.messagebox.askyesno", return_value=True), patch.object(app, "save_user_settings"), patch.object(app, "destroy") as destroy:
                    app.on_close()
                    self.assertTrue(app._closing)
                    destroy.assert_not_called()
                    self.assertTrue((Path(directory) / "停止请求.flag").is_file())
                    next_process.poll = lambda: 0
                    app._wait_for_safe_close()
                    destroy.assert_called_once()
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
