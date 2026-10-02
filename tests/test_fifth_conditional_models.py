import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fifth_conditional_models import conditional_signals, poisson_fit, barrier_labels, barrier_probabilities


def market(count):
    rng = np.random.default_rng(38)
    c = 100*np.exp(np.cumsum(rng.normal(.00001, .002, count)))
    o = np.r_[c[0], c[:-1]] if count else c.copy()
    h = np.maximum(o, c)+.15; l = np.minimum(o, c)-.15
    v = rng.uniform(100, 500, count)
    ct = np.datetime64("2025-01-01T00:00", "ms").astype(np.int64)+(np.arange(count)+1)*60000
    extras = {"trades": rng.poisson(100, count).astype(float), "delta_base": v*rng.uniform(-.8, .8, count)}
    return [o, h, l, c, v, ct], extras


class ConditionalCausality(unittest.TestCase):
    def test_future_changes_do_not_rewrite_any_old_signal_prediction_or_fit(self):
        for number, length, cut in ((78, 1800, 1650), (85, 16400, 15883),
                                    (89, 92000, 90803), (92, 10800, 10610),
                                    (93, 1800, 1650), (97, 11800, 11583), (100, 4000, 3803)):
            with self.subTest(number=number):
                data, extras = market(length); short = {}; full = {}
                a = conditional_signals(number, *(x[:cut] for x in data),
                                        {k: v[:cut] for k, v in extras.items()}, audit=short)
                changed = [x.copy() for x in data]
                for i in (0, 1, 2, 3, 4):
                    changed[i][cut:] *= 1.1
                changed_extras = {k: x.copy() for k, x in extras.items()}
                for x in changed_extras.values():
                    x[cut:] *= 1.1
                b = conditional_signals(number, *changed, changed_extras, audit=full)
                for x, y in zip(a, b):
                    np.testing.assert_array_equal(x, y[:cut])
                for key, value in short.items():
                    if isinstance(value, np.ndarray):
                        np.testing.assert_array_equal(value, full[key][:cut])
                self.assertEqual(short["models"], [m for m in full["models"] if m["at"] < cut])

    def test_training_cutoffs_never_exceed_available_time(self):
        for number, count in ((78, 1800), (85, 15700), (89, 90200), (97, 11800), (100, 3900)):
            with self.subTest(number=number):
                data, extras = market(count); audit = {}
                conditional_signals(number, *data, extras, audit=audit)
                self.assertGreater(len(audit["models"]), 0)
                for model in audit["models"]:
                    self.assertLessEqual(model["label_end"], model["at"])
                    if number in (78, 85):
                        self.assertLess(model["label_end"], model["at"])
                valid = np.isfinite(audit["prediction"])
                self.assertTrue(np.all(audit["training_cutoff"][valid] <= np.flatnonzero(valid)))

    def test_partial_bucket_does_not_use_its_future_final_volume_high_low_or_close(self):
        data, extras = market(15700); before = {}
        conditional_signals(85, *data, extras, audit=before)
        index = int(np.flatnonzero(np.isfinite(before["prediction"]))[0])
        self.assertEqual(index % 15, 4)
        changed = [x.copy() for x in data]
        stop = index-index%15+15
        for k in range(5):
            changed[k][index+1:stop] *= 1.05
        after = {}
        conditional_signals(85, *changed, extras, audit=after)
        self.assertEqual(before["prediction"][index], after["prediction"][index])
        self.assertEqual(before["models"][0], after["models"][0])

    def test_empty_short_and_constant_inputs_are_inactive(self):
        for number in (78, 85, 89, 92, 93, 97, 100):
            with self.subTest(number=number):
                n = 60; c = np.full(n, 100.); v = np.ones(n); ct = (np.arange(n)+1)*60000
                a = conditional_signals(number, c, c, c, c, v, ct, {"trades": v, "delta_base": v*0}, atr=v)
                self.assertFalse(a[0].any() or a[1].any())
                empty = np.array([])
                b = conditional_signals(number, empty, empty, empty, empty, empty, empty.astype(int),
                                        {"trades": empty, "delta_base": empty}, atr=empty)
                self.assertEqual(len(b[0]), 0)


