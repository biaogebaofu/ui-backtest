"""Indicator composition preserves confirmed events and existing exit/account paths."""
import unittest
from unittest import mock

import numpy as np

from test_support import prepare_test_features
prepare_test_features()
import backtest_engine as B
import account_replay as A
from extended_rules import stable_base_id, composite_stop_index
from indicator_combinations import DEFAULT_REGISTRY, choices_for_config, stop_spec
from selection_config import 全选配置
from backtest_worker import 开仓批次, LazyTakeProfits, LazyRowCounts, scan_row_counts


def entry_fixture(n=6):
    ones = np.ones(n, dtype=bool)
    zeros = np.zeros(n, dtype=bool)
    state = {"d1": np.array([1, 1, -1, -1, 1, 1]), "ma120": np.full(n, 100.)}
    hi = {tf: {0: (ones, ones), 1: (ones, zeros), 2: (ones, zeros),
               3: (ones, zeros), 4: (ones, zeros)} for tf in ("4h", "1h", "15m", "5m")}
    for tf in hi:
        state[tf + "_close_event"] = ones.copy()
        state[tf + "_entry3_long"] = zeros.copy()
        state[tf + "_entry3_short"] = zeros.copy()
        state[tf + "_entry4_long"] = zeros.copy()
        state[tf + "_entry4_short"] = zeros.copy()
    return state, hi


class EntryCombinationTests(unittest.TestCase):
    def test_one_minute_and_requires_each_member_in_same_direction(self):
        state, hi = entry_fixture()
        for tf in hi:
            hi[tf][0] = (np.ones(6, bool), np.ones(6, bool))
        code = DEFAULT_REGISTRY.register("entry", [1, 2], "AND")
        with mock.patch.multiple(B, N=6, CLOSE=np.array([101., 99., 99., 101., 100., 102.])):
            cand, side = B.build_candidates(state, hi, (0, 0, 0, 0, code))
        self.assertEqual(cand.tolist(), [0, 2, 5])
        self.assertEqual(side.tolist(), [1, -1, 1])

    def test_lowest_composite_timeframe_requires_its_confirmed_event(self):
        state, hi = entry_fixture()
        state["5m_entry3_long"][[1, 4]] = True
        state["5m_close_event"][:] = False
        state["5m_close_event"][[1, 5]] = True
        code = DEFAULT_REGISTRY.register("entry", [2, 3], "AND")
        hi["5m"][code] = hi["5m"][2]
        with mock.patch.object(B, "N", 6):
            cand, side = B.build_candidates(state, hi, (0, 0, 0, code, 0), entry_mode="TF_EVENT")
        self.assertEqual(cand.tolist(), [1])
        self.assertEqual(side.tolist(), [1])

    def test_higher_composite_is_latched_state_not_an_extra_trigger(self):
        state, hi = entry_fixture()
        state["5m_close_event"][:] = False
        state["5m_close_event"][2] = True
        code = DEFAULT_REGISTRY.register("entry", [2, 3], "AND")
        with mock.patch.multiple(B, N=6, CLOSE=np.full(6, 100.)), mock.patch.object(B.R, "build", return_value=(state, {}, hi, {})):
            B.build_field.cache_clear()
            *_, entries, stops = B.build_field("hist", ((code, 0, 0, 2, 0),), ("__NONE__",), entry_mode="TF_EVENT")
            B.build_field.cache_clear()
        self.assertEqual(entries[0][1].tolist(), [2])
        self.assertNotIn(code, hi["4h"], "batch composite masks must not leak into persistent native caches")

    def test_composite_identity_does_not_change_legacy_id(self):
        self.assertEqual(stable_base_id(0, (0, 0, 0, 0, 1), 457), 1370)
        one = DEFAULT_REGISTRY.register("entry", [1, 2], "AND")
        two = DEFAULT_REGISTRY.register("entry", [2, 3], "AND")
        first = stable_base_id(0, (0, 0, 0, 0, one), 0)
        self.assertLess(first, 0)
        self.assertNotEqual(first, stable_base_id(0, (0, 0, 0, 0, two), 0))
        stop = DEFAULT_REGISTRY.register("stop", ["S1", "ATR100_1m"], "OR")
        self.assertLess(composite_stop_index(stop), 0)
        self.assertEqual(set(stop_spec(stop)[1]), {"S1", "ATR100_1m"})

    def test_entry_batches_do_not_materialize_combination_pools(self):
        class Pool:
            def __iter__(self):
                yield 1
                raise AssertionError("unused entry options were eagerly consumed")
        source = 开仓批次(["hist"], [Pool(), [0], [0], [0], [1]], 1)
        self.assertEqual(next(source), ("hist", ((1, 0, 0, 0, 1),)))
        source.close()


def tp_fixture():
    close = np.array([100., 103., 104., 102., 105., 101., 100.])
    high = np.array([150., 104., 105., 104., 106., 104., 101.])
    low = np.array([50., 102., 103., 101., 103., 100., 99.])
    n = len(close)
    stops = (np.full(n, n - 1, np.int64), np.full(n, n - 1, np.int64),
             np.full(n, 90.), np.full(n, 110.), np.full(n, 90.), np.full(n, 110.))
    never = (np.full(n, n, np.int64), np.full(n, n, np.int64), close.copy(), close.copy(), 0, 0., 0.)
    return close, high, low, stops, never


