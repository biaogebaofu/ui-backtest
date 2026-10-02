"""Archive fitted fifth-round models and the predictions actually issued in this run."""
from __future__ import annotations
import gzip
import json
import pickle
from pathlib import Path
import numpy as np

_directory = None
_streams = {}
_timeframe = '1m'
_parts = {}


def set_timeframe(timeframe):
    global _timeframe
    _timeframe = timeframe


def set_output_directory(path):
    global _directory
    close_archives()
    _parts.clear()
    _directory = Path(path) if path is not None else None


def record_fit(number, timestamp, model, settings):
    if _directory is None:
        return
    _directory.mkdir(parents=True, exist_ok=True)
    key = (number, _timeframe)
    if key not in _streams:
        _streams[key] = gzip.open(_directory / f'F5-{number:03d}_{_timeframe}_训练快照.pkl.gz', 'ab', compresslevel=1)
        (_directory / f'F5-{number:03d}_{_timeframe}_模型说明.json').write_text(
            json.dumps(settings, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    pickle.dump({'fit_at': str(timestamp), 'settings': settings, 'model': model},
                _streams[key], protocol=5)


def record_predictions(number, close_times, predictions, **arrays):
    if _directory is None:
        return
    _directory.mkdir(parents=True, exist_ok=True)
    key = (number, _timeframe)
    _parts[key] = _parts.get(key, 0) + 1
    path = _directory / f'F5-{number:03d}_{_timeframe}_当时预测_{_parts[key]:04d}.npz'
    while path.exists():
        _parts[key] += 1
        path = _directory / f'F5-{number:03d}_{_timeframe}_当时预测_{_parts[key]:04d}.npz'
    np.savez_compressed(path,
                        close_times=np.asarray(close_times).astype(str),
                        predictions=np.asarray(predictions), **arrays)


def close_archives():
    for stream in _streams.values():
        stream.close()
    _streams.clear()
