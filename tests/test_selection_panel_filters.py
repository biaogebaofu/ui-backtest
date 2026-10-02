import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selection_panel import 每页止盈数, 组合选择面板

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover
    tk = None


def _has_display():
    if tk is None:
        return False
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


class SelectionPanelFilterTests(unittest.TestCase):
    def test_period_filter_matches_complete_token_only(self):
        self.assertTrue(组合选择面板._period_matches("1m+15m", "1m"))
        self.assertTrue(组合选择面板._period_matches("1m+15m", "15m"))
        self.assertFalse(组合选择面板._period_matches("15m", "5m"))
        self.assertTrue(组合选择面板._period_matches("", "无周期"))
        self.assertFalse(组合选择面板._period_matches("1h", "无周期"))

    def test_take_profit_page_is_bounded(self):
        self.assertEqual(每页止盈数, 300)


@unittest.skipUnless(_has_display(), "需要 tkinter 和可用显示")
class StopTreeRenderTests(unittest.TestCase):
    """⑨情况三的组合曾经整片渲染不出来。

    refresh_stop_tree 的短标签字典漏了 S9，凡是带⑨的行一插入就 KeyError，
    异常被 tkinter 回调吞掉，界面上看起来就是"搜不到、翻页翻不出来"。
    912 种里有 760 种带⑨，所以这不是漏一两行的小问题。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.panel = 组合选择面板(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def _codes_on_page(self):
        return [item[5:] for item in self.panel.stop_tree.get_children()]

    def test_every_stop_row_renders(self):
        for start in range(0, len(self.panel.all_stops), 100):
            self.panel.stop_page = start // 100
            self.panel.refresh_stop_tree()
            self.assertTrue(self._codes_on_page(), f"第{start // 100 + 1}页没有渲染出任何行")

    def test_search_finds_case_three_combination(self):
        self.panel._clear_stop_filters()
        self.panel.stop_search_var.set("③15分钟前轮极值+⑨1小时情况三反转量能充足")
        self.panel._stop_filter_changed()
        self.assertIn("S3_15m+S9_1h", self._codes_on_page())
        self.panel._clear_stop_filters()

    def test_component_filter_covers_rule_six_and_nine(self):
        for label, expected in (("⑥严格MACD反转", "S6"), ("⑨情况三反转量能充足", "S9")):
            self.assertEqual(expected, 组合选择面板._stop_component_key(label))

    def _round_radios(self, var):
        """轮次开关必须是真能点的控件。

        这行以前只是 LabelFrame 的标题文字，写成"第一轮 ｜ 第二轮"像页签，
        用户照着点却没有任何反应，第二轮的 5754 条止损/585 条止盈等于看不见。
        """
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Radiobutton) and str(child.cget("variable")) == str(var):
                    found.append(child)
                walk(child)

        walk(self.panel)
        return found

    def test_stop_round_switch_is_clickable(self):
        radios = self._round_radios(self.panel.stop_round_var)
        self.assertEqual(3, len(radios), "止损轮次开关应当是三个可点的单选按钮")
        expected = {"全部轮次": 912 + 5754, "第一轮": 912, "第二轮": 5754}
        for radio in radios:
            radio.invoke()
            value = self.panel.stop_round_var.get()
            self.assertEqual(expected[value], len(self.panel._visible_stops()),
                             f"点击「{radio.cget('text')}」后止损条数不对")
        self.panel._clear_stop_filters()

    def test_tp_round_switch_is_clickable(self):
        radios = self._round_radios(self.panel.tp_round_var)
        self.assertEqual(4, len(radios), "止盈轮次开关应当包含v1.41退出扩展")
        for radio in radios:
            radio.invoke()
            value = self.panel.tp_round_var.get()
            self.assertEqual(value, self.panel.tp_round_var.get())
            self.assertTrue(self.panel._visible_tps(), f"点击「{radio.cget('text')}」后止盈列表是空的")
        self.panel.tp_round_var.set("第二轮")
        self.assertTrue(all(self.panel._tp_round(x.编号) == "第二轮"
                            for x in self.panel._visible_tps()))
        self.panel._clear_tp_filters()

    def test_destroy_cancels_delayed_search_callback(self):
        panel = 组合选择面板(self.root)
        panel._schedule_stop_search()
        task = panel._stop_search_after_id
        panel.destroy()
        self.assertNotIn(task, self.root.tk.call("after", "info"))


if __name__ == "__main__":
    unittest.main()
