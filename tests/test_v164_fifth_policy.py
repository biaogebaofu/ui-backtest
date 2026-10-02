"""Retired methods stay readable in history but cannot return through execution paths."""
import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

import fifth_batch
import fifth_policy as policy
import fifth_precompute as precompute
import fingerprint_batch as batch
import fingerprint_lookup as lookup
from extended_rules import ENTRY_RULES, entry_unavailable_reason, stable_base_id
from indicator_combinations import CombinationChoices, CombinationRegistry
from ranking_view import CODE_NAMES, build_view, config_fingerprint
from selection_config import 全选配置, 规范化配置, 配置签名
from strategy_space import 生成止损组合
import test_v149_fingerprint_lookup as fixture


PROJECT = Path(__file__).resolve().parents[1]


class FifthPolicyTests(unittest.TestCase):
    def test_fixed_allowlist_and_all_historical_definitions(self):
        self.assertEqual(len(policy.ACTIVE_FIFTH_CODES), 39)
        self.assertEqual(len(policy.RETIRED_FIFTH_CODES), 81)
        self.assertEqual(set(fifth_batch.FIFTH_SPECS), set(range(264, 384)))
        self.assertEqual(set(policy.ACTIVE_FIFTH_CODES) | set(policy.RETIRED_FIFTH_CODES),
                         set(fifth_batch.FIFTH_SPECS))
        self.assertTrue(all(code in ENTRY_RULES for code in range(264, 384)))
        self.assertEqual(fifth_batch.AVAILABLE_FIFTH_CODES, policy.ACTIVE_FIFTH_CODES)
        for code in range(264):
            policy.require_entry_allowed(code)
        for code in policy.ACTIVE_FIFTH_CODES:
            policy.require_entry_allowed(code)
        for code in (*policy.RETIRED_FIFTH_CODES, 384, 999):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "永久删除"):
                policy.require_entry_allowed(code)

    def test_all_periods_and_stored_group_members_are_enforced_without_mutation(self):
        registry = CombinationRegistry()
        retired = registry.register("entry", [264, 288])
        nested = registry.register("entry", [retired, 42])
        allowed = registry.register("entry", [42, 288])
        policy.require_entry_allowed(allowed, registry)
        for code in (retired, nested):
            with self.assertRaisesRegex(ValueError, "F5-001.*永久删除"):
                policy.require_entry_allowed(code, registry)
        registry_copy = CombinationRegistry().update(registry.to_dict())
        self.assertEqual(registry_copy.to_dict(), registry.to_dict())
        for timeframe in ("1m", "5m", "15m", "1h", "4h"):
            selection = {"开仓条件": {timeframe: [42, 264, 288]}}
            original = copy.deepcopy(selection)
            with self.subTest(timeframe=timeframe), self.assertRaisesRegex(ValueError, timeframe):
                policy.require_selection_allowed(selection)
            self.assertEqual(selection, original)

    def test_direct_signals_and_cached_precomputation_cannot_bypass_policy(self):
        with self.assertRaisesRegex(ValueError, "永久删除"):
            fifth_batch.fifth_masks(264, *([None] * 9))
        with mock.patch.object(precompute.np, "load") as load:
            with self.assertRaisesRegex(ValueError, "永久删除"):
                precompute.load_signals("unused", "1m", 264, 10)
            load.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "never-created"
            with self.assertRaisesRegex(ValueError, "永久删除"):
                precompute.prepare_fifth_signals({}, [("1m", 264)], output, {}, 1, lambda event: None)
            self.assertFalse(output.exists())
        options = [CombinationChoices([0]) for _ in range(4)] + [CombinationChoices([264, 288])]
        with self.assertRaisesRegex(ValueError, "永久删除"):
            precompute.selected_tasks(options)

    def test_engine_checks_policy_before_using_precomputed_cache(self):
        # Load the production function with a tiny cache fixture, without loading
        # the user's multi-gigabyte market bundle through engine module imports.
        tree = ast.parse((PROJECT / "backtest_engine.py").read_text("utf-8-sig"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "extended_entry_data")
        node.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        namespace = {"FIFTH_SPECS": fifth_batch.FIFTH_SPECS,
                     "E": types.SimpleNamespace(D={"1m_hist": np.ones(3)}),
                     "_fifth_signal_cache": "existing-cache", "_map_fifth_signals": lambda tf, data: data}
        exec(compile(module, str(PROJECT / "backtest_engine.py"), "exec"), namespace)
        function = namespace["extended_entry_data"]
        with mock.patch.object(precompute, "load_signals", return_value="valid-signals") as load:
            with self.assertRaisesRegex(ValueError, "永久删除"):
                function("1m", "hist", 264)
            load.assert_not_called()
            self.assertEqual(function("1m", "hist", 288), "valid-signals")
            load.assert_called_once_with("existing-cache", "1m", 288, 3)

    def test_availability_reports_retirement_but_history_can_check_original_capabilities(self):
        self.assertIn("永久删除", entry_unavailable_reason(264, {"ohlcv": True}))
        self.assertEqual(entry_unavailable_reason(264, {"ohlcv": True}, allow_retired=True), "")

    def test_saved_queue_recognizes_snapshots_codes_and_names_without_market_reads(self):
        retired = [
            {"verified_snapshot": {"selection": {"开仓条件": {"1m": [264]}}}},
            {"row": {"1m条件代码": 264}},
            {"row": {"4小时条件": "指标组合（F5-025 双侧CUSUM漂移报警且F5-001 一目云层与转换线交叉）"}},
            {"row": {"开仓规则": "F5-121 新方法"}},
        ]
        with mock.patch.object(lookup, "source_bundle_fingerprint", side_effect=AssertionError("不要读取行情")):
            for match in retired:
                self.assertIn("永久删除", lookup.fingerprint_retirement_reason(match))
            for match in ({"row": {"1分钟条件": "F5-025 双侧CUSUM漂移报警"}},
                          {"row": {"1分钟条件": "42"}}, {"row": {"1分钟条件": "-123456"}}):
                self.assertEqual(lookup.fingerprint_retirement_reason(match), "")

    def test_cli_rejects_retired_before_data_pruning_or_output_creation(self):
        with tempfile.TemporaryDirectory(prefix="fifth-policy-worker-") as directory:
            root = Path(directory)
            selection = 全选配置()
            selection["开仓条件"] = {tf: [0] for tf in ("4h", "1h", "15m", "5m", "1m")}
            selection["开仓条件"]["1m"] = [42, 264, 288]
            selection["开仓指标"] = ["hist"]
            selection["入场触发口径"] = "LIVE_01"
            path = root / "selection.json"
            path.write_text(json.dumps(selection, ensure_ascii=False), "utf-8")
            before = path.read_bytes()
            output = root / "result"
            run = subprocess.run(
                [sys.executable, "-X", "utf8", str(PROJECT / "backtest_worker.py"),
                 "--csv", str(root / "missing-market.csv"), "--selection", str(path),
                 "--output", str(output), "--threads", "1", "--device", "cpu"],
                cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=45,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("永久删除", run.stdout + run.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((output / "全部回测结果.csv").exists())
            self.assertFalse((output / "数据能力自动剔除记录.json").exists())


class HistoricalFifthPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fifth-policy-history-")
        self.addCleanup(self.temporary.cleanup)
        self.helper = fixture.FingerprintLookupTests()
        self.helper.root = Path(self.temporary.name)
        self.directory, _, selection = self.helper.make_result()
        payload = json.loads((self.directory / lookup.RANKING_FILES[0]).read_text("utf-8"))
        template = dict(zip(payload["表头"], next(iter(payload["分类"].values()))[0]))
        selection["开仓指标"] = ["hist"]
        selection["开仓条件"]["1m"] = [42, *range(264, 366)]
        selection["入场触发口径"] = "LIVE_01"
        selection = 规范化配置(selection)
        self.original_selection = copy.deepcopy(selection)
        signature = 配置签名(selection)
        identity = json.loads((self.directory / "回测运行身份.json").read_text("utf-8"))
        identity["selection_signature"] = signature
        self.helper.write_json(self.directory / "组合选择.json", selection)
        self.helper.write_json(self.directory / "回测运行身份.json", identity)
        self.helper.write_json(self.directory / "断点记录.json", {
            "selection": selection, "run_identity": identity, "selection_signature": signature})
        stop_index = next(index for index, row in enumerate(生成止损组合()) if row[0] == "S1")
        self.rows = []
        for code in (288, 264):
            self.rows.append(dict(template, **{
                "基础策略编号": stable_base_id(0, (0, 0, 0, 0, code), stop_index),
                "开仓MACD口径": "MACD柱", "1分钟条件": CODE_NAMES[code], "入场触发口径": "F5_EVENT"}))
        self.payload = {"表头": list(self.rows[0]), "分类": {"分批止盈": [list(row.values()) for row in self.rows]}}
        for name in lookup.RANKING_FILES:
            self.helper.write_json(self.directory / name, self.payload)

    def restore(self, index):
        fingerprint = config_fingerprint(self.rows[index])
        match = lookup.find_fingerprint_matches(fingerprint, self.directory)[0]
        return match, lookup.restore_fingerprint_match(match)

    def test_allowed_fingerprint_from_old_mixed_103_method_selection_can_be_bound(self):
        before = (self.directory / "组合选择.json").read_bytes()
        match, restored = self.restore(0)
        self.assertEqual(restored["selection"]["开仓条件"]["1m"], [288])
        bound = lookup.bind_fingerprint_match(match, restored)
        self.assertEqual(bound["verified_snapshot"]["selection"]["开仓条件"]["1m"], [288])
        self.assertEqual((self.directory / "组合选择.json").read_bytes(), before)

    def test_retired_fingerprint_remains_readable_but_cannot_bind_merge_or_replay(self):
        match, restored = self.restore(1)
        self.assertEqual(restored["selection"]["开仓条件"]["1m"], [264])
        with self.assertRaisesRegex(ValueError, "永久删除"):
            lookup.bind_fingerprint_match(match, restored)
        with self.assertRaisesRegex(ValueError, "永久删除"):
            lookup.merge_fingerprint_restorations([restored])
        with self.assertRaisesRegex(ValueError, "永久删除"):
            batch.worker_command(PROJECT, restored, self.directory / "not-created", 1, "cpu", self.directory)

    def test_both_rows_export_complete_original_definitions_and_fingerprints(self):
        view = build_view(self.payload, self.directory)
        rows = [dict(zip(view["表头"], row)) for row in view["分类"]["分批止盈"]]
        self.assertEqual(len(rows), 2)
        for index, row in enumerate(rows):
            envelope = json.loads(row["完整单策略配置JSON"])
            self.assertEqual(envelope["selection"]["开仓条件"]["1m"], [[288], [264]][index])
            self.assertEqual(row["策略指纹"], config_fingerprint(self.rows[index]))
            self.assertIn("F5-", row["开仓规则"])
        self.assertEqual(配置签名(self.original_selection),
                         json.loads((self.directory / "回测运行身份.json").read_text("utf-8"))["selection_signature"])


if __name__ == "__main__":
    unittest.main()
