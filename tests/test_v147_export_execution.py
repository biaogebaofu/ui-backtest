import csv
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import candidate_export as candidate
import worst_export as worst
from backtest_worker import 中文表头, 全量CSV表头
from selection_config import 规范化候选筛选


def sample_row():
    return {
        "止盈方案编号": 1, "止盈后等待分钟": 1, "基础策略编号": 1,
        "开仓MACD代码": 0, "4小时条件代码": 0, "1小时条件代码": 0,
        "15分钟条件代码": 0, "5分钟条件代码": 0, "1分钟条件代码": 1,
        "入场触发口径": "LIVE_01", "成交价格口径": "CLOSE_CONFIRMED",
        "回测计算版本": "v1.47", "止损代码": "S1_1m", "交易次数（单）": 300,
        "胜率（%）": .6, "多单占比（%）": .5, "平均日完整交易数（次/日）": 1,
        "平均日成交订单数（笔/日）": 2, "平均持仓时间（分钟）": 10,
        "平均单笔收益率（%）": .001, "毛收益合计（%）": .3,
        "盈亏比（倍）": 1.5, "t值": 4, "2025毛收益（%）": .1,
        "2026毛收益（%）": .2, "所选仓位顺序": "5x",
        "所选仓位期末资金（USDC）": "225", "所选仓位累计收益率（%）": "1.25",
        "所选仓位最大回撤（%）": ".2", "所选仓位爆仓保护次数（次）": "0",
        "开仓成交偏移（%）": 0.00005, "平仓成交偏移（%）": 0.00005,
        "往返成交偏移（%）": 0.0001, "初始资金（USDC）": 100,
        "ETH最小开仓数量（ETH）": .01, "ETH单次最大开仓数量（ETH）": 20,
        "所选仓位实际成交次数（单）": "300", "所选仓位资金性停机标记（0否1是）": "0",
        "所选仓位期末可开仓数量（ETH）": ".25", "ETH最小开仓约束启用（0否1是）": 1,
        "平仓后最小开仓间隔（分钟）": 5, "开仓基础手续费率（%）": .0004,
        "平仓基础手续费率（%）": .0004, "BNB手续费抵扣（0否1是）": 1,
        "手续费返佣比例（%）": .3, "开仓净手续费率（%）": .000252,
        "平仓净手续费率（%）": .000252, "成本模式": "LEGACY_COMBINED",
    }


def write_source(output, rows, legacy=False):
    headers = list(dict.fromkeys([*全量CSV表头, *candidate.EXECUTION_COLUMNS]))
    if legacy:
        headers = [x for x in headers if x not in candidate.EXECUTION_COLUMNS]
    source = output / "全部回测结果.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (output / "缓存").mkdir()
    (output / "缓存/features_meta.json").write_text(json.dumps({
        "start_utc": "2025-01-01T00:00:00", "end_utc": "2026-12-31T00:00:00"}), "utf-8")
    return source


