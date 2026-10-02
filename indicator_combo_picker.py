"""各回测位置共用的指标组合设置窗口。"""
from __future__ import annotations

import math
import re
import tkinter as tk
from tkinter import ttk, messagebox


def parse_sizes(checked, extra):
    sizes = set(checked)
    for word in re.split(r"[,，、\s]+", extra.strip()):
        if not word:
            continue
        if not word.isdecimal() or int(word) < 2:
            raise ValueError("组合数量请填写不小于 2 的整数，用逗号分隔。")
        sizes.add(int(word))
    return sorted(sizes)


class IndicatorComboPicker(tk.Toplevel):
    def __init__(self, parent, title, spec, pool, excluded, on_apply):
        super().__init__(parent)
        self.title(title + " · 指标组合")
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.minsize(540, 420)
        self.pool = pool
        self.excluded = set(excluded)
        self.on_apply = on_apply
        self.logic = spec.get("逻辑", "AND")
        self.enabled = tk.BooleanVar(self, value=spec.get("启用", False))
        self.singles = tk.BooleanVar(self, value=spec.get("保留单项", True))
        sizes = spec.get("组合数量", [2])
        self.size_vars = {n: tk.BooleanVar(self, value=n in sizes) for n in range(2, 7)}
        self.extra = tk.StringVar(self, value=",".join(str(n) for n in sizes if n > 6))
        self.preview = tk.StringVar(self)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Checkbutton(body, text="启用指标组合", variable=self.enabled).pack(anchor="w")
        ttk.Label(body, text="从此处已勾选的规则中，自动列出所选数量的所有组合。",
                  wraplength=520).pack(anchor="w", pady=(8, 4))
        mode = "所有条件同时满足" if self.logic == "AND" else "任一条件触发就退出"
        ttk.Label(body, text="合并方式：" + mode, foreground="#1F4E78").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=10)
        for n, var in self.size_vars.items():
            ttk.Checkbutton(row, text=f"{n}项组合", variable=var).pack(side="left", padx=(0, 10))
        custom = ttk.Frame(body)
        custom.pack(fill="x")
        ttk.Label(custom, text="更多数量：").pack(side="left")
        ttk.Entry(custom, textvariable=self.extra, width=24).pack(side="left")
        ttk.Label(custom, text="例如 7,8,10").pack(side="left", padx=8)
        ttk.Checkbutton(body, text="同时保留单项回测作对照", variable=self.singles).pack(
            anchor="w", pady=(10, 6))
        ttk.Label(body, textvariable=self.preview, wraplength=520, foreground="#C00000").pack(
            anchor="w", pady=(0, 8))
        ttk.Label(body, text="组合池（此处全部已勾选项，包含筛选后隐藏的项）：").pack(anchor="w")
        table = ttk.Frame(body)
        table.pack(fill="both", expand=True, pady=(4, 6))
        listing = tk.Listbox(table, height=7, width=70)
        scroll = ttk.Scrollbar(table, orient="vertical", command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        listing.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for code, label in pool:
            suffix = "（仅单独回测）" if code in self.excluded else ""
            listing.insert("end", f"{code} · {label}{suffix}")
        ttk.Label(body, text="“不启用”不参与合并；分批止盈保留为独立方案。每一项按一条完整规则／方案计。",
                  wraplength=520, foreground="#666666").pack(anchor="w")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="应用", command=self.apply).pack(side="right")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right", padx=8)
        for var in (self.enabled, self.singles, self.extra, *self.size_vars.values()):
            var.trace_add("write", lambda *_: self.refresh())
        self.bind("<Escape>", lambda _: self.destroy())
        self.refresh()

    def get_spec(self):
        return {"启用": bool(self.enabled.get()), "组合数量": parse_sizes(
            [n for n, var in self.size_vars.items() if var.get()], self.extra.get()),
            "保留单项": bool(self.singles.get()), "逻辑": self.logic}

    def validate(self, spec):
        n = sum(code not in self.excluded for code, _ in self.pool)
        if spec["启用"]:
            if not spec["组合数量"]:
                raise ValueError("至少选择一种组合数量。")
            if any(size > n for size in spec["组合数量"]):
                raise ValueError(f"可合并的规则只有 {n} 项，请先勾选更多规则或减小组合数量。")
        return n

    def refresh(self):
        try:
            spec = self.get_spec()
            n = self.validate(spec)
            if not spec["启用"]:
                text = f"未启用：按原方式分别回测 {len(self.pool):,} 项。"
            else:
                count = sum(math.comb(n, k) for k in spec["组合数量"] if k <= n)
                singles = len(self.pool) if spec["保留单项"] else 0
                text = f"可合并 {n:,} 项 → {count:,} 个组合 + {singles:,} 个单项 = {count + singles:,} 个方案。"
            self.preview.set(text)
        except ValueError as exc:
            self.preview.set(str(exc))

    def apply(self):
        try:
            spec = self.get_spec()
            self.validate(spec)
            self.on_apply(spec)
        except ValueError as exc:
            messagebox.showerror("指标组合设置", str(exc), parent=self)
            return
        self.destroy()
