"""Hedge fingerprints project editable entries without changing pinned execution."""
from __future__ import annotations

import copy
import itertools
import unittest
from unittest.mock import patch

from fingerprint_lookup import bind_fingerprint_match, restore_many
from hedge_config import normalize_config
from hedge_editor_sync import merge_hedge_restorations
from hedge_fingerprints import core_hashes, core_names
from hedge_panel import DEFAULT_FIELDS, ENTRY_PLAN_LABELS, MODE_LABELS, PATH_LABELS, fields_to_config
from hedge_signals import TIMEFRAMES, plan_entries, strategy_id
from hedge_worker import case_identity
from indicator_combinations import DEFAULT_REGISTRY, choices_for_config
import test_v166_hedge_fingerprints as original_tests


ENTRY_KEYS = {"开仓条件", "开仓指标", "入场触发口径", "指标组合", "入场约束"}


class HedgeProjectionFixture:
    def setup_fixture(self):
        self.fixture = original_tests.FingerprintTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        matches = self.fixture.fixture(combined=True)
        self.template = restore_many(matches)[1]
        self.template["capabilities"] = {"ohlcv": True, "delta_cvd": True}
        self.template["capabilities_by_timeframe"] = {
            tf: dict(self.template["capabilities"]) for tf in TIMEFRAMES}
        self.pinned = bind_fingerprint_match(matches[1], self.template)

    def record(self, *, hour_rule=3, five_rule=57, mode="LIVE_01", hours=6., path=0,
               drop=.01, compare=True, random=False):
        value = copy.deepcopy(self.template)
        request = value["origin_identity"]["hedge_request"]
        request["entry_plan"] = "combined"
        config = normalize_config(dict(
            mode="scale_in", initial_equity=20000., max_eth=20., add_drops=[drop],
            timeout_hours=[hours], tp_weekday=.04, tp_holiday=.02, entry_gap_minutes=1,
            maker_fee_rate=0., seeds=10, seed_start=0, compare_entries=compare, paths=[path],
        ))
        cases = (0, hour_rule, 0, five_rule, 42)
        active = [(tf, code) for tf, code in zip(TIMEFRAMES, cases) if code]
        definition = dict(timeframe="+".join(tf for tf, _ in active),
                          code="+".join(f"{tf}:{code}" for tf, code in active), field="dif",
                          entry_mode=mode, cases=cases, s3_gap=.001, s3_timeframe="1m")
        descriptor = dict(definition, id=strategy_id(definition), label="portable combined entry")
        if random:
            descriptor = plan_entries({}, {}, False)[0][0]
            cases = (0, 0, 0, 0, 0)
        selection = copy.deepcopy(value["selection"])
        selection.update({"开仓指标": ["dif"], "开仓条件": dict(zip(TIMEFRAMES, ([c] for c in cases))),
                          "入场触发口径": mode, "入场约束": {"最小S3距离": .001, "S3基线周期": "1m"}})
        selection.pop("指标组合", None)
        request["selection"] = copy.deepcopy(selection)
        request["hedge"] = config
        request["indicator_registry"] = DEFAULT_REGISTRY.to_dict()
        fingerprint = case_identity(descriptor, config, hours, drop, path)
        request["hedge_exact"].update(fingerprint=fingerprint, descriptor=descriptor,
                                       hours=hours, add_drop=drop, path=path,
                                       core_files_sha256=core_hashes(core_names(request)))
        value.update(fingerprint=fingerprint, selection=selection)
        return value

    def fifteen(self):
        combinations = list(itertools.product((3, 4), (57, 154), ("LIVE_01", "MACD_CYCLE"), (6., 12.), (0, 1)))
        chosen = combinations[:14] + combinations[-1:]
        return [self.record(hour_rule=a, five_rule=b, mode=m, hours=h, path=p) for a, b, m, h, p in chosen]


