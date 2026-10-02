import os
import numpy as np
from numba import njit

FEATURE_PATH = os.environ.get(
    "BT_FEATURES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "features.npz"),
)
def _load_features(path):
    """一次性读进内存，不要留着惰性的 NpzFile。

    np.load 返回的 NpzFile 是懒加载的：每次 D["xxx"] 都要重新从 zip 解压一遍，
    实测比内存字典慢一千多倍；更要命的是它内部共用一个文件句柄，
    多线程并发读会被 zipfile 的锁串行化——18 个线程只跑出 1 个核，
    就是卡在这里，而不是 numba 内核不并行（两个 simulate 都已 nogil）。
    """
    with np.load(path) as raw:
        return {key: raw[key] for key in raw.files}


D = _load_features(FEATURE_PATH)
close = D['close']; high = D['high']; low = D['low']
n = len(close)
N = n  # sentinel = "never"
CT1 = D['ct1']

def ffill_dir(d):
    idx = np.where(d != 0, np.arange(len(d)), 0)
    np.maximum.accumulate(idx, out=idx)
    return d[idx]

def direction(x):
    d = np.zeros(len(x), dtype=np.int8)
    diff = np.diff(x)
    d[1:] = np.where(diff > 0, 1, np.where(diff < 0, -1, 0)).astype(np.int8)
    return ffill_dir(d)


