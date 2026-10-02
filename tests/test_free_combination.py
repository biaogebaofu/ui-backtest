import unittest
from unittest import mock
import numpy as np
from extended_rules import ENTRY_RULES, stable_base_id, extra_stop_components
from extended_signals import entry_masks, atr14
from selection_config import 全选配置, 规范化配置, 配置签名


class FreeCombinationTests(unittest.TestCase):
    def test_atr_warmup_and_prefix(self):
        close = np.arange(100., 150.)
        full = atr14(close+2, close-2, close)
        self.assertTrue(np.isnan(full[:13]).all())
        np.testing.assert_allclose(full[13:], 4.)
        np.testing.assert_equal(full[:30], atr14(close[:30]+2, close[:30]-2, close[:30]))

    def test_entry_filters_are_causal_and_directional(self):
        rng = np.random.default_rng(50)
        c = rng.uniform(95, 105, 100)
        o = c + rng.uniform(-1, 1, 100)
        h, l = np.maximum(o, c)+1, np.minimum(o, c)-1
        d = np.where(c > 100, 1, -1)
        ma = np.full(100, 100.)
        # 0—263保持旧MACD方向兼容；第五轮自主事件/外部依赖由专门测试验收。
        for code in (code for code in ENTRY_RULES if code < 264):
            a = entry_masks(code, d, c-100, o, h, l, c, ma)
            b = entry_masks(code, d[:40], c[:40]-100, o[:40], h[:40], l[:40], c[:40], ma[:40])
            for side, x, y in zip((1, -1), a, b):
                np.testing.assert_array_equal(x[:40], y)
                self.assertFalse(np.any(x & (d != side)))

    def test_ids_preserve_old_strategy_and_do_not_collide(self):
        self.assertEqual(stable_base_id(0, (0, 0, 0, 0, 1), 457), 1370)
        ids = {stable_base_id(f, (0, 0, 0, c, 1), s)
               for f in (0, 1) for c in range(18) for s in range(6666)}
        self.assertEqual(len(ids), 2*18*6666)
        self.assertGreater(min(stable_base_id(0, (0, 0, 0, 0, 1), s) for s in range(912, 6666)), 5700000)
        self.assertEqual(len(extra_stop_components()), 84)
        # 第三批32—63进入新命名空间，且不改写旧0—31编号。
        hi_ids = {stable_base_id(f, (0, 0, 0, 0, c), 0) for f in (0, 1) for c in range(32, 64)}
        self.assertEqual(len(hi_ids), 64)
        self.assertTrue(all(x >= 20_000_000_000_000 for x in hi_ids))
        # 第三批64—127进入base128新命名空间，且不碰旧base64 ID。
        hi2 = {stable_base_id(f, (0, 0, 0, 0, c), 0) for f in (0, 1) for c in range(64, 128)}
        self.assertEqual(len(hi2), 128)
        self.assertTrue(all(x >= 40_000_000_000_000_000 for x in hi2))
        self.assertTrue(hi_ids.isdisjoint(hi2))

    def test_fill_mode_is_explicit_and_invalid_mode_rejected(self):
        raw = 全选配置()
        self.assertEqual(raw['成交价格口径'], 'CLOSE_CONFIRMED')
        current = 配置签名(raw)
        raw.pop('成交价格口径')
        self.assertEqual(规范化配置(raw)['成交价格口径'], 'THEORETICAL')
        self.assertNotEqual(current, 配置签名(raw))
        raw['成交价格口径'] = 'unknown'
        with self.assertRaises(ValueError):
            规范化配置(raw)

    def test_close_fill_and_plan_cache_match_uncached_accounts(self):
        import account_replay as A
        c = np.array([100., 100., 99., 100., 100., 100., 100.])
        n = len(c)
        sx = np.full(n, n-1, dtype=np.int64)
        tx = np.full(n, 2, dtype=np.int64)
        p = np.full(n, 101.)
        args = (np.array([0], dtype=np.int64), np.array([1], dtype=np.int8), np.arange(n, dtype=np.int32),
                sx, sx, c[sx], c[sx], tx, tx, p, p, 0, 0., 0., c,
                np.maximum(c, 101.), np.minimum(c, 99.), np.array([1., 2., 5.]), np.zeros(n, dtype=np.int16), 1)
        for confirmed in (False, True):
            fast = A.simulate_all_sizes(*args, close_confirmed=confirmed)
            slow = A.simulate_all_sizes(*args, close_confirmed=confirmed, cache_plans=False)
            for a, b in zip(fast, slow):
                np.testing.assert_equal(a, b)
            self.assertAlmostEqual(fast[5], -.01 if confirmed else .01)

    def test_new_stop_exits_never_use_pre_entry_confirmation(self):
        import backtest_engine as B
        for code in ('ATR50_1m', 'ATR75_15m', 'MA20C1_1m', 'MA60C3_15m', 'BAD5_1m', 'BAD30_1m'):
            data = B.extended_stop_data(code)
            for x in data[:2]:
                good = x < B.N
                earliest = int(code.split('_')[0][3:]) if code.startswith('BAD') else 1
                self.assertTrue(np.all(x[good] >= np.flatnonzero(good)+earliest), code)
        # 直接逐笔循环核对BAD5，避免只验证有退出而遗漏阈值和连续窗口定义。
        xs = B.extended_stop_data('BAD5_1m')[:2]
        for i in range(300, 360, 3):
            for side, x in zip((1, -1), xs):
                expected = B.N
                for j in range(i+5, B.N):
                    if np.all((B.CLOSE[j-4:j+1] - B.CLOSE[i]) * side < 0):
                        expected = j
                        break
                self.assertEqual(x[i], expected)

    def test_partial_close_fills_are_weighted_and_cache_is_exact(self):
        import account_replay as A
        c = np.array([100., 100., 100.5, 101., 101., 99.])
        n = len(c)
        sx = np.full(n, 5, dtype=np.int64)
        fx = np.full(n, 2, dtype=np.int64)
        never = np.full(n+1, n, dtype=np.int64)
        args = (np.array([0], dtype=np.int64), np.array([1], dtype=np.int8), np.arange(n, dtype=np.int32),
                sx, sx, np.full(n, 99.), np.full(n, 99.), fx, fx,
                np.full(n, 101.), np.full(n, 99.), .5, 1, never, never, 0., 0.,
                c, c+1, c-.1, np.array([1., 2., 5.]), np.zeros(n, dtype=np.int16), 1)
        fast = A.simulate_partial_sizes(*args, close_confirmed=True)
        slow = A.simulate_partial_sizes(*args, close_confirmed=True, cache_plans=False)
        for a, b in zip(fast, slow):
            np.testing.assert_equal(a, b)
        self.assertEqual(fast[3], 3)
        self.assertAlmostEqual(fast[5], .5 * (.005 - .01))


