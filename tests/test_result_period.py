import json
import tempfile
import unittest
from pathlib import Path
from result_period import read_period_info, completed_trades_per_day


class ResultPeriodTests(unittest.TestCase):
    def test_missing_period_is_not_assumed_one_day(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(read_period_info(Path(temp)), (None, set()))
        row = {"交易次数（单）": 1200, "平均日成交单数（单/日）": 4.0}
        self.assertEqual(completed_trades_per_day(row, "移动止盈"), 2.0)

    def test_subday_duration_and_exclusive_end_year(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "回测数据说明.json").write_text(json.dumps({
                "start_utc": "2025-12-31T12:00:00Z", "end_utc": "2026-01-01T00:00:00Z"}), "utf-8")
            self.assertEqual(read_period_info(root), (0.5, {2025}))

    def test_partial_orders_cannot_always_be_divided_by_three(self):
        # 10次完整交易中只有5次触发分批，共25笔委托；2天里应每天5次交易。
        row = {"交易次数（单）": 10, "平均日成交单数（单/日）": 12.5}
        self.assertEqual(completed_trades_per_day(row, "分批止盈", 2), 5.0)
        with self.assertRaisesRegex(ValueError, "不能.*除以3"):
            completed_trades_per_day(row, "分批止盈")