def compose(plans, stops=None, cand=None, side=None):
    close, high, low, default_stops, _ = tp_fixture()
    n = len(close)
    cand = np.array([0], np.int64) if cand is None else cand
    side = np.array([1], np.int8) if side is None else side
    with mock.patch.multiple(B, CLOSE=close, HIGH=high, LOW=low, YEAR_INDEX=np.zeros(n, np.int16), YEAR_VALUES=np.array([2026])):
        return B.combined_tp_data(cand, side, np.arange(n, dtype=np.int32), stops or default_stops, plans)


class TakeProfitCombinationTests(unittest.TestCase):
    def test_members_not_triggered_do_not_turn_sample_end_into_takeprofit(self):
        close, _, _, _, never = tp_fixture()
        result = compose([never, never])
        self.assertEqual(result[0][0], len(close))

    def test_stop_and_takeprofit_same_bar_remains_stop(self):
        _, _, _, stops, never = tp_fixture()
        fixed = (np.full(7, 3, np.int64), never[1], np.full(7, 104.), never[3], 0, 0., 0.)
        stops = (np.full(7, 3, np.int64), *stops[1:])
        result = compose([never, fixed], stops)
        self.assertEqual(result[0][0], 7)

    def test_fixed_and_dynamic_choose_actual_earlier_exit(self):
        _, _, _, _, never = tp_fixture()
        trail = (*never[:4], 2, .02, .5)
        fixed = (np.full(7, 1, np.int64), never[1], np.full(7, 103.), never[3], 0, 0., 0.)
        self.assertEqual(compose([trail, fixed])[0][0], 1)
        self.assertAlmostEqual(compose([trail, fixed])[2][0], 103.)
        fixed = (np.full(7, 4, np.int64), never[1], np.full(7, 105.), never[3], 0, 0., 0.)
        result = compose([fixed, trail])
        self.assertEqual(result[0][0], 3)
        self.assertAlmostEqual(result[2][0], 102.5)

    def test_entry_bar_extremes_cannot_activate_dynamic_exit(self):
        _, _, _, _, never = tp_fixture()
        # The entry bar reached150, but subsequent highs never meet +20% activation.
        self.assertEqual(compose([(*never[:4], 2, .2, .5), never])[0][0], 7)

    def test_two_takeprofits_same_bar_choose_conservative_price(self):
        _, _, _, _, never = tp_fixture()
        first = (np.full(7, 2, np.int64), never[1], np.full(7, 103.), never[3], 0, 0., 0.)
        second = (first[0], first[1], np.full(7, 104.), first[3], 0, 0., 0.)
        result = compose([first, second])
        self.assertEqual(result[0][0], 2)
        self.assertAlmostEqual(result[2][0], 103.)

    def test_single_plan_composition_matches_original_account_results(self):
        close, high, low, stops, never = tp_fixture()
        n = len(close)
        cand = np.array([0, 1, 2, 4], np.int64); side = np.ones(4, np.int8)
        run_id = np.arange(n, dtype=np.int32)
        merged = compose([(*never[:4], 3, .02, .01)], cand=cand, side=side)
        args = (cand, side, run_id, *stops[:4])
        tail = (close, high, low, np.array([1., 20.]), np.zeros(n, np.int16), 1)
        options = dict(cooldown=1, initial=100., funding_cum=np.zeros(n, np.int64), cross=False,
                       protect_ratio=.2, min_entry_gap=2, entry_fee_rate=.0004, exit_fee_rate=.0004)
        original = A.simulate_all_sizes(*args, *(*never[:4], 3, .02, .01), *tail, **options)
        actual = A.simulate_all_sizes(*args, *merged, *tail, **options)
        for expected, observed in zip(original, actual):
            np.testing.assert_allclose(observed, expected, rtol=1e-12, atol=1e-12)

    def test_scan_counts_include_combinations_without_expanding_them(self):
        cfg = 全选配置()
        cfg.update({"开仓指标": ["hist"], "开仓条件": {"4h": [0], "1h": [0], "15m": [0], "5m": [0], "1m": [1, 2, 3]},
                    "止损代码": ["S1", "ATR100_1m"], "固定止损代码": ["OFF"], "止盈方案编号": [2, 3],
                    "止盈后等待分钟": [0], "仓位倍数": [1.]})
        spec = {"启用": True, "组合数量": [2], "保留单项": True}
        cfg["指标组合"] = {"开仓": {"1m": dict(spec, 逻辑="AND")},
                           "止损": dict(spec, 逻辑="OR"), "止盈": dict(spec, 逻辑="OR")}
        options = choices_for_config(cfg)
        rows = scan_row_counts(cfg, LazyTakeProfits(options["止盈"]))
        self.assertIsInstance(rows, LazyRowCounts)
        self.assertEqual(rows.total, 6 * 3 * 3)


if __name__ == "__main__":
    unittest.main()
