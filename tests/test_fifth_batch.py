"""第五轮纯信号的因果性、边界与参考公式验收；不运行收益回测。"""
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fifth_batch as f
from fifth_policy import ALLOWED_FIFTH_NUMBERS, RETIRED_FIFTH_CODES

# 用户永久删除的81项只保留历史定义；信号回归仅执行保留项。
LOCAL_NUMBERS=tuple(sorted((f._IMPLEMENTED-f.MODEL_METHOD_NUMBERS)&ALLOWED_FIFTH_NUMBERS))

def data(n=3300,seed=824):
    rng=np.random.default_rng(seed)
    c=2000*np.exp(np.cumsum(rng.normal(0,.002,n)));o=np.r_[c[0],c[:-1]]
    h=np.maximum(o,c)+rng.uniform(.1,2,n);l=np.minimum(o,c)-rng.uniform(.1,2,n)
    v=rng.uniform(100,300,n);ct=1767225600000+60000*np.arange(1,n+1)
    buy=v*rng.uniform(.05,.95,n);extras={'quote_volume':v*c,'taker_buy_base':buy,'taker_buy_quote':buy*c,'delta_base':2*buy-v,'trades':rng.integers(30,100,n).astype(float)}
    return [o,h,l,c,v,ct,extras]


def cut(args,start=0,end=None):
    return [a[start:end] for a in args[:-1]]+[{k:a[start:end] for k,a in args[-1].items()}]


def call(number,args,tf='1m',direction=None):
    o,h,l,c,v,ct,extras=args
    return f.fifth_masks(number+263,direction,None,o,h,l,c,v,ct,extras,tf)


class FifthCausalityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.args=data()

    def test_all_registered_ids_are_stable_and_complete(self):
        self.assertEqual(tuple(f.FIFTH_SPECS),tuple(range(264,384)))
        self.assertEqual(len(f.KLINE_ONLY_FIFTH_CODES),87)
        self.assertEqual(len(f.MICROSTRUCTURE_FIFTH_CODES),15)
        self.assertEqual(len(f.EXTERNAL_FIFTH_CODES),18)
        self.assertEqual(len({s['research_id'] for s in f.FIFTH_SPECS.values()}),120)

    def test_all_local_rules_are_prefix_invariant(self):
        for number in LOCAL_NUMBERS:
            with self.subTest(number=number):
                whole=call(number,self.args);prefix=call(number,cut(self.args,end=2423))
                np.testing.assert_array_equal(whole[0][:2423],prefix[0]);np.testing.assert_array_equal(whole[1][:2423],prefix[1])
                self.assertEqual(whole[0].dtype,np.dtype(bool));self.assertFalse(np.any(whole[0]&whole[1]))

    def test_future_price_and_flow_change_does_not_rewrite_past(self):
        alternate=data(seed=947)
        changed=[a.copy() for a in self.args[:-1]]+[{k:a.copy() for k,a in self.args[-1].items()}]
        for j in range(5):changed[j][2423:]=alternate[j][2423:]
        for k in changed[-1]:changed[-1][k][2423:]=alternate[-1][k][2423:]
        for number in LOCAL_NUMBERS:
            with self.subTest(number=number):
                a=call(number,self.args);b=call(number,changed)
                np.testing.assert_array_equal(a[0][:2423],b[0][:2423]);np.testing.assert_array_equal(a[1][:2423],b[1][:2423])

    def test_gap_resets_state_and_warmup(self):
        changed=cut(self.args);changed[5]=changed[5].copy();changed[5][1650:]+=7*60000
        for number in LOCAL_NUMBERS:
            with self.subTest(number=number):
                whole=call(number,changed);later=call(number,cut(changed,start=1650))
                np.testing.assert_array_equal(whole[0][1650:],later[0]);np.testing.assert_array_equal(whole[1][1650:],later[1])

    def test_independent_direction_ignores_macd(self):
        args=cut(self.args,end=1700)
        for number in (15,23,25,26,28,29,31,55,70,71,72,77,79,81,84,86,96):
            with self.subTest(number=number):
                a=call(number,args,direction=np.ones(1700));b=call(number,args,direction=-np.ones(1700))
                np.testing.assert_array_equal(a,b)

    def test_constant_and_zero_volume_do_not_invent_signals(self):
        n=1700;args=[np.full(n,100.) for _ in range(4)]+[np.ones(n),1767225600000+60000*np.arange(1,n+1),{}]
        args[-1]={'quote_volume':args[4]*100,'taker_buy_base':args[4]*.5,'taker_buy_quote':args[4]*50,'delta_base':np.zeros(n),'trades':np.ones(n)}
        for number in LOCAL_NUMBERS:
            with self.subTest(number=number):
                self.assertFalse(any(a.any() for a in call(number,args)))
        args[4]=np.zeros(n)
        for number in (15,25,26,51,55,71,84,96):self.assertFalse(any(a.any() for a in call(number,args)))

    def test_missing_enhanced_data_is_an_error_not_zero_results(self):
        args=cut(self.args,end=20);args[-1]={}
        for number in (70,71,72,77):
            with self.subTest(number=number),self.assertRaisesRegex(ValueError,'字段'):call(number,args)

    def test_external_and_unimplemented_rules_are_explicit(self):
        for code in RETIRED_FIFTH_CODES:
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,'永久删除'):
                call(code-263,cut(self.args,end=20))
        for code,spec in f.FIFTH_SPECS.items():
            if spec['unavailable_reason']:self.assertNotIn(code,f.AVAILABLE_FIFTH_CODES)

    def test_time_based_rules_reject_rescaled_minutes(self):
        for number in (55,71,84,86,87,88,90):
            with self.subTest(number=number),self.assertRaises(ValueError):call(number,self.args,'5m')

    def test_events_are_stable_unique_and_available_only_at_close(self):
        args=self.args
        rows=f.fifth_event_records(288,*args[:-1],args[-1])
        self.assertTrue(rows);self.assertEqual(len(rows),len({r['event_id'] for r in rows}))
        for record in rows:
            self.assertEqual(record['signal_time'],record['available_time'])
            self.assertEqual(record['signal_time'],int(args[5][record['source_index']]))
            self.assertEqual(record['expires_at']-record['signal_time'],60000)


