"""Fifth-round frozen, causal estimators. All labels are log close returns.

Optimizer choices below are fixed implementation parameters, chosen before any
performance selection. Predictions are never retroactively replaced after a fit.
"""
from __future__ import annotations
import warnings
from contextlib import contextmanager
import numpy as np
import pandas as pd
from numba import njit
from scipy.special import gammaln, logsumexp
from scipy.stats import genpareto
from fifth_model_audit import record_fit, record_predictions, set_timeframe

COST = 0.0001
VERSION = 'fifth-learned-v2'
NUMBERS = {17, 22, 27, 35, 39, 40, 41, 42, 98, 99, 101, 102}


def _lag(x, n=1):
    result = np.full_like(x, np.nan, dtype=float)
    if len(x) > n:
        result[n:] = x[:-n]
    return result


def _roll(x, n, kind='mean'):
    rolling = pd.Series(x).rolling(n, min_periods=n)
    return (rolling.std(ddof=0) if kind == 'std' else getattr(rolling, kind)()).to_numpy()


def _first(x):
    return x & ~np.r_[False, x[:-1]]


def _features(c, h, l, v, atr):
    logc = np.log(c); r = logc - _lag(logc)
    clv = np.divide(2*c-h-l, h-l, out=np.full(len(c), np.nan), where=h>l)
    return np.column_stack((r, logc-_lag(logc,3), logc-_lag(logc,10), atr/c,
                            clv, np.log(v/_lag(_roll(v,20)))))


def _settings(number, **kw):
    return dict(number=number, version=VERSION, seed=42, cost=COST,
                labels='log(C[t+h]/C[t]); only matured anchors', **kw)


@njit(cache=True, nogil=True)
def _ssa_diagonal_mean(matrix, counts):
    diagonal=np.zeros(len(counts))
    # Retain the original row-by-row accumulation order for exact predictions.
    for row in range(matrix.shape[0]):
        for col in range(matrix.shape[1]):
            diagonal[row+col]+=matrix[row,col]
    return diagonal/counts


def _ssa(logc, ct):
    n=len(logc); prediction=np.full(n,np.nan); up=np.zeros(n,bool); down=up.copy()
    errors=np.full(n,np.nan); scale=np.full(n,np.nan); projection=None; coef=None
    counts=np.convolve(np.ones(30),np.ones(91))
    windows=np.lib.stride_tricks.sliding_window_view(logc,30) if n>=30 else None
    for t in range(119,n):
        if t and np.isfinite(prediction[t-1]):
            # Store at availability time, so appending a bar cannot rewrite the
            # archived information that was known at the previous close.
            errors[t]=logc[t] - (logc[t-1]+prediction[t-1])
        matrix=windows[t-119:t-28].T
        if (t-119)%5==0:
            u, _, _=np.linalg.svd(matrix,full_matrices=False); u=u[:,:2]
            last=u[-1]; denominator=1-float(last@last)
            coef=None if denominator<=1e-6 else u[:-1]@last/denominator
            projection=u@u.T
            record_fit(17,ct[t],dict(projection=projection,coefficients=coef),
                       _settings(17,window=120,embedding=30,rank=2,refit=5,train_end=int(ct[t]),
                                 between_fits='frozen projection reconstructs latest window'))
        if coef is None: continue
        reconstructed=projection@matrix
        reconstructed=_ssa_diagonal_mean(reconstructed,counts)
        prediction[t]=coef@reconstructed[-29:]-logc[t]
        history=errors[max(0,t-119):t+1]
        if len(history)!=120 or not np.all(np.isfinite(history)):continue
        med=np.median(history); mad=1.4826*np.median(np.abs(history-med)); scale[t]=mad
        if mad>0:
            up[t]=prediction[t]>COST and prediction[t]/mad>1
            down[t]=prediction[t]<-COST and prediction[t]/mad<-1
    return _first(up),_first(down),prediction,dict(error_scale=scale,matured_errors=errors)


