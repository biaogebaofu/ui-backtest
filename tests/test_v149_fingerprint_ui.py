"""指纹回填只写测试内存，不读取或覆盖用户设置，不启动回测进程。"""
import copy
import json
import threading
from pathlib import Path
import unittest
from contextlib import ExitStack
from unittest import mock

from extended_rules import ENTRY_RULE_REQUIREMENTS
from selection_config import 全选配置, 规范化配置, 配置统计
from execution_settings import effective_fee_rates, effective_slippage


def single_selection():
    config = 全选配置()
    config.update({"开仓指标": ["hist"], "开仓位置过滤": ["EMA5_ATR100_R60_E20"],
                   "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
                   "开仓方向": ["LONG"], "交易会话": ["ASIA"],
                   "强制时间止损分钟": [15], "止盈方案编号": [8427],
                   "止盈后等待分钟": [7], "仓位倍数": [2.0], "成本模式": "FEE",
                   "平仓后最小开仓间隔分钟": 17, "入场触发口径": "TF_EVENT",
                   "成交价格口径": "THEORETICAL", "成交偏移": {"开仓": 0.00005, "平仓": 0.00005}})
    config["开仓条件"] = {tf: [0] for tf in config["开仓条件"]}
    config["开仓条件"]["1m"] = [156]
    config["止损代码"] = config["止损代码"][:1]
    config["手续费"] = {"开仓费率": 0.0005, "平仓费率": 0.0004, "BNB抵扣": True, "返佣比例": 0.2}
    config["资金约束"].update({"初始资金USDC": 321.0, "最小开仓数量ETH": 0.02,
                              "最大开仓数量ETH": 18.0, "低于最小数量停止": False,
                              "保护止损浮亏比例": 0.8, "维持保证金率": 0.006,
                              "启用全仓强平": False, "资金费率": 0.0001})
    config["入场约束"] = {"最小S3距离": 0.002, "S3基线周期": "5m"}
    config["候选筛选"].update({"启用": True, "自动导出": False, "统一目标杠杆": 2.0,
                              "p95往返偏移": 0.0005, "极端往返偏移": 0.0009})
    return 规范化配置(config)


def restored_payload():
    return {
        "selection": single_selection(),
        "sources": {key: str(Path.cwd() / "_synthetic_fixture" / "source" / f"{key}.csv") for key in ("kline", "micro", "funding", "oi", "bundle")},
        "start": "2026-01-01T08:00:00+08:00", "end": "2026-02-01",
        "source_dir": str(Path.cwd() / "_synthetic_fixture" / "history/original-result"), "fingerprint": "a" * 16,
        "engine_version": "original-engine", "current_engine_version": "current-engine",
        "source_fingerprint": "b" * 64,
        "capabilities": {key: True for required in ENTRY_RULE_REQUIREMENTS.values() for key in required},
        "ranking_settings": {"排行指标": "累计收益率（%）", "门槛": [
            {"指标": "交易次数（单）", "条件": "最低值", "值": 10, "输入值": 10}]},
        "warnings": ["原数据指纹已核验"],
    }


