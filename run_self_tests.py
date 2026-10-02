"""Run shipped regression tests without a private cache or real-data download."""
from pathlib import Path
import os
import subprocess
import sys
import unittest
from test_support import prepare_test_features


ISOLATED_MACOS_MODULE = 'test_v166_hedge_fingerprints'


def without_module(suite, module):
    remaining = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            remaining.addTest(without_module(test, module))
        elif test.__class__.__module__ != module:
            remaining.addTest(test)
    return remaining


def run_tests(suite, root):
    total = suite.countTestCases()
    isolated_count = 0
    isolated_code = 0
    if sys.platform == 'darwin':
        remaining = without_module(suite, ISOLATED_MACOS_MODULE)
        isolated_count = total - remaining.countTestCases()
        # Aqua Tk shares an event queue across interpreters. This module passes
        # alone but stalls in update() after the full suite's earlier Tk roots.
        print(f'Running {isolated_count} macOS GUI tests in a fresh process', flush=True)
        isolated_code = subprocess.run([
            sys.executable, '-u', '-m', 'unittest', 'discover',
            '-s', str(root / 'tests'), '-p', ISOLATED_MACOS_MODULE + '.py', '-v',
        ], cwd=root).returncode
        suite = remaining
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if isolated_count:
        print(f'Full regression: {total} selected tests; '
              f'{result.testsRun} in the main process, {isolated_count} isolated. '
              f'Isolated exit code: {isolated_code}; '
              f'main failures: {len(result.failures)}, errors: {len(result.errors)}.', flush=True)
    return 0 if result.wasSuccessful() and isolated_code == 0 else 1


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    os.chdir(root)
    # Never replace or write the user's saved preferences while constructing GUI.
    prepare_test_features()
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    sys.exit(run_tests(suite, root))
