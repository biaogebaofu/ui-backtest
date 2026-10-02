from __future__ import annotations
from extended_rules import FOURTH_ROUND_CODES, RESEARCH_CASE_CODES, FIFTH_ROUND_CODES

import copy
import json
import math
import traceback
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from selection_config import 成交价格口径选项
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import 轮次基线 as 基线
from selection_config import (S3基线周期选项, 入场触发口径选项 as 原入场触发口径选项, 默认入场触发口径,
                              入场触发显示代码 as 旧入场显示代码,
                              排行指标选项, 默认排行指标,
                              全选配置, 配置签名, 配置统计,
                              规范化候选筛选, 规范化配置)
from selection_panel import 组合选择面板
from fifth_policy import require_selection_allowed
from selection_availability import prepare_editor_selection
from strategy_space import 仓位列表
from data_sources import inspect_sources, CAPABILITY_LABELS
from run_safety import atomic_json
from ranking_limits import TOP_LIMITS, top_limit
from scrollable_form import ScrollableForm
from platform_support import default_output_dir, fit_window, ui_font_family, user_data_dir
from hedge_panel import HedgePanel
from extended_rules import (ENTRY_RULES, THIRD_ROUND_CODES, unavailable_entry_codes, capabilities_for_timeframe)
from execution_settings import (COST_MODE_LABELS, DEFAULT_COST_MODE, DEFAULT_FEES,
                                DEFAULT_FEE_PRESET, FEE_PRESETS, DEFAULT_SLIPPAGE,
                                DEFAULT_SLIPPAGE_PRESET, LEGACY_DEFAULT_SLIPPAGE,
                                DEFAULT_MIN_REENTRY_MINUTES, effective_fee_rates,
                                normalize_execution_settings, normalize_cost_mode, cost_modes)


项目目录 = Path(__file__).resolve().parent
用户设置文件 = user_data_dir() / "用户设置.json"
# 标题栏版本号。改版本时只改这一处，窗口标题跟着变。
工具版本 = "v1.72 分层研究与无人值守回测"
入场触发口径选项 = {**原入场触发口径选项,
                  "LIVE_01": "每个1m指标连续升/降段一次（非零轴整轮/金叉死叉周期）"}
入场显示代码 = {**旧入场显示代码, **{label: code for labels in (原入场触发口径选项, 入场触发口径选项)
              for code, label in labels.items()}}
默认数据 = ""
默认输出 = str(default_output_dir())
# 全量CSV每行的字节数随所选仓位档数线性增长：每多勾一档，就要多写期末资金、
# 累计收益、回撤、爆仓、强平、成交次数、停机标记、期末可开量这些用分号连起来
# 的列，再加上“所选仓位实际交易统计”里的15个字段。
# 旧的写法是不管勾几档都按 60GB/43,209,288行 ≈ 1390 字节/行算，两头都不准：
# 只勾1档时高估约九成，勾满22档时低估约三成，磁盘提醒因此形同虚设。
# 实测同一套列的历史结果（41列版）：2档448、4档601、5档671、22档1978 字节/行，
# 拟合每档约77字节；当前表头比41列版多10列固定字段，按49列版1档718字节反推，
# 固定部分取750字节。仍是估算——真实长度取决于数值本身的位数。
CSV固定字节 = 750
CSV每仓位档字节 = 77


def 估算CSV字节(行数, 仓位档数):
    from decimal import Decimal
    return Decimal(int(行数)) * (CSV固定字节 + CSV每仓位档字节 * max(1, int(仓位档数)))
# 逐账户保护退出尚未移植到GPU；不可调用旧内核并误称为相同计算口径。
计算设备选项 = ("自动（CPU多线程）", "仅CPU")
设备参数 = {"自动（CPU多线程）": "auto", "仅CPU": "cpu"}
# 旧设置里存的是老名字，读回来要能对上，不然会掉回默认值。
旧设备名 = {"自动（速度优先）": "自动（CPU多线程）", "仅GPU（实验）": "自动（CPU多线程）",
           "仅GPU（实验，通常更慢）": "自动（CPU多线程）"}
日志最大行数 = 4000


def 推荐线程数() -> int:
    """默认并发数为系统响应留出余量，不代表所有配置的最快线程数。"""
    return max(1, (os.cpu_count() or 4) - 2)


