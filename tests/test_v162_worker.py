"""Real worker output stays identical when fifth-round methods use processes."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from selection_config import 全选配置, 规范化配置


PROJECT = Path(__file__).resolve().parents[1]


class FifthParallelWorkerTests(unittest.TestCase):
    def test_parallel_worker_matches_original_full_csv_and_reuses_all_method_signals(self):
        import pandas as pd

        with tempfile.TemporaryDirectory(prefix="v162_worker_") as temporary:
            root = Path(temporary)
            x = np.arange(3600)
            close = 2000. + 12 * np.sin(x / 19.) + 3 * np.sin(x / 4.) + .002 * x
            opening = np.r_[close[0], close[:-1]]
            data = root / "synthetic.csv"
            pd.DataFrame({"openTime": 1735689600000 + x * 60000,
                          "open": opening, "high": np.maximum(opening, close) + .7,
                          "low": np.minimum(opening, close) - .7, "close": close,
                          "volume": 100. + x % 23}).to_csv(data, index=False)
            config = 全选配置()
            config.update({"开仓指标": ["hist", "dif"],
                           "开仓条件": {"4h": [0], "1h": [0], "15m": [0],
                                        "5m": [0, 288], "1m": [288, 300, 285]},
                           "止损代码": ["S1"], "固定止损代码": ["OFF"],
                           "叠加止盈代码": ["OFF"], "止盈方案编号": [1, 8281],
                           "止盈后等待分钟": [1], "仓位倍数": [2., 5.],
                           "入场触发口径": "LIVE_01", "成本模式": "FEE"})
            config["入场约束"]["最小S3距离"] = 0.
            config["候选筛选"].update(启用=False, 自动导出=False, 导出旧排行=False)
            selection = root / "selection.json"
            selection.write_text(json.dumps(规范化配置(config), ensure_ascii=False), "utf-8")
            env = os.environ.copy()
            env.pop("BT_FEATURES", None)
            env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       PYTHONDONTWRITEBYTECODE="1")
            script = """import sys
import fifth_precompute
import backtest_worker as worker
# Exercise two real processes regardless of host memory pressure. Resource
# downscaling is covered separately by the worker-budget tests.
fifth_precompute.available_memory = lambda: 8 * 1024**3
fifth_precompute.os.cpu_count = lambda: 2
serial = sys.argv.pop(1) == 'serial'
if serial:
    fifth_precompute.prepare_fifth_signals = lambda *a, **k: None
worker.export_excel = lambda *a, **k: None
worker.main()
"""

            def run(mode):
                output = root / mode
                command = [sys.executable, "-X", "utf8", "-c", script, mode,
                           "--csv", str(data), "--output", str(output),
                           "--selection", str(selection), "--threads", "2",
                           "--device", "cpu", "--cache-root", str(root / "features")]
                result = subprocess.run(command, cwd=PROJECT, env=env, capture_output=True,
                                        text=True, encoding="utf-8", timeout=240)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                with (output / "全部回测结果.csv").open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                return output, rows, events

            baseline, original, _ = run("serial")
            parallel, actual, events = run("parallel")
            self.assertEqual(len(actual), 24)
            self.assertEqual(actual, original)
            self.assertEqual((parallel / "全部回测结果.csv").read_bytes(),
                             (baseline / "全部回测结果.csv").read_bytes())
            self.assertGreater(sum(int(row["交易次数（单）"]) for row in actual), 0,
                               "Equality must exercise actual completed trades")
            progress = [event for event in events if event["type"] == "entry_precompute"]
            self.assertTrue(progress)
            self.assertTrue(all(event["workers"] == 2 for event in progress))
            self.assertTrue(any(len(event["running"]) == 2 for event in progress))
            self.assertEqual((progress[-1]["completed"], progress[-1]["total"], progress[-1]["state"]),
                             (4, 4, "done"))
            signals = list((parallel / "开仓预计算").glob("*/*.npy"))
            self.assertEqual({path.name for path in signals},
                             {"1m_288.npy", "1m_300.npy", "1m_285.npy", "5m_288.npy"})
            for path in signals:
                values = np.load(path, allow_pickle=False)
                self.assertEqual(values.dtype, np.bool_)
                self.assertEqual(values.shape[0], 2)
            self.assertTrue(list((parallel / "第五轮模型审计").glob("*.npz")))


class CachedFifthMappingTests(unittest.TestCase):
    def test_cached_native_high_timeframe_mapping_matches_serial_without_recalculating(self):
        from test_support import prepare_test_features
        prepare_test_features()
        import backtest_engine as B

        original_cache = B._fifth_signal_cache
        B.set_fifth_signal_cache(None)
        self.addCleanup(B.set_fifth_signal_cache, original_cache)
        n = 11
        long = np.array([True, False]); short = np.array([False, True])
        data = {f"5m_{key}": np.array([100., 101.])
                for key in ("open", "high", "low", "close", "volume", "hist", "ma20")}
        data.update({"ct1": np.arange(1, n + 1) * 60000,
                     "5m_ct": np.array([5, 10]) * 60000,
                     "5m_map": np.array([-1, -1, -1, -1, 0, 0, 0, 0, 0, 1, 1])})
        with tempfile.TemporaryDirectory(prefix="v162_mapping_") as temporary, \
                mock.patch.object(B, "N", n), mock.patch.object(B.E, "D", data):
            with mock.patch.object(B, "entry_masks", return_value=(long, short)):
                original = B.extended_entry_data("5m", "hist", 288)
            np.save(Path(temporary) / "5m_288.npy", np.array([long, short]))
            B.set_fifth_signal_cache(temporary)
            with mock.patch.object(B, "entry_masks", side_effect=AssertionError("recalculated model")) as compute:
                cached = B.extended_entry_data("5m", "hist", 288)
                alternate = B.extended_entry_data("5m", "dif", 288)
            compute.assert_not_called()
            self.assertIs(cached, alternate)
            for expected, actual in zip(original, cached):
                np.testing.assert_array_equal(expected, actual)
            self.assertEqual(np.flatnonzero(cached[0]).tolist(), [4])
            self.assertEqual(np.flatnonzero(cached[1]).tolist(), [9])


if __name__ == "__main__":
    unittest.main()
