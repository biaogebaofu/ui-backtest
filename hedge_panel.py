"""Independent dual-position backtest page, sharing only the main UI input sources."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from fifth_policy import require_selection_allowed
from indicator_combinations import DEFAULT_REGISTRY
from run_safety import atomic_json
from scrollable_form import ScrollableForm
from platform_support import user_data_dir
from selection_config import 全选配置, 规范化配置, 规范化入场触发口径


MODE_LABELS = {"scale_in": "0.5倍首仓＋0.5倍逆向补仓", "single": "1倍单仓（不补仓）"}
PATH_LABELS = {"both": "两种路径分别回测", "0": "开→高→低→收", "1": "开→低→高→收"}
ENTRY_PLAN_LABELS = {"combined": "多周期组合（按第2页勾选交叉组合）", "independent": "单周期独立对照（同周期指标组合生效）"}
DEFAULT_FIELDS = {
    "mode": "scale_in", "initial_equity": "20000", "max_eth": "20",
    "add_drops_percent": "0.1", "tp_weekday_percent": "0.4", "tp_holiday_percent": "0.2",
    "timeout_hours": "12,24,48,72", "entry_gap_minutes": "15", "maker_fee_percent": "0",
    "seeds": "100", "seed_start": "0", "compare_entries": False, "paths": "both",
    "entry_plan": "combined", "direction_limit": False,
}


def number_list(value, label, *, scale=1.0):
    parts = [part for part in re.split(r"[,，;；\s]+", str(value).strip()) if part]
    if not parts:
        raise ValueError(f"{label}至少填写一档")
    try:
        values = [float(part) * scale for part in parts]
    except ValueError as exc:
        raise ValueError(f"{label}请填写数字，多档用逗号或空格分隔") from exc
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"{label}不能填写NaN或无穷大")
    return list(dict.fromkeys(values))


def fields_to_config(values):
    from hedge_config import normalize_config
    mode = values["mode"]
    raw = {
        "mode": mode,
        "initial_equity": float(values["initial_equity"]),
        "max_eth": float(values["max_eth"]),
        "add_drops": number_list(values["add_drops_percent"], "逆向补仓跌幅", scale=0.01),
        "tp_weekday": float(values["tp_weekday_percent"]) / 100,
        "tp_holiday": float(values["tp_holiday_percent"]) / 100,
        "timeout_hours": number_list(values["timeout_hours"], "时间止损小时"),
        "entry_gap_minutes": float(values["entry_gap_minutes"]),
        "maker_fee_rate": float(values["maker_fee_percent"]) / 100,
        "seeds": float(values["seeds"]),
        "seed_start": float(values["seed_start"]),
        "compare_entries": bool(values["compare_entries"]),
        "paths": [0, 1] if values["paths"] == "both" else [int(values["paths"])],
    }
    if values.get("direction_limit", False):
        from direction_calendar import RULE_ID
        raw["direction_rule"] = RULE_ID
    return normalize_config(raw)


def selection_for_hedge(app, compare_entries):
    """Old exit/cost/size controls must not block the independent holding model."""
    raw = 全选配置()
    raw.update({
        "开仓指标": ["hist"], "开仓条件": {tf: [0] for tf in raw["开仓条件"]},
        "指标组合": {}, "入场触发口径": "LIVE_01", "开仓位置过滤": ["OFF"],
        "开仓方向": ["BOTH"], "交易会话": ["ALL"],
        "止损代码": ["OFF"], "固定止损代码": ["OFF"], "叠加止盈代码": ["OFF"],
        "强制时间止损分钟": [30], "止盈方案编号": raw["止盈方案编号"][:1],
        "止盈后等待分钟": [0], "仓位倍数": [1.0],
    })
    if compare_entries:
        panel = app.selection_panel
        fields = [field for field, var in panel.field_vars.items() if var.get()]
        if not fields:
            raise ValueError("请在第2页选择开仓MACD口径，或只运行随机基线")
        raw["开仓指标"] = fields
        if panel.indicator_combos.get("开仓"):
            raw["指标组合"] = {"开仓": copy.deepcopy(panel.indicator_combos["开仓"])}
        raw["开仓条件"] = {
            tf: [code for code, var in values.items() if var.get()] or [0]
            for tf, values in panel.case_vars.items()
        }
        if not any(code != 0 for values in raw["开仓条件"].values() for code in values):
            raise ValueError("请在第2页勾选开仓规则，或取消本页的开仓条件对照")
        modes = panel.get_entry_modes()
        raw["入场触发口径"] = modes[0] if len(modes) == 1 else modes
        raw["入场约束"] = {
            "最小S3距离": float(app.s3_gap_var.get()) / 100,
            "S3基线周期": app.s3_timeframe_var.get(),
        }
    require_selection_allowed(raw)
    selected = raw["开仓条件"]
    entry_mode = 规范化入场触发口径(raw["入场触发口径"])
    # Legacy validation handles native rules; registered compound codes are checked above.
    raw["开仓条件"] = {tf: [code for code in codes if code >= 0] or [0]
                       for tf, codes in selected.items()}
    raw["入场触发口径"] = "LIVE_01"
    result = 规范化配置(raw)
    result["开仓条件"] = selected
    result["入场触发口径"] = entry_mode
    return result


def eta_text(seconds):
    if seconds is None:
        return "估算中"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        return "估算中"
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}小时{minutes}分" if hours else f"{minutes}分{seconds}秒"


def holding_summary(config):
    """Describe the holding parameters actually passed to this page's worker."""
    size = "各方向1倍" if config["mode"] == "single" else "各方向0.5倍首仓＋0.5倍补仓"
    hours = "/".join(f"{value:g}" for value in config["timeout_hours"])
    direction = "开启方向日历" if config.get("direction_rule") else "不限制多空方向"
    return (f"本次：本金{config['initial_equity']:,.0f}U；{size}、各最多{config['max_eth']:g} ETH；"
            f"止盈工作日{config['tp_weekday'] * 100:g}%／周末假日{config['tp_holiday'] * 100:g}%；"
            f"超时{hours}小时后挂Maker平仓；单边手续费{config['maker_fee_rate'] * 100:g}%；{direction}。"
            "\n本页只沿用第2页开仓条件及S3约束；止盈、退出、仓位和成本采用本页参数。"
            "没有独立价格止损，超时不代表当时已成交。")


