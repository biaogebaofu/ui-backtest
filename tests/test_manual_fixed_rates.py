"""固定比例手填的保存/还原、子进程解码和实际退出价位回归；不启动UI或回测。"""
import ast
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategy_space import (固定比例代码, 百分数转比例, 解析固定比例档, 补全固定比例档,
                            生成固定止损档, 生成叠加止盈档, 固定止损同档代码, 叠加止盈同档代码)
from selection_config import 全选配置, 规范化配置, 配置签名
from fixed_rate_picker import FixedRatePicker


def custom(prefix, weekday="0.123456", weekend="1.25"):
    return 固定比例代码(prefix, 百分数转比例(weekday), 百分数转比例(weekend))


class FixedRateCodes(unittest.TestCase):
    def test_preset_space_and_old_ids_unchanged(self):
        for prefix, rows in (("FSL", 生成固定止损档()), ("FTP", 生成叠加止盈档())):
            self.assertEqual(len(rows), 362)
            self.assertEqual(len({r[0] for r in rows}), 362)
            for row in rows:
                self.assertEqual(解析固定比例档(row[0], prefix), row)
            self.assertEqual(固定比例代码(prefix, .001, .01), prefix + "01_10")
            self.assertEqual(固定比例代码(prefix, .0001, .0009), prefix + "B001_B009")
        self.assertEqual(固定止损同档代码(), ["OFF"] + [f"FSL{i:02d}_{i:02d}" for i in range(1, 11)])
        self.assertEqual(叠加止盈同档代码(), ["OFF"] + [f"FTP{i:02d}_{i:02d}" for i in range(1, 11)])

    def test_custom_percentages_and_labels_round_trip(self):
        for prefix in ("FSL", "FTP"):
            code = custom(prefix)
            self.assertEqual(code, prefix + "P0p123456_P1p25")
            row = 解析固定比例档(code, prefix)
            self.assertEqual(row[1:3], (.00123456, .0125))
            self.assertEqual(row[3], "工作日0.123456%／周末1.25%")
            self.assertEqual(custom(prefix, "1.2500%", "01.25"), prefix + "P1p25_P1p25")

    def test_no_old_tenths_or_basis_point_rounding(self):
        codes = {custom("FSL", rate, rate) for rate in ("0.099999", "0.1", "0.100001", "0.123456", "0.123457")}
        self.assertEqual(len(codes), 5)
        self.assertEqual(custom("FSL", "0.1", "0.1"), "FSL01_01")

    def test_invalid_inputs_and_codes_are_rejected(self):
        for value in ("", "abc", "NaN", "Infinity", "-0.1", "0", "100", "1000", "0,4", "1%%"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                百分数转比例(value)
        for code in ("FTP01_01", "FSL00_01", "FSLB999_01", "FSLP-1_01", "FSLPNaN_01", "FSLP0p100_01", "FSL01_01_extra"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                解析固定比例档(code, "FSL")

    def test_selection_survives_json_and_fingerprint_changes(self):
        raw = 全选配置()
        raw["固定止损代码"] = [custom("FSL")]
        raw["叠加止盈代码"] = [custom("FTP", "1.25", "0.123456")]
        raw["开仓条件"] = {tf: [42] if tf == "1m" else [0] for tf in raw["开仓条件"]}
        raw["止损代码"] = ["OFF"]
        raw["止盈方案编号"] = [1]
        config = 规范化配置(raw)
        restored = 规范化配置(json.loads(json.dumps(config)))
        for key in ("固定止损代码", "叠加止盈代码"):
            self.assertEqual(restored[key], raw[key])
        signature = 配置签名(restored)
        restored["固定止损代码"] = [custom("FSL", "0.123457")]
        self.assertNotEqual(signature, 配置签名(restored))

    def test_invalid_selected_custom_does_not_fall_back_to_off(self):
        raw = 全选配置()
        raw["叠加止盈代码"] = ["FTPPinvalid_01"]
        with self.assertRaises(ValueError):
            规范化配置(raw)

    def test_complete_selected_rows_are_stable(self):
        code = custom("FTP")
        a = 补全固定比例档([code, "FTP01_01", "OFF", code], "FTP")
        b = 补全固定比例档({"OFF", code, "FTP01_01"}, "FTP")
        self.assertEqual(a, b)
        self.assertEqual([r[0] for r in a], ["OFF", "FTP01_01", code])

    def test_fresh_process_can_decode_without_registration(self):
        code = custom("FSL")
        result = subprocess.run([sys.executable, "-c", "import json; from strategy_space import 解析固定比例档; print(json.dumps(解析固定比例档('" + code + "', 'FSL')))"] ,
                                cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8")
        row = json.loads(result.stdout)
        self.assertEqual(row[1:3], [.00123456, .0125])


class FixedRatePickerActions(unittest.TestCase):
    def fake_picker(self):
        chosen = {"OFF"}
        tree = SimpleNamespace(selection_set=lambda _x: None, see=lambda _x: None, selection=lambda: [])
        picker = SimpleNamespace(prefix="FSL", read=lambda: chosen.copy(), chosen_tree=tree,
                                 weekday_var=SimpleNamespace(get=lambda: "0.123456"),
                                 weekend_var=SimpleNamespace(get=lambda: "跟随工作日"))
        def apply(values):
            chosen.clear()
            chosen.update(values or {"OFF"})
        picker.set = apply
        return picker, chosen

    def test_add_multiple_groups_follow_and_independent_weekend(self):
        picker, chosen = self.fake_picker()
        FixedRatePicker._add_manual(picker)
        first = custom("FSL", "0.123456", "0.123456")
        self.assertIn(first, chosen)
        picker.weekend_var.get = lambda: "1.25"
        FixedRatePicker._add_manual(picker)
        self.assertEqual(chosen, {"OFF", first, custom("FSL")})

    def test_remove_selected_only_and_last_restores_explicit_off(self):
        picker, chosen = self.fake_picker()
        chosen.clear()
        chosen.add(custom("FSL"))
        picker.chosen_tree.selection = lambda: [custom("FSL")]
        FixedRatePicker._remove_selected(picker)
        self.assertEqual(chosen, {"OFF"})


class FixedRateExport(unittest.TestCase):
    def test_both_export_paths_keep_custom_codes_and_exact_labels(self):
        import candidate_export as candidate
        import worst_export as worst
        from test_v147_export_execution import sample_row, write_source
        row = sample_row()
        row.update({"固定止损代码": custom("FSL"), "叠加止盈代码": custom("FTP"),
                    "开仓方向": "BOTH", "交易会话": "ALL", "强制时间止损（分钟）": 0})
        tp = SimpleNamespace(编号=1, 类别="自定义档测试", 周期组合="1m", 指标="测试",
                             参数一=.001, 参数二=0., 参数三=0.)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            source = write_source(output, [row])
            with mock.patch.object(sys, "argv", ["worst_export", "--output", str(output), "--json-only"]), \
                 mock.patch.object(worst, "生成止盈方案", return_value=[tp]):
                worst.main()
            payload = json.loads((output / "各类止盈前5000名.json").read_text("utf-8"))
            legacy = dict(zip(payload["表头"], payload["分类"][tp.类别][0]))
            with source.open(encoding="utf-8-sig", newline="") as handle:
                raw = next(csv.DictReader(handle))
            native = candidate.native_rank_row(raw, 0, {"止盈类别": tp.类别, "周期组合": "1m", "指标": "测试",
                                                        "参数一（原始小数）": .001}, days=730)
            for record in (legacy, native):
                self.assertEqual(record["固定止损代码"], custom("FSL"))
                self.assertEqual(record["叠加止盈代码"], custom("FTP"))
                self.assertEqual(record["固定止损说明"], "工作日0.123456%／周末1.25%")
                self.assertEqual(record["叠加止盈说明"], "工作日0.123456%／周末1.25%")


class FixedRateExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只提取生产函数，避免import引擎加载用户市场数据或启动真实任务。
        tree = ast.parse((ROOT / "backtest_engine.py").read_text(encoding="utf-8-sig"))
        names = {"fixed_stop_data", "overlay_tp_data", "first_level_hits", "entry_calendar"}
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                node.decorator_list = []
                nodes.append(node)
        def first_hit(values, targets, above):
            return np.array([next((j for j in range(i + 1, len(values))
                                   if (values[j] >= target if above else values[j] <= target)), len(values))
                             for i, target in enumerate(targets)], dtype=np.int64)
        cls.env = {"np": np, "N": 4, "CLOSE": np.full(4, 100.),
                   "HIGH": np.array([100., 100.13, 101.26, 102.]),
                   "LOW": np.array([100., 99.87, 98.74, 98.]),
                   "IS_WEEKEND": np.array([False, True, False, True]),
                   "E": SimpleNamespace(first_above=lambda a, b: first_hit(a, b, True),
                                        first_below=lambda a, b: first_hit(a, b, False))}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "backtest_engine.py", "exec"), cls.env)

    def test_custom_stop_prices_and_first_hits_use_exact_selected_rates(self):
        _code, wd, we, _label = 解析固定比例档(custom("FSL"), "FSL")
        xl, xs, pl, ps = self.env["fixed_stop_data"](wd, we)
        np.testing.assert_allclose(pl, [99.876544, 98.75, 99.876544, 98.75], rtol=0, atol=1e-12)
        np.testing.assert_allclose(ps, [100.123456, 101.25, 100.123456, 101.25], rtol=0, atol=1e-12)
        self.assertEqual(xl[0], 1)
        self.assertEqual(xs[1], 2)

    def test_custom_overlay_prices_and_first_hits_use_exact_selected_rates(self):
        _code, wd, we, _label = 解析固定比例档(custom("FTP"), "FTP")
        xl, xs, pl, ps = self.env["overlay_tp_data"](wd, we)
        np.testing.assert_allclose(pl, [100.123456, 101.25, 100.123456, 101.25], rtol=0, atol=1e-12)
        np.testing.assert_allclose(ps, [99.876544, 98.75, 99.876544, 98.75], rtol=0, atol=1e-12)
        self.assertEqual(xl[0], 1)
        self.assertEqual(xs[1], 2)

    def test_weekend_chosen_at_utc_entry_time(self):
        timestamps = np.array([np.datetime64(s, "ms").astype(np.int64) for s in
                               ("2026-09-11T23:59:00", "2026-09-12T00:00:00", "2026-09-14T00:00:00")])
        _, weekend = self.env["entry_calendar"](timestamps)
        self.assertEqual(weekend.tolist(), [False, True, False])


if __name__ == "__main__":
    unittest.main()
