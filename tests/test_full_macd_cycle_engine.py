"""完整红绿轮次：固定起点、两段共一次实际入场，不改变旧换色口径。"""
import unittest
from unittest import mock

import numpy as np

from test_support import prepare_test_features

prepare_test_features()

import account_replay as A
import backtest_engine as B


MODES = (("MACD_FULL_RED", 1), ("MACD_FULL_GREEN", -1))


class FullMacdCycleTests(unittest.TestCase):
    def build(self, hist, mode="MACD_FULL_RED", signal=None, field="hist", gate=None,
              position=None, cases=((0, 0, 0, 0, 1),)):
        hist = np.asarray(hist, dtype=np.float64)
        n = len(hist)
        values = np.arange(n, dtype=float) if signal is None else np.asarray(signal, dtype=float)
        state = {"d1": B.E.direction(values), "ma120": np.zeros(n),
                 "1m_highvol_long": values > 0, "1m_highvol_short": values < 0}
        hi = {tf: {0: (np.ones(n, bool), np.ones(n, bool))}
              for tf in ("4h", "1h", "15m", "5m")}
        B.build_field.cache_clear()
        self.addCleanup(B.build_field.cache_clear)
        with mock.patch.object(B, "N", n), mock.patch.object(B, "CLOSE", np.full(n, 100.)), \
                mock.patch.dict(B.E.D, {"m1_hist": hist, "m1_dif": values}), \
                mock.patch.object(B.R, "build", return_value=(state, {}, hi, None)), \
                mock.patch.object(B, "s3_baseline_gate", return_value=gate), \
                mock.patch.object(B, "entry_position_gate", return_value=position), \
                mock.patch.object(B, "extended_entry_data", return_value=(values > 0, values < 0)):
            return B.build_field(field, cases, ("__NONE__",), materialize_stops=False,
                                 entry_mode=mode, position_filter="OFF" if position is None else "EMA5_ATR100")

    def replay(self, hist, candidates, directions=None, start_side=1, partial=False,
               kernel=False, cooldown=0, gap=0, first_exit=None, mode=None):
        n = len(hist)
        close = np.full(n, 100., dtype=np.float64)
        cand = np.asarray(candidates, dtype=np.int64)
        sides = (np.ones(len(cand), np.int8) if directions is None
                 else np.asarray(directions, dtype=np.int8))
        runs = (B.macd_cycle_run_id(hist) if mode == "MACD_CYCLE"
                else B.macd_full_cycle_run_id(hist, start_side))
        self.assertTrue(np.all(runs[cand] > 0), "build_field excludes unanchored candidates")
        stop = np.minimum(np.arange(n) + 3, n - 1).astype(np.int64)
        first = np.minimum(np.arange(n) + (1 if partial else 2), n - 1).astype(np.int64)
        if first_exit is not None:
            stop[cand[0]] = first_exit
            first[cand[0]] = max(int(cand[0]), first_exit - (1 if partial else 0))
        head = (cand, sides, runs, stop, stop, close, close, first, first, close, close)
        tail = (close, close, close, np.array([1., 2.]), np.zeros(n, np.int16), 1)
        options = {"funding_cum": np.zeros(n, np.int64)}
        module = B if kernel else A
        options.update({"cooldown_minutes": cooldown} if kernel
                       else {"cooldown": cooldown, "min_entry_gap": gap})
        if partial:
            never = np.full(n + 1, n, np.int64)
            return module.simulate_partial_sizes(*head, .25, 1, never, never, 0., 0., *tail, **options)
        return module.simulate_all_sizes(*head, 0, 0., 0., *tail, **options)

    def test_red_and_green_have_distinct_fixed_starts(self):
        hist = [0., 1., 2., 0., -1., -2., 0., 1., 2., -1., 0., 1., -1.]
        np.testing.assert_array_equal(B.macd_full_cycle_run_id(hist, 1),
                                      [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2])
        np.testing.assert_array_equal(B.macd_full_cycle_run_id(hist, -1),
                                      [0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 3])
        self.assertEqual(B.macd_full_cycle_run_id(hist, 1).dtype, np.dtype(np.int32))

    def test_truncated_first_color_and_leading_zeros_never_start_a_round(self):
        for _, side in MODES:
            for prefix in ([side, side], [0., 0., side, 0., side], [-side, -side]):
                hist = list(prefix) + [-side, 0., side, side]
                result = B.macd_full_cycle_run_id(hist, side)
                np.testing.assert_array_equal(result, [0] * (len(hist) - 2) + [1, 1])
            np.testing.assert_array_equal(B.macd_full_cycle_run_id([0., side, side], side), [0, 0, 0])

    def test_empty_all_zero_nonfinite_and_invalid_start(self):
        for _, side in MODES:
            self.assertEqual(len(B.macd_full_cycle_run_id([], side)), 0)
            np.testing.assert_array_equal(B.macd_full_cycle_run_id([0., np.nan, np.inf], side), [0, 0, 0])
        with self.assertRaises(ValueError):
            B.macd_full_cycle_run_id([1., -1.], 0)

    def test_nonfinite_bars_do_not_enter_reset_or_break_the_previous_color(self):
        hist = [0., np.nan, -1., np.inf, 1., 0., np.nan, np.inf, -1., 0., 1.]
        built = self.build(hist)
        np.testing.assert_array_equal(built[2], [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 2])
        np.testing.assert_array_equal(built[3][0][1], [4, 5, 8, 9, 10])

    def test_only_hist_line_cross_sets_boundaries_even_for_dif_entry(self):
        hist = [1., .5, -1., -.5, 1., .5, -1., -.5, 1.]
        by_hist = self.build(hist, signal=hist)
        by_dif = self.build(hist, signal=np.arange(len(hist)), field="dif")
        np.testing.assert_array_equal(by_hist[2], by_dif[2])
        np.testing.assert_array_equal(by_dif[2], [0, 0, 0, 0, 1, 1, 1, 1, 2])
        self.assertFalse(np.array_equal(by_hist[3][0][2], by_dif[3][0][2]))

    def test_case_three_can_enter_either_direction_in_either_color(self):
        for mode, side in MODES:
            hist = np.array([-side, side, side, -side, -side, side], dtype=float)
            built = self.build(hist, mode=mode, signal=[0., -1., 1., 1., -1., -1.],
                               cases=((0, 0, 0, 0, 3),))
            np.testing.assert_array_equal(built[3][0][1], [1, 2, 3, 4, 5])
            np.testing.assert_array_equal(built[3][0][2], [-1, 1, 1, -1, -1])
            np.testing.assert_array_equal(built[2], [0, 1, 1, 1, 1, 2])

    def test_no_future_information_changes_past_rounds_or_candidates(self):
        hist = np.array([0., 1., 2., -1., 0., 1., np.nan, -1., 0., 1., -1., 2.])
        for mode, side in MODES:
            complete = self.build(hist, mode=mode)
            for cut in range(1, len(hist)):
                prefix = self.build(hist[:cut], mode=mode)
                np.testing.assert_array_equal(prefix[2], complete[2][:cut])
                np.testing.assert_array_equal(prefix[3][0][1], complete[3][0][1][complete[3][0][1] < cut])
                changed = hist.copy(); changed[cut:] *= -1
                np.testing.assert_array_equal(B.macd_full_cycle_run_id(changed, side)[:cut], complete[2][:cut])

    def test_both_colors_and_both_directions_share_one_actual_entry(self):
        for _, side in MODES:
            hist = np.array([-side] * 3 + [side] * 6 + [-side] * 6 + [side] * 6 + [-side] * 6)
            for partial in (False, True):
                for kernel in (False, True):
                    with self.subTest(side=side, partial=partial, kernel=kernel):
                        result = self.replay(hist, [3, 6, 9, 12, 15, 18, 21, 24],
                                             [1, -1, -1, 1, -1, 1, 1, -1], start_side=side,
                                             partial=partial, kernel=kernel)
                        self.assertEqual(result[0], 2)
                        self.assertEqual(result[2], 1)
                        self.assertEqual(result[3], 6 if partial else 4)
                        np.testing.assert_array_equal(result[13], [2, 2])

    def test_first_entry_can_wait_until_second_color_without_crossing_on_signal_bar(self):
        hist = [-1.] * 3 + [1.] * 6 + [-1.] * 6 + [1.] * 6 + [-1.] * 6
        for partial in (False, True):
            for kernel in (False, True):
                result = self.replay(hist, [10, 13, 16, 19, 22], partial=partial, kernel=kernel)
                self.assertEqual(result[0], 2)
                self.assertEqual(result[4], 6 if partial else 4)

    def test_position_and_s3_rejections_leave_the_round_unused(self):
        hist = [-1.] * 3 + [1.] * 6 + [-1.] * 6 + [1.] * 6
        n = len(hist)
        position = np.zeros(n, bool); position[[3, 9, 12, 15, 18]] = True
        s3 = np.ones(n, bool); s3[3] = False
        built = self.build(hist, gate=(s3, s3), position=(position, position))
        np.testing.assert_array_equal(built[3][0][1], [9, 12, 15, 18])
        for partial in (False, True):
            for kernel in (False, True):
                result = self.replay(hist, built[3][0][1], partial=partial, kernel=kernel)
                self.assertEqual(result[0], 2)

    def test_cooldown_rejection_does_not_consume_next_round(self):
        hist = [-1.] * 3 + [1.] * 6 + [-1.] * 6 + [1.] * 10
        for partial in (False, True):
            for kernel in (False, True):
                result = self.replay(hist, [11, 15, 20, 23], partial=partial, kernel=kernel, cooldown=5)
                self.assertEqual(result[0], 2)
                self.assertEqual(result[4], 6 if partial else 4)

    def test_holding_over_next_boundary_does_not_consume_next_round(self):
        hist = [-1.] * 3 + [1.] * 6 + [-1.] * 6 + [1.] * 12
        for partial in (False, True):
            for kernel in (False, True):
                result = self.replay(hist, [12, 15, 18, 22], partial=partial, kernel=kernel, first_exit=16)
                self.assertEqual(result[0], 2)
                self.assertEqual(result[4], 7 if partial else 6)

    def test_unformed_entry_does_not_consume_round_in_actual_account_replay(self):
        hist = [-1.] * 3 + [1.] * 4 + [-1.] * 6
        for partial in (False, True):
            result = self.replay(hist, [3, 7, 10], partial=partial, first_exit=3)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[4], 3 if partial else 2)
            np.testing.assert_array_equal(result[13], [1, 1])

    def test_post_exit_gap_rejections_do_not_consume_new_round(self):
        hist = [-1.] * 3 + [1.] * 6 + [-1.] * 6 + [1.] * 12
        for partial in (False, True):
            result = self.replay(hist, [12, 15, 22, 25], partial=partial, gap=7)
            self.assertEqual(result[0], 2)

    def test_fifth_keeps_own_events_in_pure_and_mixed_selections(self):
        fifth = (0, 0, 0, 0, 288)  # F5-025 is on the permanent whitelist.
        legacy = (0, 0, 0, 0, 1)
        for mode, _ in MODES:
            for cases in ((fifth,), (fifth, legacy)):
                built = self.build(np.zeros(6), mode=mode, signal=np.ones(6), cases=cases)
                np.testing.assert_array_equal(built[3][0][1], np.arange(6))
                np.testing.assert_array_equal(B.entry_run_id_for_cases(fifth, built[2]), np.arange(6))
                self.assertEqual(B.effective_entry_mode(fifth, mode), "F5_EVENT")
                if len(cases) == 1:
                    np.testing.assert_array_equal(built[2], np.arange(6))
                else:
                    self.assertEqual(len(built[3][1][1]), 0)

    def test_old_macd_cycle_still_resets_at_every_color_change(self):
        hist = [-1.] * 3 + [1.] * 6 + [-1.] * 6 + [1.] * 6
        built = self.build(hist, mode="MACD_CYCLE")
        np.testing.assert_array_equal(built[2], [1] * 3 + [2] * 6 + [3] * 6 + [4] * 6)
        for partial in (False, True):
            for kernel in (False, True):
                result = self.replay(hist, [3, 6, 9, 12, 15, 18], partial=partial,
                                     kernel=kernel, mode="MACD_CYCLE")
                self.assertEqual(result[0], 3)

    def test_hedge_adapter_and_actual_fills_share_the_complete_round_across_directions(self):
        from hedge_engine import run_case
        from hedge_signals import SignalAdapter

        for mode, side in MODES:
            hist = [-side] * 3 + [side] * 6 + [-side] * 6 + [side] * 6 + [-side] * 6
            n = len(hist)
            signal = np.ones(n); signal[9:15] = -1; signal[15:] = -1
            built = self.build(hist, mode=mode, signal=signal, cases=((0, 0, 0, 0, 3),))
            descriptor = {"id": "full_cycle_test", "field": "hist", "cases": (0, 0, 0, 0, 3),
                          "s3_gap": 0., "s3_timeframe": "1m", "entry_mode": mode}
            adapter = SignalAdapter(B.E.FEATURE_PATH)
            with mock.patch.object(B, "N", n), mock.patch.object(B, "build_field", return_value=built) as build:
                signals, runs = adapter.build(descriptor, warmup=0)
            self.assertEqual(build.call_args.kwargs["entry_mode"], mode)
            np.testing.assert_array_equal(runs, built[2])
            self.assertFalse(signals[:3].any())
            self.assertFalse(signals.flags.writeable)
            self.assertFalse(runs.flags.writeable)
            data = {key: np.full(n, 100.) for key in ("o", "h", "l", "c")}
            data.update(ts=np.arange(n, dtype=np.int64) * 60000 + 1735689600000, tp=np.full(n, .004))
            data["h"][:] = 101.; data["l"][:] = 99.
            for delayed_fill in (False, True):
                if delayed_fill:
                    data["l"][:8] = 100.  # Posted-but-unfilled orders do not consume the round.
                result = run_case(data, {"initial_equity": 2000., "mode": "single", "entry_gap_minutes": 1},
                                  12, 0, signals=signals, run_ids=runs, path=1, detail=True)
                entries = result["fills"][result["fills"][:, 2] == 0]
                self.assertEqual(result["stats"]["entries"], 2)
                np.testing.assert_array_equal(entries[:, 0], [0, 1])
                np.testing.assert_array_equal(entries[:, 1], data["ts"][[8 if delayed_fill else 4, 16]] + 60000)


if __name__ == "__main__":
    unittest.main()
