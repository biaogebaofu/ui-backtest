"""Editor projection must include all chosen rules without mixing global settings."""
import copy
import unittest
from unittest import mock

import fingerprint_lookup as F
from selection_config import 配置统计
import test_v149_fingerprint_lookup as fixture


class FingerprintMergeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.FingerprintLookupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        directory, fingerprint, _ = self.fixture.make_result()
        self.base = F.restore_fingerprint_match(F.find_fingerprint_matches(fingerprint, directory)[0])

    def profile(self, case=152, size=10.0, mode="TF_EVENT"):
        result = copy.deepcopy(self.base)
        result["selection"]["开仓条件"]["1m"] = [case]
        result["selection"]["仓位倍数"] = [size]
        result["selection"]["入场触发口径"] = mode
        return result

    def test_four_requested_profiles_sync_case5_and152_and_three_sizes(self):
        profiles = [self.profile(152, size) for size in (10., 9., 8.)] + [self.profile(5)]
        before = copy.deepcopy(profiles)
        with mock.patch.object(F, "source_bundle_fingerprint", side_effect=AssertionError("already verified")):
            merged = F.merge_fingerprint_restorations(profiles)
        self.assertEqual(merged["selection"]["开仓条件"]["1m"], [5, 152])
        self.assertEqual(merged["selection"]["仓位倍数"], [8., 9., 10.])
        self.assertEqual(merged["selection"]["入场触发口径"], "TF_EVENT")
        self.assertEqual((merged["exact_count"], merged["unique_count"],
                          merged["projected_count"], merged["extra_count"]), (4, 4, 6, 2))
        self.assertEqual(profiles, before)
        merged["selection"]["资金约束"]["初始资金USDC"] = 1.
        self.assertEqual(profiles, before)

    def test_mode_and_position_are_independent_projection_dimensions(self):
        first, second = self.profile(), self.profile(5, 8., "MACD_CYCLE")
        first["selection"]["开仓位置过滤"] = ["OFF"]
        merged = F.merge_fingerprint_restorations([first, second])
        self.assertEqual(merged["selection"]["入场触发口径"], ["TF_EVENT", "MACD_CYCLE"])
        self.assertEqual(set(merged["selection"]["开仓位置过滤"]), {"OFF", "RANGE60_EDGE20"})
        self.assertEqual(配置统计(merged["selection"])["包含仓位完整组合数"], 16)

    def test_global_parameters_cannot_be_silently_taken_from_first_profile(self):
        changes = [
            ("资金约束", "初始资金USDC", 8000.),
            ("资金约束", "最大开仓数量ETH", 20.),
            ("资金约束", "资金费率", .002),
            ("资金约束", "保护止损浮亏比例", .8),
            ("入场约束", "最小S3距离", .004),
            ("成交偏移", "开仓", .001),  # Also protect inactive offset values in fee mode.
            ("手续费", "开仓费率", .003),
            ("手续费", "BNB抵扣", False),
            ("候选筛选", "p95往返偏移", .0004),
            ("平仓后最小开仓间隔分钟", None, 6),
            ("成交价格口径", None, "THEORETICAL"),
        ]
        for key, child, value in changes:
            profiles = [self.profile(), self.profile(5)]
            if child is None:
                profiles[1]["selection"][key] = value
            else:
                profiles[1]["selection"][key][child] = value
            before = copy.deepcopy(profiles)
            with self.subTest(key=key, child=child), self.assertRaisesRegex(ValueError, key):
                F.merge_fingerprint_restorations(profiles)
            self.assertEqual(profiles, before)

    def test_dates_sources_ranking_and_calculation_context_must_match(self):
        for key in ("sources", "start", "end", "request", "source_fingerprint", "period", "capabilities",
                    "ranking_settings", "engine_version", "code_sha256", "current_engine_version", "current_code_sha256"):
            profiles = [self.profile(), self.profile(5)]
            profiles[1][key] = {"different": True} if isinstance(profiles[1][key], dict) else "different"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "不能合并"):
                F.merge_fingerprint_restorations(profiles)

    def test_duplicate_config_does_not_inflate_editor_count(self):
        merged = F.merge_fingerprint_restorations([self.profile(), self.profile()])
        self.assertEqual((merged["exact_count"], merged["unique_count"],
                          merged["projected_count"], merged["extra_count"]), (2, 1, 1, 0))

    def test_only_complete_verified_single_strategies_can_be_projected(self):
        bad = self.profile(); bad["selection"]["仓位倍数"] = [8., 10.]
        missing = self.profile(); missing.pop("capabilities")
        for value in ([], None, [bad], [missing]):
            with self.subTest(value=value is None), self.assertRaises(ValueError):
                F.merge_fingerprint_restorations(value)


if __name__ == "__main__":
    unittest.main()
