"""F5自主事件接入、已闭合周期映射和旧MACD限次隔离。"""
import unittest
from unittest import mock

import numpy as np

from test_support import prepare_test_features
prepare_test_features()
import backtest_engine as B
from extended_signals import entry_masks
from extended_rules import (ENTRY_RULES, ENTRY_RULE_BATCH, FIFTH_ROUND_CODES,
                            FIFTH_SPECS, entry_supported_timeframes, entry_unavailable_reason)
from indicator_combinations import DEFAULT_REGISTRY


def candidate_fixture(n=6):
    state = {"d1": -np.ones(n, np.int8), "ma120": np.full(n, 100.)}
    hi = {tf: {0: (np.ones(n, bool), np.ones(n, bool))}
          for tf in ("4h", "1h", "15m", "5m")}
    for tf in hi:
        state[tf + "_close_event"] = np.ones(n, bool)
    long = np.array([False, True, False, True, False, False])
    short = np.array([False, False, False, False, True, False])
    state["extra_288"] = long, short
    return state, hi, long, short


class FifthRegistryTests(unittest.TestCase):
    def test_all_research_codes_have_frozen_fifth_namespace(self):
        self.assertEqual(tuple(FIFTH_ROUND_CODES), tuple(range(264, 384)))
        self.assertTrue(all(code in ENTRY_RULES for code in FIFTH_ROUND_CODES))
        self.assertTrue(all(ENTRY_RULE_BATCH[code] == 5 for code in FIFTH_ROUND_CODES))
        self.assertEqual([ENTRY_RULE_BATCH[c] for c in (5, 18, 144, 263)], [2, 3, 4, 4])

    def test_native_timeframes_and_pending_algorithms_are_not_silently_allowed(self):
        for code, spec in FIFTH_SPECS.items():
            with self.subTest(code=code):
                self.assertEqual(set(entry_supported_timeframes(code)), set(spec["supported_timeframes"]))
                for tf in spec["supported_timeframes"]:
                    reason = entry_unavailable_reason(code, {cap: True for cap in spec["requires"]}, tf)
                    if spec["unavailable_reason"]:
                        self.assertTrue(reason)
        self.assertTrue(entry_unavailable_reason(286, {"ohlcv": True}, "5m"))


