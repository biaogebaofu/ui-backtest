import sys
from pathlib import Path
import unittest

import numpy as np
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fifth_stat_models import tv_denoise, recurrence_determinism, stat_model_signals


class StatisticalNumerics(unittest.TestCase):
    def test_total_variation_dual_kkt_and_independent_optimizer(self):
        rng = np.random.default_rng(739)
        for n in (2, 7, 30, 120):
            for penalty in (.03, .7, 3., 40.):
                y = rng.normal(size=n)
                x, ok = tv_denoise(y, penalty)
                self.assertTrue(ok)
                dual = np.cumsum(x-y)[:-1]
                self.assertLessEqual(np.max(np.abs(dual)), penalty/2+1e-10)
                self.assertAlmostEqual(float(np.sum(x-y)), 0., places=9)
                change = np.diff(x)
                mask = np.abs(change) > 1e-9
                np.testing.assert_allclose(dual[mask], penalty/2*np.sign(change[mask]), atol=1e-9)
                if n <= 7:
                    d = np.diff(np.eye(n), axis=0)
                    objective = lambda p: (.5*np.sum((y-d.T@p)**2), d@(d.T@p-y))
                    result = minimize(objective, np.zeros(n-1), jac=True, method="L-BFGS-B",
                                      bounds=[(-penalty/2, penalty/2)]*(n-1),
                                      options={"ftol": 1e-15, "gtol": 1e-11, "maxiter": 10000})
                    np.testing.assert_allclose(x, y-d.T@result.x, atol=1e-7)

    def test_total_variation_identity_and_constant(self):
        for x in (np.array([5.]), np.full(120, .02), np.arange(20, dtype=float)):
            fitted, ok = tv_denoise(x, 0.)
            self.assertTrue(ok)
            np.testing.assert_array_equal(fitted, x)
        fitted, ok = tv_denoise(np.full(120, .02), 1.)
        self.assertTrue(ok)
        np.testing.assert_allclose(fitted, .02)

    def test_recurrence_matches_full_matrix_reference(self):
        rng = np.random.default_rng(5)
        for x in (rng.normal(size=120), np.sin(np.arange(120)*.3), np.tile([1., -1.], 60)):
            z = (x-x.mean())/x.std()
            vectors = np.lib.stride_tricks.sliding_window_view(z, 3)
            points = np.sqrt(np.sum((vectors[:, None]-vectors[None, :])**2, axis=2)) < .5
            ii, jj = np.indices(points.shape)
            points[np.abs(ii-jj) <= 2] = False
            used = 0
            for offset in range(-len(points)+1, len(points)):
                diagonal = np.diag(points, offset)
                runs = np.diff(np.r_[False, diagonal, False].astype(int))
                lengths = np.flatnonzero(runs == -1)-np.flatnonzero(runs == 1)
                used += lengths[lengths >= 3].sum()
            expected = used/points.sum() if points.any() else np.nan
            self.assertAlmostEqual(recurrence_determinism(x), expected, places=12)
        self.assertTrue(np.isnan(recurrence_determinism(np.ones(120))))


class ModelCausality(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(13)
        self.c = 100*np.exp(np.cumsum(rng.normal(0, .002, 3400)))
        self.a = np.full(len(self.c), .5)
        self.v = rng.uniform(100, 500, len(self.c))
        self.delta = rng.uniform(-.9, .9, len(self.c))*self.v

    def test_append_future_cannot_change_signals_predictions_or_old_fits(self):
        boundary = 3170
        for number in (30, 34, 37, 38, 73):
            with self.subTest(number=number):
                short = {}; long = {}
                left = stat_model_signals(number, self.c[:boundary], self.a[:boundary],
                                          volume=self.v[:boundary], delta=self.delta[:boundary], audit=short)
                altered = self.c.copy(); altered[boundary:] *= 1.4
                right = stat_model_signals(number, altered, self.a, volume=self.v, delta=self.delta, audit=long)
                for x, y in zip(left, right):
                    np.testing.assert_array_equal(x, y[:boundary])
                for key, value in short.items():
                    if isinstance(value, np.ndarray):
                        np.testing.assert_array_equal(value, long[key][:boundary])
                old = [x for x in long["models"] if x["at"] < boundary]
                self.assertEqual(short["models"], old)

    def test_models_only_use_mature_labels_and_fixed_retrain_schedule(self):
        for number, samples, horizon, stride in ((37, 1440, 1, 60), (73, 2880, 3, 60)):
            audit = {}
            stat_model_signals(number, self.c, self.a, volume=self.v, delta=self.delta, audit=audit)
            self.assertGreater(len(audit["models"]), 2)
            for fit in audit["models"]:
                self.assertEqual(fit["train_last_anchor"]+horizon, fit["label_end"])
                self.assertEqual(fit["label_end"], fit["at"])
                self.assertEqual(fit["train_last_anchor"]-fit["train_start"]+1, samples)
            self.assertTrue(np.all(np.diff([x["at"] for x in audit["models"]]) == stride))
            valid = np.isfinite(audit["prediction"])
            self.assertTrue(np.all(audit["training_cutoff"][valid] <= np.flatnonzero(valid)))

    def test_ou_training_excludes_current_price(self):
        audit = {}
        stat_model_signals(38, self.c, self.a, audit=audit)
        for fit in audit["models"]:
            self.assertEqual(fit["label_end"], fit["at"]-1)
            self.assertEqual(fit["train_start"], fit["at"]-720)
        changed = self.c.copy(); changed[900] *= 1.1
        other = {}
        stat_model_signals(38, changed, self.a, audit=other)
        a = next(x for x in audit["models"] if x["at"] == 900)
        b = next(x for x in other["models"] if x["at"] == 900)
        self.assertEqual(a["coefficients"], b["coefficients"])

    def test_ar_first_forecast_matches_direct_mature_ols(self):
        audit = {}
        stat_model_signals(37, self.c, self.a, audit=audit)
        fit = audit["models"][0]
        r = np.r_[np.nan, np.diff(np.log(self.c))]
        anchors = np.arange(fit["train_start"], fit["train_last_anchor"]+1)
        x = np.column_stack([np.ones(len(anchors))]+[r[anchors-j] for j in range(5)])
        coef = np.linalg.lstsq(x, r[anchors+1], rcond=1e-12)[0]
        np.testing.assert_allclose(coef, fit["coefficients"], atol=1e-12)
        t = fit["at"]
        self.assertAlmostEqual(audit["prediction"][t], coef @ np.r_[1., r[t-np.arange(5)]], places=12)

    def test_constant_and_short_inputs_emit_no_signal(self):
        for number in (30, 34, 37, 38, 73):
            for count in (0, 40, 3000):
                c = np.full(count, 100.)
                signals = stat_model_signals(number, c, np.ones(count), volume=np.ones(count), delta=np.zeros(count))
                self.assertEqual(sum(int(x.sum()) for x in signals), 0)

    def test_tv_requires_completed_five_bar_new_segment(self):
        returns = np.r_[np.full(160, -.001), np.full(80, .001)]
        c = 100*np.exp(np.cumsum(returns))
        audit = {}
        up, _ = stat_model_signals(30, c, np.ones(len(c)), audit=audit)
        self.assertTrue(up.any())
        self.assertTrue(np.all(audit["segment_duration"][up] >= 5))
        self.assertFalse(up[:165].any())


if __name__ == "__main__":
    unittest.main()
