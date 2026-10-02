"""Direction-calendar selector for the offline hedge page."""
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from direction_calendar import BEIJING, event_window
from platform_support import fit_window


def open_direction_dialog(parent, enabled, on_apply):
    window=tk.Toplevel(parent)
    window.title("红绿带中点方向限制")
    fit_window(window,780,550,minimum=(620,430))
    window.transient(parent.winfo_toplevel())
    actions=ttk.Frame(window,padding=14);actions.pack(side="bottom",fill="x")
    body=ttk.Frame(window,padding=14);body.pack(fill="both",expand=True)
    choice=tk.BooleanVar(window,value=enabled)
    ttk.Radiobutton(body,text="不使用：沿用原双向持仓规则",variable=choice,value=False).pack(anchor="w")
    ttk.Radiobutton(body,text="使用：红带中点后只空，绿带中点后只多，允许空仓",variable=choice,value=True).pack(anchor="w",pady=(6,8))
    ttk.Label(body,text="北京时间；同色连续带先合并，中点对应2小时K线收盘后生效。\n"
        "中间色带沿用上一方向；历史及未来年份均按公式和实际天数计算。\n"
        "切换时撤销旧方向开仓/补仓单，Maker平旧仓；旧仓全部成交退出后才允许新方向开仓。\n"
        "减仓和平仓始终允许；原信号、开仓间隔、资金和数量限制继续生效。",
        wraplength=730,justify="left").pack(anchor="w")
    dates=ttk.Frame(body);dates.pack(fill="x",pady=10)
    year=datetime.now(BEIJING).year
    start=tk.StringVar(window,value=f"{year-1}-01-01")
    end=tk.StringVar(window,value=f"{year+5}-09-15")
    ttk.Label(dates,text="日历预览").pack(side="left")
    ttk.Entry(dates,textvariable=start,width=12).pack(side="left",padx=5)
    ttk.Label(dates,text="至").pack(side="left")
    ttk.Entry(dates,textvariable=end,width=12).pack(side="left",padx=5)
    frame=ttk.Frame(body);frame.pack(fill="both",expand=True)
    table=ttk.Treeview(frame,columns=("start","end","side"),show="headings",height=11)
    for key,label,width in [("start","开始（含）· 北京时间",230),("end","结束（不含）· 北京时间",230),("side","允许方向",120)]:
        table.heading(key,text=label);table.column(key,width=width,anchor="center")
    bar=ttk.Scrollbar(frame,command=table.yview);table.configure(yscrollcommand=bar.set)
    table.pack(side="left",fill="both",expand=True);bar.pack(side="right",fill="y")
    def preview():
        try:
            first=datetime.fromisoformat(start.get()).replace(tzinfo=BEIJING)
            last=datetime.fromisoformat(end.get()).replace(tzinfo=BEIJING)
            if last.year-first.year>20:raise ValueError("预览一次最多20年")
            rows=event_window(first,last)
        except ValueError as exc:
            messagebox.showerror("日历日期",str(exc),parent=window);return
        table.delete(*table.get_children())
        for index,row in enumerate(rows):
            stop=rows[index+1][0] if index+1<len(rows) else last
            table.insert("","end",values=(max(first,row[0]).strftime('%Y-%m-%d %H:%M'),
                stop.strftime('%Y-%m-%d %H:%M'),"只多 / 空仓" if row[1]==1 else "只空 / 空仓"))
    ttk.Button(dates,text="查看日历",command=preview).pack(side="left",padx=5)
    ttk.Label(body,text="预览日期不会改变回测区间；末行截止不是反向信号。此规则为待验证假设。",
              foreground="#596579",wraplength=720).pack(anchor="w",pady=8)
    def apply():
        on_apply(choice.get());window.destroy()
    ttk.Button(actions,text="应用",command=apply).pack(side="right")
    ttk.Button(actions,text="取消",command=window.destroy).pack(side="right",padx=6)
    preview()
    return window