def _bocpd(r,ct):
    n=len(r); up=np.zeros(n,bool); down=up.copy(); prob=np.full(n,np.nan)
    trunc=np.full(n,np.nan); frozen=np.full(n,np.nan)
    previous_std=_lag(_roll(r,240,'std')); drift=_roll(r,3)
    posterior=np.array([1.]); mu=np.array([0.]); k=np.array([1.]); a=np.array([2.]); b=np.array([1.])
    scale=np.nan; last_alarm=-1000; hazard=1/120; observations=0
    for t in range(241,n):
        if not np.isfinite(scale) or scale<=0:
            scale=previous_std[t]
            if not np.isfinite(scale) or scale<=0:continue
            record_fit(27,ct[t],dict(scale=scale,prior=(0.,1.,2.,1.)),
                       _settings(27,hazard=hazard,max_run_lengths=500,scale_window=240))
        x=r[t]/scale; frozen[t]=scale; observations+=1
        df=2*a; var=b*(k+1)/(a*k)
        lp=gammaln((df+1)/2)-gammaln(df/2)-.5*np.log(df*np.pi*var) - ((df+1)/2)*np.log1p((x-mu)**2/(df*var))
        joint=np.log(np.maximum(posterior,1e-300))+lp
        logp=np.r_[logsumexp(joint)+np.log(hazard),joint+np.log1p(-hazard)]
        nextp=np.exp(logp-logsumexp(logp)); lost=float(nextp[500:].sum()); trunc[t]=lost
        nextp=nextp[:500]; posterior=nextp/nextp.sum(); prob[t]=posterior[:4].sum()
        if lost>.01:
            record_fit(27,ct[t],dict(discarded_mass=lost),
                       _settings(27,event='truncation error; this bar disabled',max_run_lengths=500))
        newk=k+1; newmu=(k*mu+x)/newk; newa=a+.5; newb=b+k*(x-mu)**2/(2*newk)
        mu=np.r_[0.,newmu][:500]; k=np.r_[1.,newk][:500]; a=np.r_[2.,newa][:500]; b=np.r_[1.,newb][:500]
        # With fewer than four observations P(r<=3)=1 by construction: that
        # initial prior mass is not evidence of a newly detected change.
        alarm=observations>3 and lost<=.01 and prob[t]>.6 and t-last_alarm>=10
        if alarm:
            up[t]=drift[t]>.3*previous_std[t]; down[t]=drift[t]<-.3*previous_std[t]
            if up[t] or down[t]:
                last_alarm=t
                # A detected new regime starts a fresh filter in the new unit.
                record_fit(27,ct[t],dict(probabilities=posterior,mu=mu,kappa=k,alpha=a,beta=b,scale=scale),
                           _settings(27,event='alarm; next bar begins new frozen-scale context'))
                posterior=np.array([1.]);mu=np.array([0.]);k=np.array([1.]);a=np.array([2.]);b=np.array([1.]);scale=np.nan;observations=0
    return up,down,prob,dict(discarded_mass=trunc,frozen_scale=frozen)


def _evt(o,c,r,ct):
    n=len(c);up=np.zeros(n,bool);down=up.copy();scores=np.full((n,2),np.nan)
    z=r/_lag(_roll(r,240,'std')); days=(ct-1)//86400000
    models=[None,None];context=None;last_day=None
    for t in range(10321,n):
        if days[t]!=last_day:
            last_day=days[t];window=z[t-10080:t]
            models=[None,None]
            if np.all(np.isfinite(window)):
                for side in range(2):
                    values=window if side else -window; threshold=np.quantile(values,.95)
                    tail=values[values>threshold]-threshold
                    if len(tail)<100:continue
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        shape,loc,scale=genpareto.fit(tail,floc=0)
                    if np.isfinite(shape) and np.isfinite(scale) and scale>0:
                        models[side]=(threshold,shape,scale)
            record_fit(35,ct[t],models,_settings(35,training=10080,scale_window=240,tail_threshold=.95,
                       tail_probability=.01,fit='scipy GPD MLE, location fixed 0',train_end=int(ct[t-1])))
        if context is not None:
            sign,at,opening,closing=context
            if t>at+5: context=None
            elif t>at:
                level=closing+(opening-closing)/3
                if sign*(c[t]-c[t-1])>0 and sign*(c[t]-level)>=0:
                    up[t]=sign>0;down[t]=sign<0;context=None
        for side,model in enumerate(models):
            if model is None:continue
            threshold,shape,scale=model;value=z[t] if side else -z[t]
            scores[t,side]=genpareto.sf(value-threshold,shape,loc=0,scale=scale) if value>threshold else 1.
            if scores[t,side]<.01:
                context=(-1 if side else 1,t,o[t],c[t])
    return up,down,scores,{}


