"""Reference checks for faster fitted quantile-model evaluation."""
import unittest
from unittest.mock import patch

import numpy as np
from threadpoolctl import threadpool_limits

import fifth_learned_models as m


def legacy_quantile(x, r, ct):
    """v1.61 reference: keep sklearn's original per-row inference calls."""
    from sklearn.linear_model import QuantileRegressor
    from sklearn.preprocessing import StandardScaler
    n = len(r); bounds = np.full((n, 2), np.nan)
    model = None; up = np.zeros(n, bool); down = up.copy()
    for t in range(262, n):
        if (t-262) % 60 == 0:
            indices = np.arange(t-241, t-1)
            if not np.isfinite(x[indices]).all() or not np.isfinite(r[indices+1]).all():
                model = None
                m.record_fit(22, ct[t-1], None,
                             m._settings(22, event='invalid training features; this fit disabled'))
                continue
            scaler = StandardScaler().fit(x[indices]); xx = scaler.transform(x[indices])
            models = [QuantileRegressor(quantile=q, alpha=.01, solver='highs').fit(xx, r[indices+1])
                      for q in (.1, .9)]
            model = scaler, models
            m.record_fit(22, ct[t-1], model, m._settings(
                22, training=240, refit=60, alpha=.01, quantiles=[.1, .9],
                feature_order=['r', 'lag_r', 'r5', 'ATR/C'], train_end=int(ct[t-1]),
                forecast='frozen before observing current return'))
        if model is None or not np.isfinite(x[t-1]).all():
            continue
        scaler, models = model
        xx = scaler.transform(x[t-1:t])
        bounds[t] = [model.predict(xx)[0] for model in models]
        if bounds[t, 0] <= bounds[t, 1]:
            up[t] = r[t] > bounds[t, 1]; down[t] = r[t] < bounds[t, 0]
    return m._first(up), m._first(down), bounds, {}


def sample(n, seed=19):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 4)) * np.array([.001, .001, .003, .0002])
    x[:, 3] += .001
    r = np.r_[0., 2*x[:-1, 0]+rng.normal(0, .0002, n-1)]
    return x, r, (np.arange(n)+1)*60000


def legacy_ssa(logc, ct):
    """v1.61 SSA reference retains the original NumPy row additions."""
    n=len(logc); prediction=np.full(n,np.nan); up=np.zeros(n,bool); down=up.copy()
    errors=np.full(n,np.nan); scale=np.full(n,np.nan); projection=None; coef=None
    counts=np.convolve(np.ones(30),np.ones(91))
    for t in range(119,n):
        if t and np.isfinite(prediction[t-1]):
            errors[t]=logc[t]-(logc[t-1]+prediction[t-1])
        window=logc[t-119:t+1]
        matrix=np.lib.stride_tricks.sliding_window_view(window,30).T
        if (t-119)%5==0:
            u,_,_=np.linalg.svd(matrix,full_matrices=False);u=u[:,:2]
            last=u[-1];denominator=1-float(last@last)
            coef=None if denominator<=1e-6 else u[:-1]@last/denominator
            projection=u@u.T
            m.record_fit(17,ct[t],dict(projection=projection,coefficients=coef),m._settings(
                17,window=120,embedding=30,rank=2,refit=5,train_end=int(ct[t]),
                between_fits='frozen projection reconstructs latest window'))
        if coef is None:continue
        reconstructed=projection@matrix;diagonal=np.zeros(120)
        for row in range(30):diagonal[row:row+91]+=reconstructed[row]
        reconstructed=diagonal/counts
        prediction[t]=coef@reconstructed[-29:]-logc[t]
        history=errors[max(0,t-119):t+1]
        if len(history)!=120 or not np.all(np.isfinite(history)):continue
        med=np.median(history);mad=1.4826*np.median(np.abs(history-med));scale[t]=mad
        if mad>0:
            up[t]=prediction[t]>m.COST and prediction[t]/mad>1
            down[t]=prediction[t]<-m.COST and prediction[t]/mad<-1
    return m._first(up),m._first(down),prediction,dict(error_scale=scale,matured_errors=errors)


