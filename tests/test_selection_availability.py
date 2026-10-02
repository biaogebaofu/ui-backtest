"""缺字段自动剔除使用各周期证据，精确指纹不可被改成新策略。"""
import copy
import unittest
from unittest import mock

from data_sources import CAPABILITY_LABELS
from selection_availability import prune_unavailable_selection
from selection_config import 全选配置, 配置签名


def selected():
    value = 全选配置()
    value["开仓条件"] = {tf: [0] for tf in value["开仓条件"]}
    value["开仓条件"]["1m"] = [1]
    return value


def kline_capabilities():
    caps = dict.fromkeys(CAPABILITY_LABELS, False)
    caps["ohlcv"] = True
    return caps, {tf: dict(caps) for tf in ("4h", "1h", "15m", "5m")}


class SelectionAvailabilityTests(unittest.TestCase):
    def test_funding_and_oi_are_removed_at_every_timeframe_but_price_rules_remain(self):
        value = selected()
        value["开仓条件"] = {tf: [42, 128, 129, 130, 131] for tf in value["开仓条件"]}
        caps, per_tf = kline_capabilities()
        result = prune_unavailable_selection(value, caps, per_tf)
        self.assertEqual(len(result["removed"]), 20)
        self.assertEqual(result["selection"]["开仓条件"], {tf: [42] for tf in value["开仓条件"]})
        self.assertTrue(result["runnable"])

    def test_global_capability_cannot_borrow_missing_high_period_data(self):
        value = selected(); value["开仓条件"]["4h"] = [128]
        value["开仓条件"]["1m"] = [128]
        caps, per_tf = kline_capabilities(); caps["funding"] = True
        result = prune_unavailable_selection(value, caps, per_tf)
        self.assertEqual(result["selection"]["开仓条件"]["4h"], [0])
        self.assertEqual(result["selection"]["开仓条件"]["1m"], [128])

    def test_real_enhanced_csv_fields_remain_available(self):
        value = selected(); value["开仓条件"]["1m"] = [58, 140, 128]
        caps, per_tf = kline_capabilities(); caps.update(trades=True, quote_volume=True)
        result = prune_unavailable_selection(value, caps, per_tf)
        self.assertEqual(result["selection"]["开仓条件"]["1m"], [58, 140])

    def test_empty_period_is_off_and_all_empty_does_not_invent_a_strategy(self):
        value = selected(); value["开仓条件"] = {tf: [128] for tf in value["开仓条件"]}
        caps, per_tf = kline_capabilities()
        result = prune_unavailable_selection(value, caps, per_tf)
        self.assertFalse(result["runnable"])
        self.assertTrue(all(codes == [0] for codes in result["selection"]["开仓条件"].values()))
        self.assertIn("没有可运行", result["message"])

    def test_invalid_combination_size_is_disabled_with_explanation(self):
        value = selected(); value["开仓条件"]["1m"] = [42, 128, 129]
        value["指标组合"] = {"开仓": {"1m": {"启用": True, "组合数量": [3], "保留单项": False, "逻辑": "AND"}}}
        caps, per_tf = kline_capabilities()
        result = prune_unavailable_selection(value, caps, per_tf)
        self.assertNotIn("指标组合", result["selection"])
        self.assertEqual(result["disabled_combinations"][0]["周期"], "1m")
        self.assertIn("原组合数量不再有效", result["message"])

    def test_exact_fingerprint_is_skipped_without_changing_its_definition(self):
        value = selected(); value["开仓条件"]["4h"] = [128]
        before = copy.deepcopy(value); signature = 配置签名(value)
        caps, per_tf = kline_capabilities()
        result = prune_unavailable_selection(value, caps, per_tf, exact=True)
        self.assertFalse(result["runnable"])
        self.assertEqual(result["selection"], before)
        self.assertEqual(配置签名(result["selection"]), signature)
        self.assertEqual(value, before)
        self.assertIn("原定义", result["message"])


class ExactListAvailabilityTests(unittest.TestCase):
    def test_preflight_skips_only_data_capability_failures_and_keeps_list_positions(self):
        import fingerprint_batch as batch
        matches = [{"fingerprint": "first"}, {"fingerprint": "second"}]
        missing = ValueError("原数据能力不足以运行该策略：[(4h,128)]")
        with mock.patch.object(batch, "restore_many", side_effect=missing), \
             mock.patch.object(batch, "restore_fingerprint_match", side_effect=[missing, {"selection": "original"}]):
            verified, skipped = batch._restore_available_matches(matches)
        self.assertIsNone(verified[0])
        self.assertEqual(verified[1], {"selection": "original"})
        self.assertEqual(list(skipped), [0])
        self.assertEqual(matches, [{"fingerprint": "first"}, {"fingerprint": "second"}])

    def test_identity_errors_are_not_silently_skipped(self):
        import fingerprint_batch as batch
        with mock.patch.object(batch, "restore_many", side_effect=ValueError("原始配置签名不一致")):
            with self.assertRaisesRegex(ValueError, "签名不一致"):
                batch._restore_available_matches([{}])


if __name__ == "__main__":
    unittest.main()