def _kalman(logc,ct):
    n=len(logc);mean=np.full(n,np.nan);sd=mean.copy();up=np.zeros(n,bool);down=up.copy()
    f=np.array([[1.,1.],[0.,1.]]); state=None;p=None;rvar=None;q=None
    for t in range(1441,n):
        if (t-1441)%1440==0:
            rvar=float(np.var(np.diff(logc[t-1440:t]),ddof=0))
            if rvar<=0:continue
            q=np.diag([.01*rvar,.001*rvar])
            if state is None:state=np.array([logc[t-1],0.]);p=np.eye(2)*rvar
            record_fit(39,ct[t],dict(R=rvar,Q=q,state=state,P=p),
                       _settings(39,variance_window=1440,variance='past log return variance',refresh=1440,train_end=int(ct[t-1])))
        if state is None or rvar is None or rvar<=0:continue
        state=f@state; p=f@p@f.T+q
        gain=p[:,0]/(p[0,0]+rvar);state=state+gain*(logc[t]-state[0])
        p=p-np.outer(gain,p[0,:]);p=(p+p.T)/2
        mean[t]=state[1];sd[t]=np.sqrt(max(0,p[1,1]))
        up[t]=mean[t]-1.645*sd[t]>COST/10;down[t]=mean[t]+1.645*sd[t]<-COST/10
    return _first(up),_first(down),mean,dict(posterior_sd=sd)


def _knn(logc,ct):
    n=len(logc);pred=np.full(n,np.nan);up=np.zeros(n,bool);down=up.copy()
    sigma=_lag(_roll(logc-_lag(logc),120,'std'))
    paths=np.full((n,20),np.nan)
    for t in range(139,n):
        if sigma[t]>0:paths[t]=(logc[t-19:t+1]-logc[t-19])/sigma[t]
    for t in range(10239,n):
        # Nonoverlapping labels; candidate path plus matured label ends before current path begins.
        anchors=np.arange(t-10080,t-29,10)
        ok=np.isfinite(paths[anchors]).all(axis=1);anchors=anchors[ok]
        if len(anchors)<30 or not np.isfinite(paths[t]).all():continue
        dist=np.sqrt(np.square(paths[anchors]-paths[t]).sum(axis=1))
        nearest=np.argsort(dist,kind='stable')[:30]
        if np.mean(dist[nearest])>=np.median(dist):continue
        actual=logc[anchors[nearest]+10]-logc[anchors[nearest]]
        pred[t]=np.median(actual)
        up[t]=pred[t]>COST+.0001 and np.mean(actual>0)>.6
        down[t]=pred[t]<-(COST+.0001) and np.mean(actual<0)>.6
        if (t-10239)%1440==0:
            record_fit(42,ct[t],dict(candidate_anchor_times=ct[anchors]),
                       _settings(42,path=20,volatility_window=120,neighbors=30,horizon=10,
                                 training=10080,anchor_spacing=10,distance_gate='mean nearest distance below median candidate distance'))
    return _first(up),_first(down),pred,{}


def _quantile(x,r,ct):
    from sklearn.linear_model import QuantileRegressor
    from sklearn.preprocessing import StandardScaler
    n=len(r);bounds=np.full((n,2),np.nan);model=None;up=np.zeros(n,bool);down=up.copy()
    # The feature/response pair at anchor j is X[j] -> r[j+1].
    for t in range(262,n):
        if (t-262)%60==0:
            indices=np.arange(t-241,t-1)
            if not np.isfinite(x[indices]).all() or not np.isfinite(r[indices+1]).all():
                model=None
                record_fit(22,ct[t-1],None,_settings(22,event='invalid training features; this fit disabled'))
                continue
            scaler=StandardScaler().fit(x[indices]);xx=scaler.transform(x[indices]);models=[]
            for q in (.1,.9):
                m=QuantileRegressor(quantile=q,alpha=.01,solver='highs').fit(xx,r[indices+1]);models.append(m)
            model=(scaler,models)
            record_fit(22,ct[t-1],model,_settings(22,training=240,refit=60,alpha=.01,
                       quantiles=[.1,.9],feature_order=['r','lag_r','r5','ATR/C'],train_end=int(ct[t-1]),
                       forecast='frozen before observing current return'))
        if model is None or not np.isfinite(x[t-1]).all():continue
        # Dense float64 features were checked above; use the fitted estimators'
        # same row-wise operations without repeating sklearn's input validation.
        # Keeping a (1, 4) row also preserves the original dot-product order.
        scaler,models=model;xx=x[t-1:t].copy();xx-=scaler.mean_;xx/=scaler.scale_
        bounds[t]=[(xx@m.coef_+m.intercept_)[0] for m in models]
        if bounds[t,0]<=bounds[t,1]:
            up[t]=r[t]>bounds[t,1];down[t]=r[t]<bounds[t,0]
    return _first(up),_first(down),bounds,{}


