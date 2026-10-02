import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_space import (生成固定止损档, 固定止损同档代码, 方向列表, 会话列表,
                            强制时间止损档, 生成止损组合, 生成止盈方案, 生成入场组合, 统计组合数)
from extended_rules import ONE_MINUTE_CASE_CODES


class StrategySpaceTests(unittest.TestCase):
    def test_exact_counts(self):
        # 第五轮扩容后仍冻结旧四轮的完整空间，防止新增规则改变历史编号/数量。
        legacy_codes = tuple(c for c in ONE_MINUTE_CASE_CODES if c < 264)
        with patch('strategy_space.ONE_MINUTE_CASE_CODES', legacy_codes):
            counts = 统计组合数()
        entry_count = 2 * 248 * 252 * 261 * 262 * 264
        self.assertEqual(counts["入场组合数"], entry_count)  # 含0；各周期排除无法由完整K线计算的分钟规则
        self.assertEqual(counts["止损组合数"], 6666)  # 152 基础(含OFF) × (不用⑨ + 5个周期)
        self.assertEqual(counts["基础策略数"], entry_count * 6666)
        self.assertEqual(counts["止盈方案数"], 9142)
        self.assertEqual(counts["仓位档数"], 22)
        self.assertEqual(counts["包含仓位完整组合数"], entry_count * 6666 * 9142 * 22)

    def test_entry_generator_uses_each_timeframes_supported_rules(self):
        # Capture the axes without enumerating the enormous Cartesian product.
        with patch("strategy_space.product", return_value=iter(())) as grid:
            self.assertEqual(list(生成入场组合()), [])
        choices = grid.call_args.args
        legacy_choices = [tuple(c for c in codes if c < 264) for codes in choices]
        self.assertEqual(tuple(map(len, legacy_choices)), (248, 252, 261, 262, 264))
        self.assertTrue(all(264 in codes for codes in choices))  # 第五轮首项在各周期可用
        self.assertTrue(all(42 in codes and 154 in codes for codes in choices))
        self.assertNotIn(132, choices[0])  # 15-minute OI cannot use a 4h/1h candle
        self.assertNotIn(132, choices[1])
        self.assertIn(132, choices[2])

    def test_s9_is_an_independent_addon_and_keeps_old_indices(self):
        rows = 生成止损组合()
        self.assertEqual(6666, len(rows))
        # 前151条与旧版顺序完全一致，旧结果的「止损代码」仍能一一对应。
        self.assertEqual("S1", rows[0][0])
        self.assertEqual(151, sum(1 for r in rows[:151] if "S9_" not in r[0]))
        codes = {r[0] for r in rows}
        # ⑨可以跨周期叠加，这是01当前实盘用的组合。
        self.assertIn("S3_15m+S5_15m+S9_5m", codes)
        self.assertIn("S1+S3_1m+S9_4h", codes)
        target = next(r for r in rows if r[0] == "S3_15m+S5_15m+S9_5m")
        self.assertIn("⑨5分钟情况三反转量能充足", target[2])
        self.assertEqual(("S3_15m", "S5_15m", "S9_5m"), target[1])

    def test_no_signal_stop_baseline_exists(self):
        rows = {r[0]: r for r in 生成止损组合()}
        self.assertIn("OFF", rows)
        self.assertEqual((), rows["OFF"][1])           # 没有任何信号止损分量
        self.assertIn("只靠", rows["OFF"][2])
        self.assertIn("OFF+S9_5m", rows)               # ⑨仍可单独叠上去

    def test_analysis_dimensions_are_declared(self):
        self.assertEqual(["BOTH", "LONG", "SHORT"], [c for c, _ in 方向列表])
        codes = [c for c, _ in 会话列表]
        self.assertEqual("ALL", codes[0])
        for code in ("WEEKDAY", "WEEKEND", "ASIA", "EU", "US", "EX_THIN"):
            self.assertIn(code, codes)
        self.assertEqual(0, 强制时间止损档[0])          # 0 = 不限，必须是第一档

    def test_fixed_stop_grid(self):
        rows = 生成固定止损档()
        self.assertEqual(362, len(rows))          # OFF + 工作日19 × 周末19
        self.assertEqual("OFF", rows[0][0])
        codes = {r[0]: r for r in rows}
        self.assertEqual((0.004, 0.004), codes["FSL04_04"][1:3])
        self.assertEqual((0.004, 0.002), codes["FSL04_02"][1:3])
        self.assertIn("同为0.4%", codes["FSL04_04"][3])
        self.assertIn("周末0.2%", codes["FSL04_02"][3])
        self.assertEqual(11, len(固定止损同档代码()))  # 默认仍保留旧10档
        self.assertIn("FSLB001_B001", codes)
        self.assertEqual((0.0001, 0.0001), codes["FSLB001_B001"][1:3])

    def test_stop_codes_unique(self):
        rows = 生成止损组合()
        self.assertEqual(len(rows), 6666)
        self.assertEqual(len({x[0] for x in rows}), 6666)
        for code in ("S4_1m", "S5_5m", "S6_4h", "S1+S3_15m+S4_15m+S5_15m+S6_15m",
                     "S9_5m" if False else "S1+S9_5m", "S3_15m+S5_15m+S9_5m"):
            self.assertIn(code, {x[0] for x in rows})

    def test_take_profit_category_counts(self):
        counts = Counter(x.类别 for x in 生成止盈方案())
        self.assertEqual(counts["固定比例止盈-周末相同"], 19)
        self.assertEqual(counts["固定比例止盈-周末独立"], 361)
        self.assertEqual(counts["MACD反转止盈"], 186)
        self.assertEqual(counts["MA偏离回归止盈"], 930)
        self.assertEqual(counts["前高前低结构止盈"], 124)
        self.assertEqual(counts["经典背离止盈"], 558)
        self.assertEqual(counts["移动止盈"], 874)
        self.assertEqual(counts["均线穿越止盈"], 30)
        self.assertEqual(counts["缩量止盈"], 45)
        self.assertEqual(counts["保本移动止盈"], 100)
        self.assertEqual(counts["分批止盈"], 5860)

    def test_trailing_take_profit_has_point_one_percent_bucket_and_two_branches(self):
        rows = [x for x in 生成止盈方案() if x.类别 == "移动止盈"]
        self.assertEqual({x.指标 for x in rows}, {"最高浮盈回吐比例", "最高价固定回撤"})
        for indicator in ("最高浮盈回吐比例", "最高价固定回撤"):
            self.assertTrue(any(x.指标 == indicator and x.参数一 == 0.001 for x in rows))


if __name__ == "__main__":
    unittest.main()
