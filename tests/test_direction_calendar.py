import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
import numpy as np
from direction_calendar import BEIJING, RULE_ID, direction, direction_array, month_index, event_window
from hedge_config import normalize_config
from hedge_engine import run_case
from hedge_panel import DEFAULT_FIELDS, fields_to_config
from hedge_worker import case_identity


def bars(n=10):
    start=datetime(2027,2,17,1,56,tzinfo=BEIJING)
    data={k:np.full(n,100.) for k in ('o','h','l','c')}
    data['ts']=int(start.timestamp()*1000)+np.arange(n,dtype=np.int64)*60000
    data['tp']=np.full(n,.1)
    return data


class CalendarTests(unittest.TestCase):
    def test_supplied_five_year_boundaries(self):
        boundaries=[('2026-09-15 00:00',-1),('2027-02-17 02:00',1),('2027-07-07 02:00',-1),
            ('2027-12-19 14:00',1),('2028-04-19 14:00',-1),('2028-11-07 02:00',1),
            ('2029-03-07 14:00',-1),('2029-09-07 02:00',1),('2030-01-05 14:00',-1),
            ('2030-07-07 02:00',1),('2030-10-19 14:00',-1),('2031-05-06 02:00',1),('2031-09-06 02:00',-1)]
        for index,(stamp,side) in enumerate(boundaries):
            t=datetime.fromisoformat(stamp).replace(tzinfo=BEIJING)
            self.assertEqual(direction(t),side)
            if index:self.assertEqual(direction(t-timedelta(milliseconds=1)),-side)
        self.assertEqual(direction(datetime(2031,9,15,tzinfo=BEIJING)),-1)
    def test_vector_scalar_and_historical_years_agree(self):
        dates=[datetime(y,m,15,tzinfo=BEIJING) for y in range(2020,2033) for m in range(1,13)]
        values=direction_array(np.array([int(t.timestamp()*1000) for t in dates]))
        self.assertEqual(values.tolist(),[direction(t) for t in dates])
    def test_original_month_index_quirk_and_naive_rejection(self):
        self.assertEqual(month_index(datetime(2026,12,8,tzinfo=BEIJING)),11)
        with self.assertRaises(ValueError):direction(datetime(2025,1,1))
    def test_switch_millisecond_included(self):
        t=int(datetime(2027,2,17,2,tzinfo=BEIJING).timestamp()*1000)
        self.assertEqual(direction_array(np.array([t-1,t,t+1])).tolist(),[-1,1,1])
    def test_enabled_config_changes_identity_off_preserves_legacy_shape(self):
        off=fields_to_config(DEFAULT_FIELDS)
        self.assertNotIn('direction_rule',off)
        self.assertEqual(off,normalize_config(dict(off,direction_rule='OFF')))
        on=fields_to_config(dict(DEFAULT_FIELDS,direction_limit=True))
        self.assertEqual(on['direction_rule'],RULE_ID)
        self.assertNotEqual(case_identity({'id':'RANDOM'},off,6,0,0),case_identity({'id':'RANDOM'},on,6,0,0))
        with self.assertRaises(ValueError):normalize_config({'direction_rule':'unknown'})


class DirectionEngineTests(unittest.TestCase):
    def run_model(self,data,**kwargs):
        return run_case(data,dict(mode='scale_in',direction_rule=RULE_ID,initial_equity=2000,
            entry_gap_minutes=1),24,0,detail=True,**kwargs)
    def test_old_short_maker_close_before_long_entry_both_paths(self):
        for path in (0,1):
            d=bars();d['h'][1]=100.03;d['l'][6:]=99.98
            result=self.run_model(d,path=path)
            fills=result['fills'];entries=fills[fills[:,2]==0]
            self.assertEqual(entries[:,0].tolist(),[1,0])
            exits=fills[fills[:,2]==5]
            self.assertEqual(len(exits),1)
            self.assertGreaterEqual(exits[0,1],d['ts'][6])
            self.assertGreater(entries[1,1],exits[0,1])
            self.assertEqual(result['stats']['direction_exits'],1)
            self.assertEqual(result['stats']['time_exits'],0)
            self.assertAlmostEqual(result['stats']['final_cash'],2000+sum(fills[:,6]-fills[:,7]))
    def test_pending_short_canceled_at_boundary(self):
        d=bars();d['h'][4:]=100.05
        signals=np.zeros((10,2),bool);signals[:,1]=True
        result=self.run_model(d,signals=signals)
        self.assertEqual(result['stats']['entries'],0)
    def test_switch_cancels_add_and_waits_if_exit_cannot_fill(self):
        d=bars();d['h'][1]=100.03;d['h'][4:]=102
        result=self.run_model(d,add_drop=.01)
        self.assertEqual(result['stats']['entries'],1)
        self.assertEqual(result['stats']['add_entries'],0)
        self.assertEqual(result['stats']['direction_exits'],0)
        self.assertEqual(result['positions'][1,1]>0,True)
        self.assertEqual(result['positions'][0,1],0)
    def test_allowed_long_add_still_works_and_flat_is_allowed(self):
        d=bars(30);d['ts']+=86400000;d['l'][1]=99.98;d['l'][4]=99.8
        result=self.run_model(d,add_drop=.001)
        self.assertEqual(result['stats']['add_entries'],1)
        self.assertTrue(all(row[0]==0 for row in result['fills']))
        result=self.run_model(d,signals=np.zeros((30,2),bool))
        self.assertEqual(result['stats']['entries'],0)
    def test_new_short_requires_old_long_close(self):
        d=bars();d['ts']+=int((datetime(2027,7,7,1,56,tzinfo=BEIJING)-datetime(2027,2,17,1,56,tzinfo=BEIJING)).total_seconds()*1000)
        d['l'][1]=99.98;d['h'][6:]=100.03
        result=self.run_model(d)
        entries=result['fills'][result['fills'][:,2]==0]
        self.assertEqual(entries[:,0].tolist(),[0,1])
        self.assertEqual(result['stats']['direction_exits'],1)


if __name__=='__main__':unittest.main()
