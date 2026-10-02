"""Compare fused stop operations to the first-round NumPy implementations."""
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

import numpy as np
from test_support import prepare_test_features

prepare_test_features()
import backtest_engine as B


def legacy_merge(stop_data, extra, set_risk=False):
    if extra is None:
        return stop_data
    x_l, x_s, p_l, p_s, risk_l, risk_s = stop_data
    ex_l, ex_s, ep_l, ep_s = extra
    x_l = np.asarray(x_l); x_s = np.asarray(x_s)
    p_l = np.asarray(p_l).copy(); p_s = np.asarray(p_s).copy()
    use_l = ex_l < x_l
    use_s = ex_s < x_s
    if set_risk:
        use_l = use_l | ((ex_l == x_l) & (ep_l < p_l))
        use_s = use_s | ((ex_s == x_s) & (ep_s > p_s))
    p_l[use_l] = ep_l[use_l]
    p_s[use_s] = ep_s[use_s]
    return (np.minimum(np.where(ex_l < x_l, ex_l, x_l), B.N - 1),
            np.minimum(np.where(ex_s < x_s, ex_s, x_s), B.N - 1),
            p_l, p_s, ep_l.copy() if set_risk else risk_l,
            ep_s.copy() if set_risk else risk_s)


def legacy_make(EX, components):
    x_long = np.full(B.N, B.N, dtype=np.int64)
    x_short = np.full(B.N, B.N, dtype=np.int64)
    p_long = np.full(B.N, B.CLOSE[-1], dtype=np.float64)
    p_short = np.full(B.N, B.CLOSE[-1], dtype=np.float64)
    risk_long = np.full(B.N, np.nan, dtype=np.float64)
    risk_short = np.full(B.N, np.nan, dtype=np.float64)
    for component in components:
        if component.startswith('ATR'):
            data = B.extended_stop_data(component)
            risk_long, risk_short = data[2], data[3]
        if component.startswith('S3_'):
            level_long, level_short = EX[f'LVL_{component[3:]}']
            valid_long = (level_long > 0) & (level_long < B.CLOSE)
            valid_short = np.isfinite(level_short) & (level_short > B.CLOSE)
            risk_long = np.where(valid_long, level_long, risk_long)
            risk_short = np.where(valid_short, level_short, risk_short)
        for long_side, x, price in ((True, x_long, p_long), (False, x_short, p_short)):
            idx, px, intrabar = B.component_exit(EX, component, long_side)
            better = idx <= x if intrabar else idx < x
            better &= idx < B.N
            x[better] = idx[better]
            price[better] = px[better]
    return np.minimum(x_long, B.N - 1), np.minimum(x_short, B.N - 1), p_long, p_short, risk_long, risk_short


