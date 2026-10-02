import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_safety


class AtomicJsonPortabilityTests(unittest.TestCase):
    def test_windows_reader_conflict_retries_without_removing_previous_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"old": true}', encoding='utf-8')
            real_replace = run_safety.os.replace
            conflict = PermissionError('reader holds destination')
            conflict.winerror = 32
            attempts = []

            def replace(source, destination):
                attempts.append(source)
                if len(attempts) == 1:
                    self.assertEqual(json.loads(path.read_text('utf-8')), {'old': True})
                    raise conflict
                return real_replace(source, destination)

            with mock.patch.object(run_safety.os, 'name', 'nt'), \
                    mock.patch.object(run_safety.os, 'replace', side_effect=replace), \
                    mock.patch.object(run_safety.time, 'sleep'):
                run_safety.atomic_json(path, {'new': True})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(path.read_text('utf-8')), {'new': True})
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_persistent_windows_conflict_is_reported_and_old_json_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"old": true}', encoding='utf-8')
            conflict = PermissionError('reader holds destination')
            conflict.winerror = 32
            with mock.patch.object(run_safety.os, 'name', 'nt'), \
                    mock.patch.object(run_safety.os, 'replace', side_effect=conflict) as replace, \
                    mock.patch.object(run_safety.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    run_safety.atomic_json(path, {'new': True})
            self.assertEqual(replace.call_count, 11)
            self.assertEqual(json.loads(path.read_text('utf-8')), {'old': True})
            self.assertEqual(list(path.parent.glob('*.tmp')), [])
