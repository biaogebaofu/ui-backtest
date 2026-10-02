"""实际小样本工作进程：剔除后的配置、断点、身份一致；精确任务只跳过。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

from data_sources import source_bundle_fingerprint
from selection_config import 全选配置, 配置签名


PROJECT = Path(__file__).resolve().parents[1]


class AvailabilityWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="availability_worker_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "bars.csv"
        x = np.arange(1440); close = 2000 + 5 * np.sin(x / 11.)
        open_ = np.r_[close[0], close[:-1]]
        pd.DataFrame({"openTime": 1735689600000 + x * 60000, "open": open_,
                      "high": np.maximum(open_, close) + .4, "low": np.minimum(open_, close) - .4,
                      "close": close, "volume": np.full(1440, 10.)}).to_csv(self.source, index=False)
        self.selection = 全选配置()
        self.selection.update({"开仓指标": ["hist"],
            "开仓条件": {"4h": [0, 128, 129, 130, 131], "1h": [0], "15m": [0], "5m": [0], "1m": [1]},
            "止损代码": ["OFF"], "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
            "强制时间止损分钟": [5], "止盈方案编号": [1], "止盈后等待分钟": [0], "仓位倍数": [1.]})
        self.selection["候选筛选"].update({"启用": False, "自动导出": False, "导出旧排行": False})
        self.output = self.root / "result"; self.output.mkdir()
        self.selection_path = self.output / "组合选择.json"
        self.selection_path.write_text(json.dumps(self.selection, ensure_ascii=False), encoding="utf-8")

    def run_worker(self, exact=False):
        command = [sys.executable, "-X", "utf8", str(PROJECT / "backtest_worker.py"),
                   "--csv", str(self.source), "--selection", str(self.selection_path),
                   "--output", str(self.output), "--threads", "1", "--device", "cpu"]
        if exact:
            sources = {"kline": str(self.source), "micro": "", "funding": "", "oi": "", "bundle": ""}
            command += ["--expected-source-fingerprint", source_bundle_fingerprint(sources)]
        result = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8",
                                timeout=120, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = []
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict): events.append(value)
        return events

    def test_editor_worker_saves_only_actual_selection_and_consistent_identity(self):
        events = self.run_worker()
        self.assertTrue(any(event.get("type") == "completed" for event in events))
        selection = json.loads(self.selection_path.read_text("utf-8"))
        self.assertEqual(selection["开仓条件"]["4h"], [0])
        self.assertEqual(selection["开仓条件"]["1m"], [1])
        checkpoint = json.loads((self.output / "断点记录.json").read_text("utf-8"))
        identity = json.loads((self.output / "回测运行身份.json").read_text("utf-8"))
        self.assertEqual(checkpoint["selection"], selection)
        self.assertEqual(identity["selection_signature"], 配置签名(selection))
        self.assertEqual(checkpoint["selection_signature"], identity["selection_signature"])
        report = json.loads((self.output / "数据能力自动剔除记录.json").read_text("utf-8"))
        self.assertEqual(len(report["removed"]), 4)

    def test_exact_worker_skips_without_replacing_original_or_creating_trades(self):
        before = self.selection_path.read_bytes()
        events = self.run_worker(exact=True)
        self.assertTrue(any(event.get("type") == "skipped" for event in events))
        self.assertFalse(any(event.get("type") == "completed" for event in events))
        self.assertEqual(self.selection_path.read_bytes(), before)
        self.assertFalse((self.output / "全部回测结果.csv").exists())
        self.assertFalse((self.output / "回测运行身份.json").exists())


if __name__ == "__main__":
    unittest.main()