def _hmm_converged(candidate):
    # hmmlearn's flag also becomes true when n_iter is exhausted.  Exhaustion
    # with a large likelihood increment is not a converged fit.
    history=np.asarray(candidate.monitor_.history,dtype=float)
    return (len(history)>=2 and np.isfinite(history[-2:]).all()
            and 0<=history[-1]-history[-2]<candidate.tol)


def _hmm_filter(prior,value,means,variances):
    with np.errstate(divide='ignore'):
        logp=np.log(prior)-.5*(np.log(2*np.pi*variances)+(value-means)**2/variances)
    return np.exp(logp-logsumexp(logp))


def _hmm(r,ct):
    from hmmlearn.hmm import GaussianHMM
    n=len(r);prob=np.full((n,2),np.nan);up=np.zeros(n,bool);down=up.copy()
    model=None;filtered=None;last_day=None;means=None;variances=None;previous=None
    days=(ct-1)//86400000
    for t in range(10081,n):
        if last_day!=days[t]:
            last_day=days[t];data=r[t-10080:t]*10000
            if not np.isfinite(data).all() or np.var(data)<=0:
                model=None
                record_fit(40,ct[t],None,_settings(40,event='invalid or constant training returns'))
                continue
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                candidate=GaussianHMM(2,covariance_type='diag',n_iter=200,tol=1e-4,random_state=42,min_covar=1e-6).fit(data[:,None])
            order=np.argsort(candidate.means_[:,0]);means=candidate.means_[order,0];variances=candidate.covars_[order,0,0]
            model=candidate if _hmm_converged(candidate) and np.isfinite(means).all() and np.isfinite(variances).all() and np.all(variances>0) else None
            filtered=candidate.startprob_[order];trans=candidate.transmat_[order][:,order]
            if model is not None:
                for index,value in enumerate(data):
                    filtered=_hmm_filter(filtered if index==0 else filtered@trans,value,means,variances)
            previous=filtered.copy() if model is not None else None
            record_fit(40,ct[t],dict(model=model,sorted_order=order,filtered=filtered),
                       _settings(40,training=10080,refit='UTC day',n_iter=200,tolerance=1e-4,
                                 scale=10000,train_end=int(ct[t-1]),probabilities='forward filtering only',
                                 converged=bool(model is not None),likelihood_history=list(candidate.monitor_.history)))
        if model is None:continue
        filtered=_hmm_filter(filtered@trans,r[t]*10000,means,variances);prob[t]=filtered
        if previous is not None:
            up[t]=previous[1]<.6 and filtered[1]>.8 and means[1]/10000>COST/10
            down[t]=previous[0]<.6 and filtered[0]>.8 and means[0]/10000<-COST/10
        previous=filtered.copy()
    return up,down,prob,{}


_parallel_gp_options = None


@contextmanager
def parallel_gp_context(options):
    """Configure GP work only in the precompute parent; daemon workers stay serial."""
    global _parallel_gp_options
    previous = _parallel_gp_options
    _parallel_gp_options = options
    try:
        yield
    finally:
        _parallel_gp_options = previous


