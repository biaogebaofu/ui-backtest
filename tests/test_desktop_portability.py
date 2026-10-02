"""Desktop paths, small displays, wheel events and macOS resource checks."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import platform_support as desktop


class DesktopHelpersTests(unittest.TestCase):
    def test_settings_are_per_user_on_each_supported_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for platform, suffix in (
                    ("win32", ("AppData", "Local", "ETHBacktest")),
                    ("darwin", ("Library", "Application Support", "ETHBacktest")),
                    ("linux", (".config", "ETHBacktest"))):
                with self.subTest(platform=platform), mock.patch.object(desktop.sys, "platform", platform), \
                        mock.patch.object(desktop.Path, "home", return_value=home), \
                        mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(desktop.user_data_dir(), home.joinpath(*suffix))
                    self.assertEqual(desktop.default_output_dir(), home / "ETHBacktest" / "results")
                    self.assertFalse(desktop.user_data_dir().exists())

    def test_platform_configuration_directories_are_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for platform, variable in (("win32", "LOCALAPPDATA"), ("linux", "XDG_CONFIG_HOME")):
                with self.subTest(platform=platform), mock.patch.object(desktop.sys, "platform", platform), \
                        mock.patch.dict(os.environ, {variable: str(base)}, clear=True):
                    self.assertEqual(desktop.user_data_dir(), base / "ETHBacktest")

    def test_large_window_and_minimum_fit_a_small_screen(self):
        window = mock.Mock()
        window.winfo_screenwidth.return_value = 800
        window.winfo_screenheight.return_value = 600
        desktop.fit_window(window, 1280, 850, minimum=(960, 600))
        window.geometry.assert_called_once_with("720x520")
        window.minsize.assert_called_once_with(720, 520)

    def test_relative_environment_path_does_not_write_into_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(desktop.sys, "platform", "linux"), \
                    mock.patch.object(desktop.Path, "home", return_value=home), \
                    mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative-settings"}, clear=True):
                self.assertEqual(desktop.user_data_dir(), home / ".config" / "ETHBacktest")

    def test_explicit_test_home_isolates_settings_and_results(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"UI_BACKTEST_HOME": directory}):
            home = Path(directory).resolve()
            self.assertEqual(desktop.user_data_dir(), home / "settings")
            self.assertEqual(desktop.default_output_dir(), home / "results")

    def test_wheel_supports_windows_macos_and_linux_events(self):
        for delta, button, units in ((120, None, -3), (-240, None, 6),
                                     (1, None, -3), (-1, None, 3),
                                     (0, 4, -3), (0, 5, 3), (0, None, 0)):
            with self.subTest(delta=delta, button=button):
                self.assertEqual(desktop.wheel_units(SimpleNamespace(delta=delta, num=button)), units)

    def test_missing_windows_font_uses_the_native_font(self):
        import tkinter.font as fonts
        native = mock.Mock()
        native.actual.return_value = "Native UI Font"
        with mock.patch.object(fonts, "families", return_value=("Native UI Font",)), \
                mock.patch.object(fonts, "nametofont", return_value=native):
            self.assertEqual(desktop.ui_font_family(mock.Mock()), "Native UI Font")

    def test_macos_counts_page_size_without_wired_or_compressed_memory(self):
        for page_size in (4096, 16384):
            stats = (f"Mach Virtual Memory Statistics: (page size of {page_size} bytes)\n"
                     "Pages free: 100.\nPages inactive: 200.\nPages speculative: 30.\n"
                     "Pages wired down: 999999.\nPages occupied by compressor: 999999.\n")
            with self.subTest(page_size=page_size), \
                    mock.patch.object(desktop.subprocess, "run", return_value=SimpleNamespace(stdout=stats)) as run:
                self.assertEqual(desktop.macos_available_memory(), 330 * page_size)
                self.assertEqual(run.call_args.args[0], ["/usr/bin/vm_stat"])
                self.assertEqual(run.call_args.kwargs["timeout"], 5)
                self.assertTrue(run.call_args.kwargs["check"])

    def test_macos_missing_statistics_or_command_failure_stops_startup(self):
        for result in (SimpleNamespace(stdout="not vm_stat"), FileNotFoundError(),
                       subprocess.TimeoutExpired("vm_stat", 5)):
            with self.subTest(result=type(result).__name__):
                arguments = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch.object(desktop.subprocess, "run", **arguments):
                    with self.assertRaisesRegex(RuntimeError, "macOS"):
                        desktop.macos_available_memory()

    def test_campaign_scheduler_uses_macos_memory_probe(self):
        import campaign_runner
        with mock.patch.object(campaign_runner.sys, "platform", "darwin"), \
                mock.patch.object(campaign_runner, "macos_available_memory", return_value=12345):
            self.assertEqual(campaign_runner.available_memory(), 12345)

    def test_macos_precompute_budget_uses_actual_available_memory(self):
        import fifth_precompute as precompute
        free_bytes = 3 * 1024**3
        with mock.patch.object(precompute.sys, "platform", "darwin"), \
                mock.patch.object(precompute, "macos_available_memory", return_value=free_bytes), \
                mock.patch.object(precompute.os, "cpu_count", return_value=18):
            self.assertEqual(precompute.available_memory(), free_bytes)
            self.assertEqual(precompute.worker_limit(18, 100, 1505), 1)

    def test_macos_precompute_probe_failure_does_not_assume_four_gib(self):
        import fifth_precompute as precompute
        with mock.patch.object(precompute.sys, "platform", "darwin"), \
                mock.patch.object(precompute, "macos_available_memory", side_effect=RuntimeError("probe failed")):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                precompute.worker_limit(18, 100, 1505)


def has_display():
    try:
        import tkinter as tk
        window = tk.Tk()
        window.withdraw()
        window.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(has_display(), "需要 tkinter 和可用显示")
class DesktopTimerLifecycleTests(unittest.TestCase):
    def test_manual_poll_keeps_one_timer_and_destroy_cancels_it(self):
        from ui import App
        from hedge_panel import HedgePanel
        with mock.patch.object(App, "load_user_settings"), \
                mock.patch.object(App, "detect_gpu"), \
                mock.patch.object(HedgePanel, "load_settings"):
            app = App()
            app.withdraw()
            destroyed = False
            try:
                for _ in range(3):
                    app.poll_messages()
                    pending = app.tk.splitlist(app.tk.call("after", "info"))
                    self.assertEqual(len(pending), 1)
                    self.assertEqual(pending[0], app._poll_after_id)
                app.destroy()
                destroyed = True
                self.assertEqual(app.tk.splitlist(app.tk.call("after", "info")), ())
            finally:
                if not destroyed:
                    app.destroy()


@unittest.skipUnless(has_display(), "需要 tkinter 和可用显示")
class DesktopUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ui
        from hedge_panel import HedgePanel
        cls.ui = ui
        cls.stack = ExitStack()
        temporary = tempfile.TemporaryDirectory(prefix="desktop_ui_")
        cls.stack.callback(temporary.cleanup)
        cls.root = Path(temporary.name)
        cls.stack.enter_context(mock.patch.object(ui, "用户设置文件", cls.root / "settings" / "用户设置.json"))
        cls.stack.enter_context(mock.patch.object(ui.App, "load_user_settings"))
        cls.stack.enter_context(mock.patch.object(ui.App, "detect_gpu"))
        cls.stack.enter_context(mock.patch.object(HedgePanel, "load_settings"))
        cls.stack.enter_context(mock.patch.object(HedgePanel, "save_settings"))
        cls.app = ui.App()
        cls.app.withdraw()
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.stack.close()

    def test_new_install_has_no_private_input_and_can_save_outside_checkout(self):
        self.assertEqual(self.app.csv_var.get(), "")
        self.assertEqual(self.app.out_var.get(), str(desktop.default_output_dir()))
        self.app.save_user_settings()
        self.assertTrue(self.ui.用户设置文件.is_file())
        self.assertFalse(self.ui.用户设置文件.is_relative_to(self.ui.项目目录))
        self.assertFalse(self.app.hedge_panel.settings_path.is_relative_to(self.ui.项目目录))
        with mock.patch.object(self.ui, "user_data_dir", return_value=self.root / "logs"), \
                mock.patch.object(self.ui.messagebox, "showerror"):
            self.app.report_callback_exception(ValueError, ValueError("synthetic UI error"), None)
        log = self.root / "logs" / "UI错误日志.txt"
        self.assertIn("synthetic UI error", log.read_text(encoding="utf-8"))

    def test_small_window_keeps_run_and_export_buttons_visible(self):
        app = self.app
        app.deiconify()
        app.geometry("960x600+30000+30000")
        app.notebook.select(2)
        app.update()
        app.update_idletasks()
        try:
            for button in (app.start_btn, app.pause_btn, app.stop_btn, app.candidate_btn, app.worst_btn):
                with self.subTest(button=button.cget("text")):
                    self.assertTrue(button.winfo_ismapped())
                    right = button.winfo_rootx() + button.winfo_width() - app.winfo_rootx()
                    bottom = button.winfo_rooty() + button.winfo_height() - app.winfo_rooty()
                    self.assertLessEqual(right, app.winfo_width())
                    self.assertLessEqual(bottom, app.winfo_height())
            self.assertIsNot(app.start_btn.master, app.candidate_btn.master)
        finally:
            app.withdraw()

    def test_entry_panel_has_linux_wheel_bindings(self):
        panel = self.app.selection_panel
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.assertTrue(panel.entry_tab.bind(event))


if __name__ == "__main__":
    unittest.main(verbosity=2)
