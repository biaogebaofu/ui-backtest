"""用真正轻量子进程验证调度生命周期，不调用耗时回测引擎。"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import campaign_runner as R


FAKE_WORKER = r'''
import argparse, csv, json, os, time
from pathlib import Path
p=argparse.ArgumentParser()
for key in ('csv','output','threads','device','selection','cache-root','expected-source-fingerprint','start','end'):
    p.add_argument('--'+key)
a=p.parse_args()
out=Path(a.output)
selection=json.loads(Path(a.selection).read_text(encoding='utf-8'))
kind=selection.get('fake','complete')
assert a.threads=='1' and a.device=='cpu'
assert os.environ['BT_ENTRY_BATCH_MB']=='16'
assert os.environ['OMP_NUM_THREADS']=='1'
control=out/'控制'
control.mkdir(exist_ok=True)
stop=control/'停止.flag'
stop.unlink(missing_ok=True)
(out/'started').touch()
if kind=='skip':
    print(json.dumps({'type':'skipped','reason':'data_unavailable','message':'缺少原始成交数据'}),flush=True)
    raise SystemExit(0)
if kind=='fail':
    raise SystemExit(2)
if kind=='silent':
    raise SystemExit(0)
if kind=='wait':
    while not stop.exists(): time.sleep(.01)
    (out/'断点记录.json').write_text('{"next_tp":1}',encoding='utf-8')
    print(json.dumps({'type':'stopped','next_tp':1}),flush=True)
    raise SystemExit(0)
with (out/'全部回测结果.csv').open('w',encoding='utf-8-sig',newline='') as h:
    w=csv.writer(h);w.writerow(['交易次数（单）','所选仓位累计收益率（%）'])
    w.writerows([[0,0],[1,-0.3],[3,0.4]])
if kind=='bad_rows':
    print(json.dumps({'type':'completed','rows':5}),flush=True)
else:
    print(json.dumps({'type':'completed','rows':3}),flush=True)
'''


class Plan:
    def __init__(self, kinds=("complete",), later=()):
        self.kinds, self.later = kinds, later
        self.advance_seen = []

    @staticmethod
    def job(index, kind, phase="phase1"):
        return {"id": f"{phase}_{index}", "phase": phase,
                "selection": {"fake": kind}, "start": "2026-01-01", "end": "2026-01-02", "tags": {}}

    def initial_jobs(self, settings):
        return [self.job(i, kind) for i, kind in enumerate(self.kinds)]

    def advance_jobs(self, settings, jobs):
        self.advance_seen.append([job["state"] for job in jobs])
        for job in jobs:
            if job["state"] == "completed":
                assert len(job.get("results")) == 3
        if not any(job["phase"] == "phase2" for job in jobs):
            return [self.job(i, kind, "phase2") for i, kind in enumerate(self.later)]
        return []

    def make_catalog(self):
        return {"note": "轻量测试计划"}


class CampaignRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="campaign_test_")
        self.base = Path(self.temporary.name)
        self.source = self.base / "app"
        self.source.mkdir()
        (self.source / "backtest_worker.py").write_text(FAKE_WORKER, encoding="utf-8")
        self.data = self.base / "bars.csv"
        self.data.write_text("timestamp,close\n0,100\n", encoding="utf-8")
        self.settings = {"csv": "bars.csv", "wall_hours": .01,
                         "poll_seconds": .01, "resource_retry_seconds": .01,
                         "failure_retry_seconds": .01, "stop_grace_seconds": 1,
                         "max_jobs": 10}
        self.settings_path = self.base / "input.json"
        self.root = self.base / "campaign"
        self.good = lambda root: {"free_bytes": 100 * R.GIB, "root_bytes": 1,
                                  "memory_bytes": 4 * R.GIB}

    def tearDown(self):
        self.temporary.cleanup()

    def init(self, plan=None):
        self.plan = plan or Plan()
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        R.initialize(self.settings_path, self.root, self.source, self.plan)
        return R.CampaignRunner(self.root, self.plan, resources=self.good)

    def test_lifecycle_archives_losses_and_zero_trades_and_advances(self):
        runner = self.init(Plan(later=("complete",)))
        state = runner.run()
        self.assertEqual(state["status"], "finished_plan")
        self.assertEqual(len(state["jobs"]), 2)
        for job in state["jobs"]:
            self.assertEqual(job["row_count"], 3)
            out = self.root / "jobs" / job["id"]
            self.assertFalse((out / "全部回测结果.csv").exists())
            rows = R.load_results(out)
            self.assertEqual(rows[0]["交易次数（单）"], "0")
            self.assertEqual(rows[1]["所选仓位累计收益率（%）"], "-0.3")
            self.assertFalse(R.read_json(out / "selection.json")["候选筛选"]["自动导出"])
        launches = [job["launches"] for job in state["jobs"]]
        after = R.CampaignRunner(self.root, self.plan, resources=self.good).run()
        self.assertEqual(launches, [job["launches"] for job in after["jobs"]])
        self.assertTrue((self.root / "analysis.json").exists())
        self.assertTrue((self.root / "研究结果汇总.md").exists())

    def test_skip_is_not_completed(self):
        state = self.init(Plan(("skip",))).run()
        self.assertEqual(state["jobs"][0]["state"], "skipped")
        self.assertIn("缺少", state["jobs"][0]["reason"])
        self.assertEqual(state["status"], "finished_with_gaps")

    def test_exit_zero_without_event_fails_with_bounded_retries(self):
        state = self.init(Plan(("silent", "complete"))).run()
        self.assertEqual(state["jobs"][0]["state"], "failed")
        self.assertEqual(state["jobs"][0]["launches"], 3)
        self.assertEqual(state["jobs"][1]["state"], "completed")

    def test_stop_event_keeps_pending_with_checkpoint(self):
        runner = self.init(Plan(("wait",)))
        thread = threading.Thread(target=runner.run)
        thread.start()
        started = self.root / "jobs" / "phase1_0" / "started"
        deadline = time.time() + 10
        while not started.exists() and time.time() < deadline:
            time.sleep(.01)
        runner.request_stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        state = R.read_json(self.root / "state.json")
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["jobs"][0]["state"], "queued")
        self.assertTrue(state["jobs"][0]["checkpoint_confirmed"])
        self.assertTrue((started.parent / "断点记录.json").exists())

    def test_running_crash_state_recovers_queued(self):
        runner = self.init()
        runner.state["jobs"][0]["state"] = "running"
        runner.save()
        state = R.CampaignRunner(self.root, self.plan, resources=self.good).run()
        self.assertEqual(state["jobs"][0]["state"], "completed")
        self.assertEqual(state["jobs"][0]["launches"], 1)

    def test_resource_before_start_waits_and_time_budget_does_not_reset(self):
        self.settings["wall_hours"] = .00004
        runner = self.init()
        runner.resources = lambda root: dict(self.good(root), free_bytes=1)
        state = runner.run()
        self.assertEqual(state["status"], "budget_time")
        self.assertEqual(state["jobs"][0]["launches"], 0)
        deadline = state["deadline"]
        after = R.CampaignRunner(self.root, self.plan, resources=self.good).run()
        self.assertEqual(after["deadline"], deadline)
        self.assertEqual(after["jobs"][0]["launches"], 0)

    def test_dynamic_low_memory_stops_only_worker(self):
        runner = self.init(Plan(("wait",)))
        started = self.root / "jobs" / "phase1_0" / "started"
        runner.resources = lambda root: dict(self.good(root), memory_bytes=500 * R.MIB) if started.exists() else self.good(root)
        thread = threading.Thread(target=runner.run)
        thread.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                state = R.read_json(self.root / "state.json")
                if state["jobs"][0].get("checkpoint_confirmed"):
                    break
                time.sleep(.02)
        finally:
            runner.request_stop()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(state["jobs"][0].get("checkpoint_confirmed"))
        self.assertIn("内存", state["jobs"][0]["reason"])

    def test_stage_deadline_checkpoints_and_reserves_later_stage(self):
        self.settings["phase_end_hours"] = {"phase1": .00015, "phase2": .01}
        runner = self.init(Plan(("complete", "wait", "complete"), later=("complete",)))
        state = runner.run()
        self.assertEqual(state["jobs"][0]["state"], "completed")
        self.assertEqual(state["jobs"][1]["state"], "deferred_budget")
        self.assertEqual(state["jobs"][2]["state"], "deferred_budget")
        self.assertEqual(state["jobs"][2]["launches"], 0)
        self.assertTrue(state["jobs"][1]["checkpoint_confirmed"])
        self.assertEqual(state["jobs"][3]["phase"], "phase2")
        self.assertEqual(state["jobs"][3]["state"], "completed")

    def test_phase_budget_defaults_scale_with_wall_budget(self):
        self.settings["wall_hours"] = 48
        runner = self.init()
        self.assertEqual(runner.settings["phase_end_hours"], {
            "coarse": 24, "bucket": 32, "refine": 40, "validation": 48})

    def test_capacity_limit_is_not_reported_as_full_plan_completion(self):
        self.settings["max_jobs"] = 1
        state = self.init(Plan(("complete",), later=("complete",))).run()
        self.assertEqual(state["status"], "budget_jobs")
        self.assertEqual(state["jobs"][0]["state"], "completed")
        self.assertEqual(len(state["jobs"]), 1)

    def test_real_planner_accepts_deferred_phase_without_false_winner(self):
        import campaign_plan as planner
        settings = {"start": "2026-06-01", "end": "2026-09-01",
                    "validation_start": "2026-09-01", "validation_end": "2026-09-21", "max_jobs": 2000}
        jobs = [R.validate_job(job) for job in planner.initial_jobs(settings)]
        for job in jobs:
            job["state"] = "deferred_budget"
        next_jobs = planner.advance_jobs(settings, jobs)
        # 未完成的代表可登记为明确的补测，不是被认作已经胜出的规则族。
        self.assertTrue(all(job["phase"] == "bucket" for job in next_jobs))
        coverage = planner.coverage_report(settings, jobs)
        self.assertEqual(coverage["budget_deferred_jobs"], len(jobs))
        self.assertEqual(coverage["coverage"]["entry"]["completed_option_count"], 0)
        report = planner.analysis_report(settings, jobs)
        self.assertEqual(report["training_candidates"], [])

    def test_completed_receipt_recovers_after_state_commit_crash(self):
        runner = self.init()
        runner.run()
        state = R.read_json(self.root / "state.json")
        state["jobs"][0]["state"] = "running"
        R.atomic_json(self.root / "state.json", state)
        recovered = R.CampaignRunner(self.root, self.plan, resources=self.good).run()
        self.assertEqual(recovered["jobs"][0]["state"], "completed")
        self.assertEqual(recovered["jobs"][0]["launches"], 1)

    def test_corrupt_completed_archive_is_never_recomputed_silently(self):
        runner = self.init()
        runner.run()
        (self.root / "jobs" / "phase1_0" / "全部回测结果.csv.gz").write_bytes(b"broken")
        with self.assertRaises((OSError, RuntimeError)):
            R.CampaignRunner(self.root, self.plan, resources=self.good).run()

    def test_changed_data_source_or_settings_refuses_resume(self):
        runner = self.init()
        self.data.write_text("different", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "数据"):
            runner.run()

    def test_changed_source_refuses_resume(self):
        runner = self.init()
        (self.source / "backtest_worker.py").write_text("# changed", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "源码"):
            runner.run()

    def test_static_display_assets_are_in_frozen_source_identity(self):
        path = self.source / "strategy_display.json"
        path.write_text("{}", encoding="utf-8")
        runner = self.init()
        path.write_text('{"different":true}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "源码"):
            runner.run()

    def test_data_fingerprint_matches_real_worker_contract(self):
        from data_sources import source_bundle_fingerprint
        runner = self.init()
        expected = source_bundle_fingerprint({"kline": str(self.data), "micro": "", "funding": "", "oi": "", "bundle": ""})
        self.assertEqual(expected, runner.identity["source_fingerprint"])

    def test_changed_runtime_refuses_resume(self):
        runner = self.init()
        with patch.object(R, "runtime_identity", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "依赖"):
                runner.run()

    def test_modified_job_definition_refuses_resume(self):
        runner = self.init()
        runner.state["jobs"][0]["selection"]["fake"] = "other"
        runner.save()
        with self.assertRaisesRegex(RuntimeError, "job"):
            R.CampaignRunner(self.root, self.plan, resources=self.good).run()

    def test_single_campaign_lock_rejects_duplicate_runner(self):
        runner = self.init()
        with R.exclusive_output(self.root):
            with self.assertRaisesRegex(RuntimeError, "另一个"):
                runner.run()

    def test_posix_termination_targets_only_own_process_group(self):
        process = Mock(pid=123456)
        with patch.object(R.os, "name", "posix"), patch.object(R.os, "killpg", create=True) as kill, \
                patch.object(R.signal, "SIGKILL", 9, create=True):
            R.terminate_worker_tree(process)
        self.assertEqual(kill.call_args_list[0].args, (123456, R.signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123456, getattr(R.signal, "SIGKILL", 9)))

    def test_windows_termination_targets_only_own_pid_tree(self):
        process = Mock(pid=123456)
        process.poll.return_value = 1
        with patch.object(R.os, "name", "nt"), patch.object(R.subprocess, "run") as run:
            R.terminate_worker_tree(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "123456", "/T", "/F"])

    def test_no_archive_data_loss_on_row_count_mismatch(self):
        runner = self.init(Plan(("bad_rows",)))
        state = runner.run()
        self.assertEqual(state["jobs"][0]["state"], "failed")
        out = self.root / "jobs" / "phase1_0"
        self.assertTrue((out / "全部回测结果.csv").exists())
        self.assertFalse((out / "完成凭证.json").exists())

    def test_archive_commit_failure_recovers_idempotently(self):
        runner = self.init()
        job = runner.state["jobs"][0]
        real_atomic = R.atomic_json

        def fail_receipt(path, payload):
            if path.name == "完成凭证.json":
                raise OSError("simulated disk write interruption")
            return real_atomic(path, payload)

        with patch.object(R, "atomic_json", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                runner.execute(job)
        out = self.root / "jobs" / job["id"]
        self.assertTrue((out / "全部回测结果.csv").exists())
        runner.recover()
        self.assertEqual(job["state"], "completed")
        self.assertFalse((out / "全部回测结果.csv").exists())

    def test_resource_boundaries_and_owned_path_guard(self):
        settings = dict(R.DEFAULTS)
        snap = self.good(self.root)
        self.assertEqual(R.resource_reason(snap, settings), "")
        self.assertIn("磁盘", R.resource_reason(dict(snap, free_bytes=1), settings))
        self.assertIn("预算", R.resource_reason(dict(snap, root_bytes=12 * R.GIB), settings))
        with self.assertRaises(ValueError):
            R.task_path(self.base.resolve(), "..", "outside")
        with self.assertRaises(ValueError):
            R.validate_job({"id": "../../outside", "phase": "a", "selection": {}})

    def test_worker_cache_directories_are_inside_own_root_before_import(self):
        self.init()
        env = R.worker_environment(self.root)
        for key in ("NUMBA_CACHE_DIR", "CUPY_CACHE_DIR", "JOBLIB_TEMP_FOLDER"):
            path = Path(env[key])
            self.assertTrue(path.is_dir())
            self.assertIn(self.root, path.parents)

    def test_existing_directory_never_reinitialized(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "新任务"):
            R.initialize(self.settings_path, self.root, self.source, self.plan)


class CampaignReadJsonTests(unittest.TestCase):
    def test_windows_transient_permission_error_retries_the_original_read(self):
        path = Path("state.json")
        conflict = PermissionError(13, "atomic replacement in progress")
        self.assertIsNone(getattr(conflict, "winerror", None))
        with patch.object(R.os, "name", "nt"), \
                patch.object(Path, "read_text", side_effect=[conflict, '{"state":"queued"}']) as read, \
                patch.object(R.time, "sleep") as sleep:
            self.assertEqual(R.read_json(path), {"state": "queued"})
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(.02)

    def test_denial_is_bounded_and_other_read_errors_are_immediate(self):
        path = Path("state.json")
        denied = PermissionError(13, "access denied")
        cases = (
            ("windows denial", "nt", denied, PermissionError, 11),
            ("other platform denial", "posix", denied, PermissionError, 1),
            ("missing file", "nt", FileNotFoundError("missing"), FileNotFoundError, 1),
            ("other IO error", "nt", OSError("IO failure"), OSError, 1),
            ("invalid JSON", "nt", "{", json.JSONDecodeError, 1),
        )
        for label, platform, value, error, attempts in cases:
            with self.subTest(label=label), patch.object(R.os, "name", platform), \
                    patch.object(Path, "read_text", side_effect=value if isinstance(value, BaseException) else None,
                                 return_value=value) as read, patch.object(R.time, "sleep") as sleep:
                with self.assertRaises(error) as raised:
                    R.read_json(path)
                if isinstance(value, BaseException):
                    self.assertIs(raised.exception, value)
            self.assertEqual(read.call_count, attempts)
            self.assertEqual(sleep.call_count, attempts - 1)


if __name__ == "__main__":
    unittest.main()
