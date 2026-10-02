"""Preserve first-hit semantics and reuse bounded stop-component arrays."""
from bisect import bisect_right
import unittest
from unittest import mock

import numpy as np

from test_support import prepare_test_features

prepare_test_features()

import engine as E
import backtest_engine as B
from extended_rules import extra_stop_components


def legacy_first_below(arr, lvl, sentinel):
    """The previous Python scan, retained as an independent regression oracle."""
    m = len(arr)
    ci, cv = [], []
    prev_l, prev_a = None, sentinel
    arr_l, lvl_l = arr.tolist(), lvl.tolist()
    out = [sentinel] * m
    for i in range(m - 1, -1, -1):
        j = i + 1
        if j >= m:
            ans = sentinel
        else:
            level = lvl_l[i]
            if arr_l[j] <= level:
                ans = j
            elif level == prev_l:
                ans = prev_a
            else:
                p = bisect_right(cv, level) - 1
                ans = ci[p] if p >= 0 else sentinel
        out[i] = ans
        prev_l, prev_a = lvl_l[i], ans
        value = arr_l[i]
        while cv and cv[-1] >= value:
            cv.pop(); ci.pop()
        cv.append(value); ci.append(i)
    return np.asarray(out, dtype=np.int64)


class FirstHitScanTests(unittest.TestCase):
    def test_original_results_for_random_repeated_and_nonfinite_prices(self):
        rng = np.random.default_rng(271828)
        for n in (0, 1, 2, 7, 31, 2048):
            values = 2000. + np.cumsum(rng.normal(0., .5, n))
            targets = values + rng.normal(0., 3., n)
            special_values, special_targets = values.copy(), targets.copy()
            if n >= 7:
                special_values[:3] = [np.nan, np.inf, -np.inf]
                special_targets[3:6] = [np.nan, np.inf, -np.inf]
            variants = [(values, targets), (values, np.full(n, 2000.)),
                        (np.round(values), np.round(targets)),
                        (special_values, special_targets), (values[::-2], targets[::-2])]
            for arr, lvl in variants:
                for sign in (1., -1.):
                    with self.subTest(n=n, sign=sign), mock.patch.object(E, 'N', n + 17):
                        expected = legacy_first_below(sign * arr, sign * lvl, E.N)
                        actual = E.first_below(arr, lvl) if sign == 1. else E.first_above(arr, lvl)
                        np.testing.assert_array_equal(actual, expected)

    def test_hits_are_strictly_after_entry_and_include_equal_levels(self):
        arr = np.array([3., 1., 2., 1., 4.])
        with mock.patch.object(E, 'N', 99):
            np.testing.assert_array_equal(E.first_below(arr, arr), [1, 3, 3, 99, 99])
            np.testing.assert_array_equal(E.first_above(arr, arr), [4, 2, 4, 4, 99])

    def test_runtime_sentinel_is_not_frozen_in_numba_cache(self):
        arr, lvl = np.array([5., 6.]), np.array([-1., -1.])
        for sentinel in (2, 882720, 7):
            with mock.patch.object(E, 'N', sentinel):
                np.testing.assert_array_equal(E.first_below(arr, lvl), [sentinel, sentinel])


class ExtendedStopCacheTests(unittest.TestCase):
    def setUp(self):
        B.extended_stop_data.cache_clear()
        self.addCleanup(B.extended_stop_data.cache_clear)

    def test_full_component_pass_reuses_arrays_within_memory_bound(self):
        components = [code for code, _ in extra_stop_components()]
        cached = {code: B.extended_stop_data(code) for code in components}
        first_info = B.extended_stop_data.cache_info()
        self.assertEqual(first_info.misses, len(components))
        self.assertLessEqual(first_info.maxsize, 84)
        self.assertLessEqual(first_info.maxsize * B.N * 4 * 8, 2_400_000_000)
        for code in components:
            self.assertIs(B.extended_stop_data(code), cached[code])
            uncached = B.extended_stop_data.__wrapped__(code)
            for actual, expected in zip(cached[code], uncached):
                np.testing.assert_array_equal(actual, expected)
        self.assertEqual(B.extended_stop_data.cache_info().misses, first_info.misses)

    def test_composite_stops_do_not_modify_cached_component_arrays(self):
        components = ('ATR100_1m', 'BAD5_1m', 'MA20C1_1m')
        original = {code: tuple(array.copy() for array in B.extended_stop_data(code))
                    for code in components}
        B.make_stop_data({}, components)
        for code in components:
            for actual, expected in zip(B.extended_stop_data(code), original[code]):
                np.testing.assert_array_equal(actual, expected)


if __name__ == '__main__':
    unittest.main()
