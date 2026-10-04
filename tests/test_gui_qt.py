# -*- coding: utf-8 -*-
"""PyQt5 界面测试：构建窗口、参数校验、完整截图流程，并渲染一张界面预览图。

运行：python tests/test_gui_qt.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 无桌面环境也能跑

from PyQt5.QtWidgets import QApplication  # noqa: E402

from PIL import Image  # noqa: E402

from screen_capture import applog, win32 as w  # noqa: E402
from screen_capture.engine import ConfigError, TARGET_SCREEN  # noqa: E402
from screen_capture.gui_qt import (  # noqa: E402
    METHOD_CHOICES, SETTINGS_VERSION, MainWindow, QSS,
)

OUT_DIR = ROOT / "_test_out" / "qt"


class QtUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(QSS)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        # 把界面设置里的保存目录指到测试目录，避免测试影响真实截图
        from PyQt5.QtCore import QSettings
        settings = QSettings("ScreenCaptureTool", "ScreenCapture")
        settings.clear()
        settings.setValue("settings_version", SETTINGS_VERSION)
        settings.setValue("output", str(OUT_DIR / "default"))
        settings.setValue("monitor", False)      # 最小化留在任务栏（默认）
        settings.setValue("tray", False)
        settings.setValue("archive", True)
        settings.sync()

    def setUp(self):
        self.win = MainWindow()
        self.app.processEvents()

    def tearDown(self):
        try:
            if self.win.engine is not None and self.win.engine.running:
                self.win.engine.stop(timeout=2)
            self.win._stop_archiver()
            applog.get_logger().removeHandler(self.win._log_handler)
            self.win.close()
            self.win.deleteLater()
            self.app.processEvents()
        except Exception:
            pass

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    # ------------------------------------------------------------------
    def test_window_builds_and_lists_windows(self):
        self.assertGreater(len(self.win.window_list), 0, "没有枚举到任何窗口")
        self.assertGreater(self.win.table.rowCount(), 0, "列表里没有行")
        self.assertEqual(len(METHOD_CHOICES), 4)
        values = [value for _label, value in METHOD_CHOICES]
        self.assertEqual(values, [w.METHOD_AUTO, w.METHOD_WGC, w.METHOD_PRINTWINDOW, w.METHOD_SCREEN])

    def test_minimize_enters_listening_and_keeps_capturing(self):
        """窗口最小化 = 进入监听模式，截图必须继续；只有退出程序才停止。"""
        import shutil as _shutil

        target_dir = OUT_DIR / "listen"
        _shutil.rmtree(target_dir, ignore_errors=True)
        self.win.screen_check.setChecked(True)
        self.win.on_screen_toggle()
        self.win.output_edit.setText(str(target_dir))
        self.win.interval_spin.setValue(0.4)
        self.win.count_spin.setValue(4)
        self.win.subdir_check.setChecked(False)
        self.assertFalse(self.win.monitor_check.isChecked(),
                         "默认不勾选：最小化后窗口应留在任务栏，方便找回")
        self.win.on_start()

        self.pump(0.8)
        self.win.showMinimized()
        self.app.processEvents()
        self.pump(0.4)

        self.assertTrue(self.win.monitoring, "最小化后没有进入监听模式")
        self.assertTrue(self.win.engine.running, "最小化后截图被停止了")
        self.assertIn("监听", self.win.status_pill.text(), self.win.status_pill.text())

        deadline = time.monotonic() + 40
        while self.win.engine.running and time.monotonic() < deadline:
            self.pump(0.05)
        self.assertEqual(self.win.counts["saved"], 4,
                         f"最小化期间截图没有继续：{self.win.counts}")
        files = sorted(target_dir.glob("*.png"))
        self.assertEqual(len(files), 4, [p.name for p in files])
        print(f"  [info] 最小化后仍完成 {len(files)} 张截图")

        self.win._restore_window()
        self.pump(0.2)
        self.assertFalse(self.win.monitoring, "恢复窗口后应退出监听模式")

    def test_new_options_and_arrow(self):
        """更多选项要齐全，下拉框要有箭头图片。"""
        from screen_capture.gui_qt import _ARROW, _ARROW_RULE

        for name in ("skip_check", "subdir_check", "manifest_check", "client_check",
                     "cursor_check", "activate_check", "archive_check",
                     "archive_delete_check", "tray_check"):
            self.assertTrue(hasattr(self.win, name), f"缺少选项：{name}")
        self.assertTrue(self.win.archive_check.isChecked(), "默认应开启每日归档")
        self.assertTrue(_ARROW, "没有找到下拉箭头图片 assets/arrow_down.png")
        self.assertIn("down-arrow", _ARROW_RULE)

    def test_archiver_started(self):
        self.assertIsNotNone(self.win.archiver, "归档线程没有启动")
        self.assertTrue(self.win.archiver.is_alive() or self.win.archiver._stop.is_set())
        print(f"  [info] 归档线程已启动，距离下次 0 点 "
              f"{self.win.archiver.seconds_until_midnight() / 3600:.1f} 小时")

    def test_archive_toggle(self):
        self.win.archive_check.setChecked(False)
        self.assertIsNone(self.win.archiver, "关闭归档后线程应当停止")
        self.win.archive_check.setChecked(True)
        self.assertIsNotNone(self.win.archiver, "重新开启后线程应当启动")

    def test_default_output_dir_under_app(self):
        """默认保存目录必须是「程序目录\\ScreenCapture」。"""
        from screen_capture import paths
        from screen_capture.gui_qt import default_output_dir

        expected = paths.app_dir() / "ScreenCapture"
        self.assertEqual(default_output_dir(), expected)
        self.assertTrue(expected.is_dir(), "默认目录应当已经创建好")
        self.assertTrue(self.win.output_edit.text().strip(), "保存目录不能为空")
        print(f"  [info] 默认保存目录：{expected}")

    def test_requires_target(self):
        self.win.table.clearSelection()
        self.win.screen_check.setChecked(False)
        self.win.on_screen_toggle()
        self.win.output_edit.setText(str(OUT_DIR))
        with self.assertRaises(ConfigError):
            self.win.collect_config()

    def test_screen_target_config(self):
        self.win.screen_check.setChecked(True)
        self.win.on_screen_toggle()
        config = self.win.collect_config()
        self.assertEqual(config.target.kind, TARGET_SCREEN)
        self.assertGreaterEqual(config.interval, 0.1)
        self.assertTrue(config.activate_before_capture)

    def test_full_capture_flow(self):
        shutil.rmtree(OUT_DIR / "flow", ignore_errors=True)
        self.win.screen_check.setChecked(True)
        self.win.on_screen_toggle()
        self.win.output_edit.setText(str(OUT_DIR / "flow"))
        self.win.interval_spin.setValue(0.5)
        self.win.count_spin.setValue(2)
        self.win.subdir_check.setChecked(False)
        self.win.on_start()

        self.assertIsNotNone(self.win.engine)
        self.assertFalse(self.win.start_button.isEnabled())
        deadline = time.monotonic() + 40
        while self.win.engine.running and time.monotonic() < deadline:
            self.pump(0.05)
        self.pump(0.5)

        self.assertEqual(self.win.counts["saved"], 2, self.win.log_view.toPlainText()[-2000:])
        self.assertTrue(self.win.start_button.isEnabled(), "结束后应恢复开始按钮")
        files = sorted((OUT_DIR / "flow").glob("*.png"))
        self.assertEqual(len(files), 2, [p.name for p in files])
        with Image.open(files[0]) as image:
            self.assertGreater(image.width, 100)
        self.assertIsNotNone(self.win.preview.pixmap(), "预览没有显示截图")
        log_text = self.win.log_view.toPlainText()
        self.assertIn("已保存第 1 张", log_text)

    def test_log_written_to_file(self):
        path = applog.get_log_path()
        self.assertIsNotNone(path, "没有初始化日志文件")
        self.win.append_log("界面测试写入的一行日志")
        logger = applog.get_logger()
        logger.info("来自测试的日志：%s", "hello")
        for handler in logger.handlers:
            handler.flush()
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        self.assertIn("来自测试的日志", content, "日志没有实时写入文件")
        print(f"  [info] 日志文件：{path}")

    def test_render_ui_preview(self):
        """把界面渲染成 PNG，便于直观确认新 UI 的样子。"""
        self.win.resize(1280, 860)
        self.win.screen_check.setChecked(True)
        self.win.on_screen_toggle()
        self.win.interval_spin.setValue(5.0)
        self.win.append_log("20:00:00  已启用 WGC 捕获：窗口被遮挡、在后台也能截到它自己的画面。")
        self.win.append_log("20:00:05  已保存第 1 张：鸣潮_20261004_200005_0001.png（1922x1112，wgc，18 ms）")
        self.win.show()
        self.pump(0.6)
        pixmap = self.win.grab()
        target = OUT_DIR / "ui_preview.png"
        self.assertTrue(pixmap.save(str(target)), "界面渲染失败")
        self.assertGreater(target.stat().st_size, 5000)
        print(f"  [info] 界面预览图：{target}  {pixmap.width()}x{pixmap.height()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
