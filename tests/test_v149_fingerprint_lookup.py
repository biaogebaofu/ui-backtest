import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fingerprint_lookup as F
from account_statistics import ENGINE_VERSION
from data_sources import source_bundle_fingerprint
from execution_settings import effective_fee_rates, effective_slippage
from extended_rules import stable_base_id
from ranking_view import CODE_NAMES, config_fingerprint
from selection_config import 全选配置, 规范化配置, 配置统计
from strategy_space import 生成止损组合


class FingerprintLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fingerprint_lookup_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def make_result(self, name="result", mode="FEE", s3=.0025, legacy_position=False):
        directory = self.root / name
        directory.mkdir(parents=True)
        data = directory / "market.csv"
        data.write_text("openTime,open,high,low,close,volume\n0,100,101,99,100,1\n", encoding="utf-8")
        sources = {key: "" for key in F.SOURCE_KEYS}
        sources["kline"] = str(data)
        raw = 全选配置()
        raw.update({
            "开仓指标": ["hist", "dif"],
            "开仓条件": {"4h": [0], "1h": [0], "15m": [0], "5m": [0], "1m": [156, 263]},
            "开仓位置过滤": ["OFF", "RANGE60_EDGE20"],
            "止损代码": ["S1"], "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
            "止盈方案编号": [2, 2421], "止盈后等待分钟": [0, 5], "仓位倍数": [1., 2.],
            "开仓方向": ["BOTH", "LONG"], "交易会话": ["ALL"], "强制时间止损分钟": [0, 30],
            "成本模式": mode, "成交偏移": {"开仓": .0003, "平仓": .0004},
            "手续费": {"开仓费率": .0005, "平仓费率": .0002, "BNB抵扣": True, "返佣比例": .2},
            "平仓后最小开仓间隔分钟": 7,
            "入场约束": {"最小S3距离": s3, "S3基线周期": "5m"},
            "资金约束": {"初始资金USDC": 7003., "最小开仓数量ETH": .03, "最大开仓数量ETH": 40.,
                     "低于最小数量停止": True, "保护止损浮亏比例": .73, "维持保证金率": .006,
                     "启用全仓强平": False, "资金费率": .0001},
        })
        selected = 规范化配置(raw)
        stop_index = next(i for i, (code, _, _) in enumerate(生成止损组合()) if code == "S1")
        fees = effective_fee_rates(selected)
        slip = effective_slippage(selected)
        row = {
            "止盈方案编号": 2421, "止盈后等待分钟": 5,
            "基础策略编号": stable_base_id(1, (0, 0, 0, 0, 263), stop_index),
            "开仓MACD口径": "DIF线", "4小时条件": CODE_NAMES[0], "1小时条件": CODE_NAMES[0],
            "15分钟条件": CODE_NAMES[0], "5分钟条件": CODE_NAMES[0], "1分钟条件": CODE_NAMES[263],
            "入场触发口径": selected["入场触发口径"], "止损代码": "S1", "固定止损代码": "OFF",
            "叠加止盈代码": "OFF", "开仓方向": "LONG", "交易会话": "ALL", "强制时间止损（分钟）": 30,
            "名义倍数（倍）": 2., "初始资金（USDC）": 7003., "ETH最小开仓数量（ETH）": .03,
            "ETH单次最大开仓数量（ETH）": 40., "ETH最小开仓约束启用（0否1是）": 1,
            "开仓成交偏移（%）": slip[0], "平仓成交偏移（%）": slip[1],
            "回测计算版本": ENGINE_VERSION + ":" + selected["成交价格口径"],
            "成交价格口径": selected["成交价格口径"], "平仓后最小开仓间隔（分钟）": 7,
            "开仓基础手续费率（%）": .0005, "平仓基础手续费率（%）": .0002,
            "BNB手续费抵扣（0否1是）": 1, "手续费返佣比例（%）": .2,
            "开仓净手续费率（%）": fees[0], "平仓净手续费率（%）": fees[1], "成本模式": mode,
            "开仓位置过滤代码": "RANGE60_EDGE20", "开仓位置过滤说明": "原位置说明",
        }
        if legacy_position:
            selected.pop("开仓位置过滤")
            selected["版本"] = 19
            row.pop("开仓位置过滤代码")
            row.pop("开仓位置过滤说明")
        request = {"start": "1970-01-01T00:00:00Z", "end": "1970-01-01T00:01:00Z",
                   "fingerprint": source_bundle_fingerprint(sources), "feature_version": 7}
        raw_trade = copy.deepcopy(selected)
        raw_trade.pop("候选筛选", None)
        signature = hashlib.sha256(json.dumps(raw_trade, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
        identity = {"feature_request": request, "engine_version": ENGINE_VERSION,
                    "code_sha256": "a" * 64, "selection_signature": signature}
        meta = {"request": request, "sources": sources, "start_utc": request["start"],
                "end_utc": request["end"], "capabilities": {"ohlcv": True, "delta_cvd": True}}
        payload = {"表头": list(row), "分类": {"分批止盈": [list(row.values())]}}
        for filename in F.RANKING_FILES:
            self.write_json(directory / filename, payload)
        self.write_json(directory / "组合选择.json", selected)
        self.write_json(directory / "断点记录.json", {"selection": selected, "run_identity": identity,
                                                      "selection_signature": signature})
        self.write_json(directory / "回测数据说明.json", meta)
        self.write_json(directory / "回测运行身份.json", identity)
        self.write_json(directory / "排行榜设置.json", {"排行指标": "胜率（%）", "门槛": []})
        return directory, config_fingerprint(row), selected

    def restore(self, directory, fp):
        matches = F.find_fingerprint_matches(fp, directory)
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(matches[0]["ranking_files"]), 2)
        return F.restore_fingerprint_match(matches[0])

    def test_multi_choice_contracts_to_one_without_losing_hidden_parameters(self):
        directory, fp, original = self.make_result()
        restored = self.restore(directory, fp)
        selection = restored["selection"]
        self.assertEqual(配置统计(selection)["包含仓位完整组合数"], 1)
        self.assertEqual(selection["开仓指标"], ["dif"])
        self.assertEqual(selection["开仓条件"]["1m"], [263])
        self.assertEqual(selection["开仓位置过滤"], ["RANGE60_EDGE20"])
        self.assertEqual(selection["止盈方案编号"], [2421])
        self.assertEqual(selection["仓位倍数"], [2.])
        self.assertEqual(selection["资金约束"], original["资金约束"])
        self.assertEqual(selection["入场约束"], original["入场约束"])
        self.assertEqual(selection["手续费"], original["手续费"])
        self.assertEqual(selection["成交偏移"], original["成交偏移"])
        self.assertEqual(selection["平仓后最小开仓间隔分钟"], 7)
        self.assertEqual(restored["start"], "1970-01-01T00:00:00Z")
        self.assertEqual(restored["end"], "1970-01-01T00:01:00Z")
        self.assertEqual(set(restored["sources"]), set(F.SOURCE_KEYS))
        self.assertEqual(restored["ranking_settings"]["排行指标"], "胜率（%）")

    def test_slippage_and_legacy_missing_position_are_preserved(self):
        directory, fp, original = self.make_result(mode="SLIPPAGE", legacy_position=True)
        restored = self.restore(directory, fp)
        self.assertEqual(restored["selection"]["成本模式"], "SLIPPAGE")
        self.assertEqual(restored["selection"]["开仓位置过滤"], ["OFF"])
        self.assertEqual(effective_fee_rates(restored["selection"]), (0., 0.))

    def test_same_fingerprint_different_contexts_remain_separate_sources(self):
        first, fp, _ = self.make_result("first", s3=.001)
        second, other_fp, _ = self.make_result("second", s3=.02)
        self.assertEqual(fp, other_fp)
        with mock.patch.object(F, "source_bundle_fingerprint", side_effect=AssertionError("find reads market data")):
            matches = F.find_fingerprint_matches(fp.upper(), self.root)
        self.assertEqual({item["source_dir"] for item in matches}, {str(first), str(second)})
        self.assertEqual({F.restore_fingerprint_match(item)["selection"]["入场约束"]["最小S3距离"]
                          for item in matches}, {.001, .02})

    def test_search_is_not_recursive_and_rejects_other_fingerprint_formats(self):
        directory, fp, _ = self.make_result("nested/result")
        self.assertEqual(F.find_fingerprint_matches(fp, self.root), [])
        self.assertEqual(len(F.find_fingerprint_matches(fp, directory)), 1)
        for invalid in ("x" * 16, "a" * 20, ""):
            with self.assertRaises(ValueError):
                F.find_fingerprint_matches(invalid, self.root)

    def test_checkpoint_conflict_and_missing_hidden_parameter_are_rejected(self):
        for missing in (False, True):
            directory, fp, _ = self.make_result(f"bad_{missing}")
            path = directory / "组合选择.json"
            raw = json.loads(path.read_text("utf-8"))
            if missing:
                raw["资金约束"].pop("维持保证金率")
            else:
                raw["入场约束"]["最小S3距离"] = .05
            self.write_json(path, raw)
            with self.assertRaises(ValueError):
                self.restore(directory, fp)

    def test_changed_data_missing_capability_and_invalid_ranking_settings_are_rejected(self):
        for problem in ("data", "capability", "ranking"):
            directory, fp, _ = self.make_result(problem)
            if problem == "data":
                (directory / "market.csv").write_text("changed", encoding="utf-8")
            elif problem == "capability":
                path = directory / "回测数据说明.json"
                meta = json.loads(path.read_text("utf-8"))
                meta["capabilities"]["delta_cvd"] = False
                self.write_json(path, meta)
            else:
                self.write_json(directory / "排行榜设置.json", {"排行指标": "unknown", "门槛": []})
            with self.assertRaises(ValueError):
                self.restore(directory, fp)

    def test_signed_selection_change_in_both_files_is_rejected(self):
        directory, fp, _ = self.make_result()
        for name in ("组合选择.json", "断点记录.json"):
            path = directory / name
            value = json.loads(path.read_text("utf-8"))
            selected = value if name == "组合选择.json" else value["selection"]
            selected["资金约束"]["资金费率"] = .002
            self.write_json(path, value)
        with self.assertRaisesRegex(ValueError, "配置签名"):
            self.restore(directory, fp)

    def test_invalid_original_values_are_rejected_even_with_matching_identity(self):
        mutations = (
            (("入场触发口径",), "UNKNOWN"),
            (("开仓方向",), ["LONG", "UNKNOWN"]),
            (("交易会话",), ["ALL", "UNKNOWN"]),
            (("叠加止盈代码",), ["UNKNOWN"]),
            (("固定止损代码",), ["UNKNOWN"]),
            (("强制时间止损分钟",), [0, 30, 999]),
            (("开仓指标",), ["hist", "dif", "UNKNOWN"]),
            (("资金约束", "低于最小数量停止"), "false"),
            (("资金约束", "启用全仓强平"), "false"),
            (("手续费", "BNB抵扣"), "false"),
            (("资金约束", "资金费率"), "0.0001"),
            (("候选筛选", "自动导出"), "false"),
            (("候选筛选", "每类最多"), 4.5),
        )
        for index, (keys, value) in enumerate(mutations):
            with self.subTest(keys=keys):
                directory, fp, selected = self.make_result(f"invalid_{index}")
                target = selected
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                trade = copy.deepcopy(selected)
                trade.pop("候选筛选")
                signature = hashlib.sha256(json.dumps(trade, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")).encode()).hexdigest()
                identity = json.loads((directory / "回测运行身份.json").read_text("utf-8"))
                identity["selection_signature"] = signature
                self.write_json(directory / "组合选择.json", selected)
                self.write_json(directory / "回测运行身份.json", identity)
                self.write_json(directory / "断点记录.json", {"selection": selected,
                                "selection_signature": signature, "run_identity": identity})
                with self.assertRaisesRegex(ValueError, "原运行组合选择"):
                    self.restore(directory, fp)

    def test_same_engine_version_with_different_code_hash_warns_without_blocking(self):
        directory, fp, _ = self.make_result()
        restored = self.restore(directory, fp)
        self.assertEqual(restored["engine_version"], restored["current_engine_version"])
        self.assertNotEqual(restored["code_sha256"], restored["current_code_sha256"])
        self.assertTrue(any("核心计算代码指纹" in warning for warning in restored["warnings"]))


if __name__ == "__main__":
    unittest.main()
