"""第二轮工作台：可运行专项组与研究清单分页；未实现假设只展示，不传给引擎。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import 新增假设 as H
from second_round import test_groups, group_config
from selection_config import 全选配置, 规范化配置, 配置统计


实现色 = "#0F7B0F"
未实现色 = "#888888"
批次色 = "#1F4E78"


class 新增假设面板(ttk.Frame):
    def __init__(self, parent, on_apply=None):
        super().__init__(parent, padding=12)
        self.on_apply = on_apply
        pages = ttk.Notebook(self)
        pages.pack(fill="both", expand=True)
        run = ttk.Frame(pages, padding=8)
        catalog = ttk.Frame(pages, padding=8)
        pages.add(run, text="第二轮专项测试（可运行）")
        pages.add(catalog, text="扩展研究库（区分实现状态）")
        self._build_runner(run)
        self._build(catalog)
        self.refresh()

    def _build_runner(self, parent):
        ttk.Label(parent, text="以1370 / 2x为对照，只应用选中测试组，不自动开始回测。",
                  font=("Microsoft YaHei UI", 12, "bold"), foreground=批次色).pack(anchor="w")
        ttk.Label(parent, text="固定/叠加止盈止损各1%，移动止盈1358，等待0，强制30分钟；1000 USDC、0.01—20 ETH。\n"
                  "保留当前数据路径、S3门槛和账户保护设置（该表未列这些参数）。此页是专项对照，不代表所有组都是新算法。",
                  wraplength=1080).pack(anchor="w", pady=8)
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        self.group_stage = tk.StringVar(value="全部")
        stage = ttk.Combobox(bar, textvariable=self.group_stage, state="readonly",
                             values=("全部", "对照", "开仓", "止损", "止盈", "等待/仓位"), width=14)
        stage.pack(side="left")
        stage.bind("<<ComboboxSelected>>", lambda _e: self._refresh_groups())
        self.group_search = tk.StringVar()
        ttk.Label(bar, text="搜索：").pack(side="left", padx=(12, 0))
        ttk.Entry(bar, textvariable=self.group_search, width=32).pack(side="left")
        self.group_search.trace_add("write", lambda *_: self._refresh_groups())
        self.groups = {row[0]: row for row in test_groups()}
        defaults = 全选配置()
        self.group_counts = {key: 配置统计(规范化配置(group_config(defaults, row)))["包含仓位完整组合数"]
                             for key, row in self.groups.items()}
        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, pady=8)
        self.group_tree = ttk.Treeview(body, columns=("环节", "测试组", "组合数"), show="headings", selectmode="browse")
        for name, width in (("环节", 90), ("测试组", 540), ("组合数", 100)):
            self.group_tree.heading(name, text=name)
            self.group_tree.column(name, width=width)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.group_tree.yview)
        self.group_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.group_tree.pack(fill="both", expand=True)
        self.group_detail = tk.StringVar(value="选中一组查看具体改动。")
        self.group_tree.bind("<<TreeviewSelect>>", self._group_detail)
        ttk.Label(parent, textvariable=self.group_detail, wraplength=1080, justify="left").pack(fill="x", pady=8)
        ttk.Button(parent, text="应用选中组到组合选择（需确认）", command=self._apply_group,
                   state="normal" if self.on_apply else "disabled").pack(anchor="w")
        ttk.Label(parent, text="建议先跑BASE复核，再逐组比较；已用来挑选的历史数据不再算样本外。所有方案默认不增加手续费，保留成交偏移。",
                  foreground="#C00000", wraplength=1080).pack(anchor="w", pady=8)
        self._refresh_groups()

    def _refresh_groups(self):
        self.group_tree.delete(*self.group_tree.get_children())
        for key, row in self.groups.items():
            if self.group_stage.get() not in ("全部", row[1]):
                continue
            if self.group_search.get().strip().lower() not in (row[0] + row[2] + row[4]).lower():
                continue
            self.group_tree.insert("", "end", iid=key, values=(row[1], row[2], self.group_counts[key]))

    def _group_detail(self, _event=None):
        selected = self.group_tree.selection()
        if selected:
            row = self.groups[selected[0]]
            self.group_detail.set(f"{row[0]}：{row[4]}\n组合数：{self.group_counts[row[0]]}；其余组合字段固定在用户第一行。")

    def _apply_group(self):
        selected = self.group_tree.selection()
        if selected and self.on_apply:
            self.on_apply(self.groups[selected[0]])

    # ------------------------------------------------------------- 构建
    def _build(self, container):
        head = ttk.Frame(container)
        head.pack(fill="x")
        ttk.Label(head, text="新增假设清单", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        stat = H.统计()
        ttk.Label(
            head,
            text=f"共 {stat['总数']} 条　已实现 {stat['已实现']} 条　未实现 {stat['未实现']} 条",
            foreground=批次色, font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left", padx=(14, 0))

        ttk.Label(
            container, foreground="#C00000", wraplength=1180, justify="left",
            text="旧研究库 + R2第二轮扩展。灰色＝待实现，不能运行；绿色＝已接入组合选择。"
                 "这里的假设与旧说明中的样本数值并非本轮核验结论，不能当作更优策略承诺。",
        ).pack(anchor="w", pady=(6, 8))

        bar = ttk.LabelFrame(container, text="筛选（搜R2只看本次新增）", padding=8)
        bar.pack(fill="x")
        self.环节_var = tk.StringVar(value="全部环节")
        self.批次_var = tk.StringVar(value="全部改动量")
        self.状态_var = tk.StringVar(value="全部状态")
        self.关键词_var = tk.StringVar()
        items = (
            ("环节", self.环节_var, ["全部环节", *H.环节列表], 12),
            ("实现改动量（＝实现批次）", self.批次_var, ["全部改动量", *H.改动量列表], 24),
            ("实现状态", self.状态_var, ["全部状态", "仅已实现", "仅未实现"], 12),
        )
        for col, (label, var, values, width) in enumerate(items):
            f = ttk.Frame(bar)
            f.grid(row=0, column=col, sticky="w", padx=(0, 12))
            ttk.Label(f, text=label).pack(anchor="w")
            box = ttk.Combobox(f, textvariable=var, state="readonly", values=values, width=width)
            box.pack(anchor="w", pady=(2, 0))
            box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        f = ttk.Frame(bar)
        f.grid(row=0, column=3, sticky="ew")
        ttk.Label(f, text="编号 / 名称 / 判据关键词").pack(anchor="w")
        ttk.Entry(f, textvariable=self.关键词_var).pack(fill="x", pady=(2, 0))
        bar.columnconfigure(3, weight=1)
        self.关键词_var.trace_add("write", lambda *_: self.refresh())

        批次栏 = ttk.Frame(container)
        批次栏.pack(fill="x", pady=(8, 4))
        ttk.Label(批次栏, text="按改动量查看（不代表实现顺序）：", foreground=批次色).grid(row=0, column=0, sticky="w")
        for i, level in enumerate(H.改动量列表, 1):
            n = H.统计()["按改动量"].get(level, 0)
            ttk.Button(批次栏, text=f"第{i}批 {level}（{n}）",
                       command=lambda lv=level: self._选批次(lv)).grid(row=1+(i-1)//3, column=(i-1)%3, sticky="ew", padx=3, pady=2)
        ttk.Button(批次栏, text="全部", command=lambda: self._选批次("全部改动量")).grid(row=0, column=2, sticky="e")

        body = ttk.Frame(container)
        body.pack(fill="both", expand=True)
        cols = ("编号", "环节", "族", "名称", "参数网格", "状态", "改动量")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", height=8, selectmode="browse")
        for name, width, anchor, stretch in (
            ("编号", 64, "center", False), ("环节", 54, "center", False),
            ("族", 132, "w", False), ("名称", 216, "w", False),
            ("参数网格", 300, "w", True), ("状态", 70, "center", False),
            ("改动量", 168, "w", False),
        ):
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, minwidth=44, anchor=anchor, stretch=stretch)
        ys = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.tag_configure("done", foreground=实现色)
        self.tree.tag_configure("todo", foreground=未实现色)
        self.tree.bind("<<TreeviewSelect>>", self._显示详情)

        detail = ttk.LabelFrame(container, text="判据与想验证的假设（选中一行查看）", padding=8)
        self.detail_visible = tk.BooleanVar(value=True)
        def toggle_detail():
            if self.detail_visible.get():
                detail.pack(fill="x", pady=(8, 0), before=self.count_label)
            else:
                detail.pack_forget()
        ttk.Checkbutton(container, text="展开规则详情（可折叠）", variable=self.detail_visible,
                        command=toggle_detail).pack(anchor="w", pady=(6, 0))
        detail.pack(fill="x", pady=(8, 0))
        self.详情 = tk.Text(detail, height=5, wrap="word", state="disabled",
                            font=("Microsoft YaHei UI", 9))
        self.详情.pack(fill="both", expand=True)

        self.计数_var = tk.StringVar()
        self.count_label = ttk.Label(container, textvariable=self.计数_var, foreground=批次色)
        self.count_label.pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------- 行为
    def _选批次(self, level):
        self.批次_var.set(level)
        self.refresh()

    def _可见行(self):
        环节 = self.环节_var.get()
        批次 = self.批次_var.get()
        状态 = self.状态_var.get()
        kw = self.关键词_var.get().strip().lower()
        out = []
        for row in H.假设表:
            链, 族, 编号, 名称, 判据, 参数, 假设, 改动量 = row
            if 环节 != "全部环节" and 链 != 环节:
                continue
            if 批次 != "全部改动量" and 改动量 != 批次:
                continue
            done = H.是否已实现(编号)
            if 状态 == "仅已实现" and not done:
                continue
            if 状态 == "仅未实现" and done:
                continue
            if kw and kw not in f"{编号} {族} {名称} {判据} {参数} {假设}".lower():
                continue
            out.append(row)
        return out

    def refresh(self):
        rows = self._可见行()
        self.tree.delete(*self.tree.get_children())
        for 链, 族, 编号, 名称, 判据, 参数, 假设, 改动量 in rows:
            done = H.是否已实现(编号)
            self.tree.insert(
                "", "end", iid="h_" + 编号,
                tags=("done",) if done else ("todo",),
                values=(编号, 链, 族, 名称,
                        参数.replace("\n", "　"),
                        "已实现" if done else "未实现", 改动量),
            )
        done_n = sum(1 for r in rows if H.是否已实现(r[2]))
        self.计数_var.set(
            f"匹配 {len(rows)} 条｜其中已实现 {done_n} 条、未实现 {len(rows) - done_n} 条"
        )

    def _显示详情(self, _event=None):
        sel = self.tree.selection()
        self.详情.configure(state="normal")
        self.详情.delete("1.0", "end")
        if sel:
            row = H.条目(sel[0][2:])
            if row:
                链, 族, 编号, 名称, 判据, 参数, 假设, 改动量 = row
                状态 = "已实现，可在「组合选择」页勾选" if H.是否已实现(编号) else \
                    f"未实现——{改动量}；实现后本行变绿，并出现在组合选择页"
                self.详情.insert("end", f"{编号}　{名称}　（{链} · {族}）\n")
                self.详情.insert("end", f"状态：{状态}\n\n")
                self.详情.insert("end", f"判据：{判据}\n\n")
                self.详情.insert("end", f"参数网格：{参数}\n\n")
                self.详情.insert("end", f"想验证的假设：{假设}\n")
        self.详情.configure(state="disabled")
