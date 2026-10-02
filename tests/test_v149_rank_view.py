import unittest

from ranking_view import build_view, config_fingerprint
import test_v147_rank_view as fixture


class VisibleMetricTests(unittest.TestCase):
    def test_single_leverage_remains_a_visible_result_column(self):
        payload = fixture.ExecutionRankViewTests().payload()
        view = build_view(payload)
        self.assertIn('名义倍数（倍）', view['表头'])
        self.assertNotIn('名义倍数（倍）', dict(view['共同设置']))
        index = view['表头'].index('名义倍数（倍）')
        for rows in view['分类'].values():
            self.assertTrue(all(row[index] == 2 for row in rows))

    def test_presentation_change_keeps_existing_fingerprint_and_results(self):
        payload = fixture.ExecutionRankViewTests().payload()
        native = dict(zip(payload['表头'],payload['分类']['移动止盈'][0]))
        view = build_view(payload)
        row = dict(zip(view['表头'],view['分类']['移动止盈'][0]))
        self.assertEqual(row['策略指纹'], config_fingerprint(native))
        for name in ('胜率（%）','期末资金（USDC）','名义倍数（倍）'):
            self.assertEqual(row[name], native[name])


if __name__ == '__main__':
    unittest.main()
