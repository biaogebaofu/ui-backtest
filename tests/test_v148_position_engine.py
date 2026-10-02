"""Entry-position boundaries, closed-candle timing and legacy gate integration."""
import unittest
from unittest import mock

import numpy as np

from entry_position import POSITION_FILTERS, normalize_position_filters, position_masks


def market(n=360):
    ct = (np.arange(n, dtype=np.int64) + 1) * 60_000
    ct5 = (np.arange(n // 5, dtype=np.int64) + 1) * 300_000
    return {
        "ct1": ct, "close": np.full(n, 100.),
        "high": np.full(n, 110.), "low": np.full(n, 90.),
        "5m_ct": ct5, "5m_close": np.full(len(ct5), 100.),
        "5m_high": np.full(len(ct5), 101.), "5m_low": np.full(len(ct5), 99.),
        "5m_map": np.searchsorted(ct5, ct, side="right") - 1,
    }


class PositionAlgorithmTests(unittest.TestCase):
    def test_catalog_normalization_and_off_need_no_reference_data(self):
        self.assertEqual(len(POSITION_FILTERS), 16)
        self.assertEqual(normalize_position_filters(["OFF", "EMA5_ATR075", "OFF"]),
                         ["OFF", "EMA5_ATR075"])
        for raw in (None, [], "OFF", ["missing"], [None]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_position_filters(raw)
        for mask in position_masks("OFF", {"close": np.array([100., np.nan])}):
            np.testing.assert_array_equal(mask, [True, True])
        with self.assertRaises(ValueError):
            position_masks("missing", market())

    def test_ema_directional_thresholds_and_warmup(self):
        data = market()
        # Native EMA=100 and ATR=2. Equality is allowed in both directions.
        data["close"][236:240] = [102., 102.001, 98., 97.999]
        long_ok, short_ok = position_masks("EMA5_ATR100", data)
        self.assertFalse(long_ok[:99].any())
        self.assertFalse(short_ok[:99].any())
        self.assertTrue(long_ok[99])  # twentieth 5m close has become available
        np.testing.assert_array_equal(long_ok[236:240], [True, False, True, True])
        np.testing.assert_array_equal(short_ok[236:240], [True, True, True, False])

    def test_zero_atr_and_no_native_bars_block_active_filter(self):
        data = market()
        data["5m_high"][:] = 100.
        data["5m_low"][:] = 100.
        self.assertFalse(any(mask.any() for mask in position_masks("EMA5_ATR100", data)))
        empty = market(3)
        self.assertFalse(any(mask.any() for mask in position_masks("EMA5_ATR100", empty)))

    def test_unclosed_stale_and_missing_native_bars_are_not_used(self):
        for mapping in (60, 58, -1, 999):
            data = market()
            data["5m_map"][300] = mapping
            # minute301: native60 closes at305 (future), native58 at295 (stale).
            with self.subTest(mapping=mapping):
                self.assertFalse(any(mask[300] for mask in position_masks("EMA5_ATR100", data)))
        data = market()
        data["5m_ct"][45:] += 300_000
        data["5m_map"] = np.searchsorted(data["5m_ct"], data["ct1"], side="right") - 1
        self.assertFalse(any(mask[300] for mask in position_masks("EMA5_ATR100", data)))

    def test_prior_range_boundaries_current_bar_excluded(self):
        for code, upper, lower in (("RANGE30_EDGE10", 108., 92.),
                                   ("RANGE60_EDGE20", 106., 94.)):
            for price, expected in ((upper, (True, True)), (upper+.001, (False, True)),
                                    (lower, (True, True)), (lower-.001, (True, False)),
                                    (111., (False, True)), (89., (True, False))):
                data = market()
                data["close"][200] = price
                data["high"][200], data["low"][200] = 1000., 1.
                with self.subTest(code=code, price=price):
                    self.assertEqual(tuple(bool(mask[200]) for mask in position_masks(code, data)), expected)

    def test_range_requires_warmup_nonzero_width_and_contiguous_minutes(self):
        data = market()
        masks = position_masks("RANGE30_EDGE10", data)
        self.assertFalse(any(mask[:30].any() for mask in masks))
        self.assertTrue(all(mask[30] for mask in masks))
        data["ct1"][190:] += 60_000
        self.assertFalse(any(mask[200] for mask in position_masks("RANGE30_EDGE10", data)))
        self.assertTrue(all(mask[220] for mask in position_masks("RANGE30_EDGE10", data)))
        data = market()
        data["high"][:] = data["low"][:] = 100.
        self.assertFalse(any(mask.any() for mask in position_masks("RANGE30_EDGE10", data)))

    def test_combination_is_and_and_sources_are_not_mutated(self):
        data = market()
        data["close"][:] = np.linspace(85., 115., len(data["close"]))
        originals = {key: value.copy() for key, value in data.items()}
        for bucket in (100, 150):
            for edge in (10, 20):
                ema = position_masks(f"EMA5_ATR{bucket}", data)
                ranges = position_masks(f"RANGE60_EDGE{edge}", data)
                combined = position_masks(f"EMA5_ATR{bucket}_R60_E{edge}", data)
                for side in (0, 1):
                    np.testing.assert_array_equal(combined[side], ema[side] & ranges[side])
        for key in data:
            np.testing.assert_array_equal(data[key], originals[key])

    def test_all_presets_are_prefix_invariant(self):
        data = market(600)
        rng = np.random.default_rng(148)
        close = 100. + np.cumsum(rng.normal(0., .15, 600))
        data["close"], data["high"], data["low"] = close, close+.2, close-.2
        data["5m_close"] = close[4::5].copy()
        data["5m_high"] = data["high"].reshape(-1, 5).max(axis=1)
        data["5m_low"] = data["low"].reshape(-1, 5).min(axis=1)
        for cut in (103, 299, 400):
            prefix = {key: value[:cut // 5 if key.startswith("5m_") and key != "5m_map" else cut].copy()
                      for key, value in data.items()}
            for code in POSITION_FILTERS:
                with self.subTest(code=code, cut=cut):
                    full_masks = position_masks(code, data)
                    prefix_masks = position_masks(code, prefix)
                    for side in (0, 1):
                        np.testing.assert_array_equal(full_masks[side][:cut], prefix_masks[side])

    def test_nonfinite_signal_close_blocks_active_presets(self):
        data = market()
        data["close"][200:203] = [np.nan, np.inf, -np.inf]
        for code in POSITION_FILTERS.keys() - {"OFF"}:
            self.assertFalse(any(mask[200:203].any() for mask in position_masks(code, data)))


class PositionGateTests(unittest.TestCase):
    def test_position_intersects_s3_without_consuming_direction_or_changing_exits(self):
        from test_support import prepare_test_features
        prepare_test_features()
        import backtest_engine as B
        signals = {"d1": np.ones(4, dtype=np.int8), "ma120": np.zeros(4)}
        high_tf = {tf: {0: (np.ones(4, bool), np.ones(4, bool))}
                   for tf in ("4h", "1h", "15m", "5m")}
        exits = {"sentinel": object()}
        position = (np.array([False, False, True, True]), np.ones(4, bool))
        s3 = (np.array([True, True, True, False]), np.ones(4, bool))
        B.build_field.cache_clear()
        self.addCleanup(B.build_field.cache_clear)
        with mock.patch.object(B, "N", 4), mock.patch.object(B.R, "build", return_value=(signals, exits, high_tf, None)), \
                mock.patch.object(B, "entry_position_gate", return_value=position), \
                mock.patch.object(B, "s3_baseline_gate", return_value=s3):
            base = B.build_field("hist", ((0, 0, 0, 0, 1),), ("S1",), materialize_stops=False)
            result = B.build_field("hist", ((0, 0, 0, 0, 1),), ("S1",),
                                   materialize_stops=False, position_filter="EMA5_ATR100")
        self.assertIs(result[0], signals)
        self.assertIs(result[1], exits)
        np.testing.assert_array_equal(result[2], base[2])
        self.assertEqual(result[4], base[4])
        # Earlier rejected minutes did not consume the otherwise-eligible same direction segment.
        np.testing.assert_array_equal(result[3][0][1], [2])
        np.testing.assert_array_equal(result[3][0][2], [1])
        np.testing.assert_array_equal(s3[0], [True, True, True, False])
        np.testing.assert_array_equal(position[0], [False, False, True, True])


if __name__ == "__main__":
    unittest.main()