def human_seconds(value: float | None) -> str:
    if value is None:
        return "估算中"
    value = max(0, int(value))
    days, rem = divmod(value, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days: return f"{days}天 {hours}小时 {minutes}分"
    if hours: return f"{hours}小时 {minutes}分"
    return f"{minutes}分 {seconds}秒"


def scan_progress_text(message):
    rows = message.get("rows", 0)
    total_rows = message.get("total_rows")
    completed = (f"已完成 {rows:,}/{total_rows:,} 行" if total_rows is not None
                 else f"累计完成 {rows:,} 行")
    return (f"止盈方案 {message.get('tp', 0):,}/{message.get('total_tp', 0):,}｜"
            f"{completed}｜预计剩余 {human_seconds(message.get('eta_seconds'))}")


def entry_precompute_text(message):
    state = message.get("state", "running")
    title = {"paused": "开仓信号预计算已暂停", "done": "开仓信号预计算完成"}.get(
        state, "开仓信号预计算")
    running = message.get("running", [])
    text = (f"{title}｜已完成 {message.get('completed', 0):,}/{message.get('total', 0):,} 种方法｜"
            f"运行 {len(running)}/{message.get('workers', 1)} 个任务｜"
            f"已用时 {human_seconds(message.get('elapsed_seconds', 0))}")
    if running:
        names = "、".join(str(name)[:40] for name in running[:3])
        text += "｜正在计算：" + names + (f" 等{len(running)}种" if len(running) > 3 else "")
    elif message.get("message"):
        text += "｜" + str(message["message"])[:120]
    return text


def _fingerprint_source_missing(match):
    source = match.get("source_dir")
    return not source or not Path(source).is_dir()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"ETH 永续本地组合回测工具 {工具版本}")
        fit_window(self, 1280, 850, minimum=(960, 600))
        self.ui_font_family = ui_font_family(self)
        self.proc: subprocess.Popen | None = None
        self.proc_kind = ""
        self.messages: queue.Queue = queue.Queue()
        self.paused = False
        self.current_output_dir: Path | None = None
        # 多数据源：主K线可直接带成交统计；也可外挂aggTrades/资金费/OI，或选择整包ZIP。
        self.csv_var = tk.StringVar(value=默认数据)
        self.micro_csv_var = tk.StringVar(value="")
        self.funding_var = tk.StringVar(value="")
        self.oi_var = tk.StringVar(value="")
        self.bundle_var = tk.StringVar(value="")
        self.start_date_var = tk.StringVar(value="")
        self.end_date_var = tk.StringVar(value="")
        self.entry_mode_var = tk.StringVar(value=入场触发口径选项[默认入场触发口径])
        self._entry_mode_code = 默认入场触发口径
        self.cost_mode_var = tk.StringVar(value=DEFAULT_COST_MODE)
        self.cost_mode_vars = {code: tk.BooleanVar(value=code == DEFAULT_COST_MODE) for code in COST_MODE_LABELS}
        self._cost_mode_codes = (DEFAULT_COST_MODE,)
        self.out_var = tk.StringVar(value=默认输出)
        self.data_report = None
        self._data_generation = 0
        self._active_job_id = None
        self._job_counter = 0
        self._active_output_dir = None
        self._closing = False
        self._poll_after_id = None
        self._close_after_id = None
        self._fingerprint_generation = 0
        self._fingerprint_busy = False
        self.fingerprint_queue = []
        self._fingerprint_queue_dialog = None
        self._fingerprint_queue_pending = []
        self._fingerprint_queue_merge_requested = False
        self.run_target_var = tk.StringVar(value="EDITOR")
        self.run_target_hint_var = tk.StringVar(value="当前运行对象：编辑组合（按下方勾选回测）")
        self._previous_run_target = "EDITOR"
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._style()
        self._build()
        self.entry_mode_var.trace_add("write", self._entry_mode_changed)
        self.load_user_settings()
        self._poll_after_id = self.after(100, self.poll_messages)

    def _style(self):
        style = ttk.Style(self)
        try: style.theme_use("vista")
        except tk.TclError: pass
        style.configure("Title.TLabel", font=(self.ui_font_family, 18, "bold"), foreground="#17365D")
        style.configure("Card.TLabelframe", background="#F7F9FC")
        style.configure("Card.TLabelframe.Label", font=(self.ui_font_family, 11, "bold"), foreground="#1F4E78")
        style.configure("Accent.TButton", font=(self.ui_font_family, 11, "bold"))
        # ★已测最优用红色，「新」新增未测用绿色。组合选择页里几百个勾选框，
        # 光靠前缀字符看不出来，必须连字色一起改。
        style.configure("Best.TCheckbutton", foreground="#C00000",
                        font=(self.ui_font_family, 9, "bold"))
        style.configure("New.TCheckbutton", foreground="#0F7B0F")
        style.configure("Round.TButton", font=(self.ui_font_family, 10, "bold"))
        style.configure("RoundOn.TButton", font=(self.ui_font_family, 10, "bold"),
                        foreground="#C00000")

    def _build(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="ETH 永续 · 多数据源全组合回测工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="K线 + 成交微结构 + 资金费 + OI｜数据能力硬校验｜逐杠杆账户重放｜CPU多线程",
                  font=(self.ui_font_family, 10)).pack(anchor="w", pady=(2, 12))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        data_tab = ttk.Frame(self.notebook, padding=12)
        select_tab = ttk.Frame(self.notebook)
        run_tab = ttk.Frame(self.notebook, padding=12)
        candidate_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(data_tab, text="  1. 数据源与能力  ")
        self.notebook.add(select_tab, text="  2. 组合选择  ")
        self.notebook.add(run_tab, text="  3. 运行与进度  ")
        self.notebook.add(candidate_tab, text="  4. 候选筛选与导出  ")
        self.data_tab, self.select_tab, self.run_tab, self.candidate_tab = data_tab, select_tab, run_tab, candidate_tab
        self._build_fingerprint_lookup(select_tab)
        # 组合面板先建，数据能力检测后可直接灰掉缺字段策略。
        self.selection_panel = 组合选择面板(select_tab, self.update_selection_summary,
                                     entry_mode_var=self.entry_mode_var, entry_mode_options=入场触发口径选项,
                                     cost_mode_vars=self.cost_mode_vars)
        self.selection_panel.pack(fill="both", expand=True)
        self._build_fingerprint_batch_view(select_tab)
        self.data_scroll = ScrollableForm(data_tab)
        self.data_scroll.pack(fill="both", expand=True)
        self._build_data_tab(self.data_scroll.body)
        self._build_run_tab(run_tab)
        self.candidate_scroll = ScrollableForm(candidate_tab)
        self.candidate_scroll.pack(fill="both", expand=True)
        self._build_candidate_tab(self.candidate_scroll.body)
        self.hedge_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.hedge_tab, text="  5. 双向持仓对照  ")
        self.hedge_panel = HedgePanel(self.hedge_tab, self, 项目目录)
        self.hedge_panel.pack(fill="both", expand=True)
        self.update_selection_summary(配置统计(全选配置()), "")
        self.refresh_round_state()
        self._sync_run_target()

    def _build_data_tab(self, outer):
        ttk.Label(outer, text="多数据源能力中心", font=(self.ui_font_family, 16, "bold"),
                  foreground="#17365D").pack(anchor="w")
        ttk.Label(
            outer,
            text="主K线负责价格与时间轴；成交微结构、资金费、OI可以来自同一份增强K线，也可以外挂独立文件。"
                 "缺字段的策略会被灰掉并在工作进程中硬校验，绝不再用0信号假装完成。",
            foreground="#7F6000", wraplength=1120, justify="left",
        ).pack(anchor="w", pady=(2, 10))

        src = ttk.LabelFrame(outer, text="数据源（显式文件优先于ZIP中同类文件）", style="Card.TLabelframe", padding=10)
        src.pack(fill="x")
        self._data_source_row(src, 0, "数据包ZIP（可选）", self.bundle_var, self.choose_bundle,
                              "可自动识别 klines_1m / agg_trades / funding_rates / open_interest")
        self._data_source_row(src, 1, "1m主K线", self.csv_var, self.choose_csv,
                              "CSV/Parquet；至少要 openTime/open/high/low/close/volume")
        self._data_source_row(src, 2, "成交微结构（可选）", self.micro_csv_var,
                              lambda: self.choose_source_file(self.micro_csv_var, "选择aggTrades或分钟微结构数据"),
                              "支持原始aggTrades，或按分钟聚合的 trades/taker/delta/avg_trade_size")
        self._data_source_row(src, 3, "资金费（可选）", self.funding_var,
                              lambda: self.choose_source_file(self.funding_var, "选择历史资金费率"),
                              "funding_rate + timestamp/funding_time；只向后对齐，不使用未来费率")
        self._data_source_row(src, 4, "Open Interest（可选）", self.oi_var,
                              lambda: self.choose_source_file(self.oi_var, "选择Open Interest历史数据"),
                              "open_interest/open_interest_value + timestamp；只向后对齐")

        action = ttk.Frame(src); action.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.inspect_data_btn = ttk.Button(action, text="检测数据与策略覆盖", command=self.inspect_data_sources)
        self.inspect_data_btn.pack(side="left")
        ttk.Button(action, text="按当前数据修剪策略", command=self.trim_to_data).pack(side="left", padx=8)
        ttk.Button(action, text="第三批全选当前可用", command=self.select_available_third).pack(side="left")
        ttk.Button(action, text="第四批全选当前可用", command=self.select_available_fourth).pack(side="left",padx=4)
        ttk.Button(action, text="第五轮全选当前可用", command=self.select_available_fifth).pack(side="left",padx=4)
        ttk.Button(action, text="去组合选择页", command=lambda: self.notebook.select(1)).pack(side="right")

        dates = ttk.Frame(src)
        dates.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(dates, text="开始日期/时间（UTC）").pack(side="left")
        ttk.Entry(dates, textvariable=self.start_date_var, width=24).pack(side="left", padx=(8, 16))
        ttk.Label(dates, text="结束日期/时间（不含）").pack(side="left")
        ttk.Entry(dates, textvariable=self.end_date_var, width=24).pack(side="left", padx=8)
        ttk.Label(src, text="留空使用数据全部区间；示例2026-01-01。结束时刻为右开边界，日期按UTC；也可输入带时区的时间。",
                  foreground="#7F6000").grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        overview = ttk.LabelFrame(outer, text="检测结果", style="Card.TLabelframe", padding=10)
        overview.pack(fill="both", expand=True, pady=(10, 0))
        self.data_summary_var = tk.StringVar(value="尚未检测。增强版Binance K线本身通常已经含 quoteVolume / trades / takerBuyBaseVol。")
        self.data_strategy_var = tk.StringVar(value="")
        ttk.Label(overview, textvariable=self.data_summary_var, font=(self.ui_font_family, 10, "bold"),
                  foreground="#1F4E78", wraplength=1120, justify="left").pack(anchor="w")
        ttk.Label(overview, textvariable=self.data_strategy_var, foreground="#7F6000",
                  wraplength=1120, justify="left").pack(anchor="w", pady=(4, 8))

        columns = ("能力", "状态", "覆盖率", "用途")
        self.data_tree = ttk.Treeview(overview, columns=columns, show="headings", height=11)
        for c, w in (("能力", 220), ("状态", 90), ("覆盖率", 100), ("用途", 680)):
            self.data_tree.heading(c, text=c); self.data_tree.column(c, width=w, anchor="w")
        self.data_tree.pack(fill="both", expand=True)
        self.data_tree.tag_configure("ok", background="#E8F5E9")
        self.data_tree.tag_configure("missing", background="#FCE4D6")

        self.data_run_summary_var = getattr(self, "data_run_summary_var", tk.StringVar(value="未检测"))
        for var in (self.csv_var, self.micro_csv_var, self.funding_var, self.oi_var, self.bundle_var,
                    self.start_date_var, self.end_date_var):
            var.trace_add("write", lambda *_: self._mark_data_dirty())
        self._mark_data_dirty()

    def _data_source_row(self, parent, row, label, variable, command, hint):
        ttk.Label(parent, text=label, width=19).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(parent, text="浏览…", command=command).grid(row=row, column=2, padx=(0, 8))
        ttk.Label(parent, text=hint, foreground="#888888", wraplength=460).grid(row=row, column=3, sticky="w")
        parent.columnconfigure(1, weight=1)

    def _build_fingerprint_lookup(self, parent):
        box = ttk.LabelFrame(parent, text="组合指纹：添加并同步勾选／可选逐条回测", padding=8)
        box.pack(fill="x", padx=12, pady=(10, 0))
        box.columnconfigure(1, weight=1)
        self.fingerprint_var = tk.StringVar()
        self.fingerprint_source_var = tk.StringVar()
        self.fingerprint_status_var = tk.StringVar(value="还原原策略、成本、账户参数、数据与日期；应用后只需点运行，结果另建新目录。")
        ttk.Label(box, text="组合指纹（表中策略指纹）").grid(row=0, column=0, sticky="w")
        self.fingerprint_entry = ttk.Entry(box, textvariable=self.fingerprint_var, width=32)
        self.fingerprint_entry.grid(row=0, column=1, sticky="ew", padx=8)
        self.fingerprint_lookup_button = ttk.Button(box, text="添加／同步", command=self.lookup_fingerprint)
        self.fingerprint_lookup_button.grid(row=0, column=2, padx=4)
        self.fingerprint_queue_button = ttk.Button(box, text="多指纹列表（0）", command=self.show_fingerprint_queue)
        self.fingerprint_queue_button.grid(row=0, column=3, padx=4)
        ttk.Label(box, text="来源目录（可选）").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(box, textvariable=self.fingerprint_source_var).grid(row=1, column=1, sticky="ew", padx=8, pady=(6, 0))
        ttk.Button(box, text="选择目录…", command=self.choose_fingerprint_source).grid(row=1, column=2, padx=4, pady=(6, 0))
        ttk.Label(box, text="多个指纹用逗号或空格隔开；同步兼容条件，可能增加交叉组合；保留当前运行对象，精确列表可手选。", foreground="#777777").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(box, textvariable=self.fingerprint_status_var, foreground="#1F4E78", wraplength=970).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))
        target = ttk.Frame(box)
        target.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(target, text="本次运行：").pack(side="left")
        for code, label in (("EDITOR", "编辑组合（勾选回测）"), ("FINGERPRINTS", "指纹精确列表（逐条独立）")):
            ttk.Radiobutton(target, text=label, variable=self.run_target_var, value=code,
                            command=self._sync_run_target).pack(side="left", padx=8)
        for var in (self.fingerprint_var, self.fingerprint_source_var, self.out_var):
            var.trace_add("write", lambda *_: self._invalidate_fingerprint_lookup())

    def _fingerprint_run_active(self):
        return bool((self.proc and self.proc.poll() is None) or self._active_job_id is not None)

    def _invalidate_fingerprint_lookup(self):
        self._fingerprint_generation += 1
        if self._fingerprint_busy:
            self._fingerprint_busy = False
            self.fingerprint_lookup_button.configure(state="normal")
            self.fingerprint_status_var.set("指纹或来源已变化，旧查找结果不会应用。")
            self._set_queue_lookup_state("normal")

    def choose_fingerprint_source(self):
        if self._fingerprint_run_active():
            messagebox.showinfo("正在运行", "回测或导出结束后再还原指纹。")
            return
        value = filedialog.askdirectory(title="选择原结果目录或其上级目录",
                                        initialdir=str(self.current_output_dir or self.out_var.get()))
        if value:
            self.fingerprint_source_var.set(value)

    def lookup_fingerprint(self):
        if self._fingerprint_run_active():
            messagebox.showinfo("正在运行", "回测或导出结束后再还原指纹。")
            return
        if self._fingerprint_busy:
            return
        fingerprint = self.fingerprint_var.get().strip()
        search_root = self.fingerprint_source_var.get().strip() or self.out_var.get().strip() or str(self.current_output_dir or "")
        if not fingerprint:
            messagebox.showerror("缺少组合指纹", "请粘贴表中的策略指纹。")
            return
        from fingerprint_lookup import parse_fingerprints
        try:
            codes = parse_fingerprints(fingerprint)
        except ValueError as exc:
            messagebox.showerror("组合指纹无效", str(exc))
            return
        self.lookup_fingerprint_queue(text=" ".join(codes), search_root=search_root)

    def _start_fingerprint_job(self, stage, payload, generation):
        def worker():
            try:
                from fingerprint_lookup import (find_fingerprint_matches, restore_fingerprint_match,
                                                find_fingerprint_groups, restore_many)
                if stage == "find":
                    result = find_fingerprint_matches(*payload)
                elif stage == "restore":
                    result = restore_fingerprint_match(payload)
                elif stage == "queue_find":
                    result = find_fingerprint_groups(*payload)
                else:
                    def progress(text):
                        self.messages.put({"type": "fingerprint_progress", "generation": generation, "message": text})
                    result = restore_many(payload, progress=progress)
                self.messages.put({"type": f"fingerprint_{stage}", "generation": generation, "result": result})
            except Exception as exc:
                self.messages.put({"type": "fingerprint_error", "generation": generation, "message": str(exc)})
        threading.Thread(target=worker, daemon=True).start()

    def _finish_fingerprint_lookup(self, text):
        self._fingerprint_busy = False
        self._fingerprint_queue_merge_requested = False
        self.fingerprint_lookup_button.configure(state="normal")
        self.fingerprint_status_var.set(text)
        self._set_queue_lookup_state("normal")

    def _choose_fingerprint_match(self, matches):
        dialog = tk.Toplevel(self)
        dialog.title("选择指纹的原结果来源")
        fit_window(dialog, 920, 360)
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="同一策略指纹可能对应不同日期或数据；请选择要还原的原结果来源。").pack(anchor="w")
        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True, pady=10)
        tree = ttk.Treeview(tree_frame, columns=("来源目录", "摘要"), show="headings", selectmode="browse")
        for column, width in (("来源目录", 580), ("摘要", 460)):
            tree.heading(column, text=column)
            tree.column(column, width=width, minwidth=180, stretch=False)
        vertical = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        for index, match in enumerate(matches):
            tree.insert("", "end", iid=str(index), values=(str(match["source_dir"]), str(match.get("summary", ""))))
        chosen = []
        def accept(_event=None):
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("选择来源", "请先选择一行来源目录。", parent=dialog)
                return
            chosen.append(matches[int(selected[0])])
            dialog.destroy()
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        ttk.Button(actions, text="应用所选来源", command=accept).pack(side="right")
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="right", padx=8)
        tree.bind("<Double-1>", accept)
        dialog.grab_set()
        self.wait_window(dialog)
        return chosen[0] if chosen else None

    def _handle_fingerprint_message(self, message):
        generation = message.get("generation")
        if generation != self._fingerprint_generation:
            return
        if self._fingerprint_run_active() or self._closing:
            self._finish_fingerprint_lookup("当前任务仍在运行，未应用指纹；结束后请重新查找。")
            return
        try:
            if message["type"] == "fingerprint_error":
                raise ValueError(message.get("message", "无法还原指纹"))
            if message["type"] == "fingerprint_progress":
                self.fingerprint_status_var.set(message["message"])
                return
            if message["type"] == "fingerprint_find":
                matches = message["result"]
                if not matches:
                    raise ValueError("未找到这个策略指纹，请核对指纹和来源目录。")
                match = matches[0] if len(matches) == 1 else self._choose_fingerprint_match(matches)
                if generation != self._fingerprint_generation:
                    return
                if match is None:
                    self._finish_fingerprint_lookup("已取消来源选择，当前参数保持不变。")
                    return
                if self._fingerprint_run_active():
                    self._finish_fingerprint_lookup("当前任务仍在运行，未应用指纹。")
                    return
                self.fingerprint_status_var.set("正在核验原数据指纹、日期和完整运行参数……")
                self._start_fingerprint_job("restore", match, generation)
            elif message["type"] == "fingerprint_restore":
                self.apply_fingerprint_restoration(message["result"])
                self._finish_fingerprint_lookup("已还原单一组合；请到运行页点击开始，本次将新建结果目录。")
            elif message["type"] == "fingerprint_queue_find":
                groups = message["result"]
                missing = [code for code, matches in groups.items() if not matches]
                if missing:
                    raise ValueError("以下指纹未找到，本次没有加入任何策略：" + "、".join(missing))
                # Keep deleted-source records in the list, but not in this addition's merge.
                # Explicitly requested matches still undergo the normal all-or-nothing verification.
                chosen = [item for item in self.fingerprint_queue if not _fingerprint_source_missing(item)]
                existing_by_key = {self._queue_key(item): item for item in chosen}
                chosen_keys = set(existing_by_key)
                for code, matches in groups.items():
                    match = matches[0] if len(matches) == 1 else self._choose_fingerprint_match(matches)
                    if generation != self._fingerprint_generation:
                        return
                    if match is None:
                        self._finish_fingerprint_lookup("已取消来源选择，策略列表保持不变。")
                        return
                    key = self._queue_key(match)
                    if key not in chosen_keys:
                        chosen.append(match)
                        chosen_keys.add(key)
                self._fingerprint_queue_pending = chosen
                self.fingerprint_status_var.set("正在核验所有指纹；全部通过后才加入列表……")
                self._start_fingerprint_job("queue_restore", chosen, generation)
            elif message["type"] == "fingerprint_queue_restore":
                from fingerprint_lookup import bind_fingerprint_match, merge_fingerprint_restorations
                restorations = message["result"]
                pending = self._fingerprint_queue_pending
                if len(restorations) != len(pending):
                    raise ValueError("核验结果数量不一致，请重新查找")
                for match, restored in zip(pending, restorations):
                    if self._queue_key(match) != self._queue_key(restored):
                        raise ValueError("核验结果来源不一致，请重新查找")
                pinned = [bind_fingerprint_match(match, restored) for match, restored in zip(pending, restorations)]
                updated = list(self.fingerprint_queue)
                indices = {self._queue_key(match): i for i, match in enumerate(updated)}
                for match in pinned:
                    key = self._queue_key(match)
                    if key in indices:
                        updated[indices[key]] = match
                    else:
                        indices[key] = len(updated)
                        updated.append(match)
                missing_sources = [item for item in updated if _fingerprint_source_missing(item)]
                missing_note = (f"另有{len(missing_sources)}条旧记录来源目录失效，已保留但未参与本次同步；"
                                "运行精确列表前请恢复来源或移除这些记录。") if missing_sources else ""
                merge_error = None
                merged = None
                hedge_projection = None
                target = "EDITOR" if self._fingerprint_queue_merge_requested else self.run_target_var.get()
                has_hedge = any(item.get("kind") == "hedge" for item in restorations)
                try:
                    if has_hedge:
                        from hedge_editor_sync import merge_hedge_restorations
                        hedge_projection = merge_hedge_restorations(restorations)
                    else:
                        merged = merge_fingerprint_restorations(restorations)
                except ValueError as exc:
                    merge_error = str(exc)
                if merged is not None:
                    self.apply_fingerprint_restoration(merged, allow_multiple=True)
                    self.run_target_var.set(target)
                elif hedge_projection is not None:
                    self.apply_hedge_fingerprint_projection(hedge_projection)
                    self.run_target_var.set(target)
                self.fingerprint_queue = updated
                self._refresh_fingerprint_queue()
                self.notebook.select(self.select_tab)
                for restored in restorations:
                    for warning in restored.get("warnings", []):
                        self.append_log(f"指纹 {restored['fingerprint']}：{warning}")
                if merged is not None:
                    self._finish_fingerprint_lookup(
                        f"已同步{merged['exact_count']}条指纹到编辑组合：{merged['projected_count']}组，"
                        f"比原有独立配置多{merged['extra_count']}组交叉组合。"
                        + ("未启动回测。" if missing_note else
                           f"如只跑原{merged['exact_count']}条，请手选“指纹精确列表”。未启动回测。")
                        + missing_note)
                elif hedge_projection is not None:
                    text = (f"已同步{len(restorations)}条双向指纹：开仓{hedge_projection['entry_count']}种组合已回填，"
                            f"双向持仓参数已回填第5页，共{hedge_projection['projected_count']}组条件方案"
                            f"（新增{hedge_projection['extra_count']}组交叉组合）＋{hedge_projection['baseline_count']}组随机基线。"
                            f"只跑原{len(restorations)}条请用“运行列表（独立回测）”；重新组合请到第5页开始。未启动回测。" + missing_note)
                    self._finish_fingerprint_lookup(text)
                    self.append_log(text)
                    self.hedge_panel.status_var.set(text)
                    self.hedge_panel.save_settings(force=True)
                    if not self.save_user_settings() or self.hedge_panel._settings_dirty:
                        self.fingerprint_status_var.set(text + "本次设置保存失败，请查看日志。")
                elif merge_error:
                    text = (f"已核验{len(restorations)}条，列表共{len(self.fingerprint_queue)}条，但不能合并编辑：{merge_error}。"
                            "编辑参数和运行对象保持不变；可手选“指纹精确列表”分别运行。" + missing_note)
                    self._finish_fingerprint_lookup(text)
                    self.append_log(text)
                    messagebox.showinfo("指纹不能合并勾选", text)
                if missing_note:
                    self.append_log(missing_note)
        except (ValueError, OSError, KeyError, TypeError, tk.TclError) as exc:
            self._finish_fingerprint_lookup("还原失败，当前参数保持不变。")
            messagebox.showerror("指纹未应用", str(exc))

    @staticmethod
    def _queue_key(match):
        return str(match["fingerprint"]).lower(), os.path.normcase(str(Path(match["source_dir"]).resolve()))

    @classmethod
    def _validated_fingerprint_queue(cls, values, *, removed=None):
        from fingerprint_lookup import parse_fingerprints, fingerprint_retirement_reason
        if not isinstance(values, list):
            raise ValueError("多指纹策略列表必须是列表")
        result, seen = [], set()
        for match in values:
            if not isinstance(match, dict) or not isinstance(match.get("row"), dict):
                raise ValueError("策略列表缺少原结果记录，请重新查找指纹")
            codes = parse_fingerprints(match.get("fingerprint", ""))
            if len(codes) != 1 or not isinstance(match.get("source_dir"), str) or not match["source_dir"]:
                raise ValueError("策略列表指纹或来源无效")
            item = dict(match, fingerprint=codes[0])
            reason = fingerprint_retirement_reason(item)
            if reason:
                if removed is None:
                    raise ValueError(reason)
                removed.append({"fingerprint": codes[0], "reason": reason})
                continue
            key = cls._queue_key(item)
            if key not in seen:
                result.append(item)
                seen.add(key)
        return result

    def _set_queue_lookup_state(self, state):
        if self._fingerprint_queue_dialog is not None:
            self.queue_lookup_button.configure(state=state)

    def _refresh_fingerprint_queue(self):
        self.fingerprint_queue_button.configure(text=f"多指纹列表（{len(self.fingerprint_queue)}）")
        trees = []
        if hasattr(self, "batch_tree"):
            trees.append(self.batch_tree)
        if self._fingerprint_queue_dialog is not None:
            trees.append(self.queue_tree)
        for tree in trees:
            tree.delete(*tree.get_children())
            for index, match in enumerate(self.fingerprint_queue):
                tree.insert("", "end", iid=str(index), values=self._queue_display_values(index, match))
        if self.fingerprint_queue and hasattr(self, "batch_tree"):
            self.batch_tree.selection_set("0")
        if hasattr(self, "batch_details"):
            self._show_batch_details()
        self._sync_run_target()

    @staticmethod
    def _queue_display_values(index, match):
        raw = match["row"]
        snapshot = match.get("verified_snapshot") or {}
        if match.get("kind") == "hedge":
            request = snapshot.get("origin_identity", {}).get("hedge_request", {})
            config = request.get("hedge", {})
            size = "0.5+0.5x/方向" if config.get("mode") == "scale_in" else ("1x/方向" if config else "待核验")
            state = "来源失效（禁运行）" if _fingerprint_source_missing(match) else ("已绑定双向参数" if config else "待重新核验")
            tp = f"{config['tp_weekday']*100:g}%/{config['tp_holiday']*100:g}%" if config else "待核验"
            cost = f"Maker {config['maker_fee_rate']*100:g}%" if config else "待核验"
            return (index + 1, match["fingerprint"], size, state, "双向｜" + raw.get("开仓策略", ""),
                    tp, str(raw.get("超时平仓（小时）", "")) + "h", cost, match["source_dir"])
        selection = snapshot.get("selection")
        if selection:
            size = "/".join(f"{float(n):g}x" for n in selection["仓位倍数"])
            mode = selection["入场触发口径"]
            tp = "/".join(str(n) for n in selection["止盈方案编号"])
            stop = "+".join(selection["止损代码"])
            cost = selection["成本模式"]
        else:
            size = f"{float(raw['名义倍数（倍）']):g}x" if raw.get("名义倍数（倍）") is not None else "待核验"
            mode, tp, stop, cost = (raw.get(key, "待核验") for key in
                                   ("入场触发口径", "止盈方案编号", "止损代码", "成本模式"))
        modes = {"LIVE_01": "升降段一次", "TF_EVENT": "收盘事件", "MACD_CYCLE": "每次换色一次",
                 "MACD_FULL_RED": "红→绿完整一轮", "MACD_FULL_GREEN": "绿→红完整一轮"}
        state = "来源失效（禁运行）" if _fingerprint_source_missing(match) else (match.get("definition_status", "已绑定参数") if selection else "待重新核验")
        return (index + 1, match["fingerprint"], size, state,
                modes.get(str(mode), str(mode)), tp, stop,
                {"SLIPPAGE": "成交偏移", "FEE": "手续费"}.get(cost, cost), match["source_dir"])

    def _new_queue_tree(self, parent, height=5):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        columns = (("序号", 35), ("指纹", 150), ("名义倍数", 70), ("核验状态", 130),
                   ("入场规则", 115), ("止盈", 95), ("止损", 125), ("成本", 90), ("原结果来源", 320))
        tree = ttk.Treeview(frame, columns=tuple(name for name, _ in columns), show="headings", selectmode="extended", height=height)
        for name, width in columns:
            tree.heading(name, text=name)
            tree.column(name, width=width, minwidth=30, stretch=False)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        return tree

    def _build_fingerprint_batch_view(self, parent):
        self.fingerprint_batch_view = ttk.Frame(parent, padding=12)
        ttk.Label(self.fingerprint_batch_view, textvariable=self.run_target_hint_var,
                  foreground="#C00000", wraplength=970).pack(fill="x", pady=(0, 6))
        actions = ttk.Frame(self.fingerprint_batch_view)
        actions.pack(fill="x", pady=(0, 6))
        ttk.Button(actions, text="运行此列表", command=self.start_fingerprint_batch).pack(side="left")
        ttk.Button(actions, text="重新核验列表", command=self.revalidate_fingerprint_queue).pack(side="left", padx=6)
        ttk.Button(actions, text="合并到编辑组合", command=lambda: self.revalidate_fingerprint_queue(apply_to_editor=True)).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="移除选中", command=lambda: self.remove_queued_fingerprints(tree=self.batch_tree)).pack(side="left")
        ttk.Label(actions, text="点击一行，下方查看它的完整参数；不混合成额外组合。", foreground="#777777").pack(side="left", padx=8)
        self.batch_tree = self._new_queue_tree(self.fingerprint_batch_view, height=4)
        detail_frame = ttk.LabelFrame(self.fingerprint_batch_view, text="选中策略的完整参数（只读，正式运行前再次核验）", padding=6)
        detail_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.batch_details = ttk.Treeview(detail_frame, columns=("参数", "值"), show="headings", height=5)
        self.batch_details.heading("参数", text="参数")
        self.batch_details.heading("值", text="原策略的实际设置")
        self.batch_details.column("参数", width=290, stretch=False)
        self.batch_details.column("值", width=820, stretch=False)
        vertical = ttk.Scrollbar(detail_frame, orient="vertical", command=self.batch_details.yview)
        horizontal = ttk.Scrollbar(detail_frame, orient="horizontal", command=self.batch_details.xview)
        self.batch_details.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.batch_details.pack(fill="both", expand=True)
        self.batch_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_batch_details())

    def _show_batch_details(self):
        self.batch_details.delete(*self.batch_details.get_children())
        selected = self.batch_tree.selection()
        if not selected:
            return
        match = self.fingerprint_queue[int(selected[0])]
        def add(name, value):
            if isinstance(value, dict):
                for key, child in value.items():
                    add(f"{name} / {key}" if name else key, child)
            else:
                if isinstance(value, (list, tuple)):
                    value = json.dumps(value, ensure_ascii=False)
                self.batch_details.insert("", "end", values=(name, value))
        add("指纹", match["fingerprint"])
        add("来源", match["source_dir"])
        if _fingerprint_source_missing(match):
            add("来源状态", "原结果目录已不存在；记录仍保留，不参与新增指纹的同步，恢复来源或移除前禁止运行精确列表。")
        snapshot = match.get("verified_snapshot")
        if not snapshot:
            add("状态", "旧列表仅有原结果行，请点“重新核验列表”后再运行；不会猜测隐藏参数。")
            add("原结果行", match["row"])
            return
        if match.get("kind") == "hedge":
            request = snapshot["origin_identity"]["hedge_request"]
            add("模型", "双向持仓独立回测；保留原随机重复次数及种子范围")
            add("双向持仓参数（比例以小数存储）", request["hedge"])
            add("全部开仓条件", request["hedge_exact"]["descriptor"])
        else:
            add("完整配置（比例数值以原始小数存储）", snapshot["selection"])
        for key in ("sources", "start", "end", "engine_version", "ranking_settings"):
            add({"sources": "数据来源", "start": "UTC开始（空白=数据起点）", "end": "UTC结束（空白=数据终点）",
                 "engine_version": "原计算版本", "ranking_settings": "原排行设置"}[key], snapshot[key])
        for key in ("止盈类别", "止盈指标", "止盈参数一", "止盈参数二", "止盈参数三", "止损说明", "固定止损说明", "叠加止盈说明"):
            if key in match["row"]:
                add(key, match["row"][key])

    def _sync_run_target(self):
        target = self.run_target_var.get()
        if self._fingerprint_run_active() and target != self._previous_run_target:
            self.run_target_var.set(self._previous_run_target)
            return
        if target != self._previous_run_target:
            self._invalidate_fingerprint_lookup()
        self._previous_run_target = target
        if not hasattr(self, "fingerprint_batch_view"):
            return
        if target == "FINGERPRINTS":
            self.selection_panel.pack_forget()
            self.fingerprint_batch_view.pack(fill="both", expand=True)
            sizes = [self._queue_display_values(i, m)[2] for i, m in enumerate(self.fingerprint_queue)]
            counts = len(self.fingerprint_queue)
            self.run_target_hint_var.set(f"本次精确运行{counts}条指纹策略｜各条名义倍数：{', '.join(sizes[:12]) or '列表为空'}"
                                         + ("…" if len(sizes) > 12 else "") + "。每条独立，不使用编辑组合的勾选、成本或日期。")
            if hasattr(self, "selection_summary_var"):
                self.selection_summary_var.set(self.run_target_hint_var.get())
                self.storage_summary_var.set(f"输出{counts}个独立子任务；每条只产生对应原指纹的1个账户组合。")
        else:
            self.fingerprint_batch_view.pack_forget()
            self.selection_panel.pack(fill="both", expand=True)
            self.run_target_hint_var.set("本次运行编辑组合，指纹列表不参与；使用当前页面的勾选、成本和日期。")
            self.selection_panel._update_summary()
        if hasattr(self, "start_btn"):
            self.start_btn.configure(text=f"开始列表（{len(self.fingerprint_queue)}条）" if target == "FINGERPRINTS" else "开始 / 断点继续")
        for widget in getattr(self, "_editor_run_controls", ()):
            widget.grid_remove() if target == "FINGERPRINTS" else widget.grid()
        if hasattr(self, "_fold_order"):
            self._apply_folds()

    def show_fingerprint_queue(self):
        if self._fingerprint_run_active():
            messagebox.showinfo("正在运行", "回测或导出结束后再编辑策略列表。")
            return
        if self._fingerprint_queue_dialog is not None:
            self._fingerprint_queue_dialog.lift()
            return
        dialog = tk.Toplevel(self)
        self._fingerprint_queue_dialog = dialog
        dialog.title("多指纹策略列表：独立顺序回测")
        fit_window(dialog, 1000, 570, minimum=(850, 500))
        dialog.transient(self)
        dialog.protocol("WM_DELETE_WINDOW", self._close_fingerprint_queue)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="粘贴多个16位指纹（换行、空格、逗号或分号分隔），核验后加入列表。来源不明确时逐条选择。",
                  wraplength=800).pack(anchor="w")
        self.queue_input = tk.Text(body, height=3, wrap="word")
        self.queue_input.pack(fill="x", pady=6)
        self.queue_input.insert("1.0", self.fingerprint_var.get())
        self.queue_input.edit_modified(False)
        def changed(_event=None):
            if self.queue_input.edit_modified():
                self.queue_input.edit_modified(False)
                self._invalidate_fingerprint_lookup()
        self.queue_input.bind("<<Modified>>", changed)
        search = ttk.Frame(body)
        search.pack(fill="x")
        ttk.Label(search, text="来源目录（可选）").pack(side="left")
        ttk.Entry(search, textvariable=self.fingerprint_source_var).pack(side="left", fill="x", expand=True, padx=6)
        self.queue_lookup_button = ttk.Button(search, text="核验并加入列表", command=self.lookup_fingerprint_queue)
        self.queue_lookup_button.pack(side="right")
        ttk.Label(body, textvariable=self.fingerprint_status_var, foreground="#1F4E78", wraplength=800).pack(fill="x", pady=6)
        self.queue_tree = self._new_queue_tree(body, height=6)
        ttk.Label(body, text="各条沿用各自原参数、成本、账户、数据及日期；当前页面勾选不参与列表回测。"
                  "不是共享资金的组合策略，也不会把参数交叉合并。\n顺序运行，每条一个新子目录；"
                  "停止或失败不启动下一条。批量任务暂不续跑，可移除已完成项后另开一批。列表随正常退出保存。",
                  foreground="#7F6000", wraplength=800).pack(fill="x", pady=8)
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        ttk.Button(actions, text="移除所选", command=self.remove_queued_fingerprints).pack(side="left")
        ttk.Button(actions, text="清空列表", command=lambda: self.remove_queued_fingerprints(clear=True)).pack(side="left", padx=6)
        ttk.Button(actions, text="合并到编辑组合", command=lambda: self.revalidate_fingerprint_queue(apply_to_editor=True)).pack(side="left", padx=6)
        ttk.Button(actions, text="重新核验", command=self.revalidate_fingerprint_queue).pack(side="left")
        ttk.Button(actions, text="关闭", command=self._close_fingerprint_queue).pack(side="right")
        self.queue_start_button = ttk.Button(actions, text="运行列表（独立回测）", command=self.start_fingerprint_batch)
        self.queue_start_button.pack(side="right", padx=6)
        self._refresh_fingerprint_queue()

    def _close_fingerprint_queue(self):
        self._invalidate_fingerprint_lookup()
        if self._fingerprint_queue_dialog is not None:
            self._fingerprint_queue_dialog.destroy()
            self._fingerprint_queue_dialog = None

    def lookup_fingerprint_queue(self, text=None, search_root=None):
        if self._fingerprint_run_active() or self._fingerprint_busy:
            return
        from fingerprint_lookup import parse_fingerprints
        try:
            if text is None:
                text = self.queue_input.get("1.0", "end").strip()
            parse_fingerprints(text)
            root = search_root or self.fingerprint_source_var.get().strip() or self.out_var.get().strip()
            if not Path(root).is_dir():
                raise ValueError("请选择存在的原结果目录或结果根目录")
        except ValueError as exc:
            messagebox.showerror("无法加入列表", str(exc))
            return
        # Ignore the Text widget's pending modification event for this submitted text.
        if self._fingerprint_queue_dialog is not None:
            self.queue_input.edit_modified(False)
        self._fingerprint_generation += 1
        self._fingerprint_queue_merge_requested = False
        self._fingerprint_busy = True
        self.fingerprint_lookup_button.configure(state="disabled")
        self._set_queue_lookup_state("disabled")
        self.fingerprint_status_var.set("正在批量查找原结果……")
        self._start_fingerprint_job("queue_find", (text, root), self._fingerprint_generation)

    def revalidate_fingerprint_queue(self, *, apply_to_editor=False):
        if self._fingerprint_run_active() or self._fingerprint_busy:
            return
        if not self.fingerprint_queue:
            messagebox.showinfo("列表为空", "请先在上方输入指纹添加。")
            return
        self._fingerprint_generation += 1
        self._fingerprint_queue_merge_requested = apply_to_editor
        self._fingerprint_busy = True
        self.fingerprint_lookup_button.configure(state="disabled")
        self._set_queue_lookup_state("disabled")
        self._fingerprint_queue_pending = list(self.fingerprint_queue)
        self.fingerprint_status_var.set("正在重新核验完整列表；任何参数来源变化都将拒绝运行。")
        self._start_fingerprint_job("queue_restore", self._fingerprint_queue_pending, self._fingerprint_generation)

    def remove_queued_fingerprints(self, clear=False, tree=None):
        if self._fingerprint_run_active() or self._fingerprint_busy:
            return
        source_tree = tree if tree is not None else self.queue_tree
        chosen = set(source_tree.selection())
        self.fingerprint_queue = [] if clear else [m for i, m in enumerate(self.fingerprint_queue) if str(i) not in chosen]
        self._refresh_fingerprint_queue()

    def start_fingerprint_batch(self):
        if self._fingerprint_run_active() or self._fingerprint_busy:
            return
        if not self.fingerprint_queue:
            messagebox.showinfo("列表为空", "请先核验并加入至少一个指纹。")
            return
        missing = [item for item in self.fingerprint_queue if _fingerprint_source_missing(item)]
        if missing:
            messagebox.showerror("列表回测未启动", f"列表有{len(missing)}条记录来源目录失效；请先恢复来源或在多指纹列表中移除。"
                                 "不会自动跳过它们运行其余策略。\n" + "\n".join(
                                     f"{item['fingerprint']}：{item['source_dir']}" for item in missing))
            return
        if any(not item.get("verified_snapshot") for item in self.fingerprint_queue):
            messagebox.showinfo("请先核验完整参数", "列表含旧版记录。请点“重新核验列表”，确认每条完整参数后再运行。")
            return
        try:
            matches = self._validated_fingerprint_queue(self.fingerprint_queue)
            threads = int(self.thread_var.get())
            if not 1 <= threads <= max(1, os.cpu_count() or 1):
                raise ValueError("CPU并发数超出范围")
            root = Path(self.out_var.get().strip())
            if not self.out_var.get().strip():
                raise ValueError("请选择结果根目录")
            root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("多指纹回测_%Y%m%d_%H%M%S")
            out = root / stamp
            suffix = 2
            while out.exists():
                out = root / f"{stamp}_{suffix}"
                suffix += 1
            out.mkdir()
            manifest = out / "指纹批量清单.json"
            atomic_json(manifest, {"version": 1, "strategies": matches})
        except (OSError, ValueError, tk.TclError) as exc:
            messagebox.showerror("列表回测未启动", str(exc))
            return
        cmd = [sys.executable, "-X", "utf8", str(项目目录 / "fingerprint_batch.py"),
               "--manifest", str(manifest), "--output", str(out), "--threads", str(threads),
               "--device", "cpu", "--cache-root", str(root / "_共享指标缓存")]
        self.run_target_var.set("FINGERPRINTS")
        self._sync_run_target()
        if not self._launch_process(cmd, out, "fingerprint_batch"):
            return
        self.current_output_dir = out
        self._last_fingerprint_batch_dir = out
        self.active_output_var.set(f"当前任务目录：{out}")
        self._close_fingerprint_queue()
        self.notebook.select(self.run_tab)
        self.pause_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        self.progress["value"] = 0
        self.status_var.set(f"正在核验{len(self.fingerprint_queue)}条策略并顺序回测……")
        self.append_log(f"启动多指纹独立回测，共{len(self.fingerprint_queue)}条，结果：{out}。不混合参数、不共享资金。")

    @staticmethod
    def _validate_date_range(start, end):
        import pandas as pd
        start, end = str(start or "").strip(), str(end or "").strip()
        try:
            first = pd.to_datetime(start, utc=True) if start else None
            last = pd.to_datetime(end, utc=True) if end else None
            if (first is not None and pd.isna(first)) or (last is not None and pd.isna(last)):
                raise ValueError("日期无效")
            if first is not None and last is not None and first >= last:
                raise ValueError("开始日期必须早于结束日期")
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"回测日期无效：{exc}") from exc
        return start, end

    def current_date_range(self):
        return self._validate_date_range(self.start_date_var.get(), self.end_date_var.get())

    def apply_hedge_fingerprint_projection(self, projection):
        if self._fingerprint_run_active():
            raise ValueError("回测或导出尚未结束，不能应用指纹")
        from hedge_panel import DEFAULT_FIELDS, MODE_LABELS, PATH_LABELS, ENTRY_PLAN_LABELS, fields_to_config
        from extended_rules import SECOND_ROUND_CODES
        selection = projection["selection"]
        require_selection_allowed(selection)
        fields = projection["fields"]
        if set(fields) != set(DEFAULT_FIELDS) or any(type(fields[key]) is not type(value) for key, value in DEFAULT_FIELDS.items()):
            raise ValueError("双向输入参数不完整或格式错误")
        fields_to_config(fields)
        if fields["mode"] not in MODE_LABELS or fields["paths"] not in PATH_LABELS or fields["entry_plan"] not in ENTRY_PLAN_LABELS:
            raise ValueError("双向持仓模式或组合模式无效")
        panel = self.selection_panel
        capabilities = dict(projection["capabilities"])
        timeframe_capabilities = dict(projection.get("capabilities_by_timeframe", {}))
        for tf, variables in panel.case_vars.items():
            codes = selection["开仓条件"][tf]
            if not codes or any(code not in variables for code in codes):
                raise ValueError(f"{tf}原开仓条件无法通过当前勾选框表示")
            if unavailable_entry_codes(codes, capabilities_for_timeframe(capabilities, timeframe_capabilities, tf), tf):
                raise ValueError(f"{tf}原数据缺少所选策略需要的字段")
        if not selection["开仓指标"] or any(field not in panel.field_vars for field in selection["开仓指标"]):
            raise ValueError("原开仓指标无效")
        s3 = selection["入场约束"]
        gap = float(s3["最小S3距离"]) * 100
        if not math.isfinite(gap) or gap < 0 or s3["S3基线周期"] not in S3基线周期选项:
            raise ValueError("原S3入场约束无效")
        sources = {key: str(projection["sources"][key] or "") for key in ("kline", "micro", "funding", "oi", "bundle")}
        if not (sources["kline"] or sources["bundle"]):
            raise ValueError("原配置缺少主K线或数据包路径")
        start, end = self._validate_date_range(projection["start"], projection["end"])
        # Validate both pages before applying. Hedge exits belong to page 5.
        for key, variable in (("kline", self.csv_var), ("micro", self.micro_csv_var),
                              ("funding", self.funding_var), ("oi", self.oi_var), ("bundle", self.bundle_var)):
            variable.set(sources[key])
        self.start_date_var.set(start)
        self.end_date_var.set(end)
        panel.indicator_combos["开仓"] = copy.deepcopy(selection["指标组合"].get("开仓", {}))
        for tf, variables in panel.case_vars.items():
            for code, variable in variables.items():
                variable.set(code in selection["开仓条件"][tf])
        for field, variable in panel.field_vars.items():
            variable.set(field in selection["开仓指标"])
        panel.set_entry_modes(selection["入场触发口径"], update_summary=False)
        for folds, codes in ((panel.entry_fold_vars, SECOND_ROUND_CODES), (panel.third_entry_fold_vars, THIRD_ROUND_CODES),
                             (panel.fourth_entry_fold_vars, FOURTH_ROUND_CODES), (panel.fifth_entry_fold_vars, FIFTH_ROUND_CODES)):
            for tf, fold in folds.items():
                if any(code in codes for code in selection["开仓条件"][tf]):
                    fold.set(True)
        self.s3_gap_var.set(gap)
        self.s3_timeframe_var.set(s3["S3基线周期"])
        self.data_report = {"capabilities": capabilities, "capabilities_by_timeframe": timeframe_capabilities}
        panel.set_data_capabilities(capabilities, uncheck_unavailable=False, timeframe_capabilities=timeframe_capabilities)
        self.hedge_panel.apply_fields(fields)
        self.data_summary_var.set("双向指纹的原数据、日期及开仓字段已核验。")
        self.data_strategy_var.set(f"已同步{projection['entry_count']}种双向开仓组合；持仓参数在第5页。")
        self.data_run_summary_var.set(f"{Path(sources['kline'] or sources['bundle']).name}｜{start or '数据起点'} ～ {end or '数据终点'}（结束不含）")

    def apply_fingerprint_restoration(self, restoration, *, allow_multiple=False):
        if self._fingerprint_run_active():
            raise ValueError("回测或导出尚未结束，不能应用指纹")
        # 后台核验数据文件与指纹；主线程在任何变量赋值前核验整份回填数据。
        previous_target = self.run_target_var.get()
        raw = restoration["selection"]
        require_selection_allowed(raw)
        missing = set(全选配置()) - set(raw)
        if missing:
            raise ValueError("原配置不完整，缺少：" + "、".join(sorted(missing)))
        config = 规范化配置(raw)
        count = 配置统计(config)["包含仓位完整组合数"]
        if not allow_multiple and count != 1:
            raise ValueError("指纹还原必须恰好对应一个完整组合")
        if allow_multiple and count != restoration["projected_count"]:
            raise ValueError("指纹合并的组合数量不一致，未应用")
        sources = {key: str(restoration["sources"][key] or "") for key in ("kline", "micro", "funding", "oi", "bundle")}
        if not (sources["kline"] or sources["bundle"]):
            raise ValueError("原配置缺少主K线或数据包路径")
        start, end = self._validate_date_range(restoration["start"], restoration["end"])
        source_dir = str(restoration["source_dir"])
        fingerprint = str(restoration["fingerprint"])
        engine_version = str(restoration["engine_version"])
        current_version = str(restoration["current_engine_version"])
        capabilities = dict(restoration["capabilities"])
        timeframe_capabilities = restoration.get("capabilities_by_timeframe", {})
        if any(unavailable_entry_codes(codes, capabilities_for_timeframe(capabilities, timeframe_capabilities, tf), tf)
               for tf, codes in config["开仓条件"].items()):
            raise ValueError("原数据缺少所选策略需要的数据字段")
        candidate = config["候选筛选"]
        if (candidate["启用"] and candidate["比较范围"] == "TARGET"
                and candidate["统一目标杠杆"] not in config["仓位倍数"]):
            raise ValueError("候选目标杠杆与还原组合不一致")
        ranking = restoration.get("ranking_settings")
        if ranking is not None:
            limit = top_limit(ranking)
            if ranking.get("排行指标") not in 排行指标选项 or not isinstance(ranking.get("门槛"), list):
                raise ValueError("原排行榜设置不完整")
            ranking_filters = []
            for item in ranking["门槛"]:
                if item.get("指标") not in 排行指标选项 or item.get("条件") not in ("最低值", "最高值"):
                    raise ValueError("原排行榜门槛无效")
                if not math.isfinite(float(item.get("输入值", item.get("值")))):
                    raise ValueError("原排行榜门槛必须是有限数字")
                value = float(item.get("输入值", item.get("值")))
                if "输入值" not in item and "（%）" in 排行指标选项[item["指标"]][0]:
                    value *= 100.0
                ranking_filters.append({**item, "输入值": value})
            ranking = {"排行指标": ranking["排行指标"], "门槛": ranking_filters}
            if limit != 5000:
                ranking["最优名额"] = limit
        warnings = [str(text) for text in restoration.get("warnings", [])]
        # 所有字段已经验证；不走旧设置加载的偏移升级或候选参数调整分支。
        for key, variable in (("kline", self.csv_var), ("micro", self.micro_csv_var),
                              ("funding", self.funding_var), ("oi", self.oi_var), ("bundle", self.bundle_var)):
            variable.set(sources[key])
        self.start_date_var.set(start)
        self.end_date_var.set(end)
        self.selection_panel.clear_data_capabilities()
        self.selection_panel.apply_config(config)
        slip = config["成交偏移"]
        self.entry_slippage_var.set(slip["开仓"] * 100.0)
        self.exit_slippage_var.set(slip["平仓"] * 100.0)
        self.slippage_preset_var.set("自定义")
        self.update_slippage_label()
        self.fill_mode_var.set(成交价格口径选项[config["成交价格口径"]])
        self.apply_execution_settings(config)
        funds = config["资金约束"]
        for variable, key, scale in ((self.initial_capital_var, "初始资金USDC", 1),
                                     (self.minimum_eth_var, "最小开仓数量ETH", 1),
                                     (self.maximum_eth_var, "最大开仓数量ETH", 1),
                                     (self.protect_ratio_var, "保护止损浮亏比例", 100),
                                     (self.maintenance_rate_var, "维持保证金率", 100),
                                     (self.funding_rate_var, "资金费率", 100)):
            variable.set(funds[key] * scale)
        self.stop_when_too_small_var.set(funds["低于最小数量停止"])
        self.cross_liquidation_var.set(funds["启用全仓强平"])
        self.s3_gap_var.set(config["入场约束"]["最小S3距离"] * 100.0)
        self.s3_timeframe_var.set(config["入场约束"]["S3基线周期"])
        self.selection_panel.set_entry_modes(config["入场触发口径"])
        self.apply_candidate_settings(candidate)
        if ranking is not None:
            self.apply_ranking_settings(ranking)
        self.data_report = {"capabilities": capabilities, "capabilities_by_timeframe": timeframe_capabilities}
        self.selection_panel.set_data_capabilities(capabilities, uncheck_unavailable=False,
                                                   timeframe_capabilities=timeframe_capabilities)
        self.data_summary_var.set("原数据指纹及策略所需字段已核验。")
        self.data_strategy_var.set(f"所选{count}个账户组合可运行。")
        self.data_run_summary_var.set(f"{Path(sources['kline'] or sources['bundle']).name}｜{start or '数据起点'} ～ {end or '数据终点'}（结束不含）")
        self.separate_output_var.set(True)
        self.force_var.set(False)
        self._active_output_dir = None
        self.mark_new_task()
        self._settings_restore_error = False
        self.run_target_var.set(previous_target if allow_multiple else "EDITOR")
        self._sync_run_target()
        self.update_selection_summary(配置统计(config), "")
        if allow_multiple:
            self.append_log(f"已将{restoration['exact_count']}条指纹同步为{count}个编辑组合；"
                            f"比{restoration['unique_count']}种原配置多{restoration['extra_count']}组交叉组合。"
                            f"指纹：{'、'.join(restoration['fingerprints'])}；来源：{'、'.join(restoration['source_dirs'])}。"
                            "只回测原指纹请手选精确列表。未开始运行，新结果将另建目录。")
        else:
            self.append_log(f"已按指纹 {fingerprint} 还原单一组合，来源：{source_dir}；原日期 {start or '数据起点'} ～ {end or '数据终点'}。未开始运行，新结果将另建目录。")
        self.append_log(f"原回测引擎：{engine_version}；当前引擎：{current_version}。"
                        + ("版本不同，重跑结果可能与原结果不同。" if engine_version != current_version else ""))
        for warning in warnings:
            self.append_log(warning)
        self.notebook.select(self.run_tab)

    def choose_source_file(self, variable, title):
        value = filedialog.askopenfilename(
            title=title,
            filetypes=[("数据文件", "*.csv *.parquet *.pq"), ("CSV", "*.csv"),
                       ("Parquet", "*.parquet *.pq"), ("所有文件", "*.*")],
        )
        if value:
            variable.set(value)

    def choose_bundle(self):
        value = filedialog.askopenfilename(title="选择多数据源ZIP", filetypes=[("ZIP数据包", "*.zip"), ("所有文件", "*.*")])
        if value:
            self.bundle_var.set(value)
            # A stale default path otherwise overrides the valid ZIP at runtime.
            if self.csv_var.get().strip() and not Path(self.csv_var.get().strip()).is_file():
                self.csv_var.set("")

    def _mark_data_dirty(self):
        self._data_generation = getattr(self, "_data_generation", 0) + 1
        self.data_report = None
        if hasattr(self, "data_summary_var"):
            self.data_summary_var.set("数据路径已变化，请点击“检测数据与策略覆盖”。")
            self.data_strategy_var.set("")
            for item in self.data_tree.get_children():
                self.data_tree.delete(item)
        if hasattr(self, "selection_panel"):
            self.selection_panel.clear_data_capabilities()
        names = []
        if self.bundle_var.get().strip(): names.append("ZIP")
        if self.csv_var.get().strip(): names.append(Path(self.csv_var.get().strip()).name)
        if self.micro_csv_var.get().strip(): names.append("+成交微结构")
        if self.funding_var.get().strip(): names.append("+资金费")
        if self.oi_var.get().strip(): names.append("+OI")
        if hasattr(self, "data_run_summary_var"):
            self.data_run_summary_var.set(" / ".join(names) if names else "未配置数据源")

    def inspect_data_sources(self):
        if getattr(self, "_data_inspecting", False):
            return
        self._data_inspecting = True
        self.inspect_data_btn.configure(state="disabled")
        self.data_summary_var.set("正在检测数据列、时间连续性、覆盖率与可运行策略……")
        args = dict(
            primary=self.csv_var.get().strip() or None,
            micro=self.micro_csv_var.get().strip() or None,
            funding=self.funding_var.get().strip() or None,
            oi=self.oi_var.get().strip() or None,
            bundle=self.bundle_var.get().strip() or None,
            cache_root=str(项目目录 / "_数据源检测缓存"),
        )
        generation = self._data_generation
        def worker():
            try:
                report = inspect_sources(**args)
                self.messages.put({"type": "data_inspection", "report": report, "generation": generation})
            except Exception as exc:
                self.messages.put({"type": "data_inspection_error", "message": str(exc), "generation": generation})
        threading.Thread(target=worker, daemon=True).start()

    def _apply_data_report(self, report):
        self.data_report = report
        caps = report.get("capabilities", {})
        self.selection_panel.set_data_capabilities(caps, uncheck_unavailable=True,
            timeframe_capabilities=report.get("capabilities_by_timeframe", {}))
        availability = self.selection_panel.last_availability_report
        if availability["message"]:
            self.append_log(availability["message"])
        self.save_user_settings()
        for item in self.data_tree.get_children():
            self.data_tree.delete(item)
        uses = {
            "ohlcv": "第一/二批 + 第三/四批价格、MACD、波动、结构策略",
            "quote_volume": "成交额倍率 140/141",
            "trades": "成交笔数 58/104/106/119",
            "taker_base": "推导主动买卖量、Delta、主动买占比",
            "taker_quote": "主动买成交额占比/成交额Delta 142/143",
            "delta_cvd": "Delta/CVD 56/57/60/102/103/107/109/110/111",
            "taker_ratio": "主动买占比 53/54/55/99/100/101/108",
            "avg_trade_size": "平均单笔规模 59/105/127",
            "funding": "资金费策略 128—131（上一期已结算值，不偷看下一期）",
            "open_interest": "OI策略 132—137/139",
            "open_interest_value": "OI价值策略 138",
        }
        coverage = report.get("coverage", {})
        for key, label in CAPABILITY_LABELS.items():
            ok = bool(caps.get(key))
            cov = float(coverage.get(key, 0.0))
            self.data_tree.insert("", "end", values=(label, "可用" if ok else "缺失", f"{cov:.1%}", uses.get(key, "")),
                                  tags=("ok" if ok else "missing",))
        start = report.get("start_ms"); end = report.get("end_ms")
        def fmt(ms):
            return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else "?"
        self.data_summary_var.set(
            f"{report.get('symbol') or '未识别品种'}｜{report.get('rows', 0):,} 根1m｜UTC {fmt(start)} ～ {fmt(end)}｜"
            f"时间缺口 {report.get('gaps', 0)}｜数据能力 {sum(bool(v) for v in caps.values())}/{len(CAPABILITY_LABELS)}"
        )
        coverage_by_tf = []
        for tf, values in self.selection_panel.case_vars.items():
            codes = [c for c in RESEARCH_CASE_CODES if c in values]
            usable = [c for c in codes if self.selection_panel._entry_available(tf, c)]
            coverage_by_tf.append(f"{tf} {len(usable)}/{len(codes)}")
        coverage_text = "、".join(coverage_by_tf)
        self.data_strategy_var.set(
            f"第三/四批各周期可运行：{coverage_text}；缺数据选项已自动取消勾选并灰显。"
            "第一/二批只要OHLCV完整就可运行。"
        )
        self.data_run_summary_var.set(
            f"{report.get('symbol') or ''}｜1m {report.get('rows',0):,}根｜第三/四批 {coverage_text}"
        )
        self.append_log("数据能力检测完成：" + self.data_summary_var.get())

    def trim_to_data(self):
        if not self.data_report:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”。")
            return
        disabled = self.selection_panel.set_data_capabilities(self.data_report.get("capabilities", {}), True,
            timeframe_capabilities=self.data_report.get("capabilities_by_timeframe", {}))
        messagebox.showinfo("已修剪", f"已取消并灰显 {len(disabled)} 类第三/四批策略中缺少当前周期数据的选项。")

    def select_available_third(self):
        if not self.data_report:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”。")
            return
        for tf in self.selection_panel.case_vars:
            self.selection_panel._set_third_source(tf, "available")
        self.notebook.select(1)

    def select_available_fourth(self):
        if not self.data_report:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”；现有勾选保持不变。")
            return
        for tf in self.selection_panel.case_vars:
            self.selection_panel._set_fourth_source(tf, "available")
        self.notebook.select(1)

    def select_available_fifth(self):
        if not self.data_report:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”；现有勾选保持不变。")
            return
        for tf in self.selection_panel.case_vars:
            self.selection_panel._set_fifth_source(tf, "available")
        self.notebook.select(1)

    def _build_run_tab(self, outer):

        self.fold_vars = {}
        self._fold_bodies = {}
        self._fold_headers = {}
        self._fold_boxes = {}
        self._fold_order = []
        # 操作栏 / 状态 / 进度条必须最先 pack(side="bottom")：Tk 的 pack 按调用顺序
        # 从剩余空间里分配，先占位的才保得住。放在各分区后面 pack 的话，窗口一矮，
        # 上面的分区把空间吃光，"开始"按钮直接被裁到可视区外，根本点不到。
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(bottom, maximum=100)
        self.progress.pack(side="bottom", fill="x", pady=(4, 0))
        self.status_var = tk.StringVar(value="等待开始")
        ttk.Label(bottom, textvariable=self.status_var,
                  font=(self.ui_font_family, 10, "bold")).pack(side="bottom", anchor="w")
        self._controls_bar = ttk.Frame(bottom)
        self._controls_bar.pack(side="bottom", fill="x", pady=(6, 4))
        self.run_scroll = ScrollableForm(outer)
        self.run_scroll.pack(fill="both", expand=True)
        outer = self.run_scroll.body
        ttk.Label(outer, textvariable=self.run_target_hint_var, foreground="#C00000", wraplength=970,
                  font=(self.ui_font_family, 10, "bold")).pack(fill="x", pady=(0, 8))
        ttk.Label(outer, text="第一轮与扩展规则统一在「组合选择」多选；不锁定基线、不替换其他勾选。",
                  foreground="#1F4E78").pack(anchor="w", pady=(0, 8))
        fold_row = ttk.Frame(outer)
        fold_row.pack(fill="x", pady=(0, 4))
        ttk.Label(fold_row, text="显示区块：", foreground="#1F4E78").pack(side="left")
        self._fold_bar = ttk.Frame(fold_row)
        self._fold_bar.pack(side="left")
        # 所有可折叠分区都装在这个容器里，收起时整块拿掉、真正还出高度。
        self._sections = ttk.Frame(outer)
        self._sections.pack(fill="x")
        paths_box = ttk.LabelFrame(self._sections, text="数据与输出", style="Card.TLabelframe", padding=10)
        paths_box.pack(fill="x")
        # 内容整块装在内层 Frame 里：折叠时把这一个 Frame forget 掉，
        # LabelFrame 才会真的收缩；逐个隐藏子控件既不省高度，
        # 重新显示时还会把 pack 顺序和参数弄乱。
        paths = ttk.Frame(paths_box)
        paths.pack(fill="both", expand=True)
        ttk.Label(paths, text="当前数据源", width=14).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(paths, textvariable=self.data_run_summary_var, foreground="#1F4E78", wraplength=800).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(paths, text="配置数据…", command=lambda: self.notebook.select(0)).grid(row=0, column=2)
        self._path_row(paths, 1, "结果根目录", self.out_var, self.choose_out)
        self.separate_output_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(paths, text="新任务自动创建独立文件夹，防止覆盖旧结果",
                        variable=self.separate_output_var).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Button(paths, text="下次建新任务", command=self.mark_new_task).grid(row=2, column=2, pady=4)
        self._editor_run_controls = [widget for widget in paths.grid_slaves()
                                     if int(widget.grid_info()["row"]) in (0, 2)]
        self.active_output_var = tk.StringVar(value="当前任务目录：尚未创建")
        ttk.Label(paths, textvariable=self.active_output_var, foreground="#7F6000").grid(
            row=3, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        options_box = ttk.LabelFrame(self._sections, text="运行设置", style="Card.TLabelframe", padding=10)
        options_box.pack(fill="x", pady=10)
        # 内容整块装在内层 Frame 里：折叠时把这一个 Frame forget 掉，
        # LabelFrame 才会真的收缩；逐个隐藏子控件既不省高度，
        # 重新显示时还会把 pack 顺序和参数弄乱。
        options = ttk.Frame(options_box)
        options.pack(fill="both", expand=True)
        ttk.Label(options, text="CPU并发数").grid(row=0, column=0, sticky="w")
        self.thread_var = tk.IntVar(value=推荐线程数())
        ttk.Spinbox(options, from_=1, to=max(1, os.cpu_count() or 20), width=8,
                    textvariable=self.thread_var,
                    command=self.update_thread_hint).grid(row=0, column=1, sticky="w", padx=(8, 4))
        ttk.Button(options, text="用默认值", width=9,
                   command=self.use_recommended_threads).grid(row=0, column=2, sticky="w", padx=(0, 18))
        self.force_var = tk.BooleanVar(value=False)
        ttk.Label(options, text="计算设备").grid(row=0, column=3, sticky="e", padx=(18, 4))
        self.device_var = tk.StringVar(value="自动（CPU多线程）")
        ttk.Combobox(options, textvariable=self.device_var, state="readonly", width=18,
                     values=计算设备选项).grid(row=0, column=4, sticky="w")
        self.thread_hint_var = tk.StringVar()
        self.thread_hint = ttk.Label(options, textvariable=self.thread_hint_var, foreground="#7F6000")
        self.thread_hint.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        # 线程数是手填的，改完不一定按回车，所以直接盯着变量。
        self.thread_var.trace_add("write", lambda *_: self.update_thread_hint())
        self.update_thread_hint()
        cost_frame = ttk.Frame(options)
        cost_frame.grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(cost_frame, text="成交偏移预设").pack(side="left")
        self.slippage_preset_var = tk.StringVar(value=DEFAULT_SLIPPAGE_PRESET)
        preset_box = ttk.Combobox(cost_frame, textvariable=self.slippage_preset_var, state="readonly", width=30,
                                  values=(DEFAULT_SLIPPAGE_PRESET, "旧版示例偏移", "指定总偏移0.05%", "不计成交偏移", "自定义"))
        preset_box.pack(side="left", padx=(8, 16))
        preset_box.bind("<<ComboboxSelected>>", self.apply_slippage_preset)
        self.entry_slippage_var = tk.DoubleVar(value=DEFAULT_SLIPPAGE["开仓"] * 100.0)
        self.exit_slippage_var = tk.DoubleVar(value=DEFAULT_SLIPPAGE["平仓"] * 100.0)
        ttk.Label(cost_frame, text="开仓偏移%").pack(side="left")
        entry_box = ttk.Entry(cost_frame, textvariable=self.entry_slippage_var, width=9)
        entry_box.pack(side="left", padx=(5, 12))
        ttk.Label(cost_frame, text="平仓偏移%").pack(side="left")
        exit_box = ttk.Entry(cost_frame, textvariable=self.exit_slippage_var, width=9)
        exit_box.pack(side="left", padx=(5, 12))
        self.slippage_controls = (preset_box, entry_box, exit_box)
        entry_box.bind("<KeyRelease>", self.slippage_edited); exit_box.bind("<KeyRelease>", self.slippage_edited)
        self.roundtrip_slippage_var = tk.StringVar()
        ttk.Label(cost_frame, textvariable=self.roundtrip_slippage_var, foreground="#7F6000").pack(side="left")
        self.update_slippage_label()
        restart_control = ttk.Checkbutton(options, text="删除原断点并从头重新计算", variable=self.force_var)
        restart_control.grid(row=3, column=0, columnspan=5, sticky="w")
        self.hardware_var = tk.StringVar(value=f"CPU：{os.cpu_count() or '?'}线程｜GPU：检测中｜内存采用分块方式")
        ttk.Label(options, textvariable=self.hardware_var).grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))
        threading.Thread(target=self.detect_gpu, daemon=True).start()
        self.fill_mode_var = tk.StringVar(value=成交价格口径选项["CLOSE_CONFIRMED"])
        fill_row = ttk.Frame(options)
        fill_row.grid(row=5, column=0, columnspan=5, sticky="w", pady=4)
        ttk.Label(fill_row, text="出场成交价格").pack(side="left")
        ttk.Combobox(fill_row, textvariable=self.fill_mode_var, state="readonly", width=42,
                     values=list(成交价格口径选项.values())).pack(side="left", padx=8)
        ttk.Label(fill_row, text="非Maker成交模拟；改口径须新建任务", foreground="#7F6000").pack(side="left")

        execution = ttk.LabelFrame(options, text="成本模式与平仓后等待", padding=8)
        execution.grid(row=6, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self._editor_run_controls.extend((cost_frame, restart_control, fill_row, execution))
        mode_row = ttk.Frame(execution)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="成本档位（可多选）").pack(side="left")
        self.cost_mode_widgets = {}
        for code, label in COST_MODE_LABELS.items():
            widget = ttk.Checkbutton(mode_row, text=label, variable=self.cost_mode_vars[code],
                                     command=self.update_cost_mode)
            widget.pack(side="left", padx=10)
            self.cost_mode_widgets[code] = widget
        self.cost_mode_hint_var = tk.StringVar()
        ttk.Label(mode_row, textvariable=self.cost_mode_hint_var, foreground="#7F6000").pack(side="left", padx=8)
        self.fee_preset_var = tk.StringVar(value=DEFAULT_FEE_PRESET)
        self.entry_fee_var = tk.DoubleVar(value=DEFAULT_FEES["开仓费率"] * 100.0)
        self.exit_fee_var = tk.DoubleVar(value=DEFAULT_FEES["平仓费率"] * 100.0)
        self.bnb_discount_var = tk.BooleanVar(value=DEFAULT_FEES["BNB抵扣"])
        self.fee_rebate_var = tk.DoubleVar(value=DEFAULT_FEES["返佣比例"] * 100.0)
        self.min_reentry_minutes_var = tk.StringVar(value=str(DEFAULT_MIN_REENTRY_MINUTES))
        self.net_fee_label_var = tk.StringVar()
        fee_row = ttk.Frame(execution)
        fee_row.pack(fill="x", pady=(6, 0))
        ttk.Label(fee_row, text="手续费预设").pack(side="left")
        fee_presets = ttk.Combobox(fee_row, textvariable=self.fee_preset_var, state="readonly", width=28,
                                  values=(*FEE_PRESETS, "自定义"))
        fee_presets.pack(side="left", padx=(8, 14))
        fee_presets.bind("<<ComboboxSelected>>", self.apply_fee_preset)
        ttk.Label(fee_row, text="未折扣费率：开仓%").pack(side="left")
        entry_fee_box = ttk.Entry(fee_row, textvariable=self.entry_fee_var, width=8)
        entry_fee_box.pack(side="left", padx=(5, 12))
        ttk.Label(fee_row, text="平仓%").pack(side="left")
        exit_fee_box = ttk.Entry(fee_row, textvariable=self.exit_fee_var, width=8)
        exit_fee_box.pack(side="left", padx=5)
        discount_row = ttk.Frame(execution)
        discount_row.pack(fill="x", pady=(6, 0))
        bnb_box = ttk.Checkbutton(discount_row, text="BNB抵扣（基础费率九折）", variable=self.bnb_discount_var)
        bnb_box.pack(side="left")
        ttk.Label(discount_row, text="返佣比例%").pack(side="left", padx=(14, 5))
        rebate_box = ttk.Entry(discount_row, textvariable=self.fee_rebate_var, width=8)
        rebate_box.pack(side="left")
        ttk.Label(discount_row, textvariable=self.net_fee_label_var, foreground="#1F4E78",
                  wraplength=520, justify="left").pack(side="left", padx=14)
        self.fee_controls = (fee_presets, entry_fee_box, exit_fee_box, bnb_box, rebate_box)
        for variable in (self.entry_fee_var, self.exit_fee_var, self.bnb_discount_var, self.fee_rebate_var):
            variable.trace_add("write", lambda *_: self.update_fee_label())
        self.update_cost_mode()
        gap_row = ttk.Frame(execution)
        gap_row.pack(fill="x", pady=(6, 0))
        ttk.Label(gap_row, text="所有平仓后最小等待（分钟）").pack(side="left")
        self.min_reentry_minutes_box = ttk.Combobox(
            gap_row, textvariable=self.min_reentry_minutes_var, width=8,
            values=(0, 1, 2, 3, 5, 10, 15, 20, 30, 45, 60))
        self.min_reentry_minutes_box.pack(side="left", padx=8)
        ttk.Label(gap_row, text="可选择或输入整数；0关闭；5=平仓后满5分钟才可开仓", foreground="#7F6000").pack(side="left")
        ttk.Label(execution,
                  text="至少选择一档。两档同时勾选＝分别回测并在同一结果表比较，不会在同一笔交易叠加扣费。\n"
                       "偏移档只用成交偏移；手续费档只用手续费、BNB抵扣和返佣。未选档参数仍保留。\n"
                       "等待与策略止盈冷却取较晚时间，不相加；等待内信号不排队补开，届时重新判断。\n"
                       "偏移预设是演示参数，请按自己的数据和成交记录校准，不代表市场统计或成交保证。\n"
                       "适用于收线确认后执行，不含THEORETICAL理论盘中触价差，不能完整模拟实盘。\n"
                       "净费率=基础费率×BNB折扣×(1−返佣比例)。费率预设仅供示例，返佣按即时冲减近似。",
                  foreground="#7F6000", wraplength=970, justify="left").pack(anchor="w", pady=(6, 0))

        funds_box = ttk.LabelFrame(self._sections, text="资金与ETH单次下单约束", style="Card.TLabelframe", padding=10)
        funds_box.pack(fill="x", pady=(0, 10))
        # 内容整块装在内层 Frame 里：折叠时把这一个 Frame forget 掉，
        # LabelFrame 才会真的收缩；逐个隐藏子控件既不省高度，
        # 重新显示时还会把 pack 顺序和参数弄乱。
        funds = ttk.Frame(funds_box)
        funds.pack(fill="both", expand=True)
        self.initial_capital_var = tk.DoubleVar(value=100.0)
        self.minimum_eth_var = tk.DoubleVar(value=0.01)
        self.maximum_eth_var = tk.DoubleVar(value=100.0)
        self.stop_when_too_small_var = tk.BooleanVar(value=True)
        ttk.Label(funds, text="初始资金（USDC）").grid(row=0, column=0, sticky="w")
        ttk.Entry(funds, textvariable=self.initial_capital_var, width=12).grid(row=0, column=1, sticky="w", padx=(8, 24))
        ttk.Label(funds, text="ETH最小开仓数量").grid(row=0, column=2, sticky="w")
        ttk.Entry(funds, textvariable=self.minimum_eth_var, width=12).grid(row=0, column=3, sticky="w", padx=(8, 24))
        ttk.Label(funds, text="ETH单次最大开仓数量").grid(row=0, column=4, sticky="w")
        ttk.Entry(funds, textvariable=self.maximum_eth_var, width=12).grid(row=0, column=5, sticky="w", padx=(8, 0))
        ttk.Checkbutton(funds, text="不足时停止该仓位档后续交易", variable=self.stop_when_too_small_var).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(7, 0))
        ttk.Label(
            funds,
            text="计划数量=账户余额×名义倍数÷当时ETH价格；低于0.01 ETH时停机，超过100 ETH时按100 ETH成交，不再按无限仓位复利。",
            foreground="#C00000",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(7, 0))

        risk_box = ttk.LabelFrame(self._sections, text="全仓风险与入场闸门", style="Card.TLabelframe", padding=10)
        risk_box.pack(fill="x", pady=(0, 10))
        # 内容整块装在内层 Frame 里：折叠时把这一个 Frame forget 掉，
        # LabelFrame 才会真的收缩；逐个隐藏子控件既不省高度，
        # 重新显示时还会把 pack 顺序和参数弄乱。
        risk = ttk.Frame(risk_box)
        risk.pack(fill="both", expand=True)
        self.protect_ratio_var = tk.DoubleVar(value=85.0)
        self.maintenance_rate_var = tk.DoubleVar(value=0.5)
        self.cross_liquidation_var = tk.BooleanVar(value=True)
        # 固定资金费假设独立于历史资金费信号输入；默认0，不模拟真实逐次收付。
        self.funding_rate_var = tk.DoubleVar(value=0.0)
        self.s3_gap_var = tk.DoubleVar(value=0.10)
        self.s3_timeframe_var = tk.StringVar(value="1m")
        ttk.Label(risk, text="②保护止损浮亏（%权益）").grid(row=0, column=0, sticky="w")
        ttk.Entry(risk, textvariable=self.protect_ratio_var, width=10).grid(row=0, column=1, sticky="w", padx=(8, 24))
        ttk.Label(risk, text="维持保证金率（%）").grid(row=0, column=2, sticky="w")
        ttk.Entry(risk, textvariable=self.maintenance_rate_var, width=10).grid(row=0, column=3, sticky="w", padx=(8, 24))
        ttk.Checkbutton(risk, text="启用全仓强平线（早于保护线时账户归零并停机）",
                        variable=self.cross_liquidation_var).grid(row=1, column=0, columnspan=6, sticky="w", pady=(7, 0))
        ttk.Label(risk, text="最小S3距离（%）").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(risk, textvariable=self.s3_gap_var, width=10).grid(row=2, column=1, sticky="w", padx=(8, 24), pady=(7, 0))
        ttk.Label(risk, text="S3基线周期").grid(row=2, column=2, sticky="w", pady=(7, 0))
        ttk.Combobox(risk, textvariable=self.s3_timeframe_var, width=8, state="readonly",
                     values=S3基线周期选项).grid(row=2, column=3, sticky="w", padx=(8, 0), pady=(7, 0))
        entry_rule_row = ttk.Frame(risk)
        entry_rule_row.grid(row=3, column=0, columnspan=6, sticky="w", pady=(7, 0))
        entry_rule_label = ttk.Label(entry_rule_row, text="本次入场规则")
        entry_rule_label.pack(side="left")
        self.entry_mode_summary = ttk.Label(entry_rule_row, textvariable=self.entry_mode_var,
                                            padding=(13, 0), wraplength=600, font="TkTextFont")
        self.entry_mode_summary.pack(side="left")
        self.entry_rules_jump_button = ttk.Button(entry_rule_row, text="到组合页修改", command=self.show_entry_rules)
        self.entry_rules_jump_button.pack(side="left", padx=8)
        # Keep this row inside the viewport even when other risk rows widen the grid.
        self.run_scroll.canvas.bind("<Configure>", lambda event: self.entry_mode_summary.configure(
            wraplength=max(1, event.width - entry_rule_label.winfo_reqwidth()
                           - self.entry_rules_jump_button.winfo_reqwidth() - 66)), add="+")
        ttk.Label(risk, text="固定资金费假设（%/8小时）").grid(row=4, column=0, sticky="w", pady=(7, 0))
        ttk.Entry(risk, textvariable=self.funding_rate_var, width=10).grid(row=4, column=1, sticky="w", padx=(8, 24), pady=(7, 0))
        ttk.Label(risk, text="0不计；持仓跨UTC每8小时结算点，双向均按成本扣除", foreground="#7F6000").grid(
            row=4, column=2, columnspan=4, sticky="w", pady=(7, 0))
        ttk.Label(
            risk,
            text="全仓口径：整个账户为持仓担保。强平线与②保护线谁先到算谁；名义倍数越高强平线越靠前。"
                 "最小S3距离：多单须在基线上方、空单须在基线下方至少该距离，已越过或基线不可用一律不开仓，填0关闭。"
                 "历史资金费可作为开仓信号输入；固定资金费假设按8小时统一扣成本，不模拟历史真实资金费收付。"
                 "入场触发/次数规则统一在组合页的入场约束区域设置，与开仓位置过滤同时生效；此处仅显示当前规则。\n"
                 "实盘差异：本地仅回测ETH单品种，不模拟01的ETH/BTC共享单仓；成交偏移是固定假设，"
                 "不模拟Maker未成交、撤单追价、网络延迟和订单竞态；资金费/OI只按已发生值向后对齐，不使用未来数据。",
            foreground="#7F6000", wraplength=1080, justify="left",
        ).grid(row=5, column=0, columnspan=6, sticky="w", pady=(7, 0))

        summary = ttk.LabelFrame(self._sections, text="当前选择范围", style="Card.TLabelframe", padding=10)
        summary.pack(fill="x")
        summary_body = ttk.Frame(summary)
        summary_body.pack(fill="both", expand=True)
        self.selection_summary_var = tk.StringVar()
        self.storage_summary_var = tk.StringVar()
        ttk.Label(summary_body, textvariable=self.selection_summary_var,
                  font=(self.ui_font_family, 11, "bold"),
                  foreground="#C00000").pack(anchor="w")
        ttk.Label(summary_body, textvariable=self.storage_summary_var,
                  foreground="#7F6000").pack(anchor="w", pady=(4, 0))
        export_row = ttk.Frame(summary_body)
        export_row.pack(fill="x", pady=(6, 0))
        self.rank_top_limit_var = tk.StringVar(value="5000")
        ttk.Label(export_row, text="最优榜导出数量").pack(side="left")
        ttk.Combobox(export_row, textvariable=self.rank_top_limit_var, state="readonly",
                     values=TOP_LIMITS, width=9).pack(side="left", padx=8)
        ttk.Label(export_row, text="各分类及全局榜分别取前N条；不足时按实际数量导出").pack(side="left")
        # 排行指标和门槛是"配一次基本不动"的，默认收起，把高度让给日志和操作栏。
        summary_detail = ttk.Frame(summary_body)
        summary_detail.pack(fill="x")
        metric_row = ttk.Frame(summary_detail); metric_row.pack(fill="x", pady=(6, 0))
        ttk.Label(metric_row, text="排行取“最优”的依据").pack(side="left")
        self.rank_metric_var = tk.StringVar(value=默认排行指标)
        ttk.Combobox(metric_row, textvariable=self.rank_metric_var, state="readonly",
                     width=30, values=list(排行指标选项)).pack(side="left", padx=(8, 0))
        ttk.Label(metric_row, foreground="#888888",
                  text="两榜按此指标排序；最优应用下方门槛，最差查看全部有效结果（含亏损、资金性停机）。"
                  ).pack(side="left", padx=(10, 0))
        filters = ttk.LabelFrame(summary_detail, text="仅最优榜的组合门槛（全部同时满足；最差榜不受这些门槛限制）", padding=6)
        filters.pack(fill="x", pady=(6, 0))
        self.ranking_filters_body = ttk.Frame(filters)
        self.ranking_filters_body.pack(side="left", fill="x", expand=True)
        self.ranking_filter_rows = []
        ttk.Button(filters, text="＋添加门槛", command=self.add_ranking_filter).pack(side="right", padx=(8, 0))
        self.add_ranking_filter("交易次数（单）", "最低值", "")
        self.add_ranking_filter("最大回撤（%）越小越好", "最高值", "")
        ttk.Button(summary_detail, text="打开组合选择页",
                   command=lambda: self.notebook.select(1)).pack(anchor="e", pady=(2, 0))
        self._foldable(summary_body, summary_detail, "排行指标与门槛", True, whole=False)

        # 常改的留着，配一次基本不动的默认收起，省掉大半屏高度。
        self._foldable(paths_box, paths, "数据与输出", False)
        self._foldable(options_box, options, "运行设置", False)
        self._foldable(funds_box, funds, "资金与下单约束", True)
        self._foldable(risk_box, risk, "全仓风险与入场闸门", True)
        self._foldable(summary, summary_body, "当前选择范围", False)
        self._apply_folds()

        controls = self._controls_bar
        self.start_btn = ttk.Button(controls, text="开始/继续所选组合", command=self.start_run, style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(controls, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(controls, text="安全停止", command=self.stop_run, state="disabled")
        self.stop_btn.pack(side="left")
        exports = ttk.Frame(self._controls_bar.master)
        exports.pack(side="bottom", fill="x", pady=(0, 4))
        controls = exports
        self.candidate_btn = ttk.Button(controls, text="从已有CSV生成候选", command=self.start_candidate_export)
        self.candidate_btn.pack(side="left", padx=8)
        self.worst_btn = ttk.Button(controls, text="从CSV重新导出排行榜", command=self.start_legacy_export)
        self.worst_btn.pack(side="left", padx=8)
        ttk.Button(controls, text="打开结果目录", command=self.open_output).pack(side="left", padx=8)
        ttk.Button(controls, text="导出服务器任务", command=self.export_server_campaign).pack(side="left", padx=4)

        # 日志放在底部操作栏之上，用剩下的全部空间；窗口矮的时候它先被压缩，
        # 而不是把操作栏顶出屏幕。height 给小一点，靠 expand 自己长。
        log_frame = ttk.LabelFrame(outer, text="实时日志", style="Card.TLabelframe", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(log_frame, height=6, font=("Consolas", 9), wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_candidate_tab(self, outer):
        mode = ttk.LabelFrame(outer, text="导出方式", style="Card.TLabelframe", padding=12)
        mode.pack(fill="x")
        self.candidate_enabled_var = tk.BooleanVar(value=True)
        self.candidate_auto_var = tk.BooleanVar(value=True)
        self.legacy_ranking_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mode, text="启用候选筛选", variable=self.candidate_enabled_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(mode, text="回测完成后自动生成候选Excel", variable=self.candidate_auto_var).grid(row=0, column=1, sticky="w", padx=24)
        ttk.Checkbutton(mode, text="另外保留自定义最优排行榜 / 最差1000",
                        variable=self.legacy_ranking_var).grid(row=0, column=2, sticky="w")
        ttk.Label(mode, text="完整原始结果始终保存为CSV；候选只分析本轮已跑结果，不改变交易参数、不补跑或换算未跑倍数。",
                  foreground="#7F6000").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ranking_count = ttk.Frame(mode)
        ranking_count.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(ranking_count, text="最优榜导出数量").pack(side="left")
        ttk.Combobox(ranking_count, textvariable=self.rank_top_limit_var, state="readonly",
                     values=TOP_LIMITS, width=9).pack(side="left", padx=8)
        ttk.Label(ranking_count, text="与运行页同步；用于各分类榜及全局榜").pack(side="left")

        plan = ttk.LabelFrame(outer, text="筛选方案与比较范围", style="Card.TLabelframe", padding=12)
        plan.pack(fill="x", pady=(10, 0))
        self.candidate_scheme_var = tk.StringVar(value="RETURN_DRAWDOWN")
        self.candidate_scope_var = tk.StringVar(value="ALL_RUN")
        self.capital_retention_var = tk.DoubleVar(value=90.0)
        ttk.Radiobutton(plan, text="本轮收益回撤优选", variable=self.candidate_scheme_var,
                        value="RETURN_DRAWDOWN").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(plan, text="原严格成本预筛", variable=self.candidate_scheme_var,
                        value="LEGACY_STRESS").grid(row=0, column=1, sticky="w", padx=16)
        self.candidate_new_preset_button = ttk.Button(plan, text="一键使用新优选方案", command=self.use_return_drawdown_preset)
        self.candidate_new_preset_button.grid(row=0, column=2, sticky="w", padx=8)
        ttk.Radiobutton(plan, text="全部已回测倍数", variable=self.candidate_scope_var,
                        value="ALL_RUN").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Radiobutton(plan, text="指定倍数（见下方）", variable=self.candidate_scope_var,
                        value="TARGET").grid(row=1, column=1, sticky="w", padx=16, pady=(7, 0))
        ttk.Label(plan, text="保留最高期末资金比例（%）").grid(row=2, column=0, sticky="w", pady=(7, 0))
        self.capital_retention_entry = ttk.Entry(plan, textvariable=self.capital_retention_var, width=10)
        self.capital_retention_entry.grid(row=2, column=1, sticky="w", padx=16, pady=(7, 0))
        self.candidate_scheme_hint = tk.StringVar()
        ttk.Label(plan, textvariable=self.candidate_scheme_hint, foreground="#7F6000", wraplength=900,
                  justify="left").grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        basis = ttk.LabelFrame(outer, text="比较倍数与旧方案压力假设", style="Card.TLabelframe", padding=12)
        basis.pack(fill="x", pady=10)
        ttk.Label(basis, text="候选比较杠杆").grid(row=0, column=0, sticky="w")
        self.target_leverage_var = tk.DoubleVar(value=5.0)
        self.candidate_leverage_box = ttk.Combobox(basis, textvariable=self.target_leverage_var,
                                                  state="readonly", width=8, values=仓位列表)
        self.candidate_leverage_box.grid(row=0, column=1, sticky="w", padx=(8, 24))
        ttk.Label(basis, text="压力场景往返偏移%（示例）").grid(row=0, column=2, sticky="w")
        self.p95_slippage_var = tk.DoubleVar(value=0.05)
        p95_entry = ttk.Entry(basis, textvariable=self.p95_slippage_var, width=10)
        p95_entry.grid(row=0, column=3, sticky="w", padx=(8, 24))
        ttk.Label(basis, text="较大压力场景%（示例）").grid(row=0, column=4, sticky="w")
        self.extreme_slippage_var = tk.DoubleVar(value=0.10)
        extreme_entry = ttk.Entry(basis, textvariable=self.extreme_slippage_var, width=10)
        extreme_entry.grid(row=0, column=5, sticky="w", padx=8)
        self.candidate_legacy_controls = [p95_entry, extreme_entry]
        ttk.Label(basis, text="压力值是演示假设，请自行校准；旧配置字段名p95保留兼容，不代表统计分位数。",
                  foreground="#7F6000").grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.candidate_leverage_hint = tk.StringVar()
        ttk.Label(basis, textvariable=self.candidate_leverage_hint, foreground="#C00000",
                  wraplength=1100).grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        gates = ttk.LabelFrame(outer, text="门槛与导出数量（灰色项仅旧方案使用）", style="Card.TLabelframe", padding=12)
        gates.pack(fill="x")
        specs = [
            ("回撤A/B标签分界%（非排序）", "preferred_mdd_var", 30.0), ("最大回撤硬上限%", "hard_mdd_var", 40.0),
            ("利润因子PF下限", "profit_factor_min_var", 1.20), ("持仓+等待占时上限%", "capacity_max_var", 70.0),
            ("多单占比下限%", "long_share_min_var", 30.0), ("多单占比上限%", "long_share_max_var", 70.0),
            ("爆仓保护次数上限", "protect_exit_max_var", 10), ("最低实际完整交易数", "min_trades_var", 200),
            ("每类研究候选最多", "per_category_max_var", 50),
            ("实盘名额（待验证，当前不生效）", "live_max_var", 10), ("全局观察最多", "watch_max_var", 20),
        ]
        for index, (label, name, value) in enumerate(specs):
            row, pair = divmod(index, 2)
            col = pair * 3
            ttk.Label(gates, text=label).grid(row=row, column=col, sticky="w", pady=4)
            var = tk.DoubleVar(value=value) if index < 6 else tk.IntVar(value=value)
            setattr(self, name, var)
            entry = ttk.Entry(gates, textvariable=var, width=12)
            entry.grid(row=row, column=col + 1, sticky="w", padx=(8, 28))
            if name in ("capacity_max_var", "long_share_min_var", "long_share_max_var"):
                self.candidate_legacy_controls.append(entry)
            if name == "live_max_var":
                entry.configure(state="disabled")

        advanced = ttk.LabelFrame(outer, text="严格高级验证（当前数据不足时绝不伪算）", style="Card.TLabelframe", padding=12)
        advanced.pack(fill="both", expand=True, pady=10)
        ttk.Label(advanced, justify="left", wraplength=1120, text=
                  "实盘候选还必须通过：样本外Calmar/最大回撤、样本外年化对数收益、DSR、PBO、Newey-West或区块Bootstrap t值、"
                  "正收益月份、参数邻域稳定度、有效独立样本、CVaR、回撤恢复时间和Maker成交率。\n"
                  "资金性停机（余额×名义倍数÷ETH价格低于最小开仓量）和全仓强平属于当前可验证的一票否决项；"
                  "②爆仓保护属于提前退出，可在上方设置允许次数（默认最多10次）。"
                  "持仓+等待占时优先用逐账户实际值；旧数据缺少时标保守估算。它不含新增全退出间隔，不是盘口容量。"
                  "利润因子PF是正的单笔净收益率合计/负的单笔净收益率绝对值合计，不是平均盈亏比。\n"
                  "当前全量CSV只有汇总统计，无法严谨反推出其他高级指标。因此本版会生成“研究候选”和“观察候选”，但“实盘候选”保持空表，"
                  "并逐项列出待验证原因；不会用普通t值冒充Newey-West t值，也不会把PBO伪造成单策略指标。",
                  foreground="#C00000").pack(anchor="w")
        ttk.Button(advanced, text="选择已有结果目录并生成候选", command=self.start_candidate_export,
                   style="Accent.TButton").pack(anchor="w", pady=(12, 0))
        for var in (self.candidate_enabled_var, self.target_leverage_var, self.candidate_scheme_var,
                    self.candidate_scope_var, self.capital_retention_var):
            var.trace_add("write", self.update_candidate_leverage_hint)
        self.update_candidate_leverage_hint()

    def use_return_drawdown_preset(self):
        if self._fingerprint_run_active():
            messagebox.showinfo("正在运行", "回测或导出结束后再切换候选方案。")
            return
        self.candidate_scheme_var.set("RETURN_DRAWDOWN")
        self.candidate_scope_var.set("ALL_RUN")
        self.capital_retention_var.set(90.0)
        self.append_log("候选已切换为本轮收益回撤优选：全部已回测倍数，保留合格最高资金的90%后按回撤升序。"
                        "启用/自动导出开关及交易参数保持不变；尚未导出或保存。")

    def update_candidate_leverage_hint(self, *_args):
        enabled = self.candidate_enabled_var.get()
        target = self.candidate_scope_var.get() == "TARGET"
        legacy = self.candidate_scheme_var.get() == "LEGACY_STRESS"
        self.candidate_leverage_box.configure(state="readonly" if enabled and target else "disabled")
        self.capital_retention_entry.configure(state="disabled" if legacy else "normal")
        for widget in self.candidate_legacy_controls:
            widget.configure(state="normal" if legacy else "disabled")
        self.candidate_leverage_hint.set(
            ("仅筛选CSV中指定的实际倍数；没有跑过该倍数时不换算或补跑。" if target else
             "逐个比较CSV中全部已回测倍数的实际账户结果；下方指定倍数保留但不生效。")
            + ("候选自动筛选未启用，开关保持原值；仍可手动从已有CSV导出。" if not enabled else ""))
        if legacy:
            self.candidate_scheme_hint.set("原严格成本预筛：保留旧年度收益、多空比例、持仓+等待占时及成本压力硬门槛，"
                                           "按原类别内研究预筛分排序。切换方案不改回测参数。")
        else:
            try:
                ratio = f"{self.capital_retention_var.get():g}%"
            except tk.TclError:
                ratio = "所填比例"
            self.candidate_scheme_hint.set(
                f"顺序：先通过硬门槛，再保留期末资金≥合格最高期末资金的{ratio}，最后按回撤升序（同回撤优先资金高）。"
                "硬门槛含真实盈利、平均净收益为正、交易数、PF、回撤、保护次数、无全仓强平/资金性停机；不达标不凑数。"
                "年度、多空比例、持仓+等待占时、成本压力仅提示，不淘汰；仍需样本外验证，不等于可实盘。")

    def _build_round_box(self, outer):
        """轮次控制：一次只放开一个环节，其余四个锁死在已测最优上。

        每轮组合数动态预览；逐轮选择不是全局最优证明，也不承诺固定耗时。
        """
        box = ttk.LabelFrame(outer, text="测试轮次（控制变量法：一次只放开一个环节）",
                             style="Card.TLabelframe", padding=10)
        box.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="一键切换：", font=(self.ui_font_family, 10, "bold"),
                  foreground="#1F4E78").pack(side="left", padx=(0, 6))
        self.round_buttons = {}
        for code, name, _fields, _tab, _note in 基线.轮次列表:
            try:
                counts = self.selection_panel.round_preview(code)
                label = f"{code}轮 {name}\n{counts['包含仓位完整组合数']:,} 组"
            except ValueError:
                label = f"{code}轮 {name}"
            btn = ttk.Button(row, text=label, style="Round.TButton", width=17,
                             command=lambda c=code: self.apply_round(c))
            btn.pack(side="left", padx=3)
            self.round_buttons[code] = btn
        ttk.Button(row, text="基线单点\n1 组（复核用）", width=15, style="Round.TButton",
                   command=lambda: self.apply_round("基线")).pack(side="left", padx=(14, 3))

        self.round_state_var = tk.StringVar(value="当前轮次：未识别")
        ttk.Label(box, textvariable=self.round_state_var,
                  font=(self.ui_font_family, 10, "bold"),
                  foreground="#C00000").pack(anchor="w", pady=(6, 0))

        # 说明文字默认收起：省下的 3 行直接还给下面的日志和操作栏。
        detail = ttk.Frame(box)
        detail.pack(fill="x")
        best = 基线.最优说明
        confirmed = "　｜　".join(
            f"{k}：{best[k]}" for k in ("开仓指标", "止损代码", "止盈方案编号",
                                        "止盈后等待分钟", "仓位倍数"))
        ttk.Label(detail, text="★ 已测最优（锁定值）　" + confirmed,
                  foreground="#C00000", wraplength=1180, justify="left").pack(anchor="w")
        pending = [k for k in 基线.已测最优 if k not in 基线.已确认字段 and k in best]
        if pending:
            ttk.Label(
                detail, foreground="#7F6000", wraplength=1180, justify="left",
                text="⚠ 尚未确认、当前用占位值：" + "　｜　".join(
                    f"{k}：{best[k]}" for k in pending)
                     + "。跑完对应轮次请把结果写回 轮次基线.py 的「已测最优」。",
            ).pack(anchor="w", pady=(3, 0))
        ttk.Label(
            detail, foreground="#888888", wraplength=1180, justify="left",
            text="多周期开仓全部交叉会产生很大的组合量；各轮组合数以界面实时统计为准。"
                 "标记：★＝已测最优，新＝新增未测。",
        ).pack(anchor="w", pady=(4, 0))
        self._foldable(box, detail, "测试轮次说明", True, whole=False)

    def apply_research_group(self, group):
        from second_round import group_config
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("正在运行", "回测或导出结束后再切换测试组。")
            return
        try:
            config = 规范化配置(group_config(self.current_selection(), group))
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        count = 配置统计(config)["包含仓位完整组合数"]
        if not messagebox.askyesno("应用第二轮测试组", f"{group[2]}：{count}组。\n{group[4]}\n\n"
                "将替换组合勾选，设1000 USDC、0.01—20 ETH、开平偏移各0.01%示例。\n"
                "数据路径不变；保护规则和未指定的S3门槛沿用运行页。候选比较设为2x，但不自动启用筛选。\n"
                "只应用配置，不开始回测。继续？"):
            return
        self.selection_panel.apply_config(config)
        self.initial_capital_var.set(1000.)
        self.minimum_eth_var.set(.01)
        self.maximum_eth_var.set(20.)
        self.entry_slippage_var.set(DEFAULT_SLIPPAGE["开仓"] * 100.0)
        self.exit_slippage_var.set(DEFAULT_SLIPPAGE["平仓"] * 100.0)
        self.slippage_preset_var.set("自定义")
        self.update_slippage_label()
        self.selection_panel.set_entry_modes(config["入场触发口径"])
        self.s3_gap_var.set(config["入场约束"]["最小S3距离"] * 100.)
        self.s3_timeframe_var.set(config["入场约束"]["S3基线周期"])
        self.target_leverage_var.set(2.)
        self.mark_new_task()
        self.save_user_settings()
        self.append_log(f"已应用第二轮{group[0]} {group[2]}，{count}组；S3门槛{self.s3_gap_var.get()}%，保护设置沿用运行页。尚未开始。")
        self.notebook.select(1)
        self.refresh_round_state()

    def apply_round(self, code):
        """切换到某一轮，并把其余四个环节锁死在已测最优上。"""
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("正在运行", "回测进行中，停止后再切换轮次。")
            return
        name = 基线.轮次字典[code][0] if code in 基线.轮次字典 else "基线单点"
        note = 基线.轮次字典[code][3] if code in 基线.轮次字典 else "五个环节全部锁定在已测最优上，只跑1组，用于复核基线是否可复现。"
        if not messagebox.askyesno(
            f"切换到 {code}轮 · {name}",
            f"{note}\n\n将覆盖「组合选择」页当前的全部勾选，改为本轮的配置。\n继续吗？",
        ):
            return
        counts = self.selection_panel.apply_round(code)
        self.selection_panel.set_entry_modes("LIVE_01")
        self.mark_new_task()
        self.append_log(
            f"已切换到 {code}轮 · {name}：入场 {counts['入场组合数']:,} × "
            f"止损 {counts['止损组合数']:,}（信号{counts['信号止损数']}×固定{counts['固定止损数']}×时间{counts['时间止损数']}） × "
            f"止盈 {counts['止盈方案数']:,} × "
            f"等待 {counts['等待时间档数']:,} × 仓位 {counts['仓位档数']:,} = "
            f"{counts['包含仓位完整组合数']:,} 组。已自动标记为新任务，结果单独存目录。"
        )
        self.refresh_round_state()

    def refresh_round_state(self):
        """识别当前勾选属于哪一轮，按钮高亮同步。"""
        if not hasattr(self, "round_state_var"):
            return
        try:
            code = self.selection_panel.detect_round()
        except (ValueError, KeyError):
            code = None
        for c, btn in self.round_buttons.items():
            btn.configure(style="RoundOn.TButton" if c == code else "Round.TButton")
        if code:
            name = 基线.轮次字典[code][0]
            self.round_state_var.set(
                f"当前轮次：{code}轮 · {name}　—　其余四个环节已锁定在已测最优上")
        else:
            self.round_state_var.set(
                "当前轮次：自定义（不符合任何一轮的控制变量条件）"
                "　—　要按轮次跑，请点上面的按钮")

    def use_recommended_threads(self):
        self.thread_var.set(推荐线程数())

    def update_thread_hint(self):
        recommended = 推荐线程数()
        total = os.cpu_count() or 20
        try:
            current = int(self.thread_var.get())
            if not 1 <= current <= total:
                raise ValueError("线程数超出范围")
        except (tk.TclError, ValueError):
            self.thread_hint_var.set(f"并发数请填 1~{total} 的整数；默认 {recommended}")
            self.thread_hint.configure(foreground="#C00000")
            return
        self.thread_hint_var.set(
            f"本机 {total} 个逻辑线程；下次启动最多并发 {current} 组任务，预计算按可用内存调整。\n"
            "开仓方法预计算使用独立进程，账户回放使用线程；这两阶段共用此并发上限。\n"
            f"默认 {recommended} 为系统响应留出余量；更多并发不一定更快，以同配置耗时为准。\n"
            "真正决定能不能跑满的是每个开仓批里的并行任务数＝每批组数×方向×会话×位置档×止损×\n"
            "叠加止盈×固定止损×时间止损×等待档。它小于并发数时，多出来的线程一定空转——\n"
            "开始运行后看日志里的“开仓信号分批”那行，它会直接报出这个数。\n"
            "当前逐账户回测仅支持CPU；“自动”和“仅CPU”均不使用GPU。"
        )
        self.thread_hint.configure(foreground="#7F6000")

    def update_selection_summary(self, counts, _text):
        if hasattr(self, "hedge_panel"):
            self.hedge_panel.refresh_context()
        if not hasattr(self, "selection_summary_var"):
            return
        self.update_cost_mode(refresh_summary=False)
        if self.run_target_var.get() == "FINGERPRINTS":
            self._sync_run_target()
            return
        # 手动改勾选之后当前轮次可能就不成立了，这里同步刷新指示灯。
        self.refresh_round_state()
        if counts is None:
            self.selection_summary_var.set("当前组合选择不完整，请到“组合选择”页补齐")
            self.storage_summary_var.set("至少选择一种开仓口径、每周期一种情况、一档位置过滤、一种止损、一种止盈、一档等待时间、一档成本和一档仓位。")
            return
        self.selection_summary_var.set(
            f"入场 {counts['基础入场组合数']:,} × 口径 {counts.get('入场口径档数', 1)}档 × 位置 {counts['开仓位置过滤档数']}档 × "
            f"止损 {counts['止损组合数']:,}（信号{counts['信号止损数']}×固定{counts['固定止损数']}×时间{counts['时间止损数']}） × "
            f"止盈 {counts['止盈方案数']:,} × "
            f"等待 {counts['等待时间档数']:,} × "
            f"成本 {counts.get('成本模式档数', 1)}档 × 仓位 {counts['仓位档数']:,} = {counts['包含仓位完整组合数']:,} 个完整结果"
        )
        from decimal import Decimal
        estimated_gb = 估算CSV字节(counts["不含仓位完整组合数"], counts["仓位档数"]) / Decimal(1024 ** 3)
        estimated_text = format(estimated_gb, ".2f" if estimated_gb < 10**12 else ".3E")
        每行字节 = CSV固定字节 + CSV每仓位档字节 * max(1, counts["仓位档数"])
        self.storage_summary_var.set(
            f"CSV预计 {counts['不含仓位完整组合数']:,} 行 × 约 {每行字节:,} 字节/行"
            f"（{counts['仓位档数']} 档仓位；每多一档约多 {CSV每仓位档字节} 字节）≈ {estimated_text}GB；"
            "按历史结果实测拟合，实际大小取决于数值长度。"
        )

    def _foldable(self, box, body, key, default_folded=False, whole=True):
        """登记一个可折叠分区。收起时整个 LabelFrame 隐藏，不只是内容。

        原来只隐藏内层 Frame，LabelFrame 的标题行和"收起"勾选框还在，
        五个分区收起后仍占掉约250像素——1080×720 下汇总行和日志会被挤没。
        现在开关统一放在顶部一条"显示区块"栏里，收起＝整块 pack_forget，
        真正还出高度；重排时按登记顺序重新 pack，顺序不会乱。
        """
        var = tk.BooleanVar(value=default_folded)
        self.fold_vars[key] = var
        self._fold_bodies[key] = body
        if whole:
            self._fold_boxes[key] = box
            self._fold_order.append(key)
            # 变量沿用"True＝收起"（用户设置里存的就是这个语义），但勾选框反过来显示：
            # 打勾＝显示这一块，符合"显示区块"的字面意思。
            ttk.Checkbutton(self._fold_bar, text=key, variable=var,
                            onvalue=False, offvalue=True,
                            command=self._apply_folds).pack(side="left", padx=(0, 12))
        else:
            # 只收内容、保留外框：轮次面板的按钮行和汇总行必须一直看得见。
            header = ttk.Frame(box)
            header.pack(fill="x", before=body)
            ttk.Checkbutton(header, text="收起说明", variable=var,
                            command=self._apply_folds).pack(side="right")
            self._fold_headers[key] = header
        return var

    def _apply_folds(self, only=None):
        """按登记顺序重排分区：登记为整块的收起时 pack_forget 整个 LabelFrame。"""
        for key in self._fold_order:
            try:
                self._fold_boxes[key].pack_forget()
            except tk.TclError:
                pass
        for key in self._fold_order:
            if self.fold_vars[key].get() or (self.run_target_var.get() == "FINGERPRINTS"
                                             and key in ("资金与下单约束", "全仓风险与入场闸门")):
                continue
            try:
                self._fold_bodies[key].pack(fill="both", expand=True)
                self._fold_boxes[key].pack(in_=self._sections, fill="x", pady=(0, 8))
            except tk.TclError:
                pass
        for key, body in self._fold_bodies.items():
            if key in self._fold_boxes:
                continue
            try:
                if self.fold_vars[key].get():
                    body.pack_forget()
                else:
                    body.pack(fill="both", expand=True)
            except tk.TclError:
                pass

    def _path_row(self, parent, row, label, variable, command):
        ttk.Label(parent, text=label, width=14).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Button(parent, text="浏览…", command=command).grid(row=row, column=2)
        parent.columnconfigure(1, weight=1)

    def choose_csv(self):
        value = filedialog.askopenfilename(title="选择1分钟K线", filetypes=[("数据文件", "*.csv *.parquet *.pq"), ("CSV", "*.csv"), ("Parquet", "*.parquet *.pq"), ("所有文件", "*.*")])
        if value: self.csv_var.set(value)

    def choose_out(self):
        value = filedialog.askdirectory(title="选择结果保存目录")
        if value:
            self.out_var.set(value)
            self.mark_new_task()

    def mark_new_task(self):
        if self.proc and self.proc.poll() is None:
            return
        self.current_output_dir = None
        if hasattr(self, "active_output_var"):
            self.active_output_var.set("当前任务目录：下次开始时自动创建")

    def _entry_mode_changed(self, *_args):
        try:
            mode = tuple(self.selection_panel.get_entry_modes())
        except ValueError:
            mode = ()
        if mode != self._entry_mode_code:
            self._entry_mode_code = mode
            if not self._fingerprint_run_active():
                self.mark_new_task()

    def show_entry_rules(self):
        if self._fingerprint_run_active():
            messagebox.showinfo("正在运行", "回测或导出结束后再修改入场规则。")
            return
        self.notebook.select(self.select_tab)
        if self.run_target_var.get() == "FINGERPRINTS":
            self.fingerprint_status_var.set("当前运行指纹精确列表：点击列表行查看它的入场规则。若要修改手动勾选，请先切换到编辑组合。")
            return
        self.selection_panel.show_entry_constraints()

    def output_dir(self) -> Path:
        if self._active_job_id is not None and self._active_output_dir is not None:
            return self._active_output_dir
        return self.current_output_dir or Path(self.out_var.get().strip())

    def resolve_output_dir(self) -> Path:
        if self.current_output_dir is not None:
            return self.current_output_dir
        root = Path(self.out_var.get().strip())
        if not self.separate_output_var.get():
            self.current_output_dir = root
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            candidate = root / f"回测_{stamp}"
            suffix = 2
            while candidate.exists():
                candidate = root / f"回测_{stamp}_{suffix}"
                suffix += 1
            self.current_output_dir = candidate
        self.active_output_var.set(f"当前任务目录：{self.current_output_dir}")
        return self.current_output_dir

    def detect_gpu(self):
        try:
            p = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=8)
            gpu = p.stdout.strip() if p.returncode == 0 else "未检测到"
        except Exception:
            gpu = "未检测到"
        self.messages.put({"type": "hardware", "gpu": gpu})

    def apply_slippage_preset(self, _event=None):
        values = {
            DEFAULT_SLIPPAGE_PRESET: (DEFAULT_SLIPPAGE["开仓"] * 100.0, DEFAULT_SLIPPAGE["平仓"] * 100.0),
            "旧版示例偏移": (LEGACY_DEFAULT_SLIPPAGE["开仓"] * 100.0, LEGACY_DEFAULT_SLIPPAGE["平仓"] * 100.0),
            "指定总偏移0.05%": (0.0, 0.05),
            "不计成交偏移": (0.0, 0.0),
        }
        if self.slippage_preset_var.get() in values:
            entry, exit_ = values[self.slippage_preset_var.get()]
            self.entry_slippage_var.set(entry); self.exit_slippage_var.set(exit_)
        self.update_slippage_label()

    def slippage_edited(self, _event=None):
        self.slippage_preset_var.set("自定义")
        self.update_slippage_label()

    def update_slippage_label(self):
        try:
            total = float(self.entry_slippage_var.get()) + float(self.exit_slippage_var.get())
            self.roundtrip_slippage_var.set(f"往返合计 {total:.5f}%（每笔完整交易扣一次）")
        except (tk.TclError, ValueError):
            self.roundtrip_slippage_var.set("请输入有效百分比")

    def get_cost_modes(self):
        return cost_modes([code for code, var in self.cost_mode_vars.items() if var.get()])

    def set_cost_modes(self, value, *, update_controls=True):
        normalized = normalize_cost_mode(value)
        selected = cost_modes(normalized)
        for code, var in self.cost_mode_vars.items():
            var.set(code in selected)
        if update_controls:
            self.update_cost_mode()

    def update_cost_mode(self, *, refresh_summary=True):
        selected = [code for code, var in self.cost_mode_vars.items() if var.get()]
        self.cost_mode_var.set(selected[0] if len(selected) == 1 else
                               "分别回测2档：SLIPPAGE / FEE" if selected else "未选择成本档位")
        if tuple(selected) != self._cost_mode_codes:
            self._cost_mode_codes = tuple(selected)
            if not self._fingerprint_run_active():
                self.mark_new_task()
        if not hasattr(self, "fee_controls"):
            return
        for widgets, enabled in ((self.slippage_controls, "SLIPPAGE" in selected), (self.fee_controls, "FEE" in selected)):
            for widget in widgets:
                widget.configure(state=("readonly" if isinstance(widget, ttk.Combobox) else "normal")
                                 if enabled else "disabled")
        self.cost_mode_hint_var.set("两档分别回测，不叠加扣费" if len(selected) == 2 else
                                    "当前只计手续费" if selected == ["FEE"] else
                                    "当前只计成交偏移" if selected else "请至少选择一种成本模式")
        self.update_fee_label()
        if refresh_summary:
            self.selection_panel._update_summary()

    def apply_fee_preset(self, _event=None):
        rates = FEE_PRESETS.get(self.fee_preset_var.get())
        if rates is not None:
            self.entry_fee_var.set(rates[0] * 100.0)
            self.exit_fee_var.set(rates[1] * 100.0)
        self.update_fee_label()

    def current_execution_settings(self):
        selected = self.get_cost_modes()
        try:
            raw = {
                "成本模式": selected[0] if len(selected) == 1 else selected,
                "平仓后最小开仓间隔分钟": self.min_reentry_minutes_var.get(),
                "手续费": {"开仓费率": float(self.entry_fee_var.get()) / 100.0,
                           "平仓费率": float(self.exit_fee_var.get()) / 100.0,
                           "BNB抵扣": self.bnb_discount_var.get(),
                           "返佣比例": float(self.fee_rebate_var.get()) / 100.0},
            }
        except (tk.TclError, ValueError, OverflowError) as exc:
            raise ValueError("手续费、返佣和等待必须填写有效数字；基础费率填未折扣百分数") from exc
        return normalize_execution_settings(raw)

    def update_fee_label(self):
        try:
            raw = {"成本模式": "FEE" if self.cost_mode_vars["FEE"].get() else "SLIPPAGE", "手续费": {
                "开仓费率": float(self.entry_fee_var.get()) / 100.0,
                "平仓费率": float(self.exit_fee_var.get()) / 100.0,
                "BNB抵扣": self.bnb_discount_var.get(), "返佣比例": float(self.fee_rebate_var.get()) / 100.0}}
            entry, exit_ = effective_fee_rates(raw)
            self.net_fee_label_var.set((f"手续费档净费率：开{entry * 100:.5f}% / 平{exit_ * 100:.5f}%"
                                       + ("；当前净费率为0，请确认零费或返佣设置；不会自动补吃单费。"
                                          if entry == 0.0 and exit_ == 0.0 else ""))
                                      if self.cost_mode_vars["FEE"].get() else "手续费档未选，本次不扣手续费")
            rates = (raw["手续费"]["开仓费率"], raw["手续费"]["平仓费率"])
            self.fee_preset_var.set(next((name for name, pair in FEE_PRESETS.items() if rates == pair), "自定义"))
        except (tk.TclError, ValueError, OverflowError):
            self.net_fee_label_var.set("请输入有效费率及0%—100%返佣比例")

    def apply_execution_settings(self, selection):
        config = normalize_execution_settings(selection)
        self.set_cost_modes(config["成本模式"], update_controls=False)
        self.min_reentry_minutes_var.set(str(config["平仓后最小开仓间隔分钟"]))
        fees = config["手续费"]
        self.entry_fee_var.set(fees["开仓费率"] * 100.0)
        self.exit_fee_var.set(fees["平仓费率"] * 100.0)
        self.bnb_discount_var.set(fees["BNB抵扣"])
        self.fee_rebate_var.set(fees["返佣比例"] * 100.0)
        self.update_cost_mode()

    def current_candidate_settings(self):
        try:
            raw = {
                "启用": self.candidate_enabled_var.get(),
                "自动导出": self.candidate_auto_var.get(),
                "筛选方案": self.candidate_scheme_var.get(),
                "比较范围": self.candidate_scope_var.get(),
                "资金保留比例": self.capital_retention_var.get() / 100.0,
                "统一目标杠杆": self.target_leverage_var.get(),
                "p95往返偏移": self.p95_slippage_var.get() / 100.0,
                "极端往返偏移": self.extreme_slippage_var.get() / 100.0,
                "最大回撤优选": self.preferred_mdd_var.get() / 100.0,
                "最大回撤硬上限": self.hard_mdd_var.get() / 100.0,
                "盈亏比下限": self.profit_factor_min_var.get(),
                "容量占用率上限": self.capacity_max_var.get() / 100.0,
                "多单占比下限": self.long_share_min_var.get() / 100.0,
                "多单占比上限": self.long_share_max_var.get() / 100.0,
                "爆仓保护次数上限": self.protect_exit_max_var.get(),
                "最低原始交易数": self.min_trades_var.get(),
                "每类最多": self.per_category_max_var.get(),
                "全局实盘最多": self.live_max_var.get(),
                "全局观察最多": self.watch_max_var.get(),
                "导出旧排行": self.legacy_ranking_var.get(),
            }
        except (tk.TclError, ValueError):
            raise ValueError("候选筛选参数必须填写有效数字")
        return 规范化候选筛选(raw)

    def apply_candidate_settings(self, settings):
        value = 规范化候选筛选(settings)
        self.candidate_enabled_var.set(value["启用"])
        self.candidate_auto_var.set(value["自动导出"])
        self.candidate_scheme_var.set(value["筛选方案"])
        self.candidate_scope_var.set(value["比较范围"])
        self.capital_retention_var.set(value["资金保留比例"] * 100.0)
        self.target_leverage_var.set(value["统一目标杠杆"])
        self.p95_slippage_var.set(value["p95往返偏移"] * 100.0)
        self.extreme_slippage_var.set(value["极端往返偏移"] * 100.0)
        self.preferred_mdd_var.set(value["最大回撤优选"] * 100.0)
        self.hard_mdd_var.set(value["最大回撤硬上限"] * 100.0)
        self.profit_factor_min_var.set(value["盈亏比下限"])
        self.capacity_max_var.set(value["容量占用率上限"] * 100.0)
        self.long_share_min_var.set(value["多单占比下限"] * 100.0)
        self.long_share_max_var.set(value["多单占比上限"] * 100.0)
        self.protect_exit_max_var.set(value["爆仓保护次数上限"])
        self.min_trades_var.set(value["最低原始交易数"])
        self.per_category_max_var.set(value["每类最多"])
        self.live_max_var.set(value["全局实盘最多"])
        self.watch_max_var.set(value["全局观察最多"])
        self.legacy_ranking_var.set(value["导出旧排行"])

    def current_selection(self):
        raw = self.selection_panel.get_config()
        try:
            entry = float(self.entry_slippage_var.get()) / 100.0
            exit_ = float(self.exit_slippage_var.get()) / 100.0
        except (tk.TclError, ValueError):
            raise ValueError("成交偏移必须填写数字")
        raw["成交偏移"] = {"开仓": entry, "平仓": exit_}
        raw.update(self.current_execution_settings())
        modes = {v: k for k, v in 成交价格口径选项.items()}
        if self.fill_mode_var.get() not in modes:
            raise ValueError("请选择有效的成交价格口径")
        raw["成交价格口径"] = modes[self.fill_mode_var.get()]
        try:
            initial_capital = float(self.initial_capital_var.get())
            minimum_eth = float(self.minimum_eth_var.get())
            maximum_eth = float(self.maximum_eth_var.get())
        except (tk.TclError, ValueError):
            raise ValueError("初始资金、ETH最小和最大开仓数量必须填写数字")
        try:
            protect_ratio = float(self.protect_ratio_var.get()) / 100.0
            maintenance_rate = float(self.maintenance_rate_var.get()) / 100.0
            s3_gap = float(self.s3_gap_var.get()) / 100.0
            funding_rate = float(self.funding_rate_var.get()) / 100.0
        except (tk.TclError, ValueError):
            raise ValueError("保护止损比例、维持保证金率、最小S3距离和固定资金费假设必须填写数字")
        raw["资金约束"] = {
            "初始资金USDC": initial_capital,
            "最小开仓数量ETH": minimum_eth,
            "最大开仓数量ETH": maximum_eth,
            "低于最小数量停止": self.stop_when_too_small_var.get(),
            "保护止损浮亏比例": protect_ratio,
            "维持保证金率": maintenance_rate,
            "启用全仓强平": self.cross_liquidation_var.get(),
            "资金费率": funding_rate,
        }
        raw["入场约束"] = {
            "最小S3距离": s3_gap,
            "S3基线周期": self.s3_timeframe_var.get(),
        }
        raw["候选筛选"] = self.current_candidate_settings()
        require_selection_allowed(raw)
        return 规范化配置(raw)

    def add_ranking_filter(self, metric="交易次数（单）", condition="最低值", value=""):
        row = ttk.Frame(self.ranking_filters_body)
        row.pack(fill="x", pady=1)
        metric_var = tk.StringVar(value=metric if metric in 排行指标选项 else "交易次数（单）")
        condition_var = tk.StringVar(value=condition if condition in ("最低值", "最高值") else "最低值")
        value_var = tk.StringVar(value=str(value))
        ttk.Combobox(row, textvariable=metric_var, state="readonly", width=30,
                     values=list(排行指标选项)).pack(side="left")
        ttk.Combobox(row, textvariable=condition_var, state="readonly", width=8,
                     values=("最低值", "最高值")).pack(side="left", padx=5)
        ttk.Entry(row, textvariable=value_var, width=12).pack(side="left")
        record = {"frame": row, "指标": metric_var, "条件": condition_var, "输入值": value_var}
        ttk.Button(row, text="删除", width=6,
                   command=lambda: self.remove_ranking_filter(record)).pack(side="left", padx=5)
        self.ranking_filter_rows.append(record)

    def remove_ranking_filter(self, record):
        if record in self.ranking_filter_rows:
            self.ranking_filter_rows.remove(record)
            record["frame"].destroy()

    def current_ranking_settings(self):
        filters = []
        for record in self.ranking_filter_rows:
            text = record["输入值"].get().strip()
            if not text:
                continue
            metric = record["指标"].get()
            condition = record["条件"].get()
            try:
                display_value = float(text)
            except ValueError as exc:
                raise ValueError(f"排行榜门槛“{metric}”必须填写数字") from exc
            column_name = 排行指标选项[metric][0]
            raw_value = display_value / 100.0 if "（%）" in column_name else display_value
            filters.append({"指标": metric, "条件": condition, "值": raw_value,
                            "输入值": display_value})
        result = {"排行指标": self.rank_metric_var.get(), "门槛": filters}
        limit = top_limit({"最优名额": int(self.rank_top_limit_var.get())})
        if limit != 5000:
            result["最优名额"] = limit
        return result

    def apply_ranking_settings(self, settings):
        for record in list(self.ranking_filter_rows):
            self.remove_ranking_filter(record)
        source = settings or {}
        self.rank_top_limit_var.set(str(top_limit(source)))
        metric = str(source.get("排行指标") or "")
        if metric in 排行指标选项:
            self.rank_metric_var.set(metric)
        for item in source.get("门槛", []):
            name = str(item.get("指标") or "")
            if name not in 排行指标选项:
                continue
            display = item.get("输入值")
            if display is None:
                display = float(item.get("值", 0.0))
                if "（%）" in 排行指标选项[name][0]:
                    display *= 100.0
            self.add_ranking_filter(name, str(item.get("条件") or "最低值"), f"{display:g}")
        if not self.ranking_filter_rows:
            self.add_ranking_filter("交易次数（单）", "最低值", "")
            self.add_ranking_filter("最大回撤（%）越小越好", "最高值", "")

    def save_user_settings(self):
        if getattr(self, "_settings_restore_error", False):
            return False
        try:
            start, end = self.current_date_range()
            payload = {
                "组合选择": self.current_selection(),
                "成交偏移预设": self.slippage_preset_var.get(),
                # 路径每次都被硬编码的 H 盘默认值盖掉，改过一次就该记住。
                "数据CSV": self.csv_var.get().strip(),
                "微结构CSV": self.micro_csv_var.get().strip(),
                "资金费数据": self.funding_var.get().strip(),
                "OI数据": self.oi_var.get().strip(),
                "数据包ZIP": self.bundle_var.get().strip(),
                "回测开始日期": start,
                "回测结束日期": end,
                "结果根目录": self.out_var.get().strip(),
                "折叠状态": {k: bool(v.get()) for k, v in self.fold_vars.items()},
                "排行指标": self.rank_metric_var.get(),
                "排行榜设置": self.current_ranking_settings(),
                "CPU线程数": self.thread_var.get(),
                "计算设备": self.device_var.get(),
                "新任务独立目录": self.separate_output_var.get(),
                "多指纹策略列表": self.fingerprint_queue,
                "运行对象": self.run_target_var.get(),
                "指纹交互版本": 154,
            }
            用户设置文件.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(用户设置文件, payload)
            return True
        except (OSError, ValueError, tk.TclError, OverflowError) as exc:
            if hasattr(self, "log"):
                self.append_log(f"设置未保存，上一份设置保持不变：{exc}")
            return False

    def load_user_settings(self):
        try:
            if 用户设置文件.is_file():
                payload = json.loads(用户设置文件.read_text("utf-8"))
            else:
                root = Path(默认输出)
                candidates = list(root.rglob("组合选择.json")) if root.is_dir() else []
                latest = max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
                if latest is None:
                    return
                payload = {"组合选择": json.loads(latest.read_text("utf-8-sig")), "成交偏移预设": "Maker均值"}
            retired_queue = []
            self.fingerprint_queue = self._validated_fingerprint_queue(
                payload.get("多指纹策略列表", []), removed=retired_queue)
            if retired_queue:
                self.append_log(f"已从保存的指纹列表移除{len(retired_queue)}条含永久删除方法的策略："
                                + "、".join(item["fingerprint"] for item in retired_queue))
            self._refresh_fingerprint_queue()
            saved_csv = str(payload.get("数据CSV") or "").strip()
            saved_out = str(payload.get("结果根目录") or "").strip()
            start, end = self._validate_date_range(payload.get("回测开始日期", ""), payload.get("回测结束日期", ""))
            if "数据CSV" in payload:
                self.csv_var.set(saved_csv)
            self.micro_csv_var.set(str(payload.get("微结构CSV") or ""))
            self.funding_var.set(str(payload.get("资金费数据") or ""))
            self.oi_var.set(str(payload.get("OI数据") or ""))
            self.bundle_var.set(str(payload.get("数据包ZIP") or ""))
            self.start_date_var.set(start)
            self.end_date_var.set(end)
            if saved_out:
                self.out_var.set(saved_out)
            self._mark_data_dirty()
            saved_threads = max(1, min(os.cpu_count() or 20,
                                       int(payload.get("CPU线程数", self.thread_var.get()))))
            self.thread_var.set(saved_threads)
            saved_device = 旧设备名.get(str(payload.get("计算设备") or ""),
                                        str(payload.get("计算设备") or ""))
            if saved_device in 计算设备选项:
                self.device_var.set(saved_device)
            if "新任务独立目录" in payload:
                self.separate_output_var.set(bool(payload["新任务独立目录"]))
            for key, folded in (payload.get("折叠状态") or {}).items():
                if key in self.fold_vars:
                    self.fold_vars[key].set(bool(folded))
            self._apply_folds()
            metric = str(payload.get("排行指标") or "").strip()
            if metric in 排行指标选项:
                self.rank_metric_var.set(metric)
            self.apply_ranking_settings(payload.get("排行榜设置") or {"排行指标": metric, "门槛": []})
            raw_config = dict(payload.get("组合选择") or {})
            old_entry_mode = raw_config.get("入场触发口径")
            if isinstance(old_entry_mode, str) and old_entry_mode in 入场显示代码:
                raw_config["入场触发口径"] = 入场显示代码[old_entry_mode]
            policy_report = prepare_editor_selection(raw_config)
            config = 规范化配置(policy_report["selection"])
            self.selection_panel.apply_config(config)
            if policy_report["message"]:
                self.selection_panel.policy_notice_var.set(policy_report["message"])
                self.append_log(policy_report["message"])
            slip = config["成交偏移"]
            old_version = int((payload.get("组合选择") or {}).get("版本", 0))
            migrated_slippage = (old_version < 19 and slip == LEGACY_DEFAULT_SLIPPAGE
                                and DEFAULT_SLIPPAGE != LEGACY_DEFAULT_SLIPPAGE)
            if migrated_slippage:
                slip = dict(DEFAULT_SLIPPAGE)
                self.append_log(
                    f"旧默认偏移已更新为演示参数：开{slip['开仓'] * 100:.8f}% / 平{slip['平仓'] * 100:.8f}%；"
                    "请自行校准；自定义偏移保持原值。")
            self.fill_mode_var.set(成交价格口径选项[config["成交价格口径"]])
            self.entry_slippage_var.set(slip["开仓"] * 100.0)
            self.exit_slippage_var.set(slip["平仓"] * 100.0)
            preset = payload.get("成交偏移预设", "自定义")
            if preset == "Maker均值":
                preset = "旧版示例偏移"
            self.slippage_preset_var.set(DEFAULT_SLIPPAGE_PRESET if migrated_slippage else preset)
            self.update_slippage_label()
            self.apply_execution_settings(config)
            funds = config["资金约束"]
            self.initial_capital_var.set(funds["初始资金USDC"])
            self.minimum_eth_var.set(funds["最小开仓数量ETH"])
            self.maximum_eth_var.set(funds["最大开仓数量ETH"])
            self.stop_when_too_small_var.set(funds["低于最小数量停止"])
            self.protect_ratio_var.set(funds["保护止损浮亏比例"] * 100.0)
            self.maintenance_rate_var.set(funds["维持保证金率"] * 100.0)
            self.cross_liquidation_var.set(funds["启用全仓强平"])
            self.funding_rate_var.set(funds["资金费率"] * 100.0)
            limits = config["入场约束"]
            self.s3_gap_var.set(limits["最小S3距离"] * 100.0)
            self.s3_timeframe_var.set(limits["S3基线周期"])
            self.selection_panel.set_entry_modes(config["入场触发口径"])
            candidate = dict(config["候选筛选"])
            if (candidate["启用"] and candidate["比较范围"] == "TARGET"
                    and candidate["统一目标杠杆"] not in config["仓位倍数"]):
                available = [value for value in 仓位列表 if value in config["仓位倍数"]]
                if available:
                    candidate["统一目标杠杆"] = available[0]
                    self.append_log(f"候选统一目标杠杆已自动匹配上次勾选的 {available[0]:g}x。")
                else:
                    candidate["启用"] = False
                    self.append_log("上次组合未勾选有效杠杆，已暂时关闭候选筛选；组合选择保持不变。")
            self.apply_candidate_settings(candidate)
            if candidate["筛选方案"] == "LEGACY_STRESS":
                self.append_log("已保留原严格成本预筛方案；如需本轮收益回撤优选，可到候选页一键切换。自动导出开关保持原值。")
            target = payload.get("运行对象", "EDITOR") if payload.get("指纹交互版本") == 154 else "EDITOR"
            if payload.get("指纹交互版本") != 154 and payload.get("运行对象") == "FINGERPRINTS":
                self.append_log("旧版自动逐条模式已恢复为编辑组合；现有勾选未改动。添加指纹后将同步兼容条件，精确列表可手动选择。")
            if target not in ("EDITOR", "FINGERPRINTS"):
                raise ValueError("保存的运行对象无效，请明确选择编辑组合或指纹列表")
            self.run_target_var.set(target)
            self._sync_run_target()
            self._settings_restore_error = False
            self.save_user_settings()
        except (OSError, json.JSONDecodeError, ValueError, tk.TclError) as exc:
            self._settings_restore_error = True
            self.append_log(f"上次组合选择无法恢复，原设置文件保持不变：{exc}")
            messagebox.showerror("上次配置需要修正", f"{exc}\n\n原设置文件保持不变。请核对配置；明确选择参数并开始新任务后才保存新设置。")

    def append_log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {text}\n")
        # 一次长任务能刷出几万行；Text 里堆满之后整个界面会越来越卡，
        # 所以只留最近这些行，前面的直接丢掉。完整记录本来就在结果目录里。
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 日志最大行数:
            self.log.delete("1.0", f"{lines - 日志最大行数 + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def validate_paths(self, out: Path):
        src_text = self.csv_var.get().strip()
        bundle_text = self.bundle_var.get().strip()
        for label, raw in (("主K线", src_text), ("ZIP", bundle_text)):
            if raw and not Path(raw).is_file():
                messagebox.showerror("数据错误", f"找不到{label}文件：{raw}\n显式路径优先；使用ZIP内K线时请清空无效的主K线路径。")
                self.notebook.select(0)
                return False
        if not (src_text and Path(src_text).is_file()) and not (bundle_text and Path(bundle_text).is_file()):
            messagebox.showerror("数据错误", "必须选择1分钟K线CSV/Parquet，或选择包含klines_1m的数据包ZIP。")
            self.notebook.select(0)
            return False
        for label, raw in (("成交微结构", self.micro_csv_var.get().strip()), ("资金费", self.funding_var.get().strip()), ("OI", self.oi_var.get().strip())):
            if raw and not Path(raw).is_file():
                messagebox.showerror("数据错误", f"找不到{label}数据文件：{raw}")
                self.notebook.select(0)
                return False
        if self.data_report:
            self.selection_panel.set_data_capabilities(self.data_report.get("capabilities", {}), True,
                timeframe_capabilities=self.data_report.get("capabilities_by_timeframe", {}))
            availability = self.selection_panel.last_availability_report
            if availability["message"]:
                self.append_log(availability["message"])
            if not availability["runnable"]:
                messagebox.showerror("没有可运行的开仓规则", availability["message"])
                self.notebook.select(1)
                return False
        try:
            selection = self.current_selection()
            counts = 配置统计(selection)
            self.current_date_range()
        except ValueError as exc:
            messagebox.showerror("组合选择不完整", str(exc))
            self.notebook.select(1)
            return False
        try:
            threads = int(self.thread_var.get())
            if not 1 <= threads <= max(1, os.cpu_count() or 1):
                raise ValueError("线程数超出范围")
        except (ValueError, tk.TclError):
            messagebox.showerror("并发设置错误", f"CPU并发数必须是1到{os.cpu_count() or 1}之间的整数")
            return False
        candidate = selection["候选筛选"]
        if (candidate["启用"] and candidate["比较范围"] == "TARGET"
                and candidate["统一目标杠杆"] not in selection["仓位倍数"]):
            messagebox.showerror(
                "缺少统一目标杠杆",
                f"候选排名选择了 {candidate['统一目标杠杆']:g}x，但组合选择中没有勾选该杠杆。\n\n"
                "请到“组合选择”勾选这一档，或在“候选筛选与导出”更换统一目标杠杆。",
            )
            self.notebook.select(3)
            return False
        out.mkdir(parents=True, exist_ok=True)
        checkpoint_path = out / "断点记录.json"
        if checkpoint_path.exists() and not self.force_var.get():
            try:
                checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
                existing_signature = checkpoint.get("selection_signature")
                if existing_signature is None:
                    messagebox.showerror(
                        "旧版断点不能续跑",
                        "这个结果目录的旧版断点不包含止盈后等待时间。\n\n请选择新的结果目录；如果确定不要旧结果，也可勾选“删除原断点并从头重新计算”。",
                    )
                    return False
                if existing_signature != 配置签名(selection):
                    messagebox.showerror(
                        "不能混用断点",
                        "这个结果目录已有另一套组合的断点。\n\n请更换结果保存目录；如果确定不要旧结果，也可勾选“删除原断点并从头重新计算”。",
                    )
                    return False
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                messagebox.showerror("断点读取失败", f"无法确认已有断点的组合：{exc}")
                return False
        free = shutil.disk_usage(out).free
        from decimal import Decimal
        estimated_gb = 估算CSV字节(counts["不含仓位完整组合数"], counts["仓位档数"]) / Decimal(1024 ** 3)
        recommended_gb = max(Decimal(1), estimated_gb * Decimal("1.15"))
        if free < recommended_gb * 1024 ** 3:
            estimated_text = format(estimated_gb, ".1f" if estimated_gb < 10**12 else ".3E")
            recommended_text = format(recommended_gb, ".1f" if recommended_gb < 10**12 else ".3E")
            return messagebox.askyesno("硬盘空间提醒",
                f"当前磁盘剩余约{free / 1024**3:.1f}GB，所选组合CSV估算约{estimated_text}GB，"
                f"建议至少保留{recommended_text}GB。\n仍然开始吗？")
        return True

    def start_run(self):
        if self._fingerprint_run_active(): return
        if self.run_target_var.get() == "FINGERPRINTS":
            self.start_fingerprint_batch()
            return
        if self.current_output_dir is not None and self.current_output_dir == getattr(self, "_last_fingerprint_batch_dir", None):
            self.mark_new_task()
        out = self.resolve_output_dir()
        if not self.validate_paths(out): return
        try:
            ranking_settings = self.current_ranking_settings()
        except ValueError as exc:
            messagebox.showerror("排行榜门槛错误", str(exc))
            return
        if self.force_var.get() and (out / "断点记录.json").exists():
            if not messagebox.askyesno("确认从头重算", "将删除该结果目录内本程序生成的CSV、断点和排行文件。确定吗？"):
                return
        selection_path = out / "组合选择.json"
        selection_path.write_text(json.dumps(self.current_selection(), ensure_ascii=False, indent=2), "utf-8")
        ranking_path = out / "排行榜设置.json"
        ranking_path.write_text(json.dumps(ranking_settings, ensure_ascii=False, indent=2), "utf-8")
        self._settings_restore_error = False
        self.save_user_settings()
        cmd = [sys.executable, "-X", "utf8", str(项目目录 / "backtest_worker.py"),
               "--rank-metric", self.rank_metric_var.get(),
               "--ranking-settings", str(ranking_path),
               "--csv", self.csv_var.get().strip(),
               "--micro-csv", self.micro_csv_var.get().strip(),
               "--funding", self.funding_var.get().strip(),
               "--oi", self.oi_var.get().strip(),
               "--bundle", self.bundle_var.get().strip(),
               "--output", str(out),
               "--threads", str(self.thread_var.get()),
               "--device", 设备参数.get(self.device_var.get(), "auto"),
               "--cache-root", str(Path(self.out_var.get().strip()) / "_共享指标缓存"),
                "--selection", str(selection_path)]
        start, end = self.current_date_range()
        if start: cmd.extend(("--start", start))
        if end: cmd.extend(("--end", end))
        if self.force_var.get(): cmd.append("--restart")
        self.append_log("启动工作进程：" + " ".join(cmd))
        self.append_log(f"本次结果独立保存到：{out}")
        if not self._launch_process(cmd, out, "backtest"):
            return
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        self.worst_btn.configure(state="disabled")
        self.candidate_btn.configure(state="disabled")
        self.notebook.tab(0, state="disabled")
        self.notebook.tab(1, state="disabled")
        self.notebook.tab(3, state="disabled")
        self.status_var.set("工作进程已启动，正在检查共享指标缓存……")

    def start_candidate_export(self):
        if self.proc and self.proc.poll() is None: return
        initial = str(self.output_dir())
        chosen = filedialog.askopenfilename(title="选择历史的“全部回测结果.csv”", initialdir=initial,
                                            filetypes=(("回测结果CSV", "*.csv"), ("所有文件", "*.*")))
        if not chosen: return
        source = Path(chosen)
        out = source.parent
        self.current_output_dir = out
        self.active_output_var.set(f"当前任务目录：{out}")
        if not source.is_file():
            messagebox.showerror("结果错误", f"所选目录没有找到：\n{source}")
            return
        try:
            settings = self.current_candidate_settings()
        except ValueError as exc:
            messagebox.showerror("候选筛选参数错误", str(exc))
            self.notebook.select(3)
            return
        settings_path = out / "候选筛选设置.json"
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
        stop_flag = out / "控制" / "停止候选导出.flag"
        if stop_flag.exists(): stop_flag.unlink()
        cmd = [sys.executable, "-X", "utf8", str(项目目录 / "candidate_export.py"),
               "--output", str(out), "--source", str(source), "--settings", str(settings_path)]
        self.append_log("开始扫描已有CSV生成分层候选：" + str(source))
        if not self._launch_process(cmd, out, "candidate"):
            return
        self.start_btn.configure(state="disabled")
        self.worst_btn.configure(state="disabled")
        self.candidate_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.notebook.tab(0, state="disabled")
        self.notebook.tab(1, state="disabled")
        self.notebook.tab(3, state="disabled")
        self.status_var.set("正在扫描已有CSV并生成候选……")

    def start_legacy_export(self):
        if self.proc and self.proc.poll() is None: return
        initial = str(self.output_dir())
        chosen = filedialog.askopenfilename(title="选择任意历史的“全部回测结果.csv”", initialdir=initial,
                                            filetypes=(("回测结果CSV", "*.csv"), ("所有文件", "*.*")))
        if not chosen: return
        source = Path(chosen)
        out = source.parent
        self.current_output_dir = out
        self.active_output_var.set(f"当前任务目录：{out}")
        if not source.is_file():
            messagebox.showerror("结果错误", f"找不到已有全量CSV：\n{source}")
            return
        try:
            ranking_settings = self.current_ranking_settings()
        except ValueError as exc:
            messagebox.showerror("排行榜门槛错误", str(exc))
            return
        settings_path = out / "排行榜设置.json"
        settings_path.write_text(json.dumps(ranking_settings, ensure_ascii=False, indent=2), "utf-8")
        cmd = [sys.executable, "-X", "utf8", str(项目目录 / "worst_export.py"),
               "--output", str(out), "--source", str(source), "--settings", str(settings_path)]
        limit = top_limit(ranking_settings)
        self.append_log(f"开始扫描历史CSV生成最优前{limit}和最差1000，不重新回测：" + str(source))
        if not self._launch_process(cmd, out, "legacy"):
            return
        self.start_btn.configure(state="disabled")
        self.worst_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set(f"正在扫描已有CSV生成最优前{limit}和最差1000……")

    def _launch_process(self, cmd, out: Path, kind: str):
        if self._fingerprint_run_active():
            return False
        try:
            process = subprocess.Popen(
                cmd, cwd=项目目录, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as exc:
            self.append_log(f"启动工作进程失败：{exc}")
            messagebox.showerror("启动失败", str(exc))
            return False
        self.proc = process
        self._invalidate_fingerprint_lookup()
        self.proc_kind = kind
        self._job_counter += 1
        self._active_job_id = self._job_counter
        self._active_output_dir = Path(out)
        self.start_btn.configure(state="disabled")
        self.worst_btn.configure(state="disabled")
        self.candidate_btn.configure(state="disabled")
        for index in range(self.notebook.index("end")):
            if index != (4 if kind == "hedge" else 2):
                self.notebook.tab(index, state="disabled")
        # Capture paths/handle on the Tk thread. The reader must never call .get()
        # or use self.proc after a new job has replaced it.
        threading.Thread(target=self.read_output,
                         args=(process, Path(out) / "工作进程日志.txt", self._active_job_id),
                         daemon=True).start()
        return True

    def read_output(self, process, log_path: Path, job_id):
        def emit(message):
            message["job_id"] = job_id
            self.messages.put(message)
        logfile = None
        try:
            try:
                logfile = log_path.open("a", encoding="utf-8", buffering=1)
            except OSError as exc:
                emit({"type": "warning", "message": f"无法保存工作日志，仍继续读取进度：{exc}"})
            if process.stdout is not None:
                for line in process.stdout:
                    if logfile is not None:
                        try:
                            logfile.write(line)
                        except OSError as exc:
                            logfile.close(); logfile = None
                            emit({"type": "warning", "message": f"日志写入失败：{exc}"})
                    try:
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError("非消息对象")
                    except (ValueError, TypeError):
                        message = {"type": "log", "message": line.rstrip()}
                    emit(message)
        except Exception as exc:
            emit({"type": "warning", "message": f"读取进度失败：{exc}"})
        finally:
            code = process.wait()
            if logfile is not None:
                try:
                    logfile.write(f"工作进程退出代码：{code}\n")
                except OSError:
                    pass
                finally:
                    logfile.close()
            emit({"type": "process_exit", "code": code})

    def toggle_pause(self):
        out = self.output_dir() / "控制"
        out.mkdir(parents=True, exist_ok=True)
        flag = out / "暂停.flag"
        if not self.paused:
            flag.write_text("pause", "utf-8"); self.paused = True
            self.pause_btn.configure(text="继续")
            self.append_log("已请求暂停；当前小任务结束后暂停。")
        else:
            if flag.exists(): flag.unlink()
            self.paused = False; self.pause_btn.configure(text="暂停")
            self.append_log("已继续运行。")

    def stop_run(self):
        if not self.proc or self.proc.poll() is not None: return
        if self.proc_kind == "hedge":
            self.hedge_panel.request_stop()
            return
        out = self.output_dir() / "控制"
        out.mkdir(parents=True, exist_ok=True)
        if self.proc_kind in ("worst", "legacy", "legacy_auto"):
            (out / "停止最差导出.flag").write_text("stop", "utf-8")
            self.append_log("已请求停止排行榜扫描。")
            return
        if self.proc_kind in ("candidate", "candidate_auto"):
            (out / "停止候选导出.flag").write_text("stop", "utf-8")
            self.append_log("已请求停止候选扫描。")
            return
        (out / "停止.flag").write_text("stop", "utf-8")
        self.append_log("已请求停止当前条及后续列表；已完成结果保留，批量任务暂不支持续跑。"
                        if self.proc_kind == "fingerprint_batch" else
                        "已请求安全停止；下次可从最后完整止盈方案继续。")

    def export_server_campaign(self):
        if any(var.get().strip() for var in (self.micro_csv_var, self.funding_var, self.oi_var, self.bundle_var)):
            messagebox.showerror("导出服务器任务", "此入口只打包主K线。已选择外部数据，请先另存主K线方案；不会静默丢弃外部数据设置。")
            return
        target = filedialog.asksaveasfilename(title="导出独立服务器任务包", defaultextension=".zip",
                                             initialfile="无人值守回测.zip", filetypes=[("ZIP", "*.zip")])
        if not target:
            return
        try:
            from campaign_export import export_bundle
            result = export_bundle(target, self.csv_var.get().strip(), self.current_selection(),
                                   self.start_date_var.get().strip(), self.end_date_var.get().strip())
            settings = result['settings']
            messagebox.showinfo("任务包已校验", f"{target}\n分层筛选：{settings['start']} 至 {settings['end']}\n"
                                f"时间复核：{settings['validation_start']} 至 {settings['validation_end']}\n"
                                "默认72小时。导出不自动上传、启动，也不改变实盘。")
        except Exception as exc:
            messagebox.showerror("导出服务器任务失败", str(exc))

    def open_output(self):
        path = self.output_dir()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("打开目录失败", f"{path}\n{exc}")

    def poll_messages(self):
        # Bound each callback so a flood of log/progress messages cannot starve
        # repaint, pause, stop and other user input.
        deadline = time.monotonic() + 0.025
        for _ in range(100):
            if time.monotonic() > deadline:
                break
            try: msg = self.messages.get_nowait()
            except queue.Empty: break
            if not isinstance(msg, dict):
                continue
            if "job_id" in msg and msg["job_id"] != self._active_job_id:
                continue
            kind = msg.get("type")
            if "job_id" in msg and self.proc_kind == "hedge":
                self.hedge_panel.handle_message(msg)
                if kind != "process_exit":
                    continue
            if kind in ("fingerprint_find", "fingerprint_restore", "fingerprint_error",
                        "fingerprint_queue_find", "fingerprint_queue_restore", "fingerprint_progress"):
                self._handle_fingerprint_message(msg)
                continue
            if isinstance(kind, str) and kind.startswith("batch_"):
                self._handle_fingerprint_batch_message(msg)
                continue
            if kind in ("data_inspection", "data_inspection_error") and msg.get("generation", self._data_generation) != self._data_generation:
                self._data_inspecting = False
                self.inspect_data_btn.configure(state="normal")
                self.data_summary_var.set("检测过程中数据路径已变化；旧结果已丢弃，请重新检测。")
                continue
            if kind == "data_inspection":
                self._data_inspecting = False
                self.inspect_data_btn.configure(state="normal")
                self._apply_data_report(msg.get("report", {}))
            elif kind == "data_inspection_error":
                self._data_inspecting = False
                self.inspect_data_btn.configure(state="normal")
                self.data_summary_var.set("检测失败：" + msg.get("message", "未知错误"))
                messagebox.showerror("数据检测失败", msg.get("message", "未知错误"))
            elif kind == "data_capabilities":
                self.append_log("工作进程确认数据能力：" + ", ".join(k for k,v in msg.get("capabilities", {}).items() if v))
                if self.proc_kind == "backtest":
                    self.data_report = dict(self.data_report or {}, capabilities=msg.get("capabilities", {}),
                        capabilities_by_timeframe=msg.get("capabilities_by_timeframe", {}))
                    self.selection_panel.set_data_capabilities(msg.get("capabilities", {}), True,
                        timeframe_capabilities=msg.get("capabilities_by_timeframe", {}))
                    if msg.get("selection"):
                        self.selection_panel.apply_config(msg["selection"])
                    self.save_user_settings()
            elif kind == "hardware":
                self.hardware_var.set(f"CPU：{os.cpu_count() or '?'}线程｜检测到GPU：{msg['gpu']}｜逐账户重放使用CPU")
            elif kind == "acceleration":
                active = "GPU" if msg.get("active") == "gpu" else "CPU"
                self.hardware_var.set(f"CPU：{os.cpu_count() or '?'}线程｜GPU：{msg.get('gpu')}｜当前实际使用：{active}")
                self.append_log(msg.get("message", "计算设备已选择"))
            elif kind == "entry_precompute":
                self.progress["value"] = min(100, 100 * msg.get("completed", 0) / max(1, msg.get("total", 0)))
                self.status_var.set(entry_precompute_text(msg))
            elif kind == "progress":
                self.progress["value"] = msg.get("percent", 0)
                self.status_var.set(scan_progress_text(msg))
                self.append_log(f"完成：{msg.get('category')}，总进度{msg.get('percent', 0):.2f}%")
            elif kind == "inner_progress":
                self.progress["value"] = msg.get("percent", self.progress["value"])
                self.status_var.set(scan_progress_text(msg))
            elif kind == "worst_progress":
                self.progress["value"] = msg.get("percent", 0)
                self.status_var.set(f"扫描CSV {msg.get('percent', 0):.2f}%｜已完成类别 {msg.get('categories_done', 0)}/{msg.get('categories_total', 0)}｜预计剩余 {human_seconds(msg.get('eta_seconds', 0))}")
            elif kind == "worst_category_done":
                self.append_log(f"最差1000已收齐：{msg.get('category')}")
            elif kind == "worst_completed":
                self.progress["value"] = 100
                self.status_var.set(f"各类止盈最差1000已生成，共{msg.get('rows', 0):,}行")
                self.append_log("最差1000 Excel已生成：" + msg["path"])
            elif kind == "worst_stopped":
                self.status_var.set("最差1000扫描已停止，可重新点击按钮从头扫描")
            elif kind == "legacy_progress":
                if self.proc_kind == "backtest": self.proc_kind = "legacy_auto"
                self.progress["value"] = msg.get("percent", 0)
                self.status_var.set(
                    f"旧排行扫描 {msg.get('percent', 0):.2f}%｜已读 {msg.get('rows', 0):,} 行｜"
                    f"预计剩余 {human_seconds(msg.get('eta_seconds', 0))}"
                )
            elif kind == "legacy_excel_ready":
                self.append_log("旧排行Excel已生成：" + msg.get("path", ""))
            elif kind == "legacy_completed":
                self.progress["value"] = 100
                top_rows, worst_rows = msg.get("top_rows", 0), msg.get("worst_rows", 0)
                limit = msg.get("top_limit", 5000)
                self.status_var.set(
                    f"排行完成｜最优前{limit}共 {top_rows:,} 行｜最差1000共 {worst_rows:,} 行"
                )
                self.append_log(f"最优前{limit} Excel：" + msg.get("top_path", ""))
                self.append_log("最差1000 Excel：" + msg.get("worst_path", ""))
                self.append_log("排行范围：最优按当前门槛筛选；最差从全量有效结果选取，不套用最优门槛；完整CSV不受影响。")
                if top_rows == 0 and worst_rows > 0:
                    self.append_log("最优榜为空，但最差榜有有效结果；请检查最优门槛，不代表未回测或手续费结果未计算。")
                elif top_rows == 0 and worst_rows == 0:
                    self.append_log("两榜均为空：未找到可入榜的有效结果；请检查全量CSV和日志，不能仅凭空榜判断是门槛淘汰。")
            elif kind == "legacy_stopped":
                self.status_var.set("旧排行扫描已停止，可重新点击按钮从头扫描")
            elif kind == "candidate_progress":
                if self.proc_kind == "backtest": self.proc_kind = "candidate_auto"
                self.progress["value"] = msg.get("percent", 0)
                self.status_var.set(
                    f"候选扫描 {msg.get('percent', 0):.2f}%｜已读 {msg.get('rows', 0):,} 行｜"
                    f"初筛通过 {msg.get('eligible', 0):,} 行"
                )
            elif kind == "candidate_completed":
                self.progress["value"] = 100
                self.status_var.set(
                    f"候选导出完成｜研究 {msg.get('research', 0):,}｜观察 {msg.get('watch', 0):,}｜实盘 {msg.get('live', 0):,}"
                )
                self.append_log("分层候选Excel已生成：" + msg.get("path", ""))
            elif kind == "candidate_stopped":
                self.status_var.set("候选扫描已停止，可从已有CSV重新生成")
            elif kind in ("stage", "warning", "log"):
                message = msg.get("message", str(msg))
                self.append_log(message)
                if kind == "stage":
                    if "total_rows" in msg:
                        self.progress["value"] = msg.get("percent", 0)
                        self.status_var.set(scan_progress_text(msg))
                    else:
                        self.status_var.set(message)
            elif kind == "tp_start":
                if "total_rows" in msg:
                    self.progress["value"] = msg.get("percent", 0)
                    self.status_var.set(scan_progress_text(msg))
                self.append_log(f"开始 {msg['tp']}/{msg['total_tp']}：{msg['category']}｜{msg['description']}")
            elif kind == "excel_ready":
                self.append_log("Excel已生成：" + msg["path"])
            elif kind == "completed":
                self.progress["value"] = 100
                self.status_var.set(f"全部完成，共输出 {msg['rows']:,} 行")
                self.append_log("全部回测完成：" + msg["csv"])
            elif kind == "paused":
                self.status_var.set("已暂停，可点击继续")
            elif kind == "stopped":
                self.status_var.set("已安全停止，下次点击开始即可断点续跑")
            elif kind == "process_exit":
                code = msg["code"]
                self.start_btn.configure(state="normal")
                self.worst_btn.configure(state="normal")
                self.candidate_btn.configure(state="normal")
                for index in range(self.notebook.index("end")):
                    self.notebook.tab(index, state="normal")
                self.pause_btn.configure(state="disabled", text="暂停")
                self.stop_btn.configure(state="disabled")
                self.paused = False
                self.proc_kind = ""
                self._active_job_id = None
                if code != 0:
                    self.status_var.set(f"工作进程异常退出，代码{code}；请查看日志")
                    self.append_log(f"工作进程退出代码：{code}")
        self._poll_after_id = self.after(100, self.poll_messages)

    def _handle_fingerprint_batch_message(self, message):
        kind = message["type"]
        index, total = message.get("index", 0), message.get("total", len(self.fingerprint_queue))
        tag = f"[{index}/{total} {message.get('fingerprint', '')}]"
        if kind == "batch_item_started":
            self.status_var.set(f"列表{tag}正在回测")
            self.append_log(f"列表{tag}开始；原参数、数据及日期独立计算。")
        elif kind == "batch_item_completed":
            self.progress["value"] = 100 * index / max(1, total)
            self.append_log(f"列表{tag}完成：{message.get('output', '')}")
        elif kind == "batch_item_skipped":
            self.progress["value"] = 100 * index / max(1, total)
            self.append_log(f"列表{tag}已跳过：{message.get('message', '')}；原指纹定义保留。")
        elif kind == "batch_child_event":
            event = message.get("event", {})
            child_kind = event.get("type")
            if child_kind == "entry_precompute":
                self.progress["value"] = 100 * max(0, index - 1) / max(1, total)
                self.status_var.set(tag + " 本条" + entry_precompute_text(event))
            elif child_kind in ("stage", "warning", "log"):
                self.append_log(tag + " " + str(event.get("message", "")))
                self.status_var.set(tag + " 本条" + scan_progress_text(event) if "total_rows" in event
                                    else tag + " " + str(event.get("message", "")))
            elif child_kind in ("excel_ready", "legacy_excel_ready", "candidate_completed"):
                self.append_log(tag + " 结果文件：" + str(event.get("path", "")))
            elif child_kind == "legacy_completed":
                top_rows, worst_rows = event.get("top_rows", 0), event.get("worst_rows", 0)
                summary = tag + f" 排行完成｜最优 {top_rows:,} 行｜最差 {worst_rows:,} 行"
                self.status_var.set(summary)
                self.append_log(summary)
                self.append_log(tag + " 最优按当前门槛筛选；最差从全量有效结果选取，不套用最优门槛；完整CSV不受影响。")
                if top_rows == 0 and worst_rows > 0:
                    self.append_log(tag + " 最优榜为空，但最差榜有有效结果；请检查最优门槛，不代表未回测或手续费结果未计算。")
                elif top_rows == 0 and worst_rows == 0:
                    self.append_log(tag + " 两榜均为空：未找到可入榜的有效结果；请检查全量CSV和日志，不能仅凭空榜判断是门槛淘汰。")
            elif child_kind in ("progress", "inner_progress", "tp_start"):
                if "completed" in event and "total" in event:
                    from hedge_panel import eta_text
                    fraction = event["completed"] / max(1, event["total"])
                    self.progress["value"] = 100 * (max(0, index - 1) + fraction) / max(1, total)
                    self.status_var.set(tag + f" 双向试验 {event['completed']:,}/{event['total']:,}｜预计剩余 " + eta_text(event.get("eta_seconds")))
                else:
                    self.progress["value"] = 100 * (max(0, index - 1) + event.get("percent", 0) / 100) / max(1, total)
                    self.status_var.set(tag + " 本条" + scan_progress_text(event))
            elif child_kind == "done" and event.get("workbook"):
                self.append_log(tag + " 双向结果文件：" + event["workbook"])
            elif child_kind == "paused":
                self.status_var.set(tag + " 本条已在安全点暂停")
        elif kind == "batch_child_log":
            self.append_log(tag + " " + str(message.get("message", "")))
        elif kind == "batch_completed":
            self.progress["value"] = 100
            self.status_var.set(f"多指纹列表处理完成｜已回测{message.get('completed_count', total)}条｜跳过{message.get('skipped_count', 0)}条")
            self.append_log("列表结果独立保存于：" + str(self.output_dir()))
        elif kind == "batch_stopped":
            self.status_var.set("列表已停止，未继续启动下一条；已完成结果保留")
            self.append_log("批量暂不续跑：可在列表移除已完成项后新开一批。")
        elif kind == "batch_failed":
            self.status_var.set("列表失败，未继续启动下一条")
            self.append_log("列表失败：" + str(message.get("message", "请检查工作进程日志")))
        elif kind == "batch_paused":
            self.status_var.set(str(message.get("message", "已请求暂停，等待当前条在安全点响应")))
        elif kind == "batch_resumed":
            self.status_var.set("多指纹列表已继续")

    def report_callback_exception(self, exc_type, value, tb):
        detail = "".join(traceback.format_exception(exc_type, value, tb))
        try:
            log_path = user_data_dir() / "UI错误日志.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + detail + "\n")
        except OSError:
            pass
        if hasattr(self, "log"):
            self.append_log(f"界面操作失败：{value}")
        if not self._closing:
            messagebox.showerror("操作失败", f"{value}\n详细错误已尝试写入UI错误日志.txt。")

    def on_close(self):
        if self._closing:
            return
        self.hedge_panel.save_settings()
        if self.proc and self.proc.poll() is None:
            prompt = ("先请求安全停止，等待双向回测保存已完成结果后再关闭窗口？"
                      if self.proc_kind == "hedge" else "先请求安全停止，等待当前任务保存断点后再关闭窗口？")
            if not messagebox.askyesno("程序仍在运行", prompt):
                return
            self._closing = True
            self.stop_run()
            self.status_var.set("正在等待工作进程安全停止并保存，请勿强制结束窗口。")
            self._close_after_id = self.after(200, self._wait_for_safe_close)
            return
        self.save_user_settings()
        self.destroy()

    def _wait_for_safe_close(self):
        if self.proc and self.proc.poll() is None:
            self._close_after_id = self.after(200, self._wait_for_safe_close)
            return
        self.save_user_settings()
        self.destroy()

    def destroy(self):
        for name in ("_poll_after_id", "_close_after_id"):
            task = getattr(self, name, None)
            if task:
                try:
                    self.after_cancel(task)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
