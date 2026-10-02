import os
import unittest
from unittest import mock
from copy import deepcopy
import numpy as np
from research_signals import ma_cross, volume_fade
from second_round import test_groups as research_groups, group_config
from selection_config import 全选配置, 规范化配置, 规范化候选筛选, 配置统计, 配置签名
from strategy_space import 生成止盈方案, 生成止损组合, 生成入场组合


class ResearchTests(unittest.TestCase):
    def test_cross_is_event_and_waits_for_confirmation(self):
        close = np.array([11., 9., 8., 7., 11., 12., 13.])
        long, short = ma_cross(close, np.full(7, 10.), 2)
        self.assertEqual(np.flatnonzero(long).tolist(), [2])
        self.assertEqual(np.flatnonzero(short).tolist(), [5])

    def test_volume_uses_previous_window_not_current_bar(self):
        volume = np.array([10.] * 20 + [5., 1., 1.])
        np.testing.assert_array_equal(np.flatnonzero(volume_fade(volume, .5, 1)), [20, 21, 22])
        np.testing.assert_array_equal(np.flatnonzero(volume_fade(volume, .5, 2)), [21, 22])
        self.assertFalse(volume_fade(np.zeros(40), .5, 1).any())
        volume[19] = np.nan
        self.assertFalse(volume_fade(volume, .5, 1).any())

    def test_future_candles_cannot_change_prior_events(self):
        rng = np.random.default_rng(4)
        close = rng.normal(10, 2, 100)
        ma = rng.normal(10, 1, 100)
        volume = rng.uniform(0, 20, 100)
        for n in (21, 36, 75):
            for k in (1, 2, 3):
                full = ma_cross(close, ma, k)
                prefix = ma_cross(close[:n], ma[:n], k)
                for a, b in zip(full, prefix):
                    np.testing.assert_array_equal(a[:n], b)
                np.testing.assert_array_equal(volume_fade(volume, .7, k)[:n], volume_fade(volume[:n], .7, k))

    def test_groups_are_valid_and_do_not_mutate_saved_selection(self):
        raw = 全选配置()
        before = deepcopy(raw)
        groups = research_groups()
        self.assertEqual(len(groups), 30)
        for group in groups:
            selected = group_config(raw, group)
            cfg = 规范化配置(selected)
            self.assertEqual(cfg["止盈方案编号"], selected["止盈方案编号"])
            self.assertGreater(配置统计(cfg)["包含仓位完整组合数"], 0)
        self.assertEqual(raw, before)

    def test_baseline_really_is_1370_and_two_x(self):
        cfg = 规范化配置(group_config(全选配置(), research_groups()[0]))
        from extended_rules import stable_base_id
        stops = 生成止损组合()
        stop_idx = [row[0] for row in stops].index(cfg["止损代码"][0])
        self.assertEqual(stable_base_id(0, (0, 0, 0, 0, 1), stop_idx), 1370)
        self.assertEqual(cfg["仓位倍数"], [2.])
        self.assertEqual(cfg["固定止损代码"], ["FSL10_10"])
        self.assertEqual(cfg["叠加止盈代码"], ["FTP10_10"])
        self.assertEqual(cfg["强制时间止损分钟"], [30])
        self.assertEqual(配置统计(cfg)["包含仓位完整组合数"], 1)

    def test_two_x_candidate_and_new_buckets_have_signatures(self):
        self.assertEqual(规范化候选筛选({"统一目标杠杆": 2.})["统一目标杠杆"], 2.)
        base = 规范化配置(group_config(全选配置(), research_groups()[0]))
        changed = deepcopy(base)
        changed["止盈方案编号"] = [8281]
        self.assertNotEqual(配置签名(base), 配置签名(changed))
        tps = 生成止盈方案()
        self.assertEqual(tps[1357].参数一, .001)
        self.assertEqual(tps[1357].参数二, .05)
        self.assertEqual(tps[-1].编号, 9142)
        self.assertEqual(len({(x.类别, x.周期组合, x.指标, x.参数一, x.参数二, x.参数三) for x in tps}), len(tps))


@unittest.skipUnless(os.environ.get("BT_FEATURES"), "需要本地指标缓存")
class ResearchEngineTests(unittest.TestCase):
    def test_all_new_tp_parameters_reach_engine_and_exit_after_entry(self):
        import backtest_engine as B
        # 覆盖全部新增参数，而非只测试UI里的方案名称。
        for tp in 生成止盈方案()[8280:8425]:
            data = B.simple_tp_data(tp)
            self.assertIsNotNone(data, tp)
            for exit_indices in data[:2]:
                self.assertEqual(len(exit_indices), B.N)
                good = exit_indices < B.N
                self.assertTrue(np.all(exit_indices[good] > np.flatnonzero(good)), tp)


class ResearchUiTests(unittest.TestCase):
    def test_apply_and_disabled_comparison_keep_user_paths(self):
        from ui import App
        with mock.patch.object(App, "save_user_settings"):
            app = App()
            try:
                app.withdraw()
                paths = app.csv_var.get(), app.out_var.get()
                app.candidate_enabled_var.set(False)
                self.assertEqual(str(app.candidate_leverage_box.cget("state")), "disabled")
                with mock.patch("ui.messagebox.askyesno", return_value=True):
                    app.apply_research_group(research_groups()[0])
                cfg = app.current_selection()
                self.assertEqual(cfg["仓位倍数"], [2.])
                self.assertEqual(cfg["候选筛选"]["统一目标杠杆"], 2.)
                self.assertFalse(cfg["候选筛选"]["启用"])
                self.assertEqual((app.csv_var.get(), app.out_var.get()), paths)
                app.candidate_enabled_var.set(True)
                self.assertEqual(app.candidate_scope_var.get(), "ALL_RUN")
                self.assertEqual(str(app.candidate_leverage_box.cget("state")), "disabled")
                app.candidate_scope_var.set("TARGET")
                self.assertEqual(str(app.candidate_leverage_box.cget("state")), "readonly")
            finally:
                for timer in app.tk.call("after", "info"):
                    app.after_cancel(timer)
                app.destroy()
