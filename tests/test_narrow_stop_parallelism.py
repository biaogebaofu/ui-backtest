"""Exercise the actual worker generators with small, observable work items."""
import ast
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from itertools import islice
import math
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / 'backtest_worker.py').read_text('utf-8-sig'))
NAMES = {'CPU分组上限', '有界顺序结果', 'cpu_groups', 'evaluate_group', 'cpu_results'}
FUNCTIONS = [node for node in ast.walk(TREE)
             if isinstance(node, ast.FunctionDef) and node.name in NAMES]


class Flag:
    def __init__(self):
        self.event = threading.Event()

    def __truediv__(self, name):
        return self

    def exists(self):
        return self.event.is_set()


def worker_fixture(stop_count=1, entries=8, cooldowns=(0,), limit=None, evaluate=None):
    flag = Flag()
    pending_tasks = {}
    stop_map = {}
    cache = {}
    cache_builds = []
    for stop in range(stop_count):
        code = f'S{stop}'
        combo = (code, 0., 0., 30)
        tasks = [(stop * entries + entry, None, None, None, code, None, combo)
                 for entry in range(entries)]
        pending_tasks[code] = {'combo': tasks}
        stop_map[code] = object()

    def combined(base, weekday, weekend, minutes, combo):
        key = (id(base), combo)
        if key not in cache:
            # Release the GIL to expose duplicate construction without the lock.
            threading.Event().wait(.005)
            cache[key] = object()
            cache_builds.append(key)
        return cache[key]

    def unexpected_make(*args):
        raise AssertionError('Narrow groups must reuse their resident base stop')

    namespace = {
        'math': math, 'deque': deque, 'islice': islice, 'CPU常驻止损上限': 8,
        'task_count': min(limit, stop_count * entries * len(cooldowns)) if limit is not None
                      else stop_count * entries * len(cooldowns),
        'stops': [(code, code, data) for code, data in stop_map.items()],
        'overlay_variants': [('OFF', '', None)],
        'selected_fixed_stops': [('OFF', 0., 0., '')],
        'selected_hard_time': [30],
        'combo_tasks': lambda stop, *_: iter(pending_tasks[stop[0]]['combo']),
        'selected_cooldowns': cooldowns,
        'stop_map': stop_map, 'control_dir': flag, 'stop_requested': flag.exists,
        'B': SimpleNamespace(combined_stop_data=combined, make_stop_data=unexpected_make),
        'args': SimpleNamespace(threads=18),
        'evaluate': evaluate or (lambda prepared: (prepared[0][0], prepared[0][-1])),
    }
    exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), str(ROOT / 'backtest_worker.py'), 'exec'), namespace)
    size = namespace['CPU分组上限'](namespace['task_count'], 18, stop_count)
    namespace['cpu_group_size'] = size
    namespace['group_stop_lock'] = threading.Lock() if size < 64 else None
    return namespace, flag, cache_builds


class NarrowStopParallelismTests(unittest.TestCase):
    def test_one_stop_eight_entries_execute_on_worker_threads_and_share_stop_data(self):
        caller = threading.get_ident()
        barrier = threading.Barrier(8)
        thread_ids = []
        stop_ids = []

        def evaluate(prepared):
            thread_ids.append(threading.get_ident())
            stop_ids.append(id(prepared[1]))
            barrier.wait(timeout=5)
            return prepared[0][0]

        namespace, _, builds = worker_fixture(evaluate=evaluate)
        self.assertEqual(namespace['cpu_group_size'], 1)
        with ThreadPoolExecutor(max_workers=18) as pool:
            actual = list(namespace['cpu_results'](pool))
        self.assertEqual(actual, list(range(8)))
        self.assertNotIn(caller, thread_ids)
        self.assertEqual(len(set(thread_ids)), 8)
        self.assertEqual(len(set(stop_ids)), 1)
        self.assertEqual(len(builds), 1)

    def test_each_take_profit_batch_uses_pool_instead_of_main_thread(self):
        caller = threading.get_ident()
        for take_profit in range(3):
            ids = []

            def evaluate(prepared):
                ids.append(threading.get_ident())
                return take_profit, prepared[0][0]

            namespace, _, _ = worker_fixture(evaluate=evaluate)
            with ThreadPoolExecutor(max_workers=18) as pool:
                actual = list(namespace['cpu_results'](pool))
            self.assertEqual(actual, [(take_profit, entry) for entry in range(8)])
            self.assertNotIn(caller, ids)

    def test_limit_cooldowns_and_stop_order_remain_identical(self):
        namespace, _, _ = worker_fixture(stop_count=2, entries=4, cooldowns=(0, 30), limit=11)
        expected = [(entry, cooldown) for stop in range(2) for cooldown in (0, 30)
                    for entry in range(stop * 4, stop * 4 + 4)][:11]
        with ThreadPoolExecutor(max_workers=18) as pool:
            self.assertEqual(list(namespace['cpu_results'](pool)), expected)

    def test_nonresident_many_stop_configuration_keeps_previous_group_size(self):
        namespace, _, _ = worker_fixture(stop_count=9, entries=80)
        self.assertEqual(namespace['cpu_group_size'], 64)
        groups = list(namespace['cpu_groups']())
        self.assertEqual([len(tasks) for _, tasks in groups], [64, 16] * 9)
        self.assertEqual([task[0] for _, tasks in groups for task in tasks], list(range(720)))

    def test_stop_before_narrow_batch_submits_no_work(self):
        called = []
        namespace, flag, _ = worker_fixture(evaluate=lambda prepared: called.append(prepared))
        flag.event.set()
        with ThreadPoolExecutor(max_workers=18) as pool:
            self.assertEqual(list(namespace['cpu_results'](pool)), [])
        self.assertEqual(called, [])

    def test_first_numba_dispatch_can_compile_from_concurrent_narrow_groups(self):
        from numba import njit

        @njit(nogil=True, cache=False)
        def cold_kernel(value):
            return value * value

        barrier = threading.Barrier(8)

        def evaluate(prepared):
            barrier.wait(timeout=5)
            return cold_kernel(prepared[0][0])

        namespace, _, _ = worker_fixture(evaluate=evaluate)
        with ThreadPoolExecutor(max_workers=18) as pool:
            self.assertEqual(list(namespace['cpu_results'](pool)), [value * value for value in range(8)])
        self.assertEqual(len(cold_kernel.nopython_signatures), 1)


if __name__ == '__main__':
    unittest.main()
