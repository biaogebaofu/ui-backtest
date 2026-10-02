from __future__ import annotations

import copy
import math
import tempfile
import unittest
from unittest import mock

import indicator_combinations as combos
from selection_config import 全选配置, 规范化配置, 配置统计, 配置签名


def spec(*sizes, keep=True, logic="AND"):
    return {"启用": True, "组合数量": list(sizes), "保留单项": keep, "逻辑": logic}


def small_config():
    config = 全选配置()
    config.update({"开仓指标": ["hist"], "开仓条件": {tf: [0] for tf in combos.TIMEFRAMES},
                   "止损代码": ["OFF", "S1", "S3_1m", "S6_1m"], "固定止损代码": ["OFF"],
                   "止盈方案编号": [1, 2, 3, 4], "止盈后等待分钟": [0], "仓位倍数": [1.]})
    config["开仓条件"]["1m"] = [0, 1, 2, 3]
    return config


class CombinationChoicesTests(unittest.TestCase):
    def test_disabled_preserves_legacy_order_and_identifiers(self):
        choices = combos.CombinationChoices([3, 0, 1, 2], {"启用": False})
        self.assertEqual(list(choices), [3, 0, 1, 2])
        self.assertEqual(choices.count, 4)
        self.assertEqual(choices[-1], 2)

    def test_pairs_triples_keep_off_only_as_single(self):
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices([0, 1, 2, 3, 4], spec(2, 3), registry=registry)
        self.assertEqual(choices.count, 5 + math.comb(4, 2) + math.comb(4, 3))
        rows = list(choices)
        self.assertEqual(rows[:5], [0, 1, 2, 3, 4])
        for code in rows[5:]:
            definition = registry.resolve("entry", code)
            self.assertNotIn(0, definition["members"])
            self.assertIn(len(definition["members"]), (2, 3))

    def test_without_singles_and_arbitrary_size(self):
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices([0, *range(1, 9)], spec(5, 8, keep=False), registry=registry)
        self.assertEqual(choices.count, math.comb(8, 5) + 1)
        self.assertEqual(registry.resolve("entry", choices[-1])["members"], list(range(1, 9)))
        self.assertNotIn(0, choices)
        self.assertNotIn(1, choices)

    def test_random_access_matches_lexicographic_iteration(self):
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices(range(7), spec(2, 4, 6), registry=registry)
        expected = list(choices)
        self.assertEqual([choices[i] for i in range(choices.count)], expected)
        self.assertEqual(choices[2:14:3], expected[2:14:3])
        self.assertEqual(choices[-1::-3], expected[-1::-3])
        self.assertEqual(list(choices), expected)
        with self.assertRaises(IndexError):
            choices[choices.count]

    def test_canonical_order_deduplication_and_logic_identity(self):
        registry = combos.CombinationRegistry()
        first = registry.register("entry", [5, 2, 5, 1], "AND")
        self.assertEqual(first, registry.register("entry", [1, 2, 5], "AND"))
        self.assertNotEqual(first, registry.register("entry", [1, 2, 5], "OR"))
        self.assertLess(first, 0)
        self.assertGreaterEqual(first, -(2 ** 63 - 1))
        self.assertNotEqual(first, registry.register("tp", [2, 3, 4], "OR"))

    def test_large_space_does_not_materialize_or_register(self):
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices(range(1, 201), spec(50, keep=False), registry=registry)
        self.assertEqual(choices.count, math.comb(200, 50))
        with self.assertRaises(OverflowError):
            len(choices)
        self.assertEqual(list(registry.iter_definitions()), [])
        first = choices[0]
        last = choices[-1]
        middle = choices[choices.count // 2]
        self.assertEqual(len(list(registry.iter_definitions())), 3)
        self.assertEqual(registry.resolve("entry", first)["members"], list(range(1, 51)))
        self.assertEqual(registry.resolve("entry", last)["members"], list(range(151, 201)))
        self.assertIn(middle, choices)
        self.assertEqual(choices[choices.count // 2], middle)

    def test_contains_is_not_an_enumeration(self):
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices(range(1, 101), spec(6, keep=False), registry=registry)
        accepted = registry.register("entry", [1, 7, 9, 24, 55, 97], "AND")
        wrong_logic = registry.register("entry", [1, 7, 9, 24, 55, 97], "OR")
        wrong_size = registry.register("entry", [1, 7], "AND")
        outside = registry.register("entry", [1, 7, 9, 24, 55, 102], "AND")
        with mock.patch.object(combos.CombinationChoices, "__iter__", side_effect=AssertionError("enumerated")):
            self.assertIn(accepted, choices)
            for code in (wrong_logic, wrong_size, outside, -1):
                self.assertNotIn(code, choices)

    def test_stop_choices_keep_existing_composite_members(self):
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices(["OFF", "S1+S3_1m", "S6_1m"],
                                            spec(2, keep=False, logic="OR"), "stop", registry)
        code = choices[0]
        self.assertTrue(code.startswith("@COMBO:OR:"))
        self.assertEqual(combos.stop_members(code, registry), ("S1+S3_1m", "S6_1m"))
        self.assertEqual(combos.stop_spec(code, registry)[1], ("S1", "S3_1m", "S6_1m"))

    def test_staged_take_profit_is_only_a_single(self):
        catalog = combos._tp_catalog()
        staged = next(row.编号 for row in catalog.values() if row.类别 == "分批止盈")
        registry = combos.CombinationRegistry()
        choices = combos.CombinationChoices([1, 2, 3, staged], spec(2, logic="OR"), "tp", registry)
        self.assertEqual(choices.count, 5)
        self.assertEqual(choices.members, (2, 3))
        self.assertIn(staged, choices)
        compound = choices[-1]
        self.assertEqual(combos.tp_members(compound, registry), (2, 3))
        self.assertEqual(combos.tp_spec(compound, registry).类别, "指标组合止盈")


class CombinationRegistryTests(unittest.TestCase):
    def test_save_load_round_trip_and_cross_process_identity(self):
        registry = combos.CombinationRegistry()
        entry = registry.register("entry", [1, 2], "AND")
        stop = registry.register("stop", ["S1", "S6_1m"], "OR")
        tp = registry.register("tp", [2, 3], "OR")
        with tempfile.TemporaryDirectory() as directory:
            path = registry.save(directory)
            self.assertEqual(path.name, combos.REGISTRY_FILENAME)
            restored = combos.CombinationRegistry().load(directory)
        self.assertEqual(restored.to_dict(), registry.to_dict())
        self.assertEqual(restored.register("entry", [2, 1], "AND"), entry)
        self.assertEqual(combos.stop_members(stop, restored), ("S1", "S6_1m"))
        self.assertEqual(combos.tp_members(tp, restored), (2, 3))

    def test_collision_is_rejected(self):
        registry = combos.CombinationRegistry()
        with mock.patch.object(combos, "_identifier", return_value=-99):
            registry.register("entry", [1, 2], "AND")
            with self.assertRaisesRegex(ValueError, "冲突"):
                registry.register("entry", [1, 3], "AND")

    def test_tampered_dictionary_is_rejected_without_partial_update(self):
        registry = combos.CombinationRegistry()
        registry.register("entry", [1, 2], "AND")
        payload = registry.to_dict()
        payload["组合"][0]["members"] = [1, 3]
        restored = combos.CombinationRegistry()
        with self.assertRaisesRegex(ValueError, "不一致"):
            restored.update(payload)
        self.assertEqual(list(restored.iter_definitions()), [])

    def test_resolve_cannot_mutate_dictionary(self):
        registry = combos.CombinationRegistry()
        code = registry.register("entry", [1, 2], "AND")
        registry.resolve("entry", code)["members"].append(4)
        self.assertEqual(registry.resolve("entry", code)["members"], [1, 2])

    def test_iteration_allows_new_registration(self):
        registry = combos.CombinationRegistry()
        registry.register("entry", [1, 2], "AND")
        registry.register("entry", [1, 3], "AND")
        iterator = registry.iter_definitions("entry")
        next(iterator)
        registry.register("entry", [1, 4], "AND")
        self.assertEqual(len(list(iterator)), 1)

    def test_missing_compound_definition_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "缺少指标组合字典"):
            combos.entry_members(-123, combos.CombinationRegistry())


class CombinationSelectionTests(unittest.TestCase):
    def test_disabled_settings_preserve_signature(self):
        original = small_config()
        disabled = copy.deepcopy(original)
        disabled["指标组合"] = {"开仓": {"1m": {"启用": False}},
                                 "止损": {"启用": False}, "止盈": {"启用": False}}
        self.assertEqual(规范化配置(original), 规范化配置(disabled))
        self.assertEqual(配置签名(original), 配置签名(disabled))
        self.assertNotIn("指标组合", 规范化配置(disabled))

    def test_public_example_full_selection_signature_is_stable(self):
        original = 全选配置()
        original["开仓条件"] = {tf: [code for code in codes if code < 264]
                                 for tf, codes in original["开仓条件"].items()}
        self.assertEqual(配置签名(original), "720abbc3033c62e10d78d9c64cc4e7c7dc4330996488f2d05eb107db222a1882")

    def test_selection_normalization_and_counts_match_enumeration(self):
        config = small_config()
        config["指标组合"] = {"开仓": {"1m": spec(3, 2, 2)},
                              "止损": spec(2, logic="OR"), "止盈": spec(2, 3, logic="OR")}
        normalized = 规范化配置(config)
        self.assertEqual(normalized["指标组合"]["开仓"]["1m"]["组合数量"], [2, 3])
        stats = 配置统计(config)
        self.assertEqual(stats["入场组合数"], 4 + 3 + 1)
        self.assertEqual(stats["信号止损数"], 4 + 3)
        self.assertEqual(stats["止盈方案数"], 4 + 3 + 1)
        self.assertEqual(stats["包含仓位完整组合数"], 8 * 7 * 8)
        self.assertEqual(规范化配置(normalized), normalized)

    def test_every_entry_timeframe_can_enable_its_own_combination(self):
        config = small_config()
        config["开仓条件"] = {tf: [0, 1, 2] for tf in combos.TIMEFRAMES}
        config["指标组合"] = {"开仓": {tf: spec(2, keep=False) for tf in combos.TIMEFRAMES}}
        choices = combos.choices_for_config(规范化配置(config))
        self.assertEqual([choices["开仓"][tf].count for tf in combos.TIMEFRAMES], [1] * 5)
        self.assertEqual(配置统计(config)["入场组合数"], 1)

    def test_invalid_stage_size_and_logic_are_rejected(self):
        bad = [{"开仓": {"2m": spec(2)}}, {"开仓": {"1m": spec(0)}},
               {"开仓": {"1m": spec(2, logic="OR")}}, {"止盈": spec(2, logic="AND")},
               {"止损": spec(4, logic="OR")}, {"开仓": {"1m": spec(4)}},
               {"未知": spec(2)}, {"止损": spec(True, logic="OR")}]
        for setting in bad:
            with self.subTest(setting=setting):
                config = small_config()
                config["指标组合"] = setting
                with self.assertRaises(ValueError):
                    规范化配置(config)

    def test_stats_never_register_or_enumerate_combinations(self):
        config = small_config()
        config["开仓条件"]["1m"] = list(range(1, 201))
        config["指标组合"] = {"开仓": {"1m": spec(10, keep=False)}}
        before = combos.DEFAULT_REGISTRY.to_dict()
        with mock.patch.object(combos.CombinationRegistry, "register", side_effect=AssertionError("registered")):
            stats = 配置统计(config)
        self.assertEqual(stats["入场组合数"], math.comb(200, 10))
        self.assertEqual(combos.DEFAULT_REGISTRY.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
