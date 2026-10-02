"""多指纹列表绑定完整原参数快照；隐藏Tk、内存保存，不启动真实工作进程。"""
import copy
import json
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path
import threading
import unittest
from unittest import mock

import test_v149_fingerprint_ui as single
import fingerprint_lookup as lookup
from ranking_view import config_fingerprint


@lru_cache(maxsize=1)
def verified_fixture():
    """Reuse a fully restored, isolated backend fixture for UI message-boundary tests."""
    from test_v149_fingerprint_lookup import FingerprintLookupTests
    fixture = FingerprintLookupTests()
    fixture.setUp()
    try:
        directory, fingerprint, _ = fixture.make_result()
        item = lookup.find_fingerprint_matches(fingerprint, directory)[0]
        return item, lookup.restore_fingerprint_match(item)
    finally:
        fixture.doCleanups()


def match(code="a", source="first", mode="LIVE_01", leverage=2.0):
    item = copy.deepcopy(verified_fixture()[0])
    item["row"].update({"入场触发口径": mode, "名义倍数（倍）": leverage,
                        "止盈后等待分钟": (int(code, 16) - 10) * 5})
    item.update(fingerprint=config_fingerprint(item["row"]), source_dir=str(Path.cwd() / source),
                summary=f"{mode} / {leverage}x")
    return item


def restoration(item):
    value = copy.deepcopy(verified_fixture()[1])
    value.update({"fingerprint": item["fingerprint"], "source_dir": item["source_dir"]})
    value["selection"]["入场触发口径"] = item["row"]["入场触发口径"]
    value["selection"]["仓位倍数"] = [item["row"]["名义倍数（倍）"]]
    value["selection"]["止盈后等待分钟"] = [item["row"]["止盈后等待分钟"]]
    return value


def bound(item):
    return lookup.bind_fingerprint_match(item, restoration(item))