class HedgeEditorProjectionTests(HedgeProjectionFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def test_fifteen_cases_restore_three_periods_and_preserve_original_parameters(self):
        records = self.fifteen()
        before = copy.deepcopy(records)
        got = merge_hedge_restorations(records)
        self.assertEqual(records, before)
        self.assertEqual(set(got["selection"]["开仓条件"]["1h"]), {3, 4})
        self.assertEqual(set(got["selection"]["开仓条件"]["5m"]), {57, 154})
        self.assertEqual(got["selection"]["开仓条件"]["1m"], [42])
        self.assertEqual(got["selection"]["开仓条件"]["4h"], [0])
        self.assertEqual(got["selection"]["开仓条件"]["15m"], [0])
        self.assertEqual(got["selection"]["开仓指标"], ["dif"])
        self.assertEqual(set(got["selection"]["入场触发口径"]), {"LIVE_01", "MACD_CYCLE"})
        self.assertEqual((got["exact_count"], got["entry_count"], got["projected_count"], got["baseline_count"]),
                         (15, 8, 32, 4))
        self.assertEqual(got["extra_count"], 17)
        config = fields_to_config(got["fields"])
        self.assertEqual(config["timeout_hours"], [6., 12.])
        self.assertEqual(config["paths"], [0, 1])
        self.assertEqual(config["add_drops"], [.01])
        self.assertEqual((config["tp_weekday"], config["tp_holiday"]), (.04, .02))
        self.assertEqual((config["seeds"], config["seed_start"], config["entry_gap_minutes"]), (10, 0, 1))
        self.assertEqual((config["initial_equity"], config["max_eth"], config["maker_fee_rate"]), (20000., 20., 0.))
        self.assertEqual(got["fields"]["entry_plan"], "combined")
        self.assertEqual(set(got["fields"]), set(DEFAULT_FIELDS))

    def test_add_drop_time_and_paths_are_the_only_merged_hedge_arrays(self):
        got = merge_hedge_restorations([self.record(), self.record(drop=.02, hours=12., path=1)])
        config = fields_to_config(got["fields"])
        self.assertEqual(config["add_drops"], [.01, .02])
        self.assertEqual(config["timeout_hours"], [6., 12.])
        self.assertEqual(config["paths"], [0, 1])

    def test_shared_source_date_s3_mode_and_fee_conflicts_are_rejected_without_mutation(self):
        def change_source(row):
            row["sources"]["kline"] += ".changed"

        def change_date(row):
            row["end"] = "2026-01-01"

        def change_s3(row):
            row["selection"]["入场约束"]["最小S3距离"] = .003
            row["origin_identity"]["hedge_request"]["selection"]["入场约束"]["最小S3距离"] = .003

        def change_plan(row):
            row["origin_identity"]["hedge_request"]["entry_plan"] = "independent"

        def change_fee(row):
            row["origin_identity"]["hedge_request"]["hedge"]["maker_fee_rate"] = .0002

        for mutation in (change_source, change_date, change_s3, change_plan, change_fee):
            records = [self.record(), self.record(hours=12.)]
            mutation(records[1])
            before = copy.deepcopy(records)
            with self.subTest(mutation=mutation.__name__), self.assertRaises(ValueError):
                merge_hedge_restorations(records)
            self.assertEqual(records, before)

    def test_mixed_engines_and_random_only_warmup_are_not_silently_changed(self):
        records = [self.record(), self.record(hours=12.)]
        records[1]["kind"] = "legacy"
        with self.assertRaises(ValueError):
            merge_hedge_restorations(records)
        with self.assertRaises(ValueError):
            merge_hedge_restorations([self.record(random=True, compare=True)])

    def test_single_compound_can_roundtrip_and_mixed_compound_pool_cannot(self):
        code = DEFAULT_REGISTRY.register("entry", [1, 42], "AND")
        records = [self.record(five_rule=code), self.record(five_rule=code, hours=12.)]
        got = merge_hedge_restorations(records)
        self.assertEqual(set(got["selection"]["开仓条件"]["5m"]), {1, 42})
        projected = dict(self.template["selection"], **got["selection"])
        self.assertEqual(list(choices_for_config(projected)["开仓"]["5m"]), [code])
        with self.assertRaises(ValueError):
            merge_hedge_restorations([records[0], self.record(five_rule=1)])
        with self.assertRaises(ValueError):
            merge_hedge_restorations([records[0], self.record(five_rule=0)])

    def test_permanently_retired_fifth_rule_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "永久删除"):
            merge_hedge_restorations([self.record(five_rule=264)])

    def test_fifth_and_one_regular_mode_share_editable_mode_without_changing_events(self):
        from hedge_entry_combinations import plan_request_entries
        from selection_config import 全选配置, 规范化配置

        records = [self.record(five_rule=288, mode="F5_EVENT"),
                   self.record(five_rule=57, mode="LIVE_01")]
        before = copy.deepcopy(records)
        projection = merge_hedge_restorations(records)
        self.assertEqual(projection["selection"]["入场触发口径"], "LIVE_01")
        editor_config = 全选配置()
        editor_config.update(projection["selection"])
        normalized = 规范化配置(editor_config)
        request = dict(selection=normalized, hedge=fields_to_config(projection["fields"]), entry_plan="combined")
        planned, _ = plan_request_entries(request, dict(capabilities=self.template["capabilities"],
                                                       capabilities_by_timeframe=self.template["capabilities_by_timeframe"]))
        original_entries = {record["origin_identity"]["hedge_request"]["hedge_exact"]["descriptor"]["id"]
                            for record in records}
        self.assertTrue(original_entries.issubset({entry["id"] for entry in planned}))
        fifth = next(entry for entry in planned if entry["id"] in original_entries and 288 in entry["cases"])
        self.assertEqual(fifth["entry_mode"], "F5_EVENT")
        self.assertEqual(records, before)

    def test_fifth_with_multiple_regular_modes_rejects_projection_before_editing(self):
        records = [self.record(five_rule=288, mode="F5_EVENT"),
                   self.record(five_rule=57, mode="LIVE_01"),
                   self.record(five_rule=57, mode="MACD_CYCLE")]
        before = copy.deepcopy(records)
        with self.assertRaisesRegex(ValueError, "第五"):
            merge_hedge_restorations(records)
        self.assertEqual(records, before)


