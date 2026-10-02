"""使用用户百万级选择和完整行情，仅计算前16组，禁止在原结果目录写入。"""
import csv
import json
import subprocess
import sys
import threading
from pathlib import Path
import ctypes
from ctypes import wintypes

class MemoryInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ('peak_rss', 'rss', 'peak_paged', 'paged',
                                            'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]

root = Path(__file__).resolve().parents[1]
settings = json.loads((root/'用户设置.json').read_text('utf-8'))
out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=False)
config = out/'验证组合.json'
config.write_text(json.dumps(settings['组合选择'], ensure_ascii=False), 'utf-8')
args = [sys.executable, '-X', 'utf8', str(root/'backtest_worker.py'), '--csv', settings['数据CSV'],
        '--output', str(out/'结果'), '--selection', str(config), '--threads', '2', '--device', 'cpu', '--limit-base', '16']
peak = [0]
for attempt in range(2):
    proc = subprocess.Popen(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
    finished = threading.Event()
    def sample():
        while not finished.wait(.1):
            info = MemoryInfo(); info.cb = ctypes.sizeof(info)
            if ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(proc._handle), ctypes.byref(info), info.cb):
                peak[0] = max(peak[0], info.peak_rss)
    monitor = threading.Thread(target=sample, daemon=True); monitor.start()
    with (out/f'日志{attempt+1}.txt').open('w', encoding='utf-8') as log:
        for line in proc.stdout:
            log.write(line); print(line.rstrip(), flush=True)
    rc = proc.wait(); finished.set(); monitor.join()
    assert rc == 0, rc
    current = (out/'结果'/'全部回测结果.csv').read_bytes()
    if attempt == 0: first = current
    else: assert current == first, '续跑改写既有结果'
with (out/'结果'/'全部回测结果.csv').open(encoding='utf-8-sig', newline='') as handle:
    rows = list(csv.DictReader(handle))
assert len(rows) == 16
assert any(int(row['交易次数（单）']) > 0 for row in rows)
print(f'PASS 完整行情+用户原始百万级选择，实际计算16组；工作进程峰值RSS {peak[0]/1024**3:.2f} GiB；续跑CSV不变')
