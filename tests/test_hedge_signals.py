"""Focused tests for the adapter, calendar, output identity and overview values."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hedge_config import normalize_config
from hedge_signals import plan_entries, fifth_tasks
from hedge_worker import dynamic_tp, summarize, case_identity, export_excel


def selection():
    return {"开仓指标": ["hist", "dif"], "开仓条件": {"1m": [0, 42], "5m": [0, 42]},
            "入场触发口径": "LIVE_01", "入场约束": {"最小S3距离": .001, "S3基线周期": "1m"}}


META = {"capabilities": {"ohlcv": True}, "capabilities_by_timeframe": {"5m": {"ohlcv": True}}}


class AdapterTests(unittest.TestCase):
    def test_independent_cases_not_cartesian(self):
        entries, skipped = plan_entries(selection(), META, True)
        self.assertEqual(len(entries), 5)
        self.assertFalse(skipped)
        self.assertTrue(all(sum(code != 0 for code in row["cases"]) == 1 for row in entries[1:]))

    def test_random_only_ignores_unrelated_old_settings(self):
        entries, skipped = plan_entries({}, {}, False)
        self.assertEqual([row["id"] for row in entries], ["RANDOM"])
        self.assertFalse(skipped)

    def test_missing_data_and_retired_rule_are_recorded(self):
        config = selection()
        config["开仓条件"]["1m"] = [128, 264, 42]
        entries, skipped = plan_entries(config, META, True)
        self.assertEqual({row["开仓代码"] for row in skipped}, {128, 264})
        self.assertTrue(any("永久删除" in row["跳过原因"] for row in skipped))
        self.assertTrue(all(row["code"] not in (128, 264) for row in entries))
        self.assertFalse(fifth_tasks(entries))

    def test_fifth_only_mode_does_not_relabel_old_rule(self):
        config = selection()
        config["入场触发口径"] = "F5_EVENT"
        entries, skipped = plan_entries(config, META, True)
        self.assertEqual(len(entries), 1)
        self.assertTrue(skipped)

    def test_calendar_beijing_midnight_holiday_and_weekend(self):
        ts = np.array(["2025-01-01T00:00", "2025-01-02T00:00", "2025-01-03T15:59", "2025-01-03T16:00"],
                      dtype="datetime64[m]").astype("datetime64[ms]").astype(np.int64)
        np.testing.assert_array_equal(dynamic_tp(ts, normalize_config()), [.002, .004, .004, .002])

    def test_unknown_holiday_year_is_rejected(self):
        ts = np.array(["2027-01-01"], dtype="datetime64[ms]").astype(np.int64)
        with self.assertRaisesRegex(ValueError, "2027"):
            dynamic_tp(ts, normalize_config())

    def test_strategy_identity_excludes_seed_includes_execution(self):
        descriptor = plan_entries({}, {}, False)[0][0]
        config = normalize_config()
        identity = case_identity(descriptor, config, 12, .001, 0)
        changed = dict(config, seeds=10, seed_start=50)
        self.assertEqual(identity, case_identity(descriptor, changed, 12, .001, 0))
        self.assertNotEqual(identity, case_identity(descriptor, config, 24, .001, 0))
        self.assertNotEqual(identity, case_identity(descriptor, dict(config, maker_fee_rate=.0002), 12, .001, 0))

    def test_even_count_median_and_infinite_pf_excel(self):
        descriptor = plan_entries({}, {}, False)[0][0]
        rows = [{"ending_equity": value, "max_drawdown": .1, "profit_factor": np.inf,
                 "trades": 2, "win_rate": 1., "daily_trades": 1., "fees": 0., "unrealized_pnl": 0.}
                for value in (100., 300.)]
        result = summarize(descriptor, normalize_config(), 12, .001, 0, rows)
        self.assertEqual(result["期末资金中位数（USDC）"], 200.)
        self.assertEqual(result["利润因子PF中位数"], np.inf)
        with tempfile.TemporaryDirectory(prefix="hedge_export_test_") as folder:
            path = export_excel(Path(folder), [result], [], [], {"说明": "离线测试"})
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True, data_only=True)
            values = list(book["方案汇总"].values)
            overview = dict(zip(values[0], values[1]))
            self.assertEqual(overview["期末资金中位数（USDC）"], 200.)
            self.assertEqual(overview["利润因子PF中位数"], "∞")
            book.close()


if __name__ == "__main__":
    unittest.main()
