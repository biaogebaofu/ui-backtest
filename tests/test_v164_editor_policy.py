"""Editor-only retirement migration; isolated Tk never loads user settings or workers."""
import copy
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import indicator_combinations as combinations
from fifth_policy import ACTIVE_FIFTH_CODES, RETIRED_FIFTH_CODES
from selection_availability import prepare_editor_selection
from selection_config import 全选配置, 规范化配置, 配置签名, 入场口径列表, 结果入场口径列表


EXPECTED_NUMBERS = {
    15, 22, 23, 25, 26, 27, 28, 29, 31, 34, 35, 36, 37, 38, 39, 40, 41,
    51, 55, 59, 70, 71, 72, 73, 77, 79, 81, 84, 85, 86, 87, 88, 90,
    96, 97, 98, 99, 101, 102,
}
TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")


def small_selection():
    cfg = 全选配置()
    cfg.update({"开仓指标": ["hist"], "开仓条件": {tf: [0] for tf in TIMEFRAMES},
                "止损代码": ["OFF"], "固定止损代码": ["OFF"], "止盈方案编号": [8281],
                "止盈后等待分钟": [1], "仓位倍数": [2.0], "入场触发口径": "LIVE_01"})
    cfg["开仓条件"]["1m"] = [42]
    return cfg


def combo_spec(*sizes, singles=False):
    return {"启用": True, "组合数量": list(sizes), "保留单项": singles, "逻辑": "AND"}


