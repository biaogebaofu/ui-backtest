import sys
from pathlib import Path
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hedge_engine import run_case
from hedge_config import normalize_config


def source(n, price=100.):
    d={k:np.full(n,price) for k in ['o','h','l','c']}
    d['ts']=np.arange(n,dtype=np.int64)*60000+1735689600000
    d['tp']=np.full(n,.004)
    return d


def once(d, **kwargs):
    signals=np.zeros((len(d['ts']),2),np.bool_);signals[:,0]=True
    return run_case(d, {'initial_equity':2000.,'mode':'scale_in',**kwargs.pop('config',{})},
                    kwargs.pop('hours',12),0,signals=signals,run_ids=np.zeros(len(signals),np.int64),
                    add_drop=.001,detail=True,**kwargs)


class HedgeEngineTests(unittest.TestCase):
    def test_standing_tp_can_fill_later_same_bar_but_not_earlier_high(self):
        d=source(2); d['l'][1]=99.98;d['h'][1]=101.
        before=once(d,path=0); after=once(d,path=1)
        self.assertEqual(before['stats']['trades'],0)
        self.assertEqual(after['stats']['trades'],1)
        self.assertEqual(list(after['fills'][:,2]),[0,3])
        self.assertEqual(after['trades'][0,11],0)

    def test_touch_does_not_fill_and_one_tick_penetration_does(self):
        d=source(3);d['l'][1]=99.99;d['l'][2]=99.98
        r=once(d)
        self.assertEqual(len(r['fills']),1)
        self.assertEqual(r['fills'][0,1],d['ts'][2]+60000)

    def test_scale_in_average_partial_size_and_remaining_tp(self):
        d=source(20);d['l'][1]=99.98
        for k in ['o','h','l','c']:d[k][17:]=99.85
        d['o'][17]=100.;d['h'][17]=100.;d['l'][17]=99.8
        d['h'][18]=100.5;d['c'][18]=100.4
        r=once(d)
        self.assertEqual(list(r['fills'][:,2]),[0,1,2,3])
        first,add,be,tp=r['fills']
        weighted=(first[3]*first[4]+add[3]*add[4])/(first[3]+add[3])
        self.assertAlmostEqual(add[5],weighted)
        self.assertEqual(be[3],add[3])
        self.assertAlmostEqual(tp[3],first[3])
        self.assertAlmostEqual(be[4],np.ceil(weighted/.01-1e-9)*.01)
        self.assertAlmostEqual(tp[4],np.ceil(weighted*1.004/.01-1e-9)*.01)
        self.assertEqual(r['stats']['add_entries'],1)
        self.assertGreaterEqual(add[1]-first[1],15*60000)
        self.assertEqual(r['stats']['trades'],1)

    def test_no_add_before_shared_gap(self):
        d=source(12);d['l'][1:]=99.5;d['h'][1:]=100.
        r=once(d)
        self.assertEqual(r['stats']['add_entries'],0)

    def test_cannot_add_again_after_breakeven_reduction(self):
        d=source(50);d['l'][1]=99.98
        d['l'][17]=99.8;d['c'][17]=99.85
        d['h'][18]=100.05
        d['l'][19:]=99.5;d['h'][19:]=100.05
        r=once(d)
        self.assertEqual(r['stats']['add_entries'],1)
        self.assertEqual(r['stats']['breakeven_exits'],1)

    def test_timeout_clock_stays_at_first_entry(self):
        d=source(30);d['l'][1]=99.98
        for k in ['o','h','l','c']:d[k][17:]=99.85
        d['o'][17]=100.;d['h'][17]=100.;d['l'][17]=99.8
        d['h'][22:]=99.9
        r=once(d,hours=20/60)
        self.assertEqual(r['stats']['add_entries'],1)
        self.assertEqual(r['stats']['time_exits'],1)
        self.assertEqual(r['trades'][0,11],21)
        self.assertEqual(r['trades'][0,12],1)

    def test_current_date_reprices_existing_tp(self):
        d=source(4);d['l'][1]=99.98
        for k in ['o','h','l','c']:d[k][2:]=100.25
        d['tp'][3:]=.002;d['h'][3]=100.3
        r=once(d)
        self.assertEqual(r['stats']['trades'],1)
        self.assertAlmostEqual(r['fills'][-1,4],100.26)

    def test_posted_run_id_consumed_on_actual_fill(self):
        d=source(40);d['l'][1:]=99.98;d['h'][1:]=101.
        r=once(d,path=1)
        self.assertEqual(r['stats']['entries'],1)

    def test_quantity_cap_and_ledger_for_both_paths_and_fees(self):
        n=4000;d=source(n,2000.);v=2000+80*np.sin(np.arange(n)*.075)
        d['o']=np.r_[v[0],v[:-1]];d['c']=v
        d['h']=np.maximum(d['o'],v)+3;d['l']=np.minimum(d['o'],v)-3
        for path in [0,1]:
            for mode in ['single','scale_in']:
                r=run_case(d,{'mode':mode,'max_eth':8,'maker_fee_rate':.0002},2,7,add_drop=.001,path=path,detail=True)
                s=r['stats'];fills=r['fills'];p=r['positions']
                self.assertLessEqual(s['max_direction_eth'],8+1e-10)
                self.assertGreaterEqual(s['min_entry_gap_minutes'],15)
                self.assertAlmostEqual(s['final_cash'],20000+(fills[:,6]-fills[:,7]).sum(),places=7)
                self.assertAlmostEqual(s['fees'],fills[:,7].sum(),places=7)
                floating=p[0,1]*(v[-1]-p[0,2])+p[1,1]*(p[1,2]-v[-1])
                self.assertAlmostEqual(s['ending_equity'],s['final_cash']+floating,places=7)
                self.assertEqual(s['entries']-s['add_entries']-s['trades'],s['open_positions'])
                if mode=='single':self.assertEqual(s['add_entries'],0)

    def test_invalid_config(self):
        for config in [{'add_drops':[0]},{'timeout_hours':[-1]},{'entry_gap_minutes':0},
                       {'initial_equity':float('nan')},{'maker_fee_rate':-.1},{'paths':[3]}]:
            with self.assertRaises(ValueError):normalize_config(config)


if __name__=='__main__':unittest.main(verbosity=2)
