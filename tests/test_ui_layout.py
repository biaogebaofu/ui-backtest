"""UI 启动与折叠的回归测试。

之前两次事故都是这一块：一次是 ui.py 漏导入 S3基线周期选项，启动即 NameError；
一次是折叠后 LabelFrame 缩到看不见，整个"运行设置"分区像是消失了。
这两种都不该靠人肉截图发现。需要 tkinter + 虚拟显示，缺任一就跳过。
"""
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import tkinter as tk
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


@unittest.skipUnless(_has_display(), "需要 tkinter 和可用显示")
class UiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.chdir(str(Path(__file__).resolve().parents[1]))
        import ui
        cls.ui = ui
        cls.settings_patch = mock.patch.object(ui.App, "save_user_settings")
        cls.settings_patch.start()
        # 布局测试固定为编辑组合，不读取用户已保存的指纹列表运行对象。
        with mock.patch.object(ui.App, "load_user_settings"):
            cls.app = ui.App()
        cls.app.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.settings_patch.stop()

    def _labelframes(self):
        found = set()

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "TLabelframe":
                    try:
                        found.add(str(c.cget("text")))
                    except Exception:
                        pass
                walk(c)
        walk(self.app)
        return found

    def test_all_sections_present(self):
        found = self._labelframes()
        for name in ("数据与输出", "运行设置", "资金与ETH单次下单约束",
                     "全仓风险与入场闸门", "当前选择范围", "实时日志"):
            self.assertIn(name, found, "分区 %s 不见了" % name)

    def test_title_carries_version(self):
        self.assertIn(self.ui.工具版本, self.app.title())

    def test_fold_saves_height_and_keeps_header(self):
        """收起要真的还出高度，展开要能原样回来，顺序不能乱。

        v1.25 起分两种收法：
        · 整块型（_fold_boxes 里的五个分区）收起时整个 LabelFrame 从布局里拿掉，
          高度全部还给日志和底部操作栏。它不会"悄悄消失"——顶部有一条
          「显示区块」勾选栏常驻，每块一个勾，勾上就回来。
        · 内容型（轮次说明、排行门槛）只收内层 Frame，外框和标题行留着，
          因为轮次按钮和组合数汇总必须一直看得见。
        """
        for key in self.app.fold_vars:
            body = self.app._fold_bodies[key]
            box = self.app._fold_boxes.get(key, body.master)
            整块型 = key in self.app._fold_boxes

            self.app.fold_vars[key].set(False)
            self.app._apply_folds()
            self.app.update_idletasks()
            opened = box.winfo_reqheight()
            order = [c.winfo_class() for c in body.winfo_children()]
            if 整块型:
                # 用 winfo_manager 而不是 winfo_ismapped：全部展开时后面的分区
                # 会被窗口高度裁掉而 ismapped=0，那是正常的，不代表它没被排进布局。
                self.assertEqual(box.winfo_manager(), "pack",
                                 "%s 展开了却没排进布局" % key)

            self.app.fold_vars[key].set(True)
            self.app._apply_folds()
            self.app.update_idletasks()
            if 整块型:
                self.assertEqual(box.winfo_manager(), "",
                                 "%s 收起了却还占着位置" % key)
            else:
                folded = box.winfo_reqheight()
                self.assertLess(folded, opened, "%s 收起没省高度" % key)
                self.assertGreater(folded, 0, "%s 收起后整块不见了" % key)

            self.app.fold_vars[key].set(False)
            self.app._apply_folds()
            self.app.update_idletasks()
            self.assertEqual(opened, box.winfo_reqheight(), "%s 展开高度没还原" % key)
            self.assertEqual(order, [c.winfo_class() for c in body.winfo_children()],
                             "%s 展开后子控件顺序乱了" % key)

    def test_fold_bar_covers_every_whole_section(self):
        """每个整块型分区都必须在「显示区块」栏里有一个勾选框，否则收起就找不回来。"""
        labels = {str(c.cget("text")) for c in self.app._fold_bar.winfo_children()}
        for key in self.app._fold_boxes:
            self.assertIn(key, labels, "分区 %s 收起后没有地方能勾回来" % key)

    def test_start_button_stays_visible_at_min_size(self):
        """最小窗口、所有分区全展开时，"开始"按钮仍要在可视区内。

        v1.25 之前操作栏跟在各分区后面顺序排布，加一个面板就把它挤出屏幕。
        """
        self.app.geometry("1080x720")
        self.app.notebook.select(2)  # v1.40新增数据源页后，运行页是第3页
        for var in self.app.fold_vars.values():
            var.set(False)
        self.app._apply_folds()
        self.app.update()
        self.app.update_idletasks()
        top = self.app.start_btn.winfo_rooty() - self.app.winfo_rooty()
        self.assertTrue(self.app.start_btn.winfo_ismapped(), "开始按钮没被显示")
        self.assertLess(top + self.app.start_btn.winfo_height(), 720,
                        "开始按钮被挤出了可视区，点不到")

    def test_rank_metric_choices_are_wired(self):
        # 指标表定义在 selection_config，ui 只是 import 进来用。
        import selection_config as C
        # 当前值可能来自 用户设置.json 里存的上次选择，只要是合法项即可，
        # 不能写死成默认值——那反而会把"持久化生效"当成失败。
        self.assertIn(self.app.rank_metric_var.get(), C.排行指标选项)
        self.assertIn("盈亏比（倍）", C.排行指标选项)
        self.assertIn("爆仓保护次数（次）越小越好", C.排行指标选项)
        self.assertGreaterEqual(len(self.app.ranking_filter_rows), 2)
        settings = self.app.current_ranking_settings()
        self.assertIn(settings["排行指标"], C.排行指标选项)


if __name__ == "__main__":
    unittest.main()