def _supervised(number,x,logc,ct):
    if number != 98 or _parallel_gp_options is None:
        return _supervised_serial(number,x,logc,ct)
    from pathlib import Path
    import json
    import shutil
    from gp_parallel import parallel_gp
    options = dict(_parallel_gp_options)
    audit_dir = Path(options.pop('audit_dir'))
    manifests = []
    result = parallel_gp(x,logc,ct,source_dir=Path(__file__).resolve().parent,
                         manifest_callback=manifests.append,**options)
    # Each valid segment appends the untouched, time-ordered original snapshots.
    # A method publishes these files only after every segment has succeeded.
    manifest = json.loads(manifests[-1].read_text(encoding='utf-8'))
    audit_dir.mkdir(parents=True,exist_ok=True)
    for block in manifest['blocks']:
        source = Path(block['audit_archive'])
        with source.open('rb') as reader, (audit_dir/source.name).open('ab') as writer:
            shutil.copyfileobj(reader,writer,1024*1024)
        settings = Path(block['audit_settings'])
        if not (audit_dir/settings.name).exists():
            shutil.copyfile(settings,audit_dir/settings.name)
    return result


def _supervised_serial(number,x,logc,ct):
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, WhiteKernel
    n=len(logc);horizon=5 if number==98 else 10;window=1000 if number==98 else 10080
    pred=np.full((n,3),np.nan);up=np.zeros(n,bool);down=up.copy();fitted=None;last_fit=-100000;last_day=None
    days=(ct-1)//86400000;mean_labels=np.full(3,np.nan)
    for t in range(window+horizon+20,n):
        fit_now=(t-last_fit>=60) if number==98 else days[t]!=last_day
        if fit_now:
            last_fit=t;last_day=days[t];end=t-horizon+1;idx=np.arange(end-window,end)
            xx=x[idx,:5] if number==98 else x[idx]
            yy=logc[idx+horizon]-logc[idx]
            if not np.isfinite(xx).all() or not np.isfinite(yy).all():
                fitted=None
                record_fit(number,ct[t],None,_settings(number,event='invalid training features; this fit disabled'))
                continue
            scaler=StandardScaler().fit(xx);xx=scaler.transform(xx)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                if number==98:
                    # Target scale and kernel optimization are learned from this training window only.
                    estimator=GaussianProcessRegressor(kernel=RBF(1.,(1e-2,1e2))+WhiteKernel(.1,(1e-6,1e1)),
                        normalize_y=True,alpha=1e-10,n_restarts_optimizer=0,random_state=42)
                    estimator.fit(xx,yy);labels=None
                else:
                    labels=np.where(yy>COST,2,np.where(yy<-COST,0,1))
                    counts=np.bincount(labels,minlength=3)
                    if np.any(counts<10):
                        fitted=None
                        record_fit(number,ct[t],None,_settings(number,event='insufficient class samples',class_counts=counts.tolist()))
                        continue
                    estimator=(LogisticRegression(C=1.,max_iter=1000,tol=1e-6,random_state=42) if number==41 else
                               HistGradientBoostingClassifier(max_depth=3,max_iter=100,learning_rate=.05,
                                                              early_stopping=False,random_state=42))
                    estimator.fit(xx,labels);mean_labels=np.array([np.mean(yy[labels==i]) for i in range(3)])
            failed=any('failed to converge' in str(w.message).lower() or 'total no. of iterations' in str(w.message).lower() for w in caught)
            fitted=None if failed else (scaler,estimator)
            record_fit(number,ct[t],dict(fitted=fitted,class_mean_returns=mean_labels),
                       _settings(number,training=window,horizon=horizon,train_last_anchor=int(ct[end-1]),
                                 label_end=int(ct[t]),refit=60 if number==98 else 'UTC day',
                                 features=['r1','r3','r10','ATR/C','CLV']+([] if number==98 else ['log relative volume']),
                                 convergence_messages=[str(w.message) for w in caught]))
        if fitted is None:continue
        scaler,estimator=fitted;current=x[t:t+1,:5] if number==98 else x[t:t+1]
        if not np.isfinite(current).all():continue
        transformed=scaler.transform(current)
        if number==98:
            mean,std=estimator.predict(transformed,return_std=True);pred[t]=[mean[0],std[0],np.nan]
            up[t]=mean[0]-1.645*std[0]>COST;down[t]=mean[0]+1.645*std[0]<-COST
        else:
            probabilities=estimator.predict_proba(transformed)[0];pred[t]=probabilities
            cutoff=.6 if number==41 else .65;advantage=.2 if number==41 else .25
            up[t]=probabilities[2]>cutoff and probabilities[2]-probabilities[0]>advantage
            down[t]=probabilities[0]>cutoff and probabilities[0]-probabilities[2]>advantage
    return _first(up),_first(down),pred,{}


