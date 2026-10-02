from __future__ import annotations

import numpy as np


CUDA_SOURCE = r'''
extern "C" __global__
void simulate_batch(
    const long long* cand, const signed char* cdir, const long long* offsets,
    const int* run_id, const long long* sx_l, const long long* sx_s,
    const double* sp_l, const double* sp_s,
    const long long* tx_l, const long long* tx_s, const double* tp_l, const double* tp_s,
    const double* close, const double* high, const double* low,
    const double* sizes, const short* year_index,
    const int n, const int entry_count, const int stop_count, const int size_count,
    const int year_count, const int dynamic_mode, const double param1, const double param2,
    const int cooldown_minutes, const double roundtrip_slippage,
    const double initial_capital, const double minimum_order_eth, const int enforce_minimum_order,
    const double maximum_order_eth,
    const long long* funding_cum, const double funding_rate,
    const double protect_ratio, const double maintenance_rate, const int cross_liquidation,
    long long* out_i, double* out_f, double* out_year,
    double* out_eq, double* out_mdd, long long* out_liq,
    long long* out_executed, long long* out_capital_stop, long long* out_forced)
{
    int tid = blockDim.x * blockIdx.x + threadIdx.x;
    int task_count = entry_count * stop_count;
    if (tid >= task_count) return;
    int entry_id = tid / stop_count;
    int stop_id = tid - entry_id * stop_count;
    long long begin = offsets[entry_id], end = offsets[entry_id + 1], cursor = begin;
    long long nt=0, wins=0, nlong=0, orders=0, hold=0;
    double gross=0.0, gross2=0.0, profit=0.0, loss=0.0;
    double eq[32], peak[32], mdd[32];
    long long liq[32], executed[32], capital_stop[32], forced[32];
    signed char active[32];
    for (int z=0; z<size_count; ++z) {
        eq[z]=1.0; peak[z]=1.0; mdd[z]=0.0; liq[z]=0; forced[z]=0;
        executed[z]=0; capital_stop[z]=0; active[z]=1;
    }
    long long last_run=-1, p=0;
    while (true) {
        while (cursor < end && cand[cursor] < p) ++cursor;
        if (cursor >= end) break;
        long long i = cand[cursor];
        if ((long long)run_id[i] == last_run) { p=i+1; ++cursor; continue; }
        int side = (int)cdir[cursor];
        long long at = (long long)stop_id * n + i;
        long long stop_x = side > 0 ? sx_l[at] : sx_s[at];
        long long exit_x = stop_x;
        double exit_price = side > 0 ? sp_l[at] : sp_s[at];
        bool took_profit = false;
        if (dynamic_mode == 0) {
            long long candidate_x = side > 0 ? tx_l[i] : tx_s[i];
            if (candidate_x < stop_x) {
                exit_x = candidate_x;
                exit_price = side > 0 ? tp_l[i] : tp_s[i];
                took_profit = true;
            }
        } else if (dynamic_mode == 2) {
            double entry = close[i], best = entry;
            bool activated = false; long long activation_bar = -1;
            for (long long j=i+1; j<stop_x; ++j) {
                if (side > 0) {
                    if (activated && j>activation_bar) {
                        double trail=entry+(best-entry)*(1.0-param2);
                        if (low[j] <= trail) { exit_x=j; exit_price=trail; took_profit=true; break; }
                    }
                    if (high[j] > best) best=high[j];
                    if (!activated && best >= entry*(1.0+param1)) { activated=true; activation_bar=j; }
                } else {
                    if (activated && j>activation_bar) {
                        double trail=entry-(entry-best)*(1.0-param2);
                        if (high[j] >= trail) { exit_x=j; exit_price=trail; took_profit=true; break; }
                    }
                    if (low[j] < best) best=low[j];
                    if (!activated && best <= entry*(1.0-param1)) { activated=true; activation_bar=j; }
                }
            }
        } else if (dynamic_mode == 3) {
            double entry=close[i], protect=entry*(1.0+param2*side);
            long long activation_bar=-1;
            for (long long j=i+1; j<stop_x; ++j) {
                if (activation_bar < 0) {
                    bool favorable = side>0 ? close[j]>=entry*(1.0+param1) : close[j]<=entry*(1.0-param1);
                    if (favorable) activation_bar=j;
                } else if (j>activation_bar) {
                    bool touched = side>0 ? low[j]<=protect : high[j]>=protect;
                    if (touched) { exit_x=j; exit_price=protect; took_profit=true; break; }
                }
            }
        } else if (dynamic_mode == 4) {
            double entry=close[i], best=entry;
            bool activated=false; long long activation_bar=-1;
            for (long long j=i+1; j<stop_x; ++j) {
                if (side > 0) {
                    if (activated && j>activation_bar) {
                        double trail=best*(1.0-param2);
                        if (low[j] <= trail) { exit_x=j; exit_price=trail; took_profit=true; break; }
                    }
                    if (high[j] > best) best=high[j];
                    if (!activated && best >= entry*(1.0+param1)) { activated=true; activation_bar=j; }
                } else {
                    if (activated && j>activation_bar) {
                        double trail=best*(1.0+param2);
                        if (high[j] >= trail) { exit_x=j; exit_price=trail; took_profit=true; break; }
                    }
                    if (low[j] < best) best=low[j];
                    if (!activated && best <= entry*(1.0-param1)) { activated=true; activation_bar=j; }
                }
            }
        }
        if (exit_x <= i) { p=i+1; ++cursor; continue; }
        double entry=close[i];
        double ret=(exit_price/entry-1.0)*side-roundtrip_slippage;
        double adverse=0.0;
        if (side>0) {
            double worst=entry;
            for (long long j=i+1; j<=exit_x; ++j) if (low[j]<worst) worst=low[j];
            adverse=fmax(0.0, 1.0-worst/entry);
        } else {
            double worst=entry;
            for (long long j=i+1; j<=exit_x; ++j) if (high[j]>worst) worst=high[j];
            adverse=fmax(0.0, worst/entry-1.0);
        }
        if (funding_rate != 0.0) ret -= funding_rate * (double)(funding_cum[exit_x]-funding_cum[i]);
        ++nt; orders+=2; hold+=exit_x-i; gross+=ret; gross2+=ret*ret;
        if (ret>0.0) { ++wins; profit+=ret; } else if (ret<0.0) loss-=ret;
        if (side>0) ++nlong;
        out_year[(long long)tid*year_count + year_index[i]] += ret;
        for (int z=0; z<size_count; ++z) {
            double mult=sizes[z];
            if (!active[z]) continue;
            double requested_qty=initial_capital*eq[z]*mult/entry;
            double actual_qty=fmin(requested_qty, maximum_order_eth);
            if (enforce_minimum_order && actual_qty < minimum_order_eth) {
                active[z]=0; capital_stop[z]=1; continue;
            }
            double effective_mult=actual_qty*entry/(initial_capital*eq[z]);
            ++executed[z];
            double loss_ratio=effective_mult*adverse;
            double liq_ratio=1.0-effective_mult*maintenance_rate;
            if (liq_ratio < 0.0) liq_ratio = 0.0;
            bool use_liq = cross_liquidation && (liq_ratio <= protect_ratio);
            double floor_v = use_liq ? 0.0 : (1.0-protect_ratio);
            double intra_factor=fmax(floor_v, 1.0-loss_ratio);
            double dd=1.0-(eq[z]*intra_factor)/peak[z]; if (dd>mdd[z]) mdd[z]=dd;
            double trade_ret;
            if (use_liq && loss_ratio >= liq_ratio) {
                trade_ret=-1.0; ++forced[z]; active[z]=0; capital_stop[z]=1;
            } else if (loss_ratio >= protect_ratio) { trade_ret=-protect_ratio; ++liq[z]; }
            else trade_ret=effective_mult*ret;
            eq[z]=fmin(1e300, eq[z]*fmax(1e-12, 1.0+trade_ret));
            if (eq[z]>peak[z]) peak[z]=eq[z];
            dd=1.0-eq[z]/peak[z]; if (dd>mdd[z]) mdd[z]=dd;
        }
        last_run=run_id[i]; p=exit_x+(took_profit?cooldown_minutes:0)+1;
    }
    long long ib=(long long)tid*5; out_i[ib]=nt; out_i[ib+1]=wins; out_i[ib+2]=nlong; out_i[ib+3]=orders; out_i[ib+4]=hold;
    long long fb=(long long)tid*4; out_f[fb]=gross; out_f[fb+1]=gross2; out_f[fb+2]=profit; out_f[fb+3]=loss;
    long long sb=(long long)tid*size_count;
    for (int z=0; z<size_count; ++z) {
        double final_qty=fmin(initial_capital*eq[z]*sizes[z]/close[n-1], maximum_order_eth);
        if (enforce_minimum_order && final_qty < minimum_order_eth) capital_stop[z]=1;
        out_eq[sb+z]=eq[z]; out_mdd[sb+z]=mdd[z]; out_liq[sb+z]=liq[z];
        out_executed[sb+z]=executed[z]; out_capital_stop[sb+z]=capital_stop[z];
        out_forced[sb+z]=forced[z];
    }
}
'''


