"""Position sweeps retain independent account results and safe resume metadata."""
import json
import unittest
from unittest import mock

import numpy as np

from entry_position import POSITION_FILTERS, position_filter_label
import test_v147_worker_execution as worker_fixture


class PositionWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        worker_fixture.FourthBatchWorkerExecutionTests.prepare_fixture.__func__(cls)
        cls.run_worker = classmethod(worker_fixture.FourthBatchWorkerExecutionTests.run_worker.__func__)
        cls.default = cls.run_worker("default", "FEE", 5, .0004)
        cls.off = cls.run_worker("off", "FEE", 5, .0004, ["OFF"])
        cls.sweep = cls.run_worker("sweep", "FEE", 5, .0004, list(POSITION_FILTERS))
        cls.stopped = cls.run_worker("resume", "FEE", 5, .0004,
                                     list(POSITION_FILTERS), stop_after_first_tp=True)
        cls.resumed = cls.run_worker("resume", "FEE", 5, .0004, list(POSITION_FILTERS))

    def test_default_and_off_are_identical_and_sweep_keeps_off_unchanged(self):
        self.assertEqual(self.default["raw"], self.off["raw"])
        off_rows = [row for row in self.sweep["rows"] if row["开仓位置过滤代码"] == "OFF"]
        self.assertEqual(off_rows, self.off["rows"])

    def test_all_position_codes_labels_and_independent_base_results_export(self):
        rows = self.sweep["rows"]
        self.assertEqual(len(rows), 6 * len(POSITION_FILTERS))
        self.assertEqual({row["开仓位置过滤代码"] for row in rows}, set(POSITION_FILTERS))
        self.assertEqual(self.sweep["raw"][0][-2:], ["开仓位置过滤代码", "开仓位置过滤说明"])
        for row in rows:
            self.assertEqual(row["开仓位置过滤说明"], position_filter_label(row["开仓位置过滤代码"]))
            self.assertEqual(row["成本模式"], "FEE")
            self.assertEqual(float(row["往返成交偏移（%）"]), 0.)
            self.assertAlmostEqual(float(row["开仓净手续费率（%）"]), .0004 * .9 * .7)
            self.assertEqual(float(row["平仓后最小开仓间隔（分钟）"]), 5.)
        for code in (156, 263):
            for tp in (2, 2421):
                choices = [row for row in rows if int(row["1分钟条件代码"]) == code
                           and int(row["止盈方案编号"]) == tp]
                self.assertEqual(len({row["基础策略编号"] for row in choices}), 1)
                off = next(row for row in choices if row["开仓位置过滤代码"] == "OFF")
                self.assertGreater(int(off["交易次数（单）"]), 0)
                self.assertTrue(any(row["交易次数（单）"] != off["交易次数（单）"] for row in choices))

    def test_progress_and_checkpoint_count_the_position_dimension(self):
        events = []
        for line in self.sweep["log"].splitlines():
            if line.startswith("{"):
                events.append(json.loads(line))
        start = next(event for event in events if event.get("type") == "start")
        self.assertEqual(start["csv_rows"], 6 * len(POSITION_FILTERS))
        self.assertEqual(start["total_combinations"], 12 * len(POSITION_FILTERS))
        progress = [event for event in events if event.get("type") == "inner_progress"]
        self.assertTrue(progress)
        self.assertTrue(all(event["base_total"] == 3 * len(POSITION_FILTERS) for event in progress))
        self.assertEqual(self.sweep["checkpoint"]["rows"], 6 * len(POSITION_FILTERS))
        self.assertEqual(self.sweep["checkpoint"]["selection"]["开仓位置过滤"], list(POSITION_FILTERS))

    def test_interrupted_resume_matches_uninterrupted_sweep_exactly(self):
        self.assertIn('"stopped"', self.stopped["log"])
        self.assertEqual(self.stopped["checkpoint"]["next_tp"], 1)
        self.assertEqual(len(self.stopped["rows"]), 3 * len(POSITION_FILTERS))
        self.assertEqual(self.resumed["raw"], self.sweep["raw"])
        self.assertEqual(self.resumed["checkpoint"]["rows"], 6 * len(POSITION_FILTERS))

    def test_off_uses_legacy_candidates_without_constructing_position_gate(self):
        from test_support import prepare_test_features
        prepare_test_features()
        import backtest_engine as B
        cases = ((0, 0, 0, 0, 156), (0, 0, 0, 0, 263))
        B.build_field.cache_clear()
        with mock.patch.object(B, "entry_position_gate", side_effect=AssertionError("OFF requested a gate")):
            signals, exits, run_id, entries, stops = B.build_field(
                "hist", cases, ("S1",), position_filter="OFF")
        high_tf = B.R.build("hist")[2]
        gate = B.s3_baseline_gate(exits, "1m", 0.)
        for case, candidates, directions in entries:
            expected = B.build_candidates(signals, high_tf, case, gate)
            np.testing.assert_array_equal(candidates, expected[0])
            np.testing.assert_array_equal(directions, expected[1])
        expected_runs = B.direction_run_id(signals["d1"])
        np.testing.assert_array_equal(run_id, expected_runs)
        components = next(parts for code, parts, label in B.STOP_DEFINITIONS if code == "S1")
        expected_stops = B.make_stop_data(exits, components)
        for actual, expected in zip(stops[0][2], expected_stops):
            np.testing.assert_array_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()
