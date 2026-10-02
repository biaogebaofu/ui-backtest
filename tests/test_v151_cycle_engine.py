"""Closed 1m histogram zero-side cycles reuse actual-entry consumption paths."""
import unittest
from unittest import mock

import numpy as np

from test_support import prepare_test_features

prepare_test_features()

import account_replay as A
import backtest_engine as B


class MacdCycleTests(unittest.TestCase):
    def build(self, hist, signal=None, field="hist", mode="MACD_CYCLE", position=None,
              s3=None, cases=(0, 0, 0, 0, 1)):
        hist = np.asarray(hist, dtype=np.float64)
        n = len(hist)
        values = hist if signal is None else np.asarray(signal, dtype=np.float64)
        signals = {"d1": B.E.direction(values), "ma120": np.zeros(n)}
        high_tf = {tf: {0: (np.ones(n, bool), np.ones(n, bool)),
                        3: (np.ones(n, bool), np.zeros(n, bool))}
                   for tf in ("4h", "1h", "15m", "5m")}
        exits = {"unchanged": object()}
        B.build_field.cache_clear()
        self.addCleanup(B.build_field.cache_clear)
        with mock.patch.object(B, "N", n), mock.patch.object(B, "CLOSE", np.full(n, 100.)), \
                mock.patch.dict(B.E.D, {"m1_hist": hist, "m1_dif": values}), \
                mock.patch.object(B.R, "build", return_value=(signals, exits, high_tf, None)), \
                mock.patch.object(B, "s3_baseline_gate", return_value=s3), \
                mock.patch.object(B, "entry_position_gate", return_value=position):
            result = B.build_field(field, (cases,), ("S1",), materialize_stops=False,
                                   entry_mode=mode, position_filter="OFF" if position is None else "EMA5_ATR100")
            expected = B.build_candidates(signals, high_tf, cases, s3, entry_mode=mode)
        self.assertIs(result[0], signals)
        self.assertIs(result[1], exits)
        return result, expected

    def replay(self, hist, candidates, directions=None, partial=False, kernel=False,
               cooldown=0, gap=0, fee=0., first_exit=None):
        n = len(hist)
        close = np.full(n, 100., dtype=np.float64)
        cand = np.asarray(candidates, dtype=np.int64)
        sides = np.ones(len(cand), dtype=np.int8) if directions is None else np.asarray(directions, dtype=np.int8)
        stop = np.minimum(np.arange(n) + (2 if partial else 3), n - 1).astype(np.int64)
        first = np.minimum(np.arange(n) + (1 if partial else 2), n - 1).astype(np.int64)
        if first_exit is not None:
            stop[cand[0]] = first_exit
            first[cand[0]] = max(int(cand[0]), first_exit - (1 if partial else 0))
        common = (cand, sides, B.macd_cycle_run_id(np.asarray(hist)), stop, stop, close, close,
                  first, first, close, close)
        tail = (close, close, close, np.array([1., 2.]), np.zeros(n, np.int16), 1)
        options = {"funding_cum": np.zeros(n, np.int64)}
        if kernel:
            options["cooldown_minutes"] = cooldown
            module = B
        else:
            options.update(cooldown=cooldown, min_entry_gap=gap, entry_fee_rate=fee,
                           exit_fee_rate=2 * fee)
            module = A
        if partial:
            never = np.full(n + 1, n, np.int64)
            return module.simulate_partial_sizes(*common, .25, 1, never, never, 0., 0., *tail, **options)
        return module.simulate_all_sizes(*common, 0, 0., 0., *tail, **options)

    def test_zero_side_ids_ignore_slope_flips_and_extend_exact_zeros(self):
        hist = [0., 0., 1., 3., 2., 0., -1., -2., 0., 1., .5, 1.]
        runs = B.macd_cycle_run_id(np.asarray(hist))
        np.testing.assert_array_equal(runs, [0, 0, 1, 1, 1, 1, 2, 2, 2, 3, 3, 3])
        self.assertEqual(runs.dtype, np.dtype(np.int32))
        self.assertEqual(len(B.macd_cycle_run_id(np.array([]))), 0)
        np.testing.assert_array_equal(B.macd_cycle_run_id(np.zeros(4)), [0, 0, 0, 0])

    def test_cycle_boundary_always_uses_hist_even_when_entry_field_is_dif(self):
        hist = [0., 1., 2., 1., .5, -.5, -1., -.5, .5, 1.]
        dif = [-3., -2., -1., .5, 1., 2., 1., .5, -.5, -1.]
        by_hist, _ = self.build(hist)
        by_dif, _ = self.build(hist, dif, "dif")
        np.testing.assert_array_equal(by_hist[2], by_dif[2])
        np.testing.assert_array_equal(by_dif[2], [0, 1, 1, 1, 1, 2, 2, 2, 3, 3])
        # Entry direction is still the selected indicator's original rule.
        self.assertFalse(np.array_equal(by_hist[3][0][2], by_dif[3][0][2]))

    def test_leading_zero_and_nonfinite_bars_cannot_enter_or_create_cycles(self):
        hist = [0., 0., 0., 1., 0., np.nan, np.inf, -1., 0.]
        result, _ = self.build(hist, np.arange(len(hist)))
        np.testing.assert_array_equal(result[2], [0, 0, 0, 1, 1, 1, 1, 2, 2])
        np.testing.assert_array_equal(result[3][0][1], [3, 4, 7, 8])

    def test_high_timeframe_state_can_trigger_without_one_minute_condition(self):
        result, _ = self.build([0., 1., .5, .8, -.2, -.5], cases=(0, 0, 0, 3, 0))
        np.testing.assert_array_equal(result[3][0][1], [1, 2, 3, 4, 5])
        np.testing.assert_array_equal(result[3][0][2], [1, 1, 1, 1, 1])
        # Negative hist does not force a short: it only starts a new allowance.
        np.testing.assert_array_equal(result[2], [0, 1, 1, 1, 2, 2])

    def test_live_and_tf_event_keep_their_candidates_run_ids_and_stops(self):
        hist = [0., 1., 2., 1., .5, -.5, -1., -.5, .5, 1.]
        for mode in ("LIVE_01", "TF_EVENT"):
            result, expected = self.build(hist, mode=mode)
            for actual, before in zip(result[3][0][1:], expected):
                np.testing.assert_array_equal(actual, before)
            expected_runs = (np.arange(len(hist), dtype=np.int32) if mode == "TF_EVENT"
                             else B.direction_run_id(B.E.direction(np.asarray(hist))))
            np.testing.assert_array_equal(result[2], expected_runs)
            cycle, _ = self.build(hist)
            self.assertEqual(result[4], cycle[4])

    def test_three_cycles_share_one_allowance_across_both_trade_directions(self):
        hist = [1.] * 6 + [-1.] * 6 + [1.] * 6
        for partial in (False, True):
            for kernel in (False, True):
                with self.subTest(partial=partial, kernel=kernel):
                    result = self.replay(hist, [0, 3, 6, 9, 12, 15], [1, -1, -1, 1, 1, -1],
                                         partial=partial, kernel=kernel)
                    self.assertEqual(result[0], 3)
                    self.assertEqual(result[2], 2)
                    self.assertEqual(result[3], 9 if partial else 6)
                    np.testing.assert_array_equal(result[13], [3, 3])

    def test_position_and_s3_filters_do_not_consume_the_unused_cycle(self):
        n = 18
        hist = [1.] * 6 + [-1.] * 6 + [1.] * 6
        position = np.zeros(n, bool); position[[3, 6, 9, 12, 15]] = True
        s3 = np.ones(n, bool); s3[6] = False
        result, _ = self.build(hist, np.arange(n), position=(position, position), s3=(s3, s3))
        cand = result[3][0][1]
        np.testing.assert_array_equal(cand, [3, 9, 12, 15])
        for partial in (False, True):
            replayed = self.replay(hist, cand, partial=partial, cooldown=3)
            # Open3/exit5, open9/exit11, then open15: rejected candidates did not consume a round.
            self.assertEqual(replayed[0], 3)
            self.assertEqual(replayed[4], 6)
        np.testing.assert_array_equal(np.flatnonzero(position), [3, 6, 9, 12, 15])
        self.assertFalse(s3[6])

    def test_holding_across_boundary_leaves_new_cycle_available_after_exit(self):
        hist = [1.] * 4 + [-1.] * 5 + [1.] * 9
        for partial in (False, True):
            for kernel in (False, True):
                result = self.replay(hist, [0, 3, 6, 10, 14], partial=partial,
                                     kernel=kernel, first_exit=4)
                self.assertEqual(result[0], 3)
                self.assertEqual(result[4], 8)

    def test_unformed_entry_does_not_consume_cycle_in_actual_account_replay(self):
        for partial in (False, True):
            result = self.replay([1.] * 8, [0, 2], partial=partial, first_exit=0)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[4], 2)
            np.testing.assert_array_equal(result[13], [1, 1])

    def test_fees_and_post_exit_gap_still_apply_to_ordinary_and_partial_accounts(self):
        hist = [1.] * 6 + [-1.] * 6 + [1.] * 6
        for partial in (False, True):
            result = self.replay(hist, [0, 3, 6, 9, 12, 15], partial=partial, gap=7, fee=.001)
            self.assertEqual(result[0], 2)
            np.testing.assert_allclose(result[10], [.997 ** 2, .994 ** 2])
            self.assertAlmostEqual(result[5], -.006)

    def test_future_hist_values_cannot_change_past_ids_or_candidates(self):
        hist = np.array([0., 0., 1., 2., 1., 0., -.5, -1., 0., 1., .5, 2.])
        for field, signal in (("hist", hist), ("dif", np.arange(len(hist), dtype=float))):
            complete, _ = self.build(hist, signal, field)
            for cut in (3, 6, 9):
                prefix, _ = self.build(hist[:cut], signal[:cut], field)
                np.testing.assert_array_equal(prefix[2], complete[2][:cut])
                past = complete[3][0][1] < cut
                np.testing.assert_array_equal(prefix[3][0][1], complete[3][0][1][past])
                np.testing.assert_array_equal(prefix[3][0][2], complete[3][0][2][past])


if __name__ == "__main__":
    unittest.main()
