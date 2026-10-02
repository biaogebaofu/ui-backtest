"""Completion notices use actual ranking counts without touching Tk or settings."""
import queue
from types import SimpleNamespace
import unittest
from unittest import mock

import ui


class RankingCompletionUiTests(unittest.TestCase):
    def app(self):
        return SimpleNamespace(messages=queue.Queue(), progress={}, status_var=mock.Mock(),
                               append_log=mock.Mock(), after=mock.Mock(return_value="scheduled"),
                               fingerprint_queue=[], poll_messages=mock.Mock())

    def deliver(self, top, worst, batch=False):
        app = self.app()
        event = {"type": "legacy_completed", "top_rows": top, "worst_rows": worst,
                 "top_path": "top.xlsx", "worst_path": "worst.xlsx"}
        with mock.patch.object(ui.messagebox, "showinfo") as info, \
                mock.patch.object(ui.messagebox, "showwarning") as warning, \
                mock.patch.object(ui.messagebox, "showerror") as error:
            if batch:
                ui.App._handle_fingerprint_batch_message(app, {
                    "type": "batch_child_event", "index": 2, "total": 4,
                    "fingerprint": "0123456789abcdef", "event": event})
            else:
                app.messages.put(event)
                ui.App.poll_messages(app)
            info.assert_not_called(); warning.assert_not_called(); error.assert_not_called()
        logs = [call.args[0] for call in app.append_log.call_args_list]
        return app, logs

    def test_empty_top_with_valid_worst_explains_scope_for_both_run_paths(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                app, logs = self.deliver(0, 4, batch)
                text = "\n".join(logs)
                self.assertIn("最优榜为空，但最差榜有有效结果", text)
                self.assertIn("不代表未回测或手续费结果未计算", text)
                self.assertIn("不套用最优门槛", text)
                self.assertNotIn("两榜均为空", text)
                self.assertIn("4", app.status_var.set.call_args.args[0])

    def test_both_empty_does_not_claim_all_results_failed_top_filters(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                _, logs = self.deliver(0, 0, batch)
                text = "\n".join(logs)
                self.assertIn("两榜均为空", text)
                self.assertIn("不能仅凭空榜判断是门槛淘汰", text)
                self.assertNotIn("最差榜有有效结果", text)
                self.assertNotIn("手续费结果未计算", text)

    def test_nonempty_rankings_do_not_emit_empty_notices(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                _, logs = self.deliver(3, 4, batch)
                self.assertNotIn("为空", "\n".join(logs))
                self.assertIn("完整CSV不受影响", "\n".join(logs))

    def test_batch_summary_identifies_exact_child_and_keeps_both_counts(self):
        app, logs = self.deliver(2, 14, batch=True)
        self.assertTrue(all(line.startswith("[2/4 0123456789abcdef]") for line in logs))
        summary = app.status_var.set.call_args.args[0]
        self.assertIn("最优 2 行", summary)
        self.assertIn("最差 14 行", summary)

    def test_single_run_keeps_paths_progress_and_polling(self):
        app, logs = self.deliver(1, 1)
        self.assertIn("最优前5000 Excel：top.xlsx", logs)
        self.assertIn("最差1000 Excel：worst.xlsx", logs)
        self.assertEqual(app.progress["value"], 100)
        app.after.assert_called_once()
        self.assertEqual(app._poll_after_id, "scheduled")


if __name__ == "__main__":
    unittest.main()