def _isolation(x,logc,c,atr,ct):
    from iso_fast import isolation
    return isolation(x,logc,c,atr,ct)


def _isolation_serial(x,logc,c,atr,ct):
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    n=len(c);score=np.full(n,np.nan);up=np.zeros(n,bool);down=up.copy();fitted=None;context=None;last_day=None
    waiting_normal=False;model_at=np.full(n,-1,dtype=np.int64);fitted_at=-1
    days=(ct-1)//86400000
    for t in range(10100,n):
        # During an anomaly the model remains frozen through its recovery/timeout.
        if days[t]!=last_day and context is None and not waiting_normal:
            last_day=days[t];train=x[t-10080:t]
            if not np.isfinite(train).all():
                fitted=None
                record_fit(102,ct[t],None,_settings(102,event='invalid training features; this fit disabled'))
                continue
            scaler=StandardScaler().fit(train)
            estimator=IsolationForest(n_estimators=100,contamination=.02,random_state=42,n_jobs=1).fit(scaler.transform(train))
            fitted=(scaler,estimator);fitted_at=t
            record_fit(102,ct[t],fitted,_settings(102,training=10080,trees=100,contamination=.02,
                       train_end=int(ct[t-1]),features=['r','TR/ATR','logV','logtrades','Delta/V']))
        if fitted is None or not np.isfinite(x[t]).all():
            context=None;waiting_normal=False
            continue
        model_at[t]=fitted_at
        scaler,estimator=fitted;score[t]=estimator.decision_function(scaler.transform(x[t:t+1]))[0]
        if waiting_normal:
            # An expired anomaly cannot rearm until this same frozen model
            # has observed one normal bar; a refit is not a recovery event.
            if score[t]>=0:waiting_normal=False
            continue
        if score[t]<0:
            if context is None:context=(t,logc[t-1],atr[t]/c[t])
            elif t-context[0]>=10:context=None;waiting_normal=True
        elif context is not None:
            at,start,threshold=context;move=logc[t-1]-start
            if t-at<=10:
                up[t]=move<-threshold and c[t]>c[t-1];down[t]=move>threshold and c[t]<c[t-1]
            context=None
    return up,down,score,dict(model_fit_index=model_at)


