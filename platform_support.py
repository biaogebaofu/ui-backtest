"""Small desktop helpers; settings never depend on the source checkout."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


def user_data_dir() -> Path:
    if os.environ.get("UI_BACKTEST_HOME"):
        return Path(os.environ["UI_BACKTEST_HOME"]).expanduser().resolve() / "settings"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        if not base.is_absolute():
            base = Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not base.is_absolute():
            base = Path.home() / ".config"
    return base / "ETHBacktest"


def default_output_dir() -> Path:
    if os.environ.get("UI_BACKTEST_HOME"):
        return Path(os.environ["UI_BACKTEST_HOME"]).expanduser().resolve() / "results"
    return Path.home() / "ETHBacktest" / "results"


def ui_font_family(widget) -> str:
    import tkinter.font as tkfont
    if "Microsoft YaHei UI" in tkfont.families(root=widget):
        return "Microsoft YaHei UI"
    return tkfont.nametofont("TkDefaultFont", root=widget).actual("family")


def fit_window(window, width, height, *, minimum=None):
    # Leave room for window decorations, the taskbar and the macOS dock.
    available_width = max(1, window.winfo_screenwidth() - 80)
    available_height = max(1, window.winfo_screenheight() - 80)
    window.geometry(f"{min(width, available_width)}x{min(height, available_height)}")
    if minimum is not None:
        window.minsize(min(minimum[0], available_width), min(minimum[1], available_height))


def wheel_units(event) -> int:
    button = getattr(event, "num", None)
    if button in (4, 5):
        return -3 if button == 4 else 3
    delta = getattr(event, "delta", 0)
    if not delta:
        return 0
    return (-1 if delta > 0 else 1) * max(1, abs(int(delta)) // 120) * 3


def macos_available_memory() -> int:
    """Estimate free/reclaimable memory without counting compressed or wired pages."""
    try:
        output = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True,
                                check=True, timeout=5, env={**os.environ, "LC_ALL": "C"}).stdout
        page_size = re.search(r"page size of (\d+) bytes", output)
        counts = {name: int(value) for name, value in
                  re.findall(r"^Pages (free|inactive|speculative):\s+(\d+)\.", output, re.MULTILINE)}
        if page_size is None or "free" not in counts or "inactive" not in counts:
            raise ValueError("vm_stat 缺少内存统计")
        size = int(page_size.group(1))
        if size <= 0:
            raise ValueError("vm_stat 页大小无效")
        return size * sum(counts.values())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError("无法读取 macOS 可用内存，不能安全启动任务") from exc
