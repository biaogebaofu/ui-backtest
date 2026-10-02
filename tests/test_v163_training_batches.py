"""Actual spawn integration, exact GP audits, and durable stop/resume coverage."""
import gzip
import json
import multiprocessing
from pathlib import Path
import pickle
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fifth_batch
import fifth_learned_models as learned
import fifth_model_audit as audit
import fifth_precompute as precompute
from test_fifth_precompute import market_data, serial_masks


def snapshots(path):
    result = []
    with gzip.open(path, 'rb') as stream:
        while True:
            try:
                result.append(pickle.load(stream))
            except EOFError:
                return result


class TrainingBatches(unittest.TestCase):
    def test_isolation_wrapper_keeps_frozen_model_transitions_and_causality(self):
        class IdentityScaler:
            def fit(self, x):
                return self

            def transform(self, x):
                return x

        class Detector:
            def __init__(self, **kwargs):
                pass

            def fit(self, x):
                return self

            def decision_function(self, x):
                return x[:, 0]

        n = 13100
        close = np.full(n, 100.)
        atr = np.ones(n)
        x = np.ones((n, 5))
        ct = (np.arange(n)-10102)*60000 + 86400000
        x[10101:10115, 0] = -1
        close[10101:10115] = 97
        close[10115] = 98
        x[10117, 2] = np.nan
        x[11000:12500, 0] = -1
        with mock.patch('sklearn.preprocessing.StandardScaler', IdentityScaler), \
                mock.patch('sklearn.ensemble.IsolationForest', Detector), \
                mock.patch.object(learned, 'record_fit') as record:
            args = (x, np.log(close), close, atr, ct)
            expected = learned._isolation_serial(*args)
            first = [(call.args[1], call.args[3]) for call in record.call_args_list]
            record.reset_mock()
            actual = learned._isolation(*args)
            second = [(call.args[1], call.args[3]) for call in record.call_args_list]
            self.assertEqual(first, second)
            for before, after in zip(expected[:3], actual[:3]):
                np.testing.assert_array_equal(before, after)
            np.testing.assert_array_equal(expected[3]['model_fit_index'], actual[3]['model_fit_index'])
            prefix = learned._isolation(*(value[:12000] for value in args))
            for before, after in zip(actual[:3], prefix[:3]):
                np.testing.assert_array_equal(before[:12000], after)

    def test_mixed_methods_use_parent_gp_and_match_signals_and_archives(self):
        from threadpoolctl import threadpool_limits
        data = market_data(1146, timeframes=('1m',), gaps=False)
        with tempfile.TemporaryDirectory(prefix='v163-mixed-') as directory:
            directory = Path(directory)
            audit.set_output_directory(directory / 'serial')
            with threadpool_limits(limits=1):
                expected = serial_masks(data, '1m', 361)
            audit.close_archives()
            expected_fits = snapshots(directory / 'serial/F5-098_1m_训练快照.pkl.gz')
            events = []
            cache = precompute.prepare_fifth_signals(
                data, [('1m', 288), ('1m', 361)], directory / 'run',
                {'test': 'mixed-gp'}, 2, events.append)
            actual = precompute.load_signals(cache, '1m', 361, 1146)
            np.testing.assert_array_equal(actual, expected)
            del actual
            actual_fits = snapshots(directory / 'run/第五轮模型审计/F5-098_1m_训练快照.pkl.gz')
            self.assertEqual(len(expected_fits), len(actual_fits))
            for before, after in zip(expected_fits, actual_fits):
                self.assertEqual(before['fit_at'], after['fit_at'])
                self.assertEqual(before['settings'], after['settings'])
                if before['model']['fitted'] is None:
                    self.assertIsNone(after['model']['fitted'])
                    continue
                s1, m1 = before['model']['fitted']
                s2, m2 = after['model']['fitted']
                np.testing.assert_array_equal(s1.mean_, s2.mean_)
                np.testing.assert_array_equal(m1.kernel_.theta, m2.kernel_.theta)
                np.testing.assert_array_equal(m1.L_, m2.L_)
            training = [event for event in events if 'training' in event]
            self.assertTrue(training)
            self.assertTrue(all(event['completed'] == 1 for event in training))
            self.assertEqual(events[-1]['state'], 'done')
            with mock.patch.object(precompute.mp, 'get_context', side_effect=AssertionError('cached work must not spawn')):
                again = precompute.prepare_fifth_signals(
                    data, [('1m', 288), ('1m', 361)], directory / 'run',
                    {'test': 'mixed-gp'}, 2, lambda event: None)
            self.assertEqual(cache, again)
            self.assertIsNone(learned._parallel_gp_options)
            audit.set_output_directory(None)

    def test_stop_preserves_gp_blocks_without_publishing_method_then_resumes(self):
        data = market_data(1566, timeframes=('1m',), gaps=False)
        # Valid flat candles give undefined CLV. Original GP records disabled
        # fits; this cheaply covers its real cache/audit/control path.
        for name in ('open', 'high', 'low', 'close'):
            data['1m_' + name][:] = 2000
        before = {child.pid for child in multiprocessing.active_children()}
        with tempfile.TemporaryDirectory(prefix='v163-resume-') as directory:
            directory = Path(directory)
            controls = directory / '控制'
            controls.mkdir()
            events = []

            def stop_after_completed_block(event):
                events.append(event)
                training = event.get('training', {})
                if 0 < training.get('done_blocks', 0) < training.get('total_blocks', 0):
                    (controls / '停止.flag').touch()

            with self.assertRaises(precompute.PrecomputeStopped):
                precompute.prepare_fifth_signals(
                    data, [('1m', 361)], directory, {'test': 'partial-gp'},
                    2, stop_after_completed_block, control_dir=controls)
            completed = list(directory.rglob('block_*/done.json'))
            self.assertTrue(completed)
            self.assertFalse(list(directory.rglob('1m_361.npy')))
            self.assertFalse(list(directory.rglob('work_*')))
            self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)
            self.assertIsNone(learned._parallel_gp_options)
            saved = {path: path.stat().st_mtime_ns for path in completed}
            (controls / '停止.flag').unlink()
            events = []
            cache = precompute.prepare_fifth_signals(
                data, [('1m', 361)], directory, {'test': 'partial-gp'},
                2, events.append, control_dir=controls)
            self.assertTrue((cache / '1m_361.npy').is_file())
            self.assertTrue(any(event.get('training', {}).get('cached_blocks', 0) > 0 for event in events))
            self.assertEqual(saved, {path: path.stat().st_mtime_ns for path in completed})
            self.assertEqual(events[-1]['state'], 'done')
            audit.set_output_directory(None)

    def test_gp_routes_outside_method_pool_and_version_tracks_helpers(self):
        import run_safety
        import account_statistics
        self.assertIn('gp_parallel.py', run_safety.CORE_FILES)
        self.assertIn('iso_fast.py', run_safety.CORE_FILES)
        self.assertTrue(account_statistics.ENGINE_VERSION.startswith('v1.63-'))
        original = learned._supervised_serial
        with learned.parallel_gp_context(None):
            self.assertIs(learned._supervised_serial, original)
        self.assertIsNone(learned._parallel_gp_options)


if __name__ == '__main__':
    unittest.main(verbosity=2)
