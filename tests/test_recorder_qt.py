# -*- coding: utf-8 -*-
"""PyQt6 录屏窗口测试：控件完整性、参数联动、预设、录制中改画质分段、真实产出 MP4。

运行：python tests/test_recorder_qt.py
单独成一个文件，避免真实录制占用 CPU/GDI 影响 test_gui_qt.py 里按张数断言的用例。
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

from PyQt6.QtWidgets import QApplication  # noqa: E402

from screen_capture.engine import TARGET_SCREEN, Target  # noqa: E402
from screen_capture.gui_qt import QSS  # noqa: E402
from screen_capture.recorder.engine import probe_encoder  # noqa: E402

OUT_DIR = ROOT / "_test_out" / "recorder_qt"

class RecorderDialogTests(unittest.TestCase):
  """PyQt6 录屏窗口：参数联动、预设、真实录制产出。"""

  @classmethod
  def setUpClass(cls):
      cls.app = QApplication.instance() or QApplication([])
      cls.app.setStyle("Fusion")
      cls.app.setStyleSheet(QSS)
      cls.out_dir = OUT_DIR / "recorder"
      shutil.rmtree(cls.out_dir, ignore_errors=True)
      cls.out_dir.mkdir(parents=True, exist_ok=True)

  def _dialog(self, **kwargs):
      from screen_capture.recorder_dialog_qt import RecorderDialog

      dialog = RecorderDialog(None, target=Target(kind=TARGET_SCREEN),
                              output_dir=str(self.out_dir), stylesheet=QSS, **kwargs)
      dialog.show_result_dialog = False    # 测试里不要模态框
      dialog.show()
      self.app.processEvents()
      self.addCleanup(self._close, dialog)
      return dialog

  def _close(self, dialog):
      try:
          if dialog.engine is not None and dialog.engine.running:
              dialog.engine.stop_and_wait(timeout=20)
          dialog.close()
          dialog.deleteLater()
          self.app.processEvents()
      except Exception:
          pass

  def _pump(self, seconds: float) -> None:
      deadline = time.monotonic() + seconds
      while time.monotonic() < deadline:
          self.app.processEvents()
          time.sleep(0.02)

  def test_dialog_builds_with_all_controls(self):
      dialog = self._dialog()
      for name in ("resolution_combo", "fps_combo", "quality_combo", "bitrate_spin",
                   "profile_combo", "scale_combo", "preset_combo", "window_list",
                   "output_edit", "pattern_edit", "countdown_combo", "duration_spin",
                   "cursor_check", "start_button", "pause_button", "stop_button"):
          self.assertTrue(hasattr(dialog, name), f"录屏窗口缺少控件 {name}")
      self.assertGreater(dialog.window_list.count(), 0, "来源列表为空（至少应有整个屏幕）")
      config = dialog._collect_config()
      config.validate()
      self.assertIn("fps", config.describe())

  def test_quality_controls_drive_config_and_preset(self):
      dialog = self._dialog()
      dialog._select_by_data(dialog.resolution_combo, "720p")
      dialog.fps_combo.setCurrentText("15")
      dialog._select_by_data(dialog.quality_combo, "standard")
      dialog.auto_bitrate_check.setChecked(True)
      self.app.processEvents()
      # 720p/15fps/标准 正好命中"流畅 720p 15fps"预设
      self.assertIn("流畅", dialog.preset_combo.currentText())

      dialog._select_by_data(dialog.quality_combo, "ultra")
      self.app.processEvents()
      self.assertEqual(dialog.preset_combo.currentText(), "自定义")

      # 手动码率必须传到配置里
      dialog.auto_bitrate_check.setChecked(False)
      dialog.bitrate_spin.setValue(2500)
      self.app.processEvents()
      self.assertEqual(dialog._collect_config().bitrate_kbps, 2500)
      self.assertTrue(dialog.bitrate_spin.isEnabled())

  def test_mid_recording_quality_change_rotates_segment(self):
      dialog = self._dialog()
      dialog._select_by_data(dialog.resolution_combo, "480p")
      dialog.fps_combo.setCurrentText("15")
      dialog._select_by_data(dialog.quality_combo, "low")
      dialog.duration_spin.setValue(4.0)
      dialog.countdown_combo.setCurrentIndex(0)
      self.app.processEvents()

      dialog._on_start()
      self._pump(1.5)
      dialog._select_by_data(dialog.resolution_combo, "720p")
      self.app.processEvents()
      self._pump(2.0)
      if dialog.engine is not None:
          dialog.engine.stop_and_wait(timeout=25)
      self._pump(0.5)

      files = sorted(self.out_dir.glob("*.mp4"))
      self.assertGreaterEqual(len(files), 1, "没有生成录像文件")
      total = sum(f.stat().st_size for f in files)
      self.assertGreater(total, 1024, f"录像文件过小：{total} 字节")
      for path in files:
          self.assertIn(b"ftyp", path.read_bytes()[:64], f"{path.name} 不是有效 MP4")

  def test_records_mp4_end_to_end(self):
      """通过界面点「开始录制」，真的产出一段可播放的 MP4。"""
      available, message = probe_encoder()
      if not available:
          self.skipTest(f"编码器不可用：{message}")
      before = {p.name for p in self.out_dir.glob("*.mp4")}
      dialog = self._dialog()
      dialog._select_by_data(dialog.resolution_combo, "480p")
      dialog.fps_combo.setCurrentText("15")
      dialog._select_by_data(dialog.quality_combo, "low")
      dialog.duration_spin.setValue(1.5)
      dialog.countdown_combo.setCurrentIndex(0)
      self.app.processEvents()

      dialog._on_start()
      deadline = time.monotonic() + 40
      while (dialog.engine is not None and dialog.engine.running
             and time.monotonic() < deadline):
          self.app.processEvents()
          time.sleep(0.05)
      self._pump(0.8)

      new_files = [p for p in self.out_dir.glob("*.mp4") if p.name not in before]
      self.assertTrue(new_files, "没有新的录像文件")
      path = max(new_files, key=lambda p: p.stat().st_mtime)
      self.assertGreater(path.stat().st_size, 1024, "录像文件过小")
      data = path.read_bytes()
      self.assertIn(b"ftyp", data[:64])
      self.assertIn(b"moov", data, "缺少 moov（未封盘）")
      # 状态栏与统计项都要更新过
      self.assertNotEqual(dialog.stat_labels["elapsed"].text(), "—")


if __name__ == "__main__":
    unittest.main(verbosity=2)
