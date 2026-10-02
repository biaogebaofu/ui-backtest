"""Real CPU worker: zero-side mode survives execution, output and resume."""
import csv
import json
import os
import subprocess
import sys
import unittest

import test_v147_worker_execution as fixture


class CycleWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FourthBatchWorkerExecutionTests.prepare_fixture.__func__(cls)
        cls.full = cls.run_worker("full")
        cls.stopped = cls.run_worker("resume", stop=True)
        cls.resumed = cls.run_worker("resume")

    @classmethod
    def run_worker(cls, name, stop=False, mode="MACD_CYCLE", expect_error=False):
        output = cls.root / name
        selection = {
            "版本": 20, "开仓指标": ["hist", "dif"],
            "开仓条件": {"4h": [0], "1h": [0], "15m": [0], "5m": [0], "1m": [156]},
            "开仓位置过滤": ["OFF", "EMA5_ATR100"],
            "止损代码": ["S1"], "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
            "开仓方向": ["BOTH"], "交易会话": ["ALL"], "强制时间止损分钟": [0],
            "止盈方案编号": [2, 2421], "止盈后等待分钟": [0], "仓位倍数": [1., 2.],
            "成交偏移": {"开仓": 0.00005, "平仓": 0.00005},
            "成本模式": "FEE", "平仓后最小开仓间隔分钟": 5,
            "手续费": {"开仓费率": .0004, "平仓费率": .0004, "BNB抵扣": True, "返佣比例": .3},
            "入场约束": {"最小S3距离": 0., "S3基线周期": "1m"},
            "资金约束": {"初始资金USDC": 1000., "最小开仓数量ETH": .01,
                     "最大开仓数量ETH": 20., "低于最小数量停止": True},
            "入场触发口径": mode, "成交价格口径": "CLOSE_CONFIRMED",
            "候选筛选": {"启用": False, "自动导出": False, "导出旧排行": False},
        }
        config = cls.root / (name + ".json")
        config.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TEMP=str(cls.root),
                   TMP=str(cls.root), NUMBA_CACHE_DIR=str(cls.root / "numba"),
                   OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
        program = [sys.executable, "-B", str(cls.project / "backtest_worker.py")]
        if stop:
            driver = (
                "import sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(cls.project)!r})\n"
                "import backtest_worker as W\noriginal = W.emit\n"
                "from itertools import count\nstop_clock = count()\nStopCheck = W.停止检查\n"
                "W.停止检查 = lambda path: StopCheck(path, clock=lambda: float(next(stop_clock)))\n"
                "def emit(event, **values):\n"
                "    original(event, **values)\n"
                "    if event == 'progress' and values.get('tp') == 1:\n"
                f"        Path({str(output / '控制' / '停止.flag')!r}).write_text('stop')\n"
                "W.emit = emit\nW.main()\n"
            )
            program = [sys.executable, "-B", "-c", driver]
        result = subprocess.run(program + [
            "--csv", str(cls.csv), "--output", str(output), "--selection", str(config),
            "--threads", "1", "--device", "cpu", "--cache-root", str(cls.root / "features"),
        ], env=env, cwd=cls.root, capture_output=True, text=True, encoding="utf-8", timeout=120)
        if expect_error:
            return result
        if result.returncode:
            raise AssertionError(result.stdout[-3000:] + result.stderr[-3000:])
        with (output / "全部回测结果.csv").open(encoding="utf-8-sig", newline="") as stream:
            raw = list(csv.reader(stream))
        return {"raw": raw, "rows": [dict(zip(raw[0], row)) for row in raw[1:]],
                "checkpoint": json.loads((output / "断点记录.json").read_text("utf-8")),
                "notes": (output / "中文字段说明.txt").read_text("utf-8"), "log": result.stdout}

    def test_cycle_and_position_sweep_reach_both_tp_paths_and_fields(self):
        rows = self.full["rows"]
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["开仓MACD代码"] for r in rows}, {"0", "1"})
        self.assertEqual({r["开仓位置过滤代码"] for r in rows}, {"OFF", "EMA5_ATR100"})
        self.assertEqual({r["止盈方案编号"] for r in rows}, {"2", "2421"})
        self.assertTrue(all(r["入场触发口径"] == "MACD_CYCLE" for r in rows))
        self.assertTrue(all(int(r["交易次数（单）"]) > 0 for r in rows))
        for row in rows:
            self.assertEqual(row["成本模式"], "FEE")
            self.assertEqual(float(row["往返成交偏移（%）"]), 0.)
            self.assertAlmostEqual(float(row["开仓净手续费率（%）"]), .0004 * .9 * .7)
            self.assertEqual(float(row["平仓后最小开仓间隔（分钟）"]), 5.)
        self.assertTrue(all(len(row) == len(self.full["raw"][0]) for row in self.full["raw"]))
        self.assertEqual(self.full["checkpoint"]["selection"]["入场触发口径"], "MACD_CYCLE")
        self.assertIn("MACD_CYCLE", self.full["notes"])
        self.assertIn("零值延续", self.full["notes"])
        self.assertIn('"completed"', self.full["log"])

    def test_interrupted_cycle_resume_matches_uninterrupted_csv(self):
        self.assertIn('"stopped"', self.stopped["log"])
        self.assertEqual(len(self.stopped["rows"]), 4)
        self.assertEqual(self.stopped["checkpoint"]["next_tp"], 1)
        self.assertEqual(self.full["raw"], self.resumed["raw"])
        self.assertEqual(self.resumed["checkpoint"]["rows"], 8)

    def test_changing_mode_cannot_mix_new_results_into_cycle_directory(self):
        output = self.root / "full"
        before = {name: (output / name).read_bytes() for name in
                  ("全部回测结果.csv", "断点记录.json", "回测运行身份.json")}
        result = self.run_worker("full", mode="LIVE_01", expect_error=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("禁止新旧结果混写", result.stdout + result.stderr)
        for name, contents in before.items():
            self.assertEqual((output / name).read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
