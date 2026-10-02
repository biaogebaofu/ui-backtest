import unittest
import numpy as np
from fourth_batch import FOURTH_SPECS,FOURTH_ROUND_CODES,KLINE_ONLY_FOURTH_CODES,MICROSTRUCTURE_FOURTH_CODES
from extended_rules import ENTRY_RULES,ENTRY_RULE_BATCH,stable_base_id,unavailable_entry_codes
from extended_signals import entry_masks

class FourthBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng=np.random.default_rng(20260909);n=4600
        cls.c=2000*np.exp(np.cumsum(rng.normal(0,.0007,n)));cls.o=np.r_[cls.c[0],cls.c[:-1]]
        cls.h=np.maximum(cls.c,cls.o)+rng.uniform(.1,2,n);cls.l=np.minimum(cls.c,cls.o)-rng.uniform(.1,2,n)
        cls.v=rng.lognormal(6,1,n);cls.times=1735689600000+np.arange(1,n+1)*60000
        cls.values=np.sin(np.arange(n)/5)+rng.normal(0,.2,n);cls.d=np.sign(cls.values-np.r_[cls.values[0],cls.values[:-1]])
        cls.ex={'delta_base':cls.v*rng.uniform(-1,1,n),'trades':rng.integers(1,10000,n).astype(float)}
    def masks(self,c,n=None):
        n=n or len(self.c)
        return entry_masks(c,self.d[:n],self.values[:n],self.o[:n],self.h[:n],self.l[:n],self.c[:n],self.c[:n],self.v[:n],self.times[:n],{k:v[:n] for k,v in self.ex.items()})
    def test_catalog_120_and_requirements(self):
        self.assertEqual(len(FOURTH_SPECS),120);self.assertEqual(len(KLINE_ONLY_FOURTH_CODES),108);self.assertEqual(len(MICROSTRUCTURE_FOURTH_CODES),12)
        self.assertEqual(set(FOURTH_SPECS),set(range(144,264)))
        self.assertTrue(all(ENTRY_RULE_BATCH[c]==4 for c in FOURTH_SPECS))
        self.assertEqual(set(unavailable_entry_codes(FOURTH_ROUND_CODES,{'ohlcv':True})),set(MICROSTRUCTURE_FOURTH_CODES))
    def test_prefix_invariance_every_rule(self):
        for c in FOURTH_ROUND_CODES:
            full=self.masks(c)
            for end in [321,1499,2973]:
                shorter=self.masks(c,end)
                with self.subTest(code=c,end=end):
                    for a,b in zip(full,shorter):np.testing.assert_array_equal(a[:end],b)
    def test_boolean_finite_shape_and_direction(self):
        for c in FOURTH_ROUND_CODES:
            a,b=self.masks(c)
            with self.subTest(code=c):
                self.assertEqual(a.dtype,np.dtype('bool'));self.assertEqual(a.shape,self.c.shape)
                self.assertFalse(np.any(a&b));self.assertTrue(np.all(self.d[a]==1));self.assertTrue(np.all(self.d[b]==-1))
    def test_namespace_no_collision(self):
        ids=[stable_base_id(f,(0,0,0,0,c),s) for f in [0,1] for c in range(264) for s in [0,10,6665]]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(stable_base_id(0,(0,0,0,0,143),0),80000000001171457)
    def test_config_roundtrip_and_high_tf_support(self):
        from selection_config import 全选配置,规范化配置
        conf=全选配置();conf['开仓条件']['1m']=list(FOURTH_ROUND_CODES)
        self.assertEqual(规范化配置(conf)['开仓条件']['1m'],list(FOURTH_ROUND_CODES))
        conf['开仓条件']['4h']=[263]
        self.assertEqual(规范化配置(conf)['开仓条件']['4h'],[263])
        conf['开仓条件']['4h']=[240]
        with self.assertRaises(ValueError):规范化配置(conf)
    def test_ui_fourth_scope_and_capabilities(self):
        from unittest import mock
        from ui import App
        with mock.patch.object(App,'save_user_settings'):
            app=App()
            try:
                app.withdraw();p=app.selection_panel
                prior={c:v.get() for c,v in p.case_vars['1m'].items() if c<144}
                p._set_fourth_source('1m','kline')
                self.assertTrue(all(p.case_vars['1m'][c].get() for c in KLINE_ONLY_FOURTH_CODES))
                self.assertEqual(prior,{c:v.get() for c,v in p.case_vars['1m'].items() if c<144})
                p.set_data_capabilities({'ohlcv':True})
                p.fourth_entry_all_vars['1m'].set(True);p._set_fourth_round('1m')
                self.assertTrue(all(not p.case_vars['1m'][c].get() for c in MICROSTRUCTURE_FOURTH_CODES))
                self.assertTrue(all(p.case_widgets['1m'][c].instate(['disabled']) for c in MICROSTRUCTURE_FOURTH_CODES))
            finally:
                for t in app.tk.call('after','info'):app.after_cancel(t)
                app.destroy()
