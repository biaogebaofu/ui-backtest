"""Regressions reproduced while auditing the shipped v1.41 ZIP.

Synthetic prices below are test fixtures, NOT profitability evidence.
"""
from __future__ import annotations
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
import numpy as np
import pandas as pd

import data_sources as D
import selection_config as C
import candidate_export as CE
import worst_export as WE
from feature_builder import build_features
from run_safety import atomic_json, verify_run_identity, exclusive_output

MS = 1777593600000
ROOT = Path(__file__).resolve().parents[1]

def candles(n=600):
    return pd.DataFrame({'openTime': MS + np.arange(n)*60000, 'open': 100.,
                         'high': 101., 'low': 99., 'close': 100., 'volume': 10.})

class DataAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root/'ETHUSDC_klines_1m.csv'
        candles().to_csv(self.base, index=False)
    def tearDown(self):
        self.tmp.cleanup()
    def write(self, name, frame):
        path = self.root/name
        frame.to_csv(path, index=False)
        return path

    def test_epoch_units_and_iso_are_the_same_utc_instant(self):
        for value in (MS//1000, MS, MS*1000, MS*1000000, '2026-05-01T00:00:00Z'):
            with self.subTest(value=value):
                self.assertEqual(D._normalize_timestamp(pd.Series([value])).tolist(), [MS])
        self.assertEqual(D._normalize_timestamp(pd.Series([0, 60000])).tolist(), [0,60000])

    def test_invalid_and_mixed_time_units_are_rejected(self):
        for values in ([MS, 'nonsense'], [MS, MS*1000], [-MS], [float('inf')]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                D._normalize_timestamp(pd.Series(values))

    def test_primary_missing_timestamp_is_not_silently_dropped(self):
        b = candles(); b.loc[10, 'openTime'] = np.nan
        b.to_csv(self.base,index=False)
        with self.assertRaisesRegex(ValueError,'空时间戳'):
            D.load_merged_source(self.base)

    def test_string_boolean_maker_flag_is_not_truthiness(self):
        frame = pd.DataFrame({'timestamp':[MS,MS+1], 'price':[100.,100.], 'quantity':[1.,2.],
                              'is_buyer_maker':['false','true']})
        result=D._aggregate_raw_aggtrades(frame)
        self.assertEqual(result.taker_buy_base.tolist(),[1.])
        self.assertEqual(result.agg_trades.tolist(),[2])
        # A count of aggregate records must not impersonate a raw trade count.
        self.assertNotIn('trades',result)

    def test_short_binance_aggtrade_schema_and_raw_count(self):
        frame=pd.DataFrame({'a':[1,2], 'p':[100.,100.], 'q':[1.,2.], 'T':[MS,MS+1],
                            'm':['false','true'],'f':[10,12],'l':[11,14], 's':['ETHUSDC']*2})
        p=self.write('raw.csv',frame)
        result=D._normalize_micro(p,'ETHUSDC')
        self.assertEqual(result.trades.tolist(),[5])
        self.assertEqual(result.taker_buy_base.tolist(),[1.])

    def test_duplicate_aggtrade_ids_deduplicate_exact_and_reject_conflict(self):
        row={'agg_trade_id':1,'timestamp':MS,'price':100.,'quantity':1.,'is_buyer_maker':False}
        result=D._aggregate_raw_aggtrades(pd.DataFrame([row,row]))
        self.assertEqual(result.agg_trades.tolist(),[1])
        other=dict(row,quantity=2.)
        with self.assertRaisesRegex(ValueError,'内容冲突'):
            D._aggregate_raw_aggtrades(pd.DataFrame([row,other]))

    def test_alias_columns_coalesce_or_fail_explicitly(self):
        good=pd.DataFrame({'quoteVolume':[1.,np.nan], 'quote_volume':[1.,2.]})
        out=D._normalize_columns(good)
        self.assertEqual(out.columns.tolist(),['quote_volume'])
        self.assertEqual(out.quote_volume.tolist(),[1.,2.])
        good.loc[0,'quoteVolume']=3.
        with self.assertRaisesRegex(ValueError,'别名字段内容冲突'):
            D._normalize_columns(good)

    def test_sparse_overlay_replaces_column_without_xy_suffixes(self):
        base=candles();base['funding_rate']=.1;base.to_csv(self.base,index=False)
        ext=self.write('fund.csv',pd.DataFrame({'fundingTime':[MS], 'fundingRate':[.0001]}))
        result=D.load_merged_source(self.base,funding=ext)
        self.assertIn('funding_rate',result)
        self.assertFalse(any(c.endswith('_x') or c.endswith('_y') for c in result))
        self.assertAlmostEqual(float(result.funding_rate.iloc[0]),.0001)

    def test_sparse_alignment_never_uses_future_values(self):
        ext=self.write('fund.csv',pd.DataFrame({'funding_time':[MS+120000], 'funding_rate':[.001]}))
        result=D.load_merged_source(self.base,funding=ext)
        self.assertTrue(pd.isna(result.funding_rate.iloc[0]))
        self.assertEqual(result.funding_rate.iloc[1],.001)

    def test_sparse_duplicate_timestamp_conflict_rejected(self):
        ext=self.write('fund.csv',pd.DataFrame({'funding_time':[MS,MS], 'funding_rate':[.001,.002]}))
        with self.assertRaisesRegex(ValueError,'内容冲突'):
            D.load_merged_source(self.base,funding=ext)

    def test_symbol_content_checked_even_when_filename_generic(self):
        ext=self.write('fund.csv',pd.DataFrame({'timestamp':[MS], 'funding_rate':[.001], 'symbol':['ETHUSDT']}))
        with self.assertRaisesRegex(ValueError,'品种不一致'):
            D.load_merged_source(self.base,funding=ext)

    def test_inspector_and_worker_both_reject_invalid_ohlc(self):
        frame=candles();frame.loc[2,'high']=90.;frame.to_csv(self.base,index=False)
        with self.assertRaisesRegex(ValueError,'OHLC价格关系'):
            D.inspect_sources(str(self.base))
        with self.assertRaisesRegex(ValueError,'OHLC价格关系'):
            build_features(str(self.base),str(self.root/'cache'))

    def test_inspector_rejects_discontinuous_data(self):
        candles().drop(index=12).to_csv(self.base,index=False)
        with self.assertRaisesRegex(ValueError,'缺口'):
            D.inspect_sources(str(self.base))

    def test_capability_coverage_requires_simultaneous_finite_fields(self):
        self.assertFalse(D.capabilities_from_frame(pd.DataFrame({'trades':[np.inf]}))['trades'])
        x=pd.DataFrame({'quote_volume':[1.,np.nan], 'taker_buy_quote':[np.nan,1.]})
        self.assertFalse(D.capabilities_from_frame(x)['taker_quote'])

    def test_partial_external_minutes_do_not_erase_existing_trade_counts(self):
        base=candles();base['trades']=20.;base.to_csv(self.base,index=False)
        ext=self.write('micro.csv',pd.DataFrame({'openTime':[MS],'trades':[10.]}))
        result=D.load_merged_source(self.base,micro=ext)
        self.assertEqual(result.trades.iloc[0],10.)
        self.assertEqual(result.trades.iloc[1],20.)
        self.assertEqual(result.avg_trade_size.iloc[1],.5)

    def test_inconsistent_taker_volume_is_rejected(self):
        b=candles();b['takerBuyBaseVol']=11.;b.to_csv(self.base,index=False)
        with self.assertRaisesRegex(ValueError,'大于'):
            D.load_merged_source(self.base)

    def test_missing_minutes_do_not_become_zero_high_tf_volume(self):
        b=candles();b['trades']=1.;b.loc[0,'trades']=np.nan;b.to_csv(self.base,index=False)
        path=build_features(str(self.base),str(self.root/'cache'))
        with np.load(path) as f:
            self.assertTrue(np.isnan(f['5m_trades'][0]))
            self.assertEqual(f['5m_trades'][1],5.)

    def test_high_tf_state_preserves_missing_last_sample(self):
        b=candles();b['funding_rate']=.001;b.loc[4,'funding_rate']=np.nan;b.to_csv(self.base,index=False)
        path=build_features(str(self.base),str(self.root/'cache'))
        with np.load(path) as f:
            self.assertTrue(np.isnan(f['5m_funding_rate'][0]))

    def test_fingerprint_detects_same_size_same_mtime_change(self):
        p=self.root/'tiny.csv';p.write_bytes(b'abc');st=p.stat()
        before=D.source_bundle_fingerprint({'kline':str(p)})
        p.write_bytes(b'xyz');os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
        after=D.source_bundle_fingerprint({'kline':str(p)})
        self.assertNotEqual(before,after)

    def test_bundle_ambiguity_is_not_silently_first_file(self):
        zpath=self.root/'data.zip'
        with zipfile.ZipFile(zpath,'w') as z:
            z.writestr('day1/ETHUSDC_klines_1m.csv',self.base.read_bytes())
            z.writestr('day2/ETHUSDC_klines_1m.csv',self.base.read_bytes())
        with self.assertRaisesRegex(ValueError,'多个kline'):
            D.resolve_sources(bundle=str(zpath),cache_root=self.root/'cache')
        # An explicit source bypasses ambiguity for that kind, as the UI says.
        got=D.resolve_sources(primary=str(self.base),bundle=str(zpath),cache_root=self.root/'cache')
        self.assertEqual(got['kline'],str(self.base))

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'), '本测试环境未安装pyarrow；未声称完成Parquet实测')
    def test_parquet_and_csv_normalize_identically(self):
        p=self.root/'ETHUSDC.parquet';candles().to_parquet(p,index=False)
        pd.testing.assert_frame_equal(D.load_merged_source(self.base),D.load_merged_source(p))

class ConfigAndExportAuditTests(unittest.TestCase):
    def test_time_stop_alone_is_valid(self):
        cfg=C.全选配置();cfg['止损代码']=[];cfg['固定止损代码']=['OFF'];cfg['强制时间止损分钟']=[5]
        self.assertEqual(C.规范化配置(cfg)['止损代码'],['OFF'])

    def test_nonfinite_capital_is_rejected(self):
        for value in (float('nan'),float('inf'),float('-inf')):
            cfg=C.全选配置();cfg['资金约束']['初始资金USDC']=value
            with self.subTest(value=value),self.assertRaises(ValueError): C.规范化配置(cfg)

    def test_explicit_empty_fixed_picker_is_not_reset_to_eleven_rules(self):
        cfg=C.全选配置();cfg['固定止损代码']=[]
        with self.assertRaisesRegex(ValueError,'OFF'): C.规范化配置(cfg)

    def test_nonfinite_candidate_profit_factor_is_rejected(self):
        with self.assertRaises(ValueError): C.规范化候选筛选({'盈亏比下限':float('nan')})

    def test_all_export_callers_accept_missing_node(self):
        import backtest_worker as W
        for module in (CE,WE):
            with mock.patch.object(module,'find_node',return_value=None), \
                 mock.patch.object(module,'export_xlsx_atomic',return_value=Path('out.xlsx')) as call:
                self.assertEqual(module.export_excel(Path('.'),Path('payload.json'),Path('out.xlsx')),Path('out.xlsx'))
                self.assertIsNone(call.call_args.args[0])
        with mock.patch.object(W,'find_node',return_value=None), \
             mock.patch.object(W,'export_xlsx_atomic',return_value=Path('out.xlsx')) as call:
            W.export_excel(Path('.'),Path('.'),Path('payload.json'),'out.xlsx')
            call.assert_called_once()

class RunSafetyAuditTests(unittest.TestCase):
    def test_identity_refuses_changed_data_before_touching_results(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);a={'source':'old','code':'a'}
            atomic_json(p/'回测运行身份.json',a)
            result=p/'全部回测结果.csv';result.write_bytes(b'preserve me')
            with self.assertRaisesRegex(RuntimeError,'禁止新旧结果混写'):
                verify_run_identity(p,{'source':'new','code':'a'})
            self.assertEqual(result.read_bytes(),b'preserve me')
            verify_run_identity(p,a)

    def test_old_checkpoint_without_identity_is_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'断点记录.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'缺少可核验'):verify_run_identity(p,{})
            verify_run_identity(p,{},restart=True)

    def test_output_lock_blocks_a_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as t:
            # Match child output and pipe decoding explicitly on Windows.
            command=[sys_executable(),'-X','utf8','-c',
                     'from pathlib import Path;from run_safety import exclusive_output;'
                     f'\nwith exclusive_output(Path({t!r})): pass']
            with exclusive_output(Path(t)):
                proc=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
                self.assertNotEqual(proc.returncode,0)
                self.assertIn('另一个回测进程',proc.stderr)
            with exclusive_output(Path(t)):
                pass

def sys_executable():
    import sys
    return sys.executable

try:
    import tkinter as tk
    root=tk.Tk();root.destroy()
    DISPLAY=True
except Exception:
    DISPLAY=False

@unittest.skipUnless(DISPLAY,'需要显示环境，CI请用xvfb-run')
class UiAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ui
        cls.ui=ui
        cls.save=mock.patch.object(ui.App,'save_user_settings',return_value=True);cls.save.start()
        cls.load=mock.patch.object(ui.App,'load_user_settings');cls.load.start()
        cls.app=ui.App();cls.app.withdraw();cls.app.update_idletasks()
    @classmethod
    def tearDownClass(cls):
        cls.app.destroy();cls.load.stop();cls.save.stop()
    def tearDown(self):
        self.app.selection_panel.clear_data_capabilities()
        self.app._active_job_id=None;self.app._closing=False
        self.app.proc=None;self.app._data_inspecting=False
        for name in ('_close_after_id',):
            task=getattr(self.app,name,None)
            if task:
                self.app.after_cancel(task);setattr(self.app,name,None)
        while not self.app.messages.empty():self.app.messages.get_nowait()

    def test_group_select_does_not_reenable_unavailable_rules(self):
        panel=self.app.selection_panel
        panel.set_data_capabilities({'ohlcv':True})
        panel._set_third_source('1m','external')
        self.assertFalse(panel.case_vars['1m'][128].get())
        panel.third_entry_all_vars['1m'].set(True);panel._set_third_round('1m')
        self.assertFalse(panel.case_vars['1m'][128].get())
        panel.apply_config(C.全选配置())
        self.assertFalse(panel.case_vars['1m'][128].get())

    def test_stale_data_report_is_discarded(self):
        a=self.app;a._data_generation=10;a._data_inspecting=True
        a.messages.put({'type':'data_inspection','generation':9,'report':{'capabilities':{'funding':True}}})
        with mock.patch.object(a,'_apply_data_report') as apply:
            a.poll_messages();apply.assert_not_called()
        self.assertIn('旧结果已丢弃',a.data_summary_var.get())

    def test_old_process_exit_does_not_unlock_new_job(self):
        a=self.app;a._active_job_id=2;a.start_btn.configure(state='disabled')
        a.messages.put({'type':'process_exit','job_id':1,'code':0})
        a.poll_messages()
        self.assertEqual(str(a.start_btn.cget('state')),'disabled')

    def test_read_output_does_not_call_tk_and_handles_nonobject_json(self):
        a=self.app;proc=mock.Mock();proc.stdout=io.StringIO('123\n{"type":"log","message":"ok"}\n');proc.wait.return_value=0
        with tempfile.TemporaryDirectory() as t, mock.patch.object(a,'output_dir',side_effect=AssertionError('must not call Tk')):
            a.read_output(proc,Path(t)/'log.txt',77)
        messages=[]
        while not a.messages.empty():messages.append(a.messages.get_nowait())
        self.assertEqual(messages[0]['type'],'log')
        self.assertEqual(messages[-1]['type'],'process_exit')
        self.assertTrue(all(x['job_id']==77 for x in messages))

    def test_safe_close_waits_for_worker_instead_of_destroying_pipe(self):
        a=self.app;a.proc=mock.Mock();a.proc.poll.return_value=None
        with mock.patch.object(self.ui.messagebox,'askyesno',return_value=True), \
             mock.patch.object(a,'stop_run') as stop, mock.patch.object(a,'destroy') as destroy:
            a.on_close();stop.assert_called_once();destroy.assert_not_called()
            self.assertTrue(a._closing)

    def test_thread_zero_rejected_by_ui(self):
        a=self.app
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'x.csv';p.write_text('x')
            a.csv_var.set(str(p));a.bundle_var.set('');a.thread_var.set(0)
            with mock.patch.object(a,'current_selection',return_value=C.全选配置()), \
                 mock.patch.object(self.ui.messagebox,'showerror') as error:
                self.assertFalse(a.validate_paths(Path(t)/'out'))
                self.assertEqual(error.call_args.args[0], '并发设置错误')
                self.assertIn('CPU并发数必须是1到', error.call_args.args[1])
        a.thread_var.set(1)

    def test_popen_failure_is_shown_without_freezing_buttons(self):
        a=self.app;a.start_btn.configure(state='normal')
        with mock.patch.object(self.ui.subprocess,'Popen',side_effect=OSError('denied')), \
             mock.patch.object(self.ui.messagebox,'showerror'):
            self.assertFalse(a._launch_process(['missing'],ROOT,'backtest'))
        self.assertEqual(str(a.start_btn.cget('state')),'normal')

    def test_candidate_export_passes_selected_csv(self):
        a=self.app;a.proc=None
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'renamed.csv';p.write_text('sample')
            with mock.patch.object(self.ui.filedialog,'askopenfilename',return_value=str(p)), \
                 mock.patch.object(a,'_launch_process',return_value=False) as launch:
                a.start_candidate_export()
            args=launch.call_args.args[0]
            self.assertEqual(args[args.index('--source')+1],str(p))

if __name__=='__main__':
    unittest.main()

class ScrollViewportTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from unittest import mock
        import ui
        try:
            probe=tk.Tk();probe.destroy()
        except tk.TclError:
            self.skipTest('需要可用显示')
        self.patch=mock.patch.object(ui.App,'save_user_settings');self.patch.start()
        # 此组检查编辑器滚动，不能受用户保存的精确列表模式影响。
        with mock.patch.object(ui.App,'load_user_settings'):
            self.app=ui.App()
        self.app.geometry('1080x720+0+0');self.app.update()
    def tearDown(self):
        if hasattr(self,'app'): self.app.destroy()
        if hasattr(self,'patch'): self.patch.stop()
    def test_stop_and_tp_tables_can_be_reached_at_min_size(self):
        app=self.app;app.notebook.select(1)
        for i,form,tree in [(1,app.selection_panel.stop_scroll,app.selection_panel.stop_tree),
                            (2,app.selection_panel.tp_scroll,app.selection_panel.tp_tree)]:
            app.selection_panel.pages.select(i);app.update()
            self.assertGreater(tree.winfo_height(),100)
            form.canvas.yview_moveto(1.);app.update()
            self.assertTrue(tree.winfo_ismapped())
            self.assertLess(tree.winfo_rooty(),form.canvas.winfo_rooty()+form.canvas.winfo_height())
    def test_candidate_export_and_run_footer_stay_reachable(self):
        from tkinter import ttk
        def walk(w):
            yield w
            for child in w.winfo_children():yield from walk(child)
        app=self.app;app.notebook.select(3);app.update()
        app.candidate_scroll.canvas.yview_moveto(1.);app.update()
        btn=next(w for w in walk(app.candidate_scroll) if isinstance(w,ttk.Button) and w.cget('text')=='选择已有结果目录并生成候选')
        self.assertLess(btn.winfo_rooty()+btn.winfo_height(),app.winfo_rooty()+app.winfo_height())
        app.notebook.select(2)
        for var in app.fold_vars.values():var.set(False)
        app._apply_folds();app.update()
        self.assertLess(app.start_btn.winfo_rooty()+app.start_btn.winfo_height(),app.winfo_rooty()+app.winfo_height())
        self.assertGreater(app.run_scroll.canvas.bbox('all')[3],app.run_scroll.canvas.winfo_height())
    def test_matrix_single_editor_apply_and_cancel_are_visible(self):
        from tkinter import ttk,Toplevel
        app=self.app;app.notebook.select(1);app.selection_panel.pages.select(1);app.update()
        picker=app.selection_panel.fixed_picker
        previous=set(picker.read());picker.matrix();picker.matrix();app.update()
        dialogs=[w for w in picker.winfo_children() if isinstance(w,Toplevel)]
        self.assertEqual(len(dialogs),1)
        dialog=dialogs[0];dialog.geometry('900x400+0+0');app.update()
        buttons=[w for child in dialog.winfo_children() for w in child.winfo_children() if isinstance(w,ttk.Button)]
        for label in ('应用','取消'):
            button=next(w for w in buttons if w.cget('text')==label)
            # Compare client bounds in the same coordinate space, excluding OS chrome.
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_rooty(),dialog.winfo_rooty())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                 dialog.winfo_rooty()+dialog.winfo_height())
            self.assertGreaterEqual(button.winfo_rootx(),dialog.winfo_rootx())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),
                                 dialog.winfo_rootx()+dialog.winfo_width())
        next(w for w in buttons if w.cget('text')=='取消').invoke()
        self.assertEqual(set(picker.read()),previous)
