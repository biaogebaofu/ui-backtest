import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selection_config import 全选配置, 配置签名, 配置统计, 规范化配置


class SelectionConfigTests(unittest.TestCase):
    def test_full_selection_counts(self):
        original = 全选配置()
        original["开仓条件"] = {tf: [code for code in codes if code < 264]
                                 for tf, codes in original["开仓条件"].items()}
        counts = 配置统计(original)
        # 显式分钟窗口和不可能匹配的UTC时间条件按周期排除。
        entries = 2 * 248 * 252 * 261 * 262 * 264
        self.assertEqual(counts["入场组合数"], entries)
        # 止损组合数 = 151 种信号止损 × 11 档默认勾选的固定比例止损（OFF + 10 档同档）
        self.assertEqual(counts["止损组合数"], 6666 * 11)  # ×1 档强制时间止损(不限)
        self.assertEqual(counts["止盈方案数"], 9142)
        self.assertEqual(counts["等待时间档数"], 31)  # 含0=不等待
        self.assertEqual(counts["仓位档数"], 22)
        self.assertEqual(counts["不含仓位完整组合数"], entries * 6666 * 11 * 9142 * 31)
        self.assertEqual(counts["包含仓位完整组合数"], entries * 6666 * 11 * 9142 * 31 * 22)

    def test_analysis_dimensions_default_to_neutral(self):
        config = 全选配置()
        # 默认只勾中性值，加这三个维度不会把默认组合数放大。
        self.assertEqual(["BOTH"], config["开仓方向"])
        self.assertEqual(["ALL"], config["交易会话"])
        self.assertEqual([0], config["强制时间止损分钟"])
        self.assertIn(0, config["止盈后等待分钟"])      # 0=止盈后不额外等待
        config["开仓方向"] = ["LONG", "SHORT"]
        config["交易会话"] = ["ASIA", "US"]
        config["强制时间止损分钟"] = [60, 240]
        got = 规范化配置(config)
        self.assertEqual(["LONG", "SHORT"], got["开仓方向"])
        self.assertEqual(["ASIA", "US"], got["交易会话"])
        self.assertEqual([60, 240], got["强制时间止损分钟"])
        # 非法值被丢弃后回退中性值，不会静默跑成别的东西。
        config["开仓方向"] = ["不存在"]
        self.assertEqual(["BOTH"], 规范化配置(config)["开仓方向"])

    def test_fixed_stop_defaults_and_selection(self):
        config = 全选配置()
        self.assertIn("OFF", config["固定止损代码"])
        self.assertIn("FSL04_04", config["固定止损代码"])
        # 周末独立的档位不在默认勾选里，避免默认组合数再乘 9 倍。
        self.assertNotIn("FSL04_02", config["固定止损代码"])
        config["固定止损代码"] = ["FSL04_02"]
        self.assertEqual(规范化配置(config)["固定止损代码"], ["FSL04_02"])
        # 非法代码被丢弃后回退到默认集合。
        config["固定止损代码"] = ["不存在的档"]
        self.assertIn("OFF", 规范化配置(config)["固定止损代码"])

    def test_sufficient_and_insufficient_reversal_cases_are_separate(self):
        config = 全选配置()
        for timeframe in ("1m", "5m", "15m", "1h", "4h"):
            self.assertIn(3, config["开仓条件"][timeframe])
            self.assertIn(4, config["开仓条件"][timeframe])

    def test_v5_settings_migrate_old_s4_to_s6_and_open_case4(self):
        old = 全选配置()
        old["版本"] = 5
        old["开仓条件"] = {tf: [3] for tf in old["开仓条件"]}
        old["止损代码"] = ["S4_1m", "S1+S3_5m+S4_5m"]
        migrated = 规范化配置(old)
        for timeframe in migrated["开仓条件"]:
            self.assertEqual(migrated["开仓条件"][timeframe], [3, 4])
        self.assertEqual(migrated["止损代码"], ["S6_1m", "S1+S3_5m+S6_5m"])

    def test_subset_counts(self):
        config = 全选配置()
        config["开仓指标"] = ["hist"]
        config["开仓条件"] = {"4h": [0], "1h": [1], "15m": [2], "5m": [0], "1m": [1]}
        config["止损代码"] = config["止损代码"][:2]
        config["固定止损代码"] = ["OFF"]
        config["止盈方案编号"] = [1, 2, 3]
        config["止盈后等待分钟"] = [1, 30]
        config["仓位倍数"] = [1.0, 2.0]
        counts = 配置统计(config)
        self.assertEqual(counts["不含仓位完整组合数"], 12)
        self.assertEqual(counts["包含仓位完整组合数"], 24)

    def test_signature_is_stable_and_selection_sensitive(self):
        first = 全选配置()
        second = 全选配置()
        self.assertEqual(配置签名(first), 配置签名(second))
        second["止盈方案编号"] = second["止盈方案编号"][:1]
        self.assertNotEqual(配置签名(first), 配置签名(second))

    def test_signature_changes_with_slippage(self):
        first = 全选配置()
        second = 全选配置()
        second["成交偏移"]["平仓"] += 0.000001
        self.assertNotEqual(配置签名(first), 配置签名(second))

    def test_signature_changes_with_minimum_order_constraint(self):
        first = 全选配置()
        second = 全选配置()
        second["资金约束"]["最小开仓数量ETH"] = 0.02
        self.assertNotEqual(配置签名(first), 配置签名(second))

    def test_signature_changes_with_maximum_order_constraint(self):
        first = 全选配置()
        second = 全选配置()
        second["资金约束"]["最大开仓数量ETH"] = 50.0
        self.assertNotEqual(配置签名(first), 配置签名(second))

    def test_default_fund_constraint_is_normalized(self):
        config = 规范化配置(全选配置())
        self.assertEqual(config["资金约束"]["初始资金USDC"], 100.0)
        self.assertEqual(config["资金约束"]["最小开仓数量ETH"], 0.01)
        self.assertEqual(config["资金约束"]["最大开仓数量ETH"], 100.0)
        self.assertTrue(config["资金约束"]["低于最小数量停止"])

    def test_candidate_export_settings_do_not_break_checkpoint_signature(self):
        first = 全选配置()
        second = 全选配置()
        second["候选筛选"]["每类最多"] = 20
        second["候选筛选"]["统一目标杠杆"] = 10.0
        self.assertEqual(配置签名(first), 配置签名(second))

    def test_entry_mode_changes_checkpoint_signature(self):
        first = 全选配置()
        second = 全选配置()
        second["入场触发口径"] = "TF_EVENT"
        self.assertNotEqual(配置签名(first), 配置签名(second))

    def test_empty_section_is_rejected(self):
        config = 全选配置()
        # 信号止损和固定止损都空，才算真的没选止损。
        config["止损代码"] = []
        config["固定止损代码"] = ["OFF"]
        with self.assertRaisesRegex(ValueError, "至少选择一种止损"):
            规范化配置(config)

    def test_fixed_stop_alone_is_a_valid_stop_choice(self):
        config = 全选配置()
        config["止损代码"] = []
        config["固定止损代码"] = ["FSL04_04"]
        got = 规范化配置(config)
        # 只用固定比例止损是合法配置，自动挂上 OFF 基座，不该被拦下。
        self.assertEqual(["OFF"], got["止损代码"])
        self.assertEqual(["FSL04_04"], got["固定止损代码"])

    def test_empty_cooldown_is_rejected(self):
        config = 全选配置()
        config["止盈后等待分钟"] = []
        with self.assertRaisesRegex(ValueError, "至少选择一种止盈后等待"):
            规范化配置(config)


if __name__ == "__main__":
    unittest.main()
