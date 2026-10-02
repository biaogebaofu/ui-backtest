"""固定比例退出多选器。

v1.41：支持 0.01%~0.09% 细桶，同时保留旧 0.1%~1.0% 代码；
工作日/周末独立矩阵使用滚动画布，避免 19×19 矩阵撑爆窗口。
"""
import tkinter as tk
from tkinter import ttk, messagebox
from strategy_space import (比例显示, 百分数转比例, 固定比例代码, 补全固定比例档)


def _fmt(rate: float) -> str:
    return 比例显示(rate)


class FixedRatePicker(ttk.Frame):
    def __init__(self, parent, rows, read, write):
        super().__init__(parent)
        self.rows = list(rows)
        self.prefix = next(row[0][:3] for row in self.rows if row[0] != "OFF")
        self.read, self.write = read, write
        self.variables = {}
        self.same_rows = [r for r in self.rows if r[0] != "OFF" and abs(r[1] - r[2]) < 1e-12]
        self.matrix_rows = [r for r in self.rows if r[0] != "OFF"]

        compact = ttk.Frame(self)
        compact.grid(row=0, column=0, sticky="w")
        off = tk.BooleanVar(value="OFF" in read())
        self.variables["OFF"] = off
        ttk.Checkbutton(compact, text="不使用", variable=off, command=self.commit).grid(
            row=0, column=0, padx=3, sticky="w")
        for i, row in enumerate(self.same_rows, start=1):
            code, wd, _we, _label = row
            var = tk.BooleanVar(value=code in read())
            self.variables[code] = var
            col = (i - 1) % 10 + 1
            rr = (i - 1) // 10
            ttk.Checkbutton(compact, text=_fmt(wd), variable=var, command=self.commit).grid(
                row=rr, column=col, padx=3, sticky="w")

        buttons = ttk.Frame(self)
        buttons.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="周末独立：展开全部矩阵", command=self.matrix).pack(side="left")
        ttk.Button(buttons, text="只选不使用", command=lambda: self.set({"OFF"})).pack(side="left", padx=5)
        ttk.Button(buttons, text="全选同档（含细桶）",
                   command=lambda: self.set({"OFF", *[r[0] for r in self.same_rows]})).pack(side="left", padx=5)
        ttk.Button(buttons, text="仅选细桶0.01%~0.09%", command=self._select_micro).pack(side="left", padx=5)

        manual = ttk.Frame(self)
        manual.grid(row=2, column=0, sticky="w", pady=(8, 4))
        self.weekday_var = tk.StringVar(value="0.4")
        self.weekend_var = tk.StringVar(value="跟随工作日")
        ttk.Label(manual, text="手动填写：工作日（%）").pack(side="left")
        ttk.Entry(manual, textvariable=self.weekday_var, width=13).pack(side="left", padx=(4, 12))
        ttk.Label(manual, text="周末（%）").pack(side="left")
        ttk.Combobox(manual, textvariable=self.weekend_var, width=15,
                     values=["跟随工作日"], state="normal").pack(side="left", padx=(4, 10))
        ttk.Button(manual, text="加入回测", command=self._add_manual).pack(side="left")
        ttk.Label(self, text="可连续加入多组；每组独立回测。例：0.123456 表示 0.123456%，填写后点击加入。",
                  foreground="#7F6000").grid(row=3, column=0, sticky="w", pady=(0, 4))
        chosen_box = ttk.Frame(self)
        chosen_box.grid(row=4, column=0, sticky="ew")
        self.chosen_tree = ttk.Treeview(chosen_box, columns=("rates",), show="headings",
                                       height=3, selectmode="extended")
        self.chosen_tree.heading("rates", text="已选比例（可多选删除；随配置保存和还原）")
        self.chosen_tree.column("rates", width=520, minwidth=250, anchor="w")
        ybar = ttk.Scrollbar(chosen_box, orient="vertical", command=self.chosen_tree.yview)
        self.chosen_tree.configure(yscrollcommand=ybar.set)
        self.chosen_tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="left", fill="y")
        ttk.Button(chosen_box, text="删除所选比例", command=self._remove_selected).pack(side="left", padx=6)
        self.sync()

    def _add_manual(self):
        try:
            weekday = 百分数转比例(self.weekday_var.get())
            raw = self.weekend_var.get().strip()
            weekend = weekday if raw == "跟随工作日" else 百分数转比例(raw)
            code = 固定比例代码(self.prefix, weekday, weekend)
        except ValueError as exc:
            messagebox.showerror("固定比例输入有误", str(exc), parent=self.winfo_toplevel())
            return
        self.set(set(self.read()) | {code})
        self.chosen_tree.selection_set(code)
        self.chosen_tree.see(code)

    def _remove_selected(self):
        self.set(set(self.read()) - set(self.chosen_tree.selection()))

    def _select_micro(self):
        chosen = {r[0] for r in self.same_rows if r[1] < 0.001}
        self.set(chosen or {"OFF"})

    def sync(self):
        selected = set(self.read())
        for code, var in self.variables.items():
            var.set(code in selected)
        if hasattr(self, "chosen_tree"):
            previous = set(self.chosen_tree.selection())
            self.chosen_tree.delete(*self.chosen_tree.get_children())
            for code, _wd, _we, label in 补全固定比例档(selected, self.prefix):
                self.chosen_tree.insert("", "end", iid=code, values=(label,))
            self.chosen_tree.selection_set(sorted(previous & selected))

    def set(self, chosen):
        self.write(set(chosen) or {"OFF"})
        self.sync()

    def commit(self):
        chosen = set(self.read()).difference(self.variables)
        chosen.update(code for code, var in self.variables.items() if var.get())
        self.set(chosen)

    def matrix(self):
        previous = getattr(self, "_matrix_dialog", None)
        if previous is not None and previous.winfo_exists():
            previous.lift(); previous.focus_set()
            return
        dialog = tk.Toplevel(self)
        self._matrix_dialog = dialog
        dialog.title("固定比例多选：列为工作日，行为周末")
        dialog.transient(self.winfo_toplevel())
        from platform_support import fit_window
        fit_window(dialog, 1100, 760)
        dialog.grab_set()
        # Reserve the footer first: resizing must never hide Apply/Cancel.
        buttons = ttk.Frame(dialog, padding=8)
        buttons.pack(side="bottom", fill="x")

        outer = ttk.Frame(dialog, padding=8)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="每个勾选格子是一种独立回测方案；不是同一笔交易叠加多个比例。",
                  foreground="#7F6000").pack(anchor="w", pady=(0, 6))

        canvas = tk.Canvas(outer, highlightthickness=0)
        ybar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        xbar = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        ybar.pack(side="right", fill="y")
        xbar.pack(side="bottom", fill="x")
        canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(canvas, padding=6)
        win = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))

        rates = sorted({r[1] for r in self.matrix_rows} | {r[2] for r in self.matrix_rows})
        lookup = {(r[1], r[2]): r[0] for r in self.matrix_rows}
        ttk.Label(frame, text="周末 ↓ / 工作日 →").grid(row=0, column=0, sticky="w")
        for i, rate in enumerate(rates, start=1):
            ttk.Label(frame, text=_fmt(rate)).grid(row=0, column=i, padx=5)
            ttk.Label(frame, text=_fmt(rate)).grid(row=i, column=0, padx=5)

        values = {}
        selected = set(self.read())
        for r_i, we in enumerate(rates, start=1):
            for c_i, wd in enumerate(rates, start=1):
                code = lookup[(wd, we)]
                var = tk.BooleanVar(value=code in selected)
                values[code] = var
                ttk.Checkbutton(frame, variable=var).grid(row=r_i, column=c_i, padx=1, pady=1)

        ttk.Button(buttons, text="全选矩阵",
                   command=lambda: [v.set(True) for v in values.values()]).pack(side="left")
        ttk.Button(buttons, text="清空矩阵",
                   command=lambda: [v.set(False) for v in values.values()]).pack(side="left", padx=6)
        ttk.Button(buttons, text="只选同档",
                   command=lambda: [v.set(code in {r[0] for r in self.same_rows}) for code, v in values.items()]).pack(side="left", padx=6)

        def apply():
            # 矩阵只编辑预设比例，不抹掉在手动输入中加入的自定义档。
            chosen = set(self.read()).difference(values)
            chosen.update(c for c, v in values.items() if v.get())
            if "OFF" in self.read():
                chosen.add("OFF")
            self.set(chosen)
            dialog.destroy()

        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="应用", command=apply).pack(side="right", padx=6)
