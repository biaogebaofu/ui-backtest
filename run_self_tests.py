"""Run shipped regression tests without a private cache or real-data download."""
from pathlib import Path
import gc
import os
import sys
import unittest
from test_support import prepare_test_features


class DesktopTestResult(unittest.TextTestResult):
    def startTest(self, test):
        if sys.platform == 'darwin':
            # Reclaim destroyed GUI objects between tests on the Tk owner thread.
            gc.collect()
        super().startTest(test)


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    os.chdir(root)
    # Never replace or write the user's saved preferences while constructing GUI.
    prepare_test_features()
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    result = unittest.TextTestRunner(verbosity=2, resultclass=DesktopTestResult).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
