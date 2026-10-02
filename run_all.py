import os
import numpy as np, itertools, time, pandas as pd
import engine as E
from functools import lru_cache

BASE = os.path.dirname(os.path.abspath(__file__))
n = E.n; close = E.close; DAYS = (E.D['ct1'][-1] - E.D['openms'][0]) / 86400000.0
CLOSE = close.tolist()

@lru_cache(maxsize=2)
def build(field):
    S = E.build_signals(field)
    EX = E.build_exits(S)
    d1 = S['d1']; ma = S['ma120']
    flip = np.zeros(n, bool); flip[1:] = d1[1:] != d1[:-1]
    trig = {}
    trig[('state', 1)] = ((d1 == 1), (d1 == -1))
    trig[('state', 2)] = ((d1 == 1) & (close > ma), (d1 == -1) & (close < ma))
    trig[('flip', 1)] = ((d1 == 1) & flip, (d1 == -1) & flip)
    trig[('flip', 2)] = ((d1 == 1) & flip & (close > ma), (d1 == -1) & flip & (close < ma))
    trig[('state', 3)] = (S['1m_highvol_long'], S['1m_highvol_short'])
    trig[('flip', 3)] = trig[('state', 3)]
    # 情况四：量能充足时顺反转方向，量能不足时反向。Word 的四个分句全部覆盖。
    trig[('state', 4)] = (S['1m_highvol_long'] | S['1m_lowvol_short'],
                          S['1m_highvol_short'] | S['1m_lowvol_long'])
    trig[('flip', 4)] = trig[('state', 4)]
    # 0 = 1分钟不启用：不要求1分钟触发，方向完全交给高周期过滤器。
    trig[('state', 0)] = (np.ones(n, bool), np.ones(n, bool))
    trig[('flip', 0)] = trig[('state', 0)]

    HI = {}
    for k in ['15m', '1h', '4h']:
        dk, inwin = E.hi_tf_state(S, k)
        HI[k] = {0: (np.ones(n, bool), np.ones(n, bool)),
                 1: (~(inwin & (dk == -1)), ~(inwin & (dk == 1))),
                 2: (inwin & (dk == 1), inwin & (dk == -1))}
    mp5 = S['5m_map']; d5 = S['5m_d'][mp5]; paused = E.five_pause(S); v5 = mp5 >= 0
    HI['5m'] = {0: (np.ones(n, bool), np.ones(n, bool)),
                1: (v5 & (d5 == 1) & ~paused, v5 & (d5 == -1) & ~paused),
                2: (v5 & (d5 == 1), v5 & (d5 == -1))}
    for k in ['5m', '15m', '1h', '4h']:
        mp = S[k + '_map']; safe = np.maximum(mp, 0); valid = mp >= 2
        # 情况三/四用"锁存"状态：高周期一旦给出方向，只要没有新的严格反转就一直有效。
        st = S[k + '_case_state'][safe]
        HI[k][3] = (valid & (st == 1), valid & (st == -1))
        # 情况四：量能充足顺向 + 量能不足反向。
        HI[k][4] = (valid & ((st == 1) | (st == -2)),
                    valid & ((st == -1) | (st == 2)))
    warm = np.zeros(n, bool); warm[:200] = True
    for k in HI:
        for c in HI[k]:
            reliable = ~warm
            # MACD(12,26,9)由文件首根K线播种。非零高周期条件至少等35根
            # 原生周期K线后才允许使用；原先只统一等待200个1m，对4h仅0.83根，
            # 会让样本开头的伪反转参与交易。
            if c != 0:
                reliable = reliable & (S[k + '_map'] >= 35)
            HI[k][c] = (HI[k][c][0] & reliable, HI[k][c][1] & reliable)
    return S, EX, HI, trig

STOPS = ['S1'] + ['S1+S4_' + k for k in ['1m','5m','15m','1h','4h']] \
               + ['S1+S3_' + k for k in ['1m','5m','15m','1h','4h']]

def stopdata(EX):
    out = {}
    e1L, e1S = EX['S1']
    for name in STOPS:
        if name == 'S1':
            xL, xS = e1L.copy(), e1S.copy()
            pL = close[np.minimum(xL, n-1)].copy(); pS = close[np.minimum(xS, n-1)].copy()
        else:
            r = name.split('+')[1]
            oL, oS = EX[r]
            xL = np.minimum(e1L, oL); xS = np.minimum(e1S, oS)
            pL = close[np.minimum(xL, n-1)].copy(); pS = close[np.minimum(xS, n-1)].copy()
            if r.startswith('S3'):
                lvlL, lvlS = EX['LVL_' + r[3:]]
                useL = (oL <= e1L) & (oL < n) & (lvlL < close)
                useS = (oS <= e1S) & (oS < n) & (lvlS > close)
                pL = np.where(useL, lvlL, pL); pS = np.where(useS, lvlS, pS)
        out[name] = (np.minimum(xL, n-1).tolist(), np.minimum(xS, n-1).tolist(),
                     pL.tolist(), pS.tolist())
    return out

