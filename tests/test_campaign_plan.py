import json
import unittest
from copy import deepcopy

import campaign_plan as P
from fifth_policy import fifth_retirement_reason


SETTINGS = {"start": "2026-06-01", "end": "2026-09-01",
            "validation_start": "2026-09-01", "validation_end": "2026-09-21",
            "max_jobs": 2000,
            "seed_selection": {"手续费": {"开仓费率": .0002, "平仓费率": .0002,
                                        "BNB抵扣": False, "返佣比例": 0},
                               "资金约束": {"初始资金USDC": 20000, "最大开仓数量ETH": 100}}}


def matched_jobs(return_a=.12, return_b=.08, trades=30):
    cfg = P._base(SETTINGS)
    cfg["开仓指标"] = ["hist"]
    cfg["开仓条件"]["1m"] = [3]
    cfg["止盈方案编号"] = [5]
    jobs = P._pair(SETTINGS, "coarse", cfg, {"family": "original:3"})
    for job, ret in zip(jobs, (return_a, return_b)):
        job["state"] = "completed"
        job["results"] = []
        for cost in ("FEE", "SLIPPAGE"):
            row = {column: str(job["selection"]["开仓条件"][tf][0])
                   for tf, column in P.TF_COLUMNS.items()}
            row.update({"开仓MACD代码": "0", "止盈方案编号": "5", "止盈后等待分钟": "0",
                        "强制时间止损（分钟）": "180", "止损代码": "OFF", "固定止损代码": "OFF",
                        "叠加止盈代码": "OFF", "开仓方向": "BOTH", "交易会话": "ALL",
                        "开仓位置过滤代码": "OFF", "入场触发口径": "MACD_CYCLE",
                        "成交价格口径": "CLOSE_CONFIRMED", "成本模式": cost,
                        "所选仓位顺序": "10/10", "所选仓位累计收益率（%）": str(ret),
                        "所选仓位最大回撤（%）": ".06", "所选仓位全仓强平次数（次）": "0",
                        "所选仓位资金性停机标记（0否1是）": "0", "交易次数（单）": str(trades)})
            job["results"].append(row)
    return jobs


class CampaignPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.initial = P.initial_jobs(SETTINGS)

    def test_catalog_json_and_retained_fifth(self):
        catalog = P.make_catalog()
        json.dumps(catalog, ensure_ascii=False)
        codes = {x["code"] for x in catalog["entries"]}
        self.assertEqual(len(codes & set(P.FIFTH_SPECS)), 39)
        self.assertTrue({361, 365} <= codes)  # F5-098, F5-102.
        self.assertFalse(any(fifth_retirement_reason(c) for c in codes))
        catalog["entries"].clear()
        self.assertTrue(P.make_catalog()["entries"])

    def test_coarse_has_every_family_and_retained_fifth(self):
        self.assertEqual({j["tags"]["family"] for j in self.initial}, set(P._groups()))
        codes = {c for j in self.initial for vv in j["selection"]["开仓条件"].values() for c in vv}
        self.assertEqual(len(codes & set(P.FIFTH_SPECS)), 39)
        self.assertTrue({361, 365} <= codes)
        selected = {row["编号"]: row for row in P.make_catalog()["take_profit"]}
        families = {selected[code]["类别"] for code in self.initial[0]["selection"]["止盈方案编号"]}
        self.assertEqual(len(families), 6)
        self.assertEqual(set(self.initial[0]["selection"]["止损代码"]), {"OFF", "OFF+S9_1m", "ATR150_5m"})

    def test_bounded_rows_and_stable_ids(self):
        self.assertEqual(len({j["id"] for j in self.initial}), len(self.initial))
        self.assertLessEqual(max(j["tags"]["result_count"] for j in self.initial), 512)
        original = self.initial[0]
        rebuilt = P._job(SETTINGS, original["phase"], original["selection"],
                         (original["tags"]["window"], original["start"], original["end"]),
                         {k: v for k, v in original["tags"].items() if k not in ("window", "result_count")})
        self.assertEqual(original["id"], rebuilt["id"])

    def test_nonzero_explicit_costs_and_account(self):
        cfg = self.initial[0]["selection"]
        self.assertEqual(cfg["手续费"]["开仓费率"], .0002)
        self.assertEqual(cfg["资金约束"]["初始资金USDC"], 20000)
        self.assertEqual(cfg["资金约束"]["最大开仓数量ETH"], 100)
        self.assertEqual(set(P.cost_modes(cfg)), {"FEE", "SLIPPAGE"})
        self.assertEqual(cfg["仓位倍数"], [1.0])
        zero = deepcopy(SETTINGS)
        zero["seed_selection"]["手续费"]["开仓费率"] = 0
        with self.assertRaises(ValueError):
            P._base(zero)

    def test_chronological_windows_have_gap(self):
        a, b, validation = P._windows(SETTINGS)
        self.assertLess(P._date(a[2]), P._date(b[1]))
        self.assertLess(P._date(b[2]), P._date(validation[1]))
        bad = {**SETTINGS, "validation_start": "2026-08-01"}
        with self.assertRaises(ValueError):
            P._windows(bad)

    def test_budget_cannot_silently_cut_initial_families(self):
        with self.assertRaises(ValueError):
            P.initial_jobs({**SETTINGS, "max_jobs": 2})

    def test_normalization_must_not_silently_delete_planned_options(self):
        cfg = P._base(SETTINGS)
        cfg["开仓条件"]["1m"] = [3]
        cfg["止损代码"] = ["OFF", "S9_1m"]  # Actual standalone code is OFF+S9_1m.
        with self.assertRaises(ValueError):
            P._job(SETTINGS, "coarse", cfg, P._windows(SETTINGS)[0], {})

    def test_pending_phase_does_not_advance(self):
        self.assertEqual(P.advance_jobs(SETTINGS, [{**self.initial[0], "state": "queued"}]), [])

    def test_rank_requires_both_windows_and_both_costs(self):
        jobs = matched_jobs()
        result = P.ranked_candidates(SETTINGS, jobs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["selection"]["仓位倍数"], [1.0])
        self.assertEqual(P.ranked_candidates(SETTINGS, jobs[:1]), [])
        jobs[0]["results"].pop()
        self.assertEqual(P.ranked_candidates(SETTINGS, jobs), [])

    def test_loss_no_trades_or_invalid_never_promote(self):
        for args in ({"return_b": -.01}, {"trades": 19}, {"return_b": float("nan")}):
            self.assertEqual(P.ranked_candidates(SETTINGS, matched_jobs(**args)), [])

    def test_real_per_leverage_count_overrides_shared_count(self):
        jobs = matched_jobs(trades=500)
        for job in jobs:
            for row in job["results"]:
                row["所选仓位实际交易统计"] = "0|0|0|0|0|0|0|0|0|0|0|0"
        self.assertEqual(P.ranked_candidates(SETTINGS, jobs), [])

    def test_placebo_cannot_promote(self):
        jobs = matched_jobs()
        for job in jobs:
            for row in job["results"]:
                row["1分钟条件代码"] = "61"
        self.assertEqual(P.ranked_candidates(SETTINGS, jobs), [])

    def test_validation_results_never_select_or_advance(self):
        jobs = matched_jobs()
        for job in jobs:
            job["phase"] = "validation"
            job["tags"]["window"] = "validation"
        self.assertEqual(P.ranked_candidates(SETTINGS, jobs), [])
        self.assertEqual(P.advance_jobs(SETTINGS, jobs), [])

    def test_theoretical_touch_is_only_a_sensitivity_control(self):
        jobs = matched_jobs(return_a=10, return_b=10)
        for job in jobs:
            for row in job["results"]:
                row["成交价格口径"] = "THEORETICAL"
        self.assertEqual(P.ranked_candidates(SETTINGS, jobs), [])

    def test_losers_get_alternate_representative(self):
        jobs = P._bucket(SETTINGS, [])
        expected = {family for family, items in P._groups().items() if not all(x["placebo"] for x in items)}
        self.assertEqual({j["tags"]["family"] for j in jobs}, expected)
        self.assertTrue(all(j["tags"]["role"] == "rescue_representative" for j in jobs))

    def test_refinement_covers_all_tp_stop_and_sizes_one_factor(self):
        jobs = P._refine(SETTINGS, matched_jobs())
        catalog = P.make_catalog()
        tps = {x for j in jobs for x in j["selection"]["止盈方案编号"]}
        stops = {x for j in jobs for x in j["selection"]["止损代码"]}
        sizes = {x for j in jobs for x in j["selection"]["仓位倍数"]}
        self.assertEqual(tps, {x["编号"] for x in catalog["take_profit"]})
        self.assertEqual(stops, {x["code"] for x in catalog["stops"]})
        self.assertEqual(sizes, set(catalog["sizes"]))
        self.assertLessEqual(max(j["tags"]["result_count"] for j in jobs), 512)
        self.assertEqual(jobs[0]["tags"]["axis"], "开仓位置过滤")
        long_axes = [j["tags"]["axis"] for j in jobs if j["tags"]["axis"] in ("止盈方案编号", "止损代码")]
        self.assertEqual(long_axes[:4], ["止盈方案编号", "止盈方案编号", "止损代码", "止损代码"])

    def test_deferred_budget_is_terminal_not_completed(self):
        self.assertIn("deferred_budget", P.TERMINAL)
        job = {**self.initial[0], "state": "deferred_budget"}
        report = P.coverage_report(SETTINGS, [job])
        self.assertEqual(report["budget_deferred_jobs"], 1)
        self.assertEqual(report["coverage"]["entry"]["completed_option_count"], 0)

    def test_analysis_contains_actual_training_and_validation(self):
        train = matched_jobs()
        frozen = P.validation_jobs(SETTINGS, train)
        self.assertEqual(len(frozen), 1)
        frozen[0]["state"] = "completed"
        frozen[0]["results"] = deepcopy(train[0]["results"])
        report = P.analysis_report(SETTINGS, train + frozen)
        self.assertEqual(len(report["training_candidates"][0]["training_observations"]), 4)
        self.assertTrue(report["validation_candidates"][0]["meets_prespecified_time_check"])
        self.assertEqual(P.validation_jobs(SETTINGS, train + frozen), [])
        frozen[0]["results"][0]["所选仓位累计收益率（%）"] = "-.01"
        report = P.analysis_report(SETTINGS, train + frozen)
        self.assertFalse(report["validation_candidates"][0]["meets_prespecified_time_check"])
        json.dumps(report, ensure_ascii=False)

    def test_coverage_distinguishes_failed_and_done(self):
        job = deepcopy(self.initial[0])
        job["state"] = "failed"
        report = P.coverage_report(SETTINGS, [job])
        self.assertEqual(report["coverage"]["entry"]["completed_option_count"], 0)
        self.assertTrue(report["coverage"]["entry"]["assigned_not_completed"])
        job["state"] = "completed"
        report = P.coverage_report(SETTINGS, [job])
        self.assertGreater(report["coverage"]["entry"]["completed_option_count"], 0)
        self.assertFalse(report["complete_cartesian_product"])


if __name__ == "__main__":
    unittest.main()