def _experts(x,logc,atr,c,ct):
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from fifth_stat_models import ar_forecasts,stat_model_signals
    n=len(c);pred=np.full(n,np.nan);issued=np.full((n,3),np.nan);variance=np.full(n,np.nan)
    weights_log=np.full((n,3),np.nan);regime=np.full(n,-1,int);up=np.zeros(n,bool);down=up.copy()
    log_weights=np.full((2,3),-np.log(3.));last_day=None;logit=None;ratio=atr/c
    boundary=_lag(pd.Series(ratio).rolling(10080,min_periods=10080).median().to_numpy())
    # Reuse the actual AR/OU estimators: their 60/30-bar calendars, admissible
    # OU half-life and rank checks must not drift from methods 37 and 38.
    ar_prediction,ar_cutoff,ar_models=ar_forecasts(c,horizon=10)
    ou_audit={};stat_model_signals(38,c,atr,audit=ou_audit)
    ou_prediction=ou_audit['prediction'];ou_cutoff=ou_audit['training_cutoff']
    refits={}
    for expert,models in (('AR',ar_models),('OU',ou_audit['models'])):
        for snapshot in models:refits.setdefault(snapshot['at'],[]).append((expert,snapshot))
    days=(ct-1)//86400000
    logit_cutoff=np.full(n,-1,dtype=np.int64);fitted_at=-1
    for t in range(720,n):
        for expert,snapshot in refits.get(t,()):
            record_fit(101,ct[t],dict(expert=expert,parameters=snapshot),
                       _settings(101,expert=expert,horizon=10,refit=60 if expert=='AR' else 30,
                                 train_last_anchor=int(ct[snapshot['train_last_anchor']]),
                                 label_end=int(ct[snapshot['label_end']])))
        if t<10110:continue
        mature=t-10
        if regime[mature]>=0 and np.isfinite(issued[mature]).all() and variance[mature]>0:
            actual=logc[t]-logc[mature];loss=np.clip((issued[mature]-actual)**2/variance[mature],0,10)
            g=regime[mature];log_weights[g]-=.1*loss
            log_weights[g]-=logsumexp(log_weights[g])
        if days[t]!=last_day:
            last_day=days[t];idx=np.arange(t-10-10080+1,t-10+1);yy=logc[idx+10]-logc[idx]
            labels=np.where(yy>COST,2,np.where(yy<-COST,0,1));counts=np.bincount(labels,minlength=3)
            logit=None;var=float(np.var(yy,ddof=0))
            if np.all(counts>=10) and var>0 and np.isfinite(x[idx]).all():
                scaler=StandardScaler().fit(x[idx])
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    classifier=LogisticRegression(C=1.,max_iter=1000,tol=1e-6,random_state=42).fit(scaler.transform(x[idx]),labels)
                if not any('converge' in str(w.message).lower() for w in caught):
                    means=np.array([yy[labels==j].mean() for j in range(3)])
                    logit=(scaler,classifier,means,var);fitted_at=t
            record_fit(101,ct[t],dict(expert='Logit',model=logit,regime_weights=np.exp(log_weights)),
                       _settings(101,expert='Logit',training=10080,horizon=10,refit='UTC day',
                                 train_last_anchor=int(ct[t-10]),label_end=int(ct[t]),
                                 loss_clip=[0,10],learning_rate=.1,loss_variance='issued 10080-label training variance'))
        if (logit is None or not np.isfinite(boundary[t]) or not np.isfinite(x[t]).all()
                or not np.isfinite(ar_prediction[t]) or not np.isfinite(ou_prediction[t])):continue
        scaler,classifier,means,var=logit;logit_cutoff[t]=fitted_at
        expert=np.array([ar_prediction[t],ou_prediction[t],
                         classifier.predict_proba(scaler.transform(x[t:t+1]))[0]@means])
        g=int(ratio[t]>boundary[t]);regime[t]=g;issued[t]=expert;variance[t]=var
        weights_log[t]=np.exp(log_weights[g]);pred[t]=expert@weights_log[t]
        up[t]=pred[t]>COST+.0001 and np.sum(expert>0)>=2
        down[t]=pred[t]<-(COST+.0001) and np.sum(expert<0)>=2
    return _first(up),_first(down),pred,dict(expert_predictions=issued,issued_label_variance=variance,
                                          regime=regime,issued_weights=weights_log,
                                          ar_training_cutoff=ar_cutoff,ou_training_cutoff=ou_cutoff,
                                          logit_training_cutoff=logit_cutoff)


def masks(number,o,h,l,c,v,ct,extras,timeframe='1m'):
    from fifth_batch import _atr
    from threadpoolctl import threadpool_limits
    set_timeframe(timeframe)
    o,h,l,c,v=[np.asarray(a,dtype=float) for a in (o,h,l,c,v)]
    ct=np.asarray(ct,dtype=np.int64);atr,tr=_atr(h,l,c);logc=np.log(c);r=logc-_lag(logc)
    with threadpool_limits(limits=1):
        if number==17:result=_ssa(logc,ct)
        elif number==27:result=_bocpd(r,ct)
        elif number==35:result=_evt(o,c,r,ct)
        elif number==39:result=_kalman(logc,ct)
        elif number==40:result=_hmm(r,ct)
        elif number==42:result=_knn(logc,ct)
        elif number==22:
            xx=np.column_stack((r,_lag(r),logc-_lag(logc,5),atr/c));result=_quantile(xx,r,ct)
        elif number in (41,98,99):result=_supervised(number,_features(c,h,l,v,atr),logc,ct)
        elif number==101:result=_experts(_features(c,h,l,v,atr),logc,atr,c,ct)
        elif number==102:
            delta=np.asarray(extras['delta_base'] if 'delta_base' in extras else 2*np.asarray(extras['taker_buy_base'])-v)
            xx=np.column_stack((r,tr/atr,np.log(v),np.log(extras['trades']),delta/v))
            result=_isolation(xx,logc,c,atr,ct)
        else:raise ValueError(f'Unsupported learned method: {number}')
    up,down,prediction,extra=result
    record_predictions(number,ct,prediction,long_signal=up,short_signal=down,**extra)
    return up,down
