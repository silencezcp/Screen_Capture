# -*- coding: utf-8 -*-
"""界面自动化测试：不点鼠标，直接驱动 CaptureApp 完成一次真实截图。

运行：python tests/test_gui.py
"""
from __future__ import annotations

import shutil
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402

from PIL import Image  # noqa: E402

from screen_capture import win32 as w  # noqa: E402
from screen_capture.gui import SCREEN_ROW, CaptureApp  # noqa: E402

OUT_DIR = ROOT / "_test_out" / "gui"


class GuiFlowTests(unittest.TestCase):
    def setUp(self):
        w.enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()          # 测试时不真的弹窗打扰用户
        self.app = CaptureApp(self.root)
        self.root.update()

    def tearDown(self):
        try:
            if self.app.engine is not None and self.app.engine.running:
                self.app.engine.stop(timeout=2)
            self.app._on_close()      # 走真实的关闭流程，顺带清理 after 回调
        except Exception:
            pass

    def _pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)

    def test_window_list_and_selection(self):
        rows = self.app.tree.get_children()
        self.assertIn(SCREEN_ROW, rows)
        self.assertGreaterEqual(len(rows), 1)
        # 选中整个屏幕，确认提示文字随之更新
        self.app.tree.selection_set(SCREEN_ROW)
        self.app._on_select()
        self.assertIn("整个屏幕", self.app.selection_var.get())

    def test_config_validation(self):
        from screen_capture.engine import ConfigError

        self.app.tree.selection_set(SCREEN_ROW)
        self.app.output_var.set("")
        with self.assertRaises(ConfigError):
            self.app._collect_config()

        self.app.output_var.set(str(OUT_DIR))
        self.app.interval_var.set("abc")
        with self.assertRaises(ConfigError):
            self.app._collect_config()

        self.app.interval_var.set("0.5")
        config = self.app._collect_config()
        self.assertEqual(config.interval, 0.5)
        self.assertEqual(config.method, w.METHOD_AUTO)

    def test_one_shot_preview(self):
        self.app.tree.selection_set(SCREEN_ROW)
        self.app.method_var.set("屏幕区域（所见即所得）")
        self.app._on_test_shot()
        deadline = time.monotonic() + 20
        while self.app.preview_photo is None and time.monotonic() < deadline:
            self._pump(0.05)
        self.assertIsNotNone(self.app.preview_photo, "试截没有生成预览图")
        self.assertIn("试截预览", self.app.preview_caption.cget("text"))
        self.assertEqual(str(self.app.test_button["state"]), "normal")
        self.assertIn("试截成功", self.app.log_text.get("1.0", "end"))

    def test_full_capture_flow(self):
        shutil.rmtree(OUT_DIR, ignore_errors=True)
        self.app.tree.selection_set(SCREEN_ROW)
        self.app.output_var.set(str(OUT_DIR))
        self.app.interval_var.set("0.5")
        self.app.count_var.set("2")
        self.app.method_var.set("自动（优先 PrintWindow，失败自动回退）")
        self.app.session_subdir_var.set(False)   # session_subdir 关掉 = flat
        self.app.format_var.set("png")

        self.app._on_start()
        self.assertIsNotNone(self.app.engine)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self.assertEqual(str(self.app.stop_button["state"]), "normal")

        deadline = time.monotonic() + 30
        while self.app.engine.running and time.monotonic() < deadline:
            self._pump(0.1)
        self._pump(0.5)

        self.assertEqual(self.app._counts["saved"], 2, self.app.log_text.get("1.0", "end"))
        self.assertEqual(str(self.app.start_button["state"]), "normal")
        self.assertEqual(str(self.app.stop_button["state"]), "disabled")

        files = sorted(OUT_DIR.glob("*.png"))
        self.assertEqual(len(files), 2, f"目录内容：{list(OUT_DIR.iterdir())}")
        with Image.open(files[0]) as image:
            width, height = image.size
        self.assertGreater(width, 100)
        print(f"  [info] 界面流程截图成功：{[f.name for f in files]}，尺寸 {width}x{height}")

        # 预览应该已经渲染出缩略图
        self.assertIsNotNone(self.app.preview_photo)
        self.assertIn("png", self.app.preview_caption.cget("text"))

        log = self.app.log_text.get("1.0", "end")
        self.assertIn("已保存", log)
        self.assertIn("已结束", log)


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    unittest.main(verbosity=2)
