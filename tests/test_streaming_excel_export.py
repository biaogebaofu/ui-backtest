import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
from streaming_excel_export import export_streaming_xlsx


class StreamingDailyTests(unittest.TestCase):
    def daily_row(self, headers, values, category="移动止盈"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "排行.json"
            payload.write_text(json.dumps({"表头": headers, "分类": {category: [values]},
                                           "初始资金": 1000}, ensure_ascii=False), "utf-8")
            target = root / "排行.xlsx"
            export_streaming_xlsx(payload, target)
            workbook = load_workbook(target, read_only=True, data_only=True)
            values = list(workbook["每日收益测算"].values)[3]
            workbook.close()
            return values

    def test_initial_capital_comes_from_strategy_and_not_fixed_example(self):
        row = self.daily_row(["平均日完整交易数（次/日）", "名义倍数（倍）",
                              "平均单笔收益率（%）", "初始资金（USDC）"],
                             [10, 5, .001, 2000])
        self.assertEqual(row[5], 2000)
        self.assertAlmostEqual(row[2], .05 - 10 * 0.0001 * 5)
        self.assertAlmostEqual(row[6], row[2] * 2000)

    def test_legacy_partial_orders_cannot_be_divided_by_three(self):
        row = self.daily_row(["平均日成交单数（单/日）", "名义倍数（倍）",
                              "平均单笔收益率（%）"], [25, 5, .001], "分批止盈")
        self.assertIsNone(row[1])
        self.assertIsNone(row[9])