class FifthReferenceTests(unittest.TestCase):
    def test_ema_uses_sma_seed_then_exact_recurrence(self):
        got=f._ema(np.array([1.,2.,3.,8.,4.]),3)
        np.testing.assert_allclose(got,[np.nan,np.nan,2.,5.,4.5],equal_nan=True)
        got=f._ema(np.array([1.,2.,3.,np.nan,10.,20.,30.]),3)
        self.assertEqual(got[-1],20.);self.assertTrue(np.isnan(got[-2]))

    def test_pivots_wait_two_full_future_relative_bars(self):
        h=np.array([1.,2.,5.,3.,2.,4.,3.]);l=-h
        ph,pl=f._confirmed_pivots(h,l)
        self.assertFalse(np.isfinite(ph[:4]).any());self.assertEqual(ph[4],5.)
        self.assertFalse(np.isfinite(pl[:4]).any());self.assertEqual(pl[4],-5.)
        h=np.array([1.,2.,5.,5.,2.]);ph,_=f._confirmed_pivots(h,-h);self.assertFalse(np.isfinite(ph).any())

    def test_theil_sen_and_mann_kendall_include_current_closed_bar(self):
        x=np.arange(60,dtype=float)
        low,high=f._statistic(x,60,20);self.assertEqual(low[-1],1.);self.assertEqual(high[-1],1.)
        got,_=f._statistic(x,60,21);s=60*59/2;var=60*59*125/18
        self.assertAlmostEqual(got[-1],(s-1)/np.sqrt(var))

    def test_retired_hilo_cannot_execute_even_with_valid_native_data(self):
        c=np.array([100.,100.,100.,104.,104.,100.,97.,97.,105.]);h=c+1;l=c-1;o=c.copy();v=np.ones(len(c));ct=60000*np.arange(1,len(c)+1)
        with self.assertRaisesRegex(ValueError,'F5-006.*永久删除'):
            call(6,[o,h,l,c,v,ct,{}])

    def test_cusum_has_literal_five_bar_cooldown(self):
        up,dn=f._sequential(np.full(14,5.),np.ones(14),25)
        np.testing.assert_array_equal(np.flatnonzero(up),[0,6,12]);self.assertFalse(dn.any())

    def test_retired_cloud_is_readable_but_cannot_generate_signals(self):
        args=data(200)
        with self.assertRaisesRegex(ValueError,'F5-001.*永久删除'):
            call(1,args)
        self.assertIn('104',f.FIFTH_SPECS[264]['definition'])

    def test_day_profile_requires_full_previous_day(self):
        args=data(3000);o,h,l,c,v,ct,e=args;atr,_=f._atr(h,l,c)
        days,profiles=f._day_profiles(h,l,c,v,ct,atr)
        firstday=int(days[0]);self.assertNotIn(firstday,profiles);self.assertIn(firstday+1,profiles)
        _,partial=f._day_profiles(h[1:],l[1:],c[1:],v[1:],ct[1:],atr[1:]);self.assertNotIn(firstday+1,partial)

    def test_native_resample_only_emits_complete_close(self):
        args=data(19);o,h,l,c,v,ct,e=args
        idx,t,*rest=f._native(o,h,l,c,v,ct,5)
        np.testing.assert_array_equal(idx,[4,9,14]);self.assertTrue(np.all(t%300000==0))
        self.assertEqual(rest[1][0],max(h[:5]));self.assertEqual(rest[-1][0],sum(v[:5]))

    def test_sar_matches_fixed_talib_style_reference(self):
        # 固定高低摆动，核验SAR状态翻向只在本根收盘可见，不输出初始方向。
        h=np.array([10.,11.,12.,13.,12.,10.,9.,8.,9.,11.,12.]);l=h-2;c=h-1
        up,dn=f._sar_masks(h,l,c)
        self.assertFalse(up[:2].any());self.assertFalse(dn[:2].any())
        self.assertEqual(np.flatnonzero(dn).tolist(),[5]);self.assertEqual(np.flatnonzero(up).tolist(),[10])


    def test_gap_zone_formation_is_not_a_visit_and_first_visit_is_consumed(self):
        o=np.array([9.5,9.5,11.5,11.5,11.4]);c=np.array([9.5,11.5,11.5,11.4,11.5])
        h=np.array([10.,11.7,12.,11.8,11.8]);l=np.array([9.,9.4,11.,10.7,10.8]);atr=np.ones(5);v=np.ones(5)
        up,dn=f._structure_masks(46,o,h,l,c,v,atr)
        self.assertEqual(np.flatnonzero(up).tolist(),[3]);self.assertFalse(dn.any())
        c[3]=10.9
        up,dn=f._structure_masks(46,o,h,l,c,v,atr)
        self.assertFalse(up.any());self.assertFalse(dn.any())

    def test_invalidated_gap_requires_a_later_retest(self):
        o=np.array([9.5,9.5,11.5,11.5,9.7]);c=np.array([9.5,11.5,11.5,9.7,9.8])
        h=np.array([10.,11.7,12.,11.6,10.1]);l=np.array([9.,9.4,11.,9.5,9.4])
        up,dn=f._structure_masks(47,o,h,l,c,np.ones(5),np.ones(5))
        self.assertEqual(np.flatnonzero(dn).tolist(),[4]);self.assertFalse(up.any())

    def test_point_and_figure_multibox_breakout_fires_on_completion_bar(self):
        c=np.r_[np.full(1440,100.),[100.,102.,100.,103.]];o=c.copy();h=c+.1;l=c-.1;v=np.ones(len(c));ct=1767225600000+60000*np.arange(1,len(c)+1)
        up,dn=f._daily_masks(84,o,h,l,c,v,ct,{},np.ones(len(c)))
        self.assertEqual(np.flatnonzero(up).tolist(),[1443]);self.assertFalse(dn.any())

    def test_flow_surprise_does_not_train_on_current_transition(self):
        n=1441;c=np.full(n,100.);c[-1]=100.2;o=c.copy();h=c+.1;l=c-1;v=np.full(n,100.);ct=60000*np.arange(1,n+1)
        delta=np.full(n,-80.);delta[-1]=80.
        up,dn=f._flow_masks(69,o,h,l,c,v,ct,{'delta_base':delta},np.ones(n))
        self.assertEqual(np.flatnonzero(up).tolist(),[1440]);self.assertFalse(dn.any())

    def test_record_rate_waits_for_all_historical_record_windows(self):
        c=np.arange(100.,400.);o=c.copy();h=c+.1;l=c-.1;v=np.ones(len(c));ct=60000*np.arange(1,len(c)+1)
        up,dn=call(96,[o,h,l,c,v,ct,{}])
        self.assertFalse(up[:139].any());self.assertFalse(dn[:139].any())


if __name__=='__main__':unittest.main()
