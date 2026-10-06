# -*- coding: utf-8 -*-
"""PyQt6 录屏窗口：来源选择、画质调节、录制控制与实时状态。

用法（在主界面点「● 录屏…」即可）::

    dialog = RecorderDialog(parent, target=Target(kind=TARGET_SCREEN), output_dir=r"D:\\videos")
    dialog.exec()

界面部分只负责收集参数；真正的录制、暂停、分段与编码在
:mod:`screen_capture.recorder.engine` 里，跑在独立线程上，
事件通过 Qt 信号回到主线程，所以录制时界面不会卡。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import win32 as w
from .engine import ConfigError, Target, TARGET_SCREEN, TARGET_WINDOW
from .recorder import quality as q
from .recorder.capture import SCALE_CHOICES, SCALE_LABELS
from .recorder.engine import (
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RECORDING,
    RecorderEngine,
    RecordingStats,
    probe_encoder,
)

COUNTDOWN_CHOICES = (
    ("不等待，立即开始", 0.0),
    ("1 秒", 1.0),
    ("2 秒", 2.0),
    ("3 秒（推荐）", 3.0),
    ("5 秒", 5.0),
    ("10 秒", 10.0),
)
FPS_CHOICES = tuple(str(v) for v in q.FPS_CHOICES)


class RecorderDialog(QDialog):
    """录屏窗口（PyQt6）。"""

    event_received = pyqtSignal(dict)

    def __init__(self, parent: Optional[QWidget] = None, target: Optional[Target] = None,
                 output_dir: Optional[str] = None, stylesheet: str = ""):
        super().__init__(parent)
        self.setWindowTitle("屏幕录制（可调画质）")
        self.setMinimumSize(900, 720)
        self.resize(980, 800)
        if stylesheet:
            self.setStyleSheet(stylesheet)

        self.engine: Optional[RecorderEngine] = None
        self.result_files: list = []
        # 录制结束后是否弹「录制完成」对话框：做成开关，自动化测试会关掉它
        # （模态框会卡住事件循环），嵌到别的程序里时也可以关掉。
        self.show_result_dialog = True
        self._closing = False
        self._countdown = 0
        self._windows: list = []
        self._row_targets: list = []

        self.config = q.load_record_config()
        if output_dir:
            self.config.output_dir = output_dir
        if target is not None and (target.kind == TARGET_SCREEN or target.hwnd):
            self.config.target = target

        self._build_ui()
        self._load_config_into_ui()
        self.refresh_windows()

        self.event_received.connect(self._on_engine_event)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(250)
        self._stats_timer.timeout.connect(self._update_stats)

        self._log("录屏窗口已就绪。选择来源与画质后点「● 开始录制」。")
        self._log("录制中修改分辨率/帧率/清晰度/码率会自动分段，旧片段照常保存。")

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(12)
        outer.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(self._build_source_group(), 1)
        left.addWidget(self._build_save_group())
        body.addLayout(left, 5)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._build_quality_group(), 1)
        right.addWidget(self._build_stats_group())
        body.addLayout(right, 4)

        outer.addLayout(self._build_actions())
        outer.addWidget(self._build_log_group())

    def _build_source_group(self) -> QGroupBox:
        box = QGroupBox("1. 录制来源")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("筛选："))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("按标题或程序名筛选…")
        self.filter_edit.textChanged.connect(lambda *_: self._fill_window_list())
        bar.addWidget(self.filter_edit, 1)
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self.refresh_windows)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.window_list = QListWidget()
        self.window_list.currentRowChanged.connect(lambda _r: self._on_source_changed())
        layout.addWidget(self.window_list, 1)

        self.source_label = QLabel("尚未选择目标")
        self.source_label.setObjectName("Hint")
        layout.addWidget(self.source_label)
        return box

    def _build_quality_group(self) -> QGroupBox:
        box = QGroupBox("2. 画质（录制中也能调，改动会自动分段）")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        row = 0

        grid.addWidget(QLabel("画质预设"), row, 0)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("自定义")
        for preset in q.QUALITY_PRESETS:
            self.preset_combo.addItem(preset.label)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        grid.addWidget(self.preset_combo, row, 1)
        row += 1

        grid.addWidget(QLabel("分辨率"), row, 0)
        self.resolution_combo = QComboBox()
        for name, _height, label in q.RESOLUTION_PRESETS:
            self.resolution_combo.addItem(label, name)
        self.resolution_combo.currentIndexChanged.connect(lambda *_: self._on_quality_changed())
        grid.addWidget(self.resolution_combo, row, 1)
        row += 1

        grid.addWidget(QLabel("帧率"), row, 0)
        fps_row = QHBoxLayout()
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(FPS_CHOICES)
        self.fps_combo.currentIndexChanged.connect(lambda *_: self._on_quality_changed())
        fps_row.addWidget(self.fps_combo)
        fps_row.addWidget(QLabel("fps"))
        fps_row.addStretch(1)
        grid.addLayout(fps_row, row, 1)
        row += 1

        grid.addWidget(QLabel("清晰度"), row, 0)
        self.quality_combo = QComboBox()
        for key in q.QUALITY_LEVELS:
            self.quality_combo.addItem(q.QUALITY_LABELS[key], key)
        self.quality_combo.currentIndexChanged.connect(lambda *_: self._on_quality_changed())
        grid.addWidget(self.quality_combo, row, 1)
        row += 1

        grid.addWidget(QLabel("码率"), row, 0)
        rate_row = QHBoxLayout()
        self.auto_bitrate_check = QCheckBox("自动")
        self.auto_bitrate_check.setChecked(True)
        self.auto_bitrate_check.stateChanged.connect(lambda *_: self._on_quality_changed())
        rate_row.addWidget(self.auto_bitrate_check)
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(100, 200000)
        self.bitrate_spin.setSingleStep(500)
        self.bitrate_spin.setSuffix(" kbps")
        self.bitrate_spin.setEnabled(False)
        self.bitrate_spin.valueChanged.connect(lambda *_: self._on_bitrate_edited())
        rate_row.addWidget(self.bitrate_spin, 1)
        grid.addLayout(rate_row, row, 1)
        row += 1

        grid.addWidget(QLabel("H.264 档位"), row, 0)
        self.profile_combo = QComboBox()
        for key, label in q.PROFILE_CHOICES:
            self.profile_combo.addItem(label, key)
        self.profile_combo.currentIndexChanged.connect(lambda *_: self._on_quality_changed())
        grid.addWidget(self.profile_combo, row, 1)
        row += 1

        grid.addWidget(QLabel("取帧方式"), row, 0)
        self.scale_combo = QComboBox()
        for key in SCALE_CHOICES:
            self.scale_combo.addItem(SCALE_LABELS[key], key)
        self.scale_combo.currentIndexChanged.connect(lambda *_: self._on_quality_changed())
        grid.addWidget(self.scale_combo, row, 1)
        row += 1

        self.estimate_label = QLabel("")
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setObjectName("Estimate")
        grid.addWidget(self.estimate_label, row, 0, 1, 2)
        row += 1
        hint = QLabel("取帧上限（本机实测）：高画质 720p≈29fps / 1080p≈19fps；"
                      "高帧率 720p≈45fps / 1080p≈29fps")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        grid.addWidget(hint, row, 0, 1, 2)
        return box

    def _build_save_group(self) -> QGroupBox:
        box = QGroupBox("3. 保存与选项")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        row = 0

        grid.addWidget(QLabel("保存目录"), row, 0)
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(self.config.output_dir)
        out_row.addWidget(self.output_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._choose_dir)
        out_row.addWidget(browse)
        grid.addLayout(out_row, row, 1)
        row += 1

        grid.addWidget(QLabel("文件名模板"), row, 0)
        self.pattern_edit = QLineEdit(self.config.filename_pattern)
        grid.addWidget(self.pattern_edit, row, 1)
        row += 1
        template_hint = QLabel("占位符：{app} {date} {time} {datetime} {resolution} {fps}")
        template_hint.setObjectName("Hint")
        grid.addWidget(template_hint, row, 0, 1, 2)
        row += 1

        grid.addWidget(QLabel("开始倒计时"), row, 0)
        self.countdown_combo = QComboBox()
        for label, _value in COUNTDOWN_CHOICES:
            self.countdown_combo.addItem(label)
        grid.addWidget(self.countdown_combo, row, 1)
        row += 1

        grid.addWidget(QLabel("最长录制"), row, 0)
        duration_row = QHBoxLayout()
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0, 86400)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSuffix(" 秒（0=不限）")
        duration_row.addWidget(self.duration_spin, 1)
        grid.addLayout(duration_row, row, 1)
        row += 1

        self.cursor_check = QCheckBox("录制鼠标光标")
        self.cursor_check.setChecked(True)
        grid.addWidget(self.cursor_check, row, 1)
        return box

    def _build_stats_group(self) -> QGroupBox:
        box = QGroupBox("录制状态")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.stat_labels = {}
        entries = (("elapsed", "已录制"), ("fps", "实际帧率"), ("size", "文件大小"),
                   ("out", "输出画面"), ("bitrate", "码率"), ("segment", "分段"),
                   ("input", "色彩输入"), ("file", "当前文件"))
        for index, (key, caption) in enumerate(entries):
            grid.addWidget(QLabel(caption), index % 4, (index // 4) * 2)
            value = QLabel("—")
            value.setObjectName("StatValue")
            grid.addWidget(value, index % 4, (index // 4) * 2 + 1)
            self.stat_labels[key] = value
        self.countdown_label = QLabel("")
        self.countdown_label.setObjectName("Countdown")
        grid.addWidget(self.countdown_label, 4, 0, 1, 4)
        return box

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.start_button = QPushButton("● 开始录制")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self._on_start)
        self.pause_button = QPushButton("‖ 暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._on_pause)
        self.stop_button = QPushButton("■ 停止")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop)
        open_dir = QPushButton("打开保存目录")
        open_dir.clicked.connect(self._open_dir)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Hint")
        row.addWidget(self.start_button)
        row.addWidget(self.pause_button)
        row.addWidget(self.stop_button)
        row.addWidget(open_dir)
        row.addStretch(1)
        row.addWidget(self.status_label)
        return row

    def _build_log_group(self) -> QGroupBox:
        box = QGroupBox("日志")
        layout = QVBoxLayout(box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(140)
        layout.addWidget(self.log_view)
        return box

    # ------------------------------------------------------------------
    # 配置 <-> 界面
    # ------------------------------------------------------------------
    def _load_config_into_ui(self) -> None:
        config = self.config
        self._select_by_data(self.resolution_combo, config.resolution)
        fps = str(config.fps if config.fps in q.FPS_CHOICES else 30)
        self.fps_combo.setCurrentText(fps)
        self._select_by_data(self.quality_combo, config.quality)
        self._select_by_data(self.profile_combo, config.profile)
        self._select_by_data(self.scale_combo, config.scale_mode)
        self.auto_bitrate_check.setChecked(config.bitrate_kbps <= 0)
        self.bitrate_spin.setEnabled(config.bitrate_kbps > 0)
        if config.bitrate_kbps > 0:
            self.bitrate_spin.setValue(config.bitrate_kbps)
        self.pattern_edit.setText(config.filename_pattern)
        self.cursor_check.setChecked(config.capture_cursor)
        self.duration_spin.setValue(config.max_duration)
        values = [value for _label, value in COUNTDOWN_CHOICES]
        index = values.index(config.start_delay) if config.start_delay in values else 3
        self.countdown_combo.setCurrentIndex(index)
        self._sync_preset_combo()
        self._update_estimate()

    @staticmethod
    def _select_by_data(combo: QComboBox, data) -> None:
        index = combo.findData(data)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _collect_config(self) -> q.RecordingConfig:
        """把界面上的值收集成 RecordingConfig（顺带校验）。"""
        target = self._selected_target()
        if target is None:
            raise ConfigError("请先在左侧选择要录制的窗口或「整个屏幕」")
        config = q.RecordingConfig(
            target=target,
            output_dir=self.output_edit.text().strip(),
            resolution=self.resolution_combo.currentData(),
            fps=int(self.fps_combo.currentText() or 30),
            quality=self.quality_combo.currentData(),
            bitrate_kbps=0 if self.auto_bitrate_check.isChecked() else int(self.bitrate_spin.value()),
            profile=self.profile_combo.currentData(),
            scale_mode=self.scale_combo.currentData(),
            filename_pattern=self.pattern_edit.text().strip() or q.DEFAULT_RECORD_PATTERN,
            start_delay=COUNTDOWN_CHOICES[self.countdown_combo.currentIndex()][1],
            max_duration=float(self.duration_spin.value()),
            capture_cursor=self.cursor_check.isChecked(),
        )
        config.validate()
        return config

    def _on_preset_changed(self, index: int) -> None:
        if index <= 0 or index - 1 >= len(q.QUALITY_PRESETS):
            return
        preset = q.QUALITY_PRESETS[index - 1]
        self._select_by_data(self.resolution_combo, preset.resolution)
        self.fps_combo.setCurrentText(str(preset.fps))
        self._select_by_data(self.quality_combo, preset.quality)
        self.auto_bitrate_check.setChecked(preset.bitrate_kbps <= 0)
        if preset.bitrate_kbps > 0:
            self.bitrate_spin.setValue(preset.bitrate_kbps)
        self._on_quality_changed()

    def _on_bitrate_edited(self) -> None:
        if not self.auto_bitrate_check.isChecked():
            self._on_quality_changed()

    def _on_quality_changed(self) -> None:
        self.bitrate_spin.setEnabled(not self.auto_bitrate_check.isChecked())
        self._sync_preset_combo()
        self._update_estimate()
        if self.engine is not None and self.engine.running:
            try:
                self.engine.apply_config(self._collect_config())
            except ConfigError:
                pass

    def _sync_preset_combo(self) -> None:
        """界面参数命中某个预设时把预设框也切过去，否则显示「自定义」。"""
        try:
            config = self._collect_config()
        except ConfigError:
            self.preset_combo.setCurrentIndex(0)
            return
        index = config.match_preset(q.QUALITY_PRESETS)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(0 if index < 0 else index + 1)
        self.preset_combo.blockSignals(False)

    def _update_estimate(self) -> None:
        rect = self._source_rect()
        if rect is None:
            self.estimate_label.setText("请选择录制目标以估算输出画质")
            return
        try:
            config = self._collect_config()
        except ConfigError:
            config = q.RecordingConfig(
                resolution=self.resolution_combo.currentData(),
                fps=int(self.fps_combo.currentText() or 30),
                quality=self.quality_combo.currentData(),
                bitrate_kbps=0 if self.auto_bitrate_check.isChecked() else int(self.bitrate_spin.value()),
            )
        width, height = config.output_size(rect[2] - rect[0], rect[3] - rect[1])
        rate = config.bitrate_for(width, height)
        self.estimate_label.setText(
            f"预计输出 {width}×{height} @{config.fps}fps，码率约 {rate} kbps"
            f"（≈ {rate * 60 / 8 / 1024:.0f} MB/分钟）")

    def _source_rect(self):
        target = self._selected_target()
        if target is None:
            return None
        if target.kind == TARGET_SCREEN:
            return w.get_virtual_screen_rect()
        if target.kind == TARGET_WINDOW and target.hwnd and w.is_window(target.hwnd):
            return w.get_window_rect_extended(target.hwnd)
        return None

    # ------------------------------------------------------------------
    # 来源列表
    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        try:
            self._windows = w.enum_windows(include_own=False)
        except Exception as exc:
            self._log(f"枚举窗口失败：{exc}")
            self._windows = []
        self._fill_window_list()

    def _fill_window_list(self) -> None:
        keyword = self.filter_edit.text().strip().lower()
        current = self._selected_target()
        self.window_list.clear()
        self._row_targets = []

        left, top, right, bottom = w.get_virtual_screen_rect()
        item = QListWidgetItem(f"【整个屏幕（所有显示器）】　{right - left} × {bottom - top}")
        self.window_list.addItem(item)
        self._row_targets.append(Target(kind=TARGET_SCREEN))

        for info in self._windows:
            if keyword and keyword not in info.title.lower() and keyword not in info.process_name.lower():
                continue
            title = info.title if len(info.title) <= 52 else info.title[:51] + "…"
            if info.minimized:
                title += "（最小化）"
            self.window_list.addItem(QListWidgetItem(f"{title}　[{info.process_name}] {info.size_text}"))
            self._row_targets.append(Target(kind=TARGET_WINDOW, hwnd=info.hwnd, title=info.title,
                                            app_label=info.process_name or info.title))

        index = 0
        if current is not None:
            for position, target in enumerate(self._row_targets):
                if target.kind == current.kind and (target.kind == TARGET_SCREEN
                                                    or target.hwnd == current.hwnd):
                    index = position
                    break
        if self.window_list.count():
            self.window_list.setCurrentRow(index)
        self._on_source_changed()

    def _selected_target(self) -> Optional[Target]:
        row = self.window_list.currentRow()
        if 0 <= row < len(self._row_targets):
            return self._row_targets[row]
        return None

    def _on_source_changed(self) -> None:
        target = self._selected_target()
        if target is None:
            self.source_label.setText("尚未选择目标")
        elif target.kind == TARGET_SCREEN:
            self.source_label.setText("当前目标：整个屏幕（所有显示器）")
        else:
            self.source_label.setText(
                f"当前目标：{target.title or hex(target.hwnd)}（{target.app_label}）")
        self._update_estimate()

    # ------------------------------------------------------------------
    # 录制控制
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        if self.engine is not None and self.engine.running:
            return
        try:
            config = self._collect_config()
        except ConfigError as exc:
            QMessageBox.warning(self, "参数有误", str(exc))
            return

        ok, message = probe_encoder()
        self._log(f"编码器检测：{message}")
        if not ok:
            QMessageBox.critical(self, "无法开始录制", message)
            return

        config.output_dir = str(Path(config.output_dir))
        try:
            Path(config.output_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.critical(self, "无法创建目录", f"{config.output_dir}\n{exc}")
            return

        self.config = config
        q.save_record_config(config)
        self.result_files = []
        self.engine = RecorderEngine(config, on_event=self.event_received.emit)
        try:
            self.engine.start(countdown=config.start_delay > 0)
        except ConfigError as exc:
            QMessageBox.warning(self, "无法开始录制", str(exc))
            return
        self._log(f"开始录制：{config.target.describe()}")
        self._log(f"画质：{config.describe()}")
        self.status_label.setText("准备中…")
        self._set_buttons(recording=True)
        self._stats_timer.start()      # 实时刷新录制状态面板
        if config.hide_window and config.start_delay > 0:
            self.showMinimized()

    def _on_pause(self) -> None:
        if self.engine is None:
            return
        if self.engine.state == STATE_PAUSED:
            self.engine.resume()
        else:
            self.engine.pause()

    def _on_stop(self) -> None:
        if self.engine is None:
            return
        self._log("正在停止并封盘…")
        self.status_label.setText("正在封盘…")
        self._set_buttons(recording=False, stopping=True)
        self.engine.stop()

    def _set_buttons(self, recording: bool, stopping: bool = False) -> None:
        state = self.engine.state if self.engine is not None else STATE_IDLE
        self.start_button.setEnabled(not (recording or stopping))
        self.stop_button.setEnabled(recording and not stopping)
        self.pause_button.setEnabled(state in (STATE_RECORDING, STATE_PAUSED))
        self.pause_button.setText("▶ 继续" if state == STATE_PAUSED else "‖ 暂停")

    # ------------------------------------------------------------------
    # 引擎事件
    # ------------------------------------------------------------------
    def _on_engine_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "started":
            self._log(f"目标：{event.get('target')}")
            self._log(f"参数：{event.get('quality')}")
        elif kind == "countdown":
            self._countdown = int(event.get("remaining") or 0)
            self.countdown_label.setText(f"{self._countdown} 秒后开始录制…")
        elif kind == "recording":
            self.countdown_label.setText("")
            self.status_label.setText("正在录制")
            self._set_buttons(recording=True)
        elif kind == "paused":
            self.status_label.setText("已暂停")
            self._set_buttons(recording=True)
        elif kind == "resumed":
            self.status_label.setText("正在录制")
            self._set_buttons(recording=True)
        elif kind == "file":
            self._log(f"写入：{event.get('path')}")
            self.result_files.append(event.get("path"))
        elif kind == "quality":
            self._log(f"画质：{event.get('text')}（输入 {event.get('input_format')}）")
        elif kind == "error":
            self._log(f"[错误] {event.get('message')}")
        elif kind == "finished":
            self.countdown_label.setText("")
            self.status_label.setText("已结束")
            self._set_buttons(recording=False)
            self._stats_timer.stop()
            summary = event.get("summary", "录制结束")
            self._log(summary)
            self._show_result(event.get("result"))

    def _show_result(self, result) -> None:
        if result is None:
            return
        files = [str(p) for p in getattr(result, "files", [])] or [
            str(p) for p in self.result_files if p]
        text = result.summary() if hasattr(result, "summary") else "录制结束"
        if not self.show_result_dialog:
            return
        if not files:
            QMessageBox.information(self, "录制结束", text)
            return
        answer = QMessageBox.question(
            self, "录制完成",
            text + "\n\n输出文件：\n" + "\n".join(files) + "\n\n是否打开保存目录？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._open_dir()

    # ------------------------------------------------------------------
    # 实时状态
    # ------------------------------------------------------------------
    def _update_stats(self) -> None:
        engine = self.engine
        if engine is None or not engine.running:
            return
        stats: RecordingStats = engine.snapshot()
        self.stat_labels["elapsed"].setText(stats.elapsed_text)
        fps_text = f"{stats.actual_fps:.1f} fps"
        if stats.dropped:
            fps_text += f"（丢 {stats.dropped}）"
        self.stat_labels["fps"].setText(fps_text)
        self.stat_labels["size"].setText(stats.size_text)
        self.stat_labels["out"].setText(f"{stats.output_width}×{stats.output_height}")
        self.stat_labels["bitrate"].setText(
            f"{stats.average_bitrate:.0f} / 设定 {stats.bitrate_kbps} kbps")
        self.stat_labels["segment"].setText(f"第 {stats.segment} 段")
        self.stat_labels["input"].setText(stats.input_format or "—")
        self.stat_labels["file"].setText(Path(stats.current_file).name if stats.current_file else "—")
        self._set_buttons(recording=True)

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------
    def _choose_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择录像保存目录",
                                                  self.output_edit.text() or str(Path.home()))
        if chosen:
            self.output_edit.setText(chosen)
            if self.engine is not None and self.engine.running:
                self._on_quality_changed()

    def _open_dir(self) -> None:
        path = Path(self.output_edit.text().strip() or ".")
        try:
            path.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            QMessageBox.critical(self, "无法打开目录", f"{path}\n{exc}")

    def _log(self, message: str) -> None:
        from datetime import datetime
        try:
            self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] {message}")
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        engine = self.engine
        if engine is not None and engine.running:
            answer = QMessageBox.question(
                self, "正在录制",
                "还在录制中，确定要停止并退出吗？\n（已录制的部分会正常保存）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            engine.stop()
            engine.join(15.0)
        try:
            if self.config is not None:
                self.config.output_dir = self.output_edit.text().strip()
                self.config.filename_pattern = self.pattern_edit.text().strip()
                q.save_record_config(self.config)
        except Exception:
            pass
        self._closing = True
        self._stats_timer.stop()
        super().closeEvent(event)


def present(parent: Optional[QWidget] = None, target: Optional[Target] = None,
            output_dir: Optional[str] = None, stylesheet: str = "") -> RecorderDialog:
    """创建并显示录屏窗口（非模态，方便同时看主界面）。"""
    dialog = RecorderDialog(parent, target=target, output_dir=output_dir, stylesheet=stylesheet)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
