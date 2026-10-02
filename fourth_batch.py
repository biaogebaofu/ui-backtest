"""第四批重建版：30个家族、120个参数化开仓规则（144—263）。

v1.44R 不声称恢复丢失的旧v1.44文件；以本模块为本次定义来源。
原有0—143完全不变。全部是已收盘1m数据上的 MACD方向 + 附加判据；
并非120个互相独立的交易优势。成交、账户、退出计算仍由原生worker执行。
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from numba import njit

# (family key, label, four parameter variants, long predicate, required capabilities)
FAMILIES = [
 ('kama','效率自适应均线',[(10,0),(10,.1),(20,0),(20,.1)],'KAMA(n,2,30)上升且C>KAMA+b×ATR14；空镜像',{'ohlcv'}),
 ('dema','双指数均线收盘穿越',[(10,),(20,),(40,),(80,)],'C从DEMA(n)=2EMA(C)-EMA(EMA(C))下方穿到上方；空镜像',{'ohlcv'}),
 ('hma','加权Hull趋势',[(9,),(16,),(25,),(36,)],'HMA(n)上升且C>HMA；HMA=WMA(2WMA(C,n//2)-WMA(C,n),floor(sqrt(n)))；空镜像',{'ohlcv'}),
 ('tema','三指数均线加速',[(10,),(20,),(40,),(60,)],'TEMA(n)=3E1-3E2+E3；C>TEMA且TEMA斜率>0、斜率继续增大；空镜像',{'ohlcv'}),
 ('trix','三重平滑动量过零',[(5,),(10,),(20,),(30,)],'EMA(EMA(EMA(C,n),n),n)的单根收益由非正转正；空镜像',{'ohlcv'}),
 ('reg_reentry','回归残差重新入轨',[(20,1),(20,2),(60,1),(60,2)],'当前收盘相对前n根线性回归外推值的残差Z，从<-z穿回>=-z；空镜像',{'ohlcv'}),
 ('reg_t','回归斜率显著性',[(20,1),(20,2),(60,1),(60,2)],'前n根log(C)线性回归斜率t统计>门槛且本根收盘上涨；空镜像',{'ohlcv'}),
 ('rank_reentry','收盘历史分位回收',[(20,.1),(20,.2),(60,.1),(60,.2)],'C相对前n根C的经验分位由<q回到>=q；空在1-q处镜像',{'ohlcv'}),
 ('stoch','随机指标极端交叉',[(9,20),(9,30),(14,20),(14,30)],'%K(n)上穿其3根均值D且前一根K<下界；空在100-下界镜像',{'ohlcv'}),
 ('cci','顺势通道极端回收',[(14,100),(14,150),(20,100),(20,150)],'CCI(n)从<-阈值回到>=-阈值；CCI=(典型价-SMA)/(0.015×均绝对偏差)；空镜像',{'ohlcv'}),
 ('dmi','方向运动交叉确认',[(14,15),(14,25),(28,15),(28,25)],'+DI(n)上穿-DI且ADX(n)>=阈值；使用alpha=1/n递推、足够预热；空镜像',{'ohlcv'}),
 ('aroon','高低点新近程度',[(14,50),(14,70),(25,50),(25,70)],'AroonUp-AroonDown>阈值且C>C前一根；同值取最近高低点；空镜像',{'ohlcv'}),
 ('ha_turn','平滑合成蜡烛翻向',[(3,1),(3,2),(5,1),(5,2)],'EMA平滑OHLC后计算Heikin-Ashi：先有反向，再连续k根同向；只用真实价格成交',{'ohlcv'}),
 ('pv_corr','量价相关顺向确认',[(20,0),(20,.3),(60,0),(60,.3)],'前n根收益与成交量变化的相关系数>=阈值，本根价格及成交量同时上涨；空要求价格下降量增加、相关<=-阈值',{'ohlcv'}),
 ('volume_absorb','高量低冲击回收',[(1.5,.25),(1.5,.5),(2,.25),(2,.5)],'V>=前20均量倍数且|C-Cprev|<=b×ATRprev，收盘在本根上半部、下影>=实体；空镜像',{'ohlcv'}),
 ('mfi','资金流指数极端回收',[(14,20),(14,30),(28,20),(28,30)],'MFI(n)从<阈值回到>=阈值；典型价涨跌划分正负价×量；空在100-阈值镜像',{'ohlcv'}),
 ('chaikin','累计收盘资金流动量',[(3,10),(3,20),(5,10),(5,20)],'ADL=累计CLV×V，EMA(ADL,快)-EMA(ADL,慢)由非正转正；空镜像',{'ohlcv'}),
 ('semivar','上下行半方差主导',[(20,1.5),(20,2),(60,1.5),(60,2)],'前n根正收益平方和>倍数×负收益平方和且本根上涨；空镜像',{'ohlcv'}),
 ('autocorr','负自相关反弹',[(20,.1),(20,.25),(60,.1),(60,.25)],'前n对相邻收益相关<-阈值、前一根跌且本根涨；空镜像',{'ohlcv'}),
 ('skew','收益偏态修复',[(20,.5),(20,1),(60,.5),(60,1)],'前n根收益偏度<-阈值且本根涨；空要求偏度>阈值且本根跌',{'ohlcv'}),
 ('park_release','高低价波动释放',[(10,1.2),(10,1.5),(20,1.2),(20,1.5)],'高低价平方对数波动n根均值，相对过去60根基准由<=倍数转为>倍数，本根实体同向',{'ohlcv'}),
 ('squeeze_break','压缩后通道突破',[(20,.5),(20,.75),(60,.5),(60,.75)],'前一根n根收盘标准差<过去120根均值×比例，本根收盘突破前n根高/低',{'ohlcv'}),
 ('daily_pivot','前日枢轴重新收复',[(0,1),(.1,1),(0,2),(.1,2)],'UTC前日P=(H+L+C)/3；先处在P不利侧，再连续k根收于P+b×ATRprev有利侧；不跨日延续确认',{'ohlcv'}),
 ('range_reclaim','价格区间极值回收',[(10,0),(10,.1),(20,.1),(60,.1)],'L<前n根最低-b×ATRprev且C>前n根最低；空为H>前n根最高+b×ATRprev且C<前n根最高',{'ohlcv'}),
 ('opening_range','UTC首段区间突破',[(15,0),(15,.1),(60,0),(60,.1)],'当日首n分钟全部收盘后，C上穿已冻结首段最高+b×ATRprev；空镜像；无完整开盘区间则不放行',{'ohlcv'}),
 ('impulse_pullback','冲量后的缩量回踩恢复',[(3,.5),(3,.8),(5,.5),(5,.8)],'t-2的前n根涨幅>ATR(t-2)，t-1回落且缩量<=前20均量比例，t收盘突破t-1最高；空镜像',{'ohlcv'}),
 ('scale_div','多尺度逆向动量恢复',[(3,20),(3,60),(5,20),(5,60)],'长窗价格涨、短窗前一根跌且本根短窗收益穿回正；空镜像',{'ohlcv'}),
 ('trade_intensity','成交强度与价格推进',[(20,1.2),(20,1.5),(60,1.2),(60,1.5)],'成交笔数/振幅比例 >= 其前n均值倍数，收盘在本根上四分位且实体为阳；空镜像；不推算盘口深度',{'ohlcv','trades'}),
 ('flow_accel','主动成交占比加速',[(3,.5),(3,1),(10,.5),(10,1)],'n根(2主动买量-量)/总量>0且相对前20根同指标的Z>阈值且较前一根增强；空镜像',{'ohlcv','delta_cvd'}),
 ('flow_residual','价格对主动流残差回收',[(20,1),(20,2),(60,1),(60,2)],'用前n根主动Delta/量回归收益；当前标准化残差由<-阈值回到>=-阈值且主动Delta>0；空镜像',{'ohlcv','delta_cvd'}),
]
FOURTH_ROUND_CODES=tuple(range(144,264))
FOURTH_SPECS={}
for family_i,(key,label,variants,formula,needs) in enumerate(FAMILIES):
    for j,args in enumerate(variants):
        code=144+family_i*4+j
        text='/'.join(f'{p:g}' for p in args)
        FOURTH_SPECS[code]={'code':code,'family':key,'family_no':family_i+1,'name':f'{label} [{text}]',
                           'params':tuple(args),'definition':formula,'requires':frozenset(needs)}
FOURTH_ENTRY_RULES={c:(v['name'],'fourth:'+v['family'],float(c)) for c,v in FOURTH_SPECS.items()}
KLINE_ONLY_FOURTH_CODES=tuple(c for c,v in FOURTH_SPECS.items() if v['requires']==frozenset({'ohlcv'}))
MICROSTRUCTURE_FOURTH_CODES=tuple(c for c in FOURTH_ROUND_CODES if c not in KLINE_ONLY_FOURTH_CODES)

def lag(x,k=1):
    a=np.asarray(x,dtype=float); out=np.full(len(a),np.nan)
    if k and k<len(a):out[k:]=a[:-k]
    return out

def roll(x,n,how='mean',prev=False):
    s=pd.Series(np.asarray(x,dtype=float)); s=s.shift(1) if prev else s
    r=s.rolling(int(n),min_periods=int(n))
    if how=='std':return r.std(ddof=0).to_numpy()
    if how=='skew':return r.skew().to_numpy()
    return getattr(r,how)().to_numpy()

def div(a,b):
    a,b=np.broadcast_arrays(np.asarray(a,dtype=float),np.asarray(b,dtype=float))
    out=np.full(a.shape,np.nan); np.divide(a,b,out=out,where=np.isfinite(b)&(np.abs(b)>1e-20))
    return out

def ewm(x,n):return pd.Series(x).ewm(span=int(n),adjust=False,min_periods=int(n)).mean().to_numpy()
def wilder(x,n):return pd.Series(x).ewm(alpha=1/float(n),adjust=False,min_periods=int(n)).mean().to_numpy()
def cross_up(a,b):return (a>b)&(lag(a)<=lag(b))
def cross_down(a,b):return (a<b)&(lag(a)>=lag(b))
def cross_level(a,b,up=True):return ((a>=b)&(lag(a)<b)) if up else ((a<=b)&(lag(a)>b))
def correlation(a,b,n,prev=True):
    return div(roll(a*b,n,prev=prev)-roll(a,n,prev=prev)*roll(b,n,prev=prev),roll(a,n,'std',prev)*roll(b,n,'std',prev))
def zprev(x,n):return div(x-roll(x,n,prev=True),roll(x,n,'std',prev=True))
def all_run(x,k):return roll(np.asarray(x,dtype=float),int(k),'sum')==int(k)

@njit(cache=True)
def _kama(c,n):
    a=np.full(len(c),np.nan)
    if len(c)<=n:return a
    a[n-1]=np.mean(c[:n]); fastest=2/3; slowest=2/31
    for t in range(n,len(c)):
        den=0.
        for j in range(t-n+1,t+1):den+=abs(c[j]-c[j-1])
        er=abs(c[t]-c[t-n])/den if den>0 else 0.
        sc=(er*(fastest-slowest)+slowest)**2
        a[t]=a[t-1]+sc*(c[t]-a[t-1])
    return a

@njit(cache=True)
def _reg(c,n):
    # Fit only [t-n,t): prediction at t; explicit SSE and slope t-stat.
    pred=np.full(len(c),np.nan); scale=pred.copy(); ts=pred.copy()
    sx=n*(n-1)/2; sxx=n*(n-1)*(2*n-1)/6; xx=sxx-sx*sx/n
    for t in range(n,len(c)):
        sy=0.;sxy=0.; syy=0.
        for j in range(n):
            v=c[t-n+j];sy+=v;sxy+=j*v;syy+=v*v
        b=(sxy-sx*sy/n)/xx; a=(sy-b*sx)/n
        residual=0.
        for j in range(n):residual+=(c[t-n+j]-a-b*j)**2
        se=(residual/(n-2))**.5
        pred[t]=a+b*n; scale[t]=se
        if se>1e-12:ts[t]=b*(xx**.5)/se
    return pred,scale,ts

@njit(cache=True)
def _rank(c,n):
    z=np.full(len(c),np.nan)
    for t in range(n,len(c)):
        cnt=0.;
        for j in range(t-n,t):cnt+=(c[j]<c[t])+.5*(c[j]==c[t])
        z[t]=cnt/n
    return z

@njit(cache=True)
def _mad(x,n):
    a=np.full(len(x),np.nan)
    for t in range(n-1,len(x)):
        mu=np.mean(x[t-n+1:t+1]);total=0.
        for j in range(t-n+1,t+1):total+=abs(x[j]-mu)
        a[t]=total/n
    return a

@njit(cache=True)
def _aroon(h,l,n):
    out=np.full(len(h),np.nan)
    for t in range(n-1,len(h)):
        ih=t-n+1;il=ih
        for j in range(t-n+2,t+1):
            if h[j]>=h[ih]:ih=j
            if l[j]<=l[il]:il=j
        out[t]=100*(ih-il)/(n-1)
    return out

@njit(cache=True)
def _ha_open(o,c):
    out=np.full(len(c),np.nan)
    for t in range(len(c)):
        if not np.isfinite(c[t]) or not np.isfinite(o[t]):continue
        out[t]=(o[t]+c[t])/2 if t==0 or not np.isfinite(out[t-1]) else (out[t-1]+c[t-1])/2
    return out

def wma(x,n):
    n=max(1,int(n)); weights=np.arange(1,n+1,dtype=float)
    # numpy convolution is causal; prepend missing values for warmup.
    if len(x)<n:return np.full(len(x),np.nan)
    return np.r_[np.full(n-1,np.nan),np.convolve(x,weights[::-1]/weights.sum(),mode='valid')]

def extra(extras,key,n):
    a=np.asarray((extras or {}).get(key,np.full(n,np.nan)),dtype=float)
    return a if len(a)==n else np.full(n,np.nan)

def fourth_masks(code,direction,values,o,h,l,c,v,close_times,extras,timeframe="1m"):
    spec=FOURTH_SPECS[code]; kind=spec['family']; p=spec['params']; nrows=len(c)
    o,h,l,c,v=[np.asarray(x,dtype=float) for x in (o,h,l,c,v)]
    prev=lag(c); change=c-prev; ret=div(change,prev); span=h-l
    from extended_signals import atr14
    atr=atr14(h,l,c); ap=lag(atr)
    up=np.zeros(nrows,dtype=bool); dn=up.copy()
    if kind=='kama':
        n,b=p; a=_kama(c,n); up=(a>lag(a))&(c>a+b*atr);dn=(a<lag(a))&(c<a-b*atr)
    elif kind in ('dema','tema','trix'):
        n=p[0];a=ewm(c,n);b=ewm(a,n)
        if kind=='dema':
            z=2*a-b;up=cross_up(c,z);dn=cross_down(c,z)
        else:
            e=ewm(b,n)
            if kind=='trix':
                r=div(e-lag(e),lag(e));up=(r>0)&(lag(r)<=0);dn=(r<0)&(lag(r)>=0)
            else:
                z=3*a-3*b+e;d=z-lag(z);up=(c>z)&(d>0)&(d>lag(d));dn=(c<z)&(d<0)&(d<lag(d))
    elif kind=='hma':
        n=p[0];z=wma(2*wma(c,n//2)-wma(c,n),int(np.sqrt(n)));up=(z>lag(z))&(c>z);dn=(z<lag(z))&(c<z)
    elif kind=='reg_reentry':
        n,z=p;pr,se,_=_reg(c,n);r=div(c-pr,se);up=cross_level(r,-z);dn=cross_level(r,z,False)
    elif kind=='reg_t':
        n,z=p;_,_,t=_reg(np.log(c),n);up=(t>z)&(change>0);dn=(t<-z)&(change<0)
    elif kind=='rank_reentry':
        n,q=p;r=_rank(c,n);up=cross_level(r,q);dn=cross_level(r,1-q,False)
    elif kind=='stoch':
        n,z=p;lo=roll(l,n,'min');hi=roll(h,n,'max');k=100*div(c-lo,hi-lo);d=roll(k,3)
        up=cross_up(k,d)&(lag(k)<z);dn=cross_down(k,d)&(lag(k)>100-z)
    elif kind=='cci':
        n,z=p;tp=(h+l+c)/3;cc=div(tp-roll(tp,n),.015*_mad(tp,n));up=cross_level(cc,-z);dn=cross_level(cc,z,False)
    elif kind=='dmi':
        n,z=p;a=h-lag(h);b=lag(l)-l;plus=np.where((a>b)&(a>0),a,0.);minus=np.where((b>a)&(b>0),b,0.)
        tr=np.maximum(span,np.maximum(np.abs(h-prev),np.abs(l-prev))); av=wilder(tr,n)
        pi=100*div(wilder(plus,n),av);mi=100*div(wilder(minus,n),av);adx=wilder(100*div(abs(pi-mi),pi+mi),n)
        up=cross_up(pi,mi)&(adx>=z);dn=cross_down(pi,mi)&(adx>=z)
    elif kind=='aroon':
        n,z=p;a=_aroon(h,l,n);up=(a>z)&(change>0);dn=(a<-z)&(change<0)
    elif kind=='ha_turn':
        n,k=p;eo=ewm(o,n);ec=ewm((o+h+l+c)/4,n);a=_ha_open(eo,ec);s=ec-a
        up=all_run(s>0,k)&(lag(s,k)<0);dn=all_run(s<0,k)&(lag(s,k)>0)
    elif kind=='pv_corr':
        n,z=p;dv=div(v-lag(v),lag(v));r=correlation(ret,dv,n)
        up=(r>=z)&(change>0)&(v>lag(v));dn=(r<=-z)&(change<0)&(v>lag(v))
    elif kind=='volume_absorb':
        mult,b=p; good=(v>=mult*roll(v,20,prev=True))&(abs(change)<=b*ap);body=abs(c-o)
        up=good&(c>(h+l)/2)&(np.minimum(c,o)-l>=body);dn=good&(c<(h+l)/2)&(h-np.maximum(c,o)>=body)
    elif kind=='mfi':
        n,z=p;tp=(h+l+c)/3;mf=tp*v;positive=roll(np.where(tp>lag(tp),mf,0.),n,'sum');negative=roll(np.where(tp<lag(tp),mf,0.),n,'sum')
        r=100*div(positive,positive+negative);up=cross_level(r,z);dn=cross_level(r,100-z,False)
    elif kind=='chaikin':
        a,b=p;clv=np.where(span>0,div(2*c-h-l,span),0.);adl=np.cumsum(clv*v);co=ewm(adl,a)-ewm(adl,b)
        up=(co>0)&(lag(co)<=0);dn=(co<0)&(lag(co)>=0)
    elif kind=='semivar':
        n,mult=p;pos=roll(np.where(ret>0,ret**2,0.),n,'sum',True);neg=roll(np.where(ret<0,ret**2,0.),n,'sum',True)
        up=(pos>mult*neg)&(change>0);dn=(neg>mult*pos)&(change<0)
    elif kind=='autocorr':
        n,z=p;ac=correlation(ret,lag(ret),n);good=ac<-z;up=good&(lag(ret)<0)&(ret>0);dn=good&(lag(ret)>0)&(ret<0)
    elif kind=='skew':
        n,z=p;s=roll(ret,n,'skew',True);up=(s<-z)&(ret>0);dn=(s>z)&(ret<0)
    elif kind=='park_release':
        n,mult=p;pv=np.log(div(h,l))**2/(4*np.log(2));q=roll(pv,n);ratio=div(q,roll(q,60,prev=True));good=(ratio>mult)&(lag(ratio)<=mult)
        up=good&(c>o);dn=good&(c<o)
    elif kind=='squeeze_break':
        n,mult=p;sd=roll(c,n,'std');good=lag(sd)<mult*roll(sd,120,prev=True);up=good&(c>roll(h,n,'max',True));dn=good&(c<roll(l,n,'min',True))
    elif kind in ('daily_pivot','opening_range'):
        if close_times is None:return up,dn
        from extended_rules import ENTRY_TIMEFRAME_MINUTES
        minutes=ENTRY_TIMEFRAME_MINUTES[timeframe]
        ms=np.asarray(close_times,dtype=np.int64)-minutes*60000;day=ms//86400000;minute=(ms%86400000)//60000
        frame=pd.DataFrame({'day':day,'minute':minute,'h':h,'l':l,'c':c})
        if kind=='daily_pivot':
            b,k=p;d=frame.groupby('day').agg(h=('h','max'),l=('l','min'),c=('c','last'),count=('c','count'))
            piv=(d.h+d.l+d.c)/3;piv=piv.where(d['count']==1440//minutes);mapped=pd.Series(day-1).map(piv).to_numpy();good=(day==lag(day,k))
            up=all_run(c>mapped+b*ap,k)&(lag(c,k)<=lag(mapped,k))&good;dn=all_run(c<mapped-b*ap,k)&(lag(c,k)>=lag(mapped,k))&good
        else:
            n,b=p;first=frame[frame.minute<n].groupby('day').agg(h=('h','max'),l=('l','min'),count=('c','count'))
            if minutes>n or n%minutes:raise ValueError(f"首{n}分钟区间不支持{timeframe}")
            hi=pd.Series(day).map(first.h.where(first['count']==n//minutes)).to_numpy();lo=pd.Series(day).map(first.l.where(first['count']==n//minutes)).to_numpy()
            good=minute>=n;up=good&(c>hi+b*ap)&(prev<=hi+b*lag(ap));dn=good&(c<lo-b*ap)&(prev>=lo-b*lag(ap))
    elif kind=='range_reclaim':
        n,b=p;lo=roll(l,n,'min',True);hi=roll(h,n,'max',True)
        up=(l<lo-b*ap)&(c>lo);dn=(h>hi+b*ap)&(c<hi)
    elif kind=='impulse_pullback':
        n,q=p;imp=lag(c,2)-lag(c,n+2);good=lag(v)<=q*lag(roll(v,20,prev=True))
        up=(imp>lag(atr,2))&(lag(c)<lag(c,2))&good&(c>lag(h));dn=(imp<-lag(atr,2))&(lag(c)>lag(c,2))&good&(c<lag(l))
    elif kind=='scale_div':
        short,long=p;rs=c-lag(c,short);rl=c-lag(c,long);up=(rl>0)&(rs>0)&(lag(rs)<=0);dn=(rl<0)&(rs<0)&(lag(rs)>=0)
    elif kind=='trade_intensity':
        n,mult=p;trades=extra(extras,'trades',nrows);intensity=div(trades,div(span,c));good=intensity>=mult*roll(intensity,n,prev=True)
        up=good&(c>=l+.75*span)&(c>o);dn=good&(c<=l+.25*span)&(c<o)
    elif kind=='flow_accel':
        n,z=p;delta=extra(extras,'delta_base',nrows);flow=div(roll(delta,n,'sum'),roll(v,n,'sum'));zs=zprev(flow,20)
        up=(flow>0)&(zs>z)&(flow>lag(flow));dn=(flow<0)&(zs<-z)&(flow<lag(flow))
    elif kind=='flow_residual':
        n,z=p;x=div(extra(extras,'delta_base',nrows),v);mu_x=roll(x,n,prev=True);mu_y=roll(ret,n,prev=True);vx=roll(x,n,'std',True)**2
        cov=roll(x*ret,n,prev=True)-mu_x*mu_y;b=div(cov,vx);a=mu_y-b*mu_x;se=np.sqrt(np.maximum(roll(ret,n,'std',True)**2-b*cov,0.))
        residual=div(ret-(a+b*x),se);up=cross_level(residual,-z)&(x>0);dn=cross_level(residual,z,False)&(x<0)
    else:raise ValueError(f'未知第四批算法:{kind}')
    valid=np.isfinite(o)&np.isfinite(h)&np.isfinite(l)&np.isfinite(c)&np.isfinite(v)&np.isfinite(values)&(v>=0)&(c>0)&(h>=l)
    # Common 200-bar engine warmup is still in force. Here only data validity + direction.
    return np.asarray(up,dtype=bool)&valid&(np.asarray(direction)==1),np.asarray(dn,dtype=bool)&valid&(np.asarray(direction)==-1)