class FreeUiTests(unittest.TestCase):
    def test_multiple_rounds_fixed_picker_and_scroll_bindings(self):
        from ui import App
        with mock.patch.object(App, 'save_user_settings'):
            app = App()
            try:
                app.withdraw()
                panel = app.selection_panel
                self.assertEqual(len(app.notebook.tabs()), 5)
                panel.case_vars['1m'][1].set(True)
                panel.case_vars['1m'][5].set(True)
                panel.case_vars['1m'][17].set(True)
                panel.fixed_picker.set({'FSL01_01', 'FSL04_02'})
                panel.overlay_picker.set({'OFF', 'FTP02_02'})
                cfg = app.current_selection()
                self.assertTrue({1, 5, 17} <= set(cfg['开仓条件']['1m']))
                self.assertEqual(set(cfg['固定止损代码']), {'FSL01_01', 'FSL04_02'})
                self.assertEqual(set(cfg['叠加止盈代码']), {'OFF', 'FTP02_02'})
                self.assertTrue(panel.entry_canvas.bind('<MouseWheel>'))
                self.assertTrue(panel.entry_canvas.bind('<Down>'))
                self.assertTrue(panel.stop_tree.bind('<MouseWheel>'))
                self.assertTrue(panel.tp_tree.bind('<MouseWheel>'))
                panel.apply_config(cfg)
                self.assertTrue(panel.fixed_picker.variables['FSL01_01'].get())
                self.assertTrue(panel.entry_fold_vars['1m'].get())
            finally:
                for timer in app.tk.call('after', 'info'):
                    app.after_cancel(timer)
                app.destroy()
