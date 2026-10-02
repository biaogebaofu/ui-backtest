import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from feature_builder import build_features, load_source


class FeatureBuilderTests(unittest.TestCase):
    def test_invalid_prices_and_conflicting_duplicate_candles_are_rejected(self):
        base = pd.DataFrame({"openTime": np.arange(600, dtype=np.int64) * 60000,
                             "open": 100., "high": 101., "low": 99., "close": 100., "volume": 1.})
        bad_high = base.copy(); bad_high.loc[5, "high"] = 90.
        nan_price = base.copy(); nan_price.loc[5, "close"] = np.nan
        different = base.iloc[[5]].copy(); different["volume"] = 2.
        duplicate = pd.concat([base, different], ignore_index=True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.csv"
            for frame, message in ((bad_high, "OHLC价格关系"), (nan_price, "NaN"),
                                   (duplicate, "内容冲突")):
                frame.to_csv(path, index=False)
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_source(path)

    def test_partial_multitimeframe_buckets_are_discarded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            count = 1000
            start = pd.Timestamp("2026-01-01T00:02:00Z")
            times = (start.value // 1_000_000 + np.arange(count) * 60_000).astype(np.int64)
            price = 2000.0 + np.arange(count) * 0.01
            pd.DataFrame({
                "openTime": times, "open": price, "high": price + 1.0,
                "low": price - 1.0, "close": price + 0.2,
                "volume": np.ones(count),
            }).to_csv(root / "bars.csv", index=False)
            path = build_features(str(root / "bars.csv"), str(root / "cache"))
            with np.load(path) as data:
                close_times = data["4h_ct"]
                self.assertEqual(len(close_times), 3)
                self.assertEqual(
                    int(close_times[0]),
                    int(pd.Timestamp("2026-01-01T08:00:00Z").value // 1_000_000),
                )


if __name__ == "__main__":
    unittest.main()
