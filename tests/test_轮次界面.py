"""轮次控制变量法的界面冒烟测试。

不进 mainloop，只构造一次窗口，验证：
1. 五个轮次按钮各自算出的组合数正确（只有本轮那一项是全变量）；
2. 等待 0 档在界面上真的存在，并且能存进配置（旧版界面只有 1—30）；
3. apply_round 之后 detect_round 能认回同一轮；
4. 已测最优值确实落在配置里。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from math import prod

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import 轮次基线 as 基线  # noqa: E402
from selection_config import 时间条件
from fifth_batch import FIFTH_SPECS, fifth_unavailable_reason

try:
    import tkinter as tk  # noqa: F401
    from ui import App
    有界面 = True
except Exception:  # pragma: no cover - 无显示环境
    有界面 = False


预期组合数 = {"A": 2 * prod(sum(c not in FIFTH_SPECS or not fifth_unavailable_reason(c, tf)
                                    for c in codes) for tf, codes in 时间条件.items()),
              "B": 6666, "C": 9142, "D": 31, "E": 22}


@unittest.skipUnless(有界面, "没有可用的图形环境")
class 轮次界面测试(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_patch = mock.patch.object(App, "save_user_settings")
        cls.settings_patch.start()
        cls.load_patch = mock.patch.object(App, "load_user_settings")
        cls.load_patch.start()
        cls.app = App()
        cls.app.withdraw()          # 不弹窗，只建控件树
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.settings_patch.stop()
        cls.load_patch.stop()

    def test_等待0档存在(self):
        self.assertIn(0, self.app.selection_panel.cooldown_vars,
                      "界面缺少『0分钟不等待』档，后端却一直支持")
        self.assertEqual(sorted(self.app.selection_panel.cooldown_vars), list(range(0, 31)))

    def test_等待0档能进配置(self):
        panel = self.app.selection_panel
        panel._set_cooldowns({0})
        self.assertEqual(panel.get_config()["止盈后等待分钟"], [0])

    def test_每轮只放开一个环节(self):
        for code, expect in 预期组合数.items():
            with self.subTest(轮次=code):
                counts = self.app.selection_panel.round_preview(code)
                self.assertEqual(counts["包含仓位完整组合数"], expect)

    def test_基线单点只有一组(self):
        counts = self.app.selection_panel.round_preview("基线")
        self.assertEqual(counts["包含仓位完整组合数"], 1)

    def test_切轮之后能认回来(self):
        panel = self.app.selection_panel
        for code in 预期组合数:
            with self.subTest(轮次=code):
                panel.apply_round(code)
                self.assertEqual(panel.detect_round(), code)

    def test_锁定值就是已测最优(self):
        panel = self.app.selection_panel
        panel.apply_round("A")           # A轮只放开开仓，其余四项应为最优
        config = panel.get_config()
        self.assertEqual(config["止损代码"], 基线.已测最优["止损代码"])
        self.assertEqual(config["止盈方案编号"], 基线.已测最优["止盈方案编号"])
        self.assertEqual(config["止盈后等待分钟"], 基线.已测最优["止盈后等待分钟"])
        self.assertEqual(config["仓位倍数"], 基线.已测最优["仓位倍数"])
        # 本轮放开的开仓维度必须是全量
        self.assertEqual(sorted(config["开仓指标"]), ["dif", "hist"])

    def test_B轮锁定开仓为最优口径(self):
        panel = self.app.selection_panel
        panel.apply_round("B")
        config = panel.get_config()
        self.assertEqual(config["开仓指标"], 基线.已测最优["开仓指标"])
        self.assertEqual(len(config["止损代码"]), 6666)


class 基线数据测试(unittest.TestCase):
    def test_最优值在合法取值内(self):
        from strategy_space import 仓位列表, 生成止损组合, 生成止盈方案
        codes = {c for c, _, _ in 生成止损组合()}
        ids = {t.编号 for t in 生成止盈方案()}
        self.assertTrue(set(基线.已测最优["止损代码"]) <= codes)
        self.assertTrue(set(基线.已测最优["止盈方案编号"]) <= ids)
        self.assertTrue(set(基线.已测最优["仓位倍数"]) <= set(仓位列表))
        self.assertTrue(set(基线.已测最优["开仓指标"]) <= {"hist", "dif"})
        self.assertTrue(set(基线.已测最优["止盈后等待分钟"]) <= set(range(0, 31)))

    def test_止盈1358就是用户说的移动止盈(self):
        from strategy_space import 生成止盈方案
        tp = {t.编号: t for t in 生成止盈方案()}[1358]
        self.assertEqual(tp.类别, "移动止盈")
        self.assertAlmostEqual(tp.参数一, 0.001)
        self.assertAlmostEqual(tp.参数二, 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
