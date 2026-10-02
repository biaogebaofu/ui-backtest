"""DIF/DEA relative-order changes are the existing histogram zero-side cycles."""
import unittest

import numpy as np

import feature_builder
import features
from test_support import prepare_test_features

prepare_test_features()

import backtest_engine as B


def line_order_runs(dif, dea):
    """Independent reference using line comparisons, without forming histogram values."""
    previous_side = current_run = 0
    runs = []
    for fast, signal in zip(dif, dea):
        side = 1 if fast > signal else -1 if fast < signal else 0
        if side and side != previous_side:
            current_run += 1
            previous_side = side
        runs.append(current_run)
    return np.asarray(runs, dtype=np.int32)


class MacdCrossEquivalenceTests(unittest.TestCase):
    def test_both_real_feature_builders_use_twice_dif_minus_dea(self):
        x = np.arange(1000, dtype=float)
        close = 100. + .01 * x + 3. * np.sin(x / 13.) + np.cos(x / 5.)
        old = features.macd(close)
        current = feature_builder.macd(close)
        for actual, expected in zip(current, old):
            np.testing.assert_array_equal(actual, expected)
        dif, dea, hist = current
        np.testing.assert_array_equal(hist, 2. * (dif - dea))
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), line_order_runs(dif, dea))

    def test_active_one_minute_alias_has_the_identical_definition(self):
        data = B.E.D
        np.testing.assert_array_equal(data["m1_hist"], data["1m_hist"])
        np.testing.assert_array_equal(data["m1_hist"], 2. * (data["m1_dif"] - data["m1_dea"]))
        np.testing.assert_array_equal(B.macd_cycle_run_id(data["m1_hist"]),
                                      line_order_runs(data["m1_dif"], data["m1_dea"]))

    def test_crosses_above_zero_do_not_require_dif_or_dea_to_cross_zero(self):
        dif = np.array([1., 2., 2., .5, 1.8])
        dea = feature_builder.ema(dif, 9)
        self.assertTrue(np.all(dif > 0) and np.all(dea > 0))
        hist = 2. * (dif - dea)
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), [0, 1, 1, 2, 3])
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), line_order_runs(dif, dea))

    def test_crosses_below_zero_are_the_same_histogram_boundaries(self):
        dif = -np.array([1., 2., 2., .5, 1.8])
        dea = feature_builder.ema(dif, 9)
        self.assertTrue(np.all(dif < 0) and np.all(dea < 0))
        hist = 2. * (dif - dea)
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), [0, 1, 1, 2, 3])
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), line_order_runs(dif, dea))

    def test_dif_own_zero_cross_does_not_create_a_histogram_cycle(self):
        dif = np.array([-.4, -.1, .2, .5])
        dea = np.array([-.8, -.66, -.488, -.2904])
        self.assertTrue(dif[1] < 0 < dif[2])
        self.assertTrue(np.all(dif > dea))
        np.testing.assert_array_equal(B.macd_cycle_run_id(2. * (dif - dea)), [1, 1, 1, 1])

    def test_exact_equality_extends_previous_side_until_a_nonzero_reversal(self):
        dea = np.ones(12)
        dif = np.array([1., 1., 2., 1., 1., 1.5, 1., 1., .5, 1., 1., 1.5])
        hist = 2. * (dif - dea)
        expected = [0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 3]
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), expected)
        np.testing.assert_array_equal(line_order_runs(dif, dea), expected)

    def test_bar_growth_or_shrinkage_without_side_change_does_not_reset_cycle(self):
        # Some charts distinguish rising/falling histogram bars by fill or shade.
        # That display distinction is not a DIF/DEA relative-order change.
        hist = np.array([2., 4., 3., 1., 2., -1., -3., -2., -4.])
        np.testing.assert_array_equal(B.macd_cycle_run_id(hist), [1, 1, 1, 1, 1, 2, 2, 2, 2])
        slope = B.E.direction(hist)
        self.assertGreater(np.count_nonzero(slope[1:] != slope[:-1]), 1)

    def test_future_prices_do_not_change_past_macd_cross_boundaries(self):
        close = 100. + np.sin(np.arange(600) / 9.)
        for cut in (40, 137, 300):
            dif, dea, hist = feature_builder.macd(close[:cut])
            future_changed = close.copy()
            future_changed[cut:] = 1000. + np.arange(len(close) - cut)
            full_dif, full_dea, full_hist = feature_builder.macd(future_changed)
            for prefix, full in ((dif, full_dif), (dea, full_dea), (hist, full_hist)):
                np.testing.assert_array_equal(prefix, full[:cut])
            np.testing.assert_array_equal(B.macd_cycle_run_id(hist),
                                          B.macd_cycle_run_id(full_hist)[:cut])


if __name__ == "__main__":
    unittest.main()