class SSAInferenceSpeedTests(unittest.TestCase):
    def test_exact_predictions_audit_arrays_and_training_snapshots(self):
        rng=np.random.default_rng(31);logc=7.+np.cumsum(rng.normal(0,.001,3000))
        ct=(np.arange(len(logc))+1)*60000
        records=[];old_records=[]
        with threadpool_limits(limits=1):
            with patch.object(m,'record_fit',side_effect=lambda *args:old_records.append(args)):
                before=legacy_ssa(logc,ct)
            with patch.object(m,'record_fit',side_effect=lambda *args:records.append(args)):
                after=m._ssa(logc,ct)
        for old,new in zip(before[:3],after[:3]):np.testing.assert_array_equal(old,new)
        for key in before[3]:np.testing.assert_array_equal(before[3][key],after[3][key])
        self.assertEqual(len(old_records),len(records))
        for old,new in zip(old_records,records):
            self.assertEqual(old[:2],new[:2]);self.assertEqual(old[3],new[3])
            for key in old[2]:np.testing.assert_array_equal(old[2][key],new[2][key])

    def test_empty_short_and_constant_inputs_match_reference(self):
        with threadpool_limits(limits=1):
            for n in (0,20,30,119,120,271):
                x=np.full(n,7.);ct=(np.arange(n)+1)*60000
                before=legacy_ssa(x,ct);after=m._ssa(x,ct)
                for old,new in zip(before[:3],after[:3]):np.testing.assert_array_equal(old,new)
                for key in before[3]:np.testing.assert_array_equal(before[3][key],after[3][key])


class QuantileInferenceSpeedTests(unittest.TestCase):
    def assert_same_result(self, first, second):
        for a, b in zip(first[:3], second[:3]):
            np.testing.assert_array_equal(a, b)
        self.assertEqual(first[3], second[3])

    def run_recorded(self, function, args):
        records = []

        def record(number, at, model, settings):
            parameters = None if model is None else (
                model[0].mean_.copy(), model[0].scale_.copy(),
                tuple((item.coef_.copy(), item.intercept_) for item in model[1]))
            records.append((number, at, settings, parameters))

        with patch.object(m, 'record_fit', side_effect=record), threadpool_limits(limits=1):
            result = function(*args)
        return result, records

    def test_exact_predictions_signals_and_training_snapshots(self):
        for variant in ('normal', 'constant', 'invalid'):
            with self.subTest(variant=variant):
                x, r, ct = sample(1085)
                if variant == 'constant':
                    x[:, 2:] = 0.
                if variant == 'invalid':
                    x[390, 0] = np.nan
                    x[785, 1] = np.inf
                    r[501] = np.nan
                before, old_records = self.run_recorded(legacy_quantile, (x, r, ct))
                after, records = self.run_recorded(m._quantile, (x, r, ct))
                self.assert_same_result(before, after)
                self.assertEqual(len(records), len(old_records))
                for old, new in zip(old_records, records):
                    self.assertEqual(old[:3], new[:3])
                    if old[3] is None:
                        self.assertIsNone(new[3])
                    else:
                        for a, b in zip(old[3][:2], new[3][:2]):
                            np.testing.assert_array_equal(a, b)
                        for old_model, new_model in zip(old[3][2], new[3][2]):
                            np.testing.assert_array_equal(old_model[0], new_model[0])
                            self.assertEqual(old_model[1], new_model[1])

    def test_exact_prefix_when_future_data_appended(self):
        x, r, ct = sample(900, seed=29)
        with threadpool_limits(limits=1):
            full = m._quantile(x, r, ct)
            prefix = m._quantile(x[:487], r[:487], ct[:487])
        for a, b in zip(full[:3], prefix[:3]):
            np.testing.assert_array_equal(a[:487], b)


if __name__ == '__main__':
    unittest.main()
