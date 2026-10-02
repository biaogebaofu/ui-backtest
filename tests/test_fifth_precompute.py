"""Real spawned-worker parity and durable-cache checks; no trading or market files."""
import tempfile
import unittest
from unittest import mock
import multiprocessing
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fifth_batch
import fifth_model_audit
import fifth_precompute as precompute


def market_data(n=720, timeframes=("1m", "5m"), gaps=True):
    data = {}
    minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
    for index, tf in enumerate(timeframes):
        rng = np.random.default_rng(716 + index)
        close = 2000 * np.exp(np.cumsum(rng.normal(0, .002, n)))
        opening = np.r_[close[0], close[:-1]]
        volume = rng.uniform(100, 300, n)
        values = {
            "open": opening,
            "high": np.maximum(opening, close) + rng.uniform(.1, 2, n),
            "low": np.minimum(opening, close) - rng.uniform(.1, 2, n),
            "close": close,
            "volume": volume,
            "ct": 1767225600000 + minutes[tf] * 60000 * np.arange(1, n + 1),
            "hist": np.sin(np.arange(n) / 6),
            "dif": np.sin(np.arange(n) / 6) / 2,
            "dea": np.zeros(n),
        }
        buy = volume * rng.uniform(.05, .95, n)
        values.update(quote_volume=volume * close, taker_buy_base=buy,
                      taker_buy_quote=buy * close, delta_base=2 * buy - volume,
                      trades=rng.integers(30, 100, n).astype(float))
        if gaps and n >= 600:
            # Two independent reset causes: an absent bar and invalid OHLCV.
            values["ct"][310:] += minutes[tf] * 60000 * 3
            values["volume"][515] = np.nan
        data.update({f"{tf}_{key}": value for key, value in values.items()})
    data["ct1"] = data[f"{timeframes[0]}_ct"].copy()
    return data


def serial_masks(data, tf, code):
    extras = {key[len(tf) + 1:]: value for key, value in data.items()
              if key.startswith(tf + "_")}
    return np.asarray(fifth_batch.fifth_masks(
        code, None, None, *(data[f"{tf}_{name}"] for name in
                           ("open", "high", "low", "close", "volume", "ct")),
        extras, tf), dtype=bool)


class FifthPrecomputeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="fifth-precompute-test-")
        cls.root = Path(cls.temporary.name)
        cls.data = market_data()
        cls.tasks = [("1m", 285), ("5m", 285), ("1m", 288),
                     ("5m", 288), ("1m", 333)]
        cls.identity = {"code_sha256": "synthetic-source-v1",
                        "feature_request": {"fingerprint": "synthetic-parity"}}
        fifth_model_audit.set_output_directory(None)
        cls.expected = {(tf, code): serial_masks(cls.data, tf, code)
                        for tf, code in cls.tasks}
        cls.events = []
        cls.cache = Path(precompute.prepare_fifth_signals(
            cls.data, cls.tasks + [cls.tasks[0]], cls.root / "run-1",
            cls.identity, 2, cls.events.append))

    @classmethod
    def tearDownClass(cls):
        fifth_model_audit.set_output_directory(None)
        cls.temporary.cleanup()

    def test_spawned_signals_match_serial_for_models_periods_gaps_and_extra_fields(self):
        for tf, code in self.tasks:
            with self.subTest(tf=tf, code=code):
                self.assertGreater(np.count_nonzero(self.expected[tf, code]), 0)
                actual = precompute.load_signals(self.cache, tf, code,
                                                 len(self.data[f"{tf}_ct"]))
                np.testing.assert_array_equal(actual, self.expected[tf, code])
                self.assertEqual(actual.dtype, np.dtype(bool))
                self.assertIsInstance(actual, np.memmap)
                self.assertFalse(actual.flags.writeable)

    def test_models_keep_separate_audit_outputs_for_each_period(self):
        audit = self.root / "run-1" / "第五轮模型审计"
        for tf in ("1m", "5m"):
            with self.subTest(tf=tf):
                self.assertTrue(list(audit.rglob(f"F5-022_{tf}_训练快照.pkl.gz")))
                paths = list(audit.rglob(f"F5-022_{tf}_当时预测_*.npz"))
                self.assertGreater(len(paths), 0)
                for path in paths:
                    with np.load(path) as record:
                        self.assertTrue(np.all(np.isin(record["close_times"].astype(np.int64),
                                                       self.data[f"{tf}_ct"])))

    def test_repeat_uses_existing_signals_without_rewriting_or_duplicate_tasks(self):
        before = {path.name: (path.stat().st_mtime_ns, path.read_bytes())
                  for path in self.cache.glob("*.npy")}
        again = Path(precompute.prepare_fifth_signals(
            self.data, self.tasks + self.tasks, self.root / "run-1",
            self.identity, 2, lambda event: None))
        self.assertEqual(again, self.cache)
        after = {path.name: (path.stat().st_mtime_ns, path.read_bytes())
                 for path in again.glob("*.npy")}
        self.assertEqual(before, after)

    def test_cross_run_cache_reuses_models_and_signals_but_rejects_changed_data_identity(self):
        shared = self.root / "shared-cache"
        identity = {"code_sha256": "fixed-source", "engine_version": "test-v1",
                    "feature_request": {"fingerprint": "dataset-a", "feature_version": 7},
                    "selection_signature": "first-exit-and-size-settings"}
        tasks = [("1m", 288), ("1m", 285)]
        first_output = self.root / "shared-first"
        first = precompute.prepare_fifth_signals(
            self.data, tasks, first_output, identity, 2, lambda event: None,
            shared_cache_dir=shared)
        before = {str(path.relative_to(shared)): (path.stat().st_mtime_ns, path.read_bytes())
                  for path in shared.rglob("*") if path.is_file()}
        self.assertTrue(before)
        second_identity = dict(identity, selection_signature="different-stop-profit-position-cost")
        second_output = self.root / "shared-second"
        events = []
        # Any attempt to start a process demonstrates that cached work was not reused.
        with mock.patch.object(precompute.mp, "get_context",
                               side_effect=AssertionError("reuse must not start workers")):
            second = precompute.prepare_fifth_signals(
                self.data, tasks, second_output, second_identity, 2, events.append,
                shared_cache_dir=shared)
        self.assertNotEqual(Path(first), Path(second))
        self.assertEqual(events[0]["completed"], len(tasks))
        for tf, code in tasks:
            np.testing.assert_array_equal(
                precompute.load_signals(first, tf, code, 720),
                precompute.load_signals(second, tf, code, 720))
        first_audit = first_output / "第五轮模型审计"
        second_audit = second_output / "第五轮模型审计"
        expected_audit = {path.name: path.read_bytes() for path in first_audit.iterdir()}
        self.assertTrue(expected_audit)
        self.assertEqual(expected_audit,
                         {path.name: path.read_bytes() for path in second_audit.iterdir()})
        self.assertEqual(before,
                         {str(path.relative_to(shared)): (path.stat().st_mtime_ns, path.read_bytes())
                          for path in shared.rglob("*") if path.is_file()})
        third_identity = dict(identity, feature_request={"fingerprint": "dataset-b",
                                                         "feature_version": 7})
        events = []
        third = precompute.prepare_fifth_signals(
            self.data, tasks, self.root / "shared-third", third_identity, 2, events.append,
            shared_cache_dir=shared)
        self.assertEqual(events[0]["completed"], 0)
        self.assertEqual(events[-1]["completed"], len(tasks))
        self.assertNotEqual(Path(first).name, Path(third).name)

    def test_loader_rejects_missing_and_wrong_length_signals(self):
        with self.assertRaises((FileNotFoundError, ValueError, RuntimeError)):
            precompute.load_signals(self.cache, "4h", 288, 720)
        with self.assertRaises((ValueError, RuntimeError)):
            precompute.load_signals(self.cache, "1m", 288, 719)

    def test_loader_rejects_malformed_array_shape(self):
        folder = self.root / "bad-shape"
        folder.mkdir(exist_ok=True)
        np.save(folder / "1m_288.npy", np.zeros((3, 10), dtype=bool))
        with self.assertRaises((ValueError, RuntimeError)):
            precompute.load_signals(folder, "1m", 288, 10)

    def test_loader_rejects_non_boolean_data(self):
        folder = self.root / "bad-dtype"
        folder.mkdir(exist_ok=True)
        np.save(folder / "1m_288.npy", np.zeros((2, 10), dtype=np.float64))
        with self.assertRaises((ValueError, RuntimeError)):
            precompute.load_signals(folder, "1m", 288, 10)

    def test_stop_before_work_does_not_publish_partial_signal(self):
        output = self.root / "stopped"
        controls = output / "控制"
        controls.mkdir(parents=True)
        (controls / "停止.flag").write_text("stop", encoding="utf-8")
        with self.assertRaises(precompute.PrecomputeStopped):
            precompute.prepare_fifth_signals(
                self.data, [("1m", 285)], output, {"test": "stopped"},
                2, lambda event: None, control_dir=controls)
        self.assertFalse(list(output.rglob("1m_285.npy")))

    def test_stop_during_work_reaps_children_and_discards_incomplete_output(self):
        output = self.root / "stopped-while-running"
        controls = output / "控制"
        controls.mkdir(parents=True)
        before = {child.pid for child in multiprocessing.active_children()}
        canceled = []

        def request_stop(event):
            if event.get("running") and not canceled:
                (controls / "停止.flag").write_text("stop", encoding="utf-8")
                canceled.append(True)

        with self.assertRaises(precompute.PrecomputeStopped):
            precompute.prepare_fifth_signals(
                market_data(6000, timeframes=("1m",), gaps=False),
                [("1m", 285)], output, {"test": "stopped-during-work"},
                1, request_stop, control_dir=controls)
        self.assertTrue(canceled)
        self.assertFalse(list(output.rglob("1m_285.npy")))
        self.assertFalse(list(output.rglob("work_*")))
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_worker_failure_is_visible_and_does_not_commit_signal(self):
        output = self.root / "unsupported-method"
        with self.assertRaisesRegex(RuntimeError, "开仓预计算失败"):
            precompute.prepare_fifth_signals(
                self.data, [("5m", 286)], output, {"test": "unsupported"},
                1, lambda event: None)
        self.assertFalse(list(output.rglob("5m_286.npy")))
        self.assertFalse(list(output.rglob("work_*")))

    def test_memory_limit_respects_requested_cpu_tasks_and_pressure(self):
        with mock.patch.object(precompute.os, "cpu_count", return_value=18):
            large_memory = 64 * 1024**3
            self.assertEqual(precompute.worker_limit(18, 50, 900000, large_memory), 18)
            self.assertEqual(precompute.worker_limit(6, 50, 900000, large_memory), 6)
            self.assertEqual(precompute.worker_limit(18, 3, 900000, large_memory), 3)
            self.assertEqual(precompute.worker_limit(99, 50, 900000, large_memory), 18)
            limited = precompute.worker_limit(18, 50, 900000, 4 * 1024**3)
            self.assertGreaterEqual(limited, 1)
            self.assertLess(limited, 18)
            self.assertEqual(precompute.worker_limit(18, 50, 900000, 0), 1)


if __name__ == "__main__":
    unittest.main()
