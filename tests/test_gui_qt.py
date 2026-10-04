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
# 界面设置写到独立的 ini 文件，绝不碰用户真实的注册表配置
SETTINGS_FILE = OUT_DIR / "settings.ini"


class QtUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(QSS)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        os.environ["SCREEN_CAPTURE_SETTINGS_FILE"] = str(SETTINGS_FILE)
        from PyQt5.QtCore import QSettings
        settings = QSettings(str(SETTINGS_FILE), QSettings.IniFormat)
        settings.clear()
        settings.setValue("settings_version", SETTINGS_VERSION)
        settings.setValue("output", str(OUT_DIR / "default"))
        settings.setValue("monitor", False)      # 最小化留在任务栏（默认）
        settings.setValue("tray", False)
        settings.setValue("archive", True)
        settings.sync()

    def setUp(self):
        self.win = MainWindow()
        # 每个用例都从固定状态开始：格式 png、目录不用子目录之外的一切按设置文件来。
        # （质量相关的用例会切换格式，而设置会在 tearDown 里存回 ini，
        #   不重置的话后面的截图用例就会写出 .jpg，与 *.png 断言对不上。）
        self.win.format_combo.setCurrentText("png")
        self.win.on_format_changed()
        self.win.quality_spin.setValue(90)
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
        self.win.folder_combo.setCurrentIndex(2)   # 不用子目录
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

    def test_folder_reuse_same_app(self):
        """同一个应用连续跑两次任务，应当复用同一个文件夹（不再每次新建）。"""
        import shutil as _shutil

        target_dir = OUT_DIR / "reuse"
        _shutil.rmtree(target_dir, ignore_errors=True)
        self.win.screen_check.setChecked(True)
        self.win.on_screen_toggle()
        self.win.output_edit.setText(str(target_dir))
        self.win.folder_combo.setCurrentIndex(0)      # 按应用复用
        self.win.interval_spin.setValue(0.3)
        self.win.count_spin.setValue(1)

        for _ in range(2):
            self.win.on_start()
            deadline = time.monotonic() + 30
            while self.win.engine.running and time.monotonic() < deadline:
                self.pump(0.05)
            self.pump(0.3)

        folders = [p for p in target_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(folders), 1, f"应当只有一个文件夹，实际：{[p.name for p in folders]}")
        shots = sorted(folders[0].glob("*.png"))
        self.assertEqual(len(shots), 2, [p.name for p in shots])
        manifest = folders[0] / "capture_manifest.csv"
        rows = [r for r in manifest.read_text(encoding="utf-8-sig").splitlines() if r.strip()]
        self.assertEqual(len(rows), 3, "两次任务的记录应当追加在同一个清单里")
        print(f"  [info] 两次任务复用同一文件夹：{folders[0].name}，共 {len(shots)} 张，清单 {len(rows) - 1} 条")

    def test_options_apply_live(self):
        """运行中改参数（间隔 / 数量上限）必须立即生效，不需要停止任务。"""
        import shutil as _shutil

        target_dir = OUT_DIR / "live"
        _shutil.rmtree(target_dir, ignore_errors=True)
        self.win.screen_check.setChecked(True)
        self.win.on_screen_toggle()
        self.win.output_edit.setText(str(target_dir))
        self.win.folder_combo.setCurrentIndex(2)      # 不用子目录
        self.win.interval_spin.setValue(5.0)          # 先用 5 秒
        self.win.count_spin.setValue(3)
        self.win.on_start()
        self.pump(1.0)
        self.assertGreaterEqual(self.win.counts["saved"], 1, "第一张没有截到")

        # 运行中把间隔改成 0.3 秒
        self.win.interval_spin.setValue(0.3)
        self.win.apply_live_config()
        self.assertEqual(self.win.engine.config.interval, 0.3, "间隔没有传给运行中的任务")
        started = time.monotonic()
        while self.win.counts["saved"] < 2 and time.monotonic() - started < 3:
            self.pump(0.05)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, f"改了间隔后没有立即按新间隔截图（等了 {elapsed:.1f}s）")

        # 运行中把上限改成「当前张数」：应立即收尾（正在抓的那一张允许落盘）
        current = self.win.counts["saved"]
        self.win.count_spin.setValue(current)
        self.win.apply_live_config()
        deadline = time.monotonic() + 10
        while self.win.engine.running and time.monotonic() < deadline:
            self.pump(0.05)
        self.assertFalse(self.win.engine.running, "改了上限后任务没有立即结束")
        final = self.win.counts["saved"]
        self.assertLessEqual(final, current + 1, f"改了上限后还多截了好几张：{current} -> {final}")
        files = sorted(target_dir.glob("*.png"))
        self.assertEqual(len(files), final, f"文件数 {len(files)} 与计数 {final} 不一致")
        print(f"  [info] 运行中改间隔/上限即时生效：新间隔后 {elapsed:.2f}s 出下一张，"
              f"上限改小后立刻收尾（共 {final} 张）")

    def test_quality_only_for_lossy_formats(self):
        """质量只对 JPG/WEBP 有效：PNG/BMP 应禁用输入框并给出提示。"""
        for fmt, usable in (("png", False), ("bmp", False), ("jpg", True), ("webp", True)):
            self.win.format_combo.setCurrentText(fmt)
            self.win.on_format_changed()
            self.assertEqual(self.win.quality_spin.isEnabled(), usable,
                             f"格式 {fmt} 的质量框可编辑状态不对")
            if usable:
                self.assertEqual(self.win.quality_hint.text(), "")
            else:
                self.assertIn("无损", self.win.quality_hint.text())
        print("  [info] 质量参数仅对 JPG/WEBP 生效，PNG/BMP 自动禁用并提示")

    def test_disabled_quality_is_visibly_different(self):
        """禁用态必须一眼看得出（底色加深），而且不能把启用态也染色。

        踩过的坑：给 QSpinBox:disabled::up-button 单独设样式，会让 Qt 把禁用配色
        画到启用态的 QSpinBox 上，于是启用/禁用看起来一模一样。
        """
        from PyQt5.QtCore import QPoint

        self.win.show()
        self.pump(0.3)

        def body_color(fmt: str) -> str:
            self.win.format_combo.setCurrentText(fmt)
            self.win.on_format_changed()
            self.pump(0.2)
            spin = self.win.quality_spin
            point = spin.mapTo(self.win, QPoint(int(spin.width() * 0.3), spin.height() // 2))
            image = self.win.grab().toImage()
            return image.pixelColor(point.x(), point.y()).name()

        disabled = body_color("png")
        enabled = body_color("jpg")
        self.assertNotEqual(disabled, enabled, "PNG 禁用态与 JPG 启用态颜色不能相同")
        self.assertEqual(enabled, "#ffffff", f"启用态应当是白底，实际 {enabled}")
        self.assertNotEqual(disabled, "#ffffff", "禁用态不应该是白底")
        print(f"  [info] 质量框：启用态 {enabled} / 禁用态 {disabled}（肉眼可区分）")

    def test_version_shown_top_left(self):
        """左上角标题旁必须显示版本号，且与包版本一致。"""
        from screen_capture import __version__

        self.win.show()
        self.pump(0.2)
        self.assertEqual(self.win.version_label.text(), f"v{__version__}")
        self.assertIn(__version__, self.win.windowTitle())
        self.assertTrue(self.win.version_label.isVisible())
        # 版本徽标应该在标题的右侧、整个头部的最左边一列
        self.assertLess(self.win.version_label.x(), self.win.status_pill.x())
        print(f"  [info] 左上角显示版本：{self.win.version_label.text()}（窗口标题：{self.win.windowTitle()}）")

    def test_new_options_and_arrow(self):
        """更多选项要齐全，下拉框要有箭头图片。"""
        from screen_capture.gui_qt import _ARROW, _ARROW_RULE

        for name in ("skip_check", "manifest_check", "client_check",
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
        self.win.folder_combo.setCurrentIndex(2)   # 不用子目录
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
