"""Precomputation has its own progress and does not borrow a replay ETA."""
import queue
from types import SimpleNamespace
import unittest
from unittest import mock

import ui


class PrecomputeUiTests(unittest.TestCase):
    def app(self):
        return SimpleNamespace(messages=queue.Queue(), progress={"value": 16},
                               status_var=mock.Mock(), append_log=mock.Mock(),
                               after=mock.Mock(return_value="scheduled"), after_cancel=mock.Mock(),
                               fingerprint_queue=[], poll_messages=mock.Mock())

    def event(self, **changes):
        return {"type": "entry_precompute", "completed": 4, "total": 20,
                "running": ["1m 280:SSA", "5m 285:分位数回归"], "workers": 18,
                "elapsed_seconds": 125, "message": "按方法独立计算", **changes}

    def deliver(self, app, event):
        app.messages.put(event)
        ui.App.poll_messages(app)
        return app.status_var.set.call_args.args[0]

    def test_precompute_displays_methods_elapsed_and_actual_concurrency(self):
        app = self.app()
        text = self.deliver(app, self.event(eta_seconds=1, rows=16, total_rows=103))
        for expected in ("开仓信号预计算", "4/20 种方法", "2/18 个任务",
                         "2分 5秒", "1m 280:SSA", "5m 285:分位数回归"):
            self.assertIn(expected, text)
        self.assertNotIn("预计剩余", text)
        self.assertNotIn("103", text)
        self.assertEqual(app.progress["value"], 20)
        app.after.assert_called_once()

    def test_many_running_names_are_bounded_but_count_is_preserved(self):
        text = ui.entry_precompute_text(self.event(running=[f"方法{i}" for i in range(18)]))
        self.assertIn("方法0、方法1、方法2 等18种", text)
        self.assertIn("18/18 个任务", text)
        self.assertNotIn("方法3", text)

    def test_pause_and_done_do_not_claim_a_trade_eta(self):
        app = self.app()
        paused = self.deliver(app, self.event(state="paused", running=[]))
        self.assertIn("预计算已暂停", paused)
        done = self.deliver(app, self.event(state="done", completed=20, running=[]))
        self.assertIn("预计算完成", done)
        self.assertEqual(app.progress["value"], 100)
        self.assertNotIn("预计剩余", paused + done)

    def test_replay_resumes_normal_progress_after_precompute_done(self):
        app = self.app()
        self.deliver(app, self.event(state="done", completed=20, running=[]))
        replay = {"type": "inner_progress", "percent": 25, "rows": 25,
                  "total_rows": 100, "tp": 1, "total_tp": 4, "eta_seconds": 60}
        text = self.deliver(app, replay)
        self.assertEqual(text, ui.scan_progress_text(replay))
        self.assertEqual(app.progress["value"], 25)
        self.assertNotIn("预计算", text)

    def test_batch_shows_child_precompute_without_claiming_child_completion(self):
        app = self.app()
        ui.App._handle_fingerprint_batch_message(app, {
            "type": "batch_child_event", "index": 2, "total": 4,
            "fingerprint": "0123456789abcdef", "event": self.event()})
        text = app.status_var.set.call_args.args[0]
        self.assertTrue(text.startswith("[2/4 0123456789abcdef] 本条开仓信号预计算"))
        self.assertIn("4/20 种方法", text)
        self.assertEqual(app.progress["value"], 25)
        replay = {"type": "inner_progress", "percent": 40, "rows": 40,
                  "total_rows": 100, "eta_seconds": 30}
        ui.App._handle_fingerprint_batch_message(app, {
            "type": "batch_child_event", "index": 2, "total": 4,
            "fingerprint": "0123456789abcdef", "event": replay})
        self.assertIn("预计剩余", app.status_var.set.call_args.args[0])
        self.assertEqual(app.progress["value"], 35)

    def test_zero_methods_event_is_safe(self):
        app = self.app()
        text = self.deliver(app, self.event(completed=0, total=0, running=[]))
        self.assertIn("0/0 种方法", text)
        self.assertEqual(app.progress["value"], 0)

    def test_concurrency_help_explains_both_stages_and_keeps_user_value(self):
        app = SimpleNamespace(thread_var=mock.Mock(), thread_hint_var=mock.Mock(),
                              thread_hint=mock.Mock())
        app.thread_var.get.return_value = 18
        with mock.patch.object(ui.os, "cpu_count", return_value=20):
            ui.App.update_thread_hint(app)
        text = app.thread_hint_var.set.call_args.args[0]
        self.assertIn("最多并发 18 组任务", text)
        self.assertIn("预计算使用独立进程", text)
        self.assertIn("账户回放使用线程", text)
        app.thread_var.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
