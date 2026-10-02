"""精确列表与编辑组合分开：隐藏Tk、隔离行情夹具，不写用户设置或启动回测。"""
import copy
import json
from pathlib import Path
import time
import unittest
from unittest import mock

import fingerprint_lookup as lookup
import test_v152_fingerprint_queue_ui as queue


class ExactListUiTests(unittest.TestCase):
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

    snapshot = queue.FingerprintQueueUiTests.snapshot
    deliver = queue.FingerprintQueueUiTests.deliver
    def add(self, items):
        queue.FingerprintQueueUiTests.add(self, items)
        # v1.54添加保留运行对象；这些测试明确选择精确列表后验证其运行与显示。
        self.app.run_target_var.set("FINGERPRINTS")
        self.app._sync_run_target()

    def state(self):
        return (copy.deepcopy(self.app.fingerprint_queue), self.app.run_target_var.get(),
                self.app.run_target_hint_var.get(), self.snapshot())

    def test_two_leverages_visible_in_explicit_list_and_synced_editor(self):
        app = self.app
        app.selection_panel.size_vars[2.0].set(False)
        app.selection_panel.size_vars[10.0].set(True)
        for left, right in ((2.0, 100.0), (9.0, 10.0)):
            app.fingerprint_queue = []
            items = [queue.match(leverage=left), queue.match("b", "second", "MACD_CYCLE", right)]
            with mock.patch.object(app, "_launch_process") as launch:
                self.add(items)
            expected = lookup.merge_fingerprint_restorations([queue.restoration(m) for m in items])
            self.assert_nested_equal(app.current_selection(), expected["selection"])
            self.assertEqual(app.run_target_var.get(), "FINGERPRINTS")
            self.assertEqual(app.selection_panel.winfo_manager(), "")
            self.assertEqual(app.fingerprint_batch_view.winfo_manager(), "pack")
            self.assertEqual([app.batch_tree.set(i, "名义倍数") for i in app.batch_tree.get_children()],
                             [f"{left:g}x", f"{right:g}x"])
            self.assertIn("2条", app.selection_summary_var.get())
            self.assertIn(f"{left:g}x", app.selection_summary_var.get())
            self.assertIn(f"{right:g}x", app.selection_summary_var.get())
            self.assertIn("2条", app.start_btn.cget("text"))
            self.assertIsNone(app._fingerprint_queue_dialog)
            launch.assert_not_called()
        self.write.assert_not_called()

    def test_primary_start_uses_exact_two_bound_matches_and_never_reads_editor(self):
        app = self.app
        self.add([queue.match(), queue.match("b", "second", "MACD_CYCLE", 100.0)])
        expected = copy.deepcopy(app.fingerprint_queue)
        app.thread_var.set(1)
        app.current_output_dir = Path(expected[0]["source_dir"])
        with mock.patch.object(app, "current_selection", side_effect=AssertionError("must not read editor")), \
                mock.patch.object(app, "current_date_range", side_effect=AssertionError("must not read editor dates")), \
                mock.patch.object(app, "validate_paths", side_effect=AssertionError("must not validate editor paths")), \
                mock.patch.object(Path, "mkdir"), mock.patch.object(Path, "exists", return_value=False), \
                mock.patch.object(app, "_launch_process", return_value=True) as launch:
            app.start_btn.invoke()
        manifest_path, manifest = self.write.call_args.args
        self.assertEqual(manifest, {"version": 1, "strategies": expected})
        self.assertEqual(len(manifest["strategies"]), 2)
        self.assertTrue(all(manifest_path.parent != Path(item["source_dir"]) for item in expected))
        cmd, output, kind = launch.call_args.args
        self.assertEqual(kind, "fingerprint_batch")
        self.assertEqual(output, manifest_path.parent)
        self.assertEqual(cmd[cmd.index("--device") + 1], "cpu")
        self.assertEqual(cmd[cmd.index("--threads") + 1], "1")
        self.assertNotIn("--selection", cmd)

    def test_explicit_editor_target_does_not_run_nonempty_list(self):
        app = self.app
        self.add([queue.match()])
        app.run_target_var.set("EDITOR"); app._sync_run_target()
        self.assertEqual(app.selection_panel.winfo_manager(), "pack")
        self.assertEqual(app.fingerprint_batch_view.winfo_manager(), "")
        with mock.patch.object(app, "start_fingerprint_batch") as batch, \
                mock.patch.object(app, "resolve_output_dir", return_value=Path.cwd()) as out, \
                mock.patch.object(app, "validate_paths", return_value=False) as validate:
            app.start_run()
        batch.assert_not_called(); out.assert_called_once(); validate.assert_called_once()

    def test_primary_comma_space_input_adds_directly_without_second_dialog_or_start(self):
        app = self.app
        first, second = queue.match(), queue.match("b", "second")
        for separator in (", ", " ", "\n"):
            app.fingerprint_var.set(first["fingerprint"] + separator + second["fingerprint"])
            with mock.patch.object(app, "_start_fingerprint_job") as job, \
                    mock.patch.object(app, "show_fingerprint_queue") as dialog, \
                    mock.patch.object(app, "_launch_process") as launch:
                app.lookup_fingerprint()
            self.assertEqual(job.call_args.args[0], "queue_find")
            self.assertEqual(lookup.parse_fingerprints(job.call_args.args[1][0]),
                             [first["fingerprint"], second["fingerprint"]])
            dialog.assert_not_called(); launch.assert_not_called()
        self.assertEqual(app.fingerprint_queue, [])
        self.assertEqual(app.run_target_var.get(), "EDITOR")

    def test_single_paste_in_list_target_appends_and_syncs_without_switching_target(self):
        app = self.app
        self.add([queue.match()])
        second = queue.match("b", "second")
        app.fingerprint_var.set(second["fingerprint"] + ", ")
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.lookup_fingerprint()
            self.assertEqual(job.call_args.args[0], "queue_find")
            self.deliver("queue_find", {second["fingerprint"]: [second]})
            restored = [queue.restoration(item) for item in app._fingerprint_queue_pending]
            self.deliver("queue_restore", restored)
        self.assertEqual(len(app.fingerprint_queue), 2)
        self.assert_nested_equal(app.current_selection(), lookup.merge_fingerprint_restorations(restored)["selection"])
        self.assertEqual(app.run_target_var.get(), "FINGERPRINTS")

    def test_readonly_details_display_each_full_original_configuration_and_dates(self):
        app = self.app
        self.add([queue.match(), queue.match("b", "second", "MACD_CYCLE", 100.0)])
        before = self.state()
        with mock.patch.object(app, "apply_fingerprint_restoration") as apply, \
                mock.patch.object(app, "start_run") as start:
            for index, item in enumerate(app.fingerprint_queue):
                app.batch_tree.selection_set(str(index)); app._show_batch_details()
                values = dict(app.batch_details.item(i, "values") for i in app.batch_details.get_children())
                selection = item["verified_snapshot"]["selection"]
                def assert_leaves(raw, prefix=""):
                    for key, value in raw.items():
                        name = f"{prefix} / {key}" if prefix else key
                        if isinstance(value, dict):
                            assert_leaves(value, name)
                        else:
                            actual = next(v for k, v in values.items() if k.endswith(" / " + name))
                            expected = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)
                            self.assertEqual(actual, expected)
                assert_leaves(selection)
                self.assertIn(item["verified_snapshot"]["start"], values.values())
                self.assertIn(item["verified_snapshot"]["end"], values.values())
                self.assertIn(item["source_dir"], values.values())
                self.assertTrue(app.batch_details.cget("xscrollcommand"))
                self.assertTrue(app.batch_details.cget("yscrollcommand"))
        apply.assert_not_called(); start.assert_not_called()
        self.assertEqual(self.state(), before)
        self.write.assert_not_called()

    def test_failed_missing_cancelled_and_stale_results_leave_target_and_queue_unchanged(self):
        app = self.app
        self.add([queue.match()])
        app.run_target_var.set("EDITOR"); app._sync_run_target()
        before = self.state()
        second, other = queue.match("b", "second"), queue.match("b", "different-source")
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {second["fingerprint"]: []})
            job.assert_not_called()
            with mock.patch.object(app, "_choose_fingerprint_match", return_value=None):
                self.deliver("queue_find", {second["fingerprint"]: [second, other]})
                job.assert_not_called()
            self.deliver("queue_find", {second["fingerprint"]: [second]})
        app._handle_fingerprint_message({"type": "fingerprint_error", "generation": app._fingerprint_generation,
                                        "message": "原数据变更"})
        generation = app._fingerprint_generation
        app.fingerprint_var.set(second["fingerprint"])
        self.deliver("queue_restore", [queue.restoration(second)], generation)
        self.assertEqual(self.state(), before)

    def test_duplicate_revalidation_keeps_original_snapshot_and_rejects_changed_hidden_parameter(self):
        app = self.app
        item = queue.match()
        self.add([item])
        before = self.state()
        fresh = copy.deepcopy(item)
        fresh["summary"] = "new source summary must not replace pinned original"
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {item["fingerprint"]: [fresh]})
            self.assertEqual(job.call_args.args[1], before[0])
        changed = queue.restoration(item)
        changed["selection"]["入场约束"]["最小S3距离"] += 0.001
        self.deliver("queue_restore", [changed])
        self.assertEqual(self.state(), before)
        self.assertIn("变化", self.error.call_args.args[1])
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.revalidate_fingerprint_queue()
            self.assertEqual(job.call_args.args[:2], ("queue_restore", before[0]))
        self.deliver("queue_restore", [queue.restoration(item)])
        self.assertEqual(self.state(), before)

    def test_legacy_unbound_record_cannot_start_until_explicit_revalidation(self):
        app = self.app
        item = queue.match()
        app.fingerprint_queue = [item]
        app.run_target_var.set("FINGERPRINTS"); app._refresh_fingerprint_queue()
        self.assertEqual(app.batch_tree.set("0", "核验状态"), "待重新核验")
        with mock.patch.object(app, "_launch_process") as launch:
            app.start_run()
        launch.assert_not_called(); self.write.assert_not_called()
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.revalidate_fingerprint_queue()
            self.assertEqual(job.call_args.args[:2], ("queue_restore", [item]))
        self.deliver("queue_restore", [queue.restoration(item)])
        self.assertEqual(app.fingerprint_queue, [queue.bound(item)])
        self.assertEqual(app.batch_tree.set("0", "核验状态"),
                         queue.bound(item).get("definition_status", "已绑定参数"))

    def test_removing_last_item_keeps_empty_list_target_and_cannot_fall_back_to_editor(self):
        app = self.app
        self.add([queue.match(), queue.match("b", "second")])
        app.batch_tree.selection_set("0")
        app.remove_queued_fingerprints(tree=app.batch_tree)
        self.assertEqual(len(app.fingerprint_queue), 1)
        app.remove_queued_fingerprints(clear=True, tree=app.batch_tree)
        self.assertEqual(app.run_target_var.get(), "FINGERPRINTS")
        self.assertEqual(app.fingerprint_queue, [])
        self.assertEqual(app.batch_details.get_children(), ())
        self.assertIn("列表为空", app.run_target_hint_var.get())
        with mock.patch.object(app, "current_selection", side_effect=AssertionError("must not fall back")), \
                mock.patch.object(app, "_launch_process") as launch:
            app.start_run()
        launch.assert_not_called(); self.write.assert_not_called()

    def test_running_prevents_target_switch_revalidation_and_list_removal(self):
        app = self.app
        self.add([queue.match()]); before = self.state()
        app._active_job_id = 99
        app.run_target_var.set("EDITOR"); app._sync_run_target()
        with mock.patch.object(app, "_start_fingerprint_job") as job, mock.patch.object(app, "start_fingerprint_batch") as batch:
            app.revalidate_fingerprint_queue()
            app.remove_queued_fingerprints(clear=True, tree=app.batch_tree)
            app.start_run()
        job.assert_not_called(); batch.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_run_page_hides_editor_controls_but_preserves_values_folds_cpu_and_output(self):
        app = self.app
        queue.FingerprintQueueUiTests.add(self, [queue.match()])
        original_folds = {key: var.get() for key, var in app.fold_vars.items()}
        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)
        shared = [next(widget for widget in walk(app.run_tab)
                       if "textvariable" in widget.keys() and str(widget.cget("textvariable")) == str(var))
                  for var in (app.thread_var, app.out_var)]
        app.attributes("-alpha", 0); app.attributes("-toolwindow", True)
        try:
            app.geometry("1080x720+30000+30000"); app.deiconify()
            for folded in (False, True):
                app.run_target_var.set("EDITOR")
                for key in ("数据与输出", "运行设置"):
                    app.fold_vars[key].set(False)
                for key in ("资金与下单约束", "全仓风险与入场闸门"):
                    app.fold_vars[key].set(folded)
                app._sync_run_target()
                app.notebook.select(app.run_tab); app.update()
                preferences = {key: var.get() for key, var in app.fold_vars.items()}
                before = (self.snapshot(), app.thread_var.get(), app.force_var.get(),
                          app.separate_output_var.get(), app.device_var.get())
                geometry = {widget: widget.grid_info() for widget in app._editor_run_controls}
                self.assertTrue(geometry)
                self.assertTrue(all(widget.winfo_manager() == "grid" for widget in geometry))
                app.run_target_var.set("FINGERPRINTS"); app._sync_run_target()
                app.notebook.select(app.run_tab); app.update()
                for widget in app._editor_run_controls:
                    self.assertEqual(widget.winfo_manager(), "")
                    self.assertFalse(widget.winfo_ismapped())
                self.assertFalse(app.min_reentry_minutes_box.winfo_ismapped())
                for key in ("资金与下单约束", "全仓风险与入场闸门"):
                    self.assertEqual(app._fold_boxes[key].winfo_manager(), "")
                for widget in shared:
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertFalse(widget.instate(["disabled"]))
                self.assertEqual({key: var.get() for key, var in app.fold_vars.items()}, preferences)
                app.run_target_var.set("EDITOR"); app._sync_run_target(); app.update()
                for widget, grid in geometry.items():
                    self.assertEqual(widget.grid_info(), grid)
                    self.assertTrue(widget.winfo_ismapped())
                for key in ("资金与下单约束", "全仓风险与入场闸门"):
                    self.assertEqual(bool(app._fold_boxes[key].winfo_manager()), not folded)
                self.assertTrue(app.min_reentry_minutes_box.winfo_ismapped())
                self.assertEqual({key: var.get() for key, var in app.fold_vars.items()}, preferences)
                self.assertEqual((self.snapshot(), app.thread_var.get(), app.force_var.get(),
                                  app.separate_output_var.get(), app.device_var.get()), before)
            self.write.assert_not_called()
        finally:
            for key, value in original_folds.items():
                app.fold_vars[key].set(value)
            app._apply_folds()
            app.withdraw()

    def test_real_backend_restorations_are_bound_and_changed_source_does_not_replace_them(self):
        from test_v149_fingerprint_lookup import FingerprintLookupTests
        fixture = FingerprintLookupTests()
        fixture.setUp()
        try:
            first, fp1, _ = fixture.make_result("first", mode="FEE")
            second, fp2, _ = fixture.make_result("second", mode="SLIPPAGE", s3=.004)
            items = [lookup.find_fingerprint_matches(fp, source)[0] for fp, source in ((fp1, first), (fp2, second))]
            with mock.patch.object(self.app, "_start_fingerprint_job"):
                self.deliver("queue_find", {item["fingerprint"]: [item] for item in items})
            self.deliver("queue_restore", lookup.restore_many(items))
            before = self.state()
            self.assertEqual(len(before[0]), 2)
            self.assertEqual([item["verified_snapshot"]["selection"]["成本模式"] for item in before[0]], ["FEE", "SLIPPAGE"])
            meta_path = second / "回测数据说明.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["request"]["end"] = "1970-01-01T00:02:00Z"
            fixture.write_json(meta_path, meta)
            self.app.revalidate_fingerprint_queue()
            deadline = time.monotonic() + 10
            while True:
                message = self.app.messages.get(timeout=max(.01, deadline-time.monotonic()))
                if message["type"] != "fingerprint_progress":
                    break
                self.app._handle_fingerprint_message(message)
                self.assertEqual(self.state(), before)
                self.assertLess(time.monotonic(), deadline, "精确列表重新核验超时")
            self.assertEqual(message["type"], "fingerprint_error")
            self.app._handle_fingerprint_message(message)
            self.assertEqual(self.state(), before)
            self.write.assert_not_called()
        finally:
            fixture.doCleanups()

    def test_main_list_and_both_leverages_are_accessible_at_1080_and_1280(self):
        app = self.app
        self.add([queue.match(leverage=9.0), queue.match("b", "second", "MACD_CYCLE", 10.0)])
        app.attributes("-alpha", 0); app.attributes("-toolwindow", True)
        try:
            for width, height in ((1080, 720), (1280, 850)):
                app.geometry(f"{width}x{height}+30000+30000"); app.deiconify()
                app.notebook.select(app.select_tab); app.update()
                self.assertFalse(app.selection_panel.winfo_ismapped())
                for widget in (app.fingerprint_lookup_button, app.batch_tree, app.batch_details):
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertGreater(widget.winfo_height(), 15)
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), app.winfo_rootx() + width)
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), app.winfo_rooty() + height)
                self.assertEqual(app.batch_tree.xview()[0], 0)
                for iid in app.batch_tree.get_children():
                    x, y, cell_width, cell_height = app.batch_tree.bbox(iid, "名义倍数")
                    self.assertGreater(cell_height, 0)
                    self.assertLessEqual(x + cell_width, app.batch_tree.winfo_width())
                app.notebook.select(app.run_tab); app.update()
                self.assertTrue(app.start_btn.winfo_ismapped())
                self.assertIn("9x", app.selection_summary_var.get())
                self.assertIn("10x", app.selection_summary_var.get())
                self.assertLessEqual(app.start_btn.winfo_rooty() + app.start_btn.winfo_height(), app.winfo_rooty() + height)
        finally:
            app.withdraw()


if __name__ == "__main__":
    unittest.main()