class FifthCandidateTests(unittest.TestCase):
    def setUp(self):
        B.cases_use_fifth.cache_clear()
        B.build_field.cache_clear()
        B.extended_entry_data.cache_clear()
        self.addCleanup(B.build_field.cache_clear)
        self.addCleanup(B.extended_entry_data.cache_clear)

    def test_fifth_ignores_implicit_macd_direction_flip_and_neutral_warmup(self):
        state, hi, long, short = candidate_fixture()
        # Old OFF arrays carry MACD's global warmup; F5 must regard OFF as neutral.
        for values in hi.values():
            values[0] = np.zeros(6, bool), np.zeros(6, bool)
        for mode in ("LIVE_01", "TF_EVENT", "MACD_CYCLE", "F5_EVENT"):
            with self.subTest(mode=mode), mock.patch.object(B, "N", 6):
                cand, side = B.build_candidates(state, hi, (0, 0, 0, 0, 288), entry_mode=mode)
            self.assertEqual(cand.tolist(), [1, 3, 4])
            self.assertEqual(side.tolist(), [1, 1, -1])

    def test_high_timeframe_fifth_can_supply_direction_with_one_minute_off(self):
        state, hi, long, short = candidate_fixture()
        hi["5m"][288] = long, short
        with mock.patch.object(B, "N", 6):
            cand, side = B.build_candidates(state, hi, (0, 0, 0, 288, 0), entry_mode="TF_EVENT")
        self.assertEqual(cand.tolist(), [1, 3, 4])
        self.assertEqual(side.tolist(), [1, 1, -1])

    def test_explicit_legacy_and_position_filters_are_still_applied(self):
        state, hi, long, short = candidate_fixture()
        keep = np.zeros(6, bool); keep[3] = True
        hi["4h"][1] = keep, np.zeros(6, bool)
        with mock.patch.object(B, "N", 6):
            cand, side = B.build_candidates(state, hi, (1, 0, 0, 0, 288))
            gated, _ = B.build_candidates(state, hi, (0, 0, 0, 0, 288), (keep, keep))
        self.assertEqual(cand.tolist(), [3])
        self.assertEqual(side.tolist(), [1])
        self.assertEqual(gated.tolist(), [3])

    def test_fifth_and_old_rule_combination_preserves_explicit_same_direction_intersection(self):
        state, hi, long, short = candidate_fixture()
        keep = np.zeros(6, bool); keep[3] = True
        state["extra_42"] = keep, np.zeros(6, bool)
        code = DEFAULT_REGISTRY.register("entry", [42, 288], "AND")
        with mock.patch.object(B, "N", 6):
            cand, side = B.build_candidates(state, hi, (0, 0, 0, 0, code), entry_mode="TF_EVENT")
        self.assertEqual(cand.tolist(), [3])
        self.assertEqual(side.tolist(), [1])

    def test_legacy_rule_without_fifth_keeps_old_direction_and_flip_logic(self):
        state, hi, _, _ = candidate_fixture()
        with mock.patch.object(B, "N", 6):
            cand, side = B.build_candidates(state, hi, (0, 0, 0, 0, 1))
            events, _ = B.build_candidates(state, hi, (0, 0, 0, 0, 1), entry_mode="TF_EVENT")
        self.assertEqual(cand.tolist(), list(range(6)))
        self.assertEqual(side.tolist(), [-1] * 6)
        self.assertEqual(events.tolist(), [])

    def test_macd_cycle_gate_is_scoped_to_legacy_cases_in_mixed_batch(self):
        state, hi, long, short = candidate_fixture()
        fifth = (0, 0, 0, 0, 288); legacy = (0, 0, 0, 0, 1)
        with mock.patch.object(B, "N", 6), mock.patch.object(B.R, "build", return_value=(state, {}, hi, {})), \
             mock.patch.object(B.E, "D", {"m1_hist": np.zeros(6)}), \
             mock.patch.object(B, "extended_entry_data", return_value=(long, short)):
            *_, entries, stops = B.build_field("hist", (fifth, legacy), ("__NONE__",), entry_mode="MACD_CYCLE")
        self.assertEqual(entries[0][1].tolist(), [1, 3, 4])
        self.assertEqual(entries[1][1].tolist(), [])

    def test_event_run_ids_do_not_suppress_later_fifth_event_in_same_macd_segment(self):
        legacy = np.ones(6, np.int32)
        self.assertIs(B.entry_run_id_for_cases((0, 0, 0, 0, 42), legacy), legacy)
        actual = B.entry_run_id_for_cases((0, 0, 0, 0, 288), legacy)
        self.assertEqual(actual.tolist(), list(range(6)))
        self.assertFalse(actual.flags.writeable)
        self.assertEqual(B.effective_entry_mode((0, 0, 0, 0, 42), "LIVE_01"), "LIVE_01")
        self.assertEqual(B.effective_entry_mode((0, 0, 0, 0, 288), "LIVE_01"), "F5_EVENT")

    def test_single_fifth_build_field_already_returns_event_run_ids(self):
        state, hi, long, short = candidate_fixture()
        with mock.patch.object(B, "N", 6), mock.patch.object(B.R, "build", return_value=(state, {}, hi, {})), \
             mock.patch.object(B, "extended_entry_data", return_value=(long, short)):
            result = B.build_field("hist", ((0, 0, 0, 0, 288),), ("__NONE__",))
        self.assertEqual(result[2].tolist(), list(range(6)))

    def test_mapping_emits_only_on_native_close_and_passes_actual_buy_fields(self):
        n = 11
        native = np.array([False, True])
        data = {f"5m_{key}": np.array([100., 101.])
                for key in ("open", "high", "low", "close", "volume", "hist", "ma20")}
        data.update({"ct1": np.arange(1, n + 1) * 60000,
                     "5m_ct": np.array([5, 10]) * 60000,
                     "5m_map": np.array([-1, -1, -1, -1, 0, 0, 0, 0, 0, 1, 1]),
                     "5m_taker_buy_base": np.array([12., 15.]),
                     "5m_taker_buy_quote": np.array([1200., 1515.])})
        with mock.patch.object(B, "N", n), mock.patch.object(B.E, "D", data), \
             mock.patch.object(B, "entry_masks", return_value=(native, np.zeros(2, bool))) as masks:
            result = B.extended_entry_data("5m", "hist", 288)
            alternate = B.extended_entry_data("5m", "dif", 288)
            self.assertIs(alternate, result)
            self.assertEqual(masks.call_count, 1)
        self.assertEqual(np.flatnonzero(result[0]).tolist(), [9])
        self.assertFalse(result[0][10], "a closed 5m event cannot repeat on the next minute")
        np.testing.assert_array_equal(masks.call_args.kwargs["extras"]["taker_buy_base"], [12., 15.])
        np.testing.assert_array_equal(masks.call_args.kwargs["extras"]["taker_buy_quote"], [1200., 1515.])

    def test_dispatch_calls_fifth_before_fourth_and_keeps_direction_output(self):
        n = 6; values = np.full(n, 100.)
        long = np.zeros(n, bool); long[3] = True
        with mock.patch("fifth_batch.fifth_masks", return_value=(long, np.zeros(n, bool))) as masks:
            result = entry_masks(288, -np.ones(n), values, values, values, values, values,
                                 values, values, np.arange(n) * 60000, timeframe="1m")
        self.assertEqual(masks.call_args.args[0], 288)
        self.assertEqual(np.flatnonzero(result[0]).tolist(), [3])

    def test_real_native_rule_reaches_candidates_without_repeating_high_period_events(self):
        # 保留的CUSUM需累计漂移；在已收盘5m末段加入确定性趋势，
        # 保留完整特征→原生规则→候选路径，避免依赖已退休的HiLo。
        data = dict(B.E.D)
        close = data["5m_close"].copy()
        close[-15:] = close[-16] + np.arange(1, 16) * 10.
        opening = np.r_[close[0], close[:-1]]
        data.update({"5m_close": close, "5m_open": opening,
                     "5m_high": np.maximum(opening, close) + .5,
                     "5m_low": np.minimum(opening, close) - .5})
        cases = (0, 0, 0, 288, 0)
        with mock.patch.object(B.E, "D", data):
            result = B.build_field("hist", (cases,), ("__NONE__",), materialize_stops=False)
            expected = B.extended_entry_data("5m", "hist", 288)
        _, cand, side = result[3][0]
        self.assertGreater(len(cand), 0)
        self.assertTrue(np.all(B.E.D["ct1"][cand] % (5 * 60000) == 0))
        np.testing.assert_array_equal(cand, np.flatnonzero(expected[0] | expected[1]))
        np.testing.assert_array_equal(side, np.where(expected[0][cand], 1, -1))
        self.assertEqual(len(np.unique(result[2][cand])), len(cand))


if __name__ == "__main__":
    unittest.main()
