import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 项目里的 cache/features.npz 是可选的：没有它时用环境变量 BT_FEATURES 指到
# 已经生成好的共享指标缓存，省得为跑测试再复制一份 95MB。
默认特征 = ROOT / "cache" / "features.npz"
if 默认特征.is_file() or not os.environ.get("BT_FEATURES"):
    os.environ["BT_FEATURES"] = str(默认特征)

import backtest_engine as B
from gpu_engine import GpuField, gpu_status, result_at


class EngineAccuracyTests(unittest.TestCase):
    def test_entry_session_and_weekend_use_signal_close_time(self):
        opens = np.array(["2026-09-04T23:59", "2026-09-06T23:59", "2026-09-04T06:59"],
                         dtype="datetime64[ms]").astype(np.int64)
        hours, weekend = B.entry_calendar(opens + 60_000)
        self.assertEqual(hours.tolist(), [0, 0, 7])
        self.assertEqual(weekend.tolist(), [True, False, False])

    def test_stop_cache_does_not_reuse_another_signal_source(self):
        n = 4
        x = np.full(n, 3, dtype=np.int64)
        hist_stop = (x, x, np.full(n, 90.), np.full(n, 110.), np.full(n, 90.), np.full(n, 110.))
        dif_stop = (x, x, np.full(n, 95.), np.full(n, 105.), np.full(n, 95.), np.full(n, 105.))
        key = ("cache-source-regression", 0, 0, 0)
        hist_result = B.combined_stop_data(hist_stop, 0, 0, 0, key)
        dif_result = B.combined_stop_data(dif_stop, 0, 0, 0, key)
        self.assertEqual(hist_result[2][0], 90.)
        self.assertEqual(dif_result[2][0], 95.)

    def test_entry_bar_extreme_cannot_cause_pre_entry_liquidation(self):
        close = np.array([100., 101., 102.])
        high = close.copy(); low = np.array([1., 100., 101.])
        x = np.full(3, 2, dtype=np.int64)
        never = np.full(3, 3, dtype=np.int64)
        result = B.simulate_all_sizes(
            np.array([0], dtype=np.int64), np.array([1], dtype=np.int8),
            np.arange(3, dtype=np.int32), x, x, close[x], close[x],
            never, never, close[x], close[x], 0, 0., 0.,
            close, high, low, np.array([1.]), np.zeros(3, dtype=np.int16), 1)
        self.assertEqual(result[12][0], 0)
        self.assertAlmostEqual(result[10][0], 1.02)
        self.assertAlmostEqual(result[11][0], 0.0)

    def _arrays(self):
        close = np.array([100., 100., 90., 110., 105., 106.])
        high = np.array([100., 101., 91., 111., 106., 107.])
        low = np.array([100., 99., 89., 109., 104., 105.])
        n = len(close)
        return close, high, low, n

    def test_same_macd_run_only_once(self):
        close, high, low, n = self._arrays()
        cand = np.array([0, 3], dtype=np.int64)
        cdir = np.array([1, 1], dtype=np.int8)
        run_id = np.ones(n, dtype=np.int32)
        sx = np.array([2, 3, 4, 5, 5, 5], dtype=np.int64)
        sp = close[sx]
        never = np.full(n, n, dtype=np.int64)
        last = np.full(n, close[-1])
        result = B.simulate_all_sizes(cand, cdir, run_id, sx, sx, sp, sp,
                                      never, never, last, last, 0, 0., 0.,
                                      close, high, low, np.array([1.0]),
                                      np.zeros(n, dtype=np.int16), 1)
        self.assertEqual(result[0], 1)

    def test_stop_wins_same_bar(self):
        close, high, low, n = self._arrays()
        cand = np.array([0], dtype=np.int64); cdir = np.array([1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        sx = np.full(n, n - 1, dtype=np.int64); sx[0] = 2
        sp = np.full(n, close[-1]); sp[0] = 90.
        tx = np.full(n, n, dtype=np.int64); tx[0] = 2
        tp = np.full(n, close[-1]); tp[0] = 110.
        result = B.simulate_all_sizes(cand, cdir, run_id, sx, sx, sp, sp,
                                      tx, tx, tp, tp, 0, 0., 0.,
                                      close, high, low, np.array([1.0]),
                                      np.zeros(n, dtype=np.int16), 1)
        self.assertAlmostEqual(result[5], -0.10, places=12)

    def test_live_entry_mode_latches_high_timeframe_but_event_mode_does_not(self):
        n = 6
        enabled = np.array([False, True, True, True, False, False])
        disabled = np.zeros(n, dtype=np.bool_)
        neutral = (np.ones(n, dtype=np.bool_), np.ones(n, dtype=np.bool_))
        high_tf = tuple(neutral for _ in range(5))
        high_tf = list(high_tf); high_tf[4] = (enabled, disabled); high_tf = tuple(high_tf)
        hi = {"4h": tuple(neutral for _ in range(5)), "1h": tuple(neutral for _ in range(5)),
              "15m": high_tf, "5m": tuple(neutral for _ in range(5))}
        s = {"d1": np.ones(n, dtype=np.int8), "ma120": np.ones(n),
             "15m_entry4_long": np.array([False, True, False, False, False, False]),
             "15m_entry4_short": disabled}
        with mock.patch.object(B, "N", n), mock.patch.object(B, "CLOSE", np.ones(n)):
            live, _ = B.build_candidates(s, hi, (0, 0, 4, 0, 0), entry_mode="LIVE_01")
            event, _ = B.build_candidates(s, hi, (0, 0, 4, 0, 0), entry_mode="TF_EVENT")
        self.assertEqual(live.tolist(), [1, 2, 3])
        self.assertEqual(event.tolist(), [1])

    def test_trailing_stop_does_not_use_new_high_before_same_bar_low(self):
        close = np.array([100., 100., 107., 109., 100.])
        high = np.array([100., 101., 110., 109.2, 100.])
        low = np.array([100., 100., 105., 109., 100.])
        n = len(close)
        cand = np.array([0], dtype=np.int64); cdir = np.array([1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        stop_x = np.full(n, n - 1, dtype=np.int64)
        stop_price = close[stop_x]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        result = B.simulate_all_sizes(cand, cdir, run_id,
                                      stop_x, stop_x, stop_price, stop_price,
                                      never, never, last, last, 2, 0.005, 0.05,
                                      close, high, low, np.array([1.0]),
                                      np.zeros(n, dtype=np.int16), 1)
        self.assertEqual(result[4], 3)
        self.assertAlmostEqual(result[5], 0.095, places=12)

    def test_metrics_separates_complete_trades_from_orders(self):
        result = (10, 6, 5, 20, 100, 1.0, 0.2, 1.5, 0.5, np.zeros(1),
                  np.ones(1), np.zeros(1), np.zeros(1, dtype=np.int64),
                  np.ones(1, dtype=np.int64) * 10, np.zeros(1, dtype=np.int64),
                  np.zeros(1, dtype=np.int64))
        with mock.patch.object(B, "DAYS", 5.0):
            metrics = B.metrics_dict(result)
        self.assertEqual(metrics["平均日完整交易数"], 2.0)
        self.assertEqual(metrics["平均日成交订单数"], 4.0)

    def test_no_fee_compound_math(self):
        close = np.array([100., 110., 100., 90., 90.])
        high = close.copy(); low = close.copy(); n = len(close)
        cand = np.array([0, 2], dtype=np.int64); cdir = np.array([1, -1], dtype=np.int8)
        run_id = np.array([1, 1, 2, 2, 3], dtype=np.int32)
        sx = np.full(n, n - 1, dtype=np.int64); sx[0] = 1; sx[2] = 3
        sp = close[sx]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        result = B.simulate_all_sizes(cand, cdir, run_id, sx, sx, sp, sp,
                                      never, never, last, last, 0, 0., 0.,
                                      close, high, low, np.array([1.0]),
                                      np.zeros(n, dtype=np.int16), 1)
        self.assertEqual(result[0], 2)
        self.assertAlmostEqual(result[10][0], 1.21, places=12)

    def test_take_profit_cooldown_skips_only_after_take_profit(self):
        close = np.full(7, 100.0); high = np.full(7, 101.0); low = np.full(7, 99.0)
        cand = np.array([0, 2, 4], dtype=np.int64)
        cdir = np.ones(3, dtype=np.int8)
        run_id = np.arange(7, dtype=np.int32)
        stop_x = np.full(7, 6, dtype=np.int64)
        stop_price = np.full(7, 90.0)
        tp_x = np.full(7, 7, dtype=np.int64)
        tp_x[0] = 1; tp_x[2] = 3; tp_x[4] = 5
        tp_price = np.full(7, 101.0)
        common = (close, high, low, np.array([1.0]), np.zeros(7, dtype=np.int16), 1)
        with_wait = B.simulate_all_sizes(
            cand, cdir, run_id, stop_x, stop_x, stop_price, stop_price,
            tp_x, tp_x, tp_price, tp_price, 0, 0.0, 0.0, *common, 1)
        self.assertEqual(with_wait[0], 2)

        never_tp = np.full(7, 7, dtype=np.int64)
        stop_each = np.array([1, 6, 3, 6, 5, 6, 6], dtype=np.int64)
        stopped = B.simulate_all_sizes(
            cand, cdir, run_id, stop_each, stop_each, stop_price, stop_price,
            never_tp, never_tp, tp_price, tp_price, 0, 0.0, 0.0, *common, 30)
        self.assertEqual(stopped[0], 3)

    def test_macd_reversal_low_volume_requires_strict_flip_and_no_volume_increase(self):
        values = np.array([3.0, 2.0, 2.5, 3.0, 2.0, 1.5])
        volume = np.array([100.0, 90.0, 80.0, 90.0, 90.0, 95.0])
        long_side, short_side = B.E.macd_reversal_low_volume(values, volume)
        self.assertTrue(long_side[2])
        self.assertTrue(short_side[4])
        self.assertFalse(long_side[3])
        self.assertFalse(short_side[5])

    def test_reversal_volume_separates_sufficient_and_insufficient(self):
        values = np.array([3.0, 2.0, 3.0, 4.0, 3.0, 2.0, 3.0])
        volume = np.array([100.0, 80.0, 90.0, 100.0, 100.0, 120.0, 110.0])
        up, down, high_up, high_down, low_up, low_down = B.E.macd_reversal_volume(values, volume)
        self.assertTrue(up[2] and high_up[2])
        self.assertTrue(down[4] and low_down[4])
        self.assertTrue(up[6] and low_up[6])
        self.assertFalse(high_down[4])

    def test_reversal_break_is_opposite_sufficient_and_larger_than_reference(self):
        values = np.array([3.0, 2.0, 3.0, 4.0, 3.0, 2.0, 3.0])
        volume = np.array([100.0, 80.0, 90.0, 100.0, 120.0, 80.0, 130.0])
        break_long, break_short = B.E.macd_reversal_break(values, volume)
        self.assertTrue(break_long[4])
        self.assertTrue(break_short[6])
        self.assertFalse(np.any(break_long[:4]))

    def test_entry_case4_covers_sufficient_trend_and_insufficient_reverse(self):
        n = B.N
        false = np.zeros(n, dtype=bool)
        true = np.ones(n, dtype=bool)
        state = {
            "d1": np.ones(n, dtype=np.int8),
            "ma120": np.zeros(n),
            "1m_highvol_long": false.copy(),
            "1m_highvol_short": false.copy(),
            "1m_lowvol_long": false.copy(),
            "1m_lowvol_short": false.copy(),
        }
        state["1m_highvol_long"][10] = True
        state["1m_highvol_short"][20] = True
        state["1m_lowvol_short"][30] = True
        state["1m_lowvol_long"][40] = True
        high_timeframes = {tf: {0: (true, true)} for tf in ("4h", "1h", "15m", "5m")}
        cand3, direction3 = B.build_candidates(state, high_timeframes, (0, 0, 0, 0, 3))
        cand4, direction4 = B.build_candidates(state, high_timeframes, (0, 0, 0, 0, 4))
        # 情况三只在量能充足时顺反转方向开仓。
        self.assertEqual(cand3.tolist(), [10, 20])
        self.assertEqual(direction3.tolist(), [1, -1])
        # 情况四覆盖Word的四个分句：量能充足顺向 + 量能不足反向。
        self.assertEqual(cand4.tolist(), [10, 20, 30, 40])
        self.assertEqual(direction4.tolist(), [1, -1, 1, -1])

    def test_entry_case0_takes_direction_from_higher_timeframes_only(self):
        n = B.N
        false = np.zeros(n, dtype=bool)
        true = np.ones(n, dtype=bool)
        state = {
            "d1": np.ones(n, dtype=np.int8),
            "ma120": np.zeros(n),
            "1m_highvol_long": false.copy(),
            "1m_highvol_short": false.copy(),
            "1m_lowvol_long": false.copy(),
            "1m_lowvol_short": false.copy(),
        }
        # 5m 只在这两根上给方向：11 允许做多，12 允许做空。
        long_only = false.copy(); long_only[11] = True
        short_only = false.copy(); short_only[12] = True
        high_timeframes = {tf: {0: (true, true)} for tf in ("4h", "1h", "15m")}
        high_timeframes["5m"] = {0: (true, true), 2: (long_only, short_only)}
        cand, direction = B.build_candidates(state, high_timeframes, (0, 0, 0, 2, 0))
        # 1分钟不启用：不等1分钟触发，高周期给哪个方向就开哪个方向。
        self.assertEqual(cand.tolist(), [11, 12])
        self.assertEqual(direction.tolist(), [1, -1])
        # 高周期也全不启用时两个方向都允许，等于没有方向，一根都不开。
        empty, _ = B.build_candidates(state, high_timeframes, (0, 0, 0, 0, 0))
        self.assertEqual(empty.tolist(), [])
        # 1分钟启用时行为不变：情况一仍然只看1分钟柱值方向。
        case1, dir1 = B.build_candidates(state, high_timeframes, (0, 0, 0, 2, 1))
        self.assertEqual(case1.tolist(), [11])
        self.assertEqual(dir1.tolist(), [1])

    def test_fixed_stop_prices_and_weekend_bucket(self):
        weekday = ~B.IS_WEEKEND
        weekend = B.IS_WEEKEND
        x_l, x_s, level_l, level_s = B.fixed_stop_data(0.004, 0.002)
        # 多单止损在下、空单在上；比例按开仓时刻的UTC星期定档。
        rate_l = 1.0 - level_l / B.CLOSE
        rate_s = level_s / B.CLOSE - 1.0
        self.assertAlmostEqual(0.004, float(rate_l[weekday].mean()), places=9)
        self.assertAlmostEqual(0.002, float(rate_l[weekend].mean()), places=9)
        self.assertAlmostEqual(0.004, float(rate_s[weekday].mean()), places=9)
        self.assertAlmostEqual(0.002, float(rate_s[weekend].mean()), places=9)
        self.assertIsNone(B.fixed_stop_data(0.0, 0.0))

    def test_time_stop_does_not_truncate_to_last_bar(self):
        minutes = 30
        data = B.time_stop_data(minutes)
        self.assertIsNotNone(data)
        x_l, x_s, _p_l, _p_s = data
        self.assertEqual(int(x_l[0]), minutes)
        self.assertEqual(int(x_s[0]), minutes)
        # 最后30根以后没有足够未来K线，必须保持never=N，不能被提前平在样本末尾。
        self.assertTrue(np.all(x_l[B.N - minutes:] == B.N))
        self.assertTrue(np.all(x_s[B.N - minutes:] == B.N))

    def test_fixed_stop_never_exits_later_than_either_rule(self):
        _, EX, _, _ = B.R.build("hist")
        base = B.make_stop_data(EX, ("S3_1m",))
        fixed = B.fixed_stop_data(0.003, 0.003)
        merged = B.merge_fixed_stop(base, fixed)
        limit = np.minimum(base[0], np.minimum(fixed[0], B.N - 1))
        self.assertTrue(bool((merged[0] <= limit).all()))
        limit_s = np.minimum(base[1], np.minimum(fixed[1], B.N - 1))
        self.assertTrue(bool((merged[1] <= limit_s).all()))
        # 启用固定止损后 1R 一律可用，不再依赖③是否被选中。
        self.assertTrue(bool(np.isfinite(merged[4]).all()))
        # OFF 档原样返回，不改动任何数组。
        self.assertIs(base, B.merge_fixed_stop(base, B.fixed_stop_data(0.0, 0.0)))

    def test_min_s3_gap_blocks_entries_too_close_to_baseline(self):
        n = B.N
        close = B.CLOSE
        # 多单基线取收盘价的 99.95%（距离只有 0.05%），空单基线设为不可用。
        level_long = close * 0.9995
        level_short = np.full(n, 1e18)
        EX = {"LVL_1m": (level_long, level_short)}
        loose = B.s3_baseline_gate(EX, "1m", 0.0005)
        tight = B.s3_baseline_gate(EX, "1m", 0.001)
        self.assertTrue(bool(loose[0][100]))     # 恰好满足 0.05%
        self.assertFalse(bool(tight[0][100]))    # 0.10% 不满足，禁止开仓
        self.assertFalse(bool(loose[1][100]))    # 基线不可用，空单一律禁止
        off = B.s3_baseline_gate(EX, "1m", 0.0)
        self.assertTrue(bool(off[0][100]) and bool(off[1][100]))  # 设为0即关闭闸门

    def test_roundtrip_slippage_is_deducted_once_before_leverage(self):
        close = np.array([100.0, 110.0, 110.0])
        high = close.copy(); low = close.copy(); n = len(close)
        cand = np.array([0], dtype=np.int64); cdir = np.array([1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        stop_x = np.array([1, 2, 2], dtype=np.int64)
        stop_price = close[stop_x]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        result = B.simulate_all_sizes(
            cand, cdir, run_id, stop_x, stop_x, stop_price, stop_price,
            never, never, last, last, 0, 0.0, 0.0,
            close, high, low, np.array([2.0]), np.zeros(n, dtype=np.int16), 1,
            0, 0.01)
        self.assertAlmostEqual(result[5], 0.09, places=12)
        self.assertAlmostEqual(result[10][0], 1.18, places=12)

    def test_minimum_eth_quantity_stops_trading_after_capital_loss(self):
        close = np.array([100.0, 15.0, 100.0, 100.0])
        high = close.copy(); low = close.copy(); n = len(close)
        cand = np.array([0, 2], dtype=np.int64); cdir = np.array([1, 1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        stop_x = np.array([1, 3, 3, 3], dtype=np.int64)
        stop_price = close[stop_x]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        result = B.simulate_all_sizes(
            cand, cdir, run_id, stop_x, stop_x, stop_price, stop_price,
            never, never, last, last, 0, 0.0, 0.0,
            close, high, low, np.array([1.0]), np.zeros(n, dtype=np.int16), 1,
            0, 0.0, 1.0, 0.01, True)
        self.assertAlmostEqual(result[10][0], 0.15, places=12)
        self.assertEqual(result[12][0], 1)
        self.assertEqual(result[13][0], 1)
        self.assertEqual(result[14][0], 1)

    def test_maximum_eth_quantity_caps_position_profit(self):
        close = np.array([100.0, 110.0, 110.0])
        high = close.copy(); low = close.copy(); n = len(close)
        cand = np.array([0], dtype=np.int64); cdir = np.array([1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        stop_x = np.array([1, 2, 2], dtype=np.int64)
        stop_price = close[stop_x]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        result = B.simulate_all_sizes(
            cand, cdir, run_id, stop_x, stop_x, stop_price, stop_price,
            never, never, last, last, 0, 0.0, 0.0,
            close, high, low, np.array([1.0]), np.zeros(n, dtype=np.int16), 1,
            0, 0.0, 20_000.0, 0.01, True, 100.0)
        self.assertAlmostEqual(result[10][0], 1.05, places=12)

    def test_gpu_minimum_eth_quantity_matches_cpu_when_cuda_available(self):
        available, detail = gpu_status()
        if not available:
            self.skipTest(detail)
        gpu_temp = Path(tempfile.gettempdir()) / "eth_backtest_gpu_test_cache"
        gpu_temp.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = str(gpu_temp)
        os.environ["TMP"] = str(gpu_temp)
        os.environ["CUPY_CACHE_DIR"] = str(gpu_temp / "cupy")
        close = np.array([100.0, 15.0, 100.0, 100.0])
        high = close.copy(); low = close.copy(); n = len(close)
        cand = np.array([0, 2], dtype=np.int64); cdir = np.array([1, 1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        stop_x = np.array([1, 3, 3, 3], dtype=np.int64)
        stop_price = close[stop_x]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        entries = [((1, 1, 1, 1, 1), cand, cdir)]
        stops = [("T", "测试", (stop_x, stop_x, stop_price, stop_price, None, None))]
        gpu = GpuField(entries, stops, run_id, close, high, low, np.array([1.0]),
                       np.zeros(n, dtype=np.int16), 1)
        batch = gpu.simulate((never, never, last, last, 0, 0.0, 0.0), 0, 0.0, 1.0, 0.01, True)
        result = result_at(batch, 0)
        self.assertAlmostEqual(result[10][0], 0.15, places=12)
        self.assertEqual(result[12][0], 1)
        self.assertEqual(result[13][0], 1)
        self.assertEqual(result[14][0], 1)

    def test_gpu_maximum_eth_quantity_matches_cpu_when_cuda_available(self):
        available, detail = gpu_status()
        if not available:
            self.skipTest(detail)
        close = np.array([100.0, 110.0, 110.0])
        high = close.copy(); low = close.copy(); n = len(close)
        cand = np.array([0], dtype=np.int64); cdir = np.array([1], dtype=np.int8)
        run_id = np.arange(n, dtype=np.int32)
        stop_x = np.array([1, 2, 2], dtype=np.int64)
        stop_price = close[stop_x]
        never = np.full(n, n, dtype=np.int64); last = np.full(n, close[-1])
        entries = [((1, 1, 1, 1, 1), cand, cdir)]
        stops = [("T", "测试", (stop_x, stop_x, stop_price, stop_price, None, None))]
        gpu = GpuField(entries, stops, run_id, close, high, low, np.array([1.0]),
                       np.zeros(n, dtype=np.int16), 1)
        batch = gpu.simulate((never, never, last, last, 0, 0.0, 0.0),
                             0, 0.0, 20_000.0, 0.01, True, 100.0)
        self.assertAlmostEqual(result_at(batch, 0)[10][0], 1.05, places=12)


if __name__ == "__main__":
    unittest.main()