def macd_reversal_volume(values, volume):
    """拆分严格MACD反转的方向，以及量能充足/不足四种事件。"""
    values = np.asarray(values, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    up_flip = np.zeros(len(values), dtype=bool)
    down_flip = np.zeros(len(values), dtype=bool)
    up_flip[2:] = (values[1:-1] < values[:-2]) & (values[2:] > values[1:-1])
    down_flip[2:] = (values[1:-1] > values[:-2]) & (values[2:] < values[1:-1])
    volume_up = np.zeros(len(values), dtype=bool)
    volume_up[1:] = volume[1:] > volume[:-1]
    sufficient_up = up_flip & volume_up
    sufficient_down = down_flip & volume_up
    insufficient_up = up_flip & ~volume_up
    insufficient_down = down_flip & ~volume_up
    return up_flip, down_flip, sufficient_up, sufficient_down, insufficient_up, insufficient_down


def latched_case_state(values, volume):
    """每根柱子上"当前有效"的反转+量能状态，直到出现新的严格反转才更新。

    +1 = 向上反转且量能充足   +2 = 向上反转但量能不足
    -1 = 向下反转且量能充足   -2 = 向下反转但量能不足
     0 = 序列开头还没出现过任何严格反转

    这是情况三/情况四的正确口径：5分钟一旦给出方向，只要没有新的反转就一直有效；
    出现新反转才重新计算。原来只在反转那一根柱子上有效（高周期只管5分钟），
    导致1分钟还没等到触发，高周期许可就已经过期了。
    """
    values = np.asarray(values, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    m = len(values)
    state = np.zeros(m, dtype=np.int8)
    up_flip, down_flip, suf_up, suf_dn, ins_up, ins_dn = macd_reversal_volume(values, volume)
    cur = 0
    for i in range(m):
        if up_flip[i]:
            cur = 1 if suf_up[i] else 2
        elif down_flip[i]:
            cur = -1 if suf_dn[i] else -2
        state[i] = cur
    return state


def macd_reversal_low_volume(values, volume):
    """兼容接口：返回向上/向下严格反转且本根成交量不大于上一根。"""
    return macd_reversal_volume(values, volume)[4:6]


def macd_reversal_break(values, volume):
    """返回多/空持仓被反破事件。

    当前反转必须量能充足，并且成交量大于前一次相反方向反转K线；
    多单由后续向下反转反破，空单由后续向上反转反破。
    """
    values = np.asarray(values, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    up_flip, down_flip, sufficient_up, sufficient_down, _, _ = macd_reversal_volume(values, volume)
    break_long = np.zeros(len(values), dtype=bool)
    break_short = np.zeros(len(values), dtype=bool)
    previous_up_volume = np.nan
    previous_down_volume = np.nan
    for index in range(len(values)):
        if up_flip[index]:
            if sufficient_up[index] and np.isfinite(previous_down_volume):
                break_short[index] = volume[index] > previous_down_volume
            previous_up_volume = volume[index]
        elif down_flip[index]:
            if sufficient_down[index] and np.isfinite(previous_up_volume):
                break_long[index] = volume[index] > previous_up_volume
            previous_down_volume = volume[index]
    return break_long, break_short

def run_counter(d):
    m = len(d)
    chg = np.ones(m, dtype=bool)
    chg[1:] = d[1:] != d[:-1]
    starts = np.where(chg, np.arange(m), 0)
    np.maximum.accumulate(starts, out=starts)
    return (np.arange(m) - starts + 1).astype(np.int32)

def prev_pivot(d):
    """returns (trough_idx, peak_idx): most recent CONFIRMED macd extreme bar as of each bar.
    trough = last bar of a falling run (macd low); peak = last bar of a rising run (macd high)."""
    m = len(d)
    end = np.zeros(m, dtype=bool)
    end[:-1] = d[1:] != d[:-1]          # bar j ends a run
    tr = np.where(end & (d == -1), np.arange(m), -1)
    pk = np.where(end & (d == 1), np.arange(m), -1)
    # shift by 1: the extreme is only known at the next bar
    tr = np.concatenate([[-1], tr[:-1]]); pk = np.concatenate([[-1], pk[:-1]])
    np.maximum.accumulate(tr, out=tr); np.maximum.accumulate(pk, out=pk)
    return tr, pk

def next_true_ge(mask):
    m = len(mask)
    idx = np.where(mask, np.arange(m), N).astype(np.int64)
    out = np.empty(m + 1, dtype=np.int64)
    out[m] = N
    out[:m] = np.minimum.accumulate(idx[::-1])[::-1]
    return out

def gather(A, idx):
    idx = np.asarray(idx)
    out = np.full(len(idx), N, dtype=np.int64)
    ok = idx < N
    out[ok] = A[idx[ok]]
    return out

# ---------------------------------------------------------------- signals
def build_signals(field='dif'):
    S = {}
    S['d1'] = direction(D['m1_' + field])
    S['ma120'] = D['ma120']
    for k in ['1m', '5m', '15m', '1h', '4h']:
        masks = macd_reversal_volume(D[k + '_' + field], D[k + '_volume'])
        up_flip, down_flip, high_long, high_short, low_long, low_short = masks
        break_long, break_short = macd_reversal_break(D[k + '_' + field], D[k + '_volume'])
        S[k + '_flip_long'] = up_flip
        S[k + '_flip_short'] = down_flip
        S[k + '_highvol_long'] = high_long
        S[k + '_highvol_short'] = high_short
        S[k + '_lowvol_long'] = low_long
        S[k + '_lowvol_short'] = low_short
        S[k + '_break_long'] = break_long
        S[k + '_break_short'] = break_short
        S[k + '_case_state'] = latched_case_state(D[k + '_' + field], D[k + '_volume'])
    for k in ['5m', '15m', '1h', '4h']:
        dk = direction(D[k + '_' + field])
        S[k + '_d'] = dk
        S[k + '_cnt'] = run_counter(dk)
        S[k + '_map'] = D[k + '_map']
        # 高周期条件不能在整段状态里借用1分钟MACD反复触发。
        # 把每根已收盘高周期K线，以及情况三/四的原生反转，映射到唯一的1分钟时刻。
        mp = S[k + '_map']
        safe = np.maximum(mp, 0)
        bar_close = np.zeros(n, dtype=bool)
        bar_close[0] = mp[0] >= 0
        bar_close[1:] = (mp[1:] != mp[:-1]) & (mp[1:] >= 0)
        state = S[k + '_case_state'][safe]
        native_reversal = S[k + '_flip_long'][safe] | S[k + '_flip_short'][safe]
        reversal = bar_close & native_reversal
        S[k + '_close_event'] = bar_close
        S[k + '_entry3_long'] = reversal & (state == 1)
        S[k + '_entry3_short'] = reversal & (state == -1)
        S[k + '_entry4_long'] = reversal & ((state == 1) | (state == -2))
        S[k + '_entry4_short'] = reversal & ((state == -1) | (state == 2))
    return S

def hi_tf_state(S, k):
    mp = S[k + '_map']
    safe = np.maximum(mp, 0)
    d = S[k + '_d'][safe]
    cnt = S[k + '_cnt'][safe]
    inwin = (cnt <= 3) & (mp >= 1)
    return d, inwin

def five_pause(S, pause_min=60, need=5):
    d = S['5m_d']; cnt = S['5m_cnt']; ct = D['5m_ct']
    m = len(d)
    pause_until = np.zeros(m, dtype=np.int64)
    bad = 0; cur = 0
    for j in range(1, m):
        if d[j] != d[j-1]:
            L = cnt[j-1]
            if L < need:
                bad += 1
                if bad >= 2:
                    cur = max(cur, int(ct[j]) + pause_min * 60000)
                    bad = 0
            else:
                bad = 0
        pause_until[j] = cur
    mp = S['5m_map']
    safe = np.maximum(mp, 0)
    return (mp >= 0) & (CT1 < pause_until[safe])

# ---------------------------------------------------------------- level scan
def first_below(arr, lvl):
    """out[i] = first j > i with arr[j] <= lvl[i]"""
    return _first_below_kernel(arr, lvl, N)


@njit(cache=True, nogil=True)
def _first_below_kernel(arr, lvl, sentinel):
    # Pass the dataset sentinel explicitly: a cached specialization must not
    # retain the length of a previous dataset. Keep strict comparisons/NaNs.
    m = len(arr)
    out = np.full(m, sentinel, dtype=np.int64)
    ci = np.empty(m, dtype=np.int64)
    cv = np.empty_like(arr)
    count = 0
    prev_a = sentinel
    for i in range(m - 1, -1, -1):
        j = i + 1
        if j >= m:
            ans = sentinel
        else:
            L = lvl[i]
            if arr[j] <= L:
                ans = j
            elif L == lvl[j]:
                ans = prev_a
            else:
                left, right = 0, count
                while left < right:
                    mid = (left + right) // 2
                    if L < cv[mid]:
                        right = mid
                    else:
                        left = mid + 1
                p = left - 1
                ans = ci[p] if p >= 0 else sentinel
        out[i] = ans
        prev_a = ans
        v = arr[i]
        while count and cv[count - 1] >= v:
            count -= 1
        cv[count] = v; ci[count] = i
        count += 1
    return out

def first_above(arr, lvl):
    return first_below(-arr, -lvl)

# ---------------------------------------------------------------- exits
def build_exits(S):
    E = {}
    dif = D['m1_dif']; dea = D['m1_dea']; hist = D['m1_hist']
    d1 = S['d1']
    ar = np.arange(1, n + 1)

    flipD = np.zeros(n, bool); flipD[1:] = (d1[1:] == -1) & (d1[:-1] == 1)
    flipU = np.zeros(n, bool); flipU[1:] = (d1[1:] == 1) & (d1[:-1] == -1)
    crossU = np.zeros(n, bool); crossU[1:] = (dif[1:] > dea[1:]) & (dif[:-1] <= dea[:-1])
    crossD = np.zeros(n, bool); crossD[1:] = (dif[1:] < dea[1:]) & (dif[:-1] >= dea[:-1])
    hdn = np.zeros(n, bool); hdn[1:] = hist[1:] < hist[:-1]
    hup = np.zeros(n, bool); hup[1:] = hist[1:] > hist[:-1]

    A, B, C, Dd = next_true_ge(flipD), next_true_ge(dif < dea), next_true_ge(crossU), next_true_ge(hdn)
    eL = gather(Dd, gather(C, gather(B, gather(A, ar))))
    A2, B2, C2, D2 = next_true_ge(flipU), next_true_ge(dif > dea), next_true_ge(crossD), next_true_ge(hup)
    eS = gather(D2, gather(C2, gather(B2, gather(A2, ar))))
    E['S1'] = (eL, eS)

    def minute_events(k, native_mask):
        if k == '1m':
            return np.asarray(native_mask, dtype=bool)
        events = np.zeros(n, dtype=bool)
        minute_index = np.searchsorted(CT1, D[k + '_ct'], side='left')
        valid = (minute_index >= 0) & (minute_index < n)
        events[minute_index[valid]] = np.asarray(native_mask, dtype=bool)[valid]
        return events

    for k in ['1m', '5m', '15m', '1h', '4h']:
        low_long = minute_events(k, S[k + '_lowvol_long'])
        low_short = minute_events(k, S[k + '_lowvol_short'])
        break_long = minute_events(k, S[k + '_break_long'])
        break_short = minute_events(k, S[k + '_break_short'])
        flip_up = minute_events(k, S[k + '_flip_long'])
        flip_down = minute_events(k, S[k + '_flip_short'])
        E['S4_' + k] = (gather(next_true_ge(low_long), ar),
                        gather(next_true_ge(low_short), ar))
        E['S5_' + k] = (gather(next_true_ge(break_long), ar),
                        gather(next_true_ge(break_short), ar))
        E['S6_' + k] = (gather(next_true_ge(flip_down), ar),
                        gather(next_true_ge(flip_up), ar))
        # ⑨情况三止损：出现一根反向的"量能充足反转"就离场。
        # 多单被向下的量能充足反转打掉，空单被向上的打掉。
        high_long = minute_events(k, S[k + '_highvol_long'])
        high_short = minute_events(k, S[k + '_highvol_short'])
        E['S9_' + k] = (gather(next_true_ge(high_short), ar),
                        gather(next_true_ge(high_long), ar))

    for k in ['1m', '5m', '15m', '1h', '4h']:
        if k == '1m':
            dk = d1; hi = high; lo = low; mp = np.arange(n)
        else:
            dk = S[k + '_d']; hi = D[k + '_high']; lo = D[k + '_low']; mp = S[k + '_map']
        tr, pk = prev_pivot(dk)
        safe = np.maximum(mp, 0)
        tr = tr[safe]; pk = pk[safe]
        tr = np.where(mp >= 0, tr, -1)
        pk = np.where(mp >= 0, pk, -1)
        lvlL = np.where(tr >= 0, lo[np.clip(tr, 0, None)] * 0.999, -1e18)
        lvlS = np.where(pk >= 0, hi[np.clip(pk, 0, None)] * 1.001, 1e18)
        E['LVL_' + k] = (lvlL, lvlS)
        E['S3_' + k] = (first_below(low, lvlL), first_above(high, lvlS))
    return E