class StopMergeEquivalenceTests(unittest.TestCase):
    def assert_arrays_identical(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for got, wanted in zip(actual, expected):
            self.assertEqual(got.dtype, wanted.dtype)
            self.assertEqual(got.shape, wanted.shape)
            # Include signed zero and the original NaN bits, not just tolerance.
            self.assertEqual(got.tobytes(), wanted.tobytes())

    def test_extra_and_fixed_stop_boundaries_nonfinite_prices_and_input_ownership(self):
        rng = np.random.default_rng(20260911)
        for n in (1, 2, 19, 257):
            idx = rng.integers(0, n + 2, n, dtype=np.int64)
            idx2 = rng.integers(0, n + 2, n, dtype=np.int64)
            price = rng.normal(100., 10., n)
            price2 = rng.normal(100., 10., n)
            if n >= 19:
                price[:6] = [np.nan, np.inf, -np.inf, -0., 0., 100.]
                price2[:6] = [100., np.nan, np.inf, 0., -0., 100.]
                idx[:9] = n - 1
                idx2[:9] = [n - 1, n - 1, n - 1, n, n + 1, 0, 0, n - 1, n - 1]
            stop = (idx, idx[::-1], price, price[::-1], price2, price2[::-1])
            extra = (idx2, idx2[::-1], price2, price2[::-1])
            snapshots = [array.tobytes() for array in (*stop, *extra)]
            for array in (*stop, *extra):
                array.flags.writeable = False
            for sentinel in (n, n + 27):
                with mock.patch.object(B, 'N', sentinel):
                    for set_risk in (False, True):
                        got = B.merge_extra_stop(stop, extra, set_risk)
                        self.assert_arrays_identical(got, legacy_merge(stop, extra, set_risk))
                        if set_risk:
                            self.assertFalse(np.shares_memory(got[4], extra[2]))
                        else:
                            self.assertIs(got[4], stop[4])
                            self.assertIs(got[5], stop[5])
                    self.assert_arrays_identical(B.merge_fixed_stop(stop, extra), legacy_merge(stop, extra, True))
                    self.assertIs(B.merge_extra_stop(stop, None), stop)
                    self.assertIs(B.merge_fixed_stop(stop, None), stop)
            self.assertEqual(snapshots, [array.tobytes() for array in (*stop, *extra)])

    def test_fixed_ties_choose_adverse_price_while_time_stop_keeps_existing_price(self):
        x = np.array([1, 1, 3], dtype=np.int64)
        stop = (x, x, np.array([100., np.nan, 99.]), np.array([100., np.nan, 101.]),
                np.zeros(3), np.zeros(3))
        extra = (x, x, np.array([90., 80., 98.]), np.array([110., 120., 102.]))
        with mock.patch.object(B, 'N', 3):
            ordinary = B.merge_extra_stop(stop, extra)
            fixed = B.merge_fixed_stop(stop, extra)
        self.assert_arrays_identical(ordinary[2:4], stop[2:4])
        np.testing.assert_array_equal(fixed[2], [90., np.nan, 98.])
        np.testing.assert_array_equal(fixed[3], [110., np.nan, 102.])
        np.testing.assert_array_equal(fixed[0], [1, 1, 2])

    def test_multi_component_risk_overwrite_order_and_parallel_read_only_inputs(self):
        rng = np.random.default_rng(18001)
        for n in (1, 2, 33, 257):
            close = rng.normal(100., 5., n)
            idx = rng.integers(0, n + 1, n, dtype=np.int64)
            other = rng.integers(0, n + 1, n, dtype=np.int64)
            levels = (close - rng.uniform(-2., 8., n), close + rng.uniform(-2., 8., n))
            if n >= 33:
                levels[0][:5] = [0., np.nan, -np.inf, np.inf, -0.]
                levels[1][:5] = [np.inf, -np.inf, np.nan, 0., -0.]
            exits = {'S1': (idx, other), 'S4_1m': (other, idx), 'S3_1m': (idx, idx),
                     'LVL_1m': levels}
            extended = {'ATR100_1m': (idx, other, close - 2., close + 2.),
                        'BAD5_1m': (other, idx, close.copy(), close.copy())}
            inputs = [close] + [array for pair in exits.values() for array in pair]
            inputs += [array for data in extended.values() for array in data]
            snapshots = [array.tobytes() for array in inputs]
            for array in inputs:
                array.flags.writeable = False
            combinations = [(), ('S1',), ('S1', 'S4_1m'), ('S3_1m', 'ATR100_1m'),
                            ('ATR100_1m', 'S3_1m'), ('S1', 'S3_1m', 'ATR100_1m', 'BAD5_1m'),
                            ('S3_1m', 'S1'), ('ATR100_1m', 'ATR100_1m')]
            with mock.patch.object(B, 'N', n), mock.patch.object(B, 'CLOSE', close), \
                    mock.patch.object(B, 'extended_stop_data', side_effect=extended.__getitem__):
                expected = [legacy_make(exits, components) for components in combinations]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    actual = list(pool.map(lambda components: B.make_stop_data(exits, components), combinations))
                for got, wanted in zip(actual, expected):
                    self.assert_arrays_identical(got, wanted)
            self.assertEqual(snapshots, [array.tobytes() for array in inputs])


if __name__ == '__main__':
    unittest.main()
