import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from scipy.stats import t as student_t
from threadpoolctl import threadpool_limits

import fifth_learned_models as m


def market(n, seed=42):
    rng=np.random.default_rng(seed);x=np.empty(n);x[0]=7.5
    for i in range(1,n):x[i]=7.5+.985*(x[i-1]-7.5)+rng.normal(0,.0008)
    c=np.exp(x);o=np.r_[c[0],c[:-1]];h=np.maximum(o,c)*1.0005;l=np.minimum(o,c)*.9995
    v=rng.uniform(10,20,n);ct=(np.arange(n)+1)*60000
    return o,h,l,c,v,ct


class LearnedModelTests(unittest.TestCase):
    def test_population_scale_and_no_fabricated_clv(self):
        x=np.array([1.,2.,3.,4.]);self.assertAlmostEqual(m._roll(x,3,'std')[2],np.std(x[:3]))
        f=m._features(x,x,x,np.ones(4),np.ones(4))
        self.assertTrue(np.isnan(f[:,4]).all())

    def test_ssa_reconstruction_frozen_coefficients_and_prefix(self):
        *_,c,v,ct=market(270)
        logc=np.log(c);out=m._ssa(logc,ct)
        matrix=np.lib.stride_tricks.sliding_window_view(logc[:120],30).T
        u=np.linalg.svd(matrix,full_matrices=False)[0][:,:2];a=u[:-1]@u[-1]/(1-u[-1]@u[-1])
        for t in (119,120,123):
            latest=np.lib.stride_tricks.sliding_window_view(logc[t-119:t+1],30).T
            projected=u@u.T@latest;diagonal=np.zeros(120)
            for row in range(30):diagonal[row:row+91]+=projected[row]
            forecast=a@(diagonal/np.convolve(np.ones(30),np.ones(91)))[-29:]-logc[t]
            self.assertAlmostEqual(out[2][t],forecast,places=11)
        self.assertTrue(np.isnan(out[3]['error_scale'][238]))
        self.assertTrue(np.isfinite(out[3]['error_scale'][239]))
        short=m._ssa(logc[:255],ct[:255])
        for a,b in zip(out[:3],short[:3]):np.testing.assert_allclose(a[:255],b,equal_nan=True)
        for key in short[3]:np.testing.assert_allclose(out[3][key][:255],short[3][key],equal_nan=True)
        self.assertAlmostEqual(out[3]['matured_errors'][120],logc[120]-logc[119]-out[2][119])

    def test_bocpd_matches_student_t_recursion_and_records_truncation(self):
        rng=np.random.default_rng(9);r=rng.normal(0,.001,1200);ct=(np.arange(len(r))+1)*60000
        fits=[]
        with patch.object(m,'record_fit',side_effect=lambda *args:fits.append(args)):
            out=m._bocpd(r,ct)
        prior=np.array([1.]);mu=np.array([0.]);k=np.array([1.]);alpha=np.array([2.]);beta=np.array([1.])
        scale=np.std(r[1:241])
        for j in range(241,246):
            value=r[j]/scale;pdf=student_t.pdf(value,df=2*alpha,loc=mu,scale=np.sqrt(beta*(k+1)/(alpha*k)))
            joint=prior*pdf;prior=np.r_[joint.sum()/120,joint*119/120];prior/=prior.sum()
            self.assertAlmostEqual(out[2][j],prior[:4].sum(),places=11)
            nextk=k+1;nextmu=(k*mu+value)/nextk;nextbeta=beta+k*(value-mu)**2/(2*nextk)
            mu=np.r_[0.,nextmu];k=np.r_[1.,nextk];alpha=np.r_[2.,alpha+.5];beta=np.r_[1.,nextbeta]
        lost=out[3]['discarded_mass']>.01
        self.assertTrue(lost.any())
        self.assertFalse(np.any((out[0]|out[1])[241:244]))
        self.assertFalse(np.any((out[0]|out[1])[lost]))
        self.assertEqual(int(lost.sum()),sum(a[3].get('event','').startswith('truncation error') for a in fits))

    def test_kalman_first_filter_matches_state_equations(self):
        *_,c,v,ct=market(1460);x=np.log(c);out=m._kalman(x,ct);j=1441
        rv=np.var(np.diff(x[j-1440:j]));f=np.array([[1.,1.],[0.,1.]])
        p=f@(np.eye(2)*rv)@f.T+np.diag([.01*rv,.001*rv]);gain=p[:,0]/(p[0,0]+rv)
        state=np.array([x[j-1],0.])+gain*(x[j]-x[j-1]);p-=np.outer(gain,p[0,:])
        self.assertAlmostEqual(out[2][j],state[1],places=14)
        self.assertAlmostEqual(out[3]['posterior_sd'][j],np.sqrt(p[1,1]),places=14)
        short=m._kalman(x[:1450],ct[:1450])
        np.testing.assert_allclose(out[2][:1450],short[2],equal_nan=True)

    def test_quantile_threshold_predates_tested_return(self):
        rng=np.random.default_rng(6);x=rng.normal(size=(270,4));r=rng.normal(0,.001,270);ct=(np.arange(270)+1)*60000
        with threadpool_limits(limits=1):
            first=m._quantile(x,r,ct);changed=r.copy();changed[262:]+=1
            second=m._quantile(x,changed,ct)
        np.testing.assert_allclose(first[2],second[2],equal_nan=True)
        self.assertTrue(np.isfinite(first[2][262]).all())

    def test_hmm_does_not_accept_iteration_exhaustion_as_convergence(self):
        def model(history):return SimpleNamespace(tol=1e-4,monitor_=SimpleNamespace(history=history,converged=True))
        self.assertFalse(m._hmm_converged(model([100.,105.])))
        self.assertFalse(m._hmm_converged(model([100.,99.99999])))
        self.assertTrue(m._hmm_converged(model([100.,100.000001])))
        self.assertFalse(m._hmm_converged(model([np.nan,100.])))
        prior=np.array([.8,.2]);means=np.array([-1.,2.]);variances=np.array([.5,1.]);value=.2
        expected=prior*np.exp(-.5*(value-means)**2/variances)/np.sqrt(2*np.pi*variances);expected/=expected.sum()
        np.testing.assert_allclose(m._hmm_filter(prior,value,means,variances),expected)

    def test_experts_share_ar_ou_calendars_and_only_mature_losses(self):
        from fifth_stat_models import ar_forecasts,stat_model_signals
        o,h,l,c,v,ct=market(10210);atr=np.full(len(c),2.);x=m._features(c,h,l,v,atr)
        with threadpool_limits(limits=1):
            out=m._experts(x,np.log(c),atr,c,ct)
            short=m._experts(x[:10170],np.log(c[:10170]),atr[:10170],c[:10170],ct[:10170])
        ar,ar_cutoff,_=ar_forecasts(c,horizon=10);ou={};stat_model_signals(38,c,atr,audit=ou)
        issued=out[3]['expert_predictions'];valid=np.isfinite(issued).all(axis=1)
        self.assertGreater(valid.sum(),20)
        np.testing.assert_allclose(issued[valid,0],ar[valid]);np.testing.assert_allclose(issued[valid,1],ou['prediction'][valid])
        np.testing.assert_array_equal(out[3]['ar_training_cutoff'],ar_cutoff)
        np.testing.assert_array_equal(out[3]['ou_training_cutoff'],ou['training_cutoff'])
        first=np.flatnonzero(valid)[0]
        np.testing.assert_allclose(out[3]['issued_weights'][first],np.full(3,1/3))
        for t in np.flatnonzero(valid & (np.arange(len(c))<first+10)):
            np.testing.assert_allclose(out[3]['issued_weights'][t],np.full(3,1/3))
        for a,b in zip(out[:3],short[:3]):np.testing.assert_allclose(a[:10170],b,equal_nan=True)
        for key in short[3]:np.testing.assert_allclose(out[3][key][:10170],short[3][key],equal_nan=True)

    def test_isolation_same_model_recovery_and_timeout_rearm(self):
        class IdentityScaler:
            def fit(self,x):return self
            def transform(self,x):return x
        fits=[]
        class Detector:
            def __init__(self,**kwargs):pass
            def fit(self,x):fits.append(1);return self
            def decision_function(self,x):return x[:,0]
        n=10120;x=np.ones((n,5));x[10101:10103,0]=-1;c=np.full(n,100.);c[10101]=98;c[10102]=97;c[10103]=98
        ct=(np.arange(n)-10102)*60000+86400000
        with patch('sklearn.preprocessing.StandardScaler',IdentityScaler),patch('sklearn.ensemble.IsolationForest',Detector):
            out=m._isolation(x,np.log(c),c,np.ones(n),ct)
        self.assertTrue(out[0][10103]);self.assertEqual(out[3]['model_fit_index'][10103],10100)
        self.assertEqual(out[3]['model_fit_index'][10104],10104)
        x[10101:10115,0]=-1;c[10101:10115]=97;c[10115]=98
        with patch('sklearn.preprocessing.StandardScaler',IdentityScaler),patch('sklearn.ensemble.IsolationForest',Detector):
            out=m._isolation(x,np.log(c),c,np.ones(n),ct)
        self.assertFalse(out[0].any() or out[1].any())
        self.assertEqual(out[3]['model_fit_index'][10115],10100)
        self.assertEqual(out[3]['model_fit_index'][10116],10116)

    def test_delta_only_input_does_not_evaluate_missing_taker_key(self):
        o,h,l,c,v,ct=market(30);n=len(c);result=(np.zeros(n,bool),np.zeros(n,bool),np.zeros(n),{})
        with patch.object(m,'_isolation',return_value=result) as run:
            m.masks(102,o,h,l,c,v,ct,{'delta_base':np.ones(n),'trades':np.ones(n)})
        np.testing.assert_allclose(run.call_args.args[0][:,4],1/v)

    def test_classifier_invalid_training_features_disable_without_fake_zero(self):
        o,h,l,c,v,ct=market(10113);x=m._features(c,h,l,v,np.ones(len(c)))
        x[100,4]=np.nan
        for number in (41,99):
            out=m._supervised(number,x,np.log(c),ct)
            self.assertFalse(out[0].any() or out[1].any());self.assertTrue(np.isnan(out[2]).all())

    def test_all_twelve_real_estimators_are_causal_through_dispatch(self):
        sizes={17:270,22:280,27:800,35:10340,39:1470,40:10105,
               41:10135,42:10265,98:1030,99:10135,101:10145,102:10125}
        for number,n in sizes.items():
            with self.subTest(number=number):
                o,h,l,c,v,ct=market(n);extras={'delta_base':np.sin(np.arange(n))*v/2,'trades':v*10}
                capture=[]
                with patch.object(m,'record_predictions',side_effect=lambda *a,**kw:capture.append((a,kw))):
                    full=m.masks(number,o,h,l,c,v,ct,extras)
                    prefix=m.masks(number,o[:-5],h[:-5],l[:-5],c[:-5],v[:-5],ct[:-5],{k:a[:-5] for k,a in extras.items()})
                for a,b in zip(full,prefix):np.testing.assert_array_equal(a[:-5],b)
                np.testing.assert_allclose(capture[0][0][2][:-5],capture[1][0][2],equal_nan=True)
                for key,b in capture[1][1].items():
                    np.testing.assert_allclose(capture[0][1][key][:-5],b,equal_nan=True)


if __name__=='__main__':unittest.main()
