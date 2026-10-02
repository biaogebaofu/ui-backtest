"""Real worker: fifth events and exact custom rates survive result export/restore."""
import json
import unittest
from decimal import Decimal
import test_availability_worker as fixture
from selection_config import 规范化配置, 配置签名, 配置统计
from strategy_space import 固定比例代码
from ranking_view import build_view, config_fingerprint
from fingerprint_lookup import find_fingerprint_matches, restore_fingerprint_match


class FifthWorkerTests(fixture.AvailabilityWorkerTests):
    def test_fifth_custom_exits_result_and_exact_restore(self):
        self.selection['开仓条件']={tf:[0] for tf in ('4h','1h','15m','5m','1m')}
        self.selection['开仓条件']['5m']=[288]
        self.selection['入场触发口径']='LIVE_01'
        self.selection['止盈方案编号']=[8281]
        self.selection['固定止损代码']=[固定比例代码('FSL',Decimal('.00123456'),Decimal('.0125'))]
        self.selection['叠加止盈代码']=[固定比例代码('FTP',Decimal('.006789'),Decimal('.009876'))]
        self.selection['候选筛选']['导出旧排行']=True
        self.selection['入场约束']['最小S3距离']=0
        self.selection_path.write_text(json.dumps(self.selection,ensure_ascii=False),encoding='utf-8')
        events=self.run_worker()
        self.assertTrue(any(e.get('type')=='completed' for e in events))
        payload=json.loads((self.output/'各类止盈前5000名.json').read_text(encoding='utf-8'))
        view=build_view(payload,self.output)
        row=dict(zip(view['表头'],next(iter(view['分类'].values()))[0]))
        envelope=json.loads(row['完整单策略配置JSON'])
        chosen=envelope['selection']
        self.assertEqual(chosen['入场触发口径'],'F5_EVENT')
        self.assertEqual(chosen['开仓条件']['5m'],[288])
        self.assertEqual(chosen['固定止损代码'],self.selection['固定止损代码'])
        self.assertEqual(chosen['叠加止盈代码'],self.selection['叠加止盈代码'])
        self.assertEqual(配置统计(chosen)['包含仓位完整组合数'],1)
        self.assertIn('独立方向',row['开仓判据（当前代码解释）'])
        matches=find_fingerprint_matches(row['策略指纹'],self.output)
        self.assertTrue(matches)
        restored=restore_fingerprint_match(matches[0])
        self.assertEqual(restored['selection'],规范化配置(chosen))


if __name__=='__main__':unittest.main()
