"""Portable campaign export: UTC limits, source boundary and exclusive output."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import campaign_export as campaign
from selection_config import 全选配置


class CampaignExportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.project = self.tmp / "source"
        self.project.mkdir()
        names = set(getattr(campaign, "EXPORT_SOURCE_FILES", ()))
        # Before the explicit catalog exists, reproduce the original three-file export.
        names.update(("campaign_export.py", "campaign_runner.py", "campaign_planner.py",
                      "requirements.txt", "AGENTS.md"))
        for name in names:
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# public test source\n", encoding="utf-8")
        self.data = self.tmp / "main.csv"
        self.data.write_text("openTime,open,high,low,close,volume\n0,1,1,1,1,1\n", encoding="utf-8")
        self.target = self.tmp / "campaign.zip"
        self.stack.enter_context(mock.patch.object(campaign, "__file__", str(self.project / "campaign_export.py")))
        self.first = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.last = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.dates = self.stack.enter_context(mock.patch.object(campaign, "data_dates", return_value=(self.first, self.last)))

    def tearDown(self):
        self.stack.close()

    def export(self, **kwargs):
        return campaign.export_bundle(self.target, self.data, 全选配置(), **kwargs)

    def test_default_window_is_latest_120_days_in_utc_and_all_hashes_match(self):
        result = self.export()
        settings = result["settings"]
        self.assertEqual(datetime.fromisoformat(settings["start"]), self.last - timedelta(days=120))
        self.assertEqual(datetime.fromisoformat(settings["validation_end"]), self.last)
        self.assertEqual(settings["end"], settings["validation_start"])
        for key in ("start", "end", "validation_start", "validation_end"):
            self.assertTrue(settings[key].endswith("+00:00"))
        with zipfile.ZipFile(self.target) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest, result)
            self.assertEqual(json.loads(archive.read("settings.json")), settings)
            data = archive.read("data/market.csv")
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["data_sha256"])
            self.assertEqual(len(data), manifest["data_bytes"])
            for name, digest in manifest["code_sha256"].items():
                self.assertEqual(hashlib.sha256(archive.read("app/" + name)).hexdigest(), digest)
            self.assertIsNone(archive.testzip())

    def test_explicit_timezone_is_converted_not_silently_relabelled(self):
        settings = self.export(start="2026-08-01T08:00:00+08:00", end="2026-09-21T09:00:00+09:00")["settings"]
        self.assertEqual(settings["start"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(settings["validation_end"], "2026-09-21T00:00:00+00:00")

    def test_out_of_bounds_reversed_or_too_short_window_is_rejected(self):
        for start, end in (("2025-12-31", "2026-08-01"), ("2026-08-01", "2026-09-22"),
                           ("2026-09-01", "2026-09-21"), ("2026-09-21", "2026-08-01")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.export(start=start, end=end)
            self.assertFalse(self.target.exists())

    def test_only_primary_csv_and_whitelisted_code_no_settings_or_credentials(self):
        # These user files must never hitchhike on a broad '*.py' source glob.
        for name in ("credentials.py", "deploy_private.py", "用户设置.json", "api_keys.json", "funding.csv", "oi.csv"):
            (self.project / name).write_text("PRIVATE_TEST_SENTINEL", encoding="utf-8")
        self.export()
        with zipfile.ZipFile(self.target) as archive:
            names = archive.namelist()
            self.assertEqual([name for name in names if name.startswith("data/")], ["data/market.csv"])
            for name in names:
                self.assertNotIn(b"PRIVATE_TEST_SENTINEL", archive.read(name))
            self.assertNotIn("app/credentials.py", names)
            self.assertNotIn("app/deploy_private.py", names)
            self.assertNotIn("app/用户设置.json", names)

    def test_existing_target_is_not_overwritten_or_deleted(self):
        self.target.write_bytes(b"another task result")
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(self.target.read_bytes(), b"another task result")
        self.dates.assert_not_called()

    def test_race_before_exclusive_create_preserves_other_file(self):
        original = zipfile.ZipFile
        def race(path, mode="r", *args, **kwargs):
            if mode == "x":
                Path(path).write_bytes(b"other process won exclusive creation")
            return original(path, mode, *args, **kwargs)
        with mock.patch.object(campaign.zipfile, "ZipFile", side_effect=race), self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.target.read_bytes(), b"other process won exclusive creation")

    def test_archive_failure_removes_only_its_own_new_partial_output(self):
        other = self.tmp / "existing.zip"
        other.write_bytes(b"preserve")
        with mock.patch.object(zipfile.ZipFile, "write", side_effect=OSError("simulated full disk")), self.assertRaises(OSError):
            self.export()
        self.assertFalse(self.target.exists())
        self.assertEqual(other.read_bytes(), b"preserve")
        self.assertTrue(self.data.exists())

    def test_actual_data_bounds_come_from_content_not_misleading_filename(self):
        # Call the original function rather than parsing mocked values.
        path = self.tmp / "ETHUSDC_1900-01-01_2099-01-01.csv"
        start = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
        path.write_text(f"openTime\n{start + 60000}\n{start}\n", encoding="utf-8")
        with mock.patch.object(campaign, "data_dates", _REAL_DATA_DATES):
            first, last = campaign.data_dates(path)
        self.assertEqual(first, datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(last, datetime(2026, 9, 1, 0, 2, tzinfo=timezone.utc))


_REAL_DATA_DATES = campaign.data_dates


if __name__ == "__main__":
    unittest.main()
