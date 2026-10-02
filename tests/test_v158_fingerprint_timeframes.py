import copy
import hashlib
import json
import unittest

import fingerprint_lookup as lookup
from extended_rules import stable_base_id
from ranking_view import CODE_NAMES, config_fingerprint
from selection_config import 配置统计
from strategy_space import 生成止损组合
import test_v149_fingerprint_lookup as fixture


class FingerprintTimeframeTests(unittest.TestCase):
    setUp = fixture.FingerprintLookupTests.setUp
    make_result = fixture.FingerprintLookupTests.make_result
    write_json = fixture.FingerprintLookupTests.write_json

    def high_timeframe_result(self):
        directory, _, selected = self.make_result()
        selected['开仓条件']['1m'] = [0]
        selected['开仓条件']['5m'] = [263]
        trade = copy.deepcopy(selected)
        trade.pop('候选筛选', None)
        signature = hashlib.sha256(json.dumps(trade, ensure_ascii=False, sort_keys=True,
                                              separators=(',', ':')).encode()).hexdigest()
        for name in ('组合选择.json', '断点记录.json', '回测运行身份.json'):
            path = directory / name
            value = json.loads(path.read_text(encoding='utf-8'))
            if name == '组合选择.json':
                value = selected
            elif name == '断点记录.json':
                value['selection'] = selected
                value['selection_signature'] = signature
                value['run_identity']['selection_signature'] = signature
            else:
                value['selection_signature'] = signature
            self.write_json(path, value)
        stop_index = next(i for i, (code, _, _) in enumerate(生成止损组合()) if code == 'S1')
        for name in lookup.RANKING_FILES:
            path = directory / name
            value = json.loads(path.read_text(encoding='utf-8'))
            row = dict(zip(value['表头'], value['分类']['分批止盈'][0]))
            row.update({'1分钟条件': CODE_NAMES[0], '5分钟条件': CODE_NAMES[263],
                        '基础策略编号': stable_base_id(1, (0, 0, 0, 263, 0), stop_index)})
            value['分类']['分批止盈'][0] = [row[key] for key in value['表头']]
            self.write_json(path, value)
        return directory, config_fingerprint(row)

    def test_original_high_timeframe_fingerprint_requires_native_capabilities(self):
        directory, fingerprint = self.high_timeframe_result()
        match = lookup.find_fingerprint_matches(fingerprint, directory)[0]
        with self.assertRaisesRegex(ValueError, '原数据能力不足'):
            lookup.restore_fingerprint_match(match)
        path = directory / '回测数据说明.json'
        meta = json.loads(path.read_text(encoding='utf-8'))
        meta['capabilities_by_timeframe'] = {'5m': {'ohlcv': True, 'delta_cvd': True}}
        self.write_json(path, meta)
        restored = lookup.restore_fingerprint_match(match)
        self.assertEqual(restored['selection']['开仓条件']['5m'], [263])
        self.assertEqual(restored['selection']['开仓条件']['1m'], [0])
        self.assertEqual(配置统计(restored['selection'])['包含仓位完整组合数'], 1)
        self.assertEqual(restored['capabilities_by_timeframe'], meta['capabilities_by_timeframe'])

    def test_non_boolean_native_capability_is_not_accepted_as_available(self):
        directory, fingerprint = self.high_timeframe_result()
        path = directory / '回测数据说明.json'
        meta = json.loads(path.read_text(encoding='utf-8'))
        meta['capabilities_by_timeframe'] = {'5m': {'ohlcv': True, 'delta_cvd': 'false'}}
        self.write_json(path, meta)
        match = lookup.find_fingerprint_matches(fingerprint, directory)[0]
        with self.assertRaisesRegex(ValueError, '各周期能力记录不完整'):
            lookup.restore_fingerprint_match(match)

    def test_merge_cannot_borrow_native_capability_from_a_different_original(self):
        directory, fingerprint, _ = self.make_result()
        match = lookup.find_fingerprint_matches(fingerprint, directory)[0]
        first = lookup.restore_fingerprint_match(match)
        other = copy.deepcopy(first)
        other['capabilities_by_timeframe'] = {'5m': {'ohlcv': True}}
        with self.assertRaisesRegex(ValueError, '各周期数据能力不同'):
            lookup.merge_fingerprint_restorations([first, other])


if __name__ == '__main__':
    unittest.main()