class EditorMigrationTests(unittest.TestCase):
    def test_retired_members_removed_from_all_five_periods_without_input_mutation(self):
        cfg = small_selection()
        cfg["开仓条件"] = {tf: [42, 264, 278, 383] for tf in TIMEFRAMES}
        before = copy.deepcopy(cfg)
        report = prepare_editor_selection(cfg)
        self.assertEqual(cfg, before)
        self.assertEqual(report["selection"]["开仓条件"], {tf: [42, 278] for tf in TIMEFRAMES})
        self.assertEqual(len(report["removed"]), 10)
        self.assertEqual({item["周期"] for item in report["removed"]}, set(TIMEFRAMES))
        self.assertIn("永久删除", report["message"])

    def test_empty_periods_turn_off_without_substituting_a_new_strategy(self):
        cfg = small_selection()
        cfg["开仓条件"] = {tf: [264, 383] for tf in TIMEFRAMES}
        report = prepare_editor_selection(cfg)
        self.assertEqual(report["selection"]["开仓条件"], {tf: [0] for tf in TIMEFRAMES})

    def test_stored_group_with_retired_member_is_removed_whole(self):
        registry = combinations.CombinationRegistry()
        bad = registry.register("entry", [42, 264, 278])
        good = registry.register("entry", [42, 278])
        cfg = small_selection()
        cfg["开仓条件"]["1m"] = [bad, good, 42]
        with mock.patch.object(combinations, "DEFAULT_REGISTRY", registry):
            report = prepare_editor_selection(cfg)
        self.assertEqual(report["selection"]["开仓条件"]["1m"], [good, 42])
        self.assertEqual([item["代码"] for item in report["removed"]], [bad])
        self.assertEqual(registry.resolve("entry", bad)["members"], [42, 264, 278])

    def test_missing_group_dictionary_is_not_misreported_as_retirement(self):
        cfg = small_selection(); cfg["开仓条件"]["1m"] = [-91234567, 42]
        before = copy.deepcopy(cfg)
        with mock.patch.object(combinations, "DEFAULT_REGISTRY", combinations.CombinationRegistry()):
            with self.assertRaisesRegex(ValueError, "缺少指标组合字典"):
                prepare_editor_selection(cfg)
        self.assertEqual(cfg, before)

    def test_invalid_combination_size_is_disabled_with_an_actionable_notice(self):
        cfg = small_selection()
        cfg["开仓条件"]["1m"] = [42, 264, 278]
        cfg["指标组合"] = {"开仓": {"1m": combo_spec(2, 3)}}
        report = prepare_editor_selection(cfg)
        self.assertNotIn("指标组合", report["selection"])
        self.assertIn("1m剔除后成员不足", report["message"])
        self.assertIn("重新选择组合数量", report["message"])

    def test_legal_remaining_combination_sizes_and_other_stages_are_unchanged(self):
        cfg = small_selection()
        cfg["开仓条件"]["1m"] = [42, 264, 278]
        cfg["指标组合"] = {"开仓": {"1m": combo_spec(2)},
                            "止损": {"启用": True, "组合数量": [2], "保留单项": True, "逻辑": "OR"}}
        report = prepare_editor_selection(cfg)
        self.assertEqual(report["selection"]["指标组合"], cfg["指标组合"])

    def test_fifth_only_mode_on_ordinary_rules_recovers_to_default(self):
        cfg = small_selection(); cfg["入场触发口径"] = "F5_EVENT"
        report = prepare_editor_selection(cfg)
        self.assertEqual(report["selection"]["入场触发口径"], "LIVE_01")
        self.assertIn("已退出第五批专用事件模式", report["message"])
        self.assertEqual(规范化配置(report["selection"])["入场触发口径"], "LIVE_01")

    def test_invalid_fifth_mode_keeps_the_users_other_mode(self):
        cfg = small_selection(); cfg["入场触发口径"] = ["TF_EVENT", "F5_EVENT"]
        report = prepare_editor_selection(cfg)
        self.assertEqual(report["selection"]["入场触发口径"], "TF_EVENT")

    def test_mixed_old_and_fifth_rules_recover_but_fifth_result_still_uses_own_event(self):
        cfg = small_selection()
        cfg["开仓条件"]["1m"] = [42, 278]
        cfg["入场触发口径"] = "F5_EVENT"
        report = prepare_editor_selection(cfg)
        result = 规范化配置(report["selection"])
        self.assertEqual(结果入场口径列表(result, [42]), ["LIVE_01"])
        self.assertEqual(结果入场口径列表(result, [278]), ["F5_EVENT"])

    def test_valid_fifth_event_modes_remain_unchanged(self):
        for timeframe in TIMEFRAMES:
            cfg = small_selection()
            cfg["开仓条件"] = {tf: [0] for tf in TIMEFRAMES}
            cfg["开仓条件"][timeframe] = [278, 288]
            cfg["入场触发口径"] = "F5_EVENT"
            with self.subTest(timeframe=timeframe):
                report = prepare_editor_selection(cfg)
                self.assertEqual(report["selection"], cfg)
                self.assertEqual(report["message"], "")
                self.assertEqual(规范化配置(report["selection"])["入场触发口径"], "F5_EVENT")

    def test_legal_fifth_and_ordinary_group_event_mode_is_preserved(self):
        cfg = small_selection()
        cfg["开仓条件"]["1m"] = [42, 278]
        cfg["指标组合"] = {"开仓": {"1m": combo_spec(2)}}
        cfg["入场触发口径"] = "F5_EVENT"
        report = prepare_editor_selection(cfg)
        self.assertEqual(report["selection"], cfg)
        self.assertEqual(规范化配置(report["selection"])["入场触发口径"], "F5_EVENT")

    def test_first_four_batches_keep_each_ordinary_mode_and_signature(self):
        for mode in ["LIVE_01", "TF_EVENT", "MACD_CYCLE", ["LIVE_01", "TF_EVENT"]]:
            cfg = small_selection(); cfg["开仓条件"]["1m"] = [1, 5, 42, 144]
            cfg["入场触发口径"] = mode
            with self.subTest(mode=mode):
                report = prepare_editor_selection(cfg)
                self.assertEqual(report["selection"], cfg)
                self.assertEqual(report["message"], "")
                self.assertEqual(配置签名(cfg), 配置签名(report["selection"]))

    def test_historical_normalization_signatures_remain_readable_without_editor_migration(self):
        # Golden hashes computed from the pre-v1.64 main registry, before any policy edits.
        for entries, expected in [
            ([42], "992fc234dd29ae746078ea3e3b5c34e5398370519e1323cbaed5c56d8fae6daf"),
            ([42, 264, 278], "2ddcb9cf876af58d152d2274df2bd5f54f3e6903ed9e72bd88856327195892ce"),
            ([361, 365], "164837a65aba59211c2d538168ba96cb0ecdfbb5c04e41fa4a244e2c164f807b"),
        ]:
            cfg = small_selection(); cfg["开仓条件"]["1m"] = entries
            with self.subTest(entries=entries):
                self.assertEqual(配置签名(cfg), expected)
                self.assertEqual(规范化配置(cfg)["开仓条件"]["1m"], entries)

    def test_user_allowlist_is_exact_and_keeps_expensive_methods(self):
        self.assertEqual(set(ACTIVE_FIFTH_CODES), {263 + n for n in EXPECTED_NUMBERS})
        self.assertEqual(len(RETIRED_FIFTH_CODES), 81)
        self.assertIn(361, ACTIVE_FIFTH_CODES)  # F5-098 Gaussian process
        self.assertIn(365, ACTIVE_FIFTH_CODES)  # F5-102 Isolation model


class HiddenSelectionPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from selection_panel import 组合选择面板
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.panel = 组合选择面板(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.panel.destroy()
        cls.root.destroy()

    def setUp(self):
        self.panel.clear_data_capabilities()
        self.panel.apply_config(small_selection())

    def test_all_five_period_panels_expose_only_supported_allowlisted_methods(self):
        from fifth_batch import FIFTH_SPECS
        for tf in TIMEFRAMES:
            codes = set(self.panel.case_vars[tf])
            self.assertFalse(codes.intersection(RETIRED_FIFTH_CODES), tf)
            expected = {code for code in ACTIVE_FIFTH_CODES if tf in FIFTH_SPECS[code]["supported_timeframes"]}
            self.assertEqual(codes.intersection(range(264, 384)), expected, tf)

    def test_catalog_contains_exactly_the_39_retained_methods(self):
        def descend(widget):
            for child in widget.winfo_children():
                yield child
                yield from descend(child)
        dialog = self.panel._show_fifth_catalog()
        dialog.withdraw()
        try:
            tree = next(w for w in descend(dialog) if isinstance(w, ttk.Treeview))
            self.assertEqual({int(iid) for iid in tree.get_children()}, set(ACTIVE_FIFTH_CODES))
        finally:
            dialog.destroy()

    def test_applying_old_selection_cleans_rules_and_displays_policy_notice(self):
        cfg = small_selection()
        cfg["开仓条件"] = {tf: [42, 264, 278] for tf in TIMEFRAMES}
        cfg["入场触发口径"] = "F5_EVENT"
        self.panel.apply_config(cfg)
        got = self.panel.get_config()
        self.assertEqual(got["开仓条件"], {tf: [42, 278] for tf in TIMEFRAMES})
        self.assertEqual(got["入场触发口径"], "LIVE_01")
        self.assertIn("永久删除", self.panel.policy_notice_var.get())

    def test_reset_all_never_reintroduces_retired_methods(self):
        with mock.patch("selection_panel.messagebox.askyesno", return_value=True):
            self.panel.reset_all()
        for tf, options in self.panel.case_vars.items():
            self.assertFalse(set(options).intersection(RETIRED_FIFTH_CODES), tf)
            self.assertFalse(set(self.panel.get_config()["开仓条件"][tf]).intersection(RETIRED_FIFTH_CODES), tf)

    def test_regular_mode_button_repairs_mode_without_changing_selected_strategies(self):
        before = self.panel.get_config()["开仓条件"]
        self.panel.set_entry_modes("F5_EVENT")
        self.assertIn("不必选择第五批", self.panel.summary_var.get())
        self.panel._use_regular_entry_mode()
        self.assertEqual(self.panel.get_config()["入场触发口径"], "LIVE_01")
        self.assertEqual(self.panel.get_config()["开仓条件"], before)
        self.assertIn("普通入场模式", self.panel.policy_notice_var.get())

    def test_removing_every_old_rule_keeps_every_ui_period_off(self):
        cfg = small_selection()
        cfg["开仓条件"] = {tf: [264] for tf in TIMEFRAMES}
        cfg["入场触发口径"] = "F5_EVENT"
        self.panel.apply_config(cfg)
        self.assertEqual(self.panel.get_config()["开仓条件"], {tf: [0] for tf in TIMEFRAMES})
        self.assertEqual(self.panel.get_config()["入场触发口径"], "LIVE_01")


class SavedFingerprintQueuePolicyTests(unittest.TestCase):
    """Call the queue validator directly: no App, settings, worker, or source reads."""
    @classmethod
    def setUpClass(cls):
        from ui import App
        cls.validate = App._validated_fingerprint_queue

    @staticmethod
    def item(code, *, snapshot=False, text_only=False):
        row = {"开仓规则": f"F5-{code - 263:03d} 策略"} if text_only else {"1m条件代码": code}
        item = {"fingerprint": f"{code:016x}", "source_dir": str(Path(__file__).resolve().parent / "synthetic_source"), "row": row}
        if snapshot:
            cfg = small_selection(); cfg["开仓条件"]["1m"] = [code]
            item["verified_snapshot"] = {"selection": cfg}
        return item

    def test_saved_queue_drops_only_retired_entries_and_records_each_removed_fingerprint(self):
        ordinary = self.item(42)
        retained = self.item(361, snapshot=True)
        retired = self.item(264, snapshot=True)
        text_retired = self.item(383, text_only=True)
        values = [ordinary, retired, retained, text_retired]
        before = copy.deepcopy(values); removed = []
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("queue migration must not read source files")):
            actual = self.validate(values, removed=removed)
        self.assertEqual(actual, [ordinary, retained])
        self.assertEqual([row["fingerprint"] for row in removed], [retired["fingerprint"], text_retired["fingerprint"]])
        self.assertTrue(all("永久删除" in row["reason"] for row in removed))
        self.assertEqual(values, before)

    def test_new_addition_of_retired_method_is_rejected_without_implicit_filtering(self):
        values = [self.item(278), self.item(264, snapshot=True)]
        before = copy.deepcopy(values)
        with self.assertRaisesRegex(ValueError, "F5-001.*永久删除"):
            self.validate(values)
        self.assertEqual(values, before)

    def test_all_39_retained_fingerprints_survive_both_load_and_new_addition(self):
        items = [self.item(code, snapshot=True) for code in ACTIVE_FIFTH_CODES]
        removed = []
        self.assertEqual(self.validate(items, removed=removed), items)
        self.assertEqual(removed, [])
        self.assertEqual(self.validate(items), items)

    def test_retired_snapshot_cannot_hide_behind_a_retained_display_row(self):
        item = self.item(264, snapshot=True)
        item["row"] = {"1m条件代码": 278, "开仓规则": "F5-015 滚动谐波相位预测"}
        with self.assertRaisesRegex(ValueError, "F5-001.*永久删除"):
            self.validate([item])

    def test_deleted_member_in_a_compound_display_name_blocks_import(self):
        item = self.item(278)
        item["row"] = {"开仓规则": "F5-015 滚动谐波相位预测 AND F5-001 一目云层与转换线交叉"}
        with self.assertRaisesRegex(ValueError, "F5-001.*永久删除"):
            self.validate([item])

    def test_unrelated_malformed_queue_item_is_an_error_even_during_old_list_migration(self):
        malformed = self.item(278); malformed["row"] = None
        removed = []
        with self.assertRaisesRegex(ValueError, "缺少原结果记录"):
            self.validate([malformed], removed=removed)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