class ConditionalNumerics(unittest.TestCase):
    def test_poisson_recovers_exact_log_linear_intensity(self):
        rng = np.random.default_rng(2)
        x = rng.normal(size=(4000, 2)); expected = np.exp(4+.3*x[:, 0]-.2*x[:, 1])
        coef = poisson_fit(x, expected)
        self.assertIsNotNone(coef)
        np.testing.assert_allclose(coef, [4., .3, -.2], atol=1e-6)

    def test_barrier_labels_use_frozen_atr_and_strict_close_crossing(self):
        close = np.r_[100., 101., 101.01, np.full(13, 100.)]
        atr = np.ones(len(close)); labels, maturity = barrier_labels(close, atr)
        self.assertEqual((labels[0], maturity[0]), (2, 2))
        self.assertEqual((labels[4], maturity[4]), (1, 14))
        self.assertEqual((labels[-1], maturity[-1]), (-1, -1))
        atr[1:] = 100
        changed, times = barrier_labels(close, atr)
        self.assertEqual((changed[0], times[0]), (labels[0], maturity[0]))

    def test_conditional_barrier_counts_match_only_mature_rolling_anchors(self):
        rng = np.random.default_rng(9); n = 10300
        close = 100+np.cumsum(rng.normal(0, .2, n)); atr = np.full(n, .3)
        labels, maturity = barrier_labels(close, atr)
        cells = rng.integers(0, 2, n, dtype=np.int64)
        probabilities, counts = barrier_probabilities(cells, labels, maturity)
        for t in (70, 300, 700, 10100, 10299):
            anchors = np.arange(max(0, t-10080), t)
            keep = anchors[(maturity[anchors] >= 0)&(maturity[anchors] <= t)&(cells[anchors] == cells[t])]
            expected = np.bincount(labels[keep], minlength=3)
            self.assertEqual(counts[t], len(keep))
            if len(keep) >= 100:
                np.testing.assert_allclose(probabilities[t], (expected+1)/(len(keep)+3))
            else:
                self.assertTrue(np.isnan(probabilities[t]).all())

    def test_word_median_uses_original_symbols_and_mature_targets(self):
        n = 2400; returns = np.tile([.001, .001, -.0003, -.0003, .001, -.0003], 400)
        c = 100*np.exp(np.cumsum(returns)); o = np.r_[c[0], c[:-1]]
        audit = {}; ct = (np.arange(n)+1)*60000
        conditional_signals(93, o, np.maximum(c, o)+.1, np.minimum(c, o)-.1, c, np.ones(n), ct,
                            atr=np.ones(n), audit=audit)
        valid = np.flatnonzero(np.isfinite(audit["prediction"]))
        self.assertGreater(len(valid), 0)
        for t in valid[::max(1, len(valid)//8)]:
            anchors = np.arange(max(0, t-10080), t-4)
            anchors = anchors[audit["word"][anchors] == audit["word"][t]]
            target = np.log(c[anchors+5])-np.log(c[anchors])
            self.assertEqual(audit["sample_count"][t], len(anchors))
            self.assertAlmostEqual(audit["prediction"][t], np.median(target), places=12)

    def test_flow_model_quality_uses_saved_current_model_predictions(self):
        data, extras = market(11800); audit = {}
        conditional_signals(97, *data, extras, audit=audit)
        valid = np.flatnonzero(np.isfinite(audit["mature_error_mae"]))
        self.assertGreater(len(valid), 0)
        t = int(valid[len(valid)//2]); model = audit["model_id"][t]
        anchors = np.arange(t-249, t-9)
        self.assertTrue(np.all(audit["model_id"][anchors] == model))
        y = np.log(data[3][anchors+10])-np.log(data[3][anchors])
        self.assertAlmostEqual(audit["mature_error_mae"][t], np.mean(np.abs(audit["prediction"][anchors]-y)), places=12)
        self.assertAlmostEqual(audit["mature_outcome_std"][t], y.std(ddof=0), places=12)
        for fit in audit["models"]:
            before = min(len(data[3]), fit["at"]+249)
            self.assertTrue(np.isnan(audit["mature_error_mae"][fit["at"]:before]).all())

    def test_conformal_uses_901st_stored_error_and_separate_training(self):
        data, extras = market(4100); audit = {}
        conditional_signals(100, *data, extras, audit=audit)
        valid = np.flatnonzero(np.isfinite(audit["calibration_quantile"]))
        self.assertGreater(len(valid), 0)
        for t in valid[::max(1, len(valid)//5)]:
            anchors = np.flatnonzero(np.isfinite(audit["prediction"][:t-4]))[-1000:]
            targets = np.log(data[3][anchors+5])-np.log(data[3][anchors])
            errors = np.abs(audit["prediction"][anchors]-targets)
            self.assertAlmostEqual(audit["calibration_quantile"][t], sorted(errors)[900], places=12)
            self.assertLess(audit["training_cutoff"][t], anchors[0])
            self.assertEqual(audit["calibration_first_anchor"][t], anchors[0])


if __name__ == "__main__":
    unittest.main()
