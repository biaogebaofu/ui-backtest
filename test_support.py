"""Small deterministic fixtures for self-tests, never user market data."""
from __future__ import annotations
import atexit
import os
import tempfile
from pathlib import Path

_fixture_dir = None

def prepare_test_features():
    global _fixture_dir
    current = os.environ.get('BT_FEATURES', '')
    if current and Path(current).is_file():
        return Path(current)
    import numpy as np
    import pandas as pd
    from feature_builder import build_features
    _fixture_dir = tempfile.TemporaryDirectory(prefix='eth_ui_selftest_')
    atexit.register(_fixture_dir.cleanup)
    root = Path(_fixture_dir.name)
    count = 1440
    x = np.arange(count)
    close = 2000. + 5. * np.sin(x / 11.) + 2. * np.sin(x / 3.)
    open_ = np.r_[close[0], close[:-1]]
    pd.DataFrame({
        'openTime': int(pd.Timestamp('2026-01-02T12:00:00Z').value // 1_000_000) + x * 60000, 'open': open_,
        'high': np.maximum(open_, close) + 0.5, 'low': np.minimum(open_, close) - 0.5,
        'close': close, 'volume': 10. + (x % 11),
    }).to_csv(root/'ETHUSDC_test.csv', index=False)
    path = build_features(str(root/'ETHUSDC_test.csv'), str(root/'cache'))
    os.environ['BT_FEATURES'] = str(path)
    return path