class HiddenHedgeSyncUiTests(HedgeProjectionFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        import tkinter as tk
        from ui import App

        self.patches = []
        init = tk.Tk.__init__

        def hidden_init(window, *args, **kwargs):
            init(window, *args, **kwargs)
            window.withdraw()

        for p in (patch.object(tk.Tk, "__init__", hidden_init), patch.object(App, "load_user_settings"),
                  patch.object(App, "detect_gpu"), patch.object(App, "save_user_settings"),
                  patch("hedge_panel.HedgePanel.load_settings"), patch("ui.messagebox.showerror"),
                  patch("ui.messagebox.showinfo")):
            self.patches.append(p)
            p.start()
            self.addCleanup(p.stop)
        self.app = App()
        self.addCleanup(self.app.destroy)
        if self.app._poll_after_id:
            self.app.after_cancel(self.app._poll_after_id)
            self.app._poll_after_id = None
        self.app.hedge_panel.settings_path = self.fixture.root / "saved_hedge_settings.json"

    def test_projection_changes_entries_and_hedge_page_preserves_old_exits_cpu_and_queue(self):
        app = self.app
        projection = merge_hedge_restorations(self.fifteen())
        previous = app.selection_panel.get_config()
        exit_groups = copy.deepcopy(app.selection_panel.indicator_combos)
        unrelated = {name: getattr(app, name).get() for name in
                     ("thread_var", "initial_capital_var", "minimum_eth_var", "maximum_eth_var",
                      "entry_slippage_var", "exit_slippage_var")}
        app.fingerprint_queue = [copy.deepcopy(self.pinned)]
        original_queue = copy.deepcopy(app.fingerprint_queue)
        with patch.object(app, "_launch_process") as launch:
            app.apply_hedge_fingerprint_projection(projection)
        current = app.selection_panel.get_config()
        for key in set(previous) - ENTRY_KEYS:
            self.assertEqual(current[key], previous[key], key)
        for key in ("止损", "止盈"):
            self.assertEqual(app.selection_panel.indicator_combos.get(key), exit_groups.get(key))
        self.assertEqual(current["开仓条件"], projection["selection"]["开仓条件"])
        self.assertEqual(current["开仓指标"], ["dif"])
        self.assertEqual(set(app.selection_panel.get_entry_modes()), {"LIVE_01", "MACD_CYCLE"})
        self.assertEqual(app.fingerprint_queue, original_queue)
        for name, value in unrelated.items():
            self.assertEqual(getattr(app, name).get(), value, name)
        self.assertEqual(app.csv_var.get(), projection["sources"]["kline"])
        self.assertEqual((app.start_date_var.get(), app.end_date_var.get()), (projection["start"], projection["end"]))
        self.assertEqual(float(app.s3_gap_var.get()), .1)
        panel = app.hedge_panel
        config = fields_to_config({key: var.get() for key, var in panel.vars.items()})
        self.assertEqual(config, fields_to_config(projection["fields"]))
        self.assertEqual(panel.mode_var.get(), MODE_LABELS["scale_in"])
        self.assertEqual(panel.paths_var.get(), PATH_LABELS["both"])
        self.assertEqual(panel.entry_plan_var.get(), ENTRY_PLAN_LABELS["combined"])
        launch.assert_not_called()
        self.assertIsNone(app.proc)

    def test_bad_projection_does_not_partially_change_either_editor(self):
        app = self.app
        projection = merge_hedge_restorations(self.fifteen())
        projection["fields"]["maker_fee_percent"] = "nan"
        before = (app.selection_panel.get_config(), {key: var.get() for key, var in app.hedge_panel.vars.items()},
                  app.csv_var.get(), app.start_date_var.get(), app.end_date_var.get())
        with self.assertRaises(ValueError):
            app.apply_hedge_fingerprint_projection(projection)
        after = (app.selection_panel.get_config(), {key: var.get() for key, var in app.hedge_panel.vars.items()},
                 app.csv_var.get(), app.start_date_var.get(), app.end_date_var.get())
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