class ExecutionExportTests(unittest.TestCase):
    def test_old_csv_execution_settings_default_to_zero(self):
        values = candidate.execution_values({})
        self.assertEqual(values.pop("成本模式"), "SLIPPAGE")
        self.assertEqual(set(values.values()), {0})

    def test_cost_mode_is_text_and_matches_worker_columns(self):
        start = 中文表头.index(candidate.EXECUTION_COLUMNS[0])
        self.assertEqual(tuple(中文表头[start:start + 8]), candidate.EXECUTION_COLUMNS)
        for mode in ("SLIPPAGE", "FEE", "LEGACY_COMBINED"):
            self.assertEqual(candidate.execution_values({"成本模式": mode})["成本模式"], mode)
        self.assertEqual(candidate.execution_values({"开仓净手续费率（%）": .001})["成本模式"], "FEE")
        self.assertEqual(candidate.execution_values({"开仓净手续费率（%）": .001,
                        "往返成交偏移（%）": .001})["成本模式"], "LEGACY_COMBINED")

    def test_cost_stress_preserves_already_deducted_fees(self):
        net = .001 - .000252 - .000252 - 0.0001
        result = candidate.cost_metrics(net, 0.0001, 0.0005, 0.001)
        self.assertAlmostEqual(result["gross_before_cost"], .001 - .000504)
        self.assertAlmostEqual(result["p95_net"], .001 - .000504 - 0.0005)
        self.assertAlmostEqual(result["extreme_net"], .001 - .000504 - 0.001)
        self.assertAlmostEqual(candidate.cost_metrics(net, 0.0001, 0.0001, 0.0001)["p95_net"], net)

    def test_capacity_retains_original_tp_wait_and_excludes_new_gap(self):
        row = {"平均持仓时间（分钟）": 10, "平仓后最小开仓间隔（分钟）": 5}
        value, basis = candidate.capacity_for_row(row, 12, 2)
        self.assertAlmostEqual(value, 12 * 12 / 1440)
        self.assertIn("不含新增全退出间隔", basis)
        self.assertIn("非盘口容量", basis)
        self.assertAlmostEqual(candidate.capacity_for_row(row, 12, 7)[0], 12 * 17 / 1440)
        row["实际容量占用率（%）"] = .22
        value, basis = candidate.capacity_for_row(row, 12, 7)
        self.assertEqual(value, .22)
        self.assertIn("不含新增全退出间隔", basis)
        self.assertIn("非盘口容量", basis)

    def test_candidates_keep_source_fees_gap_and_separate_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            row = sample_row()
            write_source(output, [row, dict(row, **{"平仓后最小开仓间隔（分钟）": 0}),
                                  dict(row, **{"开仓净手续费率（%）": 0})])
            with (output / "止盈方案字典.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["止盈方案编号", "止盈类别"])
                writer.writeheader()
                writer.writerow({"止盈方案编号": 1, "止盈类别": "测试止盈"})
            settings = output / "settings.json"
            settings.write_text(json.dumps(规范化候选筛选({"统一目标杠杆": 5})), "utf-8")
            with mock.patch.object(sys, "argv", ["candidate_export", "--output", str(output), "--settings", str(settings)]), \
                 mock.patch.object(candidate, "export_excel"):
                candidate.main()
            result = json.loads((output / "分层候选数据_5x.json").read_text("utf-8"))
            rows = result["研究候选"]
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({r["策略指纹"] for r in rows}), 3)
            for exported in rows:
                self.assertEqual(exported["目标杠杆期末资金（USDC）"], 225)
                self.assertEqual(exported["平均成本后单笔收益（%）"], .001)
                self.assertAlmostEqual(exported["p95成本后单笔收益（%）"], .001 + 0.0001 - 0.0005)
                self.assertEqual(exported["平仓净手续费率（%）"], .000252)
                self.assertEqual(exported["手续费返佣比例（%）"], .3)
            self.assertIn("已含该次回测手续费", result["成交成本说明"])

    def test_legacy_and_new_rankings_keep_row_width_and_do_not_recharge(self):
        tp = SimpleNamespace(编号=1, 类别="测试止盈", 周期组合="", 指标="测试",
                             参数一=.001, 参数二=.01, 参数三=0)
        for legacy in (True, False):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                write_source(output, [sample_row()], legacy=legacy)
                with mock.patch.object(sys, "argv", ["worst_export", "--output", str(output), "--json-only"]), \
                     mock.patch.object(worst, "生成止盈方案", return_value=[tp]), \
                     mock.patch.object(worst, "生成止损组合", return_value=[]):
                    worst.main()
                for name in ("各类止盈前5000名.json", "各类止盈最差1000名.json"):
                    payload = json.loads((output / name).read_text("utf-8"))
                    row = payload["分类"]["测试止盈"][0]
                    self.assertEqual(len(row), len(payload["表头"]))
                    exported = dict(zip(payload["表头"], row))
                    self.assertEqual(exported["期末资金（USDC）"], 225)
                    self.assertEqual(exported["平均单笔收益率（%）"], .001)
                    self.assertEqual(exported["平仓后最小开仓间隔（分钟）"], 0 if legacy else 5)
                    self.assertEqual(exported["开仓净手续费率（%）"], 0 if legacy else .000252)
                    self.assertEqual(exported["成本模式"], "SLIPPAGE" if legacy else "LEGACY_COMBINED")

    def test_excel_does_not_overwrite_actual_capacity_or_net_return(self):
        source = (Path(__file__).resolve().parents[1] / "candidate_excel_export.mjs").read_text("utf-8")
        self.assertNotIn('formulaColumn(sheet, headers, rows, "容量占用率（%）"', source)
        self.assertNotIn('formulaColumn(sheet, headers, rows, "平均成本后单笔收益（%）"', source)
        self.assertIn('idx["平均成本后单笔收益（%）"]', source)


if __name__ == "__main__":
    unittest.main()
