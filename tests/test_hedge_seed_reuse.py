"""Only unreachable random-choice branches may share a simulation across seeds."""
import itertools
import unittest

import numpy as np

from hedge_config import normalize_config
from hedge_engine import run_case
from hedge_worker import seed_independent


class SeedReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        n = 4320
        rng = np.random.default_rng(715)
        close = 2500 + np.cumsum(rng.normal(0, 2.5, n))
        opening = np.r_[2500., close[:-1]]
        cls.data = dict(ts=1735776000000 + np.arange(n, dtype=np.int64)*60000,
                        o=opening, h=np.maximum(opening, close)+rng.uniform(.5, 3., n),
                        l=np.minimum(opening, close)-rng.uniform(.5, 3., n), c=close,
                        tp=np.where(np.arange(n)<2880, .004, .002))
        cls.ids = np.full(n, -1, np.int64)
        cls.one_side = np.zeros((n, 2), np.bool_)
        cls.one_side[np.arange(n), (np.arange(n)//15) % 2] = True
        cls.both = np.ones((n, 2), np.bool_)

    def test_overlapping_unrestricted_signals_keep_random_trials(self):
        config = normalize_config(dict(mode="single", entry_gap_minutes=1))
        self.assertFalse(seed_independent(self.both, config))
        # A single overlap must retain independent trials, even when other bars are unique.
        rare = self.one_side.copy(); rare[100] = True
        self.assertFalse(seed_independent(rare, config))
        results = [run_case(self.data, config, 1, seed, self.both, self.ids) for seed in (0, 7, 42)]
        self.assertGreater(len({r["stats"]["ending_equity"] for r in results}), 1)

    def test_all_account_outputs_equal_when_random_choice_is_unreachable(self):
        for mode, path, fee, calendar in itertools.product(("single", "scale_in"), (0, 1), (0., .0002), (False, True)):
            with self.subTest(mode=mode, path=path, fee=fee, calendar=calendar):
                config = normalize_config(dict(mode=mode, entry_gap_minutes=1, maker_fee_rate=fee,
                    direction_rule="yijing-midpoint-v1" if calendar else "OFF"))
                data = dict(self.data)
                if calendar:
                    data["direction_limits"] = np.where((np.arange(len(self.ids)+1)//720)%2, -1, 1).astype(np.int8)
                signals = self.both if calendar else self.one_side
                self.assertTrue(seed_independent(signals, config))
                original = run_case(data, config, 1, 0, signals, self.ids, add_drop=.001, path=path, detail=True)
                self.assertGreater(original["stats"]["trades"], 10)
                if mode == "scale_in":
                    self.assertGreater(original["stats"]["add_entries"], 0)
                for seed in (7, 42):
                    actual = run_case(data, config, 1, seed, signals, self.ids, add_drop=.001, path=path, detail=True)
                    np.testing.assert_array_equal(list(actual["stats"].values()), list(original["stats"].values()))
                    for key in ("daily", "trades", "fills", "positions"):
                        np.testing.assert_array_equal(actual[key], original[key])


if __name__ == "__main__":
    unittest.main()
