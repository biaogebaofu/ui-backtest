"""Progress reflects computed work, scan dimensions, and active time after resume."""
import queue
from types import SimpleNamespace
import unittest
from unittest import mock

from backtest_worker import ScanProgress, scan_row_counts
from selection_config import 全选配置
import ui


class ScanEtaTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0

    def tracker(self, total, resumed=0):
        return ScanProgress(total, resumed, clock=lambda: self.now)

    def test_one_tp_reports_partial_work_before_any_tp_is_complete(self):
        progress = self.tracker(1000)
        initial = progress.snapshot(0)
        self.assertIsNone(initial['eta_seconds'])
        self.now += 20
        partial = progress.snapshot(100)
        self.assertEqual(partial['percent'], 10)
        self.assertEqual(partial['eta_seconds'], 180)
        self.now += 180
        final = progress.snapshot(1000)
        self.assertEqual(final['percent'], 100)
        self.assertEqual(final['eta_seconds'], 0)

    def test_resume_uses_only_new_work_and_keeps_overall_percent(self):
        progress = self.tracker(1000, resumed=850)
        initial = progress.snapshot(850)
        self.assertEqual(initial['percent'], 85)
        self.assertIsNone(initial['eta_seconds'])
        self.now += 10
        partial = progress.snapshot(900)
        self.assertEqual(partial['percent'], 90)
        self.assertEqual(partial['eta_seconds'], 20)

    def test_pause_and_repeated_pause_calls_do_not_lower_throughput(self):
        progress = self.tracker(100)
        self.now += 10
        progress.pause()
        self.now += 600
        progress.pause()
        paused = progress.snapshot(10)
        self.assertEqual(paused['elapsed_seconds'], 10)
        self.assertEqual(paused['eta_seconds'], 90)
        self.assertTrue(progress.resume())
        self.assertFalse(progress.resume())
        self.now += 10
        after = progress.snapshot(20)
        self.assertEqual(after['elapsed_seconds'], 20)
        self.assertEqual(after['eta_seconds'], 80)

    def test_flush_preserves_completed_rows_and_forced_boundaries_bypass_throttle(self):
        progress = self.tracker(500)
        self.now += 5
        buffered = progress.snapshot(100 + 50)
        self.assertIsNone(progress.snapshot(151))
        flushed = progress.snapshot(150 + 0, force=True)
        self.assertEqual(buffered, flushed)
        self.now += 1
        self.assertIsNotNone(progress.snapshot(151))

    def test_all_dimensions_overlay_exceptions_and_per_tp_limits(self):
        config = 全选配置()
        config.update({
            '开仓指标': ['hist', 'dif'], '开仓方向': ['BOTH', 'LONG'],
            '交易会话': ['ALL', 'WEEKDAY'], '开仓位置过滤': ['OFF', 'RANGE60_EDGE20'],
            '开仓条件': {'4h': [0, 1], '1h': [0], '15m': [0], '5m': [0], '1m': [1, 2, 3]},
            '入场触发口径': ['LIVE_01', 'TF_EVENT', 'MACD_CYCLE'],
            '成本模式': ['FEE', 'SLIPPAGE'], '止损代码': ['S1', 'S2'],
            '固定止损代码': ['OFF', 'FS_A'], '强制时间止损分钟': [0, 60],
            '止盈后等待分钟': [0, 5], '叠加止盈代码': ['OFF', 'OV_A', 'OV_B'],
            '仓位倍数': [1, 10, 20],
        })
        tps = [SimpleNamespace(类别=name) for name in ('固定比例止盈', '分批止盈', '盈亏比止盈')]
        # 96 entry combinations x 3 entry modes x 2 costs x 8 stops x 2 waits.
        per_plain_tp = 96 * 3 * 2 * 8 * 2
        self.assertEqual(scan_row_counts(config, tps),
                         [per_plain_tp * 3, per_plain_tp, per_plain_tp])
        self.assertEqual(scan_row_counts(config, tps, 7), [7, 7, 7])
        self.assertEqual(scan_row_counts(config, tps, per_plain_tp + 1),
                         [per_plain_tp + 1, per_plain_tp, per_plain_tp])
        self.assertEqual(scan_row_counts(config, tps[:1], 10**9), [per_plain_tp * 3])


class ScanEtaUiTests(unittest.TestCase):
    def fake_app(self):
        return SimpleNamespace(messages=queue.Queue(), progress={'value': 0},
                               status_var=mock.Mock(), append_log=mock.Mock(),
                               after=mock.Mock(), after_cancel=mock.Mock(),
                               poll_messages=mock.Mock(), fingerprint_queue=[])

    def event(self, **updates):
        event = {'type': 'inner_progress', 'tp': 1, 'total_tp': 1,
                 'base_done': 25, 'base_total': 100,
                 'rows': 25, 'total_rows': 100, 'percent': 25, 'eta_seconds': 90}
        event.update(updates)
        return event

    def test_inner_and_tp_boundary_keep_eta_and_computed_progress(self):
        app = self.fake_app()
        for kind in ('inner_progress', 'progress'):
            app.messages.put(self.event(type=kind))
            ui.App.poll_messages(app)
            self.assertEqual(app.progress['value'], 25)
            text = app.status_var.set.call_args.args[0]
            self.assertIn('已完成 25/100 行', text)
            self.assertIn('预计剩余 1分 30秒', text)
            self.assertNotIn('已写', text)

    def test_no_new_sample_displays_estimating_instead_of_zero(self):
        text = ui.scan_progress_text(self.event(rows=50, eta_seconds=None))
        self.assertIn('预计剩余 估算中', text)
        self.assertNotIn('0分 0秒', text)

    def test_long_preparation_stages_keep_scan_eta_visible_and_still_log_details(self):
        app = self.fake_app()
        for kind in ('stage', 'tp_start'):
            app.messages.put(self.event(type=kind, message='生成开仓批次',
                                       category='固定比例止盈', description='说明'))
            ui.App.poll_messages(app)
            self.assertIn('预计剩余 1分 30秒', app.status_var.set.call_args.args[0])
            self.assertEqual(app.progress['value'], 25)
        self.assertIn('生成开仓批次', app.append_log.call_args_list[0].args[0])

    def test_batch_child_shows_eta_and_advances_current_item_fraction(self):
        app = self.fake_app()
        ui.App._handle_fingerprint_batch_message(app, {
            'type': 'batch_child_event', 'index': 2, 'total': 4,
            'event': self.event(),
        })
        self.assertEqual(app.progress['value'], 31.25)
        self.assertIn('预计剩余 1分 30秒', app.status_var.set.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
