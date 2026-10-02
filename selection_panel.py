from __future__ import annotations

import math
import copy
import tkinter as tk
from tkinter import messagebox, ttk
from extended_rules import (ENTRY_RULES, SECOND_ROUND_CODES, THIRD_ROUND_CODES,
                            KLINE_ONLY_THIRD_CODES, MICROSTRUCTURE_THIRD_CODES, EXTERNAL_THIRD_CODES,
                            FOURTH_ROUND_CODES, RESEARCH_CASE_CODES, FIFTH_ROUND_CODES,
                            KLINE_ONLY_FIFTH_CODES, MICROSTRUCTURE_FIFTH_CODES, EXTERNAL_FIFTH_CODES,
                            KLINE_ONLY_FOURTH_CODES, MICROSTRUCTURE_FOURTH_CODES,
                            entry_supported_timeframes, entry_unavailable_reason, capabilities_for_timeframe)
from fixed_rate_picker import FixedRatePicker
from fifth_policy import ACTIVE_FIFTH_CODES, fifth_retirement_reason
from selection_availability import prepare_editor_selection
from indicator_combo_picker import IndicatorComboPicker
from scrollable_form import ScrollableForm
from platform_support import fit_window, ui_font_family, wheel_units
from entry_position import POSITION_FILTERS
from execution_settings import COST_MODE_LABELS, DEFAULT_COST_MODE

import 轮次基线 as 基线
from selection_config import (时间条件, 全选配置, 配置统计, 规范化配置, 入场触发口径选项,
                              默认入场触发口径, 规范化入场触发口径)
from strategy_space import (生成固定止损档, 固定止损同档代码, 方向列表, 会话列表,
                            强制时间止损档, 仓位列表, 周期列表, 生成止损组合, 生成止盈方案,
                            生成叠加止盈档, 叠加止盈同档代码, 固定比例代码, 固定止损比例档,
                            补全固定比例档, 百分数转比例)


情况名称 = {
    0: "不启用该周期",
    1: "情况一",
    2: "情况二",
    3: "情况三（量能充足）",
    4: "情况四（不足时反向）",
    **{code: row[0] for code, row in ENTRY_RULES.items()},
}
# 1m 的 0 不是"不参与过滤"，而是"不需要1分钟触发"，方向直接由高周期给出，
# 所以单独换个说法，免得看成和高周期一样的意思。
一分钟情况名称 = {**情况名称, 0: "不启用（高周期直接开）"}
指标名称 = {"hist": "MACD柱", "dif": "DIF线"}
每页止盈数 = 300
每页止损数 = 100
# 策略空间保证旧方案只追加、不插入；这里仅用于界面分层，不参与回测计算。
# 止损前 912 条、止盈编号 1—8280 是原有第一轮，之后的条目属于第二轮扩展。
第一轮止损数量 = 912
第一轮止盈编号上限 = 8280
第二轮止盈编号上限 = 8865  # v1.41新增退出从8866起，继续追加不改历史编号

# 已测最优与新增未测的统一标记。★是结论，「新」是还没跑过的东西，两者绝不能混看。
# 不用 emoji：Tk 在 Windows 上渲染成方框，几百个勾选框里全是豆腐块。
最优标记 = "★"
新增标记 = "新"
最优色 = "#C00000"
新增色 = "#0F7B0F"
# Treeview 用行底色再强调一次：只靠一个字符在几百行里根本看不见。
最优行底色 = "#FFF2CC"
新增行底色 = "#E8F5E9"


def 标注(text, 是最优=False, 是新增=False):
    """给勾选框文字加前缀标记。"""
    if 是最优:
        return f"{最优标记} {text}"
    if 是新增:
        return f"{新增标记} {text}"
    return text


