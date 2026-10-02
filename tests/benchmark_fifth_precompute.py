"""Opt-in bounded synthetic benchmark; compares every resulting signal bit.

Example: python tests/benchmark_fifth_precompute.py --rows 30000 --workers 4
It uses temporary synthetic data and does not stop, alter or inspect a live run.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import sys

# Keep numerical libraries equal in serial and parallel; external workers own
# parallelism. These must be set before importing NumPy or fifth_batch.
for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS"):
    os.environ[name] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import fifth_model_audit
import fifth_precompute
from test_fifth_precompute import market_data, serial_masks


DEFAULT_NUMBERS = (10, 18, 19, 20, 21, 24, 29, 31, 32, 33,
                   48, 49, 51, 52, 53, 54, 66, 76)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--numbers", default=",".join(map(str, DEFAULT_NUMBERS)))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    numbers = tuple(dict.fromkeys(int(value) for value in args.numbers.split(",")))
    tasks = [("1m", number + 263) for number in numbers]
    data = market_data(args.rows, timeframes=("1m",), gaps=False)
    fifth_model_audit.set_output_directory(None)
    warmup = market_data(min(600, args.rows), timeframes=("1m",), gaps=False)
    for tf, code in tasks:
        serial_masks(warmup, tf, code)
    reference = {}
    timings = []
    begin = time.perf_counter()
    for tf, code in tasks:
        started = time.perf_counter()
        reference[tf, code] = serial_masks(data, tf, code)
        duration = time.perf_counter() - started
        timings.append({"number": code - 263, "seconds": round(duration, 6)})
        print(f"serial F5-{code - 263:03d}: {duration:.3f}s", flush=True)
    serial_seconds = time.perf_counter() - begin
    events = []
    with tempfile.TemporaryDirectory(prefix="fifth-parallel-benchmark-") as temporary:
        output = Path(temporary) / "run"
        started = time.perf_counter()
        cache = fifth_precompute.prepare_fifth_signals(
            data, tasks, output, {"benchmark": "fixed-synthetic-v1"},
            args.workers, events.append)
        parallel_seconds = time.perf_counter() - started
        for tf, code in tasks:
            actual = fifth_precompute.load_signals(cache, tf, code, args.rows)
            np.testing.assert_array_equal(actual, reference[tf, code])
            del actual
        started = time.perf_counter()
        reused = fifth_precompute.prepare_fifth_signals(
            data, tasks, output, {"benchmark": "fixed-synthetic-v1"},
            args.workers, lambda event: None)
        cached_seconds = time.perf_counter() - started
        assert Path(reused) == Path(cache)
    result = {"rows_per_method": args.rows, "methods": len(tasks),
              "requested_workers": args.workers,
              "serial_seconds": serial_seconds,
              "parallel_seconds_including_spawn_and_cache_io": parallel_seconds,
              "same_run_cached_seconds": cached_seconds,
              "speedup": serial_seconds / parallel_seconds,
              "all_signals_bit_equal": True,
              "serial_method_timings": timings, "events": events}
    encoded = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("events", "serial_method_timings")},
                     ensure_ascii=False, indent=2), flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