class FingerprintQueueUiTests(unittest.TestCase):
    assert_nested_equal = single.FingerprintUiTests.assert_nested_equal

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import ui
        cls.ui = ui
        cls.original_load = ui.App.load_user_settings
        cls.stack = ExitStack()
        original_init, original_top = tk.Tk.__init__, tk.Toplevel.__init__
        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs); window.withdraw()
        def hidden_top(window, *args, **kwargs):
            original_top(window, *args, **kwargs); window.withdraw()
        cls.stack.enter_context(mock.patch.object(tk.Tk, "__init__", hidden_init))
        cls.stack.enter_context(mock.patch.object(tk.Toplevel, "__init__", hidden_top))
        cls.stack.enter_context(mock.patch.object(ui.App, "load_user_settings"))
        cls.stack.enter_context(mock.patch.object(ui.App, "detect_gpu"))
        cls.write = cls.stack.enter_context(mock.patch.object(ui, "atomic_json"))
        cls.info = cls.stack.enter_context(mock.patch.object(ui.messagebox, "showinfo"))
        cls.error = cls.stack.enter_context(mock.patch.object(ui.messagebox, "showerror"))
        cls.callback_error = cls.stack.enter_context(mock.patch.object(ui.App, "report_callback_exception"))
        cls.app = ui.App()
        if cls.app._poll_after_id:
            cls.app.after_cancel(cls.app._poll_after_id); cls.app._poll_after_id = None

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy(); cls.stack.close()

    def setUp(self):
        # These controller fixtures use synthetic source paths, not filesystem lookup.
        # Real deleted/restored directory checks are covered by test_v157_missing_fingerprint_sources.
        self.source_presence = mock.patch.object(self.ui, "_fingerprint_source_missing", return_value=False)
        self.source_presence.start()
        self.addCleanup(self.source_presence.stop)
        app = self.app
        app.proc = None; app.proc_kind = ""; app._active_job_id = None; app._active_output_dir = None
        app._closing = False; app._settings_restore_error = False
        app._close_fingerprint_queue()
        app.fingerprint_queue = []; app._fingerprint_queue_pending = []
        app.apply_fingerprint_restoration(single.restored_payload())
        app.out_var.set(str(Path.cwd()))
        app.fingerprint_var.set(""); app.fingerprint_source_var.set("")
        app.show_fingerprint_queue()
        while not app.messages.empty(): app.messages.get_nowait()
        for mocked in (self.write, self.info, self.error, self.callback_error): mocked.reset_mock()

    def tearDown(self):
        self.app.proc = None; self.app._active_job_id = None
        self.app._close_fingerprint_queue()
        self.callback_error.assert_not_called()

    def snapshot(self):
        app = self.app
        return (app.current_selection(), app.current_date_range(),
                [v.get() for v in (app.csv_var, app.micro_csv_var, app.funding_var, app.oi_var, app.bundle_var)],
                app.current_output_dir, app.out_var.get())

    def deliver(self, stage, result, generation=None):
        self.app._handle_fingerprint_message({"type": "fingerprint_" + stage, "result": result,
            "generation": self.app._fingerprint_generation if generation is None else generation})

    def add(self, items):
        groups = {m["fingerprint"]: [m] for m in items}
        existing = {self.app._queue_key(m): m for m in self.app.fingerprint_queue}
        pending = list(self.app.fingerprint_queue)
        pending += [m for m in items if self.app._queue_key(m) not in existing]
        with mock.patch.object(self.app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", groups)
            job.assert_called_once_with("queue_restore", pending, self.app._fingerprint_generation)
        self.deliver("queue_restore", [restoration(m) for m in pending])

    def test_two_fingerprints_join_and_sync_compatible_editor_without_starting(self):
        before = self.snapshot()
        items = [match(), match("b", "second", "MACD_CYCLE", 100.0)]
        with mock.patch.object(self.app, "start_run") as start, mock.patch.object(self.app, "_launch_process") as launch:
            self.add(items)
        self.assertEqual(self.app.fingerprint_queue, [bound(m) for m in items])
        expected = lookup.merge_fingerprint_restorations([restoration(m) for m in items])
        self.assert_nested_equal(self.app.current_selection(), expected["selection"])
        self.assertEqual(self.app.run_target_var.get(), "EDITOR")
        self.assertEqual(self.app.out_var.get(), before[-1])
        self.assertEqual(len(self.app.queue_tree.get_children()), 2)
        self.assertNotIn("selection", self.app.fingerprint_queue[0])
        self.assertIn("verified_snapshot", self.app.fingerprint_queue[0])
        start.assert_not_called(); launch.assert_not_called(); self.write.assert_not_called()

    def test_same_fingerprint_source_deduplicates_but_other_source_survives(self):
        first = match()
        self.add([first]); self.add([copy.deepcopy(first)])
        other = match(source="other-date")
        self.add([other])
        self.assertEqual(self.app.fingerprint_queue, [bound(first), bound(other)])
        self.assertEqual(len(self.app.queue_tree.get_children()), 2)

    def test_any_missing_or_restore_failure_adds_nothing(self):
        original = match("c", "existing")
        self.add([original])
        before = self.snapshot()
        with mock.patch.object(self.app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {"a" * 16: [match()], "b" * 16: []})
            job.assert_not_called()
            self.deliver("queue_find", {"a" * 16: [match()]})
        self.app._handle_fingerprint_message({"type": "fingerprint_error", "generation": self.app._fingerprint_generation,
                                               "message": "second original data failed validation"})
        self.assertEqual(self.app.fingerprint_queue, [bound(original)])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.error.call_count, 2)

    def test_partial_or_mismatched_restore_is_atomic(self):
        items = [match(), match("b", "second")]
        self.app._fingerprint_queue_pending = items
        for results in ([restoration(items[0])], [restoration(items[0]), restoration(match("b", "wrong-source"))]):
            self.deliver("queue_restore", results)
            self.assertEqual(self.app.fingerprint_queue, [])

    def test_generation_changes_cancel_and_close_discard_old_results(self):
        app = self.app
        generation = app._fingerprint_generation
        app.fingerprint_source_var.set(str(Path.cwd() / "tests"))
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {"a" * 16: [match()]}, generation)
            job.assert_not_called()
            with mock.patch.object(app, "_choose_fingerprint_match", return_value=None):
                self.deliver("queue_find", {"a" * 16: [match()], "b" * 16: [match("b"), match("b", "other")]})
                job.assert_not_called()
        generation = app._fingerprint_generation
        app._fingerprint_queue_pending = [match()]
        app._close_fingerprint_queue()
        self.deliver("queue_restore", [restoration(match())], generation)
        self.assertEqual(app.fingerprint_queue, [])

    def test_text_change_invalidates_in_flight_queue_result(self):
        app = self.app
        app.queue_input.insert("1.0", "a" * 16)
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.lookup_fingerprint_queue()
            generation = app._fingerprint_generation
            self.assertEqual(job.call_args.args[0], "queue_find")
        app.queue_input.insert("end", "\n" + "b" * 16)
        app.queue_input.event_generate("<<Modified>>")
        self.assertGreater(app._fingerprint_generation, generation)
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {"a" * 16: [match()]}, generation)
            job.assert_not_called()

    def test_repeated_or_delimited_single_input_uses_normalized_fingerprint(self):
        app = self.app
        for text in ("a" * 16 + ", ", "a" * 16 + " " + "a" * 16):
            app.fingerprint_var.set(text)
            with mock.patch.object(app, "_start_fingerprint_job") as job:
                app.lookup_fingerprint()
                self.assertEqual(job.call_args.args[:2], ("queue_find", ("a" * 16, str(Path.cwd()))))

    def test_queue_background_stages_only_deliver_messages(self):
        import fingerprint_lookup
        app = self.app
        before = self.snapshot()
        worker_threads = []
        def find(text, root):
            worker_threads.append(threading.get_ident())
            return {"a" * 16: [match()]}
        def restore(items, progress=None):
            worker_threads.append(threading.get_ident())
            progress("正在核验指纹 1/1")
            return [restoration(m) for m in items]
        with mock.patch.object(fingerprint_lookup, "find_fingerprint_groups", side_effect=find), \
                mock.patch.object(fingerprint_lookup, "restore_many", side_effect=restore):
            app._start_fingerprint_job("queue_find", ("a" * 16, str(Path.cwd())), 91)
            found = app.messages.get(timeout=5)
            app._start_fingerprint_job("queue_restore", [match()], 92)
            progress = app.messages.get(timeout=5)
            restored = app.messages.get(timeout=5)
        self.assertEqual((found["type"], found["generation"]), ("fingerprint_queue_find", 91))
        self.assertEqual((progress["type"], progress["generation"]), ("fingerprint_progress", 92))
        self.assertEqual((restored["type"], restored["generation"]), ("fingerprint_queue_restore", 92))
        self.assertTrue(all(t != threading.get_ident() for t in worker_threads))
        self.assertEqual(app.fingerprint_queue, [])
        self.assertEqual(self.snapshot(), before)

    def test_main_multiple_and_single_paste_both_add_without_dialog_or_start(self):
        app = self.app
        before = self.snapshot()
        app._close_fingerprint_queue()
        app.fingerprint_var.set("a" * 16 + "; " + "b" * 16)
        with mock.patch.object(app, "_start_fingerprint_job") as job, mock.patch.object(app, "start_fingerprint_batch") as run:
            app.lookup_fingerprint()
            self.assertIsNone(app._fingerprint_queue_dialog)
            self.assertEqual(job.call_args.args[0], "queue_find")
            self.assertIn("a" * 16, job.call_args.args[1][0])
            self.assertIn("b" * 16, job.call_args.args[1][0])
            run.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        app._close_fingerprint_queue(); app.fingerprint_var.set("a" * 16)
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.lookup_fingerprint()
            self.assertEqual(job.call_args.args[0], "queue_find")
            self.deliver("queue_find", {match()["fingerprint"]: [match()]})
            self.assertEqual(job.call_args.args[0], "queue_restore")
        with mock.patch.object(app, "apply_fingerprint_restoration") as apply:
            self.deliver("queue_restore", [restoration(match())])
            apply.assert_called_once()
            self.assertTrue(apply.call_args.kwargs["allow_multiple"])
        self.assertEqual(app.fingerprint_queue, [bound(match())])

    def test_remove_selected_and_clear_preserve_current_settings(self):
        app = self.app
        items = [match(), match("b", "second"), match("c", "third")]
        self.add(items); before = self.snapshot()
        app.queue_tree.selection_set("1")
        app.remove_queued_fingerprints()
        self.assertEqual(app.fingerprint_queue, [bound(items[0]), bound(items[2])])
        app.remove_queued_fingerprints(clear=True)
        self.assertEqual(app.fingerprint_queue, [])
        self.assertEqual(self.snapshot(), before)

    def test_batch_manifest_is_exact_original_matches_cpu_sequential_not_current_ui(self):
        app = self.app
        items = [match(mode="LIVE_01", leverage=2.0), match("b", "second", "MACD_CYCLE", 100.0)]
        self.add(items)
        app.selection_panel.set_entry_modes("TF_EVENT")
        app.initial_capital_var.set(9999)
        app.start_date_var.set("2026-07-01"); app.end_date_var.set("2026-07-02")
        app.thread_var.set(1)
        before = (app.current_selection(), app.current_date_range())
        original_out = Path(items[0]["source_dir"])
        app.current_output_dir = original_out
        with mock.patch.object(Path, "mkdir"), mock.patch.object(Path, "exists", return_value=False), \
                mock.patch.object(app, "_launch_process", return_value=True) as launch:
            app.start_fingerprint_batch()
        manifest_path, manifest = self.write.call_args.args
        self.assertEqual(manifest, {"version": 1, "strategies": [bound(m) for m in items]})
        self.assertEqual((app.current_selection(), app.current_date_range()), before)
        self.assertNotEqual(manifest_path.parent, original_out)
        self.assertEqual(manifest_path.parent.parent, Path.cwd())
        cmd, output, kind = launch.call_args.args
        self.assertEqual(kind, "fingerprint_batch")
        self.assertEqual(output, manifest_path.parent)
        self.assertEqual(cmd[cmd.index("--manifest") + 1], str(manifest_path))
        self.assertEqual(cmd[cmd.index("--device") + 1], "cpu")
        self.assertEqual(cmd[cmd.index("--threads") + 1], "1")
        self.assertNotIn("--selection", cmd); self.assertNotIn("--restart", cmd)
        self.assertIsNone(app._fingerprint_queue_dialog)

    def test_running_blocks_queue_mutations_and_stop_uses_batch_control(self):
        app = self.app
        self.add([match()]); before = copy.deepcopy(app.fingerprint_queue)
        app.queue_tree.selection_set("0")
        for proc, job in ((mock.Mock(poll=mock.Mock(return_value=None)), None), (None, 71)):
            app.proc, app._active_job_id = proc, job
            with mock.patch.object(app, "_start_fingerprint_job") as lookup, mock.patch.object(app, "_launch_process") as launch:
                app.remove_queued_fingerprints(); app.remove_queued_fingerprints(clear=True)
                app.lookup_fingerprint_queue(); app.start_fingerprint_batch()
                lookup.assert_not_called(); launch.assert_not_called()
            self.assertEqual(app.fingerprint_queue, before)
        app.proc = mock.Mock(poll=mock.Mock(return_value=None))
        app.proc_kind = "fingerprint_batch"
        app._active_output_dir = Path.cwd() / "uncreated-test-batch"
        with mock.patch.object(Path, "mkdir"), mock.patch.object(Path, "write_text", autospec=True) as write_flag:
            app.stop_run()
        self.assertEqual(write_flag.call_args.args[0], app._active_output_dir / "控制" / "停止.flag")
        self.assertNotIn("断点续跑", app.status_var.get())

    def test_queue_persists_bound_matches_and_load_does_not_restore_or_run(self):
        app = self.app
        items = [match(), match("b", "second", "MACD_CYCLE", 100.0)]
        self.add(items)
        app.run_target_var.set("FINGERPRINTS"); app._sync_run_target()
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        self.assertEqual(saved["多指纹策略列表"], [bound(m) for m in items])
        self.assertEqual(saved["运行对象"], "FINGERPRINTS")
        app.fingerprint_queue = []
        path = mock.Mock(is_file=mock.Mock(return_value=True), read_text=mock.Mock(return_value=json.dumps(saved)))
        with mock.patch.object(self.ui, "用户设置文件", path), mock.patch.object(app, "_start_fingerprint_job") as lookup, \
                mock.patch.object(app, "_launch_process") as launch:
            type(self).original_load(app)
        self.assertEqual(app.fingerprint_queue, [bound(m) for m in items])
        self.assertEqual(app.run_target_var.get(), "FINGERPRINTS")
        lookup.assert_not_called(); launch.assert_not_called()
        self.assertEqual(len(app.queue_tree.get_children()), 2)

    def test_popup_controls_and_long_sources_are_reachable_at_850_and_1000(self):
        app = self.app
        self.add([match(source="长目录" * 50)])
        dialog = app._fingerprint_queue_dialog
        app.attributes("-alpha", 0)
        if app.tk.call("tk", "windowingsystem") == "win32":
            app.attributes("-toolwindow", True)
        app.geometry("1080x720+30000+30000"); app.deiconify()
        dialog.attributes("-alpha", 0)
        if dialog.tk.call("tk", "windowingsystem") == "win32":
            dialog.attributes("-toolwindow", True)
        try:
            for width, height in ((850, 500), (1000, 570)):
                dialog.geometry(f"{width}x{height}+30000+30000"); dialog.deiconify(); app.update()
                for widget in (app.queue_input, app.queue_lookup_button, app.queue_start_button):
                    right = widget.winfo_rootx() + widget.winfo_width() - dialog.winfo_rootx()
                    bottom = widget.winfo_rooty() + widget.winfo_height() - dialog.winfo_rooty()
                    self.assertLessEqual(right, width); self.assertLessEqual(bottom, height)
                    self.assertTrue(widget.winfo_ismapped())
                self.assertTrue(app.queue_tree.cget("xscrollcommand"))
                self.assertTrue(app.queue_tree.cget("yscrollcommand"))
                app.queue_tree.xview_moveto(1); app.update()
                self.assertGreater(app.queue_tree.xview()[0], 0)
                self.assertEqual(app.queue_tree.item("0", "values")[-1], app.fingerprint_queue[0]["source_dir"])
        finally:
            dialog.withdraw(); app.withdraw()


if __name__ == "__main__":
    unittest.main()
