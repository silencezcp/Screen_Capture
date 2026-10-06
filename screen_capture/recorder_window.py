# -*- coding: utf-8 -*-
"""tkinter 录屏窗口：来源选择、画质调节、录制控制与实时状态。

独立成一个 Toplevel，主界面只需要调用 :func:`present`；
命令行模式不使用本模块（见 `cli.py` 的 ``--record``）。
"""
from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import win32 as w
from .engine import ConfigError, Target, TARGET_SCREEN, TARGET_WINDOW
from .recorder import quality as q
from .recorder.engine import (
    STATE_COUNTDOWN,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RECORDING,
    RecorderEngine,
    RecordingResult,
    RecordingStats,
    probe_encoder,
)

__all__ = ["present"]

_WINDOW_ROW_PREFIX = "hwnd:"
_COUNTDOWN_CHOICES = ("不等待，立即开始", "1 秒", "2 秒", "3 秒（推荐）", "5 秒", "10 秒")
_COUNTDOWN_VALUES = (0.0, 1.0, 2.0, 3.0, 5.0, 10.0)


class RecorderWindow:
    """录屏窗口。"""

    def __init__(self, root: tk.Tk, target: Optional[Target] = None,
                 output_dir: Optional[str] = None):
        self.root = root
        self.engine: Optional[RecorderEngine] = None
        self.events: "queue.Queue[dict]" = queue.Queue()
        self._result: Optional[RecordingResult] = None
        self._closed = False
        self._after_ids: list[str] = []
        self._windows: list[w.WindowInfo] = []

        self.config = q.load_record_config()
        if output_dir:
            self.config.output_dir = output_dir
        if target is not None and (target.kind == TARGET_SCREEN or target.hwnd):
            self.config.target = target

        self.window = tk.Toplevel(root)
        self.window.title("屏幕录制（可调画质）")
        self.window.geometry("880x760")
        self.window.minsize(820, 680)
        self.window.transient(root)

        self._build()
        self._load_config_into_ui()
        self.refresh_windows()
        self._drain_events()
        self._tick()

        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.grab_set()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        body = ttk.Frame(outer)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1, minsize=420)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # ---- 1. 录制来源 ----
        source = ttk.LabelFrame(body, text=" 1. 录制来源 ", padding=8)
        source.grid(row=0, column=0, sticky="new")
        source.columnconfigure(0, weight=1)

        bar = ttk.Frame(source)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="筛选：").grid(row=0, column=0)
        self.filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.filter_var).grid(row=0, column=1, sticky="ew", padx=4)
        self.filter_var.trace_add("write", lambda *_: self._fill_window_list())
        ttk.Button(bar, text="刷新", command=self.refresh_windows).grid(row=0, column=2)

        self.source_var = tk.StringVar(value=_WINDOW_ROW_PREFIX + "0")
        list_box = ttk.Frame(source)
        list_box.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        list_box.columnconfigure(0, weight=1)
        self.window_list = tk.Listbox(list_box, height=7, exportselection=False)
        self.window_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_box, orient="vertical", command=self.window_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.window_list.configure(yscrollcommand=scroll.set)
        self.window_list.bind("<<ListboxSelect>>", lambda _e: self._on_source_changed())

        self.source_hint = ttk.Label(source, text="", foreground="#0a5", wraplength=400)
        self.source_hint.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # ---- 2. 画质 ----
        picture = ttk.LabelFrame(body, text=" 2. 画质（可随时调整，录制中改会自动分段） ", padding=8)
        picture.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(10, 0))
        picture.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(picture, text="画质预设").grid(row=row, column=0, sticky="w", pady=3)
        self.preset_var = tk.StringVar(value="自定义")
        self.preset_box = ttk.Combobox(picture, textvariable=self.preset_var, state="readonly",
                                       values=["自定义"] + [p.label for p in q.QUALITY_PRESETS])
        self.preset_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.preset_box.bind("<<ComboboxSelected>>", lambda _e: self._on_preset())
        row += 1

        ttk.Label(picture, text="分辨率").grid(row=row, column=0, sticky="w", pady=3)
        self.resolution_var = tk.StringVar(value=q.RESOLUTION_LABELS["1080p"])
        self.resolution_box = ttk.Combobox(
            picture, textvariable=self.resolution_var, state="readonly",
            values=[label for _n, _h, label in q.RESOLUTION_PRESETS])
        self.resolution_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.resolution_box.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_changed())
        row += 1

        ttk.Label(picture, text="帧率").grid(row=row, column=0, sticky="w", pady=3)
        fps_box = ttk.Frame(picture)
        fps_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.fps_var = tk.StringVar(value="30")
        fps_values = [str(v) for v in q.FPS_CHOICES]
        self.fps_box = ttk.Combobox(fps_box, textvariable=self.fps_var, state="readonly",
                                    values=fps_values, width=6)
        self.fps_box.pack(side="left")
        self.fps_box.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_changed())
        ttk.Label(fps_box, text=" fps").pack(side="left")
        row += 1

        ttk.Label(picture, text="清晰度").grid(row=row, column=0, sticky="w", pady=3)
        self.quality_var = tk.StringVar(value=q.QUALITY_LABELS["high"])
        self.quality_box = ttk.Combobox(
            picture, textvariable=self.quality_var, state="readonly",
            values=[q.QUALITY_LABELS[k] for k in q.QUALITY_LEVELS])
        self.quality_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.quality_box.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_changed())
        row += 1

        ttk.Label(picture, text="码率（kbps）").grid(row=row, column=0, sticky="w", pady=3)
        rate_box = ttk.Frame(picture)
        rate_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.auto_bitrate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(rate_box, text="自动", variable=self.auto_bitrate_var,
                        command=self._on_quality_changed).pack(side="left")
        self.bitrate_var = tk.StringVar(value="0")
        self.bitrate_spin = ttk.Spinbox(rate_box, from_=100, to=200000, increment=500,
                                        textvariable=self.bitrate_var, width=8)
        self.bitrate_spin.pack(side="left", padx=(6, 0))
        self.bitrate_var.trace_add("write", lambda *_: self._on_bitrate_edited())
        row += 1

        ttk.Label(picture, text="H.264 档位").grid(row=row, column=0, sticky="w", pady=3)
        self.profile_var = tk.StringVar(value=q.PROFILE_CHOICES[0][1])
        profile_box = ttk.Combobox(picture, textvariable=self.profile_var, state="readonly",
                                   values=[label for _k, label in q.PROFILE_CHOICES])
        profile_box.grid(row=row, column=1, sticky="ew", pady=3)
        profile_box.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_changed())
        row += 1

        ttk.Label(picture, text="色彩转换").grid(row=row, column=0, sticky="w", pady=3)
        self.input_var = tk.StringVar(value=q.INPUT_LABELS["auto"])
        input_box = ttk.Combobox(picture, textvariable=self.input_var, state="readonly",
                                 values=[q.INPUT_LABELS["auto"]])
        input_box.grid(row=row, column=1, sticky="ew", pady=3)
        input_box.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_changed())
        row += 1

        ttk.Label(picture, text="取帧方式").grid(row=row, column=0, sticky="w", pady=3)
        from .recorder.capture import SCALE_CHOICES, SCALE_LABELS
        self.scale_var = tk.StringVar(value=SCALE_LABELS["quality"])
        scale_box = ttk.Combobox(picture, textvariable=self.scale_var, state="readonly",
                                 values=[SCALE_LABELS[k] for k in SCALE_CHOICES])
        scale_box.grid(row=row, column=1, sticky="ew", pady=3)
        scale_box.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_changed())
        row += 1

        self.estimate_var = tk.StringVar(value="")
        ttk.Label(picture, textvariable=self.estimate_var, foreground="#036",
                  wraplength=380).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        row += 1
        ttk.Label(picture,
                  text="取帧上限（本机实测）：高画质 720p≈29fps / 1080p≈19fps；"
                       "高帧率 720p≈45fps / 1080p≈29fps",
                  foreground="#888", wraplength=380).grid(row=row, column=0, columnspan=2,
                                                          sticky="w")
        row += 1

        # ---- 3. 保存与选项 ----
        save = ttk.LabelFrame(body, text=" 3. 保存与选项 ", padding=8)
        save.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        save.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(save, text="保存目录").grid(row=row, column=0, sticky="w", pady=3)
        out_box = ttk.Frame(save)
        out_box.grid(row=row, column=1, sticky="ew", pady=3)
        out_box.columnconfigure(0, weight=1)
        self.output_var = tk.StringVar(value=self.config.output_dir)
        ttk.Entry(out_box, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_box, text="浏览…", command=self._choose_dir).grid(row=0, column=1, padx=(4, 0))
        row += 1

        ttk.Label(save, text="文件名模板").grid(row=row, column=0, sticky="w", pady=3)
        self.pattern_var = tk.StringVar(value=self.config.filename_pattern)
        ttk.Entry(save, textvariable=self.pattern_var).grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        ttk.Label(save, text="占位符：{app} {date} {time} {datetime} {resolution} {fps}",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Label(save, text="开始倒计时").grid(row=row, column=0, sticky="w", pady=3)
        self.countdown_var = tk.StringVar(value=_COUNTDOWN_CHOICES[3])
        ttk.Combobox(save, textvariable=self.countdown_var, state="readonly",
                     values=list(_COUNTDOWN_CHOICES)).grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Label(save, text="最长录制（秒，0=不限）").grid(row=row, column=0, sticky="w", pady=3)
        self.duration_var = tk.StringVar(value="0")
        ttk.Entry(save, textvariable=self.duration_var, width=10).grid(row=row, column=1,
                                                                      sticky="w", pady=3)
        row += 1

        self.cursor_var = tk.BooleanVar(value=True)
        self.hide_var = tk.BooleanVar(value=True)
        checks = ttk.Frame(save)
        checks.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(checks, text="录制鼠标光标", variable=self.cursor_var).pack(anchor="w")
        ttk.Checkbutton(checks, text="开始录制后最小化本窗口", variable=self.hide_var).pack(anchor="w")

        # ---- 底部：控制 + 状态 ----
        bottom = ttk.Frame(outer)
        bottom.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(3, weight=1)
        self.start_button = ttk.Button(bottom, text="● 开始录制", command=self._on_start)
        self.start_button.grid(row=0, column=0)
        self.pause_button = ttk.Button(bottom, text="‖ 暂停", command=self._on_pause, state="disabled")
        self.pause_button.grid(row=0, column=1, padx=6)
        self.stop_button = ttk.Button(bottom, text="■ 停止", command=self._on_stop, state="disabled")
        self.stop_button.grid(row=0, column=2)
        ttk.Button(bottom, text="打开目录", command=self._open_dir).grid(row=0, column=4, padx=6)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(6, 4)).grid(row=0, column=3, sticky="ew", padx=(10, 0))

        # ---- 状态面板 + 日志 ----
        info = ttk.LabelFrame(outer, text=" 录制状态 ", padding=8)
        info.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for index in range(4):
            info.columnconfigure(index, weight=1)
        self.stat_vars = {}
        labels = (("elapsed", "已录制"), ("fps", "实际帧率"), ("size", "文件大小"),
                  ("out", "输出画面"), ("bitrate", "码率"), ("segment", "分段"),
                  ("input", "色彩输入"), ("file", "当前文件"))
        for index, (key, text) in enumerate(labels):
            box = ttk.Frame(info)
            box.grid(row=index // 4, column=index % 4, sticky="ew", pady=3, padx=4)
            ttk.Label(box, text=text, foreground="#666").pack(anchor="w")
            var = tk.StringVar(value="—")
            ttk.Label(box, textvariable=var, font=("", 11, "bold")).pack(anchor="w")
            self.stat_vars[key] = var
        self.countdown_label = ttk.Label(info, text="", foreground="#036", font=("", 16, "bold"))
        self.countdown_label.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        log_frame = ttk.LabelFrame(outer, text=" 日志 ", padding=6)
        log_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=6, wrap="none", state="disabled",
                                background="#111", foreground="#ddd")
        self.log_text.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    # ------------------------------------------------------------------
    # 配置 <-> 界面
    # ------------------------------------------------------------------
    def _load_config_into_ui(self) -> None:
        config = self.config
        self.resolution_var.set(q.RESOLUTION_LABELS.get(config.resolution, q.RESOLUTION_LABELS["1080p"]))
        self.fps_var.set(str(config.fps if config.fps in q.FPS_CHOICES else 30))
        self.quality_var.set(q.QUALITY_LABELS.get(config.quality, q.QUALITY_LABELS["high"]))
        self.auto_bitrate_var.set(config.bitrate_kbps <= 0)
        if config.bitrate_kbps > 0:
            self.bitrate_var.set(str(config.bitrate_kbps))
        self.profile_var.set(dict((k, label) for k, label in q.PROFILE_CHOICES)
                             .get(config.profile, q.PROFILE_CHOICES[0][1]))
        self.input_var.set(q.INPUT_LABELS.get(config.input_format, q.INPUT_LABELS["auto"]))
        from .recorder.capture import SCALE_LABELS
        self.scale_var.set(SCALE_LABELS.get(config.scale_mode, SCALE_LABELS["quality"]))
        self.pattern_var.set(config.filename_pattern)
        self.output_var.set(config.output_dir)
        self.cursor_var.set(config.capture_cursor)
        self.hide_var.set(config.hide_window)
        if config.start_delay in _COUNTDOWN_VALUES:
            self.countdown_var.set(_COUNTDOWN_CHOICES[_COUNTDOWN_VALUES.index(config.start_delay)])
        if config.max_duration > 0:
            self.duration_var.set(str(config.max_duration))
        self._sync_bitrate_spin()
        self._update_estimate()

    def _collect_config(self) -> q.RecordingConfig:
        """把界面上的值收集成一个 RecordingConfig。"""
        target = self._selected_target()
        if target is None:
            raise ConfigError("请先在左侧选择要录制的窗口或「整个屏幕」")
        config = q.RecordingConfig(
            target=target,
            output_dir=self.output_var.get().strip(),
            resolution=self._resolution_key(),
            fps=int(self.fps_var.get() or 30),
            quality=self._quality_key(),
            bitrate_kbps=0 if self.auto_bitrate_var.get() else self._bitrate_value(),
            profile=self._profile_key(),
            input_format=self._input_key(),
            scale_mode=self._scale_key(),
            filename_pattern=self.pattern_var.get().strip() or q.DEFAULT_RECORD_PATTERN,
            start_delay=self._countdown_value(),
            max_duration=self._duration_value(),
            capture_cursor=bool(self.cursor_var.get()),
            hide_window=bool(self.hide_var.get()),
        )
        config.validate()
        return config

    def _resolution_key(self) -> str:
        label = self.resolution_var.get()
        for name, _height, text in q.RESOLUTION_PRESETS:
            if text == label:
                return name
        return "native"

    def _quality_key(self) -> str:
        label = self.quality_var.get()
        for key, text in q.QUALITY_LABELS.items():
            if text == label:
                return key
        return "high"

    def _profile_key(self) -> str:
        label = self.profile_var.get()
        for key, text in q.PROFILE_CHOICES:
            if text == label:
                return key
        return "high"

    def _input_key(self) -> str:
        label = self.input_var.get()
        for key, text in q.INPUT_LABELS.items():
            if text == label:
                return key
        return "auto"

    def _scale_key(self) -> str:
        from .recorder.capture import SCALE_LABELS
        label = self.scale_var.get()
        for key, text in SCALE_LABELS.items():
            if text == label:
                return key
        return "quality"

    def _countdown_value(self) -> float:
        label = self.countdown_var.get()
        if label in _COUNTDOWN_CHOICES:
            return _COUNTDOWN_VALUES[_COUNTDOWN_CHOICES.index(label)]
        return 3.0

    def _duration_value(self) -> float:
        try:
            return max(0.0, float(self.duration_var.get().strip() or "0"))
        except ValueError:
            return 0.0

    def _bitrate_value(self) -> int:
        try:
            return max(0, int(float(self.bitrate_var.get().strip() or "0")))
        except ValueError:
            return 0

    def _sync_bitrate_spin(self) -> None:
        auto = self.auto_bitrate_var.get()
        self.bitrate_spin.configure(state="disabled" if auto else "normal")

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _on_preset(self) -> None:
        label = self.preset_var.get()
        for preset in q.QUALITY_PRESETS:
            if preset.label == label:
                self.resolution_var.set(q.RESOLUTION_LABELS[preset.resolution])
                self.fps_var.set(str(preset.fps))
                self.quality_var.set(q.QUALITY_LABELS[preset.quality])
                self.auto_bitrate_var.set(preset.bitrate_kbps <= 0)
                if preset.bitrate_kbps > 0:
                    self.bitrate_var.set(str(preset.bitrate_kbps))
                self._sync_bitrate_spin()
                break
        self._on_quality_changed()

    def _on_quality_changed(self) -> None:
        if not self._is_custom():
            self.preset_var.set("自定义")
        self._update_estimate()
        self._push_config()

    def _on_bitrate_edited(self) -> None:
        if self.auto_bitrate_var.get():
            return
        self._on_quality_changed()

    def _is_custom(self) -> bool:
        label = self.preset_var.get()
        for preset in q.QUALITY_PRESETS:
            if preset.label != label:
                continue
            return not (preset.resolution == self._resolution_key()
                        and preset.fps == int(self.fps_var.get() or 0)
                        and preset.quality == self._quality_key())
        return True

    def _push_config(self) -> None:
        """把当前界面参数推给正在运行的引擎（录制中即时生效）。"""
        if self.engine is None or not self.engine.running:
            return
        try:
            self.engine.apply_config(self._collect_config())
        except ConfigError:
            pass

    def _update_estimate(self) -> None:
        rect = self._source_rect()
        if rect is None:
            self.estimate_var.set("请选择录制目标以估算输出画质")
            return
        try:
            config = self._collect_config()
        except ConfigError:
            # 目标还没选好时用临时配置估算
            config = q.RecordingConfig()
            config.resolution = self._resolution_key()
            config.fps = int(self.fps_var.get() or 30)
            config.quality = self._quality_key()
            config.bitrate_kbps = 0 if self.auto_bitrate_var.get() else self._bitrate_value()
        width, height = config.output_size(rect[2] - rect[0], rect[3] - rect[1])
        rate = config.bitrate_for(width, height)
        per_minute = rate * 60 / 8 / 1024
        self.estimate_var.set(
            f"预计输出 {width}x{height} @{config.fps}fps，码率约 {rate} kbps"
            f"（≈ {per_minute:.0f} MB/分钟）")

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
    # 窗口列表
    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        try:
            self._windows = w.enum_windows(include_own=False)
        except Exception as exc:
            self._log(f"枚举窗口失败：{exc}")
            self._windows = []
        self._fill_window_list()

    def _fill_window_list(self) -> None:
        keyword = self.filter_var.get().strip().lower()
        current = self._selected_target()
        self.window_list.delete(0, "end")
        self._row_targets: list[Optional[Target]] = []

        vx1, vy1, vx2, vy2 = w.get_virtual_screen_rect()
        self.window_list.insert("end", f"【整个屏幕（所有显示器）】 {vx2 - vx1}x{vy2 - vy1}")
        self._row_targets.append(Target(kind=TARGET_SCREEN))

        for info in self._windows:
            if keyword and keyword not in info.title.lower() and keyword not in info.process_name.lower():
                continue
            title = info.title if len(info.title) <= 52 else info.title[:51] + "…"
            if info.minimized:
                title += "（最小化）"
            self.window_list.insert("end", f"{title}  [{info.process_name}] {info.size_text}")
            self._row_targets.append(Target(kind=TARGET_WINDOW, hwnd=info.hwnd, title=info.title,
                                            app_label=info.process_name or info.title))

        index = 0
        if current is not None:
            for position, target in enumerate(self._row_targets):
                if target is None:
                    continue
                if target.kind == current.kind and (target.kind == TARGET_SCREEN or target.hwnd == current.hwnd):
                    index = position
                    break
        if self.window_list.size():
            self.window_list.selection_clear(0, "end")
            self.window_list.selection_set(index)
            self.window_list.see(index)
        self._on_source_changed()

    def _selected_target(self) -> Optional[Target]:
        selection = self.window_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        targets = getattr(self, "_row_targets", [])
        if 0 <= index < len(targets):
            return targets[index]
        return None

    def _on_source_changed(self) -> None:
        target = self._selected_target()
        if target is None:
            self.source_hint.configure(text="尚未选择目标")
        elif target.kind == TARGET_SCREEN:
            self.source_hint.configure(text="当前目标：整个屏幕（所有显示器）")
        else:
            self.source_hint.configure(
                text=f"当前目标：{target.title or hex(target.hwnd)}（{target.app_label}）")
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
            messagebox.showwarning("参数有误", str(exc), parent=self.window)
            return

        ok, message = probe_encoder()
        if not ok:
            messagebox.showerror("无法开始录制", message, parent=self.window)
            self._log(f"编码器检测失败：{message}")
            return
        self._log(f"编码器检测通过：{message}")

        config.output_dir = str(Path(config.output_dir))
        try:
            Path(config.output_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("无法创建目录", f"{config.output_dir}\n{exc}", parent=self.window)
            return

        self.config = config
        q.save_record_config(config)
        self.engine = RecorderEngine(config, on_event=self.events.put)
        try:
            self.engine.start(countdown=config.start_delay > 0)
        except ConfigError as exc:
            messagebox.showwarning("无法开始录制", str(exc), parent=self.window)
            return
        self._log(f"开始录制：{config.target.describe()}")
        self._log(f"画质：{config.describe()}")
        self._set_buttons(recording=True)
        if config.hide_window and config.start_delay > 0:
            try:
                self.window.iconify()
            except tk.TclError:
                pass

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
        self._set_buttons(recording=False, stopping=True)
        self.engine.stop()

    def _set_buttons(self, recording: bool, stopping: bool = False) -> None:
        self.start_button.configure(state="disabled" if (recording or stopping) else "normal")
        self.stop_button.configure(state="normal" if recording and not stopping else "disabled")
        state = self.engine.state if self.engine is not None else STATE_IDLE
        self.pause_button.configure(
            state="normal" if state in (STATE_RECORDING, STATE_PAUSED) else "disabled")
        self.pause_button.configure(text="▶ 继续" if state == STATE_PAUSED else "‖ 暂停")

    # ------------------------------------------------------------------
    # 事件轮询与刷新
    # ------------------------------------------------------------------
    def _drain_events(self) -> None:
        if self._closed:
            return
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self._after_ids.append(self.window.after(120, self._drain_events))

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "started":
            self._log(f"目标：{event.get('target')}")
            self._log(f"参数：{event.get('quality')}")
            self.status_var.set("准备中…")
        elif kind == "countdown":
            self.countdown_label.configure(text=f"{event.get('remaining')} 秒后开始录制…")
        elif kind == "recording":
            self.countdown_label.configure(text="")
            self.status_var.set("正在录制")
            self._set_buttons(recording=True)
        elif kind == "paused":
            self.status_var.set("已暂停")
            self._set_buttons(recording=True)
        elif kind == "resumed":
            self.status_var.set("正在录制")
            self._set_buttons(recording=True)
        elif kind == "file":
            self._log(f"写入：{event.get('path')}")
        elif kind == "quality":
            self._log(f"画质：{event.get('text')}（输入 {event.get('input_format')}）")
        elif kind == "error":
            self._log(f"[错误] {event.get('message')}")
            self.status_var.set("出错")
        elif kind == "finished":
            self._result = event.get("result")
            self.status_var.set("已结束")
            self.countdown_label.configure(text="")
            self._set_buttons(recording=False)
            self._log(event.get("summary", "录制结束"))
            self._show_result(self._result)

    def _show_result(self, result: Optional[RecordingResult]) -> None:
        if result is None:
            return
        if not result.files:
            messagebox.showinfo("录制结束", result.summary(), parent=self.window)
            return
        lines = [result.summary(), "", "输出文件："] + [str(p) for p in result.files]
        answer = messagebox.askyesno("录制完成", "\n".join(lines) + "\n\n是否打开保存目录？",
                                     parent=self.window)
        if answer:
            self._open_dir()

    def _tick(self) -> None:
        if self._closed:
            return
        engine = self.engine
        if engine is not None and engine.running:
            stats: RecordingStats = engine.snapshot()
            self.stat_vars["elapsed"].set(stats.elapsed_text)
            self.stat_vars["fps"].set(f"{stats.actual_fps:.1f} fps"
                                      + (f"（丢 {stats.dropped}）" if stats.dropped else ""))
            self.stat_vars["size"].set(stats.size_text)
            self.stat_vars["out"].set(f"{stats.output_width}x{stats.output_height}")
            self.stat_vars["bitrate"].set(f"{stats.average_bitrate:.0f} kbps / 设定 {stats.bitrate_kbps}")
            self.stat_vars["segment"].set(f"第 {stats.segment} 段")
            self.stat_vars["input"].set(stats.input_format or "—")
            self.stat_vars["file"].set(Path(stats.current_file).name if stats.current_file else "—")
            self._set_buttons(recording=True)
        self._after_ids.append(self.window.after(250, self._tick))

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------
    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择录像保存目录",
                                         initialdir=self.output_var.get() or str(Path.home()),
                                         parent=self.window)
        if chosen:
            self.output_var.set(chosen)
            self._push_config()

    def _open_dir(self) -> None:
        path = Path(self.output_var.get().strip() or ".")
        try:
            path.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", f"{path}\n{exc}", parent=self.window)

    def _log(self, message: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{_now()}] {message}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        engine = self.engine
        if engine is not None and engine.running:
            if not messagebox.askyesno("正在录制", "还在录制中，确定要停止并退出吗？\n（已录制部分会正常保存）",
                                       parent=self.window):
                return
            engine.stop()
            engine.join(15.0)
        try:
            q.save_record_config(self.config)
        except Exception:
            pass
        self._closed = True
        for after_id in self._after_ids:
            try:
                self.window.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids = []
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def present(root: tk.Tk, target: Optional[Target] = None,
            output_dir: Optional[str] = None) -> RecorderWindow:
    """打开录屏窗口。"""
    window = RecorderWindow(root, target=target, output_dir=output_dir)
    try:
        window.window.focus_force()
    except tk.TclError:
        pass
    return window
