import unittest

import backtest_worker as W


class RankingFilterTests(unittest.TestCase):
    def tearDown(self):
        W.应用排行设置({"排行指标": W.默认排行指标, "门槛": []})

    def _row(self, trades, drawdown, final_money):
        row = [0] * len(W.中文表头)
        row[W.中文表头.index("交易次数（单）")] = trades
        row[W.中文表头.index("最大回撤（%）")] = drawdown
        row[W.中文表头.index("期末资金（USDC）")] = final_money
        return row

    def test_multiple_gates_and_smaller_is_better_ranking(self):
        W.应用排行设置({
            "排行指标": "最大回撤（%）越小越好",
            "门槛": [
                {"指标": "交易次数（单）", "条件": "最低值", "值": 10},
                {"指标": "最大回撤（%）越小越好", "条件": "最高值", "值": 0.4},
            ],
        })
        rejected = self._row(9, 0.1, 1000)
        high_drawdown = self._row(20, 0.3, 200)
        low_drawdown = self._row(20, 0.2, 100)
        top = []
        worst = []
        for counter, row in enumerate((rejected, high_drawdown, low_drawdown), 1):
            W.update_top(top, row, counter)
            W.update_worst(worst, row, counter)
        self.assertEqual(len(top), 2)
        self.assertEqual(len(worst), 3)
        self.assertGreater(W.排行键(low_drawdown), W.排行键(high_drawdown))
        self.assertTrue(all(item[2] is not rejected for item in top))

    def test_invalid_smaller_is_better_metric_cannot_rank_first(self):
        W.应用排行设置({"排行指标": "最大回撤（%）越小越好", "门槛": [
            {"指标": "最大回撤（%）越小越好", "条件": "最高值", "值": .4}]})
        for value in (float("nan"), float("inf"), "无数据"):
            row = self._row(20, value, 100)
            self.assertFalse(W.排行行合格(row))
            self.assertEqual(W.排行键(row), float("-inf"))


if __name__ == "__main__":
    unittest.main()
