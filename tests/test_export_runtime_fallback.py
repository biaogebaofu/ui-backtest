from __future__ import annotations

import tempfile
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import export_runtime


class ExportRuntimeFallbackTests(unittest.TestCase):
    def test_hung_renderer_is_stopped_and_complete_streaming_export_replaces_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / 'payload.json'
            payload.write_text('{}', encoding='utf-8')
            script = root / 'candidate_export.mjs'
            script.write_text('// test renderer', encoding='utf-8')
            output = root / 'out.xlsx'
            real_run = subprocess.run
            def sleeping_renderer(command, **kwargs):
                Path(command[-1]).write_bytes(b'incomplete renderer file')
                return real_run([sys.executable, '-c', 'import time; time.sleep(20)'], **kwargs)
            def stream(_payload, target):
                self.assertFalse(target.exists())
                target.write_bytes(b'complete streaming workbook')
            start = time.monotonic()
            with patch.object(export_runtime, 'ensure_artifact_tool'), \
                 patch.object(export_runtime, 'RENDER_TIMEOUT_SECONDS', .1), \
                 patch.object(export_runtime.subprocess, 'run', side_effect=sleeping_renderer), \
                 patch.object(export_runtime, 'export_streaming_xlsx', side_effect=stream) as fallback:
                actual = export_runtime.export_xlsx_atomic('node', script, payload, output, root)
            self.assertLess(time.monotonic() - start, 10)
            self.assertEqual(actual.read_bytes(), b'complete streaming workbook')
            fallback.assert_called_once()
            self.assertEqual(list(root.glob('*.tmp.xlsx')), [])

    def test_missing_artifact_tool_falls_back_to_streaming(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "payload.json"
            payload.write_text("{}", encoding="utf-8")
            output = root / "out.xlsx"

            def fake_stream(_payload: Path, target: Path):
                target.write_bytes(b"xlsx")

            with patch.object(export_runtime, "ensure_artifact_tool", side_effect=RuntimeError("missing")), \
                 patch.object(export_runtime, "export_streaming_xlsx", side_effect=fake_stream) as mocked:
                actual = export_runtime.export_xlsx_atomic(
                    "node", root / "missing.js", payload, output, root
                )
            self.assertEqual(actual, output)
            self.assertEqual(output.read_bytes(), b"xlsx")
            mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