class FingerprintUiTests(unittest.TestCase):
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
        app = self.app
        app.proc = None
        app._active_job_id = None
        app._active_output_dir = None
        app._closing = False
        app._settings_restore_error = False
        app._invalidate_fingerprint_lookup()
        app.out_var.set(str(Path.cwd()))
        app.current_output_dir = None
        app.start_date_var.set("")
        app.end_date_var.set("")
        app.fingerprint_var.set("a" * 16)
        app.fingerprint_source_var.set("")
        app.funding_rate_var.set(0.0)
        app.selection_panel.clear_data_capabilities()
        app.selection_panel.apply_config(全选配置())
        app.apply_execution_settings(全选配置())
        app.apply_candidate_settings(全选配置()["候选筛选"])

    def snapshot(self):
        app = self.app
        return {"selection": app.current_selection(),
                "sources": [v.get() for v in (app.csv_var, app.micro_csv_var, app.funding_var, app.oi_var, app.bundle_var)],
                "dates": (app.start_date_var.get(), app.end_date_var.get()),
                "root": app.out_var.get(), "current_output": app.current_output_dir,
                "separate": app.separate_output_var.get(), "force": app.force_var.get(),
                "ranking": app.current_ranking_settings()}

    def assert_nested_equal(self, actual, expected):
        if isinstance(expected, dict):
            self.assertEqual(set(actual), set(expected))
            for key, value in expected.items():
                self.assert_nested_equal(actual[key], value)
        elif isinstance(expected, float):
            self.assertAlmostEqual(actual, expected, places=12)
        else:
            self.assertEqual(actual, expected)

    def test_full_restore_is_one_combination_preserves_costs_and_prepares_new_directory(self):
        app = self.app
        payload = restored_payload()
        app.current_output_dir = Path.cwd() / "existing-old-result"
        app.separate_output_var.set(False)
        app.force_var.set(True)
        with mock.patch.object(app, "start_run") as start, mock.patch.object(app, "_launch_process") as launch:
            app.apply_fingerprint_restoration(payload)
        self.assert_nested_equal(app.current_selection(), payload["selection"])
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 1)
        self.assertEqual([v.get() for v in (app.csv_var, app.micro_csv_var, app.funding_var, app.oi_var, app.bundle_var)],
                         [payload["sources"][key] for key in ("kline", "micro", "funding", "oi", "bundle")])
        self.assertEqual(app.current_date_range(), (payload["start"], payload["end"]))
        self.assertTrue(app.separate_output_var.get())
        self.assertFalse(app.force_var.get())
        self.assertIsNone(app.current_output_dir)
        future = app.resolve_output_dir()
        self.assertEqual(future.parent, Path.cwd())
        self.assertNotEqual(future, Path(payload["source_dir"]))
        self.assertFalse(future.exists())
        self.assertEqual(effective_slippage(app.current_selection()), (0.0, 0.0))
        self.assertGreater(effective_fee_rates(app.current_selection())[0], 0)
        self.assertAlmostEqual(app.current_selection()["资金约束"]["资金费率"], 0.0001)
        self.assertIn("版本不同，重跑结果可能与原结果不同", app.log.get("1.0", "end"))
        start.assert_not_called()
        launch.assert_not_called()
        self.write.assert_not_called()

    def test_incomplete_or_invalid_payload_does_not_partly_change_ui(self):
        app = self.app
        before = self.snapshot()
        invalid = []
        payload = restored_payload(); payload["selection"].pop("资金约束"); invalid.append(payload)
        payload = restored_payload(); payload["selection"]["仓位倍数"] = [1.0, 2.0]; invalid.append(payload)
        payload = restored_payload(); payload["start"] = "not-a-date"; invalid.append(payload)
        payload = restored_payload(); payload["sources"].pop("oi"); invalid.append(payload)
        payload = restored_payload(); payload["ranking_settings"]["门槛"][0]["输入值"] = "nan"; invalid.append(payload)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises((ValueError, KeyError)):
                app.apply_fingerprint_restoration(payload)
            self.assertEqual(self.snapshot(), before)
        self.write.assert_not_called()

    def test_running_or_pending_process_exit_blocks_lookup_and_apply(self):
        app = self.app
        before = self.snapshot()
        for process, job_id in ((mock.Mock(poll=mock.Mock(return_value=None)), None), (None, 7)):
            app.proc, app._active_job_id = process, job_id
            with mock.patch.object(app, "_start_fingerprint_job") as job:
                app.lookup_fingerprint()
                job.assert_not_called()
            with self.assertRaises(ValueError):
                app.apply_fingerprint_restoration(restored_payload())
            self.assertEqual(self.snapshot(), before)

    def test_lookup_uses_history_root_or_explicit_source_and_single_match_restores(self):
        app = self.app
        app.current_output_dir = Path.cwd() / "one-current-result"
        match = {"fingerprint": "a" * 16, "source_dir": str(Path.cwd() / "_synthetic_fixture" / "history/original"), "row": {}}
        before = self.snapshot()
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.lookup_fingerprint()
            self.assertEqual(job.call_args.args[:2], ("queue_find", ("a" * 16, str(Path.cwd()))))
            generation = app._fingerprint_generation
            app._handle_fingerprint_message({"type": "fingerprint_find", "generation": generation, "result": [match]})
            self.assertEqual(job.call_args.args, ("restore", match, generation))
            self.assertEqual(self.snapshot(), before)
            app._handle_fingerprint_message({"type": "fingerprint_restore", "generation": generation, "result": restored_payload()})
            self.assertFalse(app._fingerprint_busy)
            app.fingerprint_source_var.set(str(Path.cwd() / "tests"))
            app.lookup_fingerprint()
            self.assertEqual(job.call_args.args[1][1], str(Path.cwd() / "tests"))

    def test_multiple_sources_require_explicit_choice_and_cancel_is_unchanged(self):
        app = self.app
        before = self.snapshot()
        matches = [{"source_dir": str(Path.cwd() / "_synthetic_fixture" / "older"), "fingerprint": "a" * 16, "row": {}},
                   {"source_dir": str(Path.cwd() / "_synthetic_fixture" / "newer"), "fingerprint": "a" * 16, "row": {}}]
        token = app._fingerprint_generation
        with mock.patch.object(app, "_choose_fingerprint_match", return_value=None) as choose, mock.patch.object(app, "_start_fingerprint_job") as job:
            app._handle_fingerprint_message({"type": "fingerprint_find", "generation": token, "result": matches})
            choose.assert_called_once_with(matches)
            job.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        with mock.patch.object(app, "_choose_fingerprint_match", return_value=matches[0]), mock.patch.object(app, "_start_fingerprint_job") as job:
            app._handle_fingerprint_message({"type": "fingerprint_find", "generation": token, "result": matches})
            job.assert_called_once_with("restore", matches[0], token)
        self.assertEqual(self.snapshot(), before)

    def test_empty_missing_stale_and_error_results_do_not_change_parameters(self):
        app = self.app
        before = self.snapshot()
        app.fingerprint_var.set("")
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.lookup_fingerprint(); job.assert_not_called()
        token = app._fingerprint_generation
        app.fingerprint_var.set("b" * 16)
        app._handle_fingerprint_message({"type": "fingerprint_restore", "generation": token, "result": restored_payload()})
        app._handle_fingerprint_message({"type": "fingerprint_find", "generation": app._fingerprint_generation, "result": []})
        app._handle_fingerprint_message({"type": "fingerprint_error", "generation": app._fingerprint_generation,
                                        "message": "原数据已变化，无法还原"})
        self.assertEqual(self.snapshot(), before)
        self.write.assert_not_called()

    def test_dates_and_fixed_funding_are_saved_and_loaded(self):
        app = self.app
        app.apply_fingerprint_restoration(restored_payload())
        self.assertTrue(app.save_user_settings())
        payload = copy.deepcopy(self.write.call_args.args[1])
        self.assertEqual(payload["回测开始日期"], "2026-01-01T08:00:00+08:00")
        self.assertEqual(payload["回测结束日期"], "2026-02-01")
        app.start_date_var.set(""); app.end_date_var.set(""); app.funding_rate_var.set(0.0)
        path = mock.Mock()
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps(payload, ensure_ascii=False)
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(app)
        self.assertEqual(app.current_date_range(), (payload["回测开始日期"], payload["回测结束日期"]))
        self.assertAlmostEqual(app.current_selection()["资金约束"]["资金费率"], 0.0001)

    def test_run_passes_original_dates_and_never_restarts_old_result(self):
        app = self.app
        app.apply_fingerprint_restoration(restored_payload())
        future = Path.cwd() / "test-not-created-fingerprint-result"
        with mock.patch.object(app, "resolve_output_dir", return_value=future), \
                mock.patch.object(app, "validate_paths", return_value=True), \
                mock.patch.object(app, "_launch_process", return_value=False) as launch, \
                mock.patch.object(Path, "write_text") as write_file:
            app.start_run()
        cmd = launch.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--start") + 1], "2026-01-01T08:00:00+08:00")
        self.assertEqual(cmd[cmd.index("--end") + 1], "2026-02-01")
        self.assertNotIn("--restart", cmd)
        self.assertEqual(launch.call_args.args[1], future)
        self.assertFalse(future.exists())
        self.assertEqual(write_file.call_count, 2)

    def test_invalid_dates_are_rejected(self):
        for start, end in (("bad", ""), ("NaT", ""), ("2026-02-01", "2026-01-01")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.app._validate_date_range(start, end)

    def test_background_lookup_only_delivers_message_until_ui_applies_it(self):
        app = self.app
        before = self.snapshot()
        called_threads = []
        def find(fingerprint, root):
            called_threads.append(threading.get_ident())
            return [{"fingerprint": fingerprint, "source_dir": root, "row": {}}]
        while not app.messages.empty():
            app.messages.get_nowait()
        with mock.patch("fingerprint_lookup.find_fingerprint_matches", side_effect=find):
            app._start_fingerprint_job("find", ("a" * 16, str(Path.cwd())), 987)
            message = app.messages.get(timeout=3)
        self.assertNotEqual(called_threads[0], threading.get_ident())
        self.assertEqual(message["type"], "fingerprint_find")
        self.assertEqual(message["generation"], 987)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
