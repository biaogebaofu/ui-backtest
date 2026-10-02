"""Exercise the real settings loader with only in-memory settings and a hidden UI."""
import copy
import json
from pathlib import Path
from unittest import mock
import unittest

import test_v152_fingerprint_queue_ui as fixture


class SettingsMigrationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FingerprintQueueUiTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FingerprintQueueUiTests.tearDownClass.__func__)
    setUp = fixture.FingerprintQueueUiTests.setUp
    tearDown = fixture.FingerprintQueueUiTests.tearDown
    assert_nested_equal = fixture.FingerprintQueueUiTests.assert_nested_equal

    def test_saved_editor_and_queue_are_cleaned_without_changing_account_or_launching(self):
        app = self.app
        app.save_user_settings()
        saved = copy.deepcopy(self.write.call_args.args[1])
        original = copy.deepcopy(saved["组合选择"])
        config = saved["组合选择"]
        config["开仓条件"] = {tf: [0] for tf in ("4h", "1h", "15m", "5m", "1m")}
        config["开仓条件"]["1m"] = [42, 264, 288]
        config["入场触发口径"] = "F5_EVENT"
        saved["多指纹策略列表"] = [{"fingerprint": "a" * 16, "source_dir": str(Path.cwd() / "_synthetic_fixture" / "test-source"),
                                    "row": {"1分钟条件": "F5-001 一目云层与转换线交叉"}}]
        path = mock.Mock(is_file=mock.Mock(return_value=True),
                         read_text=mock.Mock(return_value=json.dumps(saved)))
        with mock.patch.object(self.ui, "用户设置文件", path), \
                mock.patch.object(app, "_launch_process") as launch, \
                mock.patch.object(app, "_start_fingerprint_job") as lookup:
            type(self).original_load(app)
        self.error.assert_not_called()
        self.assertFalse(app._settings_restore_error)
        launch.assert_not_called()
        lookup.assert_not_called()
        self.assertEqual(app.fingerprint_queue, [])
        actual = app.current_selection()
        self.assertEqual(actual["开仓条件"]["1m"], [42, 288])
        self.assertEqual(actual["入场触发口径"], "LIVE_01")
        for key in ("资金约束", "手续费", "成交偏移", "成本模式", "止损代码", "止盈方案编号"):
            self.assert_nested_equal(actual[key], original[key])
        self.assertIn("永久删除", app.selection_panel.policy_notice_var.get())
        written = self.write.call_args.args[1]
        self.assertEqual(written["组合选择"], actual)
        self.assertEqual(written["多指纹策略列表"], [])


if __name__ == "__main__":
    unittest.main()
