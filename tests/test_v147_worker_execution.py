"""Bounded real-worker regressions using generated prices and order flow."""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


class FourthBatchWorkerExecutionTests(unittest.TestCase):
    @classmethod
    def prepare_fixture(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="v147_fourth_worker_")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.project = Path(__file__).resolve().parents[1]
        rng = np.random.default_rng(20260909)
        count = 4600
        close = 2000 * np.exp(np.cumsum(rng.normal(0, .0007, count)))
        open_ = np.r_[close[0], close[:-1]]
        volume = rng.lognormal(6, 1, count)
        cls.csv = cls.root / "synthetic.csv"
        pd.DataFrame({
            "openTime": 1735689600000 + np.arange(count) * 60000,
            "open": open_, "high": np.maximum(open_, close) + rng.uniform(.1, 2, count),
            "low": np.minimum(open_, close) - rng.uniform(.1, 2, count),
            "close": close, "volume": volume,
            "trades": rng.integers(1, 10000, count),
            "taker_buy_base": volume * rng.uniform(.01, .99, count),
        }).to_csv(cls.csv, index=False)

    @classmethod
    def setUpClass(cls):
        cls.prepare_fixture()
        cls.runs = {}
        for name, mode, gap, fee in (("baseline", "FEE", 0, 0.),
                                     ("fees", "FEE", 0, .0004),
                                     ("gap5", "FEE", 5, .0004),
                                     ("slippage", "SLIPPAGE", 0, .0004)):
            cls.runs[name] = cls.run_worker(name, mode, gap, fee)

    @classmethod
    def run_worker(cls, name, mode, gap, fee, positions=None, stop_after_first_tp=False):
        output = cls.root / name
        selection = {
            "版本": 19, "开仓指标": ["hist"],
            "开仓条件": {"4h": [0], "1h": [0], "15m": [0], "5m": [0],
                     "1m": [154, 156, 263]},
            "止损代码": ["S1"], "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
            "开仓方向": ["BOTH"], "交易会话": ["ALL"], "强制时间止损分钟": [0],
            "止盈方案编号": [2, 2421], "止盈后等待分钟": [0], "仓位倍数": [1., 2.],
            "成交偏移": {"开仓": 0.00005, "平仓": 0.00005},
            "成本模式": mode, "平仓后最小开仓间隔分钟": gap,
            "手续费": {"开仓费率": fee, "平仓费率": fee, "BNB抵扣": True, "返佣比例": .3},
            "入场约束": {"最小S3距离": 0., "S3基线周期": "1m"},
            "资金约束": {"初始资金USDC": 1000., "最小开仓数量ETH": .01,
                     "最大开仓数量ETH": 20., "低于最小数量停止": True},
            "入场触发口径": "LIVE_01", "成交价格口径": "CLOSE_CONFIRMED",
            "候选筛选": {"启用": False, "自动导出": False, "导出旧排行": False},
        }
        if positions is not None:
            selection["开仓位置过滤"] = positions
        config = cls.root / (name + ".json")
        config.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TEMP=str(cls.root),
                   TMP=str(cls.root), NUMBA_CACHE_DIR=str(cls.root / "numba"),
                   OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
        program = [sys.executable, "-B", str(cls.project / "backtest_worker.py")]
        if stop_after_first_tp:
            # Exercise the real control flag at a deterministic checkpoint boundary.
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
        if result.returncode:
            raise AssertionError(result.stdout[-3000:] + result.stderr[-3000:])
        with (output / "全部回测结果.csv").open(encoding="utf-8-sig", newline="") as stream:
            raw = list(csv.reader(stream))
        return {"raw": raw, "rows": [dict(zip(raw[0], row)) for row in raw[1:]],
                "checkpoint": json.loads((output / "断点记录.json").read_text("utf-8")),
                "dictionary": json.loads((output / "开仓扩展规则字典.json").read_text("utf-8")),
                "log": result.stdout, "mode": mode, "gap": gap, "fee": fee}

    def test_fourth_rules_reach_both_account_paths_and_complete(self):
        for run in self.runs.values():
            self.assertEqual(len(run["rows"]), 6)
            self.assertEqual({int(row["1分钟条件代码"]) for row in run["rows"]}, {154, 156, 263})
            self.assertEqual({int(row["止盈方案编号"]) for row in run["rows"]}, {2, 2421})
            self.assertTrue(all(int(row["交易次数（单）"]) > 0 for row in run["rows"]))
            self.assertEqual(run["checkpoint"]["rows"], 6)
            self.assertIn('"completed"', run["log"])

    def test_all_fourth_names_and_batch_numbers_are_exported(self):
        from extended_rules import ENTRY_RULES
        for run in self.runs.values():
            fourth = {int(code): value for code, value in run["dictionary"].items()
                      if value["批次"] == 4}
            self.assertEqual(set(fourth), set(range(144, 264)))
            for code, value in fourth.items():
                self.assertEqual(value["名称"], ENTRY_RULES[code][0])

    def test_cost_modes_are_mutually_exclusive_in_actual_csv(self):
        for run in self.runs.values():
            header = run["raw"][0]
            import backtest_worker as W
            cost_start = header.index(W.执行成本表头[0])
            self.assertEqual(header[cost_start:cost_start + len(W.执行成本表头)], W.执行成本表头)
            for raw in run["raw"][1:]:
                self.assertEqual(len(raw), len(header))
            for row in run["rows"]:
                self.assertEqual(row["成本模式"], run["mode"])
                self.assertEqual(float(row["平仓后最小开仓间隔（分钟）"]), run["gap"])
                expected_fee = run["fee"] * .9 * .7 if run["mode"] == "FEE" else 0.
                self.assertAlmostEqual(float(row["开仓净手续费率（%）"]), expected_fee)
                self.assertAlmostEqual(float(row["平仓净手续费率（%）"]), expected_fee)
                expected_slip = 0.0001 if run["mode"] == "SLIPPAGE" else 0.
                self.assertAlmostEqual(float(row["往返成交偏移（%）"]), expected_slip)
                from account_statistics import ENGINE_VERSION
                self.assertIn(ENGINE_VERSION, row["回测计算版本"])
            selected = run["checkpoint"]["selection"]
            self.assertEqual(selected["成本模式"], run["mode"])
            self.assertEqual(selected["平仓后最小开仓间隔分钟"], run["gap"])

    def test_selected_cost_reduces_capital_for_both_take_profit_paths(self):
        def by_key(name):
            return {(r["1分钟条件代码"], r["止盈方案编号"]): r for r in self.runs[name]["rows"]}
        baseline = by_key("baseline")
        for name in ("fees", "slippage"):
            for key, paid in by_key(name).items():
                free = baseline[key]
                self.assertEqual(paid["交易次数（单）"], free["交易次数（单）"])
                for before, after in zip(free["所选仓位期末资金（USDC）"].split(";"),
                                         paid["所选仓位期末资金（USDC）"].split(";")):
                    self.assertLess(float(after), float(before))


if __name__ == "__main__":
    unittest.main()
