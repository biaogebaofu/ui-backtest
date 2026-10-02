"""Windows commit pressure and numerical-library limits at child import time."""
import multiprocessing
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

THREAD_KEYS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS')
ENV_BEFORE_NUMPY = {key: os.environ.get(key) for key in THREAD_KEYS}

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fifth_precompute as precompute
from gp_parallel import single_threaded_children

GIB = 1024**3


def child_import_limits(results):
    from scipy import linalg
    from threadpoolctl import threadpool_info
    linalg.norm([1., 2.])
    results.put((ENV_BEFORE_NUMPY, [pool['num_threads'] for pool in threadpool_info()]))


class WorkerBudget(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows commit accounting')
    def test_windows_limits_available_memory_by_commit_or_physical(self):
        def status(physical, commit):
            def fill(pointer):
                pointer._obj.avail_phys = physical
                pointer._obj.avail_page = commit
                return 1
            return fill

        api = precompute.ctypes.windll.kernel32
        with mock.patch.object(api, 'GlobalMemoryStatusEx', side_effect=status(14*GIB, 4*GIB)):
            self.assertEqual(precompute.available_memory(), 4*GIB)
            with mock.patch.object(precompute.os, 'cpu_count', return_value=18):
                self.assertEqual(precompute.worker_limit(18, 100, 1505), 2)
        with mock.patch.object(api, 'GlobalMemoryStatusEx', side_effect=status(3*GIB, 15*GIB)):
            self.assertEqual(precompute.available_memory(), 3*GIB)

    def test_gp_workers_have_import_headroom_even_for_small_training_blocks(self):
        with mock.patch.object(precompute.os, 'cpu_count', return_value=18):
            self.assertEqual(precompute.worker_limit(18, 100, 1505, 14*GIB), 13)
            self.assertEqual(precompute.worker_limit(18, 100, 1505, 8*GIB), 7)
            self.assertEqual(precompute.worker_limit(6, 100, 1505, 14*GIB), 6)
            self.assertEqual(precompute.worker_limit(18, 2, 1505, 14*GIB), 2)
            self.assertEqual(precompute.worker_limit(18, 100, 900000, 8*GIB), 5)

    def test_spawn_inherits_caps_before_numpy_import_and_restores_parent(self):
        context = multiprocessing.get_context('spawn')
        results = context.Queue()
        previous = {key: os.environ.get(key) for key in THREAD_KEYS}
        try:
            with single_threaded_children():
                child = context.Process(target=child_import_limits, args=(results,))
                child.start()
            self.assertEqual({key: os.environ.get(key) for key in THREAD_KEYS}, previous)
            environment, pools = results.get(timeout=30)
            child.join(timeout=30)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(environment, dict.fromkeys(THREAD_KEYS, '1'))
            self.assertTrue(pools)
            self.assertTrue(all(count == 1 for count in pools), pools)
        finally:
            if 'child' in locals() and child.is_alive():
                child.terminate()
                child.join()
            results.close()

    def test_failed_spawn_setup_restores_existing_and_absent_environment(self):
        with mock.patch.dict(os.environ, {'OPENBLAS_NUM_THREADS': '18'}):
            os.environ.pop('NUMEXPR_NUM_THREADS', None)
            previous = {key: os.environ.get(key) for key in THREAD_KEYS}
            with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
                with single_threaded_children():
                    raise RuntimeError('spawn failed')
            self.assertEqual({key: os.environ.get(key) for key in THREAD_KEYS}, previous)


if __name__ == '__main__':
    unittest.main(verbosity=2)
