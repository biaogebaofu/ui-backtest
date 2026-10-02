"""Stable two-digit suffix per result directory, shared by all UI exports."""
import json
import re
import time
from pathlib import Path
from run_safety import atomic_json, exclusive_output


def distinguished_workbook_path(output, project_dir):
    output = Path(output)
    if not re.fullmatch(r'各类止盈(?:最优前\d+名|最差\d+名)\.xlsx', output.name):
        return output
    registry = Path(project_dir) / '导出区分码记录'
    key = str(output.parent.resolve()).casefold()
    deadline = time.monotonic() + 5
    while True:
        try:
            with exclusive_output(registry):
                path = registry / '目录编号.json'
                state = json.loads(path.read_text('utf-8')) if path.exists() else {'next': 1, 'directories': {}}
                codes = state['directories']
                if key not in codes:
                    codes[key] = f"{state['next'] % 100:02d}"
                    state['next'] += 1
                    atomic_json(path, state)
                code = codes[key]
            return output.with_name(f'{output.stem}_{code}{output.suffix}')
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)
