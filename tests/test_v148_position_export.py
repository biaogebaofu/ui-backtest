import csv
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import candidate_export as candidate
from entry_position import POSITION_FILTERS, position_filter_label
from ranking_view import build_view, config_fingerprint
from selection_config import 规范化候选筛选
from test_v147_export_execution import sample_row, write_source
import worst_export as worst


class PositionExportTests(unittest.TestCase):
    def rows(self):
        return [dict(sample_row(), **candidate.position_values({"开仓位置过滤代码": code}))
                for code in POSITION_FILTERS]

    def test_legacy_off_and_execution_columns_remain_distinct(self):
        self.assertEqual(candidate.position_values({}), {
            "开仓位置过滤代码": "OFF", "开仓位置过滤说明": position_filter_label("OFF")})
        row = sample_row()
        self.assertEqual(config_fingerprint(row),
                         config_fingerprint(dict(row, **{"开仓位置过滤代码": "OFF"})))
        self.assertEqual(len(candidate.EXECUTION_COLUMNS), 8)
        self.assertEqual(candidate.execution_values(row)["成本模式"], "LEGACY_COMBINED")

    def test_all_position_variants_survive_candidate_and_cluster_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            rows = self.rows()
            write_source(output, [*rows, rows[0]])
            with (output / "止盈方案字典.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["止盈方案编号", "止盈类别"])
                writer.writeheader()
                writer.writerow({"止盈方案编号": 1, "止盈类别": "测试止盈"})
            settings = output / "settings.json"
            settings.write_text(json.dumps(规范化候选筛选({"统一目标杠杆": 5})), "utf-8")
            with mock.patch.object(sys, "argv", ["candidate_export", "--output", str(output),
                                                "--settings", str(settings)]), \
                 mock.patch.object(candidate, "export_excel"):
                candidate.main()
            result = json.loads((output / "分层候选数据_5x.json").read_text("utf-8"))
            exported = result["研究候选"]
            self.assertEqual(len(exported), len(POSITION_FILTERS))
            for field in ("策略指纹", "相似策略簇", "开仓位置过滤代码"):
                self.assertEqual(len({r[field] for r in exported}), len(POSITION_FILTERS))
            for row in exported:
                self.assertEqual(row["开仓位置过滤说明"], position_filter_label(row["开仓位置过滤代码"]))
                self.assertEqual(row["基础策略编号"], 1)
                self.assertEqual(row["成本模式"], "LEGACY_COMBINED")
                self.assertEqual(row["平均成本后单笔收益（%）"], .001)

    def test_best_and_worst_exports_keep_position_fields_and_legacy_off(self):
        tp = SimpleNamespace(编号=1, 类别="测试止盈", 周期组合="", 指标="测试",
                             参数一=.001, 参数二=.01, 参数三=0)
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                rows = [sample_row()] if legacy else self.rows()
                source = write_source(output, rows)
                if legacy:
                    with source.open(encoding="utf-8-sig", newline="") as handle:
                        reader = csv.DictReader(handle)
                        headers = [name for name in reader.fieldnames if name not in candidate.POSITION_COLUMNS]
                        old_rows = list(reader)
                    with source.open("w", encoding="utf-8-sig", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(old_rows)
                with mock.patch.object(sys, "argv", ["worst_export", "--output", str(output), "--json-only"]), \
                     mock.patch.object(worst, "生成止盈方案", return_value=[tp]), \
                     mock.patch.object(worst, "生成止损组合", return_value=[]):
                    worst.main()
                for name in ("各类止盈前5000名.json", "各类止盈最差1000名.json"):
                    payload = json.loads((output / name).read_text("utf-8"))
                    records = payload["分类"]["测试止盈"]
                    self.assertTrue(all(len(row) == len(payload["表头"]) for row in records))
                    exported = [dict(zip(payload["表头"], row)) for row in records]
                    expected = {"OFF"} if legacy else set(POSITION_FILTERS)
                    self.assertEqual({r["开仓位置过滤代码"] for r in exported}, expected)
                    for row in exported:
                        self.assertEqual(row["开仓位置过滤说明"], position_filter_label(row["开仓位置过滤代码"]))
                        self.assertEqual(row["成本模式"], "LEGACY_COMBINED")
                        self.assertEqual(row["期末资金（USDC）"], 225)
                        self.assertEqual(row["平均单笔收益率（%）"], .001)

    def test_rank_view_shows_position_and_does_not_merge_same_base_id(self):
        rows = self.rows()
        for row in rows:
            row.update({"1分钟条件": "情况一", "开仓MACD口径": "MACD柱",
                        "期末资金（USDC）": 225, "最大回撤（%）": .2})
        self.assertEqual(len({config_fingerprint(row) for row in rows}), len(POSITION_FILTERS))
        headers = list(rows[0])
        payload = {"表头": headers, "分类": {"测试止盈": [[r[k] for k in headers] for r in rows]}}
        view = build_view(payload)
        self.assertEqual(len(view["分类"]["全局前5000"]), len(POSITION_FILTERS))
        pos = view["表头"].index("开仓位置过滤")
        self.assertEqual({r[pos] for r in view["分类"]["全局前5000"]},
                         {position_filter_label(code) for code in POSITION_FILTERS})
        old_headers = [k for k in headers if k not in candidate.POSITION_COLUMNS]
        old_view = build_view({"表头": old_headers,
                               "分类": {"测试止盈": [[rows[0][k] for k in old_headers]]}})
        self.assertEqual(old_view["分类"]["全局前5000"][0][old_view["表头"].index("开仓位置过滤")],
                         position_filter_label("OFF"))


if __name__ == "__main__":
    unittest.main()
