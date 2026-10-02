"""A deleted old source cannot block new additions or silently enter a batch."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_v152_fingerprint_queue_ui as queue
from selection_config import 配置统计


class MissingFingerprintSourceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        queue.FingerprintQueueUiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        queue.FingerprintQueueUiTests.tearDownClass.__func__(cls)

    def setUp(self):
        queue.FingerprintQueueUiTests.setUp(self)
        self.source_presence.stop()  # Exercise the real directory checks in these tests.
        self.temp = tempfile.TemporaryDirectory(prefix='v157_sources_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tearDown(self):
        queue.FingerprintQueueUiTests.tearDown(self)

    snapshot = queue.FingerprintQueueUiTests.snapshot
    deliver = queue.FingerprintQueueUiTests.deliver

    def item(self, code='a', exists=True, size=10.):
        item = queue.match(code=code, leverage=size)
        path = self.root / code
        if exists:
            path.mkdir(exist_ok=True)
        item['source_dir'] = str(path)
        return item

    def test_six_deleted_old_sources_are_preserved_but_do_not_block_new_item(self):
        app = self.app
        stale = [queue.bound(self.item(code, exists=False)) for code in 'abcdef']
        original = copy.deepcopy(stale)
        app.fingerprint_queue = stale
        new = self.item('0', size=10.)
        # Use a legal zero cooldown for this synthetic code.
        new['row']['止盈后等待分钟'] = 0
        from ranking_view import config_fingerprint
        new['fingerprint'] = config_fingerprint(new['row'])
        with mock.patch.object(app, '_start_fingerprint_job') as job:
            self.deliver('queue_find', {new['fingerprint']: [new]})
            self.assertEqual(job.call_args.args[:2], ('queue_restore', [new]))
        self.deliver('queue_restore', [queue.restoration(new)])
        self.assertEqual(app.fingerprint_queue[:6], original)
        self.assertEqual(len(app.fingerprint_queue), 7)
        self.assertEqual(配置统计(app.current_selection())['包含仓位完整组合数'], 1)
        self.assertEqual(app.current_selection()['仓位倍数'], [10.])
        self.assertEqual(app.run_target_var.get(), 'EDITOR')
        self.assertIn('6条旧记录', app.fingerprint_status_var.get())
        self.assertIn('来源失效', app._queue_display_values(0, stale[0])[3])
        self.error.assert_not_called()
        self.write.assert_not_called()

    def test_existing_valid_records_still_accumulate_and_revalidate(self):
        app = self.app
        old = queue.bound(self.item('a', size=8.))
        new = self.item('b', size=10.)
        app.fingerprint_queue = [old]
        with mock.patch.object(app, '_start_fingerprint_job') as job:
            self.deliver('queue_find', {new['fingerprint']: [new]})
            self.assertEqual(job.call_args.args[1], [old, new])
        self.deliver('queue_restore', [queue.restoration(old), queue.restoration(new)])
        self.assertEqual(app.current_selection()['仓位倍数'], [8., 10.])
        self.assertEqual(len(app.fingerprint_queue), 2)
        self.assertNotIn('旧记录来源目录失效', app.fingerprint_status_var.get())

    def test_requested_item_failure_keeps_list_and_editor_unchanged(self):
        app = self.app
        old = queue.bound(self.item('a', exists=False))
        app.fingerprint_queue = [old]
        before, original = self.snapshot(), copy.deepcopy(app.fingerprint_queue)
        new = self.item('b')
        with mock.patch.object(app, '_start_fingerprint_job'):
            self.deliver('queue_find', {new['fingerprint']: [new]})
        self.deliver('error', '来源在核验期间变化')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(app.fingerprint_queue, original)
        self.write.assert_not_called()

    def test_deleted_source_blocks_batch_even_with_previously_verified_snapshot(self):
        app = self.app
        app.fingerprint_queue = [queue.bound(self.item('a', exists=False))]
        with mock.patch.object(app, '_launch_process') as launch, mock.patch.object(Path, 'mkdir') as mkdir:
            app.start_fingerprint_batch()
        launch.assert_not_called()
        mkdir.assert_not_called()
        self.write.assert_not_called()
        self.assertIn('来源目录失效', self.error.call_args.args[1])

    def test_explicit_revalidation_remains_strict_and_source_recovery_clears_display(self):
        app = self.app
        stale = queue.bound(self.item('a', exists=False))
        app.fingerprint_queue = [stale]
        with mock.patch.object(app, '_start_fingerprint_job') as job:
            app.revalidate_fingerprint_queue()
            self.assertEqual(job.call_args.args[:2], ('queue_restore', [stale]))
        self.assertIn('来源失效', app._queue_display_values(0, stale)[3])
        Path(stale['source_dir']).mkdir()
        self.assertEqual(app._queue_display_values(0, stale)[3], stale.get('definition_status', '已绑定参数'))


if __name__ == '__main__':
    unittest.main()
