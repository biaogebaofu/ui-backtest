"""Cost alternatives restore to scalar rows and merge only compatible inputs."""
import copy
import json
import unittest

import fingerprint_lookup as F
from execution_settings import effective_fee_rates, effective_slippage
from ranking_view import config_fingerprint
from selection_config import 配置统计
import test_v153_lookup_snapshot as fixture


class CostFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.LookupSnapshotTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def paired_result(self):
        directory, _, _ = self.fixture.fixture.make_result()
        self.fixture.replace_original_config(directory, lambda raw: raw.update(成本模式=["SLIPPAGE", "FEE"]))
        path = directory / F.RANKING_FILES[0]
        payload = json.loads(path.read_text("utf-8"))
        headers = payload["表头"]
        fee_row = dict(zip(headers, payload["分类"]["分批止盈"][0]))
        slip_row = dict(fee_row, **{"成本模式": "SLIPPAGE", "开仓成交偏移（%）": .0003,
                                   "平仓成交偏移（%）": .0004, "开仓净手续费率（%）": 0.,
                                   "平仓净手续费率（%）": 0.})
        payload["分类"]["分批止盈"].append([slip_row[key] for key in headers])
        for filename in F.RANKING_FILES:
            self.fixture.fixture.write_json(directory / filename, payload)
        return directory, [config_fingerprint(row) for row in (fee_row, slip_row)]

    def test_multi_cost_run_restores_each_effective_scalar_cost(self):
        directory, fingerprints = self.paired_result()
        self.assertNotEqual(*fingerprints)
        restored = [F.restore_fingerprint_match(F.find_fingerprint_matches(fp, directory)[0])
                    for fp in fingerprints]
        self.assertEqual([item["selection"]["成本模式"] for item in restored], ["FEE", "SLIPPAGE"])
        for item in restored:
            self.assertEqual(配置统计(item["selection"])["包含仓位完整组合数"], 1)
        fee, slip = (item["selection"] for item in restored)
        self.assertEqual(effective_slippage(fee), (0., 0.))
        self.assertEqual(effective_fee_rates(slip), (0., 0.))
        self.assertEqual(effective_slippage(slip), (.0003, .0004))
        self.assertGreater(effective_fee_rates(fee)[0], 0)
        merged = F.merge_fingerprint_restorations(restored)
        self.assertEqual(merged["selection"]["成本模式"], ["SLIPPAGE", "FEE"])
        self.assertEqual((merged["unique_count"], merged["projected_count"], merged["extra_count"]), (2, 2, 0))

    def test_unselected_mode_is_rejected_even_with_valid_row_fingerprint(self):
        directory, fingerprints = self.paired_result()
        self.fixture.replace_original_config(directory, lambda raw: raw.update(成本模式="FEE"))
        with self.assertRaisesRegex(ValueError, "成本模式不属于"):
            F.restore_fingerprint_match(F.find_fingerprint_matches(fingerprints[1], directory)[0])

    def test_merge_still_rejects_different_inactive_raw_fee_or_offset_inputs(self):
        directory, fingerprints = self.paired_result()
        restored = [F.restore_fingerprint_match(F.find_fingerprint_matches(fp, directory)[0])
                    for fp in fingerprints]
        for key, child in (("手续费", "开仓费率"), ("成交偏移", "平仓")):
            changed = copy.deepcopy(restored)
            changed[1]["selection"][key][child] += .0001
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                F.merge_fingerprint_restorations(changed)


if __name__ == "__main__":
    unittest.main()
