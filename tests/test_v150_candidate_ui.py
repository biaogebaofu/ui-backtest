"""候选方案仅改筛选设置；隐藏Tk、内存配置，不写用户设置或启动回测。"""
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest import mock

import test_v149_fingerprint_ui as fingerprint_tests
from selection_config import 规范化候选筛选


class CandidateSchemeUiTests(unittest.TestCase):
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
        app = self.app
        app.proc = None
        app._active_job_id = None
        app._closing = False
        app._settings_restore_error = False
        app.apply_fingerprint_restoration(fingerprint_tests.restored_payload())
        app.apply_candidate_settings(规范化候选筛选(None))
        self.write.reset_mock()
        self.info.reset_mock()
        self.error.reset_mock()

    def load_memory(self, config):
        payload = {"组合选择": config, "回测开始日期": "2026-01-01", "回测结束日期": "2026-02-01"}
        path = mock.Mock()
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps(payload, ensure_ascii=False)
        with mock.patch.object(self.ui, "用户设置文件", path):
            type(self).original_load(self.app)

    def test_new_defaults_and_scheme_states(self):
        app = self.app
        self.assertEqual(app.current_candidate_settings()["筛选方案"], "RETURN_DRAWDOWN")
        self.assertEqual(app.current_candidate_settings()["比较范围"], "ALL_RUN")
        self.assertEqual(app.current_candidate_settings()["资金保留比例"], .9)
        self.assertEqual(str(app.candidate_leverage_box.cget("state")), "disabled")
        self.assertEqual(str(app.capital_retention_entry.cget("state")), "normal")
        self.assertTrue(all(w.instate(["disabled"]) for w in app.candidate_legacy_controls))
        old_values = (app.p95_slippage_var.get(), app.capacity_max_var.get(), app.long_share_min_var.get())
        app.candidate_scheme_var.set("LEGACY_STRESS")
        app.candidate_scope_var.set("TARGET")
        self.assertEqual(str(app.candidate_leverage_box.cget("state")), "readonly")
        self.assertEqual(str(app.capital_retention_entry.cget("state")), "disabled")
        self.assertTrue(all(not w.instate(["disabled"]) for w in app.candidate_legacy_controls))
        self.assertEqual((app.p95_slippage_var.get(), app.capacity_max_var.get(), app.long_share_min_var.get()), old_values)
        app.candidate_enabled_var.set(False)
        self.assertEqual(str(app.candidate_leverage_box.cget("state")), "disabled")
        self.assertIn("仍可手动", app.candidate_leverage_hint.get())

    def test_legacy_settings_stay_legacy_until_explicit_preset(self):
        app = self.app
        old = 规范化候选筛选(None)
        for key in ("筛选方案", "比较范围", "资金保留比例"):
            old.pop(key)
        old.update({"启用": False, "自动导出": False, "统一目标杠杆": 10.0})
        app.apply_candidate_settings(old)
        before = app.current_selection()
        self.assertEqual(before["候选筛选"]["筛选方案"], "LEGACY_STRESS")
        self.assertEqual(before["候选筛选"]["比较范围"], "TARGET")
        dates = app.current_date_range()
        with mock.patch.object(app, "start_run") as start, mock.patch.object(app, "start_candidate_export") as export:
            app.use_return_drawdown_preset()
        after = app.current_selection()
        expected = copy.deepcopy(before)
        expected["候选筛选"].update({"筛选方案": "RETURN_DRAWDOWN", "比较范围": "ALL_RUN", "资金保留比例": .9})
        self.assertEqual(after, expected)
        self.assertEqual(app.current_date_range(), dates)
        self.assertFalse(after["候选筛选"]["启用"])
        self.assertFalse(after["候选筛选"]["自动导出"])
        start.assert_not_called(); export.assert_not_called(); self.write.assert_not_called()

    def test_running_or_pending_exit_blocks_one_click_switch(self):
        app = self.app
        app.apply_candidate_settings({"统一目标杠杆": 10.0})
        before = app.current_candidate_settings()
        for proc, job in ((mock.Mock(poll=mock.Mock(return_value=None)), None), (None, 8)):
            app.proc, app._active_job_id = proc, job
            app.use_return_drawdown_preset()
            self.assertEqual(app.current_candidate_settings(), before)
        app.proc, app._active_job_id = None, None
        self.write.assert_not_called()

    def test_only_target_range_blocks_unselected_leverage(self):
        app = self.app
        app.csv_var.set(__file__)
        for var in (app.bundle_var, app.micro_csv_var, app.funding_var, app.oi_var):
            var.set("")
        app.target_leverage_var.set(10.0)
        out = Path.cwd() / "test-v150-not-created"
        with mock.patch.object(Path, "mkdir") as mkdir, mock.patch.object(Path, "exists", return_value=False), \
                mock.patch.object(self.ui.shutil, "disk_usage", return_value=mock.Mock(free=10**12)):
            self.assertTrue(app.validate_paths(out))
            app.candidate_scope_var.set("TARGET")
            self.assertFalse(app.validate_paths(out))
            app.target_leverage_var.set(2.0)
            self.assertTrue(app.validate_paths(out))
        self.assertEqual(app.current_selection()["仓位倍数"], [2.0])

    def test_fingerprint_all_range_preserves_inactive_target_and_target_range_rejects(self):
        app = self.app
        payload = fingerprint_tests.restored_payload()
        candidate = payload["selection"]["候选筛选"]
        candidate.update({"筛选方案": "RETURN_DRAWDOWN", "比较范围": "ALL_RUN", "资金保留比例": .85,
                          "统一目标杠杆": 10.0})
        app.apply_fingerprint_restoration(payload)
        self.assertEqual(app.current_candidate_settings()["统一目标杠杆"], 10.0)
        self.assertEqual(app.current_candidate_settings()["资金保留比例"], .85)
        before = app.current_selection()
        candidate["比较范围"] = "TARGET"
        with self.assertRaisesRegex(ValueError, "杠杆"):
            app.apply_fingerprint_restoration(payload)
        self.assertEqual(app.current_selection(), before)
        self.write.assert_not_called()

    def test_save_load_new_fields_preserves_dates_costs_and_disabled_auto(self):
        app = self.app
        app.candidate_scope_var.set("ALL_RUN")
        app.target_leverage_var.set(10.0)
        app.capital_retention_var.set(87.0)
        app.candidate_auto_var.set(False)
        config = app.current_selection()
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        self.assertEqual(saved["组合选择"]["候选筛选"]["资金保留比例"], .87)
        self.load_memory(config)
        self.assertEqual(app.current_selection(), config)
        self.assertEqual(app.current_date_range(), ("2026-01-01", "2026-02-01"))
        self.assertFalse(app.candidate_auto_var.get())
        self.assertEqual(app.target_leverage_var.get(), 10.0)
        self.assertEqual(app.current_selection()["成本模式"], "FEE")
        self.assertEqual(app.current_selection()["仓位倍数"], [2.0])

    def test_load_old_settings_keeps_scheme_and_automatic_switches(self):
        config = fingerprint_tests.single_selection()
        for key in ("筛选方案", "比较范围", "资金保留比例"):
            config["候选筛选"].pop(key)
        config["候选筛选"].update({"启用": False, "自动导出": False, "统一目标杠杆": 10.0})
        self.load_memory(config)
        candidate = self.app.current_candidate_settings()
        self.assertEqual((candidate["筛选方案"], candidate["比较范围"]), ("LEGACY_STRESS", "TARGET"))
        self.assertEqual(candidate["统一目标杠杆"], 10.0)
        self.assertFalse(candidate["启用"])
        self.assertFalse(candidate["自动导出"])
        self.assertIn("已保留原严格成本预筛", self.app.log.get("1.0", "end"))

    def test_live01_names_are_display_only_and_old_labels_load(self):
        app = self.app
        label = self.ui.入场触发口径选项["LIVE_01"]
        self.assertIn("连续升/降段一次", label)
        self.assertIn("非零轴整轮/金叉死叉周期", label)
        for code in ("LIVE_01", "TF_EVENT"):
            for labels in (self.ui.原入场触发口径选项, self.ui.入场触发口径选项):
                app.selection_panel.set_entry_modes(labels[code])
                self.assertEqual(app.current_selection()["入场触发口径"], code)
                config = fingerprint_tests.single_selection()
                config["入场触发口径"] = labels[code]
                self.load_memory(config)
                self.assertEqual(app.current_selection()["入场触发口径"], code)
                self.assertEqual(app.entry_mode_var.get(), self.ui.入场触发口径选项[code])

    def test_invalid_new_fields_are_rejected_and_order_is_explicit(self):
        app = self.app
        for value in (0, 101, float("nan")):
            app.capital_retention_var.set(value)
            with self.assertRaises(ValueError):
                app.current_candidate_settings()
        app.capital_retention_var.set(90)
        hint = app.candidate_scheme_hint.get()
        self.assertLess(hint.index("先通过硬门槛"), hint.index("再保留"))
        self.assertLess(hint.index("再保留"), hint.index("最后按回撤升序"))
        self.assertIn("不达标不凑数", hint)
        self.assertIn("仍需样本外验证", hint)

    def test_new_controls_fit_minimum_and_default_windows(self):
        import tkinter.font as tkfont
        app = self.app
        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)
        app.attributes("-alpha", 0)
        if app.tk.call("tk", "windowingsystem") == "win32":
            app.attributes("-toolwindow", True)
        try:
            for width, height in ((1080, 720), (1280, 850)):
                app.geometry(f"{width}x{height}+30000+30000")
                app.deiconify()
                app.notebook.select(app.candidate_tab)
                app.update()
                for child in app.capital_retention_entry.master.winfo_children():
                    right = child.winfo_rootx() + child.winfo_width() - app.candidate_tab.winfo_rootx()
                    self.assertLessEqual(right, app.candidate_tab.winfo_width())
                app.candidate_scroll.canvas.yview_moveto(1)
                app.update()
                export = next(w for w in walk(app.candidate_tab) if "text" in w.keys()
                              and w.cget("text") == "选择已有结果目录并生成候选")
                top = export.winfo_rooty() - app.candidate_tab.winfo_rooty()
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(top + export.winfo_height(), app.candidate_tab.winfo_height())
                app.selection_panel.set_entry_modes("LIVE_01")
                app.notebook.select(app.run_tab)
                app.update()
                entry = next(w for w in walk(app.run_tab) if "textvariable" in w.keys()
                             and str(w.cget("textvariable")) == str(app.entry_mode_var))
                font = tkfont.Font(app, font=entry.cget("font"))
                self.assertLessEqual(font.measure(app.entry_mode_var.get()) + 26, entry.winfo_width())
        finally:
            app.withdraw()


if __name__ == "__main__":
    unittest.main()
