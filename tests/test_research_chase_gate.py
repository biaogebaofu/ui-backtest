"""Past-only B04 candidate: 5 one-minute closes / SMA(TR,14), not Wilder ATR.

Reference: loss_attribution_20260922/deep_regimes/build_regimes.py PastState.extra
and hypotheses.py B04. Research association is not a proven strategy benefit.
"""
import unittest

import numpy as np

from entry_position import POSITION_FILTERS, position_masks


def market(close):
    close = np.asarray(close, dtype=float)
    return {"close": close, "high": close + 1, "low": close - 1,
            "ct1": (np.arange(len(close), dtype=np.int64) + 1) * 60_000}


def research_reference(data, limit):
    """Independent per-anchor equivalent of Bars.count and B04's predicate."""
    close, high, low, ct = (data[k] for k in ("close", "high", "low", "ct1"))
    result = [np.zeros(len(close), dtype=bool), np.zeros(len(close), dtype=bool)]
    for i in range(23, len(close)):
        if not np.all(np.diff(ct[i - 23:i + 1]) == 60_000):
            continue
        window = np.stack([close[i - 23:i + 1], high[i - 23:i + 1], low[i - 23:i + 1]])
        if not np.isfinite(window).all() or np.any(high[i - 23:i + 1] < low[i - 23:i + 1]):
            continue
        tr = [max(high[j] - low[j], abs(high[j] - close[j - 1]),
                  abs(low[j] - close[j - 1])) for j in range(i - 13, i + 1)]
        atr = sum(tr) / 14
        if atr <= 0:
            continue
        change = close[i] - close[i - 5]
        result[0][i], result[1][i] = change < limit * atr, -change < limit * atr
    return result


class ResearchChaseGateTests(unittest.TestCase):
    def test_old_thirteen_filters_remain_and_new_candidates_are_explicit(self):
        old = {"OFF", "EMA5_ATR075", "EMA5_ATR100", "EMA5_ATR150", "EMA5_ATR200",
               "RANGE30_EDGE10", "RANGE30_EDGE20", "RANGE60_EDGE10", "RANGE60_EDGE20",
               "EMA5_ATR100_R60_E10", "EMA5_ATR100_R60_E20",
               "EMA5_ATR150_R60_E10", "EMA5_ATR150_R60_E20"}
        new = {f"RESEARCH_CHASE5_SMAATR{x}" for x in (100, 150, 200)}
        self.assertEqual(set(POSITION_FILTERS), old | new)
        for code in new:
            self.assertIn("研究", POSITION_FILTERS[code]["label"])

    def test_24_closed_bars_required_and_exact_equality_is_rejected(self):
        # Increment .6 => 5-close change 3, TR=2, 3/2=1.5 exactly.
        data = market(100 + np.arange(50) * .6)
        data["close"] = np.round(data["close"], 8)
        data["high"], data["low"] = data["close"] + 1, data["close"] - 1
        long_ok, short_ok = position_masks("RESEARCH_CHASE5_SMAATR150", data)
        self.assertFalse(long_ok[:23].any())
        self.assertFalse(short_ok[:23].any())
        # Choose the binary-exact 112 -> 115 endpoint, avoiding fixture round-off.
        self.assertEqual(float(data["close"][25] - data["close"][20]), 3.)
        self.assertFalse(long_ok[25])
        self.assertTrue(short_ok[25])
        negative = market(200 - data["close"])
        self.assertTrue(position_masks("RESEARCH_CHASE5_SMAATR150", negative)[0][25])
        self.assertFalse(position_masks("RESEARCH_CHASE5_SMAATR150", negative)[1][25])

    def test_threshold_buckets_and_sma_reference(self):
        rng = np.random.default_rng(319)
        data = market(100 + np.cumsum(rng.normal(0, .5, 400)))
        # Vary true range sharply: SMA and Wilder are intentionally not interchangeable.
        data["high"][110:115] += 15
        data["low"][205:208] -= 9
        for bucket in (100, 150, 200):
            actual = position_masks(f"RESEARCH_CHASE5_SMAATR{bucket}", data)
            expected = research_reference(data, bucket / 100)
            for a, e in zip(actual, expected):
                np.testing.assert_array_equal(a, e)

    def test_prefix_invariance_no_future_bars_or_prices(self):
        rng = np.random.default_rng(392)
        data = market(100 + np.cumsum(rng.normal(0, .9, 251)))
        baseline = position_masks("RESEARCH_CHASE5_SMAATR150", data)
        for end in (1, 13, 23, 24, 30, 79, 161, 250):
            prefix = {key: value[:end].copy() for key, value in data.items()}
            for full, partial in zip(baseline, position_masks("RESEARCH_CHASE5_SMAATR150", prefix)):
                np.testing.assert_array_equal(partial, full[:end])
        changed = {key: value.copy() for key, value in data.items()}
        for key in ("high", "low", "close"):
            changed[key][90:] *= 100
        for full, modified in zip(baseline, position_masks("RESEARCH_CHASE5_SMAATR150", changed)):
            np.testing.assert_array_equal(full[:90], modified[:90])

    def test_missing_minute_requires_24_new_consecutive_bars(self):
        data = market(np.full(80, 100.))
        data["ct1"][30:] += 60_000
        actual = position_masks("RESEARCH_CHASE5_SMAATR150", data)
        for mask in actual:
            self.assertTrue(mask[29])
            self.assertFalse(mask[30:53].any())
            self.assertTrue(mask[53])

    def test_missing_and_duplicate_minutes_cannot_cancel_each_other(self):
        data = market(np.full(70, 100.))
        # A two-minute jump then one duplicated close; total 24-bar span looks normal.
        data["ct1"][30] += 60_000
        for mask in position_masks("RESEARCH_CHASE5_SMAATR150", data):
            self.assertFalse(mask[40])
            self.assertTrue(mask[54])

    def test_nonfinite_and_zero_atr_do_not_mean_safe(self):
        for key in ("close", "high", "low"):
            data = market(np.full(80, 100.))
            data[key][30] = np.nan
            for mask in position_masks("RESEARCH_CHASE5_SMAATR150", data):
                self.assertFalse(mask[30:54].any())
                self.assertTrue(mask[54])
        data = market(np.full(50, 100.))
        data["high"] = data["low"] = data["close"].copy()
        self.assertFalse(any(mask.any() for mask in position_masks("RESEARCH_CHASE5_SMAATR150", data)))

    def test_invalid_ohlc_is_blocked_and_input_is_not_mutated(self):
        for key, value in (("high", 99.), ("low", 101.), ("low", 0.)):
            data = market(np.full(80, 100.))
            data[key][30] = value
            before = {name: values.copy() for name, values in data.items()}
            for mask in position_masks("RESEARCH_CHASE5_SMAATR150", data):
                self.assertFalse(mask[30:54].any())
                self.assertTrue(mask[54])
            for name in data:
                np.testing.assert_array_equal(data[name], before[name])


if __name__ == "__main__":
    unittest.main()
