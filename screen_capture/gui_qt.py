# -*- coding: utf-8 -*-
"""PyQt5 图形界面：奶白色现代风格。

* 三步式布局：选目标窗口 → 调截图设置 → 开始 / 停止，右侧实时日志；
* 截图引擎跑在后台线程，事件通过 Qt 信号回到主线程，界面不会卡；
* 所有日志实时写入本地文件（见 applog），界面里也能一键打开日志。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QEvent, QObject, QSettings, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap,
)
from PyQt5.QtWidgets import (
    QAbstractItemView, QAction, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import applog
from . import paths
from . import win32 as w
from .archive import DailyArchiver
from .engine import (
    CaptureConfig, CaptureEngine, ConfigError, Target,
    TARGET_SCREEN, TARGET_WINDOW, resolve_target, unknown_placeholders,
)

logger = applog.get_logger()

# --------------------------------------------------------------------------
# 配色：奶白 + 焦糖
# --------------------------------------------------------------------------
CREAM = "#FAF6EF"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#F3EDE3"
BORDER = "#E8DFD2"
TEXT = "#3B3530"
TEXT_SUB = "#8B8073"
PRIMARY = "#E0913F"
PRIMARY_HOVER = "#CE7F2E"
PRIMARY_SOFT = "#FBEBD9"
DARK_ACCENT = "#B06824"
OK_COLOR = "#4F9D69"


def _arrow_icon_path() -> str:
    """下拉箭头图片路径（QSS 的 image: url() 需要真实文件）。"""
    for candidate in (paths.resource_dir() / "assets" / "arrow_down.png",
                      paths.app_dir() / "assets" / "arrow_down.png",
                      Path(__file__).resolve().parent.parent / "assets" / "arrow_down.png"):
        if candidate.is_file():
            return candidate.as_posix()
    return ""


_ARROW = _arrow_icon_path()
_ARROW_RULE = (f"QComboBox::down-arrow {{ image: url({_ARROW}); width: 12px; height: 12px; }}"
               if _ARROW else "")

QSS = f"""
#Root {{ background: {CREAM}; }}
QWidget {{ color: {TEXT}; font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#CardTitle {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
QLabel#Hint {{ color: {TEXT_SUB}; font-size: 12px; }}
QLabel#AppTitle {{ font-size: 19px; font-weight: 700; color: {TEXT}; }}
QLabel#AppSub {{ color: {TEXT_SUB}; font-size: 12px; }}
QLabel#Status {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; padding: 7px 14px; color: {TEXT}; }}
QLabel#Preview {{ background: {SURFACE_ALT}; border: 1px dashed {BORDER}; border-radius: 12px; color: {TEXT_SUB}; }}
QPushButton {{ background: {SURFACE_ALT}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 15px; color: {TEXT}; }}
QPushButton:hover {{ background: #EFE7DB; }}
QPushButton:pressed {{ background: #E5DACA; }}
QPushButton:disabled {{ color: #BDB2A4; background: #F6F1E9; }}
QPushButton#Primary {{ background: {PRIMARY}; color: #FFFFFF; border: none; font-weight: 600; padding: 10px 26px; border-radius: 11px; }}
QPushButton#Primary:hover {{ background: {PRIMARY_HOVER}; }}
QPushButton#Primary:disabled {{ background: #E9DCC9; color: #B6A896; }}
QPushButton#Danger {{ background: #FFFFFF; color: #C05A5A; border: 1px solid #E7C9C9; font-weight: 600; padding: 10px 22px; border-radius: 11px; }}
QPushButton#Danger:hover {{ background: #FBEFEF; }}
QPushButton#Quick {{ padding: 4px 10px; border-radius: 8px; color: {TEXT_SUB}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 9px; selection-background-color: {PRIMARY};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {PRIMARY}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none; background: transparent;
}}
{_ARROW_RULE}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 18px; background: transparent; border: none; }}
QFrame#SubCard {{ background: {SURFACE_ALT}; border: 1px solid {BORDER}; border-radius: 11px; }}
QLabel#SubTitle {{ font-weight: 600; color: {TEXT_SUB}; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; outline: none; }}
QTableWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 11px; gridline-color: transparent; outline: none; }}
QTableWidget::item {{ padding: 5px 6px; border-bottom: 1px solid #F4ECE0; }}
QTableWidget::item:selected {{ background: {PRIMARY_SOFT}; color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE_ALT}; border: none; padding: 7px 6px; color: {TEXT_SUB}; font-weight: 600; }}
QTableCornerButton::section {{ background: {SURFACE_ALT}; border: none; }}
QPlainTextEdit {{ background: #2E2A26; color: #EAE3D9; border: none; border-radius: 11px; padding: 8px; font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; }}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid #D8CDBC; background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border: 1px solid {PRIMARY}; }}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border: 1px solid {PRIMARY}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #DED3C2; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #CFC2AD; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #DED3C2; border-radius: 5px; }}
QScrollArea {{ border: none; background: transparent; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; padding: 5px; }}
"""

METHOD_CHOICES = [
    ("自动（WGC 优先，最稳）", w.METHOD_AUTO),
    ("WGC 窗口捕获（可截被遮挡/后台窗口）", w.METHOD_WGC),
    ("PrintWindow（GDI 后台截图）", w.METHOD_PRINTWINDOW),
    ("屏幕区域（所见即所得）", w.METHOD_SCREEN),
]
FORMAT_CHOICES = ["png", "jpg", "bmp", "webp"]
QUICK_INTERVALS = [1, 2, 5, 10, 30, 60]
# 版本 2 起默认保存目录改为「程序目录\ScreenCapture」，旧版本记录要重设一次
SETTINGS_VERSION = 3


def default_output_dir() -> Path:
    """默认保存到「程序目录\\ScreenCapture」。"""
    return paths.default_capture_dir()


def app_icon() -> QIcon:
    """程序图标：优先用打包进来的 assets/app.ico，没有就临时画一个。"""
    for candidate in (paths.resource_dir() / "assets" / "app.ico",
                      paths.app_dir() / "assets" / "app.ico",
                      Path(__file__).resolve().parent.parent / "assets" / "app.ico"):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(PRIMARY))
    painter.drawRoundedRect(4, 16, 56, 40, 9, 9)
    painter.setBrush(QColor(SURFACE))
    painter.drawEllipse(20, 26, 24, 24)
    painter.setBrush(QColor(DARK_ACCENT))
    painter.drawEllipse(26, 32, 12, 12)
    painter.end()
    return QIcon(pixmap)


class EventBridge(QObject):
    """把后台线程的事件安全地送回主线程。"""

    event = pyqtSignal(dict)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("应用窗口定时截图工具")
        self.resize(1360, 960)
        self.setMinimumSize(1120, 780)

        self.engine: CaptureEngine | None = None
        self.bridge = EventBridge()
        self.bridge.event.connect(self.on_engine_event)
        self.window_list: list[w.WindowInfo] = []
        self.selected_hwnd: int = 0
        self.select_screen: bool = False
        self.next_at: float | None = None
        self.counts = {"saved": 0, "skipped": 0}
        self.started_at: float | None = None
        self.preview_path: Path | None = None
        self.settings = QSettings("ScreenCaptureTool", "ScreenCapture")
        self.tray: QSystemTrayIcon | None = None
        self.archiver: DailyArchiver | None = None
        self.monitoring = False          # 监听模式：窗口收起来了但截图继续
        self._tray_hint_shown = False
        self._quitting = False

        self._build_ui()
        self.setWindowIcon(app_icon())
        self._load_settings()
        self.refresh_windows()
        self._attach_logging()
        self.archive_check.stateChanged.connect(lambda _v: self._on_archive_toggled())
        self._build_tray()
        self._start_archiver()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

        logger.info("界面已就绪（PyQt5 %s）", _qt_version())

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        outer.addLayout(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(12)
        outer.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_target_card(), 1)
        left.addWidget(self._build_preview_card())
        body.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_settings_card())
        right.addWidget(self._build_log_card(), 1)
        body.addLayout(right, 2)

        outer.addLayout(self._build_actions())

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("应用窗口定时截图工具", objectName="AppTitle")
        sub = QLabel("选择窗口 · 自定义间隔 · WGC 后台捕获", objectName="AppSub")
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title)
        column.addWidget(sub)
        row.addLayout(column)
        row.addStretch(1)
        self.status_pill = QLabel("就绪：请选择目标窗口并设置间隔时间", objectName="Status")
        row.addWidget(self.status_pill)
        return row

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame(objectName="Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)
        label = QLabel(title, objectName="CardTitle")
        layout.addWidget(label)
        return frame, layout

    def _build_target_card(self) -> QFrame:
        frame, layout = self._card("1. 选择要截图的应用窗口")

        bar = QHBoxLayout()
        self.filter_edit = QLineEdit(placeholderText="按标题或程序名筛选…")
        self.filter_edit.textChanged.connect(lambda _t: self.fill_table())
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self.refresh_windows)
        bar.addWidget(self.filter_edit, 1)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["窗口标题", "程序", "尺寸", "句柄"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_select)
        self.table.doubleClicked.connect(lambda _i: self.on_start())
        layout.addWidget(self.table, 1)

        options = QHBoxLayout()
        self.screen_check = QCheckBox("改截整个屏幕（所有显示器）")
        self.screen_check.stateChanged.connect(lambda _v: self.on_screen_toggle())
        self.own_check = QCheckBox("显示本程序窗口")
        self.own_check.stateChanged.connect(lambda _v: self.refresh_windows())
        self.min_check = QCheckBox("显示最小化窗口")
        self.min_check.setChecked(True)
        self.min_check.stateChanged.connect(lambda _v: self.fill_table())
        options.addWidget(self.screen_check)
        options.addWidget(self.own_check)
        options.addWidget(self.min_check)
        options.addStretch(1)
        layout.addLayout(options)

        self.target_label = QLabel("尚未选择目标", objectName="Hint")
        layout.addWidget(self.target_label)
        return frame

    def _build_preview_card(self) -> QFrame:
        frame, layout = self._card("2. 最新截图预览")
        self.preview = QLabel("（还没有截图）", objectName="Preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(170)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.preview)
        self.preview_caption = QLabel("", objectName="Hint")
        self.preview_caption.setWordWrap(True)
        layout.addWidget(self.preview_caption)
        return frame

    def _build_settings_card(self) -> QFrame:
        frame, outer = self._card("3. 截图设置")

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 6, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setAlignment(Qt.AlignTop)     # 行贴着顶部排，多余高度留在下方
        row = 0

        grid.addWidget(QLabel("截图间隔（秒）"), row, 0)
        interval_row = QHBoxLayout()
        interval_row.setSpacing(8)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 86400)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setValue(5.0)
        self.interval_spin.setSingleStep(0.5)
        self.interval_spin.setFixedWidth(96)
        interval_row.addWidget(self.interval_spin)
        interval_row.addWidget(QLabel("快捷："))
        for value in QUICK_INTERVALS:
            button = QPushButton(f"{value}s", objectName="Quick")
            button.clicked.connect(lambda _c, v=value: self.interval_spin.setValue(float(v)))
            interval_row.addWidget(button)
        interval_row.addStretch(1)
        grid.addLayout(interval_row, row, 1)
        row += 1

        grid.addWidget(QLabel("首张延迟（秒）"), row, 0)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0, 86400)
        self.delay_spin.setDecimals(1)
        self.delay_spin.setFixedWidth(96)
        grid.addWidget(self.delay_spin, row, 1)
        row += 1

        grid.addWidget(QLabel("截图数量上限"), row, 0)
        count_row = QHBoxLayout()
        count_row.setSpacing(8)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(0, 1000000)
        self.count_spin.setSpecialValueText("0（不限）")
        self.count_spin.setFixedWidth(96)
        count_row.addWidget(self.count_spin)
        count_row.addWidget(QLabel("最长运行时长（秒）"))
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0, 86400 * 7)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSpecialValueText("0（不限）")
        self.duration_spin.setFixedWidth(96)
        count_row.addWidget(self.duration_spin)
        count_row.addStretch(1)
        grid.addLayout(count_row, row, 1)
        row += 1

        grid.addWidget(QLabel("截图方式"), row, 0)
        self.method_combo = QComboBox()
        for label, _value in METHOD_CHOICES:
            self.method_combo.addItem(label)
        self.method_combo.currentIndexChanged.connect(lambda _i: self.on_method_changed())
        grid.addWidget(self.method_combo, row, 1)
        row += 1

        grid.addWidget(QLabel("图片格式 / 质量"), row, 0)
        format_row = QHBoxLayout()
        format_row.setSpacing(8)
        self.format_combo = QComboBox()
        self.format_combo.setFixedWidth(90)
        self.format_combo.addItems(FORMAT_CHOICES)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(90)
        self.quality_spin.setFixedWidth(74)
        format_row.addWidget(self.format_combo)
        format_row.addWidget(self.quality_spin)
        format_row.addStretch(1)
        grid.addLayout(format_row, row, 1)
        row += 1

        grid.addWidget(QLabel("文件名模板"), row, 0)
        self.pattern_edit = QLineEdit("{app}_{date}_{time}_{index:04d}")
        self.pattern_edit.setToolTip(
            "可用占位符：{app} 应用名，{date} 日期，{time} 时间，{datetime} 日期+时间，"
            "{index} 序号（{index:04d} 补零），{hwnd} 窗口句柄，{ms} 毫秒"
        )
        grid.addWidget(self.pattern_edit, row, 1)
        row += 1

        grid.addWidget(QLabel("保存目录"), row, 0)
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        self.output_edit = QLineEdit(str(default_output_dir()))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self.on_choose_dir)
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse)
        grid.addLayout(out_row, row, 1)
        row += 1

        options_card = QFrame(objectName="SubCard")
        options_layout = QVBoxLayout(options_card)
        options_layout.setContentsMargins(12, 10, 12, 12)
        options_layout.setSpacing(7)
        options_layout.addWidget(QLabel("更多选项", objectName="SubTitle"))

        self.skip_check = QCheckBox("画面无变化时跳过保存")
        self.subdir_check = QCheckBox("每次开始创建独立子目录")
        self.subdir_check.setChecked(True)
        self.manifest_check = QCheckBox("生成截图清单 CSV")
        self.manifest_check.setChecked(True)
        self.client_check = QCheckBox("只截客户区（去掉标题栏）")
        self.cursor_check = QCheckBox("画面包含鼠标光标")
        self.activate_check = QCheckBox("截图前把窗口切到前台")
        self.activate_check.setChecked(True)
        self.activate_check.setToolTip(
            "屏幕区域方式下，窗口被遮挡会截到遮挡窗口；勾选后每次抓帧前先把目标窗口带到最前")
        self.archive_check = QCheckBox("每天 0 点自动压缩前一天的截图")
        self.archive_check.setChecked(True)
        self.archive_check.setToolTip(
            "把前一天的图片打包成 <输出目录>\\_archive\\YYYY-MM-DD.zip，节省磁盘空间")
        self.archive_delete_check = QCheckBox("归档后删除原图")
        self.archive_delete_check.setChecked(True)
        self.monitor_check = QCheckBox("最小化后进入监听模式（截图继续）")
        self.monitor_check.setChecked(True)
        self.monitor_check.setToolTip(
            "窗口最小化时收进托盘继续截图，只有退出程序才会停止")
        self.tray_check = QCheckBox("点关闭按钮也收进托盘（不停止）")
        self.tray_check.setChecked(False)
        self.tray_check.setToolTip(
            "不勾选时：点 × 直接退出并停止截图。勾选后：点 × 只是收进托盘，"
            "需要从托盘菜单「退出」才真正结束")

        options_grid = QGridLayout()
        options_grid.setHorizontalSpacing(18)
        options_grid.setVerticalSpacing(9)
        boxes = (
            self.skip_check, self.subdir_check, self.manifest_check,
            self.client_check, self.cursor_check, self.activate_check,
            self.archive_check, self.archive_delete_check,
            self.monitor_check, self.tray_check,
        )
        for index, box in enumerate(boxes):
            options_grid.addWidget(box, index // 3, index % 3)
        options_layout.addLayout(options_grid)

        grid.addWidget(options_card, row, 0, 1, 2)
        row += 1

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setMinimumHeight(500)
        outer.addWidget(scroll, 1)
        return frame

    def _build_log_card(self) -> QFrame:
        frame, layout = self._card("4. 运行日志（实时写入本地文件）")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setMinimumHeight(120)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.log_view.setFont(font)
        layout.addWidget(self.log_view, 1)

        row = QHBoxLayout()
        self.log_path_label = QLabel("日志文件：准备中…", objectName="Hint")
        self.log_path_label.setWordWrap(True)
        open_file = QPushButton("打开日志文件")
        open_file.clicked.connect(self.on_open_log_file)
        open_dir = QPushButton("打开日志目录")
        open_dir.clicked.connect(self.on_open_log_dir)
        clear = QPushButton("清空显示")
        clear.clicked.connect(self.log_view.clear)
        row.addWidget(self.log_path_label, 1)
        row.addWidget(open_file)
        row.addWidget(open_dir)
        row.addWidget(clear)
        layout.addLayout(row)
        return frame

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        self.start_button = QPushButton("▶  开始截图", objectName="Primary")
        self.start_button.clicked.connect(self.on_start)
        self.stop_button = QPushButton("■  停止", objectName="Danger")
        self.stop_button.clicked.connect(self.on_stop)
        self.stop_button.setEnabled(False)
        self.test_button = QPushButton("试截一张")
        self.test_button.clicked.connect(self.on_test_shot)
        open_dir = QPushButton("打开保存目录")
        open_dir.clicked.connect(self.on_open_output_dir)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.test_button)
        row.addWidget(open_dir)
        row.addStretch(1)
        self.counter_label = QLabel("已保存 0 张｜跳过 0 张", objectName="Hint")
        row.addWidget(self.counter_label)
        return row

    # ------------------------------------------------------------------
    # 托盘 / 后台运行
    # ------------------------------------------------------------------
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.info("系统托盘不可用，关闭窗口即退出程序")
            return
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip("应用窗口定时截图工具")
        menu = QMenu(self)
        action_show = QAction("显示主界面", self)
        action_show.triggered.connect(self._restore_window)
        action_start = QAction("开始截图", self)
        action_start.triggered.connect(self.on_start)
        action_stop = QAction("停止", self)
        action_stop.triggered.connect(self.on_stop)
        action_open = QAction("打开保存目录", self)
        action_open.triggered.connect(self.on_open_output_dir)
        action_quit = QAction("退出", self)
        action_quit.triggered.connect(self._quit_app)
        menu.addAction(action_show)
        menu.addSeparator()
        menu.addAction(action_start)
        menu.addAction(action_stop)
        menu.addAction(action_open)
        menu.addSeparator()
        menu.addAction(action_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray = tray
        self._tray_menu = menu

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self._restore_window()

    def _restore_window(self) -> None:
        self.monitoring = False
        self.showNormal()
        self.raise_()
        self.activateWindow()
        status = "运行中" if (self.engine is not None and self.engine.running) else "待机"
        logger.info("窗口已恢复显示（%s）", status)

    # ------------------------------------------------------------------
    # 监听模式：最小化 ≠ 停止
    # ------------------------------------------------------------------
    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            self._enter_monitoring()
        super().changeEvent(event)

    def _enter_monitoring(self) -> None:
        """窗口最小化：进入监听模式，截图继续跑，只有退出程序才停止。"""
        if self.monitoring or self._quitting:
            return
        self.monitoring = True
        running = self.engine is not None and self.engine.running
        logger.info("窗口已最小化：进入监听模式，%s（已保存 %s 张）",
                    "截图继续进行" if running else "尚未开始截图",
                    self.counts["saved"])
        if not self.monitor_check.isChecked():
            return
        if self.tray is not None:
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    "已进入监听模式",
                    ("截图会继续在后台执行。" if running else "窗口已收进托盘。")
                    + "双击托盘图标恢复窗口；右键托盘可停止或退出。",
                    QSystemTrayIcon.Information, 4000,
                )

    def _quit_app(self) -> None:
        self._quitting = True
        if self.engine is not None and self.engine.running:
            self.engine.stop(timeout=1.0)
        self._stop_archiver()
        if self.tray is not None:
            self.tray.hide()
        logger.info("用户从托盘退出程序")
        QApplication.quit()

    # ------------------------------------------------------------------
    # 每日归档
    # ------------------------------------------------------------------
    def _start_archiver(self) -> None:
        if self.archiver is not None or not self.archive_check.isChecked():
            return
        self.archiver = DailyArchiver(
            root_provider=lambda: self.output_edit.text().strip() or str(default_output_dir()),
            delete_originals=self.archive_delete_check.isChecked(),
            on_event=lambda path: self.bridge.event.emit({"type": "archived", "path": str(path)}),
        )
        self.archiver.start()
        logger.info("已启用每日归档：每天 0 点把前一天的截图压缩到 _archive 目录")

    def _stop_archiver(self) -> None:
        if self.archiver is not None:
            self.archiver.stop()
            self.archiver = None

    def _on_archive_toggled(self) -> None:
        if self.archive_check.isChecked():
            self._start_archiver()
            self.append_log("已开启每日归档：每天 0 点把前一天的截图压缩成 zip")
        else:
            self._stop_archiver()
            self.append_log("已关闭每日归档")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _attach_logging(self) -> None:
        path = applog.setup_logging()
        self.log_path_label.setText(f"日志文件：{path}")
        handler = applog.attach_callback_handler(self._log_from_thread)
        self._log_handler = handler

    def _log_from_thread(self, text: str, _record) -> None:
        """可能在后台线程被调用，用信号切回主线程。"""
        self.bridge.event.emit({"type": "_log", "text": text})

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def on_open_log_file(self) -> None:
        path = applog.get_log_path()
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def on_open_log_dir(self) -> None:
        path = applog.get_log_path()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    # ------------------------------------------------------------------
    # 窗口列表
    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.window_list = w.enum_windows(include_own=self.own_check.isChecked())
        except Exception as exc:
            logger.error("枚举窗口失败：%s", exc)
            self.window_list = []
        finally:
            QApplication.restoreOverrideCursor()
        self.fill_table()
        logger.info("已刷新窗口列表，共 %d 个可见窗口", len(self.window_list))

    def fill_table(self) -> None:
        keyword = self.filter_edit.text().strip().lower()
        self.table.setRowCount(0)
        for info in self.window_list:
            if not self.min_check.isChecked() and info.minimized:
                continue
            if keyword and keyword not in info.title.lower() and keyword not in info.process_name.lower():
                continue
            self._add_row(info)
        self._sync_target_label()

    def _add_row(self, info: w.WindowInfo) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        title = info.title if len(info.title) <= 70 else info.title[:69] + "…"
        if info.minimized:
            title += "（最小化）"
        values = [title, info.process_name, info.size_text, f"0x{info.hwnd:X}"]
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column == 3:
                item.setTextAlignment(Qt.AlignCenter)
            item.setData(Qt.UserRole, info.hwnd)
            self.table.setItem(row, column, item)

    def _selected_hwnd(self) -> int:
        items = self.table.selectedItems()
        if not items:
            return 0
        return int(items[0].data(Qt.UserRole) or 0)

    def on_screen_toggle(self) -> None:
        if self.screen_check.isChecked():
            self.table.clearSelection()
            self.table.setEnabled(False)
        else:
            self.table.setEnabled(True)
        self.fill_table()

    def on_select(self) -> None:
        self.select_screen = self.screen_check.isChecked()
        self.selected_hwnd = 0 if self.select_screen else self._selected_hwnd()
        self._sync_target_label()

    def _sync_target_label(self) -> None:
        if self.screen_check.isChecked():
            left, top, right, bottom = w.get_virtual_screen_rect()
            self.target_label.setText(
                f"当前目标：整个屏幕（所有显示器）　{right - left} × {bottom - top}"
            )
            self.target_label.setStyleSheet(f"color: {OK_COLOR};")
            return
        info = self._current_info()
        if info is None:
            self.target_label.setText("尚未选择目标")
            self.target_label.setStyleSheet(f"color: {TEXT_SUB};")
            return
        flag = "　⚠ 已最小化" if info.minimized else ""
        self.target_label.setText(f"当前目标：{info.title or info.process_name}{flag}")
        self.target_label.setStyleSheet(f"color: {OK_COLOR};")

    def _current_info(self) -> w.WindowInfo | None:
        hwnd = self._selected_hwnd()
        for info in self.window_list:
            if info.hwnd == hwnd:
                return info
        return None

    def on_method_changed(self) -> None:
        method = self.current_method()
        self.activate_check.setEnabled(method == w.METHOD_SCREEN)
        if method == w.METHOD_AUTO and not _wgc_available():
            self.append_log("提示：当前环境没有可用的 WGC，自动方式会退回 GDI 截图。")

    def current_method(self) -> str:
        index = max(0, self.method_combo.currentIndex())
        return METHOD_CHOICES[index][1]

    def on_choose_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择截图保存目录", self.output_edit.text())
        if chosen:
            self.output_edit.setText(chosen)

    def on_open_output_dir(self) -> None:
        path = Path(self.output_edit.text())
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开目录", f"{path}\n{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ------------------------------------------------------------------
    # 开始 / 停止 / 试截
    # ------------------------------------------------------------------
    def collect_config(self) -> CaptureConfig:
        if self.screen_check.isChecked():
            target = Target(kind=TARGET_SCREEN)
        else:
            info = self._current_info()
            if info is None:
                raise ConfigError("请先在列表中选择一个应用窗口（或勾选“改截整个屏幕”）")
            target = Target(kind=TARGET_WINDOW, hwnd=info.hwnd, title=info.title,
                            app_label=info.process_name or info.title, class_name=info.class_name)

        config = CaptureConfig(
            target=target,
            output_dir=self.output_edit.text().strip(),
            interval=float(self.interval_spin.value()),
            start_delay=float(self.delay_spin.value()),
            max_shots=int(self.count_spin.value()),
            max_duration=float(self.duration_spin.value()),
            method=self.current_method(),
            skip_unchanged=self.skip_check.isChecked(),
            session_subdir=self.subdir_check.isChecked(),
            image_format=self.format_combo.currentText(),
            jpeg_quality=int(self.quality_spin.value()),
            filename_pattern=self.pattern_edit.text().strip() or "{app}_{date}_{time}_{index:04d}",
            write_manifest=self.manifest_check.isChecked(),
            client_only=self.client_check.isChecked(),
            capture_cursor=self.cursor_check.isChecked(),
            activate_before_capture=self.activate_check.isChecked(),
        )
        config.validate()

        if target.kind == TARGET_WINDOW and config.method == w.METHOD_WGC and not _wgc_available():
            raise ConfigError("当前环境不可用 WGC，请改用「自动」或其它截图方式")
        return config

    def on_start(self) -> None:
        if self.engine is not None and self.engine.running:
            return
        try:
            config = self.collect_config()
        except ConfigError as exc:
            QMessageBox.warning(self, "无法开始截图", str(exc))
            return

        self.counts = {"saved": 0, "skipped": 0}
        self.started_at = time.monotonic()
        self.next_at = None
        self._save_settings()

        self.engine = CaptureEngine(config, self.bridge.event.emit)
        try:
            self.engine.start()
        except Exception as exc:
            self.engine = None
            QMessageBox.critical(self, "启动失败", str(exc))
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.test_button.setEnabled(False)
        unknown = unknown_placeholders(config.filename_pattern)
        if unknown:
            self.append_log("提示：文件名模板里的 "
                            + "、".join("{" + n + "}" for n in unknown) + " 无法识别，已用默认命名。")

    def on_stop(self) -> None:
        if self.engine is not None and self.engine.running:
            logger.info("用户点击停止")
            self.engine.stop(timeout=0.05)
        self.set_idle()

    def set_idle(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.test_button.setEnabled(True)
        self.next_at = None

    def on_test_shot(self) -> None:
        try:
            config = self.collect_config()
        except ConfigError as exc:
            QMessageBox.warning(self, "无法试截", str(exc))
            return
        self.test_button.setEnabled(False)
        self.append_log("正在试截一张…")

        def work() -> None:
            try:
                if config.target.kind == TARGET_SCREEN:
                    image, used = w.capture_screen_full(), w.METHOD_SCREEN
                else:
                    from . import capture_wgc as wgc
                    image = None
                    used = config.method
                    if config.method in (w.METHOD_AUTO, w.METHOD_WGC) and wgc.available():
                        try:
                            session = wgc.WgcSession(
                                config.target.title, config.target.class_name,
                                client_only=config.client_only,
                                capture_cursor=config.capture_cursor,
                            )
                            image = session.grab(1.0)
                            session.close()
                            used = w.METHOD_WGC
                        except Exception as exc:
                            logger.warning("试截时 WGC 不可用：%s", exc)
                    if image is None:
                        image, used = w.capture_window(
                            config.target.hwnd, method=config.method,
                            client_only=config.client_only,
                            activate=config.activate_before_capture,
                        )
                path = Path(tempfile.gettempdir()) / "screen_capture_preview.png"
                image.save(path, "PNG")
                self.bridge.event.emit({
                    "type": "test-shot", "path": str(path), "filename": "试截预览（未保存到目录）",
                    "width": image.width, "height": image.height, "method": used,
                    "bytes": path.stat().st_size,
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                })
            except Exception as exc:
                self.bridge.event.emit({"type": "error", "message": f"试截失败：{exc}", "fatal": False})
            finally:
                self.bridge.event.emit({"type": "test-done"})

        threading.Thread(target=work, name="test-shot", daemon=True).start()

    # ------------------------------------------------------------------
    # 引擎事件
    # ------------------------------------------------------------------
    def on_engine_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "_log":
            self.append_log(event["text"])
            return
        if kind == "started":
            self.status_pill.setText(
                f"正在截图：{event['target']}｜间隔 {event['interval']:g} 秒｜方式 {event['method']}"
            )
        elif kind == "shot":
            self.counts["saved"] = event.get("total_saved", self.counts["saved"] + 1)
            self.show_preview(event)
        elif kind == "skip":
            self.counts["skipped"] = event.get("total_skipped", self.counts["skipped"] + 1)
        elif kind == "test-shot":
            self.show_preview(event)
        elif kind == "test-done":
            self.test_button.setEnabled(True)
        elif kind == "waiting":
            self.next_at = event.get("next_at")
        elif kind == "error":
            if event.get("fatal"):
                QMessageBox.critical(self, "截图失败", event["message"])
        elif kind == "finished":
            self.set_idle()
            self.status_pill.setText(
                f"已结束（{event['reason']}）：保存 {event.get('saved', 0)} 张，跳过 {event.get('skipped', 0)} 张"
            )
        elif kind == "archived":
            path = Path(event["path"])
            self.append_log(f"已归档前一天的截图：{path.name}"
                            f"（{path.stat().st_size / 1024 / 1024:.1f} MB）")

    def show_preview(self, event: dict) -> None:
        try:
            pixmap = QPixmap(event["path"])
            if pixmap.isNull():
                return
            scaled = pixmap.scaled(self.preview.width() - 8, self.preview.height() - 8,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.preview.setPixmap(scaled)
            self.preview.setStyleSheet("border: 1px solid #E8DFD2; border-radius: 12px;")
            self.preview_path = Path(event["path"])
            self.preview_caption.setText(
                f"{event['filename']}　{event['width']}×{event['height']}　"
                f"{event.get('bytes', 0) / 1024:.1f} KB　方式：{event.get('method', '')}"
            )
        except Exception as exc:
            logger.warning("预览失败：%s", exc)

    def _tick(self) -> None:
        running = self.engine is not None and self.engine.running
        if running:
            elapsed = time.monotonic() - (self.started_at or time.monotonic())
            remaining = ""
            if self.next_at is not None:
                remaining = f"｜下次 {max(0.0, self.next_at - time.monotonic()):.1f} 秒后"
            prefix = "监听中（窗口已收起）" if self.monitoring else "运行中"
            text = (f"{prefix} {elapsed:,.0f} 秒｜保存 {self.counts['saved']} 张"
                    f"｜跳过 {self.counts['skipped']} 张{remaining}")
        else:
            text = ("监听模式：窗口已收起，尚未开始截图" if self.monitoring
                    else "就绪：请选择目标窗口并设置间隔时间")
        self.status_pill.setText(text)
        if self.tray is not None:
            self.tray.setToolTip(f"应用窗口定时截图工具\n{text}")
        self.counter_label.setText(
            f"已保存 {self.counts['saved']} 张｜跳过 {self.counts['skipped']} 张"
        )

    # ------------------------------------------------------------------
    # 设置持久化与关闭
    # ------------------------------------------------------------------
    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("settings_version", SETTINGS_VERSION)
        s.setValue("interval", self.interval_spin.value())
        s.setValue("delay", self.delay_spin.value())
        s.setValue("count", self.count_spin.value())
        s.setValue("duration", self.duration_spin.value())
        s.setValue("method", self.method_combo.currentIndex())
        s.setValue("format", self.format_combo.currentText())
        s.setValue("quality", self.quality_spin.value())
        s.setValue("pattern", self.pattern_edit.text())
        s.setValue("output", self.output_edit.text())
        s.setValue("skip", self.skip_check.isChecked())
        s.setValue("subdir", self.subdir_check.isChecked())
        s.setValue("manifest", self.manifest_check.isChecked())
        s.setValue("client", self.client_check.isChecked())
        s.setValue("cursor", self.cursor_check.isChecked())
        s.setValue("activate", self.activate_check.isChecked())
        s.setValue("archive", self.archive_check.isChecked())
        s.setValue("archive_delete", self.archive_delete_check.isChecked())
        s.setValue("monitor", self.monitor_check.isChecked())
        s.setValue("tray", self.tray_check.isChecked())

    def _load_settings(self) -> None:
        s = self.settings
        self.interval_spin.setValue(float(s.value("interval", 5.0)))
        self.delay_spin.setValue(float(s.value("delay", 0.0)))
        self.count_spin.setValue(int(s.value("count", 0)))
        self.duration_spin.setValue(float(s.value("duration", 0.0)))
        self.method_combo.setCurrentIndex(int(s.value("method", 0)))
        fmt = str(s.value("format", "png"))
        if fmt in FORMAT_CHOICES:
            self.format_combo.setCurrentText(fmt)
        self.quality_spin.setValue(int(s.value("quality", 90)))
        self.pattern_edit.setText(str(s.value("pattern", "{app}_{date}_{time}_{index:04d}")))
        try:
            stored_version = int(s.value("settings_version", 0))
        except (TypeError, ValueError):
            stored_version = 0
        fresh = stored_version < SETTINGS_VERSION
        if fresh:
            # 老版本的默认保存目录、托盘/监听默认值都变了，这里统一回落到新默认
            self.output_edit.setText(str(default_output_dir()))
            s.setValue("settings_version", SETTINGS_VERSION)
        else:
            self.output_edit.setText(str(s.value("output", str(default_output_dir()))))
        self.skip_check.setChecked(s.value("skip", False, type=bool))
        self.subdir_check.setChecked(s.value("subdir", True, type=bool))
        self.manifest_check.setChecked(s.value("manifest", True, type=bool))
        self.client_check.setChecked(s.value("client", False, type=bool))
        self.cursor_check.setChecked(s.value("cursor", False, type=bool))
        self.activate_check.setChecked(s.value("activate", True, type=bool))
        self.archive_check.setChecked(s.value("archive", True, type=bool))
        self.archive_delete_check.setChecked(s.value("archive_delete", True, type=bool))
        # 新版默认：最小化就进监听模式；点 × 直接退出（停止截图）
        self.monitor_check.setChecked(True if fresh else s.value("monitor", True, type=bool))
        self.tray_check.setChecked(False if fresh else s.value("tray", False, type=bool))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        running = self.engine is not None and self.engine.running
        # 勾了「点关闭按钮也收进托盘」：× 只收进托盘，继续监听
        if self.tray is not None and self.tray_check.isChecked() and not self._quitting:
            event.ignore()
            self.monitoring = True
            self.hide()
            logger.info("窗口已收进托盘：截图继续，退出请用托盘菜单的「退出」")
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    "仍在后台运行",
                    "截图任务会继续；双击托盘图标恢复窗口，右键托盘「退出」才真正结束。",
                    QSystemTrayIcon.Information, 4000,
                )
            return
        # 默认：关掉窗口 = 退出程序 = 停止截图
        if running:
            answer = QMessageBox.question(
                self, "退出程序",
                f"正在截图（已保存 {self.counts['saved']} 张）。\n关闭程序会停止截图，确定退出吗？")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._quitting = True
        self.monitoring = False
        if self.engine is not None and self.engine.running:
            logger.info("退出程序，已停止截图")
            self.engine.stop(timeout=1.0)
        self._stop_archiver()
        if self.tray is not None:
            self.tray.hide()
        self._save_settings()
        logger.info("界面关闭")
        event.accept()


def _qt_version() -> str:
    from PyQt5.QtCore import QT_VERSION_STR
    return QT_VERSION_STR


def _wgc_available() -> bool:
    from . import capture_wgc as wgc
    return wgc.available()


def launch() -> int:
    """启动 Qt 界面。"""
    w.enable_dpi_awareness()
    applog.setup_logging()
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("应用窗口定时截图工具")
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(launch())
