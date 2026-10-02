"""Queue records must stay bound to their verified original row and source context."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

import fingerprint_batch as B
import fingerprint_lookup as F
from ranking_view import config_fingerprint
from selection_config import 配置签名, 配置统计
import test_v149_fingerprint_lookup as fixture


class LookupSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.FingerprintLookupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def original(self, name="original", mode="FEE"):
        directory, fingerprint, _ = self.fixture.make_result(name, mode=mode)
        match = F.find_fingerprint_matches(fingerprint, directory)[0]
        restored = F.restore_fingerprint_match(match)
        return directory, match, restored, F.bind_fingerprint_match(match, restored)

    def replace_original_config(self, directory, change):
        # Model a legitimate replacement run: both selections and both stored
        # identities agree. A 16-character row fingerprint omits hidden controls.
        raw = json.loads((directory / "组合选择.json").read_text("utf-8"))
        change(raw)
        trade = copy.deepcopy(raw); trade.pop("候选筛选", None)
        signature = hashlib.sha256(json.dumps(trade, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
        identity = json.loads((directory / "回测运行身份.json").read_text("utf-8"))
        identity["selection_signature"] = signature
        checkpoint = json.loads((directory / "断点记录.json").read_text("utf-8"))
        checkpoint.update(selection=raw, selection_signature=signature, run_identity=identity)
        for name, value in (("组合选择.json", raw), ("断点记录.json", checkpoint), ("回测运行身份.json", identity)):
            self.fixture.write_json(directory / name, value)

    def manifest(self, matches):
        path = self.root / "manifest.json"
        self.fixture.write_json(path, {"version": 1, "strategies": matches})
        return path

    def fake_complete(self, command, project, control, output, index, total, fingerprint):
        (output / "全部回测结果.csv").write_text("synthetic\n1\n", "utf-8")
        restored = json.loads((output / "指纹来源.json").read_text("utf-8"))
        signature = 配置签名(restored["selection"])
        identity = {"selection_signature": signature, "feature_request": restored["request"],
                    "engine_version": restored["current_engine_version"], "code_sha256": restored["current_code_sha256"]}
        self.fixture.write_json(output / "回测数据说明.json", {
            "sources": restored["sources"], "request": restored["request"], **restored["period"]})
        self.fixture.write_json(output / "回测运行身份.json", identity)
        self.fixture.write_json(output / "断点记录.json", {
            "selection": restored["selection"], "selection_signature": signature, "run_identity": identity})
        return "completed", "test only; no worker process"

    def test_given_row_must_match_fingerprint_before_any_source_lookup(self):
        _, match, _, _ = self.original()
        for invalid_row in (None, {}, dict(match["row"], **{"名义倍数（倍）": 1.})):
            changed = dict(match, row=invalid_row)
            with self.subTest(row=invalid_row), mock.patch.object(F, "find_fingerprint_matches") as find:
                with self.assertRaises(ValueError):
                    F.restore_fingerprint_match(changed)
                find.assert_not_called()
            expected = "清单原始行必须是完整对象" if invalid_row is None else "原始行与策略指纹不匹配"
            with self.assertRaisesRegex(ValueError, expected):
                B.read_manifest(self.manifest([changed]))

    def test_fresh_record_cannot_silently_replace_missing_or_changed_unhashed_fields(self):
        _, match, _, _ = self.original()
        for kind in ("missing", "changed"):
            stale = copy.deepcopy(match)
            if kind == "missing":
                stale["row"].pop("开仓位置过滤说明")
            else:
                stale["row"]["开仓位置过滤说明"] = "another description"
            self.assertEqual(config_fingerprint(stale["row"]), match["fingerprint"])
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "重新核验"):
                F.restore_fingerprint_match(stale)

    def test_binding_is_independent_and_contains_exactly_one_complete_selection(self):
        _, match, restored, pinned = self.original()
        self.assertNotIn("verified_snapshot", match)
        snapshot = pinned["verified_snapshot"]
        self.assertEqual(set(snapshot), {"version", "identity_sha256", *F.SNAPSHOT_FIELDS})
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(配置统计(snapshot["selection"])["包含仓位完整组合数"], 1)
        self.assertNotIn("current_code_sha256", snapshot)
        self.assertNotIn("current_engine_version", snapshot)
        self.assertNotIn("warnings", snapshot)
        self.assertEqual(snapshot["selection"], restored["selection"])
        pinned["row"]["开仓位置过滤说明"] = "edited copy"
        snapshot["selection"]["资金约束"]["资金费率"] = .1
        self.assertNotEqual(pinned["row"], match["row"])
        self.assertEqual(restored["selection"]["资金约束"]["资金费率"], .0001)

    def test_valid_replacement_of_hidden_controls_is_rejected_for_pinned_queue(self):
        for key, value in (("最小S3距离", .004), ("资金费率", .002)):
            directory, match, restored, pinned = self.original(key)
            parent = "入场约束" if key == "最小S3距离" else "资金约束"
            self.replace_original_config(directory, lambda raw: raw[parent].update({key: value}))
            self.assertEqual(F.find_fingerprint_matches(match["fingerprint"], directory)[0]["row"], match["row"])
            # Legacy records can explicitly re-verify, but an existing pin cannot be refreshed silently.
            fresh = F.restore_fingerprint_match(match)
            self.assertEqual(fresh["selection"][parent][key], value)
            with self.assertRaisesRegex(ValueError, "加入列表后.*变化"):
                F.restore_fingerprint_match(pinned)
            with self.assertRaisesRegex(ValueError, "加入列表后.*变化"):
                F.bind_fingerprint_match(pinned, fresh)
            self.assertNotEqual(restored["origin_identity"], fresh["origin_identity"])

    def test_data_period_change_with_matching_original_identities_is_rejected(self):
        directory, _, _, pinned = self.original()
        meta = json.loads((directory / "回测数据说明.json").read_text("utf-8"))
        identity = json.loads((directory / "回测运行身份.json").read_text("utf-8"))
        checkpoint = json.loads((directory / "断点记录.json").read_text("utf-8"))
        meta["request"]["end"] = "1970-01-01T00:02:00Z"
        identity["feature_request"] = copy.deepcopy(meta["request"])
        checkpoint["run_identity"] = copy.deepcopy(identity)
        for name, value in (("回测数据说明.json", meta), ("回测运行身份.json", identity), ("断点记录.json", checkpoint)):
            self.fixture.write_json(directory / name, value)
        with self.assertRaisesRegex(ValueError, "加入列表后.*变化"):
            F.restore_fingerprint_match(pinned)

    def test_original_ranking_settings_change_is_not_silently_adopted(self):
        directory, _, _, pinned = self.original()
        self.fixture.write_json(directory / "排行榜设置.json", {"排行指标": "期末资金（USDC）", "门槛": []})
        with self.assertRaisesRegex(ValueError, "ranking_settings"):
            F.restore_fingerprint_match(pinned)

    def test_current_code_upgrade_warns_but_does_not_invalidate_original_snapshot(self):
        _, _, _, pinned = self.original()
        with mock.patch.object(F, "ENGINE_VERSION", "test-new-current-version"), \
                mock.patch.object(F, "build_run_identity", return_value={"code_sha256": "b" * 64}):
            restored = F.restore_fingerprint_match(pinned)
        self.assertEqual(restored["current_code_sha256"], "b" * 64)
        self.assertTrue(restored["warnings"])
        self.assertEqual(F.bind_fingerprint_match(pinned, restored)["verified_snapshot"], pinned["verified_snapshot"])

    def test_malformed_or_edited_snapshot_is_rejected(self):
        _, _, _, pinned = self.original()
        for kind in ("version", "missing", "selection"):
            changed = copy.deepcopy(pinned)
            if kind == "version": changed["verified_snapshot"]["version"] = 2
            elif kind == "missing": changed["verified_snapshot"].pop("request")
            else: changed["verified_snapshot"]["selection"]["仓位倍数"] = [1.]
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "快照"):
                F.restore_fingerprint_match(changed)

    def test_legacy_manifest_is_reverified_and_records_pins_without_rewriting_input_rows(self):
        _, match, _, _ = self.original()
        path = self.manifest([match])
        original_bytes = path.read_bytes()
        output = self.root / "batch"
        with mock.patch.object(B, "_run_child", side_effect=self.fake_complete), mock.patch.object(B, "emit") as emit:
            state = B.run_batch(path, output)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(path.read_bytes(), original_bytes)
        snapshot = json.loads((output / "批量输入快照.json").read_text("utf-8"))
        record = snapshot["strategies"][0]
        self.assertEqual(record["row"], match["row"])
        self.assertIn("verified_snapshot", record)
        self.assertIn("verified_manifest_sha256", state)
        self.assertTrue(any(call.args[0] == "warning" and "旧列表" in call.kwargs.get("message", "")
                            for call in emit.call_args_list))

    def test_changed_pinned_source_blocks_entire_batch_before_any_worker(self):
        _, _, _, first = self.original("first")
        directory, _, _, second = self.original("second", "SLIPPAGE")
        self.replace_original_config(directory, lambda raw: raw["资金约束"].update({"资金费率": .002}))
        with mock.patch.object(B, "_run_child") as child, mock.patch.object(B, "emit"):
            with self.assertRaisesRegex(ValueError, "加入列表后.*变化"):
                B.run_batch(self.manifest([first, second]), self.root / "batch")
        child.assert_not_called()

    def test_multimode_multisize_source_restores_only_exact_row_and_cost_mode(self):
        fingerprints = set()
        for cost_mode in ("FEE", "SLIPPAGE"):
            directory, match, _, _ = self.original(cost_mode, cost_mode)
            modes = ["LIVE_01", "TF_EVENT", "MACD_CYCLE"]
            self.replace_original_config(directory, lambda raw: raw.update({"入场触发口径": modes}))
            rows = [dict(match["row"], **{"入场触发口径": mode, "名义倍数（倍）": size})
                    for mode in modes for size in (1., 2.)]
            payload = {"表头": list(rows[0]), "分类": {"test": [list(row.values()) for row in rows]}}
            for name in F.RANKING_FILES:
                self.fixture.write_json(directory / name, payload)
            for row in rows:
                fingerprint = config_fingerprint(row)
                fingerprints.add(fingerprint)
                found = F.find_fingerprint_matches(fingerprint, directory)[0]
                restored = F.restore_fingerprint_match(found)
                chosen = restored["selection"]
                self.assertEqual(配置统计(chosen)["包含仓位完整组合数"], 1)
                self.assertEqual(chosen["入场触发口径"], row["入场触发口径"])
                self.assertIsInstance(chosen["入场触发口径"], str)
                self.assertEqual(chosen["仓位倍数"], [row["名义倍数（倍）"]])
                self.assertEqual(chosen["成本模式"], cost_mode)
                pinned = F.bind_fingerprint_match(found, restored)
                self.assertEqual(pinned["verified_snapshot"]["selection"], chosen)
                self.assertEqual(F.restore_fingerprint_match(pinned)["selection"], chosen)
        self.assertEqual(len(fingerprints), 12)

    def test_row_mode_outside_original_selection_is_rejected(self):
        directory, match, _, _ = self.original()
        row = dict(match["row"], **{"入场触发口径": "MACD_CYCLE"})
        payload = {"表头": list(row), "分类": {"test": [list(row.values())]}}
        for name in F.RANKING_FILES:
            self.fixture.write_json(directory / name, payload)
        found = F.find_fingerprint_matches(config_fingerprint(row), directory)[0]
        with self.assertRaisesRegex(ValueError, "入场触发口径不属于原运行选择"):
            F.restore_fingerprint_match(found)

    def test_worker_receives_pinned_source_fingerprint_and_completion_is_verified(self):
        _, _, restored, pinned = self.original()
        with mock.patch.object(B, "_run_child", side_effect=self.fake_complete) as child, mock.patch.object(B, "emit"):
            state = B.run_batch(self.manifest([pinned]), self.root / "batch")
        command = child.call_args.args[0]
        self.assertEqual(command[command.index("--expected-source-fingerprint") + 1], restored["source_fingerprint"])
        self.assertTrue(state["items"][0]["identity_verified"])

    def test_child_reporting_completed_with_wrong_actual_identity_stops_next_item(self):
        _, _, _, first = self.original("first")
        _, _, _, second = self.original("second", "SLIPPAGE")
        cases = (("组合选择.json", "selection"), ("断点记录.json", "checkpoint"),
                 ("回测数据说明.json", "source"), ("回测数据说明.json", "period"),
                 ("回测运行身份.json", "identity"), ("排行榜设置.json", "ranking"))
        for filename, field in cases:
            def corrupt(command, project, control, output, index, total, fingerprint):
                result = self.fake_complete(command, project, control, output, index, total, fingerprint)
                value = json.loads((output / filename).read_text("utf-8"))
                if field == "selection": value["入场触发口径"] = "MACD_CYCLE"
                elif field == "checkpoint": value["selection"]["仓位倍数"] = [1.]
                elif field == "source": value["request"]["fingerprint"] = "b" * 64
                elif field == "period": value["request"]["end"] = "1970-01-01T00:02:00Z"
                elif field == "identity": value["code_sha256"] = "b" * 64
                else: value["门槛"] = [{"指标": "胜率（%）", "条件": "最低值", "值": .9}]
                self.fixture.write_json(output / filename, value)
                return result
            with self.subTest(field=field), mock.patch.object(B, "_run_child", side_effect=corrupt) as child, \
                    mock.patch.object(B, "emit"):
                with self.assertRaisesRegex(ValueError, "子任务实际"):
                    B.run_batch(self.manifest([first, second]), self.root / field)
                self.assertEqual(child.call_count, 1)
            status = json.loads((self.root / field / B.STATUS_FILE).read_text("utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["items"][0]["status"], "failed")
            self.assertEqual(status["items"][1]["status"], "pending")

    def test_completed_child_missing_verification_files_is_not_accepted(self):
        _, _, _, pinned = self.original()
        def csv_only(command, project, control, output, index, total, fingerprint):
            (output / "全部回测结果.csv").write_text("not sufficient\n", "utf-8")
            return "completed", "unverified"
        with mock.patch.object(B, "_run_child", side_effect=csv_only), mock.patch.object(B, "emit"):
            with self.assertRaisesRegex(ValueError, "完成核验缺少有效"):
                B.run_batch(self.manifest([pinned]), self.root / "batch")


if __name__ == "__main__":
    unittest.main()
