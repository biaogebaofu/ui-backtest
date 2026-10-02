import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import candidate_export
from backtest_worker import 全量CSV表头
from candidate_export import (calculate_capacity, capacity_for_row, cost_metrics,
                              minimum_order_was_enabled, preliminary_failures)
from selection_config import 规范化候选筛选


class CandidateExportTests(unittest.TestCase):
    def test_capacity_uses_completed_trades_not_order_count(self):
        self.assertAlmostEqual(calculate_capacity(12.0, 30.0, 10), 1.0 / 3.0)

    def test_actual_capacity_takes_precedence_over_legacy_estimate(self):
        value, basis = capacity_for_row({"实际容量占用率（%）": "0.3"}, 100., 30)
        self.assertEqual(value, .3)
        self.assertIn("逐账户", basis)
        value, basis = capacity_for_row({"平均持仓时间（分钟）": 30}, 12., 10)
        self.assertAlmostEqual(value, 1/3)
        self.assertIn("估算", basis)

    def test_invalid_actual_capacity_is_not_clamped_or_accepted(self):
        for value in (1.1, -1., "nan"):
            with self.assertRaises(ValueError):
                capacity_for_row({"实际容量占用率（%）": value}, 0., 0)

    def test_minimum_quantity_parameter_alone_does_not_prove_it_was_enforced(self):
        row = {"ETH最小开仓数量（ETH）": .01}
        self.assertFalse(minimum_order_was_enabled(row))
        row["ETH最小开仓约束启用（0否1是）"] = "0"
        self.assertFalse(minimum_order_was_enabled(row))
        row["ETH最小开仓约束启用（0否1是）"] = "1"
        self.assertTrue(minimum_order_was_enabled(row))

    def test_cost_pressure_reconstructs_gross_before_applied_slippage(self):
        values = cost_metrics(0.0010, 0.0002, 0.0005, 0.0008)
        self.assertAlmostEqual(values["gross_before_cost"], 0.0012)
        self.assertAlmostEqual(values["p95_net"], 0.0007)
        self.assertAlmostEqual(values["extreme_net"], 0.0004)
        self.assertAlmostEqual(values["cost_ratio"], 6.0)

    def test_available_hard_gates_are_explicit(self):
        settings = 规范化候选筛选({"最低原始交易数": 200})
        self.assertEqual(settings["爆仓保护次数上限"], 10)
        record = {
            "liquidations": 0, "forced_liquidations": 0, "mdd": 0.20, "y2025": 0.1, "y2026": 0.1,
            "profit_factor": 1.5, "p95_net": 0.0001, "cost_ratio": 3.0,
            "gross_before_cost": 0.001, "capacity": 0.30, "long_share": 0.50, "trades": 300,
            "minimum_order_verified": True, "capital_stop": 0,
        }
        self.assertEqual(preliminary_failures(record, settings, {2025, 2026}), [])
        record["mdd"] = 0.50
        self.assertIn("最大回撤超过硬上限", preliminary_failures(record, settings, {2025, 2026}))
        record["mdd"] = 0.20
        record["liquidations"] = 11
        self.assertIn("爆仓保护次数超过上限", preliminary_failures(record, settings, {2025, 2026}))
        record["liquidations"] = 0
        record["forced_liquidations"] = 1
        self.assertIn("发生全仓强平", preliminary_failures(record, settings, {2025, 2026}))

    def test_minimum_order_capital_stop_is_hard_failure(self):
        settings = 规范化候选筛选(None)
        record = {
            "liquidations": 0, "forced_liquidations": 0, "mdd": 0.20, "y2025": 0.1, "y2026": 0.1,
            "profit_factor": 1.5, "p95_net": 0.0001, "cost_ratio": 3.0,
            "gross_before_cost": 0.001, "capacity": 0.30, "long_share": 0.50, "trades": 300,
            "minimum_order_verified": True, "capital_stop": 1,
        }
        self.assertIn("资金不足ETH最小开仓数量", preliminary_failures(record, settings, {2025, 2026}))

    def test_new_csv_fields_flow_through_candidate_export(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            row = {name: "" for name in 全量CSV表头}
            row.update({
                "止盈方案编号": 1, "止盈后等待分钟": 1, "基础策略编号": 1,
                "开仓MACD代码": 0, "4小时条件代码": 1, "1小时条件代码": 1,
                "15分钟条件代码": 1, "5分钟条件代码": 1, "1分钟条件代码": 1,
                "固定止损代码": "FSL04_04", "叠加止盈代码": "OVERLAY_TEST",
                "开仓方向": "LONG", "交易会话": "ASIA", "强制时间止损（分钟）": 15,
                "止损代码": "S1", "交易次数（单）": 300, "胜率（%）": 0.6,
                "多单占比（%）": 0.5, "平均持仓时间（分钟）": 10,
                "平均单笔收益率（%）": 0.001, "盈亏比（倍）": 1.5, "t值": 4,
                "2025毛收益（%）": 0.1, "2026毛收益（%）": 0.1,
                "所选仓位顺序": "5x", "所选仓位期末资金（USDC）": "200",
                "所选仓位最大回撤（%）": "0.2", "所选仓位爆仓保护次数（次）": "0",
                "往返成交偏移（%）": 0.0001, "初始资金（USDC）": 100,
                "ETH最小开仓数量（ETH）": 0.01, "ETH单次最大开仓数量（ETH）": 100,
                "所选仓位实际成交次数（单）": "300",
                "所选仓位资金性停机标记（0否1是）": "0", "所选仓位期末可开仓数量（ETH）": "0.25",
                "ETH最小开仓约束启用（0否1是）": 1,
            })
            with (output / "全部回测结果.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=全量CSV表头)
                writer.writeheader(); writer.writerow(row)
                # 同一基础策略号但强制平仓不同，必须保留独立指纹。
                second = dict(row); second["强制时间止损（分钟）"] = 30
                second["ETH最小开仓约束启用（0否1是）"] = 0
                second["初始资金（USDC）"] = 777
                second["所选仓位期末资金（USDC）"] = "1554"
                writer.writerow(second)
                writer.writerow(row)  # 完全相同的重复行只保留一次。
            with (output / "止盈方案字典.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "止盈方案编号", "止盈类别", "周期组合", "指标", "参数一（原始小数）",
                    "参数二（原始小数）", "参数三（原始小数）", "完整说明（参数含义及单位）"])
                writer.writeheader(); writer.writerow({"止盈方案编号": 1, "止盈类别": "测试止盈"})
            settings_path = output / "设置.json"
            settings_path.write_text(json.dumps(规范化候选筛选({"统一目标杠杆": 5.0})), "utf-8")
            cache = output / "缓存"
            cache.mkdir()
            (cache / "features_meta.json").write_text(json.dumps({
                "start_utc": "2025-01-01T00:00:00", "end_utc": "2026-12-31T00:00:00"}), "utf-8")
            with mock.patch.object(sys, "argv", ["candidate_export.py", "--output", str(output), "--settings", str(settings_path)]), \
                 mock.patch.object(candidate_export, "export_excel"):
                candidate_export.main()
            with (output / "每类研究候选_5x.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                exports = list(csv.DictReader(handle))
            disabled = next(r for r in exports if r["强制时间止损（分钟）"] == "30")
            self.assertEqual(float(disabled["初始资金（USDC）"]), 777)
            self.assertTrue(disabled["最小/最大下单量约束已验证"].startswith("否"))
            exported = next(r for r in exports if r["强制时间止损（分钟）"] == "15")
            self.assertEqual(len(exports), 2)
            self.assertEqual(len({x["策略指纹"] for x in exports}), 2)
            self.assertEqual(exported["固定止损代码"], "FSL04_04")
            self.assertEqual(exported["叠加止盈代码"], "OVERLAY_TEST")
            self.assertEqual(exported["开仓方向"], "LONG")
            self.assertEqual(exported["交易会话"], "ASIA")
            self.assertEqual(exported["资金性停机标记（0否1是）"], "0")
            self.assertEqual(exported["最小/最大下单量约束已验证"], "是")
            self.assertEqual(float(exported["ETH单次最大开仓数量（ETH）"]), 100.0)


if __name__ == "__main__":
    unittest.main()
