"""Run shipped regression tests without a private cache or real-data download."""
from pathlib import Path
import os
import sys
import unittest
from test_support import prepare_test_features

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    os.chdir(root)
    # Never replace or write the user's saved preferences while constructing GUI.
    prepare_test_features()
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
