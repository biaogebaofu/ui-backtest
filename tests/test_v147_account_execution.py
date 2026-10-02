"""Deterministic account cash-flow and post-exit entry-gap regression tests."""
import unittest

import numpy as np

from test_support import prepare_test_features

prepare_test_features()

import account_replay as A


class AccountExecutionTests(unittest.TestCase):
    def replay(self, close, cand=(0,), side=1, size=1.0, low=None, high=None,
               exits=None, take_profit=None, partial=False, first=2, fraction=.25,
               prices=None, **options):
        close = np.array(close, dtype=np.float64)
        n = len(close)
        stop = np.array(exits if exits is not None else [n - 1] * n, dtype=np.int64)
        stop_price = close[stop] if prices is None else np.asarray(prices, dtype=np.float64)
        tx = np.array(take_profit if take_profit is not None else [n] * n, dtype=np.int64)
        tp_price = close[np.minimum(tx, n - 1)]
        if partial:
            tx = np.full(n, first, dtype=np.int64)
            tp_price = np.full(n, close[first])
        common = (
            np.array(cand, dtype=np.int64), np.full(len(cand), side, dtype=np.int8),
            np.arange(n, dtype=np.int32), stop, stop, stop_price, stop_price,
            tx, tx, tp_price, tp_price,
        )
        tail = (
            close, np.asarray(high if high is not None else close),
            np.asarray(low if low is not None else close), np.array([size]),
            np.zeros(n, dtype=np.int16), 1,
        )
        if partial:
            never = np.full(n + 1, n, dtype=np.int64)
            return A.simulate_partial_sizes(
                *common, fraction, 1, never, never, 0., 0., *tail, **options)
        return A.simulate_all_sizes(*common, 0, 0., 0., *tail, **options)

    def test_zero_options_preserve_default_results(self):
        for side in (1, -1):
            default = self.replay([100., 103., 101., 102.], side=side)
            explicit = self.replay([100., 103., 101., 102.], side=side,
                                   min_entry_gap=0, entry_fee_rate=0., exit_fee_rate=0.)
            self.assertEqual(len(default), 19)  # Appended per-account first capacity entry index.
            for before, after in zip(default, explicit):
                np.testing.assert_array_equal(before, after)

    def test_legacy_zero_cost_results_are_unchanged(self):
        result = self.replay([100., 110.], size=2.)
        self.assertEqual(result[0], 1)
        self.assertEqual(result[1], 1)
        self.assertEqual(result[3], 2)
        self.assertAlmostEqual(result[5], .1)
        self.assertAlmostEqual(result[10][0], 1.2)
        self.assertAlmostEqual(result[11][0], 0.)

    def test_five_minute_gap_applies_to_stop_and_take_profit(self):
        n = 9
        exits = list(range(1, n)) + [n - 1]
        for tp in (None, exits):
            actual_exits = exits if tp is None else [n - 1] * n
            for candidate, expected in ((5, 1), (6, 2)):
                result = self.replay([100.] * n, cand=(0, candidate),
                                     exits=actual_exits, take_profit=tp, min_entry_gap=5)
                self.assertEqual(result[0], expected)

    def test_gap_and_tp_cooldown_take_maximum_not_sum(self):
        n = 11
        tp = list(range(1, n)) + [n - 1]
        allowed = self.replay([100.] * n, cand=(0, 6), take_profit=tp,
                              cooldown=3, min_entry_gap=5)
        blocked = self.replay([100.] * n, cand=(0, 6), take_profit=tp,
                              cooldown=5, min_entry_gap=5)
        legacy_boundary = self.replay([100.] * n, cand=(0, 7), take_profit=tp,
                                      cooldown=5, min_entry_gap=5)
        self.assertEqual(allowed[0], 2)
        self.assertEqual(blocked[0], 1)
        self.assertEqual(legacy_boundary[0], 2)

    def test_asymmetric_fees_use_entry_and_exit_notional_on_both_sides(self):
        for side, exit_price, exit_ratio in ((1, 110., 1.1), (-1, 90., .9)):
            result = self.replay([100., exit_price], side=side, size=2.,
                                 entry_fee_rate=.001, exit_fee_rate=.002)
            net = .1 - .001 - exit_ratio * .002
            self.assertAlmostEqual(result[5], net)
            self.assertAlmostEqual(result[10][0], 1. + 2. * net)

    def test_fees_and_slippage_are_independent(self):
        result = self.replay([100., 110.], size=2., slippage=.003,
                             entry_fee_rate=.001, exit_fee_rate=.002)
        self.assertAlmostEqual(result[5], .1 - .003 - .001 - 1.1 * .002)

    def test_fee_uses_selected_close_confirmed_exit_price(self):
        result = self.replay([100., 110.], prices=[120., 120.], close_confirmed=True,
                             entry_fee_rate=.001, exit_fee_rate=.002)
        self.assertAlmostEqual(result[5], .1 - .001 - 1.1 * .002)

    def test_partial_fills_charge_only_the_quantity_actually_closed(self):
        for side, close, ratio in ((1, [100., 105., 110., 115., 120.], 1.175),
                                  (-1, [100., 95., 90., 85., 80.], .825)):
            result = self.replay(close, side=side, size=2., partial=True,
                                 entry_fee_rate=.001, exit_fee_rate=.002)
            self.assertEqual(result[3], 3)
            self.assertAlmostEqual(result[5], .175 - .001 - ratio * .002)
            self.assertAlmostEqual(result[10][0], 1. + 2. * result[5])

    def test_gap_starts_at_final_partial_exit(self):
        n = 12
        exits = [4] * n
        exits[8] = 10
        result = self.replay([100.] * n, cand=(0, 8), exits=exits,
                             partial=True, first=2, min_entry_gap=5)
        self.assertEqual(result[0], 1)

    def test_fees_affect_drawdown_and_minimum_order_stop(self):
        result = self.replay([100., 100., 100., 100.], cand=(0, 2),
                             exits=[1, 3, 3, 3], initial=1., min_qty=.01,
                             enforce_minimum=True, entry_fee_rate=.001, exit_fee_rate=.002)
        self.assertEqual(result[0], 1)
        self.assertEqual(result[14][0], 1)
        self.assertAlmostEqual(result[10][0], .997)
        self.assertAlmostEqual(result[11][0], .003)

    def test_opening_fee_drawdown_is_recorded_even_if_price_immediately_rises(self):
        result = self.replay([100., 110.], size=2., entry_fee_rate=.001)
        self.assertAlmostEqual(result[11][0], .002)

    def test_protection_price_consumes_paid_fee_budget_then_charges_exit(self):
        result = self.replay([100.] * 4, size=10., low=[100., 91.55, 100., 100.],
                             entry_fee_rate=.001, exit_fee_rate=.002)
        # A 0.1% entry fee moves the 8.5% price stop to 8.4%; Q*91.6 is exited.
        self.assertEqual(result[12][0], 1)
        self.assertEqual(result[4], 1)
        self.assertAlmostEqual(result[5], -.085 - .916 * .002)
        self.assertAlmostEqual(result[10][0], .15 - 10. * .916 * .002)

    def test_protection_before_partial_does_not_charge_unfilled_first_exit(self):
        result = self.replay([100., 100., 110., 110., 110.], size=10.,
                             low=[100., 90., 110., 110., 110.], partial=True,
                             entry_fee_rate=.001, exit_fee_rate=.002)
        self.assertEqual(result[3], 2)
        self.assertAlmostEqual(result[5], -.085 - .916 * .002)

    def test_protection_after_partial_uses_remaining_quantity_and_paid_fees(self):
        result = self.replay([100., 101., 110., 100., 100.], size=10.,
                             low=[100., 101., 110., 80., 100.], partial=True,
                             entry_fee_rate=.001, exit_fee_rate=.002)
        self.assertEqual(result[3], 3)
        self.assertEqual(result[4], 3)
        # 25% was closed at110: paid fee=.001+.25*1.1*.002=.00155.
        price_return = -.085 + .00155
        net = price_return - .001 - (1. + price_return) * .002
        self.assertAlmostEqual(result[5], net)
        self.assertAlmostEqual(result[10][0], 1. + 10. * net)

    def test_protection_exit_also_starts_five_minute_gap(self):
        n = 9
        low = [100., 90.] + [100.] * (n - 2)
        for candidate, expected in ((5, 1), (6, 2)):
            result = self.replay([100.] * n, cand=(0, candidate), size=10.,
                                 low=low, min_entry_gap=5, entry_fee_rate=.001)
            self.assertEqual(result[0], expected)

    def test_liquidation_with_fees_stays_zero_and_does_not_reenter(self):
        result = self.replay([100.] * 8, cand=(0, 6), size=100.,
                             low=[100., 99.] + [100.] * 6, min_entry_gap=5,
                             entry_fee_rate=.001, exit_fee_rate=.002)
        self.assertEqual(result[15][0], 1)
        self.assertEqual(result[10][0], 0.)
        self.assertEqual(result[14][0], 1)
        self.assertEqual(result[0], 1)


if __name__ == '__main__':
    unittest.main()
