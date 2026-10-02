import copy
import math
from pathlib import Path
import random
from types import SimpleNamespace
import unittest
from unittest import mock

import fingerprint_lookup as F
from test_v149_fingerprint_lookup import FingerprintLookupTests


class FingerprintSpeedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FingerprintLookupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def match(self, name='result'):
        directory, fingerprint, _ = self.fixture.make_result(name)
        return F.find_fingerprint_matches(fingerprint, directory)[0]

    def test_scalar_list_membership_preserves_original_validation(self):
        values = [False, True, 0, 1, -2, 1.0, -2.0, 1.5, '', '1', None,
                  float('inf'), -float('inf'), float('nan')]
        rng = random.Random(43)
        pairs = [([a], [b]) for a in values for b in values]
        pairs += [(rng.choices(values, k=8), rng.choices(values, k=8)) for _ in range(500)]
        pairs += [([1, 1], [1, 2]), ([True, 1], [1, True]), ([1, 2], [2.0, 1.0])]
        for raw, normalized in pairs:
            expected = len(raw) == len(normalized) and all(any(
                type(item) is type(other) and item == other
                or type(item) in (int, float) and type(other) in (int, float)
                and math.isfinite(item) and item == other for other in normalized) for item in raw)
            try:
                F._unchanged_parameter(raw, normalized, 'choices')
                actual = True
            except ValueError:
                actual = False
            self.assertEqual(actual, expected, (raw, normalized))
        F._unchanged_parameter(list(range(9142)), list(reversed(range(9142))), 'all TP')
        with self.assertRaises(ValueError):
            F._unchanged_parameter(list(range(9142)), list(range(1, 9143)), 'changed TP')

    def test_batch_reads_each_ranking_once_and_preserves_results(self):
        match = self.match()
        expected = F.restore_fingerprint_match(match)
        progress = []
        with mock.patch.object(F, '_read_json', wraps=F._read_json) as read:
            restored = F.restore_many([match] * 12, progress=progress.append)
        self.assertEqual(restored, [expected] * 12)
        ranking_reads = [call.args[0] for call in read.call_args_list if call.args[0].name in F.RANKING_FILES]
        self.assertEqual(len(ranking_reads), 2)
        self.assertIn('12/12', progress[-1])
        restored[0]['selection']['资金约束']['初始资金USDC'] = 1
        self.assertEqual(restored[1], expected)

    def test_batch_rejects_rankings_changed_after_shared_read(self):
        match = self.match()
        original = F.restore_fingerprint_match
        def changed(item, **kwargs):
            result = original(item, **kwargs)
            path = Path(item['source_dir']) / F.RANKING_FILES[0]
            path.write_text(path.read_text('utf-8') + ' ', 'utf-8')
            return result
        with mock.patch.object(F, 'restore_fingerprint_match', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '核验期间原排行榜已变化'):
                F.restore_many([match, match])

    def test_later_batch_reloads_original_rows_and_rejects_changed_snapshot(self):
        match = self.match()
        restored = F.restore_many([match])[0]
        pinned = F.bind_fingerprint_match(match, restored)
        pinned['verified_snapshot']['start'] = 'changed'
        with self.assertRaisesRegex(ValueError, '快照'):
            F.restore_many([pinned])
        changed = copy.deepcopy(match)
        changed['row']['ETH单次最大开仓数量（ETH）'] = 999
        with self.assertRaises(ValueError):
            F.restore_many([changed])

    def test_ui_progress_preserves_busy_state_and_ignores_stale_generation(self):
        import ui
        app = SimpleNamespace(_fingerprint_generation=3, _closing=False,
                              _fingerprint_run_active=lambda: False,
                              fingerprint_status_var=mock.Mock(), _fingerprint_busy=True)
        ui.App._handle_fingerprint_message(app, {'type': 'fingerprint_progress', 'generation': 2, 'message': 'old'})
        app.fingerprint_status_var.set.assert_not_called()
        ui.App._handle_fingerprint_message(app, {'type': 'fingerprint_progress', 'generation': 3, 'message': '3/12'})
        app.fingerprint_status_var.set.assert_called_once_with('3/12')
        self.assertTrue(app._fingerprint_busy)


if __name__ == '__main__':
    unittest.main()
