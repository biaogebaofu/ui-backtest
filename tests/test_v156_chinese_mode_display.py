"""Human-facing entry/cost labels cannot change machine identity or settings."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from ranking_view import build_view, config_fingerprint, CODE_NAMES
from strategy_description import describe_row, load_context
from strategy_display import DISPLAY_LABELS, candidate_display_payload, mode_label
import test_v155_complete_export as fixture


class ChineseModeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixture.CompleteExportTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def test_main_three_entries_two_costs_keep_exact_fingerprint_and_config(self):
        original = copy.deepcopy(self.helper.payload)
        records = [dict(row, 入场触发口径=mode) for mode in DISPLAY_LABELS['entry'] for row in self.helper.rows]
        payload = copy.deepcopy(original)
        payload['分类']['移动止盈'] = [[row[h] for h in payload['表头']] for row in records]
        before = copy.deepcopy(payload)
        context = load_context(payload,None,CODE_NAMES)
        view = build_view(payload)
        headers = view['表头']
        self.assertEqual(headers[headers.index('MACD口径')+1], '入场次数规则')
        self.assertLess(headers.index('成本模式'), headers.index('MACD口径'))
        self.assertEqual(len(headers),len(set(headers)))
        self.assertGreater(headers.index('成本模式代码'),headers.index('期末资金（USDC）'))
        for raw, values in zip(records,view['分类']['移动止盈']):
            row = dict(zip(headers,values))
            self.assertEqual(row['入场次数规则'],DISPLAY_LABELS['entry'][raw['入场触发口径']])
            self.assertEqual(row['成本模式'],DISPLAY_LABELS['cost'][raw['成本模式']])
            self.assertEqual(row['成本模式代码'],raw['成本模式'])
            self.assertEqual(row['入场触发口径'],raw['入场触发口径'])
            self.assertEqual(row['策略指纹'],config_fingerprint(raw))
            self.assertEqual(row['完整单策略配置JSON'],describe_row(raw,context)['完整单策略配置JSON'])
            for key in ('期末资金（USDC）','名义倍数（倍）','胜率（%）','最大回撤（%）'):
                self.assertEqual(row[key],raw[key])
        self.assertEqual(payload,before)

    def test_single_constant_mode_stays_in_front_and_unknown_is_not_defaulted(self):
        for mode in DISPLAY_LABELS['entry']:
            payload = copy.deepcopy(self.helper.payload)
            raw = dict(self.helper.rows[0], 入场触发口径=mode)
            payload['分类']['移动止盈'] = [[raw[h] for h in payload['表头']]]
            view = build_view(payload)
            row = dict(zip(view['表头'],view['分类']['移动止盈'][0]))
            self.assertEqual(row['入场次数规则'],DISPLAY_LABELS['entry'][mode])
            self.assertEqual(row['成本模式'],'仅成交偏移')
        self.assertEqual(mode_label(None,'entry'),'未记录（查原结果）')
        self.assertIn('未知模式',mode_label('not-a-mode','cost'))

    def candidate(self):
        # Candidate reading payloads already protect long IDs before JSON.parse.
        rows = [dict(row, 基础策略编号=str(row['基础策略编号']), 策略指纹=config_fingerprint(row), 完整单策略配置JSON='{"selection":{"成本模式":"'+row['成本模式']+'"}}') for row in self.helper.rows]
        return {'表头':list(rows[0]),'研究候选':rows,'观察候选':rows[:1],'实盘候选':[],'设置':{}}

    def test_candidate_projection_is_display_only_and_keeps_raw_codes_on_right(self):
        payload = self.candidate(); before = copy.deepcopy(payload)
        view = candidate_display_payload(payload)
        position = view['表头'].index('策略指纹')
        self.assertEqual(view['表头'][position+1:position+3],['入场次数规则','成本模式'])
        self.assertEqual(view['表头'][-1],'成本模式代码')
        self.assertEqual(len(view['表头']),len(set(view['表头'])))
        for key in ('研究候选','观察候选'):
            for raw,row in zip(payload[key],view[key]):
                self.assertEqual(row['成本模式'],DISPLAY_LABELS['cost'][raw['成本模式']])
                self.assertEqual(row['成本模式代码'],raw['成本模式'])
                for name,value in raw.items():
                    if name != '成本模式':self.assertEqual(row[name],value)
        self.assertEqual(payload,before)

    @unittest.skipUnless(shutil.which('node'),'Node is optional on portable installs')
    def test_javascript_and_python_candidate_projections_are_identical(self):
        payload = self.candidate()
        module = (Path(__file__).resolve().parents[1]/'strategy_display.mjs').as_uri()
        script = f'import {{candidateDisplayPayload}} from {json.dumps(module)}; let s=""; for await(const chunk of process.stdin)s+=chunk; const p=JSON.parse(s); const display=candidateDisplayPayload(p); process.stdout.write(JSON.stringify({{display, original:p}}));'
        result = subprocess.run([shutil.which('node'),'--input-type=module','-e',script],input=json.dumps(payload,ensure_ascii=False),text=True,encoding='utf-8',capture_output=True,check=True)
        actual = json.loads(result.stdout)
        self.assertEqual(actual['display'],candidate_display_payload(payload))
        self.assertEqual(actual['original'],payload)


if __name__ == '__main__':
    unittest.main()