YEAR_BIN = None
def set_year_bin(arr):
    global YEAR_BIN
    YEAR_BIN = arr

def simulate(cand, cdir, sd, record=False):
    xL, xS, pL, pS = sd
    m = len(cand)
    nt = 0; gsum = 0.0; hold = 0; nlong = 0
    g2 = 0.0; h1 = 0.0; h2s = 0.0; half = n // 2
    yearly = [0.0, 0.0]
    rets = [] if record else None
    idx = [] if record else None
    p = 0
    while True:
        k = cand.searchsorted(p)
        if k >= m: break
        i = int(cand[k]); dr = int(cdir[k])
        x = xL[i] if dr > 0 else xS[i]
        px = pL[i] if dr > 0 else pS[i]
        if x <= i:
            p = i + 1; continue
        ret = (px / CLOSE[i] - 1.0) * dr
        nt += 1; gsum += ret; hold += x - i; g2 += ret * ret
        if i < half: h1 += ret
        else: h2s += ret
        yearly[YEAR_BIN[i]] += ret
        if dr > 0: nlong += 1
        if record: rets.append(ret); idx.append((i, x, dr))
        p = x + 1
    if nt == 0: return None
    return dict(nt=nt, gross=gsum, g2=g2, hold=hold/nt, tpd=nt/DAYS, expo=hold/n,
                long_ratio=nlong/nt, h1=h1, h2=h2s, y2025=yearly[0], y2026=yearly[1],
                rets=rets, idx=idx)

def metrics(rets, fee_rt):
    a = np.asarray(rets) - fee_rt
    cum = np.cumsum(a); peak = np.maximum.accumulate(cum)
    gp = a[a > 0].sum(); gl = -a[a < 0].sum()
    return dict(net=a.sum(), win=float((a > 0).mean()),
                pf=(gp / gl if gl > 0 else np.inf),
                mdd=float((peak - cum).max()), avg_bp=a.mean() * 1e4)

def run(field, mode, save=True):
    S, EX, HI, trig = build(field)
    SD = stopdata(EX)
    rows = []
    for c4, c1, c15, c5, cm in itertools.product([0,1,2,3,4],[0,1,2,3,4],[0,1,2,3,4],[0,1,2,3,4],[0,1,2,3,4]):
        tl, ts = trig[(mode, cm)]
        al = HI['4h'][c4][0] & HI['1h'][c1][0] & HI['15m'][c15][0] & HI['5m'][c5][0] & tl
        ash = HI['4h'][c4][1] & HI['1h'][c1][1] & HI['15m'][c15][1] & HI['5m'][c5][1] & ts
        if cm == 0:
            # 高周期同时允许两个方向等于没给方向，不开仓；否则会凭空偏多。
            both = al & ash
            al = al & ~both; ash = ash & ~both
        cand = np.flatnonzero(al | ash)
        if len(cand) == 0: continue
        cdir = np.where(al[cand], 1, -1)
        for s in STOPS:
            r = simulate(cand, cdir, SD[s])
            if r is None: continue
            r.pop('rets'); r.pop('idx')
            r.update(c4h=c4, c1h=c1, c15m=c15, c5m=c5, c1m=cm, stop=s, field=field, mode=mode,
                     nper=1 + (c4>0) + (c1>0) + (c15>0) + (c5>0))
            rows.append(r)
    df = pd.DataFrame(rows)
    df['gross_bp'] = df.gross / df.nt * 1e4
    var = (df.g2 - df.gross ** 2 / df.nt) / (df.nt - 1)
    df['sd_bp'] = np.sqrt(var.clip(lower=0)) * 1e4
    df['tstat4'] = (df.gross - df.nt * 4e-4) / df.nt / np.sqrt(var.clip(lower=1e-18) / df.nt)
    df['tstat0'] = df.gross / df.nt / np.sqrt(var.clip(lower=1e-18) / df.nt)
    for f in [0, 2, 4, 10]:
        df['net_%dbp' % f] = df.gross - df.nt * f / 1e4
    if save: df.to_pickle(os.path.join(BASE, 'grid_%s_%s.pkl' % (field, mode)))
    return df

if __name__ == '__main__':
    years = pd.to_datetime(E.D['openms'], unit='ms', utc=True).year.values
    set_year_bin((years >= 2026).astype(np.int8).tolist())
    allr = []
    for field in ['dif', 'hist']:
        for mode in ['state', 'flip']:
            t0 = time.time()
            d = run(field, mode)
            allr.append(d)
            print(field, mode, len(d), 'rows', round(time.time()-t0,1), 's',
                  '| best net@4bp %.4f' % d['net_4bp'].max(),
                  '| best gross %.4f' % d.gross.max(),
                  '| median tpd %.1f' % d.tpd.median())
    A = pd.concat(allr, ignore_index=True)
    A.to_pickle(os.path.join(BASE, 'grid_all.pkl'))
    print('total', len(A))