class HedgePanel(ttk.Frame):
    def __init__(self, parent, app, project_dir, *, settings_path=None):
        super().__init__(parent, padding=10)
        self.app = app
        self.project_dir = Path(project_dir)
        self.settings_path = Path(settings_path) if settings_path else user_data_dir() / "双向持仓设置.json"
        self.output = None
        self.running = False
        self._terminal = None
        self._settings_dirty = False
        self._context_after_id = None
        self.vars = {
            key: (tk.BooleanVar(self, value=value) if isinstance(value, bool) else tk.StringVar(self, value=value))
            for key, value in DEFAULT_FIELDS.items()
        }
        self.mode_var = tk.StringVar(self, value=MODE_LABELS["scale_in"])
        self.paths_var = tk.StringVar(self, value=PATH_LABELS["both"])
        self.entry_plan_var = tk.StringVar(self, value=ENTRY_PLAN_LABELS["combined"])
        self.context_var = tk.StringVar(self)
        self.scope_var = tk.StringVar(self)
        self.model_var = tk.StringVar(self)
        self.direction_status = tk.StringVar(self)
        self.status_var = tk.StringVar(self, value="准备就绪。每次开始均新建独立结果目录。")
        self.eta_var = tk.StringVar(self, value="预计剩余：估算中")
        self.input_widgets = []
        self._build()
        self.load_settings()
        for var in self.vars.values():
            var.trace_add("write", self._mark_settings_dirty)
        for name in ("csv_var", "bundle_var", "start_date_var", "end_date_var", "thread_var"):
            getattr(app, name).trace_add("write", lambda *_: self.refresh_context())
        self.refresh_context()
        self.bind("<Destroy>", self._cancel_context_refresh, add="+")

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=3)
        self.rowconfigure(1, weight=1)
        scroll = ScrollableForm(self)
        scroll.grid(row=0, column=0, sticky="nsew")
        body = scroll.body
        ttk.Label(body, text="双向持仓与补仓对照", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, textvariable=self.context_var, wraplength=1100, foreground="#596579").pack(anchor="w", pady=(3, 4))
        source_actions = ttk.Frame(body)
        source_actions.pack(fill="x", pady=(0, 6))
        ttk.Button(source_actions, text="修改数据与日期", command=lambda: self.app.notebook.select(0)).pack(side="left")
        ttk.Button(source_actions, text="选择开仓规则", command=lambda: self.app.notebook.select(1)).pack(side="left", padx=6)
        ttk.Button(source_actions, text="刷新选择摘要", command=self.refresh_context).pack(side="left")
        ttk.Button(source_actions, text="导入/重测双向指纹", command=lambda: self.app.show_fingerprint_queue()).pack(side="left", padx=6)
        params = ttk.LabelFrame(body, text="本页独立持仓参数", padding=10)
        params.pack(fill="x")
        for c in (1, 3, 5):
            params.columnconfigure(c, weight=1)
        ttk.Label(params, text="持仓模式").grid(row=0, column=0, sticky="w")
        mode = ttk.Combobox(params, textvariable=self.mode_var, values=list(MODE_LABELS.values()), state="readonly", width=30)
        mode.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 20), pady=4)
        mode.bind("<<ComboboxSelected>>", self._mode_changed)
        self.input_widgets.append((mode, "readonly"))
        self._entry(params, 0, 4, "本金（USDC）", "initial_equity")
        self._entry(params, 1, 0, "每方向上限（ETH）", "max_eth")
        self.add_entry = self._entry(params, 1, 2, "逆向补仓跌幅（%）", "add_drops_percent", width=22)
        self._entry(params, 1, 4, "超时Maker退出（小时）", "timeout_hours", width=22)
        self._entry(params, 2, 0, "工作日止盈（%）", "tp_weekday_percent")
        self._entry(params, 2, 2, "周末/中国假日止盈（%）", "tp_holiday_percent")
        self._entry(params, 2, 4, "开仓/补仓间隔（分钟）", "entry_gap_minutes")
        self._entry(params, 3, 0, "Maker单边手续费（%）", "maker_fee_percent")
        self._entry(params, 3, 2, "随机重复次数", "seeds")
        self._entry(params, 3, 4, "起始随机种子", "seed_start")
        ttk.Label(params, text="分钟内价格路径").grid(row=4, column=0, sticky="w")
        paths = ttk.Combobox(params, textvariable=self.paths_var, values=list(PATH_LABELS.values()), state="readonly", width=25)
        paths.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(6, 20), pady=4)
        paths.bind("<<ComboboxSelected>>", self._paths_changed)
        self.input_widgets.append((paths, "readonly"))
        ttk.Label(params, text="并发复用第3页设置", foreground="#596579").grid(row=4, column=4, columnspan=2, sticky="w")
        ttk.Label(params, text="多档数值用逗号或空格分隔，例如补仓跌幅 0.1,0.2,0.3；超时退出 12,24,48,72。",
                  foreground="#596579").grid(row=5, column=0, columnspan=6, sticky="w", pady=(4, 0))
        direction_button=ttk.Button(params,text="方向限制…",command=self.show_direction_limit)
        direction_button.grid(row=6,column=0,sticky="w",pady=(8,0))
        self.input_widgets.append((direction_button,"normal"))
        ttk.Label(params,textvariable=self.direction_status,foreground="#596579").grid(
            row=6,column=1,columnspan=5,sticky="w",padx=6,pady=(8,0))
        ttk.Label(params, textvariable=self.model_var, foreground="#596579", wraplength=1080,
                  justify="left").grid(row=7, column=0, columnspan=6, sticky="w", pady=(6, 0))
        rules = ttk.LabelFrame(body, text="开仓对照", padding=10)
        rules.pack(fill="x", pady=(8, 0))
        compare = ttk.Checkbutton(rules, text="除随机基线外，对照第2页当前勾选的开仓条件",
                                  variable=self.vars["compare_entries"], command=self._selection_changed)
        compare.pack(anchor="w")
        self.input_widgets.append((compare, "normal"))
        entry_plan = ttk.Combobox(rules, textvariable=self.entry_plan_var, values=list(ENTRY_PLAN_LABELS.values()), state="readonly", width=49)
        entry_plan.pack(anchor="w", pady=(4, 0))
        entry_plan.bind("<<ComboboxSelected>>", self._entry_plan_changed)
        self.input_widgets.append((entry_plan, "readonly"))
        ttk.Label(rules, textvariable=self.scope_var, foreground="#1F4E78", wraplength=1080).pack(anchor="w", pady=(4, 0))
        ttk.Label(rules, text="随机重复次数用于比较不同随机种子；确定性开仓条件可能每次完全相同，重复结果不等于独立验证。",
                  foreground="#596579", wraplength=1080).pack(anchor="w", pady=(3, 0))
        ttk.Label(rules, text="多周期组合：各周期勾选项交叉组合，所有启用周期共同过滤；同周期两两/三三沿用第2页“指标组合”。\n勾选“不启用”会产生关闭该周期的方案；要强制该周期参与，请取消“不启用”。规则对照共同预热200根；方向多空、全天、关闭位置过滤。",
                  foreground="#596579", wraplength=1080).pack(anchor="w", pady=(3, 0))
        ttk.Label(body, text=(
            "补仓：相对首仓均价逆向达到所填跌幅后，追加当时权益的0.5倍；每方向总量受本页ETH上限控制。"
            "补完按新加权均价保本平掉追加数量，余仓按该均价止盈。时间止损从首仓起算，补仓不重置。"
            "\n多、空各最多一组持仓，全账户开仓及补仓共用间隔。成交后立即预挂相应止盈/保本单；北京时间跨日动态切换止盈比例。"
            "\n全程Maker挂单，分钟K线只近似成交；不含真实排队、资金费率和交易所逐档强平。2026-09-14核实01 Maker费率为0%，可在本页改填。"),
            foreground="#7F6000", wraplength=1110, justify="left").pack(anchor="w", pady=(8, 6))
        log_box = ttk.LabelFrame(self, text="本页运行日志", padding=4)
        log_box.grid(row=1, column=0, sticky="nsew", pady=(8, 6))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log = tk.Text(log_box, height=6, state="disabled", wrap="word", font=("Consolas", 9))
        bar = ttk.Scrollbar(log_box, command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew")
        self.start_btn = ttk.Button(actions, text="开始双向回测", command=self.start, style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="安全停止", command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(actions, text="打开本次结果", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left")
        ttk.Label(actions, textvariable=self.eta_var).pack(side="right")
        ttk.Label(self, textvariable=self.status_var, wraplength=1120).grid(row=3, column=0, sticky="w", pady=(6, 3))
        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.grid(row=4, column=0, sticky="ew")

    def _entry(self, frame, row, col, label, key, width=15):
        ttk.Label(frame, text=label).grid(row=row, column=col, sticky="w", pady=4)
        entry = ttk.Entry(frame, textvariable=self.vars[key], width=width)
        entry.grid(row=row, column=col + 1, sticky="ew", padx=(6, 20), pady=4)
        entry.bind("<FocusOut>", lambda _e: self.save_settings())
        self.input_widgets.append((entry, "normal"))
        return entry

    def _mode_changed(self, _event=None):
        self.vars["mode"].set(next(code for code, label in MODE_LABELS.items() if label == self.mode_var.get()))
        self._apply_input_states()
        self.save_settings()

    def _paths_changed(self, _event=None):
        self.vars["paths"].set(next(code for code, label in PATH_LABELS.items() if label == self.paths_var.get()))
        self.save_settings()

    def show_direction_limit(self):
        if self.running:return
        from direction_calendar_dialog import open_direction_dialog
        def apply(enabled):
            if self.running:return
            self.vars["direction_limit"].set(enabled)
            self._selection_changed()
        return open_direction_dialog(self,self.vars["direction_limit"].get(),apply)

    def _selection_changed(self):
        self.refresh_context()
        self.save_settings()

    def _entry_plan_changed(self, _event=None):
        self.vars["entry_plan"].set(next(code for code, label in ENTRY_PLAN_LABELS.items() if label == self.entry_plan_var.get()))
        self._selection_changed()

    def _apply_input_states(self):
        for widget, normal_state in self.input_widgets:
            widget.configure(state="disabled" if self.running else normal_state)
        if not self.running and self.vars["mode"].get() == "single":
            self.add_entry.configure(state="disabled")

    def refresh_context(self):
        app = self.app
        try:
            self.model_var.set(holding_summary(fields_to_config({key: var.get() for key, var in self.vars.items()})))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.model_var.set("本页参数待调整：" + str(exc))
        self.direction_status.set("已使用：红带中点只空／绿带中点只多；先平旧仓再开新方向"
            if self.vars["direction_limit"].get() else "未使用：沿用原双向规则")
        source = app.csv_var.get().strip() or app.bundle_var.get().strip()
        start = app.start_date_var.get().strip() or "数据起点"
        end = app.end_date_var.get().strip() or "数据终点"
        self.context_var.set(f"复用数据：{Path(source).name if source else '尚未选择'}；日期：{start} ～ {end}（沿用第1页口径）；CPU并发：{app.thread_var.get()}")
        counts = {tf: sum(bool(var.get()) for code, var in items.items() if code != 0)
                  for tf, items in app.selection_panel.case_vars.items()}
        chosen = "、".join(f"{tf} {count}种" for tf, count in counts.items() if count) or "尚未勾选开仓规则"
        self.scope_var.set(("随机基线必含；当前开仓勾选：" + chosen + "。规则及数据支持情况在开始后逐项核验。")
                           if self.vars["compare_entries"].get() else "本次只运行随机开仓；无需选择旧页的开仓、止损或止盈。")
        if self.vars["compare_entries"].get():
            try:
                from indicator_combinations import choices_for_config
                from selection_config import 入场口径列表
                selection = selection_for_hedge(app, True)
                choices = choices_for_config(selection)["开仓"]
                if self.vars["entry_plan"].get() == "combined":
                    nonempty = math.prod(item.count for item in choices.values()) - int(all(0 in item for item in choices.values()))
                    label = "多周期组合"
                else:
                    nonempty = sum(item.count - int(0 in item) for item in choices.values())
                    label = "单周期独立对照（各周期不交叉）"
                count = nonempty * len(selection["开仓指标"]) * len(入场口径列表(selection))
                config = fields_to_config({key: var.get() for key, var in self.vars.items()})
                variants = len(config["timeout_hours"]) * len(config["paths"]) * (len(config["add_drops"]) if config["mode"] == "scale_in" else 1)
                trials = (count + 1) * variants * config["seeds"]
                self.scope_var.set(f"{label}：{chosen}；最多{count:,}条开仓组合，另含1条随机基线；本页参数共{variants}档 × {config['seeds']}次重复，最多{trials:,}次试验。缺数据及重复口径在运行前剔除。")
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                self.scope_var.set("开仓选择待调整：" + str(exc))

    def apply_fields(self, fields):
        fields_to_config(fields)
        labels = (MODE_LABELS[fields["mode"]], PATH_LABELS[fields["paths"]], ENTRY_PLAN_LABELS[fields["entry_plan"]])
        for key in DEFAULT_FIELDS:
            self.vars[key].set(fields[key])
        self.mode_var.set(labels[0])
        self.paths_var.set(labels[1])
        self.entry_plan_var.set(labels[2])
        self._apply_input_states()
        self.refresh_context()

    def load_settings(self):
        try:
            if not self.settings_path.exists():
                return
            saved = json.loads(self.settings_path.read_text("utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("fields", {}), dict):
                raise ValueError("设置文件格式不正确")
            for key, value in saved.get("fields", {}).items():
                if key in self.vars and isinstance(value, type(DEFAULT_FIELDS[key])):
                    if key == "mode" and value not in MODE_LABELS:
                        continue
                    if key == "paths" and value not in PATH_LABELS:
                        continue
                    if key == "entry_plan" and value not in ENTRY_PLAN_LABELS:
                        continue
                    self.vars[key].set(value)
        except (OSError, ValueError, TypeError) as exc:
            self.append_log(f"双向页设置未能恢复，采用默认值：{exc}")
        finally:
            self._settings_dirty = False
            self.mode_var.set(MODE_LABELS[self.vars["mode"].get()])
            self.paths_var.set(PATH_LABELS[self.vars["paths"].get()])
            self.entry_plan_var.set(ENTRY_PLAN_LABELS[self.vars["entry_plan"].get()])
            self._apply_input_states()

    def _mark_settings_dirty(self, *_args):
        self._settings_dirty = True
        if self._context_after_id is None:
            self._context_after_id = self.after_idle(self._refresh_context_pending)

    def _refresh_context_pending(self):
        self._context_after_id = None
        self.refresh_context()

    def _cancel_context_refresh(self, event):
        if event.widget is self and self._context_after_id is not None:
            self.after_cancel(self._context_after_id)
            self._context_after_id = None

    def save_settings(self, *, force=False):
        if not force and not self._settings_dirty:
            return
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.settings_path, {"version": 1, "fields": {key: var.get() for key, var in self.vars.items()}})
            self._settings_dirty = False
        except (OSError, tk.TclError) as exc:
            self.status_var.set(f"双向页设置保存失败：{exc}")

    def build_request(self):
        hedge = fields_to_config({key: var.get() for key, var in self.vars.items()})
        start, end = self.app.current_date_range()
        names = {"kline": "csv_var", "micro": "micro_csv_var", "funding": "funding_var", "oi": "oi_var", "bundle": "bundle_var"}
        sources = {key: getattr(self.app, name).get().strip() for key, name in names.items()}
        if not (sources["kline"] or sources["bundle"]):
            raise ValueError("请先在第1页选择1分钟K线或数据包")
        for name, filename in sources.items():
            if filename and not Path(filename).is_file():
                raise ValueError(f"找不到数据文件（{name}）：{filename}")
        selection = selection_for_hedge(self.app, hedge["compare_entries"])
        return {"sources": sources, "start": start, "end": end, "selection": selection, "hedge": hedge,
                "entry_plan": self.vars["entry_plan"].get(),
                "indicator_registry": DEFAULT_REGISTRY.to_dict()}

    def start(self):
        if self.app._fingerprint_run_active():
            self.status_var.set("当前回测或导出尚未结束，结束后再开始双向回测。")
            return
        try:
            request = self.build_request()
            threads = int(self.app.thread_var.get())
            if not 1 <= threads <= max(1, os.cpu_count() or 1):
                raise ValueError(f"并发数应为1到{os.cpu_count() or 1}之间的整数")
            output_root = self.app.out_var.get().strip()
            if not output_root:
                raise ValueError("请在第3页选择结果保存目录")
            worker = self.project_dir / "hedge_worker.py"
            if not worker.is_file():
                raise ValueError("未找到双向回测工作程序，请检查安装文件")
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
            out = Path(output_root) / f"双向持仓对照_{stamp}"
            out.mkdir(parents=True, exist_ok=False)
            request_path = out / "双向回测请求.json"
            atomic_json(request_path, request)
        except (ValueError, TypeError, OSError, tk.TclError) as exc:
            messagebox.showerror("双向回测参数错误", str(exc), parent=self)
            return
        command = [sys.executable, "-X", "utf8", str(worker), "--request", str(request_path),
                   "--output", str(out), "--threads", str(threads)]
        if not self.app._launch_process(command, out, "hedge"):
            return
        self.output = out
        self._terminal = None
        self.running = True
        self.progress["value"] = 0
        self.eta_var.set("预计剩余：估算中")
        self.status_var.set("已开始，正在核验数据与开仓条件。")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        self._apply_input_states()
        self.save_settings(force=True)
        self.append_log(f"本次结果独立保存到：{out}")
        self.append_log(holding_summary(request["hedge"]))

    def append_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 1500:
            self.log.delete("1.0", f"{lines - 1500 + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def request_stop(self):
        if self.app.proc_kind != "hedge" or not self.app.proc or self.app.proc.poll() is not None:
            return
        out = self.output or self.app._active_output_dir
        try:
            (Path(out) / "停止请求.flag").write_text("stop", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("停止请求写入失败", str(exc), parent=self)
            return
        self.stop_btn.configure(state="disabled")
        self.status_var.set("已请求安全停止，正在等待当前小任务保存结果。")
        self.append_log("已请求安全停止；已完成结果保留。")

    def handle_message(self, message):
        kind = message.get("type")
        if kind == "progress":
            done, total = message.get("completed", 0), message.get("total", 0)
            percent = message.get("percent", 100 * done / total if total else 0)
            self.progress["value"] = min(100, max(0, percent))
            self.status_var.set(f"已完成 {done:,}/{total:,}｜{message.get('message', '')}")
            self.eta_var.set("预计剩余：" + eta_text(message.get("eta_seconds")))
        elif kind in ("stage", "log", "warning"):
            text = str(message.get("message", ""))
            self.append_log(text)
            if kind == "stage":
                self.status_var.set(text)
                self.eta_var.set("预计剩余：" + eta_text(message.get("eta_seconds")))
        elif kind == "done":
            self._terminal = "done"
            self.progress["value"] = 100
            self.eta_var.set("预计剩余：0分0秒")
            self.status_var.set("双向回测与导出完成。")
            self.append_log("结果已保存：" + str(message.get("output", self.output)))
        elif kind == "error":
            self._terminal = "error"
            self.status_var.set("运行失败：" + str(message.get("message", "请查看日志")))
            self.append_log(self.status_var.get())
        elif kind == "stopped":
            self._terminal = "stopped"
            self.status_var.set("已安全停止，已完成结果保留。")
            self.eta_var.set("预计剩余：已停止")
            self.append_log(self.status_var.get())
        elif kind == "process_exit":
            self.running = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self._apply_input_states()
            if message.get("code", 0) != 0 and self._terminal != "error":
                self.status_var.set(f"工作进程异常退出（{message.get('code')}），请查看本页日志。")
            elif self._terminal is None:
                self.status_var.set("工作进程已退出，未收到完成标记，请核对日志。")
            self.append_log(f"工作进程退出代码：{message.get('code')}")

    def open_output(self):
        if self.output is None:
            return
        try:
            if os.name == "nt":
                os.startfile(self.output)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.output)])
        except OSError as exc:
            messagebox.showerror("打开结果失败", str(exc), parent=self)
