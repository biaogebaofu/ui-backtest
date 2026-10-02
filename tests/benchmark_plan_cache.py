"""同一实际行情、同一组合对比计划复用开关，先验证数值完全相同。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from time import perf_counter
from statistics import median
import numpy as np
import backtest_engine as B
import account_replay as A
from strategy_space import 生成止盈方案

_, _, runs, entries, stops = B.build_field('hist', ((0, 0, 0, 0, 1),), ('S3_1m+S9_15m',))
_, cand, side = entries[0]
sx_l, sx_s, sp_l, sp_s, _, _ = stops[0][2]
tx_l, tx_s, pl, ps, mode, p1, p2 = B.simple_tp_data(生成止盈方案()[1357])
args = (cand, side, runs, sx_l, sx_s, sp_l, sp_s, tx_l, tx_s, pl, ps, mode, p1, p2,
        B.CLOSE, B.HIGH, B.LOW, np.array([1., 2., 3., 5., 7., 10., 15., 20.]), B.YEAR_INDEX, len(B.YEAR_VALUES))
ref = A.simulate_all_sizes(*args, cache_plans=False)
new = A.simulate_all_sizes(*args, cache_plans=True)
for a, b in zip(ref, new):
    np.testing.assert_equal(a, b)
samples = {False: [], True: []}
for _ in range(5):
    for cache in (False, True):
        start = perf_counter()
        A.simulate_all_sizes(*args, cache_plans=cache)
        samples[cache].append(perf_counter()-start)
old, fast = median(samples[False]), median(samples[True])
print(f'K线={B.N:,}；候选信号={len(cand):,}；杠杆8档；数值逐字段完全相同')
print(f'重复规划中位数={old:.4f}s；共享规划中位数={fast:.4f}s；内核加速={old/fast:.2f}倍')
print('不包含指标准备、磁盘写入、Excel导出或首次JIT编译，不代表全任务同比加速。')
