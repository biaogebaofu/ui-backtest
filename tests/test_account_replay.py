import unittest
from unittest import mock
import numpy as np
import account_replay as A
import backtest_engine as B


class AccountReplayTests(unittest.TestCase):
    def simulate(self, close, low, cand, sizes, exits=None, initial=100., minimum=0., years=None,
                 take_profit=None, cooldown=0):
        close = np.array(close, dtype=float); low = np.array(low, dtype=float)
        n = len(close)
        exits = np.array(exits if exits is not None else [n-1]*n, dtype=np.int64)
        never = np.full(n, n, dtype=np.int64)
        tx = never if take_profit is None else np.array(take_profit, dtype=np.int64)
        price = close[exits]
        years = np.array(years if years is not None else [0]*n, dtype=np.int16)
        return A.simulate_all_sizes(
            np.array(cand, dtype=np.int64), np.ones(len(cand), dtype=np.int8),
            np.arange(n, dtype=np.int32), exits, exits, price, price,
            tx, tx, price, price, 0, 0., 0.,
            close, close.copy(), low, np.array(sizes), years, int(years.max()) + 1,
            cooldown, 0., initial, minimum, minimum > 0.)

    def test_unrealized_close_peak_is_in_drawdown(self):
        r = self.simulate([100., 200., 100.], [100., 200., 100.], [0], [1.])
        self.assertAlmostEqual(r[10][0], 1.)
        self.assertAlmostEqual(r[11][0], 0.5)

    def test_each_leverage_reenters_after_its_own_protection_exit(self):
        r = self.simulate([100.]*6, [100., 90., 100., 100., 100., 100.],
                          [0, 2, 4], [1., 10.], [5, 5, 3, 5, 5, 5])
        m = B.metrics_dict(r)
        small = B.metrics_for_size(m, 0); large = B.metrics_for_size(m, 1)
        self.assertEqual(small["交易次数"], 1)
        self.assertEqual(large["交易次数"], 3)
        self.assertEqual(large["平均持仓分钟"], 1.)
        self.assertEqual(large["爆仓保护次数"][0], 1)
        self.assertAlmostEqual(large["期末资金倍数"][0], 0.15)
        self.assertEqual(large["胜率"], 0.)

    def test_no_phantom_trades_after_minimum_order_stop(self):
        r = self.simulate([100., 15., 100., 100.], [100., 15., 100., 100.],
                          [0, 2], [1.], [1, 3, 3, 3], initial=1., minimum=.01)
        self.assertEqual(r[0], 1)
        self.assertEqual(r[13][0], 1)
        self.assertEqual(r[14][0], 1)

    def test_full_liquidation_is_zero_and_stops(self):
        r = self.simulate([100.]*4, [100., 99., 100., 100.], [0, 2], [100.])
        self.assertEqual(r[10][0], 0.)
        self.assertEqual(r[0], 1)
        self.assertEqual(r[15][0], 1)

    def test_empty_entry_candidates_keep_zero_trade_accounts(self):
        r = self.simulate([100., 100.], [100., 100.], [], [1., 5.])
        for index in range(2):
            m = B.metrics_for_size(B.metrics_dict(r), index)
            self.assertEqual(m["交易次数"], 0)
            self.assertEqual(m["期末资金倍数"][0], 1.)

    def test_yearly_return_uses_actual_exit_year(self):
        r = self.simulate([100., 110.], [100., 110.], [0], [1.], years=[0, 1])
        self.assertEqual(r[9][0], 0.)
        self.assertAlmostEqual(r[9][1], .1)

    def test_capacity_does_not_add_take_profit_wait_after_a_stop(self):
        r = self.simulate([100.]*8, [100.]*8, [0], [1.], exits=[3]*8, cooldown=10)
        with mock.patch.object(B, "DAYS", 8 / 1440):
            m = B.metrics_for_size(B.metrics_dict(r), 0)
        self.assertEqual(m["实际止盈等待总分钟"], 0)
        self.assertAlmostEqual(m["实际容量占用率"], 3/8)

    def test_capacity_clips_last_take_profit_wait_to_data_end(self):
        r = self.simulate([100.]*8, [100.]*8, [0], [1.],
                          take_profit=[3]*8, cooldown=10)
        with mock.patch.object(B, "DAYS", 8 / 1440):
            m = B.metrics_for_size(B.metrics_dict(r), 0)
        self.assertEqual(m["实际止盈等待总分钟"], 4)
        self.assertAlmostEqual(m["实际容量占用率"], 7/8)

    def test_partial_protection_before_first_fill_has_only_two_orders(self):
        close = np.full(6, 100.); low = close.copy(); low[1] = 90.
        sx = np.full(6, 5, dtype=np.int64); fx = np.full(6, 2, dtype=np.int64)
        never = np.full(7, 6, dtype=np.int64)
        r = A.simulate_partial_sizes(
            np.array([0], dtype=np.int64), np.array([1], dtype=np.int8),
            np.arange(6, dtype=np.int32), sx, sx, close, close,
            fx, fx, np.full(6, 101.), np.full(6, 99.), .5, 2, never, never, .01, .05,
            close, close.copy(), low, np.array([10.]), np.zeros(6, dtype=np.int16), 1)
        self.assertEqual(r[0], 1)
        self.assertEqual(r[3], 2)
        self.assertEqual(r[4], 1)


if __name__ == "__main__":
    unittest.main()
