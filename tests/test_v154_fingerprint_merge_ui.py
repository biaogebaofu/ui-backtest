"""添加指纹同步兼容勾选，不强制运行对象；隔离隐藏Tk，不写用户设置。"""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

import fingerprint_lookup as lookup
from ranking_view import CODE_NAMES, config_fingerprint
from selection_config import 配置统计
import test_v152_fingerprint_queue_ui as queue


def four_records():
    items, restored = [], []
    for index, (code, size) in enumerate(((152, 10.), (152, 9.), (152, 8.), (5, 10.))):
        item = queue.match(mode="TF_EVENT", leverage=size)
        item["row"]["1分钟条件"] = CODE_NAMES[code]
        item["fingerprint"] = config_fingerprint(item["row"])
        value = queue.restoration(item)
        value["selection"]["开仓条件"]["1m"] = [code]
        items.append(item); restored.append(value)
    return items, restored


class FingerprintMergeUiTests(unittest.TestCase):
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

    def add_four(self):
        items, restored = four_records()
        with mock.patch.object(self.app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {item["fingerprint"]: [item] for item in items})
            self.assertEqual(job.call_args.args[1], items)
        self.deliver("queue_restore", restored)
        return items, restored

    def test_four_originals_sync_six_editor_combinations_and_expand_both_batches(self):
        app = self.app
        with mock.patch.object(app, "_launch_process") as launch:
            items, restored = self.add_four()
        config = app.current_selection()
        self.assertEqual(app.run_target_var.get(), "EDITOR")
        self.assertEqual(config["开仓条件"]["1m"], [5, 152])
        self.assertEqual(config["仓位倍数"], [8., 9., 10.])
        self.assertEqual(配置统计(config)["包含仓位完整组合数"], 6)
        self.assertEqual(app.fingerprint_queue, [lookup.bind_fingerprint_match(m, r) for m, r in zip(items, restored)])
        self.assertTrue(app.selection_panel.case_vars["1m"][5].get())
        self.assertTrue(app.selection_panel.case_vars["1m"][152].get())
        self.assertTrue(app.selection_panel.entry_fold_vars["1m"].get())
        self.assertTrue(app.selection_panel.fourth_entry_fold_vars["1m"].get())
        self.assertEqual(app.selection_panel.winfo_manager(), "pack")
        self.assertIsNone(app._fingerprint_queue_dialog)
        for phrase in ("4条", "6组", "多2组", "手选"):
            self.assertIn(phrase, app.fingerprint_status_var.get())
        launch.assert_not_called(); self.write.assert_not_called()

    def test_adding_in_explicit_exact_mode_also_syncs_editor_without_switching_target(self):
        app = self.app
        app.run_target_var.set("FINGERPRINTS"); app._sync_run_target()
        self.add_four()
        self.assertEqual(app.run_target_var.get(), "FINGERPRINTS")
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 6)
        app.run_target_var.set("EDITOR"); app._sync_run_target()
        self.assertEqual(app.current_selection()["开仓条件"]["1m"], [5, 152])
        self.assertEqual(len(app.fingerprint_queue), 4)

    def test_single_fingerprints_added_one_by_one_accumulate_and_sync_all_conditions(self):
        app = self.app
        items, restored = four_records()
        for index, item in enumerate(items):
            app.fingerprint_var.set(item["fingerprint"] + ", ")
            with mock.patch.object(app, "_start_fingerprint_job") as job:
                app.lookup_fingerprint()
                self.assertEqual(job.call_args.args[0], "queue_find")
                self.deliver("queue_find", {item["fingerprint"]: [item]})
                self.assertEqual(len(job.call_args.args[1]), index + 1)
            self.deliver("queue_restore", restored[:index + 1])
            self.assertEqual(len(app.fingerprint_queue), index + 1)
            self.assertEqual(app.run_target_var.get(), "EDITOR")
        self.assertEqual(app.current_selection()["开仓条件"]["1m"], [5, 152])
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 6)
        self.assertIsNone(app._fingerprint_queue_dialog)

    def test_explicit_merge_action_restores_existing_list_then_selects_editor(self):
        app = self.app
        items, restored = four_records()
        app.fingerprint_queue = [lookup.bind_fingerprint_match(m, r) for m, r in zip(items, restored)]
        app.run_target_var.set("FINGERPRINTS"); app._refresh_fingerprint_queue()
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            app.revalidate_fingerprint_queue(apply_to_editor=True)
            self.assertEqual(job.call_args.args[:2], ("queue_restore", app.fingerprint_queue))
        self.deliver("queue_restore", restored)
        self.assertEqual(app.run_target_var.get(), "EDITOR")
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 6)

    def test_global_conflicts_only_add_exact_list_without_overwriting_editor_or_target(self):
        app = self.app
        for field in ("cost", "source", "risk", "date", "ranking"):
            app.fingerprint_queue = []
            before, target = self.snapshot(), app.run_target_var.get()
            items, restored = four_records()
            if field == "cost":
                restored[-1]["selection"]["成交偏移"]["开仓"] += .0001
            elif field == "source":
                restored[-1]["sources"]["kline"] += ".different"
            elif field == "risk":
                restored[-1]["selection"]["资金约束"]["初始资金USDC"] += 1
            elif field == "date":
                restored[-1]["end"] = "2026-09-10"
            else:
                restored[-1]["ranking_settings"]["排行指标"] = "期末资金（USDC）"
            app._fingerprint_queue_pending = items
            self.deliver("queue_restore", restored)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(app.run_target_var.get(), target)
            self.assertEqual(len(app.fingerprint_queue), 4)
            self.assertIn("不能合并", app.fingerprint_status_var.get())
            self.assertIn("保持不变", self.info.call_args.args[1])
        self.write.assert_not_called()

    def test_new_addition_revalidates_all_original_pins_before_merging(self):
        app = self.app
        items, restored = four_records()
        app._fingerprint_queue_pending = items[:3]
        self.deliver("queue_restore", restored[:3])
        original = copy.deepcopy(app.fingerprint_queue)
        before = self.snapshot()
        with mock.patch.object(app, "_start_fingerprint_job") as job:
            self.deliver("queue_find", {items[-1]["fingerprint"]: [items[-1]]})
            self.assertEqual(job.call_args.args[1], original + [items[-1]])
        changed = copy.deepcopy(restored)
        changed[0]["selection"]["入场约束"]["最小S3距离"] += .001
        self.deliver("queue_restore", changed)
        self.assertEqual(app.fingerprint_queue, original)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("变化", self.error.call_args.args[1])

    def test_target_switch_during_lookup_discards_late_apply_intent(self):
        app = self.app
        items, restored = four_records()
        app.fingerprint_queue = [lookup.bind_fingerprint_match(m, r) for m, r in zip(items, restored)]
        app.run_target_var.set("FINGERPRINTS"); app._refresh_fingerprint_queue()
        before = self.snapshot()
        with mock.patch.object(app, "_start_fingerprint_job"):
            app.revalidate_fingerprint_queue(apply_to_editor=True)
        generation = app._fingerprint_generation
        app.run_target_var.set("EDITOR"); app._sync_run_target()
        self.deliver("queue_restore", restored, generation)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(app.run_target_var.get(), "EDITOR")

    def test_old_auto_list_target_migrates_without_changing_saved_editor_and_new_choice_persists(self):
        app = self.app
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        saved["运行对象"] = "FINGERPRINTS"
        saved.pop("指纹交互版本", None)
        before = app.current_selection()
        def load(payload):
            path = mock.Mock(is_file=mock.Mock(return_value=True),
                             read_text=mock.Mock(return_value=json.dumps(payload)))
            with mock.patch.object(self.ui, "用户设置文件", path), \
                    mock.patch.object(app, "_start_fingerprint_job") as job:
                type(self).original_load(app)
                job.assert_not_called()
        load(saved)
        self.assertEqual(app.run_target_var.get(), "EDITOR")
        self.assertEqual(app.current_selection(), before)
        self.assertEqual(self.write.call_args.args[1]["指纹交互版本"], 154)
        saved["指纹交互版本"] = 154
        load(saved)
        self.assertEqual(app.run_target_var.get(), "FINGERPRINTS")
        self.assertEqual(app.current_selection(), before)

    def test_exact_start_still_has_four_originals_while_editor_count_is_six(self):
        app = self.app
        self.add_four()
        self.assertEqual(配置统计(app.current_selection())["包含仓位完整组合数"], 6)
        original = copy.deepcopy(app.fingerprint_queue)
        app.run_target_var.set("FINGERPRINTS"); app._sync_run_target()
        app.thread_var.set(1)
        with mock.patch.object(Path, "mkdir"), mock.patch.object(Path, "exists", return_value=False), \
                mock.patch.object(app, "_launch_process", return_value=True):
            app.start_run()
        self.assertEqual(self.write.call_args.args[1], {"version": 1, "strategies": original})
        self.assertEqual(len(original), 4)


if __name__ == "__main__":
    unittest.main()
