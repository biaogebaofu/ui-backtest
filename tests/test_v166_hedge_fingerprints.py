import copy
import csv
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from data_sources import source_bundle_fingerprint
from fingerprint_lookup import find_fingerprint_groups, restore_many, restore_fingerprint_match, bind_fingerprint_match, merge_fingerprint_restorations
from fingerprint_batch import read_manifest, worker_command
from hedge_config import normalize_config
from hedge_engine import ENGINE_VERSION
from hedge_entry_combinations import plan_request_entries, fifth_tasks
from hedge_fingerprints import BASE, core_hashes, core_names, resolve_exact
from hedge_signals import TIMEFRAMES
from hedge_worker import summarize, csv_rows, summary_headers, write_json


def selection():
    return {"开仓指标": ["hist"], "开仓条件": {tf: [42] if tf in ("1m", "5m") else [0] for tf in TIMEFRAMES},
            "止损代码": ["OFF"], "止盈方案编号": [1], "入场触发口径": "LIVE_01",
            "入场约束": {"最小S3距离": .001, "S3基线周期": "1m"}}


META = {"capabilities": {"ohlcv": True}, "capabilities_by_timeframe": {tf: {"ohlcv": True} for tf in TIMEFRAMES}}


class CombinedTests(unittest.TestCase):
    def test_two_required_timeframes_really_combine(self):
        req = dict(selection=selection(), hedge=normalize_config({"compare_entries": True}), entry_plan="combined")
        entries, skipped = plan_request_entries(req, META)
        self.assertFalse(skipped)
        self.assertEqual(len(entries), 2)  # random + 5m AND 1m
        self.assertEqual(entries[1]["cases"], (0, 0, 0, 42, 42))
        self.assertIn("5m", entries[1]["label"])
        self.assertIn("1m", entries[1]["label"])
        req.pop("entry_plan")
        old, _ = plan_request_entries(req, META)
        self.assertEqual(len(old), 3)
        self.assertTrue(all(sum(bool(code) for code in item["cases"]) == 1 for item in old[1:]))

    def test_optional_off_and_multiple_choices_cartesian(self):
        selected = selection()
        selected["开仓条件"]["5m"] = [0, 1, 42]
        selected["开仓条件"]["1m"] = [1, 42]
        rows, _ = plan_request_entries(dict(selection=selected, hedge={"compare_entries": True}, entry_plan="combined"), META)
        self.assertEqual(len(rows), 7)
        self.assertEqual({tuple(row["cases"])[-2:] for row in rows[1:]}, {(a, b) for a in (0, 1, 42) for b in (1, 42)})

    def test_required_missing_period_is_not_silently_disabled(self):
        selected = selection()
        selected["开仓条件"]["5m"] = [128]
        rows, skipped = plan_request_entries(dict(selection=selected, hedge={"compare_entries": True}, entry_plan="combined"), META)
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped[0]["开仓代码"], 128)

    def test_same_timeframe_groups_and_fifth_policy(self):
        from indicator_combinations import entry_members
        selected = selection()
        selected["开仓条件"]["5m"] = [1, 42]
        selected["指标组合"] = {"开仓": {"5m": {"启用": True, "组合数量": [2], "保留单项": False, "逻辑": "AND"}}}
        req = dict(selection=selected, hedge={"compare_entries": True}, entry_plan="combined")
        rows, _ = plan_request_entries(req, META)
        self.assertEqual(len(rows), 2)
        self.assertEqual(entry_members(rows[1]["cases"][3]), (1, 42))
        selected.pop("指标组合")
        selected["开仓条件"]["5m"] = [264, 288]
        rows, skipped = plan_request_entries(req, META)
        self.assertTrue(any("永久删除" in item["跳过原因"] for item in skipped))
        self.assertEqual(fifth_tasks(rows), [("5m", 288)])

    def test_independent_timeframes_keep_selected_same_timeframe_groups(self):
        from indicator_combinations import entry_members, entry_logic
        selected = selection()
        selected["开仓条件"]["5m"] = [1, 42]
        selected["指标组合"] = {"开仓": {"5m": {"启用": True, "组合数量": [2], "保留单项": False, "逻辑": "AND"}}}
        request = dict(selection=selected, hedge={"compare_entries": True}, entry_plan="independent")
        rows, skipped = plan_request_entries(request, META)
        self.assertFalse(skipped)
        self.assertEqual(len(rows), 3)  # random + one 5m group + the 1m rule
        group = next(row for row in rows if row["timeframe"] == "5m")
        self.assertEqual(entry_members(group["code"]), (1, 42))
        self.assertEqual(entry_logic(group["code"]), "AND")
        self.assertEqual(sum(bool(code) for code in group["cases"]), 1)
        selected["指标组合"]["开仓"]["5m"].update(保留单项=True)
        rows, skipped = plan_request_entries(request, META)
        five = [row for row in rows if row["timeframe"] == "5m"]
        self.assertEqual(len(five), 3)
        group = next(row for row in five if row["code"] < 0)
        self.assertEqual(entry_logic(group["code"]), "AND")


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "双向结果"
        self.output.mkdir()
        data = self.root / "kline.csv"
        data.write_text("sample market data", "utf-8")
        self.sources = dict(kline=str(data), micro="", funding="", oi="", bundle="")
        self.feature = dict(fingerprint=source_bundle_fingerprint(self.sources), start="", end="", feature_version=7)
        self.meta = dict(META, sources=self.sources, request=self.feature, start_utc="2025-01-01", end_utc="2025-01-02")

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, combined=False, mode="scale_in", direction_rule=None):
        request = dict(sources=self.sources, start="", end="", selection=selection(),
                       hedge=normalize_config(dict(compare_entries=True, seeds=2, mode=mode)),
                       indicator_registry={"版本": 1, "组合": []})
        if combined:
            request["entry_plan"] = "combined"
        if direction_rule:
            request['hedge']['direction_rule']=direction_rule
        descriptors, _ = plan_request_entries(request, self.meta)
        stats = dict(ending_equity=21000., max_drawdown=.1, profit_factor=2., trades=3., win_rate=.75,
                     daily_trades=2., fees=0., unrealized_pnl=0., direction_exits=1.)
        config = request["hedge"]
        rows = [summarize(d, config, 12., .001 if mode == "scale_in" else 0., 0, [stats, stats]) for d in descriptors]
        csv_rows(self.output / "方案汇总.csv", summary_headers(rows), rows)
        write_json(self.output / "双向回测请求.json", request)
        write_json(self.output / "开仓独立比较清单.json", dict(entries=descriptors, skipped=[]))
        write_json(self.output / "行情与节假日核对.json", dict(features=self.meta))
        write_json(self.output / "双向运行身份.json", dict(engine_version=ENGINE_VERSION, feature_request=self.feature, files_sha256=core_hashes(core_names(request))))
        shutil.copyfile(BASE / "hedge_holidays.json", self.output / "已用节假日日历.json")
        groups = find_fingerprint_groups(" ".join(row["策略标识"] for row in rows), self.root)
        return [items[0] for items in groups.values()]

    def test_direction_rule_restore_and_editor_sync(self):
        from direction_calendar import RULE_ID
        from hedge_editor_sync import merge_hedge_restorations
        matches=self.fixture(combined=True,direction_rule=RULE_ID)
        restored=restore_many(matches)
        for result in restored:
            self.assertEqual(result['origin_identity']['hedge_request']['hedge']['direction_rule'],RULE_ID)
        projected=merge_hedge_restorations(restored)
        self.assertTrue(projected['fields']['direction_limit'])
        with patch('hedge_fingerprints.core_hashes',return_value={}):
            with self.assertRaises(ValueError):resolve_exact(restored[0]['origin_identity']['hedge_request'],self.meta)

    def test_audited_off_upgrade_preserves_original_identity_and_imports(self):
        from hedge_fingerprints import OFF_COMPATIBLE_REVISIONS
        from hedge_editor_sync import merge_hedge_restorations
        matches = self.fixture(combined=True)
        identity_path = self.output / "双向运行身份.json"
        identity = json.loads(identity_path.read_text("utf-8"))
        identity["files_sha256"].update({name: old for name, (old, _) in OFF_COMPATIBLE_REVISIONS.items()})
        write_json(identity_path, identity)
        restored = restore_many(matches)
        for match, result in zip(matches, restored):
            self.assertEqual(result["origin_identity"]["original_identity"], identity)
            self.assertNotEqual(result["code_sha256"], result["current_code_sha256"])
            self.assertIn("兼容", result["warnings"][0])
            pinned = bind_fingerprint_match(match, result)
            self.assertEqual(restore_fingerprint_match(pinned), result)
            descriptor, _ = resolve_exact(result["origin_identity"]["hedge_request"], self.meta)
            self.assertEqual(descriptor["id"], result["origin_identity"]["hedge_request"]["hedge_exact"]["descriptor"]["id"])
        self.assertFalse(merge_hedge_restorations(restored)["fields"]["direction_limit"])

    def test_audited_upgrade_rejects_unknown_code_calendar_and_enabled_rule(self):
        from hedge_fingerprints import OFF_COMPATIBLE_REVISIONS
        from direction_calendar import RULE_ID
        result = restore_many(self.fixture(combined=True))[1]
        request = result["origin_identity"]["hedge_request"]
        request["hedge_exact"]["core_files_sha256"].update(
            {name: old for name, (old, _) in OFF_COMPATIBLE_REVISIONS.items()})
        resolve_exact(request, self.meta)
        for name in core_names(request):
            with self.subTest(file=name, side="original"):
                changed = copy.deepcopy(request)
                changed["hedge_exact"]["core_files_sha256"][name] = "unknown"
                with self.assertRaisesRegex(ValueError, "核心"):
                    resolve_exact(changed, self.meta)
            with self.subTest(file=name, side="current"):
                current = core_hashes(core_names(request))
                current[name] = "unknown"
                with patch("hedge_fingerprints.core_hashes", return_value=current):
                    with self.assertRaisesRegex(ValueError, "核心"):
                        resolve_exact(request, self.meta)
        changed = copy.deepcopy(request)
        changed["hedge"]["direction_rule"] = RULE_ID
        with self.assertRaisesRegex(ValueError, "核心"):
            resolve_exact(changed, self.meta)
        changed = copy.deepcopy(request)
        changed["hedge"]["tp_weekday"] *= 2
        with self.assertRaisesRegex(ValueError, "指纹不匹配"):
            resolve_exact(changed, self.meta)
        changed_meta = dict(self.meta, end_utc="2030-01-01")
        with self.assertRaisesRegex(ValueError, "行情身份"):
            resolve_exact(request, changed_meta)

    def test_pinned_signal_upgrade_preserves_exact_single_and_combined_cases(self):
        from hedge_fingerprints import PINNED_SIGNAL_REVISIONS
        for combined in (False, True):
            with self.subTest(combined=combined):
                matches = self.fixture(combined=combined)
                path = self.output / "双向运行身份.json"
                identity = json.loads(path.read_text("utf-8"))
                identity["files_sha256"]["hedge_signals.py"] = PINNED_SIGNAL_REVISIONS[0]
                write_json(path, identity)
                for match, restored in zip(matches, restore_many(matches)):
                    self.assertEqual(restored["fingerprint"], match["fingerprint"])
                    request = restored["origin_identity"]["hedge_request"]
                    descriptor, _ = resolve_exact(request, self.meta)
                    self.assertEqual(descriptor, request["hedge_exact"]["descriptor"])

    def test_pinned_signal_upgrade_rejects_expansion_or_other_core_changes(self):
        from hedge_fingerprints import PINNED_SIGNAL_REVISIONS
        request = restore_many(self.fixture(combined=True))[1]["origin_identity"]["hedge_request"]
        request["hedge_exact"]["core_files_sha256"]["hedge_signals.py"] = PINNED_SIGNAL_REVISIONS[0]
        resolve_exact(request, self.meta)
        expanded = copy.deepcopy(request)
        expanded["selection"]["指标组合"] = {"开仓": {"1m": {"启用": True, "组合数量": [2], "保留单项": False, "逻辑": "AND"}}}
        with self.assertRaisesRegex(ValueError, "核心"):
            resolve_exact(expanded, self.meta)
        for name in core_names(request):
            changed = copy.deepcopy(request)
            changed["hedge_exact"]["core_files_sha256"][name] = "unknown"
            with self.subTest(file=name), self.assertRaisesRegex(ValueError, "核心"):
                resolve_exact(changed, self.meta)

    def test_reconciliation_allows_new_columns_but_requires_every_original_field(self):
        from hedge_fingerprints import result_summary
        from hedge_worker import PARAMETER_HEADERS
        matches = self.fixture(combined=True)
        match = matches[0]
        restored = restore_many(matches)[0]
        extra = dict(match["row"])
        match = dict(match, row={key: value for key, value in match["row"].items()
                                if key not in PARAMETER_HEADERS})
        original = match["row"]
        def reconcile(values):
            with (self.output / "方案汇总.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(values))
                writer.writeheader()
                writer.writerow(values)
            return result_summary(self.output, match, restored)
        report = reconcile(extra)
        self.assertIn("原汇总已有字段全部一致", report["数值对账说明"])
        self.assertEqual(report["资金差额（USDC）"], 0.)
        missing = dict(extra)
        missing.pop("盈利比例（%）")
        self.assertIn("原汇总字段缺失或数值存在差异", reconcile(missing)["数值对账说明"])
        changed = dict(extra)
        changed["期末资金中位数（USDC）"] = str(float(original["期末资金中位数（USDC）"]) + 1.)
        report = reconcile(changed)
        self.assertIn("原汇总字段缺失或数值存在差异", report["数值对账说明"])
        self.assertEqual(report["资金差额（USDC）"], 1.)

    def test_restore_all_original_variants_once_and_dispatch_hedge(self):
        matches = self.fixture()
        with patch("data_sources.source_bundle_fingerprint", wraps=source_bundle_fingerprint) as fingerprint:
            restored = restore_many(matches)
        self.assertEqual(fingerprint.call_count, 1)
        for match, result in zip(matches, restored):
            pinned = bind_fingerprint_match(match, result)
            self.assertEqual(result, restore_fingerprint_match(pinned))
            request = result["origin_identity"]["hedge_request"]
            descriptor, variant = resolve_exact(request, self.meta)
            self.assertEqual(variant, (12., .001, 0))
            self.assertEqual(request["hedge"]["seeds"], 2)
            self.assertEqual(request["hedge"]["timeout_hours"], [12.])
            self.assertEqual(descriptor["id"], request["hedge_exact"]["descriptor"]["id"])
        manifest = self.root / "manifest.json"
        write_json(manifest, dict(version=1, strategies=matches))
        self.assertEqual(len(read_manifest(manifest)["strategies"]), 3)
        command = worker_command(BASE, restored[1], self.root, 2, "cpu", self.root / "cache")
        self.assertEqual(Path(command[3]).name, "hedge_worker.py")
        with self.assertRaisesRegex(ValueError, "双向"):
            merge_fingerprint_restorations(restored)

    def test_combined_and_single_position_original_request_restore(self):
        for combined, mode in ((True, "scale_in"), (False, "single")):
            matches = self.fixture(combined, mode)
            restored = restore_many(matches)
            request = restored[-1]["origin_identity"]["hedge_request"]
            descriptor, _ = resolve_exact(request, self.meta)
            self.assertEqual(sum(bool(code) for code in descriptor["cases"]), 2 if combined else 1)

    def test_tampered_row_seed_count_core_and_source_are_rejected(self):
        matches = self.fixture()
        pinned = bind_fingerprint_match(matches[1], restore_many(matches)[1])
        changed = copy.deepcopy(pinned)
        changed["row"]["随机重复次数"] = "999"
        with self.assertRaises(ValueError):
            restore_fingerprint_match(changed)
        request_path = self.output / "双向回测请求.json"
        original = json.loads(request_path.read_text("utf-8"))
        changed = copy.deepcopy(original)
        changed["hedge"]["seed_start"] += 1
        write_json(request_path, changed)
        with self.assertRaisesRegex(ValueError, "已变化"):
            restore_fingerprint_match(pinned)
        write_json(request_path, original)
        with patch("hedge_fingerprints.core_hashes", return_value={}):
            with self.assertRaisesRegex(ValueError, "核心"):
                restore_fingerprint_match(pinned)
        Path(self.sources["kline"]).write_text("different data", "utf-8")
        with self.assertRaisesRegex(ValueError, "行情"):
            restore_fingerprint_match(pinned)

    def test_queue_displays_hedge_parameters(self):
        from ui import App
        match = self.fixture()[1]
        restored = restore_fingerprint_match(match)
        values = App._queue_display_values(0, bind_fingerprint_match(match, restored))
        self.assertIn("双向", values[3])
        self.assertEqual(values[5], "0.4%/0.2%")
        self.assertEqual(values[7], "Maker 0%")

    def test_real_ui_async_import_syncs_entries_and_shows_details(self):
        from ui import App
        matches = self.fixture(combined=True)
        with patch.object(App, "load_user_settings"), patch.object(App, "detect_gpu"), patch.object(App, "save_user_settings"), \
                patch("ui.messagebox.showerror") as error, patch("ui.messagebox.showinfo") as info:
            app = App()
            app.withdraw()
            app.hedge_panel.settings_path = self.root / "hedge_settings.json"
            try:
                before = app.selection_panel.get_config()
                app.lookup_fingerprint_queue(" ".join(m["fingerprint"] for m in matches), str(self.output))
                deadline = time.monotonic() + 10
                while app._fingerprint_busy and time.monotonic() < deadline:
                    app.update()
                    time.sleep(.01)
                self.assertFalse(app._fingerprint_busy)
                self.assertEqual(len(app.fingerprint_queue), 2)
                after = app.selection_panel.get_config()
                for key in set(before) - {"开仓条件", "开仓指标", "入场触发口径", "指标组合"}:
                    self.assertEqual(after[key], before[key], key)
                self.assertTrue(app.hedge_panel.vars["compare_entries"].get())
                self.assertIn("已同步", app.fingerprint_status_var.get())
                app.batch_tree.selection_set("1")
                app._show_batch_details()
                details = [app.batch_details.item(item)["values"] for item in app.batch_details.get_children()]
                self.assertTrue(any("全部开仓条件" in str(value) and "cases" in str(value) for value in details))
                self.assertIsNone(app.proc)
                error.assert_not_called()
                info.assert_not_called()
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main()
