import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worst_export
from backtest_worker import 中文表头, 全量CSV表头


class LegacyExportTests(unittest.TestCase):
    def test_export_cache_without_temp_environment_stays_in_writable_temp_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "results"
            output.mkdir()
            with (output / "全部回测结果.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerow(全量CSV表头)
            system_temp = root / "system_temp"
            system_temp.mkdir()
            mkdir = Path.mkdir

            def confined_mkdir(path, *args, **kwargs):
                self.assertTrue(path.is_relative_to(root), f"缓存不得写到测试目录之外：{path}")
                return mkdir(path, *args, **kwargs)

            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(tempfile, "gettempdir", return_value=str(system_temp)) as tempdir, \
                 mock.patch.object(Path, "mkdir", confined_mkdir), \
                 mock.patch.object(sys, "argv", ["worst_export.py", "--output", str(output)]), \
                 mock.patch.object(worst_export, "export_excel", side_effect=lambda _project, _payload, target: target), \
                 mock.patch.object(worst_export, "emit"):
                worst_export.main()
                tempdir.assert_called_once()
                runtime_temp = Path(os.environ["TEMP"])
                self.assertEqual(os.environ["TMP"], str(runtime_temp))
                self.assertTrue(runtime_temp.is_relative_to(system_temp))
                self.assertTrue(runtime_temp.is_dir())

    def test_existing_csv_builds_both_rankings_with_complete_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            row = {name: "" for name in 全量CSV表头}
            row.update({
                "止盈方案编号": 1, "止盈后等待分钟": 1, "基础策略编号": 1,
                "开仓MACD代码": 0, "4小时条件代码": 1, "1小时条件代码": 1,
                "15分钟条件代码": 1, "5分钟条件代码": 1, "1分钟条件代码": 1,
                "止损代码": "S1_1m", "交易次数（单）": 10, "胜率（%）": 0.6,
                "多单占比（%）": 0.5, "平均日完整交易数（次/日）": 0.1,
                "平均日成交订单数（笔/日）": 0.2,
                "平均持仓时间（分钟）": 10, "平均单笔收益率（%）": 0.001,
                "毛收益合计（%）": 0.01, "盈亏比（倍）": 1.5, "t值": 2,
                "2025毛收益（%）": 0.01, "2026毛收益（%）": 0.02,
                "所选仓位顺序": "5x", "所选仓位期末资金（USDC）": "200",
                "所选仓位累计收益率（%）": "1", "所选仓位最大回撤（%）": "0.2",
                "所选仓位爆仓保护次数（次）": "0", "开仓成交偏移（%）": 0.0001,
                "平仓成交偏移（%）": 0.0001, "往返成交偏移（%）": 0.0002,
                "初始资金（USDC）": 100, "ETH最小开仓数量（ETH）": 0.01,
                "ETH单次最大开仓数量（ETH）": 100,
                "所选仓位实际成交次数（单）": "10", "所选仓位资金性停机标记（0否1是）": "0",
                "所选仓位期末可开仓数量（ETH）": "0.25",
            })
            source = output / "历史全部回测结果.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=全量CSV表头)
                writer.writeheader(); writer.writerow(row)
                rejected = dict(row)
                rejected["交易次数（单）"] = 5
                rejected["所选仓位期末资金（USDC）"] = "9999"
                rejected["所选仓位最大回撤（%）"] = "0.1"
                writer.writerow(rejected)
            settings = output / "排行榜设置.json"
            settings.write_text(json.dumps({
                "排行指标": "最大回撤（%）越小越好",
                "门槛": [{"指标": "交易次数（单）", "条件": "最低值", "值": 8}],
            }, ensure_ascii=False), "utf-8")
            (output / "断点记录.json").write_text(json.dumps({"top": {}, "worst": {}}), "utf-8")
            with mock.patch.object(sys, "argv", ["worst_export.py", "--output", str(output),
                                                        "--source", str(source), "--settings", str(settings)]), \
                 mock.patch.object(worst_export, "export_excel"):
                worst_export.main()
            top = json.loads((output / "各类止盈前5000名.json").read_text("utf-8"))
            worst = json.loads((output / "各类止盈最差1000名.json").read_text("utf-8"))
            self.assertEqual(top["名额"], 5000)
            self.assertEqual(worst["名额"], 1000)
            self.assertEqual(top["排行指标"], "最大回撤（%）越小越好")
            top_row = next(iter(top["分类"].values()))[0]
            worst_row = next(iter(worst["分类"].values()))[0]
            self.assertEqual(sum(len(rows) for rows in top["分类"].values()), 1)
            self.assertEqual(len(top_row), len(中文表头))
            self.assertEqual(len(worst_row), len(中文表头))
            self.assertEqual([top_row[中文表头.index(name)] for name in (
                "初始资金（USDC）", "ETH最小开仓数量（ETH）", "ETH单次最大开仓数量（ETH）",
                "实际成交次数（单）", "资金性停机标记（0否1是）", "期末可开仓数量（ETH）")],
                [100.0, 0.01, 100.0, 10, 0, 0.25])
            self.assertIsNone(top_row[中文表头.index("实际容量占用率（%）")])


if __name__ == "__main__":
    unittest.main()