class 组合选择面板(ttk.Frame):
    def __init__(self, parent, on_change=None, *, entry_mode_var=None, entry_mode_options=None, cost_mode_vars=None):
        super().__init__(parent)
        self.ui_font_family = ui_font_family(self)
        self.on_change = on_change
        self.entry_mode_options = dict(entry_mode_options or 入场触发口径选项)
        self.entry_mode_var = (entry_mode_var if entry_mode_var is not None else
                               tk.StringVar(value=self.entry_mode_options[默认入场触发口径]))
        self.entry_mode_vars = {code: tk.BooleanVar(value=code == 默认入场触发口径)
                                for code in self.entry_mode_options}
        self.set_entry_modes(self.entry_mode_var.get(), update_summary=False)
        self.cost_mode_vars = (cost_mode_vars if cost_mode_vars is not None else
                               {code: tk.BooleanVar(value=code == DEFAULT_COST_MODE) for code in COST_MODE_LABELS})
        self.all_stops = 生成止损组合()
        self.stop_order = {code: index for index, (code, _components, _label) in enumerate(self.all_stops)}
        self.all_tps = 生成止盈方案()
        self.selected_stops = {code for code, _components, _label in self.all_stops}
        self.selected_fixed_stops = set(固定止损同档代码())
        self.selected_overlay_tps = {"OFF"}
        self.selected_tps = {x.编号 for x in self.all_tps}
        self.indicator_combos = {}
        self.combo_buttons = {}
        self.combo_status_vars = {}
        self.stop_page = 0
        self.tp_page = 0
        self._stop_search_after_id = None
        self._search_after_id = None
        self.data_capabilities = {}
        self.capabilities_by_timeframe = {}
        self._data_capabilities_known = False
        self._build()

    def destroy(self):
        # 搜索框防抖任务属于本面板；销毁后不能再回调已不存在的控件。
        for name in ("_stop_search_after_id", "_search_after_id"):
            task = getattr(self, name, None)
            if task:
                self.after_cancel(task)
                setattr(self, name, None)
        super().destroy()

    def _build(self):
        header = ttk.Frame(self, padding=(14, 10, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text="组合选择与筛选", font=(self.ui_font_family, 15, "bold")).pack(side="left")
        ttk.Label(header, text="第五批保留39种；其余81种已永久删除", foreground="#1F4E78").pack(
            side="left", padx=(14, 0)
        )
        ttk.Button(header, text="全选当前策略（组合很大）", command=self.reset_all).pack(side="right")
        ttk.Button(header, text="使用普通入场模式", command=self._use_regular_entry_mode).pack(side="right", padx=8)
        self.policy_notice_var = tk.StringVar()
        ttk.Label(self, textvariable=self.policy_notice_var, foreground="#7F6000", wraplength=1100,
                  padding=(14, 0)).pack(fill="x")

        self.summary_var = tk.StringVar()
        ttk.Label(
            self,
            textvariable=self.summary_var,
            foreground="#C00000",
            font=(self.ui_font_family, 10, "bold"),
            padding=(14, 2, 14, 8),
        ).pack(fill="x")

        self.pages = ttk.Notebook(self)
        self.pages.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.entry_tab = ttk.Frame(self.pages, padding=12)
        self.stop_tab = ttk.Frame(self.pages, padding=12)
        self.tp_tab = ttk.Frame(self.pages, padding=12)
        self.cooldown_tab = ttk.Frame(self.pages, padding=12)
        self.size_tab = ttk.Frame(self.pages, padding=12)
        for frame, title in (
            (self.entry_tab, "1. 开仓条件"),
            (self.stop_tab, "2. 止损（含持仓时间）"),
            (self.tp_tab, "3. 止盈（含持仓时间）"),
            (self.cooldown_tab, "4. 止盈后等待"),
            (self.size_tab, "5. 仓位 / 杠杆"),
        ):
            self.pages.add(frame, text=f"  {title}  ")

        # 开仓规则增加后采用独立滚动区，各周期扩展选项还可折叠。
        entry_canvas = tk.Canvas(self.entry_tab, highlightthickness=0)
        self.entry_canvas = entry_canvas
        entry_scroll = ttk.Scrollbar(self.entry_tab, orient="vertical", command=entry_canvas.yview)
        entry_canvas.configure(yscrollcommand=entry_scroll.set)
        entry_scroll.pack(side="right", fill="y")
        entry_canvas.pack(side="left", fill="both", expand=True)
        entry_body = ttk.Frame(entry_canvas)
        entry_window = entry_canvas.create_window((0, 0), window=entry_body, anchor="nw")
        entry_body.bind("<Configure>", lambda _e: entry_canvas.configure(scrollregion=entry_canvas.bbox("all")))
        entry_canvas.bind("<Configure>", lambda e: entry_canvas.itemconfigure(entry_window, width=e.width))
        self._build_entries(entry_body)
        self.stop_scroll = ScrollableForm(self.stop_tab)
        self.stop_scroll.pack(fill="both", expand=True)
        self.tp_scroll = ScrollableForm(self.tp_tab)
        self.tp_scroll.pack(fill="both", expand=True)
        self._build_stops(self.stop_scroll.body)
        self._build_tps(self.tp_scroll.body)
        self._build_cooldowns(self.cooldown_tab)
        self._build_sizes(self.size_tab)
        self._bind_scroll(self.entry_tab, self.entry_canvas)
        # Scroll each table internally; use the surrounding form's scrollbar to
        # reveal the table/pager when the settings exceed the viewport.
        self._bind_scroll(self.stop_tree, self.stop_tree)
        self._bind_scroll(self.tp_tree, self.tp_tree)
        self._update_summary()

    def _bind_scroll(self, widget, target):
        def wheel(event):
            units = wheel_units(event)
            if not units:
                return None
            target.yview_scroll(units, "units")
            return "break"
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(event, wheel)
        # 输入框/下拉框保留自身的编辑按键，列表保留上下移动选中行。
        if not isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Treeview, tk.Entry)):
            for key, direction in (("Up", -1), ("Down", 1), ("Prior", -1), ("Next", 1)):
                def move(_event, d=direction, units="pages" if key in ("Prior", "Next") else "units"):
                    target.yview_scroll(d, units)
                    return "break"
                widget.bind(f"<{key}>", move)
            widget.bind("<Button-1>", lambda _e: target.focus_set(), add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child, target)

    def _set_fixed_rates(self, selected):
        self.selected_fixed_stops = set(selected)
        self._refresh_fixed_stop_summary()
        self._update_summary()

    def show_entry_constraints(self):
        self.pages.select(self.entry_tab)
        self.update_idletasks()
        bounds = self.entry_canvas.bbox("all")
        if bounds and bounds[3] > 0:
            self.entry_canvas.yview_moveto(self.entry_constraints_box.winfo_y() / bounds[3])

    def _set_overlay_rates(self, selected):
        self.selected_overlay_tps = set(selected)
        self._refresh_overlay_summary()
        self._update_summary()

    def _build_entries(self, body):
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Label(actions, text="快捷选择：", foreground="#1F4E78").pack(side="left")
        ttk.Button(actions, text="文档全部情况", command=lambda: self._entry_preset("all")).pack(side="left", padx=3)
        ttk.Button(actions, text="各周期仅情况一", command=lambda: self._entry_preset("case1")).pack(side="left", padx=3)
        ttk.Button(actions, text="各周期仅情况三", command=lambda: self._entry_preset("case3")).pack(side="left", padx=3)
        ttk.Button(actions, text="各周期仅情况四", command=lambda: self._entry_preset("case4")).pack(side="left", padx=3)
        ttk.Button(actions, text="仅1分钟基础，高周期关闭", command=lambda: self._entry_preset("one_minute")).pack(
            side="left", padx=3
        )

        indicator = ttk.LabelFrame(body, text="MACD计算口径（可多选）", padding=10)
        indicator.pack(fill="x", pady=(0, 10))
        self.field_vars = {}
        best_fields = set(基线.已测最优["开仓指标"])
        for field in ("hist", "dif"):
            var = tk.BooleanVar(value=True)
            self.field_vars[field] = var
            best = field in best_fields
            ttk.Checkbutton(
                indicator, text=标注(指标名称[field], 是最优=best),
                variable=var, command=self._update_summary,
                style="Best.TCheckbutton" if best else "TCheckbutton",
            ).pack(side="left", padx=(0, 22))
        ttk.Label(indicator, foreground=最优色,
                  text=f"{最优标记} = 已测最优：{基线.最优说明['开仓指标']}").pack(side="left", padx=(10, 0))

        self.entry_constraints_box = ttk.LabelFrame(body, text="入场约束（次数规则与位置过滤同时生效）", padding=10)
        self.entry_constraints_box.pack(fill="x", pady=(0, 10))
        self.entry_trigger_box = ttk.LabelFrame(self.entry_constraints_box, text="入场触发/次数规则（可多选，分别回测）", padding=10)
        self.entry_trigger_box.pack(fill="x", pady=(0, 10))
        self.entry_mode_widgets = {}
        ttk.Label(self.entry_trigger_box,
                  text="不必选择第五批。只测前几批或新旧混合扫描，可选普通入场模式；含第五批的组合会自动使用其自身事件。",
                  foreground="#1F4E78", wraplength=880).pack(fill="x", pady=(0, 6))
        for code, label in self.entry_mode_options.items():
            button = ttk.Checkbutton(self.entry_trigger_box, text=label,
                                     variable=self.entry_mode_vars[code], command=self._entry_modes_changed)
            button.pack(anchor="w", pady=2)
            self.entry_mode_widgets[code] = button
        ttk.Label(self.entry_trigger_box,
                  text="至少选一种；多选＝各规则分别回测，不是同时叠加限次。每多选一种会增加一套组合，"
                       "并分别与下方每档位置过滤组合。\n"
                       "LIVE_01按1m所选指标连续升/降段限次；TF_EVENT按最低启用周期收盘事件触发；"
                       "MACD_CYCLE按1m DIF/DEA金叉死叉（柱红绿换色）分轮：hist=2×(DIF−DEA)，"
                       "所以hist穿0等于两线交叉，并非DIF本身穿0。每次换色后一轮最多开一次；"
                       "同侧柱增减、空心/实心变化不另计轮，恰好为0时延续此前一侧。\n"
                       "完整轮次：固定红起点或绿起点，连续两色才是一轮；中间换色、止盈或止损均不恢复次数，"
                       "每轮多空合计最多实际开仓一次。两种起点分别回测，不交替重置。"
                       "样本起始不足一轮时，等首次真实换入所选起点颜色后启用；仍需满足所选开仓条件。",
                  foreground="#7F6000", wraplength=880, justify="left").pack(fill="x", pady=(6, 0))
        position_box = ttk.LabelFrame(self.entry_constraints_box, text="开仓位置过滤（独立回测维度，可多选比较）", padding=10)
        self.position_box = position_box
        position_box.pack(fill="x")
        position_actions = ttk.Frame(position_box)
        position_actions.pack(fill="x")
        self.position_baseline_button = ttk.Button(
            position_actions, text="关闭基线", command=lambda: self._set_position_filters(["OFF"]))
        self.position_baseline_button.pack(side="left")
        self.position_compare_button = ttk.Button(
            position_actions, text=f"比较所有{len(POSITION_FILTERS)}档", command=lambda: self._set_position_filters(list(POSITION_FILTERS)))
        self.position_compare_button.pack(side="left", padx=8)
        ttk.Label(position_box,
                  text="多选＝分别回测，每选一档增加一套组合；只有组合档中的两项条件需要同时满足。"
                       "仅限制开仓，不反手、不提前平仓。\n"
                       "只用当时已收数据：5m EMA20与同周期ATR14限制多单向上、空单向下的偏离；区间使用此前30/60根1m最高/最低价，不含当前信号K。\n"
                       "位置过滤与开仓周期分别选择；第三/四批按所选周期的已收盘K线判断。\n"
                       "研究追价档为待验证候选：近5根1m价差÷1m真实振幅SMA14；达到阈值禁开，需24根连续已收K线。",
                  foreground="#7F6000", wraplength=880, justify="left").pack(fill="x", pady=(6, 4))
        position_rows = ttk.Frame(position_box)
        position_rows.pack(fill="x")
        self.position_filter_vars = {}
        self.position_filter_widgets = {}
        self.position_filter_order = list(POSITION_FILTERS)
        for index, (code, spec) in enumerate(POSITION_FILTERS.items()):
            var = tk.BooleanVar(value=code == "OFF")
            self.position_filter_vars[code] = var
            widget = ttk.Checkbutton(position_rows, text=spec["label"], variable=var,
                                     command=self._update_summary)
            widget.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 22), pady=3)
            self.position_filter_widgets[code] = widget

        position_columns = 2
        def layout_positions(event):
            nonlocal position_columns
            widgets = list(self.position_filter_widgets.values())
            required = sum(max(widget.winfo_reqwidth() for widget in widgets[column::2])
                           for column in range(2)) + 44
            columns = 2 if required <= event.width else 1
            if columns == position_columns:
                return
            position_columns = columns
            for index, widget in enumerate(widgets):
                widget.grid_configure(row=index // columns, column=index % columns)
        position_rows.bind("<Configure>", layout_positions)

        conditions = ttk.LabelFrame(body, text="五个周期的开仓条件（每个周期至少保留一项）", padding=10)
        conditions.pack(fill="x")
        self.case_vars = {}
        self.case_widgets = {}
        self.entry_fold_vars = {}          # 第二批，保留旧属性名
        self.entry_all_vars = {}
        self.third_entry_fold_vars = {}
        self.third_entry_all_vars = {}
        self.fourth_entry_fold_vars = {}
        self.fourth_entry_all_vars = {}
        self.fifth_entry_fold_vars = {}
        self.fifth_entry_all_vars = {}
        for row, (timeframe, values) in enumerate(时间条件.items()):
            values = [c for c in values if not fifth_retirement_reason(c)]
            group = ttk.LabelFrame(conditions, text=timeframe, padding=6)
            group.pack(fill="x", pady=3)
            self._build_combo_button(group, ("开仓", timeframe), f"{timeframe} 开仓")
            original = ttk.LabelFrame(group, text="第一批 · v8 情况一至四", padding=3)
            original.pack(fill="x")

            expanded = ttk.LabelFrame(group, text="第二批 · 0904 零轴 / 影线 / 实体 / 均线过滤", padding=3)
            show = tk.BooleanVar(value=False)
            self.entry_fold_vars[timeframe] = show
            actions = ttk.Frame(group); actions.pack(fill="x")
            all_var = tk.BooleanVar(value=False); self.entry_all_vars[timeframe] = all_var
            ttk.Checkbutton(actions, text="第二批全选 / 全不选", variable=all_var,
                            command=lambda tf=timeframe: self._set_second_round(tf)).pack(side="left")
            fold_button = ttk.Button(actions, text="展开第二批", command=lambda v=show: v.set(not v.get()))
            fold_button.pack(side="left", padx=12)
            def toggle_second(v=show, frame=expanded, button=fold_button):
                if v.get(): frame.pack(fill="x", pady=(4, 0))
                else: frame.pack_forget()
                button.configure(text="收起第二批" if v.get() else "展开第二批")
            show.trace_add("write", lambda *_args, callback=toggle_second: callback())

            third_codes = [c for c in values if c in THIRD_ROUND_CODES]
            third = None
            if third_codes:
                third = ttk.LabelFrame(group, text="第三批 · K线 / 成交微结构 / 资金费 / OI / 实验对照", padding=3)
                third_show = tk.BooleanVar(value=False); self.third_entry_fold_vars[timeframe] = third_show
                third_actions = ttk.Frame(group); third_actions.pack(fill="x")
                third_all = tk.BooleanVar(value=False); self.third_entry_all_vars[timeframe] = third_all
                ttk.Checkbutton(third_actions, text="第三批全选 / 全不选", variable=third_all,
                                command=lambda tf=timeframe: self._set_third_round(tf)).pack(side="left")
                ttk.Button(third_actions, text="仅OHLCV",
                           command=lambda tf=timeframe: self._set_third_source(tf, "kline")).pack(side="left", padx=(12, 4))
                ttk.Button(third_actions, text="仅成交微结构",
                           command=lambda tf=timeframe: self._set_third_source(tf, "micro")).pack(side="left", padx=4)
                ttk.Button(third_actions, text="仅资金费/OI",
                           command=lambda tf=timeframe: self._set_third_source(tf, "external")).pack(side="left", padx=4)
                ttk.Button(third_actions, text="当前数据可用",
                           command=lambda tf=timeframe: self._set_third_source(tf, "available")).pack(side="left", padx=4)
                third_button = ttk.Button(third_actions, text="展开第三批", command=lambda v=third_show: v.set(not v.get()))
                third_button.pack(side="left", padx=12)
                def toggle_third(v=third_show, frame=third, button=third_button):
                    if v.get(): frame.pack(fill="x", pady=(4, 0))
                    else: frame.pack_forget()
                    button.configure(text="收起第三批" if v.get() else "展开第三批")
                third_show.trace_add("write", lambda *_args, callback=toggle_third: callback())

            fourth = None
            if any(c in FOURTH_ROUND_CODES for c in values):
                fourth = ttk.LabelFrame(group, text="第四批 · 120条 / 30家族（144—263；定义以随包清单为准）", padding=3)
                fourth_show = tk.BooleanVar(value=False); self.fourth_entry_fold_vars[timeframe] = fourth_show
                fourth_all = tk.BooleanVar(value=False); self.fourth_entry_all_vars[timeframe] = fourth_all
                fa = ttk.Frame(group); fa.pack(fill="x")
                ttk.Checkbutton(fa, text="第四批全选 / 全不选", variable=fourth_all,
                                command=lambda tf=timeframe: self._set_fourth_round(tf)).pack(side="left")
                for title,source in [("仅OHLCV","kline"),("仅成交微结构","micro"),("当前数据可用","available")]:
                    ttk.Button(fa,text=title,command=lambda tf=timeframe,ss=source:self._set_fourth_source(tf,ss)).pack(side="left",padx=4)
                fourth_button=ttk.Button(fa,text="展开第四批",command=lambda v=fourth_show:v.set(not v.get()))
                fourth_button.pack(side="left",padx=8)
                def toggle_fourth(v=fourth_show,frame=fourth,button=fourth_button):
                    if v.get(): frame.pack(fill="x",pady=(4,0))
                    else: frame.pack_forget()
                    button.configure(text="收起第四批" if v.get() else "展开第四批")
                fourth_show.trace_add("write",lambda *_a,cb=toggle_fourth:cb())

            fifth = None
            if any(c in FIFTH_ROUND_CODES for c in values):
                fifth = ttk.LabelFrame(group, text="第五批 · 保留39种（其余81种永久删除，各周期仅显示原生支持的方法）", padding=3)
                fifth_show = tk.BooleanVar(value=False)
                fifth_all = tk.BooleanVar(value=False)
                self.fifth_entry_fold_vars[timeframe] = fifth_show
                self.fifth_entry_all_vars[timeframe] = fifth_all
                fa5 = ttk.Frame(group); fa5.pack(fill="x")
                ttk.Checkbutton(fa5, text="第五轮全选可用 / 全不选", variable=fifth_all,
                                command=lambda tf=timeframe: self._set_fifth_round(tf)).pack(side="left")
                for title, source in (("仅OHLCV", "kline"), ("仅增强成交", "micro"), ("当前数据可用", "available")):
                    ttk.Button(fa5, text=title, command=lambda tf=timeframe, ss=source:
                               self._set_fifth_source(tf, ss)).pack(side="left", padx=4)
                fifth_button = ttk.Button(fa5, text="展开第五轮", command=lambda v=fifth_show: v.set(not v.get()))
                fifth_button.pack(side="left", padx=8)
                ttk.Button(fa5, text="查看保留的39种", command=self._show_fifth_catalog).pack(side="left", padx=4)
                def toggle_fifth(v=fifth_show, frame=fifth, button=fifth_button):
                    if v.get(): frame.pack(fill="x", pady=(4, 0))
                    else: frame.pack_forget()
                    button.configure(text="收起第五轮" if v.get() else "展开第五轮")
                fifth_show.trace_add("write", lambda *_a, cb=toggle_fifth: cb())

            self.case_vars[timeframe] = {}
            names = 一分钟情况名称 if timeframe == "1m" else 情况名称
            second_index = 0; third_index = 0; fourth_index = 0; fifth_index = 0
            for value in values:
                var = tk.BooleanVar(value=value < 5)
                self.case_vars[timeframe][value] = var
                new_flag = value not in RESEARCH_CASE_CODES and ((timeframe, value) in 基线.新增开仓情况 or value >= 18)
                if value < 5:
                    frame, rr, cc = original, 0, value
                elif value in SECOND_ROUND_CODES:
                    frame, rr, cc = expanded, second_index // 3, second_index % 3
                    second_index += 1
                elif value in FOURTH_ROUND_CODES:
                    frame, rr, cc = fourth, fourth_index // 3, fourth_index % 3
                    fourth_index += 1
                elif value in FIFTH_ROUND_CODES:
                    frame, rr, cc = fifth, fifth_index // 3, fifth_index % 3
                    fifth_index += 1
                else:
                    frame, rr, cc = third, third_index // 3, third_index % 3
                    third_index += 1
                cb = ttk.Checkbutton(
                    frame, text=标注(names[value], 是新增=new_flag), variable=var,
                    command=self._update_summary, style="New.TCheckbutton" if new_flag else "TCheckbutton",
                )
                cb.grid(row=rr, column=cc, sticky="w", padx=(0, 15), pady=4)
                self.case_widgets.setdefault(timeframe, {})[value] = cb
                if value in FIFTH_ROUND_CODES and not self._entry_available(timeframe, value):
                    cb.configure(state="disabled")
            if fifth is not None:
                ttk.Label(fifth, text="F5独立产生多空与一次性事件，不暗中叠加MACD；明确勾选的旧规则仍参与过滤。\n"
                          "灰色项表示该方法尚缺计算支持或数据；完整定义与未接入原因见第五轮目录。当前成交仍使用运行页所选口径。",
                          wraplength=1000, foreground="#7F6000").grid(
                              row=(fifth_index + 2) // 3, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(conditions, text="同周期多选默认分别回测；打开该周期的“指标组合”，可枚举两项、三项、四项或更多条件同时满足的策略。\n"
                  "不同周期开仓条件取交集；止损、止盈页有相同按钮，组合退出按任一条件触发执行。",
                  wraplength=1020, foreground="#7F6000").pack(anchor="w", pady=6)
        # 1m 的"不启用"和高周期的不是一回事，说明紧跟在这张表下面，
        # 别放到整页最底下——那里已经被分析维度挤到看不见了。
        ttk.Label(
            body, foreground="#7F6000", wraplength=1120, justify="left",
            text="“不启用（高周期直接开）”是1m专有的一档：不再等1分钟触发，高周期允许哪个方向就开哪个方向；"
                 "两个方向同时被允许（高周期全不启用，或情况一在窗口外）视为没有方向，不开仓。"
                 "同一段1分钟MACD单调行情仍然最多只开一次。",
        ).pack(fill="x", pady=(6, 0))

        angles = ttk.LabelFrame(body, text="分析维度（默认都是中性值，不改变组合数；做专项分析时才勾）", padding=10)
        angles.pack(fill="x", pady=(10, 0))
        self.direction_vars, self.session_vars, self.hard_time_vars = {}, {}, {}
        ttk.Label(angles, text="开仓方向", width=10,
                  font=(self.ui_font_family, 10, "bold")).grid(row=0, column=0, sticky="w", pady=4)
        for col, (code, name) in enumerate(方向列表, 1):
            var = tk.BooleanVar(value=(code == "BOTH"))
            self.direction_vars[code] = var
            new = code in 基线.新增开仓方向
            ttk.Checkbutton(angles, text=标注(name, 是新增=new), variable=var,
                            command=self._update_summary,
                            style="New.TCheckbutton" if new else "TCheckbutton",
                            ).grid(row=0, column=col, sticky="w", padx=(0, 16))
        ttk.Label(angles, text="交易会话", width=10,
                  font=(self.ui_font_family, 10, "bold")).grid(row=1, column=0, sticky="nw", pady=4)
        sess = ttk.Frame(angles); sess.grid(row=1, column=1, columnspan=6, sticky="w")
        for i, (code, name) in enumerate(会话列表):
            var = tk.BooleanVar(value=(code == "ALL"))
            self.session_vars[code] = var
            new = code in 基线.新增交易会话
            ttk.Checkbutton(sess, text=标注(name, 是新增=new), variable=var,
                            command=self._update_summary,
                            style="New.TCheckbutton" if new else "TCheckbutton",
                            ).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 14))
        ttk.Label(angles, foreground=新增色, wraplength=1100, justify="left",
                  text=f"{新增标记} = 后来才加进工具、此前测试没覆盖到的档位。"
                       f"这里仅保留开仓方向/交易会话两个分析维度；持仓时间已移入止损/止盈。"
                  ).grid(row=4, column=0, columnspan=7, sticky="w", pady=(4, 0))
        ttk.Label(angles, foreground="#888888", wraplength=1100, justify="left",
                  text="开仓方向：分开跑只做多/只做空，检验信号是否只有一边有效。"
                       "交易会话：只限制开仓，不强制平掉已有仓位。"
                  ).grid(row=3, column=0, columnspan=7, sticky="w", pady=(8, 0))

        note = (
            "情况三：量能充足时顺反转方向开仓，量能不足不开户。情况四：量能充足时顺向，"
            "量能不足（成交量≤上一根）时反向开仓。只使用已收盘K线；高周期成交量由1m求和。"
        )
        ttk.Label(body, text=note, foreground="#7F6000", wraplength=1120, justify="left").pack(
            fill="x", pady=(10, 0)
        )

    def _entry_preset(self, preset):
        for timeframe, values in self.case_vars.items():
            allowed = set(values)
            if preset == "all":
                selected = allowed
            elif preset == "case1":
                selected = {1}
            elif preset == "case3":
                selected = {3}
            elif preset == "case4":
                selected = {4}
            else:
                selected = {1} if timeframe == "1m" else {0}
            for value, var in values.items():
                var.set(value in selected)
        self._update_summary()

    def _build_stops(self, body):
        fixed = ttk.LabelFrame(body, text="固定比例止损｜0.01%起步；旧0.1%~1.0%代码保持兼容", padding=8)
        fixed.pack(fill="x", pady=(0, 8))
        self.fixed_stop_weekday_var = tk.StringVar(value="0.4%")
        self.fixed_stop_weekend_var = tk.StringVar(value="跟随工作日")
        rates = [f"{r*100:.2f}%".rstrip("0").rstrip(".") + ("" if str(r).endswith("%") else "") for r in 固定止损比例档]
        row = ttk.Frame(fixed)  # 旧配置解析变量保留，不再显示逐档加入入口。
        ttk.Label(row, text="工作日").pack(side="left")
        ttk.Combobox(row, textvariable=self.fixed_stop_weekday_var, state="readonly",
                     width=8, values=rates).pack(side="left", padx=(6, 16))
        ttk.Label(row, text="周末").pack(side="left")
        ttk.Combobox(row, textvariable=self.fixed_stop_weekend_var, state="readonly",
                     width=10, values=["跟随工作日", *rates]).pack(side="left", padx=(6, 16))
        ttk.Button(row, text="加入所选", command=self._add_fixed_stop).pack(side="left")
        ttk.Button(row, text="只保留OFF",
                   command=lambda: self._preset_fixed_stops("off")).pack(side="left", padx=5)
        ttk.Button(row, text="OFF+旧同档10种",
                   command=lambda: self._preset_fixed_stops("same")).pack(side="left", padx=5)
        self.fixed_picker = FixedRatePicker(fixed, 生成固定止损档(), lambda: self.selected_fixed_stops,
                                            self._set_fixed_rates)
        self.fixed_picker.pack(anchor="w")
        self.fixed_stop_summary_var = tk.StringVar()
        ttk.Label(fixed, textvariable=self.fixed_stop_summary_var,
                  foreground="#7F6000", wraplength=1080, justify="left").pack(fill="x", pady=(6, 0))
        ttk.Label(fixed, foreground="#888888", wraplength=1080, justify="left",
                  text="多单止损价=开仓价×(1-比例)，空单=开仓价×(1+比例)。新增0.01%~0.09%细桶；"
                       "旧0.1%~1.0%编码不变。多个勾选分别生成组合；与信号止损、时间止损并行，先触发者退出。"
                  ).pack(fill="x", pady=(2, 0))

        time_box = ttk.LabelFrame(body, text="时间止损｜持仓时间作为正式退出规则", padding=8)
        time_box.pack(fill="x", pady=(0, 8))
        ttk.Label(time_box, text="到点无论盈亏按收盘价退出；0=不启用。多个时长分别生成组合。",
                  foreground="#7F6000").pack(anchor="w", pady=(0, 6))
        grid = ttk.Frame(time_box); grid.pack(anchor="w")
        for i, minutes in enumerate(强制时间止损档):
            var = tk.BooleanVar(value=(minutes == 0))
            self.hard_time_vars[minutes] = var
            text = "不启用" if minutes == 0 else (f"{minutes//60}小时" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}分钟")
            ttk.Checkbutton(grid, text=text, variable=var, command=self._update_summary).grid(
                row=i // 10, column=i % 10, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(time_box, foreground="#888888", wraplength=1080, justify="left",
                  text="时间止损与固定比例/ATR/信号止损属于同一退出层；时间止盈仍在止盈页，只有到点且有浮盈才退出。"
                  ).pack(fill="x", pady=(6, 0))

        filters = ttk.LabelFrame(body, text="信号止损筛选", padding=8)
        self._build_combo_button(body, ("止损",), "信号止损")
        filters.pack(fill="x", pady=(0, 8))
        self.stop_period_var = tk.StringVar(value="全部周期")
        self.stop_round_var = tk.StringVar(value="全部轮次")
        self.stop_component_var = tk.StringVar(value="全部止损规则")
        self.stop_selected_filter_var = tk.StringVar(value="全部组合")
        self.stop_search_var = tk.StringVar()
        # 这一行原来只是 LabelFrame 的标题文字，写成「第一轮 ｜ 第二轮」像页签却点不动。
        # 改成真的单选按钮，直接驱动原来的 stop_round_var，筛选逻辑不变。
        round_row = ttk.Frame(filters)
        round_row.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(round_row, text="轮次").pack(side="left", padx=(0, 8))
        for value, text in (("全部轮次", "全部轮次"),
                            ("第一轮", "第一轮 · MACD/结构止损"),
                            ("第二轮", "第二轮 · ATR/均线/连续不利收盘")):
            ttk.Radiobutton(round_row, text=text, value=value, variable=self.stop_round_var,
                            command=self._stop_filter_changed).pack(side="left", padx=(0, 12))
        filter_items = (
            ("周期", self.stop_period_var, ["全部周期", "通用", *周期列表], 12),
            ("包含规则", self.stop_component_var,
             ["全部止损规则", "①1分钟序列", "③前轮极值", "④反转量能不足", "⑤反转被反破",
              "⑥严格MACD反转", "⑨情况三反转量能充足", "ATR止损", "均线止损", "连续不利收盘"], 24),
            ("选择状态", self.stop_selected_filter_var,
             ["全部组合", "仅已选", "仅未选", "仅已测最优", "仅新增未测"], 14),
        )
        for col, (label, variable, values, width) in enumerate(filter_items):
            frame = ttk.Frame(filters)
            frame.grid(row=1, column=col, sticky="w", padx=(0, 12))
            ttk.Label(frame, text=label).pack(anchor="w")
            box = ttk.Combobox(frame, textvariable=variable, state="readonly", values=values, width=width)
            box.pack(anchor="w", pady=(2, 0))
            box.bind("<<ComboboxSelected>>", self._stop_filter_changed)
        search_frame = ttk.Frame(filters)
        search_frame.grid(row=1, column=3, sticky="ew")
        ttk.Label(search_frame, text="代码 / 说明关键词").pack(anchor="w")
        ttk.Entry(search_frame, textvariable=self.stop_search_var).pack(fill="x", pady=(2, 0))
        filters.columnconfigure(3, weight=1)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="筛选结果全部勾选", command=lambda: self._set_filtered_stops(True)).pack(side="left")
        ttk.Button(actions, text="筛选结果全部取消", command=lambda: self._set_filtered_stops(False)).pack(side="left", padx=5)
        ttk.Button(actions, text="本轮全选", command=lambda: self._set_round_stops(True)).pack(side="left", padx=(12, 0))
        ttk.Button(actions, text="本轮全不选", command=lambda: self._set_round_stops(False)).pack(side="left", padx=5)
        ttk.Button(actions, text="只看已选", command=self._show_selected_stops).pack(side="left", padx=(12, 5))
        ttk.Button(actions, text="清除筛选", command=self._clear_stop_filters).pack(side="left")
        ttk.Label(
            actions,
            text="④量能不足、⑤被反破、⑥普通反转是三个独立规则，可与①③交叉组合。",
            foreground="#7F6000",
        ).pack(side="left", padx=14)

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)
        columns = ("selected", "mark", "round", "code", "period", "components", "description")
        self.stop_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=18, selectmode="browse")
        headings = (
            ("selected", "选择", 52, "center", False),
            ("mark", "标记", 52, "center", False),
            ("round", "轮次", 58, "center", False),
            ("code", "止损代码", 250, "w", False),
            ("period", "周期", 70, "center", False),
            ("components", "包含规则", 200, "w", False),
            ("description", "完整说明", 460, "w", True),
        )
        for name, label, width, anchor, stretch in headings:
            self.stop_tree.heading(name, text=label)
            self.stop_tree.column(name, width=width, minwidth=45, anchor=anchor, stretch=stretch)
        self.stop_tree.tag_configure("best", background=最优行底色, foreground=最优色)
        self.stop_tree.tag_configure("new", background=新增行底色, foreground=新增色)
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.stop_tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.stop_tree.xview)
        self.stop_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.stop_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.stop_tree.bind("<Button-1>", self._toggle_stop)
        self.stop_tree.bind("<space>", self._toggle_selected_stop)

        pager = ttk.Frame(body)
        pager.pack(fill="x", pady=(8, 0))
        self.stop_match_var = tk.StringVar()
        ttk.Label(pager, textvariable=self.stop_match_var, foreground="#1F4E78").pack(side="left")
        ttk.Button(pager, text="上一页", command=lambda: self._change_stop_page(-1)).pack(side="right")
        self.stop_page_var = tk.StringVar()
        ttk.Label(pager, textvariable=self.stop_page_var, width=18, anchor="center").pack(side="right", padx=8)
        ttk.Button(pager, text="下一页", command=lambda: self._change_stop_page(1)).pack(side="right")
        self.stop_search_var.trace_add("write", self._schedule_stop_search)
        self.refresh_stop_tree()

    @staticmethod
    def _stop_component_key(label):
        return {
            "①1分钟序列": "S1",
            "③前轮极值": "S3",
            "④反转量能不足": "S4",
            "⑤反转被反破": "S5",
            "⑥严格MACD反转": "S6",
            "⑨情况三反转量能充足": "S9",
            "ATR止损": "ATR", "均线止损": "MA", "连续不利收盘": "BAD",
        }.get(label)

    def _visible_stops(self):
        round_filter = self.stop_round_var.get()
        period = self.stop_period_var.get()
        component_key = self._stop_component_key(self.stop_component_var.get())
        selected_filter = self.stop_selected_filter_var.get()
        keyword = self.stop_search_var.get().strip().lower()
        result = []
        for code, components, label in self.all_stops:
            round_name = self._stop_round(code)
            if round_filter != "全部轮次" and round_name != round_filter:
                continue
            if period == "通用" and code != "S1":
                continue
            if period not in ("全部周期", "通用") and f"_{period}" not in code:
                continue
            if component_key and not any(
                (part.startswith(component_key) if component_key in ("ATR", "MA", "BAD") else
                 part == component_key or part.startswith(component_key + "_")) for part in components
            ):
                continue
            is_selected = code in self.selected_stops
            if selected_filter == "仅已选" and not is_selected:
                continue
            if selected_filter == "仅未选" and is_selected:
                continue
            if selected_filter == "仅已测最优" and not 基线.是最优止损(code):
                continue
            if selected_filter == "仅新增未测" and not 基线.是新增止损(code):
                continue
            if keyword and keyword not in f"{code} {label}".lower():
                continue
            result.append((code, components, label))
        return result

    def _stop_round(self, code):
        """返回界面展示用的轮次；不改变代码的实际排序或回测含义。"""
        return "第一轮" if self.stop_order.get(code, 第一轮止损数量) < 第一轮止损数量 else "第二轮"

    def _stop_filter_changed(self, _event=None):
        self.stop_page = 0
        self.refresh_stop_tree()

    def _schedule_stop_search(self, *_args):
        if self._stop_search_after_id:
            self.after_cancel(self._stop_search_after_id)
        self._stop_search_after_id = self.after(250, self._stop_filter_changed)

    def _clear_stop_filters(self):
        self.stop_round_var.set("全部轮次")
        self.stop_period_var.set("全部周期")
        self.stop_component_var.set("全部止损规则")
        self.stop_selected_filter_var.set("全部组合")
        self.stop_search_var.set("")
        self._stop_filter_changed()

    def _show_selected_stops(self):
        self.stop_selected_filter_var.set("仅已选")
        self._stop_filter_changed()

    def refresh_stop_tree(self):
        matches = self._visible_stops()
        page_count = max(1, math.ceil(len(matches) / 每页止损数))
        self.stop_page = min(max(0, self.stop_page), page_count - 1)
        start = self.stop_page * 每页止损数
        page_rows = matches[start:start + 每页止损数]
        self.stop_tree.delete(*self.stop_tree.get_children())
        component_labels = {"S1": "①序列", "S3": "③极值", "S4": "④量能不足", "S5": "⑤被反破",
                            "S6": "⑥普通反转", "S9": "⑨情况三"}
        for code, components, label in page_rows:
            timeframe = "通用" if code == "S1" else next((tf for tf in 周期列表 if f"_{tf}" in code), "—")
            short_components = "+".join(component_labels.get(part.split("_", 1)[0], part) for part in components)
            best, new = 基线.是最优止损(code), 基线.是新增止损(code)
            mark = 最优标记 if best else (新增标记 if new else "")
            tags = ("best",) if best else (("new",) if new else ())
            self.stop_tree.insert(
                "", "end", iid="stop_" + code, tags=tags,
                values=("☑" if code in self.selected_stops else "☐", mark,
                        self._stop_round(code), code, timeframe, short_components, label),
            )
        shown_end = min(start + len(page_rows), len(matches))
        shown_text = "0" if not matches else f"{start + 1:,}—{shown_end:,}"
        self.stop_match_var.set(
            f"匹配 {len(matches):,} 种｜当前显示 {shown_text}｜已勾选 {len(self.selected_stops):,}/{len(self.all_stops):,}"
        )
        self.stop_page_var.set(f"第 {self.stop_page + 1}/{page_count} 页")

    def _change_stop_page(self, delta):
        self.stop_page += delta
        self.refresh_stop_tree()

    def _toggle_stop_id(self, code):
        if code in self.selected_stops:
            self.selected_stops.remove(code)
        else:
            self.selected_stops.add(code)
        self.refresh_stop_tree()
        self._update_summary()

    def _toggle_stop(self, event):
        if self.stop_tree.identify_region(event.x, event.y) != "cell" or self.stop_tree.identify_column(event.x) != "#1":
            return
        item = self.stop_tree.identify_row(event.y)
        if item:
            self._toggle_stop_id(item[5:])
            return "break"

    def _toggle_selected_stop(self, _event):
        selected = self.stop_tree.selection()
        if selected:
            self._toggle_stop_id(selected[0][5:])
        return "break"

    def _set_filtered_stops(self, enabled):
        codes = {code for code, _components, _label in self._visible_stops()}
        if enabled:
            self.selected_stops.update(codes)
        else:
            self.selected_stops.difference_update(codes)
        self.refresh_stop_tree()
        self._update_summary()

    def _set_round_stops(self, enabled):
        """只操作轮次下的全部止损，不受周期、关键词和分页影响。"""
        round_filter = self.stop_round_var.get()
        codes = {
            code for code, _components, _label in self.all_stops
            if round_filter == "全部轮次" or self._stop_round(code) == round_filter
        }
        if enabled:
            self.selected_stops.update(codes)
        else:
            self.selected_stops.difference_update(codes)
        self.refresh_stop_tree()
        self._update_summary()

    def _fixed_stop_code(self):
        weekday = 百分数转比例(self.fixed_stop_weekday_var.get())
        raw = self.fixed_stop_weekend_var.get()
        weekend = weekday if raw == "跟随工作日" else 百分数转比例(raw)
        return 固定比例代码("FSL", weekday, weekend)

    def _add_fixed_stop(self):
        self.selected_fixed_stops.add(self._fixed_stop_code())
        self._refresh_fixed_stop_summary(); self._update_summary()

    def _preset_fixed_stops(self, preset):
        self.selected_fixed_stops = ({"OFF"} if preset == "off"
                                     else set(固定止损同档代码()))
        self._refresh_fixed_stop_summary(); self._update_summary()

    def _refresh_fixed_stop_summary(self):
        if hasattr(self, "fixed_picker"):
            self.fixed_picker.sync()
        rows = 补全固定比例档(self.selected_fixed_stops, "FSL")
        labels = {row[0]: row[3] for row in rows}
        chosen = [row[0] for row in rows if row[0] in self.selected_fixed_stops]
        if not chosen:
            self.selected_fixed_stops = {"OFF"}; chosen = ["OFF"]
        shown = "；".join("%s(%s)" % (c, labels[c]) for c in chosen[:8])
        more = "" if len(chosen) <= 8 else " 等共%d档" % len(chosen)
        new_count = len([c for c in chosen if c in 基线.新增固定止损代码 or "B" in c])
        tail = ("　%s其中 %d 档是新增未测" % (新增标记, new_count)) if new_count else ""
        self.fixed_stop_summary_var.set("已选 %d 档：%s%s%s" % (len(chosen), shown, more, tail))

    def _build_overlay_tp(self, body):
        box = ttk.LabelFrame(body, text="叠加固定比例止盈｜0.01%起步；可与动态止盈并行", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.overlay_weekday_var = tk.StringVar(value="0.4%")
        self.overlay_weekend_var = tk.StringVar(value="跟随工作日")
        rates = [f"{r*100:.2f}%".rstrip("0").rstrip(".") for r in 固定止损比例档]
        row = ttk.Frame(box)  # 保留旧配置解析，不显示旧逐档加入控件。
        ttk.Label(row, text="工作日").pack(side="left")
        ttk.Combobox(row, textvariable=self.overlay_weekday_var, state="readonly",
                     width=8, values=rates).pack(side="left", padx=(6, 16))
        ttk.Label(row, text="周末").pack(side="left")
        ttk.Combobox(row, textvariable=self.overlay_weekend_var, state="readonly",
                     width=10, values=["跟随工作日", *rates]).pack(side="left", padx=(6, 16))
        ttk.Button(row, text="加入所选", command=self._add_overlay_tp).pack(side="left")
        ttk.Button(row, text="只保留OFF",
                   command=lambda: self._preset_overlay_tp("off")).pack(side="left", padx=5)
        ttk.Button(row, text="OFF+旧同档10种",
                   command=lambda: self._preset_overlay_tp("same")).pack(side="left", padx=5)
        self.overlay_picker = FixedRatePicker(box, 生成叠加止盈档(), lambda: self.selected_overlay_tps,
                                              self._set_overlay_rates)
        self.overlay_picker.pack(anchor="w")
        self.overlay_summary_var = tk.StringVar()
        ttk.Label(box, textvariable=self.overlay_summary_var, foreground="#7F6000",
                  wraplength=1080, justify="left").pack(fill="x", pady=(6, 0))
        ttk.Label(box, foreground="#888888", wraplength=1080, justify="left",
                  text="固定比例与所选止盈并行，谁先触发算谁。多个勾选分别生成组合。"
                       "分批止盈和盈亏比止盈不参与此叠加；回测成交价口径不代表Maker必然成交。"
                  ).pack(fill="x", pady=(2, 0))
        self._refresh_overlay_summary()

    def _overlay_code(self):
        wd = 百分数转比例(self.overlay_weekday_var.get())
        raw = self.overlay_weekend_var.get()
        we = wd if raw == "跟随工作日" else 百分数转比例(raw)
        return 固定比例代码("FTP", wd, we)

    def _add_overlay_tp(self):
        self.selected_overlay_tps.add(self._overlay_code())
        self._refresh_overlay_summary(); self._update_summary()

    def _preset_overlay_tp(self, preset):
        self.selected_overlay_tps = ({"OFF"} if preset == "off"
                                     else set(叠加止盈同档代码()))
        self._refresh_overlay_summary(); self._update_summary()

    def _refresh_overlay_summary(self):
        if hasattr(self, "overlay_picker"):
            self.overlay_picker.sync()
        rows = 补全固定比例档(self.selected_overlay_tps, "FTP")
        labels = {r[0]: r[3] for r in rows}
        chosen = [r[0] for r in rows if r[0] in self.selected_overlay_tps]
        if not chosen:
            self.selected_overlay_tps = {"OFF"}; chosen = ["OFF"]
        shown = "；".join("%s(%s)" % (c, labels[c]) for c in chosen[:8])
        more = "" if len(chosen) <= 8 else " 等共%d档" % len(chosen)
        new_count = len([c for c in chosen if c in 基线.新增叠加止盈代码 or "B" in c])
        tail = ("　%s其中 %d 档是新增未测" % (新增标记, new_count)) if new_count else ""
        self.overlay_summary_var.set("已选 %d 档：%s%s%s" % (len(chosen), shown, more, tail))

    def _build_tps(self, body):
        self._build_overlay_tp(body)
        self._build_combo_button(body, ("止盈",), "止盈")
        filter_box = ttk.LabelFrame(body, text="止盈方案筛选", padding=8)
        filter_box.pack(fill="x", pady=(0, 8))
        categories = ["全部类别"] + list(dict.fromkeys(x.类别 for x in self.all_tps))
        indicators = ["全部指标", "无指标"] + sorted({x.指标 for x in self.all_tps if x.指标})
        self.tp_category_var = tk.StringVar(value="全部类别")
        self.tp_round_var = tk.StringVar(value="全部轮次")
        self.tp_period_var = tk.StringVar(value="全部周期")
        self.tp_indicator_var = tk.StringVar(value="全部指标")
        self.tp_selected_filter_var = tk.StringVar(value="全部方案")
        self.tp_search_var = tk.StringVar()
        # 同止损：原来的标题文字看着像页签，这里换成能点的单选按钮。
        round_row = ttk.Frame(filter_box)
        round_row.grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        ttk.Label(round_row, text="轮次").pack(side="left", padx=(0, 8))
        for value, text in (("全部轮次", "全部轮次"),
                            ("第一轮", "第一轮 · 原止盈类别"),
                            ("第二轮", "第二轮 · ATR/均线/量能及移动止盈细桶"),
                            ("v1.41退出扩展", "v1.41 · 0.01%固定退出 / 时间细桶")):
            ttk.Radiobutton(round_row, text=text, value=value, variable=self.tp_round_var,
                            command=self._tp_filter_changed).pack(side="left", padx=(0, 12))

        filter_items = (
            ("止盈类别", self.tp_category_var, categories, 24),
            ("包含周期", self.tp_period_var, ["全部周期", "无周期", *周期列表], 12),
            ("指标", self.tp_indicator_var, indicators, 22),
            ("选择状态", self.tp_selected_filter_var,
             ["全部方案", "仅已选", "仅未选", "仅已测最优"], 14),
        )
        for col, (label, variable, values, width) in enumerate(filter_items):
            frame = ttk.Frame(filter_box)
            frame.grid(row=1, column=col, sticky="w", padx=(0, 10))
            ttk.Label(frame, text=label).pack(anchor="w")
            box = ttk.Combobox(frame, textvariable=variable, state="readonly", values=values, width=width)
            box.pack(anchor="w", pady=(2, 0))
            box.bind("<<ComboboxSelected>>", self._tp_filter_changed)

        search_frame = ttk.Frame(filter_box)
        search_frame.grid(row=1, column=4, sticky="ew")
        ttk.Label(search_frame, text="关键词 / 编号 / 参数").pack(anchor="w")
        search = ttk.Entry(search_frame, textvariable=self.tp_search_var)
        search.pack(fill="x", pady=(2, 0))
        filter_box.columnconfigure(4, weight=1)
        fine = ttk.Frame(filter_box)
        fine.grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.tp_fine_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(fine, text="仅筛选移动回吐细桶", variable=self.tp_fine_enabled,
                        command=self._tp_filter_changed).pack(side="left")
        self.tp_activation_min = tk.StringVar(value="0.1")
        self.tp_activation_max = tk.StringVar(value="0.1")
        self.tp_retrace_min = tk.StringVar(value="0.01")
        self.tp_retrace_max = tk.StringVar(value="0.1")
        for label, a, b in (("启动浮盈%", self.tp_activation_min, self.tp_activation_max),
                            ("回吐系数", self.tp_retrace_min, self.tp_retrace_max)):
            ttk.Label(fine, text=label).pack(side="left", padx=(10, 3))
            ttk.Entry(fine, textvariable=a, width=6).pack(side="left")
            ttk.Label(fine, text="至").pack(side="left")
            ttk.Entry(fine, textvariable=b, width=6).pack(side="left")
        ttk.Button(fine, text="筛选", command=self._tp_filter_changed).pack(side="left", padx=8)
        ttk.Label(fine, text="系数0.01=回吐浮盈1%；筛选不改勾选").pack(side="left")

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="筛选结果全部勾选", command=lambda: self._set_filtered_tps(True)).pack(side="left")
        ttk.Button(actions, text="筛选结果全部取消", command=lambda: self._set_filtered_tps(False)).pack(
            side="left", padx=5
        )
        ttk.Button(actions, text="本轮全选", command=lambda: self._set_round_tps(True)).pack(side="left", padx=(12, 0))
        ttk.Button(actions, text="本轮全不选", command=lambda: self._set_round_tps(False)).pack(side="left", padx=5)
        ttk.Button(actions, text="只看已选", command=self._show_selected_tps).pack(side="left", padx=(12, 5))
        ttk.Button(actions, text="清除筛选", command=self._clear_tp_filters).pack(side="left")
        ttk.Label(
            actions,
            text="批量按钮作用于全部匹配项（包括未显示的分页）",
            foreground="#7F6000",
        ).pack(side="left", padx=14)

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)
        columns = ("selected", "mark", "round", "id", "category", "period", "indicator", "params", "description")
        self.tp_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=17, selectmode="browse")
        headings = (
            ("selected", "选择", 52, "center", False),
            ("mark", "标记", 52, "center", False),
            ("round", "轮次", 58, "center", False),
            ("id", "编号", 66, "center", False),
            ("category", "止盈类别", 162, "w", False),
            ("period", "周期组合", 108, "w", False),
            ("indicator", "指标", 126, "w", False),
            ("params", "参数一 / 二 / 三", 146, "w", False),
            ("description", "方案说明", 400, "w", True),
        )
        for name, label, width, anchor, stretch in headings:
            self.tp_tree.heading(name, text=label)
            self.tp_tree.column(name, width=width, minwidth=45, anchor=anchor, stretch=stretch)
        self.tp_tree.tag_configure("best", background=最优行底色, foreground=最优色)
        self.tp_tree.tag_configure("new", background=新增行底色, foreground=新增色)
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tp_tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tp_tree.xview)
        self.tp_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tp_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tp_tree.bind("<Button-1>", self._toggle_tp)
        self.tp_tree.bind("<space>", self._toggle_selected_tp)

        pager = ttk.Frame(body)
        pager.pack(fill="x", pady=(8, 0))
        self.tp_match_var = tk.StringVar()
        ttk.Label(pager, textvariable=self.tp_match_var, foreground="#1F4E78").pack(side="left")
        ttk.Button(pager, text="上一页", command=lambda: self._change_tp_page(-1)).pack(side="right")
        self.tp_page_var = tk.StringVar()
        ttk.Label(pager, textvariable=self.tp_page_var, width=18, anchor="center").pack(side="right", padx=8)
        ttk.Button(pager, text="下一页", command=lambda: self._change_tp_page(1)).pack(side="right")

        self.tp_search_var.trace_add("write", self._schedule_tp_search)
        self.refresh_tp_tree()

    @staticmethod
    def _period_matches(tp_periods, selected_period):
        if selected_period == "全部周期":
            return True
        if selected_period == "无周期":
            return not tp_periods
        return selected_period in tp_periods.split("+")

    def _visible_tps(self):
        round_filter = self.tp_round_var.get()
        category = self.tp_category_var.get()
        period = self.tp_period_var.get()
        indicator = self.tp_indicator_var.get()
        selected_filter = self.tp_selected_filter_var.get()
        keyword = self.tp_search_var.get().strip().lower()
        result = []
        for tp in self.all_tps:
            round_name = self._tp_round(tp.编号)
            if round_filter != "全部轮次" and round_name != round_filter:
                continue
            if self.tp_fine_enabled.get():
                try:
                    amin, amax = float(self.tp_activation_min.get()) / 100., float(self.tp_activation_max.get()) / 100.
                    rmin, rmax = float(self.tp_retrace_min.get()), float(self.tp_retrace_max.get())
                except ValueError:
                    continue
                if not (tp.类别 == "移动止盈" and tp.指标 == "最高浮盈回吐比例"
                        and amin <= tp.参数一 <= amax and rmin <= tp.参数二 <= rmax):
                    continue
            if category != "全部类别" and tp.类别 != category:
                continue
            if not self._period_matches(tp.周期组合, period):
                continue
            if indicator == "无指标" and tp.指标:
                continue
            if indicator not in ("全部指标", "无指标") and tp.指标 != indicator:
                continue
            is_selected = tp.编号 in self.selected_tps
            if selected_filter == "仅已选" and not is_selected:
                continue
            if selected_filter == "仅未选" and is_selected:
                continue
            if selected_filter == "仅已测最优" and not 基线.是最优止盈(tp.编号):
                continue
            haystack = (
                f"{tp.编号} {tp.类别} {tp.周期组合} {tp.指标} "
                f"{tp.参数一:g} {tp.参数二:g} {tp.参数三:g} {tp.说明}"
            ).lower()
            if keyword and keyword not in haystack:
                continue
            result.append(tp)
        return result

    @staticmethod
    def _tp_round(tp_id):
        """止盈编号严格追加：1~8280第一轮，8281~8865第二轮，之后为v1.41退出扩展。"""
        tp_id = int(tp_id)
        if tp_id <= 第一轮止盈编号上限:
            return "第一轮"
        if tp_id <= 第二轮止盈编号上限:
            return "第二轮"
        return "v1.41退出扩展"

    def _tp_filter_changed(self, _event=None):
        self.tp_page = 0
        self.refresh_tp_tree()

    def _schedule_tp_search(self, *_args):
        if self._search_after_id:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(250, self._tp_filter_changed)

    def _clear_tp_filters(self):
        self.tp_fine_enabled.set(False)
        self.tp_round_var.set("全部轮次")
        self.tp_category_var.set("全部类别")
        self.tp_period_var.set("全部周期")
        self.tp_indicator_var.set("全部指标")
        self.tp_selected_filter_var.set("全部方案")
        self.tp_search_var.set("")
        self._tp_filter_changed()

    def _show_selected_tps(self):
        self.tp_selected_filter_var.set("仅已选")
        self._tp_filter_changed()

    def refresh_tp_tree(self):
        matches = self._visible_tps()
        page_count = max(1, math.ceil(len(matches) / 每页止盈数))
        self.tp_page = min(max(0, self.tp_page), page_count - 1)
        start = self.tp_page * 每页止盈数
        page_rows = matches[start:start + 每页止盈数]
        self.tp_tree.delete(*self.tp_tree.get_children())
        for tp in page_rows:
            params = f"{tp.参数一:g} / {tp.参数二:g} / {tp.参数三:g}"
            best, new = 基线.是最优止盈(tp.编号), (tp.编号 in 基线.新增止盈编号 or tp.编号 > 第二轮止盈编号上限)
            mark = 最优标记 if best else (新增标记 if new else "")
            tags = ("best",) if best else (("new",) if new else ())
            self.tp_tree.insert(
                "",
                "end",
                iid=f"tp_{tp.编号}",
                tags=tags,
                values=(
                    "☑" if tp.编号 in self.selected_tps else "☐",
                    mark,
                    self._tp_round(tp.编号),
                    tp.编号,
                    tp.类别,
                    tp.周期组合 or "—",
                    tp.指标 or "—",
                    params,
                    tp.说明,
                ),
            )
        shown_end = min(start + len(page_rows), len(matches))
        shown_text = "0" if not matches else f"{start + 1:,}—{shown_end:,}"
        self.tp_match_var.set(
            f"匹配 {len(matches):,} 种｜当前显示 {shown_text}｜已勾选 {len(self.selected_tps):,}/{len(self.all_tps):,}"
        )
        self.tp_page_var.set(f"第 {self.tp_page + 1}/{page_count} 页")

    def _change_tp_page(self, delta):
        self.tp_page += delta
        self.refresh_tp_tree()

    def _toggle_tp_id(self, tp_id):
        if tp_id in self.selected_tps:
            self.selected_tps.remove(tp_id)
        else:
            self.selected_tps.add(tp_id)
        self.refresh_tp_tree()
        self._update_summary()

    def _toggle_tp(self, event):
        if self.tp_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tp_tree.identify_column(event.x) != "#1":
            return
        item = self.tp_tree.identify_row(event.y)
        if not item:
            return
        self._toggle_tp_id(int(item.split("_", 1)[1]))
        return "break"

    def _toggle_selected_tp(self, _event):
        selected = self.tp_tree.selection()
        if selected:
            self._toggle_tp_id(int(selected[0].split("_", 1)[1]))
        return "break"

    def _set_filtered_tps(self, enabled):
        ids = {tp.编号 for tp in self._visible_tps()}
        if enabled:
            self.selected_tps.update(ids)
        else:
            self.selected_tps.difference_update(ids)
        self.refresh_tp_tree()
        self._update_summary()

    def _set_round_tps(self, enabled):
        """只操作轮次下的全部止盈，不受类别、关键词和分页影响。"""
        round_filter = self.tp_round_var.get()
        ids = {
            tp.编号 for tp in self.all_tps
            if round_filter == "全部轮次" or self._tp_round(tp.编号) == round_filter
        }
        if enabled:
            self.selected_tps.update(ids)
        else:
            self.selected_tps.difference_update(ids)
        self.refresh_tp_tree()
        self._update_summary()

    def _build_cooldowns(self, body):
        self.cooldown_vars = {}
        best_cooldowns = set(基线.已测最优["止盈后等待分钟"])
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(0, 12))
        ttk.Label(actions, text="常用范围：", foreground="#1F4E78").pack(side="left")
        for label, values in (
            ("★仅1分钟（已测最优）", {1}),
            ("0 / 1分钟对照", {0, 1}),
            ("0 / 1 / 5 / 10分钟", {0, 1, 5, 10}),
            ("0—5分钟", set(range(0, 6))),
            ("0—10分钟", set(range(0, 11))),
            ("0—30分钟全部（D轮）", set(range(0, 31))),
        ):
            ttk.Button(actions, text=label, command=lambda chosen=values: self._set_cooldowns(chosen)).pack(
                side="left", padx=3
            )
        ttk.Button(actions, text="清空", command=lambda: self._set_vars(self.cooldown_vars, False)).pack(
            side="right"
        )

        ttk.Label(
            body,
            text="等待时间只在止盈退出后生效；止损或强平保护退出不触发。"
                 "0分钟＝止盈后不额外等待，与止损退出一致，是判断“等待到底有没有用”的基准档。",
            foreground="#7F6000", wraplength=1100, justify="left",
        ).pack(anchor="w", pady=(0, 10))
        grid = ttk.LabelFrame(body, text="0—30分钟（每1分钟一档，共31档）", padding=12)
        grid.pack(fill="x")
        # 0 档后端一直支持（规范化配置取 range(0,31)），但界面从来没建过这个勾选框，
        # 所以它既选不上、存进设置里也会被 apply_config 静默丢掉。这里补上。
        for index, minute in enumerate(range(0, 31)):
            var = tk.BooleanVar(value=True)
            self.cooldown_vars[minute] = var
            best = minute in best_cooldowns
            new = minute in 基线.新增等待分钟
            text = "0分钟（不等待）" if minute == 0 else f"{minute}分钟"
            ttk.Checkbutton(grid, text=标注(text, 是最优=best, 是新增=new), variable=var,
                            command=self._update_summary,
                            style=("Best.TCheckbutton" if best
                                   else "New.TCheckbutton" if new else "TCheckbutton")).grid(
                row=index // 8, column=index % 8, sticky="w", padx=(0, 16), pady=5
            )
        for col in range(8):
            grid.columnconfigure(col, weight=1)
        ttk.Label(body, wraplength=1100, justify="left", foreground=最优色,
                  text=f"{最优标记} 已测最优：{基线.最优说明['止盈后等待分钟']}　　"
                       "0分钟表示止盈后不额外等待；第二轮等待组保留它作对照。").pack(anchor="w", pady=(8, 0))

    def _set_cooldowns(self, selected):
        for minute, var in self.cooldown_vars.items():
            var.set(minute in selected)
        self._update_summary()

    def _build_sizes(self, body):
        self.size_vars = {}
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(0, 12))
        ttk.Label(actions, text="常用范围：", foreground="#1F4E78").pack(side="left")
        for label, values in (
            ("★仅2x（用户选定对照）", {2.0}),
            ("低杠杆对照 2x / 3x / 5x / 10x", {2.0, 3.0, 5.0, 10.0}),
            ("仓位0.1—1.0", {x / 10 for x in range(1, 11)}),
            ("2x—10x", {float(x) for x in range(2, 11)}),
            ("20x / 50x / 100x", {20.0, 50.0, 100.0}),
        ):
            ttk.Button(actions, text=label, command=lambda chosen=values: self._set_sizes(chosen)).pack(
                side="left", padx=3
            )
        ttk.Button(actions, text="全选", command=lambda: self._set_vars(self.size_vars, True)).pack(side="right")
        ttk.Button(actions, text="清空", command=lambda: self._set_vars(self.size_vars, False)).pack(
            side="right", padx=5
        )

        ttk.Label(
            body,
            text="这里决定实际回测杠杆，每档独立重放账户。候选页杠杆仅筛选对应结果；数量上限按运行页设置，最高100 ETH。",
            foreground="#7F6000",
            wraplength=1100,
        ).pack(anchor="w", pady=(0, 10))

        groups = (
            ("仓位比例", [x for x in 仓位列表 if x <= 1.0]),
            ("低至中杠杆", [x for x in 仓位列表 if 1.0 < x <= 10.0]),
            ("高杠杆", [x for x in 仓位列表 if x > 10.0]),
        )
        best_sizes = set(基线.已测最优["仓位倍数"])
        for title, values in groups:
            group = ttk.LabelFrame(body, text=title, padding=10)
            group.pack(fill="x", pady=(0, 10))
            for index, size in enumerate(values):
                var = tk.BooleanVar(value=True)
                self.size_vars[size] = var
                label = f"{size:g}x" if size > 1 else f"{size:.1f}仓"
                best = size in best_sizes
                ttk.Checkbutton(group, text=标注(label, 是最优=best), variable=var,
                                command=self._update_summary,
                                style="Best.TCheckbutton" if best else "TCheckbutton").grid(
                    row=index // 10, column=index % 10, sticky="w", padx=(0, 24), pady=4
                )
        ttk.Label(body, foreground=最优色,
                  text=f"{最优标记} 已测最优：{基线.最优说明['仓位倍数']}").pack(anchor="w")

    def _set_sizes(self, selected):
        for size, var in self.size_vars.items():
            var.set(size in selected)
        self._update_summary()

    def _set_vars(self, values, enabled):
        for var in values.values():
            var.set(enabled)
        self._update_summary()

    def get_entry_modes(self):
        modes = [code for code in self.entry_mode_options if self.entry_mode_vars[code].get()]
        if not modes:
            raise ValueError("至少选择一种入场触发/次数规则")
        return modes

    def set_entry_modes(self, value, *, update_summary=True):
        display_codes = {label: code for code, label in self.entry_mode_options.items()}
        raw = [display_codes.get(item, item) if isinstance(item, str) else item for item in value] if isinstance(value, list) else (
            display_codes.get(value, value) if isinstance(value, str) else value)
        normalized = 规范化入场触发口径(raw)
        modes = [normalized] if isinstance(normalized, str) else normalized
        for code, var in self.entry_mode_vars.items():
            var.set(code in modes)
        self._sync_entry_mode_summary()
        if update_summary:
            self._update_summary()

    def _sync_entry_mode_summary(self):
        modes = [code for code in self.entry_mode_options if self.entry_mode_vars[code].get()]
        text = (self.entry_mode_options[modes[0]] if len(modes) == 1 else
                f"分别回测 {len(modes)} 种：{' / '.join(modes)}" if modes else "未选择入场规则")
        if self.entry_mode_var.get() != text:
            self.entry_mode_var.set(text)

    def _entry_modes_changed(self):
        self._sync_entry_mode_summary()
        self._update_summary()

    def _use_regular_entry_mode(self):
        self.set_entry_modes(默认入场触发口径)
        self.policy_notice_var.set("已切换为普通入场模式：每个1m指标方向段最多一次；含第五批的组合仍自动按自身事件运行。")
        self.show_entry_constraints()

    def _combo_spec(self, path):
        value = self.indicator_combos
        for key in path:
            value = value.get(key, {})
        return {"启用": False, "组合数量": [2], "保留单项": True,
                "逻辑": "AND" if path[0] == "开仓" else "OR", **value}

    def _build_combo_button(self, parent, path, title):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 7))
        button = ttk.Button(row, text="指标组合", command=lambda: self._show_combo_picker(path, title))
        button.pack(side="left")
        status = tk.StringVar(value="关闭 · 已勾选项分别回测")
        ttk.Label(row, textvariable=status, foreground="#1F4E78").pack(side="left", padx=10)
        self.combo_buttons[path] = button
        self.combo_status_vars[path] = status

    def _combo_pool(self, path):
        if path[0] == "开仓":
            names = 一分钟情况名称 if path[1] == "1m" else 情况名称
            return [(code, names[code]) for code, var in self.case_vars[path[1]].items() if var.get()], {0}
        if path[0] == "止损":
            return [(code, label) for code, _, label in self.all_stops if code in self.selected_stops], {"OFF"}
        return ([(tp.编号, tp.说明 or tp.类别) for tp in self.all_tps if tp.编号 in self.selected_tps],
                {tp.编号 for tp in self.all_tps if tp.类别 in ("不使用止盈", "分批止盈")})

    def _show_combo_picker(self, path, title):
        pool, excluded = self._combo_pool(path)
        return IndicatorComboPicker(self, title, self._combo_spec(path), pool, excluded,
                                    lambda spec: self._apply_combo_spec(path, spec))

    def _apply_combo_spec(self, path, spec):
        parent = self.indicator_combos
        for key in path[:-1]:
            parent = parent.setdefault(key, {})
        parent[path[-1]] = copy.deepcopy(spec)
        self._update_summary()

    def _update_combo_summaries(self):
        for path, var in self.combo_status_vars.items():
            spec = self._combo_spec(path)
            if spec["启用"]:
                sizes = "/".join(str(n) for n in spec["组合数量"])
                singles = "；含单项对照" if spec["保留单项"] else "；只跑组合"
                var.set(f"已开启 · {sizes} 项组合{singles}")
            else:
                var.set("关闭 · 已勾选项分别回测")

    def get_config(self):
        modes = self.get_entry_modes()
        costs = [code for code, var in self.cost_mode_vars.items() if var.get()]
        return 规范化配置({
            # 必须带版本号。不带的话 规范化配置 会当成 v5 及更早的旧设置，
            # 触发 S4→S6 迁移，把界面上勾的④反转量能不足全部改写成⑥严格MACD反转：
            # 912 种止损组合会当场塌成 432 种，含④的那 480 种永远选不上。
            "版本": 20,
            "指标组合": copy.deepcopy(self.indicator_combos),
            "开仓指标": [key for key, var in self.field_vars.items() if var.get()],
            "开仓条件": {
                timeframe: [value for value, var in values.items() if var.get()]
                for timeframe, values in self.case_vars.items()
            },
            "开仓位置过滤": [code for code in self.position_filter_order if self.position_filter_vars[code].get()],
            "入场触发口径": modes[0] if len(modes) == 1 else modes,
            "成本模式": costs[0] if len(costs) == 1 else costs,
            "止损代码": sorted(self.selected_stops, key=self.stop_order.__getitem__),
            "固定止损代码": [row[0] for row in 补全固定比例档(self.selected_fixed_stops, "FSL")],
            "叠加止盈代码": [row[0] for row in 补全固定比例档(self.selected_overlay_tps, "FTP")],
            "开仓方向": [c for c, _ in 方向列表 if self.direction_vars[c].get()] or ["BOTH"],
            "交易会话": [c for c, _ in 会话列表 if self.session_vars[c].get()] or ["ALL"],
            "强制时间止损分钟": [m for m in 强制时间止损档
                          if self.hard_time_vars[m].get()] or [0],
            "止盈方案编号": sorted(self.selected_tps),
            "止盈后等待分钟": [key for key, var in self.cooldown_vars.items() if var.get()],
            "仓位倍数": [key for key, var in self.size_vars.items() if var.get()],
        })

    def _update_tab_badges(self, counts=None):
        entry_fields = sum(var.get() for var in self.field_vars.values())
        entry_cases = math.prod(sum(var.get() for var in values.values()) for values in self.case_vars.values())
        position_count = sum(var.get() for var in self.position_filter_vars.values())
        mode_count = sum(var.get() for var in self.entry_mode_vars.values())
        stop_count = len(self.selected_stops)
        tp_count = len(self.selected_tps)
        if counts is not None:
            entry_cases = counts["入场组合数"]
            entry_fields = position_count = mode_count = 1
            stop_count = counts["信号止损数"]
            tp_count = counts["止盈方案数"]
        cooldown_count = sum(var.get() for var in self.cooldown_vars.values())
        size_count = sum(var.get() for var in self.size_vars.values())
        titles = (
            f"  1. 开仓（{entry_fields * entry_cases * position_count * mode_count:,}）  ",
            f"  2. 止损（信号{stop_count}）  ",
            f"  3. 止盈（{tp_count:,}）  ",
            f"  4. 等待（{cooldown_count}）  ",
            f"  5. 仓位（{size_count}）  ",
        )
        for index, title in enumerate(titles):
            self.pages.tab(index, text=title)

    def _set_second_round(self, timeframe):
        enabled = self.entry_all_vars[timeframe].get()
        for code in SECOND_ROUND_CODES:
            if code in self.case_vars[timeframe]:
                self.case_vars[timeframe][code].set(enabled)
        self._update_summary()

    def _set_position_filters(self, codes):
        self.position_filter_order = list(codes) + [code for code in POSITION_FILTERS if code not in codes]
        for code, var in self.position_filter_vars.items():
            var.set(code in codes)
        self._update_summary()

    def _set_third_round(self, timeframe):
        enabled = self.third_entry_all_vars[timeframe].get()
        for code in THIRD_ROUND_CODES:
            if code in self.case_vars[timeframe]:
                self.case_vars[timeframe][code].set(enabled)
        self._update_summary()

    def _set_third_source(self, timeframe, source):
        """第三批按数据来源快捷选择；available只选择当前数据真正具备的规则。"""
        if source == "available" and not self._data_capabilities_known:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”；现有勾选保持不变。")
            return
        if source == "kline":
            wanted = set(KLINE_ONLY_THIRD_CODES)
        elif source == "micro":
            wanted = set(MICROSTRUCTURE_THIRD_CODES)
        elif source == "external":
            wanted = set(EXTERNAL_THIRD_CODES)
        else:
            wanted = {c for c in THIRD_ROUND_CODES if self._entry_available(timeframe, c)}
        for code in THIRD_ROUND_CODES:
            if code in self.case_vars[timeframe]:
                self.case_vars[timeframe][code].set(code in wanted)
        self._update_summary()

    def _set_fourth_round(self, timeframe):
        enabled=self.fourth_entry_all_vars[timeframe].get()
        for c in FOURTH_ROUND_CODES:
            if c in self.case_vars[timeframe]:self.case_vars[timeframe][c].set(enabled)
        self._update_summary()

    def _set_fourth_source(self,timeframe,source):
        if source == "available" and not self._data_capabilities_known:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”；现有勾选保持不变。")
            return
        wanted=set(KLINE_ONLY_FOURTH_CODES if source=="kline" else MICROSTRUCTURE_FOURTH_CODES if source=="micro" else
                   [c for c in FOURTH_ROUND_CODES if self._entry_available(timeframe, c)])
        for c in FOURTH_ROUND_CODES:
            if c in self.case_vars[timeframe]:self.case_vars[timeframe][c].set(c in wanted)
        self._update_summary()

    def _set_fifth_round(self, timeframe):
        enabled = self.fifth_entry_all_vars[timeframe].get()
        for code in FIFTH_ROUND_CODES:
            if code in self.case_vars[timeframe]:
                self.case_vars[timeframe][code].set(enabled and self._entry_available(timeframe, code))
        self._update_summary()

    def _set_fifth_source(self, timeframe, source):
        if source == "available" and not self._data_capabilities_known:
            messagebox.showinfo("先检测数据", "请先点击“检测数据与策略覆盖”；现有勾选保持不变。")
            return
        pool = KLINE_ONLY_FIFTH_CODES if source == "kline" else MICROSTRUCTURE_FIFTH_CODES if source == "micro" else FIFTH_ROUND_CODES
        wanted = {c for c in pool if self._entry_available(timeframe, c)}
        for code in FIFTH_ROUND_CODES:
            if code in self.case_vars[timeframe]:
                self.case_vars[timeframe][code].set(code in wanted)
        self._update_summary()

    def _show_fifth_catalog(self):
        from fifth_batch import FIFTH_SPECS
        dialog = tk.Toplevel(self)
        dialog.title("第五批保留39种开仓方法 · 定义与可用状态")
        fit_window(dialog, 1100, 760)
        dialog.transient(self.winfo_toplevel())
        body = ttk.Frame(dialog, padding=10); body.pack(fill="both", expand=True)
        ttk.Label(body, text="仅显示用户指定保留的39种；其余81种永久删除，不能通过全选、旧设置或指纹导入恢复。",
                  foreground="#7F6000").pack(anchor="w", pady=(0, 8))
        details = tk.Text(body, height=11, wrap="word")
        details.pack(side="bottom", fill="x", pady=(8, 0))
        columns = ("id", "name", "tf", "requires", "status")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        bar = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y"); tree.pack(fill="both", expand=True)
        for key, title, width in zip(columns, ("研究编号", "方法", "原生支持周期", "所需数据", "计算状态"), (100, 250, 150, 200, 330)):
            tree.heading(key, text=title); tree.column(key, width=width, minwidth=70)
        for code, spec in sorted(FIFTH_SPECS.items()):
            if code not in ACTIVE_FIFTH_CODES:
                continue
            tree.insert("", "end", iid=str(code), values=(spec["research_id"], spec["name"],
                        "/".join(spec["supported_timeframes"]), "/".join(spec["requires"]) or "OHLCV/时间",
                        spec.get("unavailable_reason") or "已接入；仍需检测当前数据"))
        def show(_event=None):
            chosen = tree.selection()
            if not chosen: return
            code = int(chosen[0]); spec = FIFTH_SPECS[code]
            details.configure(state="normal"); details.delete("1.0", "end")
            details.insert("end", f"{spec['research_id']} / 代码{code} / {spec['name']}\n{spec['definition']}\n\n"
                           + (spec.get("unavailable_reason") or "已接入独立方向与因果事件计算。具体数据能力以检测结果为准。"))
            details.configure(state="disabled")
        tree.bind("<<TreeviewSelect>>", show)
        if tree.get_children(): tree.selection_set(tree.get_children()[0]); show()
        return dialog

    def _entry_available(self, timeframe, code):
        if fifth_retirement_reason(code):
            return False
        if timeframe not in entry_supported_timeframes(code):
            return False
        if code in FIFTH_ROUND_CODES:
            from fifth_batch import FIFTH_SPECS
            if FIFTH_SPECS[code].get("unavailable_reason"):
                return False
        if not self._data_capabilities_known:
            return True
        caps = capabilities_for_timeframe(self.data_capabilities, self.capabilities_by_timeframe, timeframe)
        return not entry_unavailable_reason(code, caps, timeframe)

    def clear_data_capabilities(self):
        """数据路径刚变化时先解除灰显；重新检测后再按真实能力限制。"""
        self.data_capabilities = {}
        self.capabilities_by_timeframe = {}
        self._data_capabilities_known = False
        for tf, widgets in getattr(self, "case_widgets", {}).items():
            for code, widget in widgets.items():
                if code in RESEARCH_CASE_CODES:
                    widget.configure(state="normal" if self._entry_available(tf, code) else "disabled")

    def set_data_capabilities(self, capabilities: dict, uncheck_unavailable: bool = True, timeframe_capabilities=None):
        """把数据能力映射到第三/四批勾选框。缺字段的策略直接灰掉，避免“0信号但看起来跑完了”。"""
        self.data_capabilities = dict(capabilities or {})
        self.capabilities_by_timeframe = dict(timeframe_capabilities or {})
        self._data_capabilities_known = True
        disabled = []
        for tf, widgets in getattr(self, "case_widgets", {}).items():
            for code, widget in widgets.items():
                if code not in RESEARCH_CASE_CODES:
                    continue
                ok = self._entry_available(tf, code)
                widget.configure(state="normal" if ok else "disabled")
                if not ok:
                    disabled.append(code)
        if uncheck_unavailable:
            self._trim_unavailable_selections()
        self._skip_availability_prune = True
        try:
            self._update_summary()
        finally:
            self._skip_availability_prune = False
        return sorted(set(disabled))

    def _trim_unavailable_selections(self):
        from selection_availability import prune_unavailable_selection
        raw = {"开仓条件": {tf: [code for code, var in values.items() if var.get()]
                            for tf, values in self.case_vars.items()},
               "指标组合": copy.deepcopy(self.indicator_combos)}
        report = prune_unavailable_selection(raw, self.data_capabilities, self.capabilities_by_timeframe)
        for tf, values in self.case_vars.items():
            selected = report["selection"]["开仓条件"][tf]
            for code, var in values.items():
                var.set(code in selected)
        self.indicator_combos = copy.deepcopy(report["selection"].get("指标组合", {}))
        self.last_availability_report = report
        self._update_combo_summaries()
        return report

    def _update_summary(self):
        self._update_combo_summaries()
        # Group-selection and apply_config must not bypass disabled checkboxes.
        if getattr(self, "_data_capabilities_known", False):
            if not getattr(self, "_skip_availability_prune", False):
                self._trim_unavailable_selections()
        else:
            for tf, values in self.case_vars.items():
                for code in FIFTH_ROUND_CODES:
                    if code in values and not self._entry_available(tf, code):
                        values[code].set(False)
        for tf, var in getattr(self, "entry_all_vars", {}).items():
            codes = [c for c in SECOND_ROUND_CODES if c in self.case_vars[tf]]
            var.set(bool(codes) and all(self.case_vars[tf][code].get() for code in codes))
        for tf, var in getattr(self, "third_entry_all_vars", {}).items():
            codes = [c for c in THIRD_ROUND_CODES if c in self.case_vars[tf] and
                     self._entry_available(tf, c)]
            var.set(bool(codes) and all(self.case_vars[tf][code].get() for code in codes))
        for tf, var in getattr(self, "fourth_entry_all_vars", {}).items():
            codes=[c for c in FOURTH_ROUND_CODES if c in self.case_vars[tf] and
                   self._entry_available(tf, c)]
            var.set(bool(codes) and all(self.case_vars[tf][c].get() for c in codes))
        for tf, var in getattr(self, "fifth_entry_all_vars", {}).items():
            codes = [c for c in FIFTH_ROUND_CODES if c in self.case_vars[tf] and self._entry_available(tf, c)]
            var.set(bool(codes) and all(self.case_vars[tf][c].get() for c in codes))
        try:
            counts = 配置统计(self.get_config())
            text = (
                f"当前：入场 {counts['基础入场组合数']:,} × 口径 {counts['入场口径档数']}种 × 位置 {counts['开仓位置过滤档数']}档 × "
                f"止损 {counts['止损组合数']:,}（信号{counts['信号止损数']}×固定{counts['固定止损数']}×时间{counts['时间止损数']}） × "
                f"止盈 {counts['止盈方案数']:,} × 等待 {counts['等待时间档数']:,} × "
                f"成本 {counts['成本模式档数']}档 × 仓位 {counts['仓位档数']:,} = {counts['包含仓位完整组合数']:,} 个结果；"
                f"完整CSV {counts['不含仓位完整组合数']:,} 行"
            )
        except ValueError as exc:
            counts = None
            text = f"当前选择不完整：{exc}"
        self._update_tab_badges(counts)
        self.summary_var.set(text)
        if self.on_change:
            self.on_change(counts, text)

    def reset_all(self):
        if not messagebox.askyesno(
            "全选当前策略",
            "全选会产生非常大的组合数量。确定全选当前保留的策略吗？第五批已永久删除的81种不会恢复。",
            parent=self,
        ):
            return
        self.apply_config(全选配置())

    # ------------------------------------------------------------ 轮次控制
    def _round_config(self, code):
        """按轮次拼一份配置：本轮字段放全变量，其余锁死在已测最优上。"""
        开放 = set(基线.轮次字段(code))
        最优 = 基线.已测最优

        def 取(字段, 全量):
            return list(全量) if 字段 in 开放 else list(最优[字段])

        cases = {}
        for tf, allowed in 时间条件.items():
            cases[tf] = ([c for c in allowed if self._entry_available(tf, c)]
                         if "开仓条件" in 开放 else list(最优["开仓条件"][tf]))
        return {
            "版本": 20,
            "开仓指标": 取("开仓指标", ("hist", "dif")),
            "开仓条件": cases,
            "开仓位置过滤": ["OFF"],
            "止损代码": 取("止损代码", [row[0] for row in self.all_stops]),
            # 下面这些不参与五轮扫描，一律锁死在基线值上，免得组合数悄悄翻倍。
            "固定止损代码": list(最优["固定止损代码"]),
            "叠加止盈代码": list(最优["叠加止盈代码"]),
            "开仓方向": list(最优["开仓方向"]),
            "交易会话": list(最优["交易会话"]),
            "强制时间止损分钟": list(最优["强制时间止损分钟"]),
            "止盈方案编号": 取("止盈方案编号", [x.编号 for x in self.all_tps]),
            "止盈后等待分钟": 取("止盈后等待分钟", range(0, 31)),
            "仓位倍数": 取("仓位倍数", 仓位列表),
        }

    def apply_round(self, code):
        """切到某一轮：调用方负责确认。返回该轮的组合统计，便于界面显示。

        code 不在轮次表里（例如"基线"）时不放开任何字段，
        全部锁死在已测最优上，得到 1 个组合，用于单点复核。
        """
        self.apply_config(self._round_config(code))
        if code in 基线.轮次字典:
            self.pages.select(基线.轮次字典[code][2])
        return 配置统计(self.get_config())

    def round_preview(self, code):
        """不改动界面，只算这一轮会产生多少组合。"""
        return 配置统计(规范化配置(self._round_config(code)))

    def detect_round(self):
        """看当前勾选像哪一轮：本轮字段全开、其余字段恰好等于最优值。"""
        current = self.get_config()
        for code, _name, fields, _tab, _note in 基线.轮次列表:
            expect = 规范化配置(self._round_config(code))
            if all(current[k] == expect[k] for k in
                   tuple(基线.已测最优) + ("开仓位置过滤",)):
                return code
        return None

    def apply_config(self, raw_config):
        report = prepare_editor_selection(raw_config)
        full = 规范化配置(report["selection"])
        self.last_policy_report = report
        self.policy_notice_var.set(report["message"])
        self.indicator_combos = copy.deepcopy(full.get("指标组合", {}))
        self.set_entry_modes(full["入场触发口径"], update_summary=False)
        costs = [full["成本模式"]] if isinstance(full["成本模式"], str) else full["成本模式"]
        for code, var in self.cost_mode_vars.items():
            var.set(code in costs)
        position_codes = full["开仓位置过滤"]
        self.position_filter_order = position_codes + [code for code in POSITION_FILTERS if code not in position_codes]
        for code, var in self.position_filter_vars.items():
            var.set(code in position_codes)
        for key, var in self.field_vars.items():
            var.set(key in full["开仓指标"])
        for timeframe, values in self.case_vars.items():
            for value, var in values.items():
                var.set(value in full["开仓条件"][timeframe])
        self.selected_stops = set(full["止损代码"])
        self.selected_fixed_stops = set(full.get("固定止损代码") or 固定止损同档代码())
        self._refresh_fixed_stop_summary()
        self.selected_overlay_tps = set(full.get("叠加止盈代码") or ["OFF"])
        self._refresh_overlay_summary()
        for code, var in self.direction_vars.items():
            var.set(code in set(full.get("开仓方向") or ["BOTH"]))
        for code, var in self.session_vars.items():
            var.set(code in set(full.get("交易会话") or ["ALL"]))
        for minutes, var in self.hard_time_vars.items():
            var.set(minutes in set(full.get("强制时间止损分钟") or [0]))
        self.selected_tps = set(full["止盈方案编号"])
        for tf, fold in self.entry_fold_vars.items():
            if any(code in SECOND_ROUND_CODES for code in full["开仓条件"][tf]):
                fold.set(True)
        for tf, fold in self.third_entry_fold_vars.items():
            if any(code in THIRD_ROUND_CODES for code in full["开仓条件"][tf]):
                fold.set(True)
        for tf, fold in self.fourth_entry_fold_vars.items():
            if any(code in FOURTH_ROUND_CODES for code in full["开仓条件"][tf]):
                fold.set(True)
        for tf, fold in self.fifth_entry_fold_vars.items():
            if any(code in FIFTH_ROUND_CODES for code in full["开仓条件"][tf]):
                fold.set(True)
        for minute, var in self.cooldown_vars.items():
            var.set(minute in full["止盈后等待分钟"])
        for size, var in self.size_vars.items():
            var.set(size in full["仓位倍数"])
        self.refresh_stop_tree()
        self.refresh_tp_tree()
        self._update_summary()
