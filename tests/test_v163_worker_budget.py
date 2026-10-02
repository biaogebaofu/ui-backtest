"""Windows commit pressure and numerical-library limits at child import time."""
import ctypes
import multiprocessing
import os
from pathlib import Path
import platform
import sys
import unittest
from unittest import mock

THREAD_KEYS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
ENV_BEFORE_NUMPY = {key: os.environ.get(key) for key in THREAD_KEYS}

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fifth_precompute as precompute
import gp_parallel
from gp_parallel import single_threaded_children

GIB = 1024**3


def child_import_limits(results):
    import numpy as np
    import scipy
    from scipy.linalg.blas import dgemm
    from threadpoolctl import threadpool_info
    precompute._initialize_worker()
    matrix = np.arange(64, dtype=float).reshape(8, 8)
    product = matrix @ matrix
    np.testing.assert_allclose(dgemm(1., matrix, matrix), product)
    backends = [module.show_config(mode='dicts')['Build Dependencies']['blas']['name']
                for module in (np, scipy)]
    accelerate_model = None
    if 'accelerate' in backends:
        accelerate = ctypes.CDLL('/System/Library/Frameworks/Accelerate.framework/Accelerate')
        get_threading = getattr(accelerate, 'BLASGetThreading', None)
        if get_threading is not None:
            get_threading.argtypes = []
            get_threading.restype = ctypes.c_uint
            accelerate_model = get_threading()
    results.put((ENV_BEFORE_NUMPY, [pool['num_threads'] for pool in threadpool_info()],
                 backends, accelerate_model, platform.mac_ver()[0]))


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
            environment, pools, backends, accelerate_model, mac_version = results.get(timeout=30)
            child.join(timeout=30)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(environment, dict.fromkeys(THREAD_KEYS, '1'))
            if 'accelerate' in backends:
                self.assertEqual(sys.platform, 'darwin')
                if accelerate_model is None:
                    self.assertTrue(mac_version)
                    self.assertLess(int(mac_version.split('.')[0]), 15)
                    self.assertEqual(environment['VECLIB_MAXIMUM_THREADS'], '1')
                else:
                    self.assertEqual(accelerate_model, 1)  # BLAS_THREADING_SINGLE_THREADED
            else:
                self.assertTrue(pools)
            self.assertTrue(all(count == 1 for count in pools), pools)
        finally:
            if 'child' in locals() and child.is_alive():
                child.terminate()
                child.join()
            results.close()

    def test_accelerate_native_limit_sets_single_thread_and_rejects_failure(self):
        accelerate = mock.Mock(spec=['BLASSetThreading'])
        accelerate.BLASSetThreading.return_value = 0
        with mock.patch.object(gp_parallel.sys, 'platform', 'darwin'), \
                mock.patch.object(ctypes, 'CDLL', return_value=accelerate):
            gp_parallel._limit_accelerate_threads()
            accelerate.BLASSetThreading.assert_called_once_with(1)
            self.assertEqual(accelerate.BLASSetThreading.argtypes, [ctypes.c_uint])
            self.assertEqual(accelerate.BLASSetThreading.restype, ctypes.c_int)
            accelerate.BLASSetThreading.return_value = -1
            with self.assertRaisesRegex(RuntimeError, 'Accelerate'):
                gp_parallel._limit_accelerate_threads()

    def test_accelerate_without_native_api_requires_older_macos(self):
        with mock.patch.object(gp_parallel.sys, 'platform', 'darwin'), \
                mock.patch.object(ctypes, 'CDLL', return_value=mock.Mock(spec=[])), \
                mock.patch.object(platform, 'mac_ver', return_value=('14.7', (), '')):
            gp_parallel._limit_accelerate_threads()
            with mock.patch.object(platform, 'mac_ver', return_value=('15.0', (), '')):
                with self.assertRaisesRegex(RuntimeError, 'Accelerate'):
                    gp_parallel._limit_accelerate_threads()

    def test_failed_spawn_setup_restores_existing_and_absent_environment(self):
        with mock.patch.dict(os.environ, {'OPENBLAS_NUM_THREADS': '18',
                                         'VECLIB_MAXIMUM_THREADS': '18'}):
            os.environ.pop('NUMEXPR_NUM_THREADS', None)
            previous = {key: os.environ.get(key) for key in THREAD_KEYS}
            with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
                with single_threaded_children():
                    raise RuntimeError('spawn failed')
            self.assertEqual({key: os.environ.get(key) for key in THREAD_KEYS}, previous)


if __name__ == '__main__':
    unittest.main(verbosity=2)
