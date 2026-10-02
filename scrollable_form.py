"""A scoped scrollable form: long settings must not hide tables or action buttons.

No bind_all: wheel handlers from one tab must not steal events from another.
Native list/text/input widgets keep their own wheel/editing behavior.
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk

class ScrollableForm(ttk.Frame):
    def __init__(self, parent, *, padding=0):
        super().__init__(parent)
        self.columnconfigure(0, weight=1); self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, width=1, height=1, takefocus=True)
        self.vertical = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        self.vertical.grid(row=0,column=1,sticky='ns')
        self.horizontal.grid(row=1,column=0,sticky='ew')
        self.body = ttk.Frame(self.canvas, padding=padding)
        self.window = self.canvas.create_window((0,0), window=self.body, anchor='nw')
        self._bound = set()
        self.body.bind('<Configure>', self._layout)
        self.canvas.bind('<Configure>', self._layout)
        self.body.bind('<Map>', lambda _e: self.bind_wheel())
        self.canvas.bind('<Prior>', lambda _e: self._key(-1))
        self.canvas.bind('<Next>', lambda _e: self._key(1))
        self.bind_wheel()

    def _layout(self, _event=None):
        self.canvas.itemconfigure(self.window,
            width=max(self.body.winfo_reqwidth(),self.canvas.winfo_width()),
            height=max(self.body.winfo_reqheight(),self.canvas.winfo_height()))
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        self.bind_wheel()

    def _key(self, direction):
        self.canvas.yview_scroll(direction,'pages')
        return 'break'

    def bind_wheel(self):
        def walk(w):
            yield w
            for child in w.winfo_children(): yield from walk(child)
        excluded=(ttk.Treeview,ttk.Entry,ttk.Combobox,ttk.Spinbox,tk.Text,tk.Entry,tk.Listbox,
                  ttk.Scrollbar,tk.Scrollbar)
        for w in walk(self):
            if str(w) in self._bound or isinstance(w,excluded): continue
            self._bound.add(str(w))
            for event in ('<MouseWheel>','<Shift-MouseWheel>','<Button-4>','<Button-5>'):
                w.bind(event,self._wheel,add='+')

    def _wheel(self,event):
        horizontal=bool(event.state & 1)
        view=self.canvas.xview if horizontal else self.canvas.yview
        scroll=self.canvas.xview_scroll if horizontal else self.canvas.yview_scroll
        if view()==(0.,1.): return None
        button=getattr(event,'num',None)
        delta=getattr(event,'delta',0)
        direction=(-1 if button==4 or delta>0 else 1)
        scroll(direction*max(1,abs(int(delta))//120)*3,'units')
        return 'break'
