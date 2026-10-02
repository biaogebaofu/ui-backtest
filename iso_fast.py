"""Exact F5-102 state machine with scores batched only under its frozen model."""
import numpy as np
import fifth_learned_models as original


def isolation(x,logc,c,atr,ct):
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    n=len(c);score=np.full(n,np.nan);up=np.zeros(n,bool);down=up.copy();fitted=None;context=None;last_day=None
    waiting_normal=False;model_at=np.full(n,-1,dtype=np.int64);fitted_at=-1
    days=(ct-1)//86400000
    cached_start=0;cached_end=0;cached_scores=np.empty(0)
    for t in range(10100,n):
        # During an anomaly the model remains frozen through its recovery/timeout.
        if days[t]!=last_day and context is None and not waiting_normal:
            last_day=days[t];train=x[t-10080:t]
            cached_end=t
            if not np.isfinite(train).all():
                fitted=None
                original.record_fit(102,ct[t],None,original._settings(102,event='invalid training features; this fit disabled'))
                continue
            scaler=StandardScaler().fit(train)
            estimator=IsolationForest(n_estimators=100,contamination=.02,random_state=42,n_jobs=1).fit(scaler.transform(train))
            fitted=(scaler,estimator);fitted_at=t
            original.record_fit(102,ct[t],fitted,original._settings(102,training=10080,trees=100,contamination=.02,
                       train_end=int(ct[t-1]),features=['r','TR/ATR','logV','logtrades','Delta/V']))
        if fitted is None or not np.isfinite(x[t]).all():
            context=None;waiting_normal=False
            continue
        model_at[t]=fitted_at
        scaler,estimator=fitted
        if t>=cached_end:
            cached_start=t;cached_end=min(n,t+1440)
            current=x[cached_start:cached_end]
            valid=np.isfinite(current).all(axis=1)
            cached_scores=np.full(len(current),np.nan)
            cached_scores[valid]=estimator.decision_function(scaler.transform(current[valid]))
        score[t]=cached_scores[t-cached_start]
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