def gpu_status():
    try:
        import cupy as cp
        if cp.cuda.runtime.getDeviceCount() < 1:
            return False, "未检测到CUDA显卡"
        name = cp.cuda.runtime.getDeviceProperties(0)["name"]
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        return True, str(name)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class GpuField:
    def __init__(self, entries, stops, run_id, close, high, low, sizes, year_index, year_count):
        import cupy as cp
        self.cp = cp
        self.n = len(close)
        self.entry_count = len(entries)
        self.stop_count = len(stops)
        self.size_count = len(sizes)
        self.year_count = year_count
        if self.size_count > 32:
            raise ValueError("GPU内核最多支持32档仓位")
        offsets = [0]
        cand_parts, dir_parts = [], []
        for _, cand, cdir in entries:
            cand_parts.append(np.asarray(cand, dtype=np.int64))
            dir_parts.append(np.asarray(cdir, dtype=np.int8))
            offsets.append(offsets[-1] + len(cand))
        self.cand = cp.asarray(np.concatenate(cand_parts))
        self.cdir = cp.asarray(np.concatenate(dir_parts))
        self.offsets = cp.asarray(np.asarray(offsets, dtype=np.int64))
        self.run_id = cp.asarray(np.asarray(run_id, dtype=np.int32))
        shape = (self.stop_count, self.n)
        self.sx_l = cp.empty(shape, dtype=cp.int64); self.sx_s = cp.empty(shape, dtype=cp.int64)
        self.sp_l = cp.empty(shape, dtype=cp.float64); self.sp_s = cp.empty(shape, dtype=cp.float64)
        for j, (_, _, data) in enumerate(stops):
            self.sx_l[j] = cp.asarray(data[0]); self.sx_s[j] = cp.asarray(data[1])
            self.sp_l[j] = cp.asarray(data[2]); self.sp_s[j] = cp.asarray(data[3])
        self.close = cp.asarray(close); self.high = cp.asarray(high); self.low = cp.asarray(low)
        self.sizes = cp.asarray(sizes); self.year_index = cp.asarray(year_index)
        # 禁用融合乘加，使累计平方和与Numba CPU的逐步双精度运算保持一致。
        self.kernel = cp.RawKernel(CUDA_SOURCE, "simulate_batch", options=("--std=c++11", "--fmad=false"))

    def simulate(self, tp_data, cooldown_minutes=0, roundtrip_slippage=0.0,
                 initial_capital=100.0, minimum_order_eth=0.0, enforce_minimum_order=False,
                 maximum_order_eth=100.0, funding_cum=None, funding_rate=0.0,
                 protect_ratio=0.85, maintenance_rate=0.005, cross_liquidation=True):
        cp = self.cp
        tx_l, tx_s, tp_l, tp_s, mode, p1, p2 = tp_data
        tx_l_d=cp.asarray(tx_l); tx_s_d=cp.asarray(tx_s); tp_l_d=cp.asarray(tp_l); tp_s_d=cp.asarray(tp_s)
        tasks=self.entry_count*self.stop_count
        out_i=cp.zeros((tasks,5), dtype=cp.int64); out_f=cp.zeros((tasks,4), dtype=cp.float64)
        out_year=cp.zeros((tasks,self.year_count), dtype=cp.float64)
        out_eq=cp.empty((tasks,self.size_count), dtype=cp.float64)
        out_mdd=cp.empty_like(out_eq); out_liq=cp.empty((tasks,self.size_count), dtype=cp.int64)
        out_executed=cp.empty((tasks,self.size_count), dtype=cp.int64)
        out_capital_stop=cp.empty((tasks,self.size_count), dtype=cp.int64)
        out_forced=cp.empty((tasks,self.size_count), dtype=cp.int64)
        if funding_cum is None:
            funding_cum = np.zeros(self.n + 1, dtype=np.int64)
        funding_d = cp.asarray(np.asarray(funding_cum, dtype=np.int64))
        block=128; grid=((tasks+block-1)//block,)
        self.kernel(grid, (block,), (self.cand,self.cdir,self.offsets,self.run_id,
            self.sx_l,self.sx_s,self.sp_l,self.sp_s,tx_l_d,tx_s_d,tp_l_d,tp_s_d,
            self.close,self.high,self.low,self.sizes,self.year_index,
            np.int32(self.n),np.int32(self.entry_count),np.int32(self.stop_count),np.int32(self.size_count),
            np.int32(self.year_count),np.int32(mode),np.float64(p1),np.float64(p2),
            np.int32(cooldown_minutes),np.float64(roundtrip_slippage),
            np.float64(initial_capital),np.float64(minimum_order_eth),np.int32(bool(enforce_minimum_order)),
            np.float64(maximum_order_eth),
            funding_d,np.float64(funding_rate),
            np.float64(protect_ratio),np.float64(maintenance_rate),np.int32(bool(cross_liquidation)),
            out_i,out_f,out_year,out_eq,out_mdd,out_liq,out_executed,out_capital_stop,out_forced))
        cp.cuda.runtime.deviceSynchronize()
        return tuple(cp.asnumpy(x) for x in (
            out_i,out_f,out_year,out_eq,out_mdd,out_liq,out_executed,out_capital_stop,out_forced))


def result_at(batch, index):
    oi, of, yearly, eq, mdd, liq, executed, capital_stop, forced = batch
    return (int(oi[index,0]), int(oi[index,1]), int(oi[index,2]), int(oi[index,3]), int(oi[index,4]),
            float(of[index,0]), float(of[index,1]), float(of[index,2]), float(of[index,3]),
            yearly[index], eq[index], mdd[index], liq[index], executed[index], capital_stop[index],
            forced[index])
