"""End-to-end selection, save/restore, and model evidence regressions."""
import gzip
import json
from pathlib import Path
import pickle
import tempfile
import unittest
from unittest import mock
import numpy as np

import test_v152_fingerprint_queue_ui as fixture
from selection_config import 规范化配置, 配置统计, 结果入场口径列表
from strategy_space import 固定比例代码, 百分数转比例


class NewControlsTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FingerprintQueueUiTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FingerprintQueueUiTests.tearDownClass.__func__)
    setUp = fixture.FingerprintQueueUiTests.setUp
    tearDown = fixture.FingerprintQueueUiTests.tearDown

    def test_both_manual_fields_keep_precision_after_save_and_restore(self):
        panel=self.app.selection_panel
        expected={}
        for picker,prefix,key in ((panel.fixed_picker,'FSL','固定止损代码'),
                                  (panel.overlay_picker,'FTP','叠加止盈代码')):
            picker.weekday_var.set('0.123456%');picker.weekend_var.set('1.25')
            picker._add_manual()
            expected[key]=固定比例代码(prefix,百分数转比例('0.123456'),百分数转比例('1.25'))
        config=panel.get_config()
        for key,code in expected.items():self.assertIn(code,config[key])
        self.app.save_user_settings();saved=self.write.call_args.args[1]['组合选择']
        panel.apply_config(saved)
        for key,code in expected.items():self.assertIn(code,panel.get_config()[key])

    def test_fifth_catalog_shows_39_but_historical_120_definitions_remain(self):
        import tkinter.ttk as ttk
        from fifth_batch import FIFTH_SPECS
        panel=self.app.selection_panel;dialog=panel._show_fifth_catalog()
        try:
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child;yield from descendants(child)
            tree=next(w for w in descendants(dialog) if isinstance(w,ttk.Treeview))
            self.assertEqual(len(tree.get_children()),39)
            self.assertTrue(all(not panel._entry_available('1m',c) for c in range(366,384)))
            self.assertEqual(len(FIFTH_SPECS),120)
        finally:dialog.destroy()

    def test_fifth_native_event_survives_ui_save_without_hidden_one_minute_rule(self):
        panel=self.app.selection_panel;config=panel.get_config()
        panel.set_data_capabilities({'ohlcv':True}, timeframe_capabilities={
            tf:{'ohlcv':True} for tf in ('4h','1h','15m','5m','1m')})
        config['开仓条件']={tf:[0] for tf in ('4h','1h','15m','5m','1m')}
        config['开仓条件']['5m']=[288];config['入场触发口径']='F5_EVENT'
        panel.apply_config(config);actual=panel.get_config()
        self.assertEqual(actual['开仓条件']['1m'],[0])
        self.assertEqual(actual['开仓条件']['5m'],[288])
        self.assertEqual(actual['入场触发口径'],'F5_EVENT')
        self.assertEqual(规范化配置(actual)['入场触发口径'],'F5_EVENT')


class ModelArchiveTests(unittest.TestCase):
    def test_timeframes_and_disconnected_segments_never_overwrite_predictions(self):
        import fifth_model_audit as audit
        with tempfile.TemporaryDirectory() as folder:
            audit.set_output_directory(folder)
            try:
                for tf,ct in [('1m',100),('5m',300),('1m',500)]:
                    audit.set_timeframe(tf)
                    audit.record_fit(37,ct,{'coef':[.1,.2]},{'horizon':1})
                    audit.record_predictions(37,[ct],[ct/100])
            finally:audit.close_archives();audit.set_output_directory(None)
            arrays=list(Path(folder).glob('*.npz'))
            self.assertEqual(len(arrays),3)
            self.assertEqual(sorted(float(np.load(p)['predictions'][0]) for p in arrays),[1,3,5])
            with gzip.open(Path(folder)/'F5-037_1m_训练快照.pkl.gz','rb') as stream:
                self.assertEqual([pickle.load(stream)['fit_at'],pickle.load(stream)['fit_at']],['100','500'])


if __name__=='__main__':unittest.main()
