"""Verify actual worker task generators stay lazy and preserve legacy ordering."""
import ast
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
import unittest

WORKER = Path(__file__).resolve().parents[1] / 'backtest_worker.py'
TREE = ast.parse(WORKER.read_text('utf-8-sig'))
FUNCTIONS = [node for node in ast.walk(TREE) if isinstance(node, ast.FunctionDef)
             and node.name in ('combo_tasks', 'cpu_groups')]


def fixture():
    namespace = {
        'stops': [(f'S{i}', f'stop{i}', None) for i in range(3)],
        'overlay_variants': [('OFF', 'none', None), ('TP1', 'fixed', object())],
        'selected_fixed_stops': [('OFF', 0., 0., 'none'), ('FS1', .01, .02, 'fixed')],
        'selected_hard_time': [0, 30], 'selected_cooldowns': [0, 10],
        'entry_variants': [(d, 'ALL', 'OFF', 'none', [(i, None, None) for i in range(5)])
                           for d in ('BOTH', 'LONG')],
        'B': SimpleNamespace(STOP_INDEX={f'S{i}': i for i in range(3)}),
        'field_index': 0, 'stable_base_id': lambda field, cases, stop: stop * 100 + cases,
        'cpu_group_size': 7, 'task_count': 480,
    }
    exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), str(WORKER), 'exec'), namespace)
    return namespace


class LazyCpuTaskTests(unittest.TestCase):
    def test_exact_legacy_stop_combo_cooldown_entry_order_and_limit(self):
        ns = fixture()
        legacy = {}
        for overlay in ns['overlay_variants']:
            for stop in ns['stops']:
                for fixed in ns['selected_fixed_stops']:
                    for hard in ns['selected_hard_time']:
                        key = (fixed[0], hard, overlay[0])
                        legacy.setdefault(stop[0], {})[key] = list(ns['combo_tasks'](stop, overlay, fixed, hard))
        expected = [(*task, cooldown) for combos in legacy.values() for tasks in combos.values()
                    for cooldown in ns['selected_cooldowns'] for task in tasks]
        for limit in (1, 7, 70, len(expected)):
            ns['task_count'] = limit
            groups = list(ns['cpu_groups']())
            self.assertTrue(all(len(group) <= 7 for _, group in groups))
            self.assertEqual([task for _, group in groups for task in group], expected[:limit])

    def test_millions_of_combinations_only_materialize_requested_group(self):
        ns = fixture()
        ns['selected_hard_time'] = range(1_000_000)
        ns['task_count'] = 240_000_000
        ns['cpu_group_size'] = 64
        built = []
        original = ns['stable_base_id']
        def count(*args):
            built.append(args)
            return original(*args)
        ns['stable_base_id'] = count
        source = ns['cpu_groups']()
        code, group = next(source)
        self.assertEqual(code, 'S0')
        self.assertEqual(len(group), 64)
        self.assertEqual(len(built), 64)
        source.close()


if __name__ == '__main__':
    unittest.main()
