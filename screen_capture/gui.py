# -*- coding: utf-8 -*-
"""tkinter 图形界面：选择应用窗口、手动配置间隔时间、开始 / 停止定时截图。"""
from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import win32 as w
from .engine import (
    CaptureConfig,
    CaptureEngine,
    ConfigError,
    Target,
    TARGET_SCREEN,
    TARGET_WINDOW,
    unknown_placeholders,
)

SCREEN_ROW = "__screen__"
METHOD_CHOICES = [
    ("自动（优先 PrintWindow，失败自动回退）", w.METHOD_AUTO),
    ("PrintWindow（被遮挡也能截）", w.METHOD_PRINTWINDOW),
    ("屏幕区域（所见即所得）", w.METHOD_SCREEN),
]
FORMAT_CHOICES = ["png", "jpg", "bmp", "webp"]
QUICK_INTERVALS = ["1", "2", "5", "10", "30", "60"]


class _Cancelled(Exception):
    """用户在确认框中取消了本次启动。"""


def default_output_dir() -> Path:
    """默认保存到「程序目录\\ScreenCapture」（与 Qt 界面一致）。"""
    from . import paths

    return paths.default_capture_dir()


class CaptureApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.engine: CaptureEngine | None = None
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.window_list: list[w.WindowInfo] = []
        self.preview_photo: ImageTk.PhotoImage | None = None
        self._next_at: float | None = None
        self._counts = {"saved": 0, "skipped": 0, "attempts": 0}
        self._started_at: float | None = None
        self._closed = False
        self._after_ids: list[str] = []

        root.title("应用窗口定时截图工具")
        root.geometry("1020x780")
        root.minsize(940, 680)

        self._setup_style()
        self._build_ui()
        self.refresh_windows()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_ids.append(self.root.after(120, self._drain_events))
        self._after_ids.append(self.root.after(200, self._tick))

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    def _setup_style(self) -> None:
        try:
            dpi = w.system_dpi()
            self.root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except Exception:
            pass
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        base_font = tkfont.nametofont("TkDefaultFont")
        line = base_font.metrics("linespace")
        style.configure("Treeview", rowheight=int(line * 1.6))
        style.configure("Title.TLabel", font=(base_font.cget("family"),
                                              base_font.cget("size"), "bold"))
        style.configure("Status.TLabel", padding=(6, 4))

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=10)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=0, minsize=430)
        main.rowconfigure(0, weight=1)
        main.rowconfigure(2, weight=0)

        # ---- 左：目标窗口 ------------------------------------------------
        left = ttk.LabelFrame(main, text=" 1. 选择要截图的应用窗口 ", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="筛选：").grid(row=0, column=0, sticky="w")
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(bar, textvariable=self.filter_var)
        self.filter_entry.grid(row=0, column=1, sticky="ew", padx=4)
        self.filter_var.trace_add("write", lambda *_: self._fill_tree())
        ttk.Button(bar, text="刷新列表", command=self.refresh_windows).grid(row=0, column=2)

        opts = ttk.Frame(left)
        opts.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        self.show_own_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="显示本程序自身的窗口", variable=self.show_own_var,
                        command=self.refresh_windows).grid(row=0, column=0, sticky="w")
        self.show_min_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="显示已最小化的窗口", variable=self.show_min_var,
                        command=self._fill_tree).grid(row=0, column=1, sticky="w", padx=(12, 0))

        columns = ("title", "process", "size", "hwnd")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("title", text="窗口标题")
        self.tree.heading("process", text="程序")
        self.tree.heading("size", text="尺寸")
        self.tree.heading("hwnd", text="句柄")
        self.tree.column("title", width=250, anchor="w")
        self.tree.column("process", width=130, anchor="w")
        self.tree.column("size", width=90, anchor="center")
        self.tree.column("hwnd", width=90, anchor="center")
        self.tree.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Double-1>", lambda _e: self._on_start())

        self.selection_var = tk.StringVar(value="尚未选择目标")
        ttk.Label(left, textvariable=self.selection_var, style="Status.TLabel",
                  foreground="#0a5").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ---- 右：设置 ----------------------------------------------------
        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        settings = ttk.LabelFrame(right, text=" 2. 截图设置 ", padding=8)
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        row = 0

        # 间隔时间 + 快捷按钮
        ttk.Label(settings, text="截图间隔（秒）").grid(row=row, column=0, sticky="w", pady=3)
        self.interval_var = tk.StringVar(value="5")
        interval_box = ttk.Frame(settings)
        interval_box.grid(row=row, column=1, sticky="ew", pady=3)
        interval_box.columnconfigure(0, weight=1)
        ttk.Entry(interval_box, textvariable=self.interval_var, width=10).grid(row=0, column=0, sticky="w")
        quick = ttk.Frame(interval_box)
        quick.grid(row=1, column=0, sticky="w", pady=(3, 0))
        for value in QUICK_INTERVALS:
            ttk.Button(quick, text=f"{value}s", width=4,
                       command=lambda v=value: self.interval_var.set(v)).pack(side="left", padx=1)
        row += 1

        self.delay_var = tk.StringVar(value="0")
        self._entry_row(settings, row, "首张延迟（秒）", self.delay_var); row += 1

        self.count_var = tk.StringVar(value="0")
        self._entry_row(settings, row, "截图数量上限（0=不限）", self.count_var); row += 1

        self.duration_var = tk.StringVar(value="0")
        self._entry_row(settings, row, "最长运行时长秒（0=不限）", self.duration_var); row += 1

        ttk.Label(settings, text="截图方式").grid(row=row, column=0, sticky="w", pady=3)
        self.method_var = tk.StringVar(value=METHOD_CHOICES[0][0])
        ttk.Combobox(settings, textvariable=self.method_var, state="readonly",
                     values=[c[0] for c in METHOD_CHOICES]).grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Label(settings, text="图片格式").grid(row=row, column=0, sticky="w", pady=3)
        fmt_box = ttk.Frame(settings)
        fmt_box.grid(row=row, column=1, sticky="ew", pady=3)
        self.format_var = tk.StringVar(value="png")
        ttk.Combobox(fmt_box, textvariable=self.format_var, state="readonly", width=7,
                     values=FORMAT_CHOICES).pack(side="left")
        ttk.Label(fmt_box, text=" 质量(1-100)").pack(side="left")
        self.quality_var = tk.StringVar(value="90")
        ttk.Spinbox(fmt_box, from_=1, to=100, textvariable=self.quality_var, width=5).pack(side="left")
        row += 1

        self.pattern_var = tk.StringVar(value="{app}_{date}_{time}_{index:04d}")
        ttk.Label(settings, text="文件名模板").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(settings, textvariable=self.pattern_var).grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        ttk.Label(settings, text="可用占位符：{app} {date} {time} {datetime} {index} {hwnd}",
                  foreground="#666").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Label(settings, text="保存目录").grid(row=row, column=0, sticky="w", pady=3)
        out_box = ttk.Frame(settings)
        out_box.grid(row=row, column=1, sticky="ew", pady=3)
        out_box.columnconfigure(0, weight=1)
        self.output_var = tk.StringVar(value=str(default_output_dir()))
        ttk.Entry(out_box, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_box, text="浏览…", command=self._choose_dir).grid(row=0, column=1, padx=(4, 0))
        row += 1

        self.skip_unchanged_var = tk.BooleanVar(value=False)
        self.session_subdir_var = tk.BooleanVar(value=True)
        self.manifest_var = tk.BooleanVar(value=True)
        checks = ttk.Frame(settings)
        checks.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(checks, text="画面无变化时跳过保存", variable=self.skip_unchanged_var).pack(anchor="w")
        ttk.Checkbutton(checks, text="每次开始新建时间戳子目录（不勾选则按应用复用）", variable=self.session_subdir_var).pack(anchor="w")
        ttk.Checkbutton(checks, text="生成截图清单 capture_manifest.csv",
                        variable=self.manifest_var).pack(anchor="w")

        # ---- 右：预览 ----------------------------------------------------
        preview = ttk.LabelFrame(right, text=" 3. 最新截图预览 ", padding=8)
        preview.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        right.rowconfigure(1, weight=1)
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)
        self.preview_label = ttk.Label(preview, text="（还没有截图）", anchor="center",
                                       background="#1e1e1e", foreground="#bbb")
        self.preview_label.grid(row=0, column=0, sticky="nsew")
        self.preview_caption = ttk.Label(preview, text="", foreground="#666", wraplength=380)
        self.preview_caption.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # ---- 底部按钮 ----------------------------------------------------
        bottom = ttk.Frame(main)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(4, weight=1)
        self.start_button = ttk.Button(bottom, text="▶ 开始截图", command=self._on_start)
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(bottom, text="■ 停止", command=self._on_stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=6)
        self.test_button = ttk.Button(bottom, text="试截一张", command=self._on_test_shot)
        self.test_button.grid(row=0, column=2)
        ttk.Button(bottom, text="● 录屏…", command=self._on_record).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(bottom, text="打开保存目录", command=self._open_output_dir).grid(row=0, column=4, padx=6)
        self.status_var = tk.StringVar(value="就绪：请选择目标窗口并设置间隔时间")
        ttk.Label(bottom, textvariable=self.status_var, style="Status.TLabel",
                  relief="sunken", anchor="w").grid(row=0, column=5, sticky="ew", padx=(10, 0))

        # ---- 日志 --------------------------------------------------------
        log_frame = ttk.LabelFrame(main, text=" 运行日志 ", padding=6)
        log_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=8, wrap="none", state="disabled",
                                background="#111", foreground="#ddd", insertbackground="#ddd")
        self.log_text.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self._log("提示：先选择左侧窗口（或双击列表），设置好间隔时间后点击「开始截图」。")

    def _entry_row(self, parent, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var, width=12).grid(row=row, column=1, sticky="w", pady=3)

    # ------------------------------------------------------------------
    # 目标窗口列表
    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        selected = self._selected_iid()
        try:
            infos = w.enum_windows(include_own=self.show_own_var.get())
        except Exception as exc:
            self._log(f"枚举窗口失败：{exc}")
            return
        self.window_list = infos
        self._fill_tree(keep=selected)
        self._log(f"已刷新窗口列表，共 {len(infos)} 个可见窗口。")

    def _fill_tree(self, keep: str | None = None) -> None:
        keyword = self.filter_var.get().strip().lower()
        keep = keep if keep is not None else self._selected_iid()
        self.tree.delete(*self.tree.get_children())

        vw = w.get_virtual_screen_rect()
        self.tree.insert("", "end", iid=SCREEN_ROW, values=(
            "【整个屏幕（所有显示器）】", "—", f"{vw[2] - vw[0]} x {vw[3] - vw[1]}", "—",
        ))

        shown = 0
        for info in self.window_list:
            if not self.show_min_var.get() and info.minimized:
                continue
            if keyword and keyword not in info.title.lower() and keyword not in info.process_name.lower():
                continue
            title = info.title if len(info.title) <= 60 else info.title[:59] + "…"
            if info.minimized:
                title += "（最小化）"
            self.tree.insert("", "end", iid=str(info.hwnd), values=(
                title, info.process_name, info.size_text, f"0x{info.hwnd:X}",
            ))
            shown += 1
        if keep and self.tree.exists(str(keep)):
            self.tree.selection_set(str(keep))
            self.tree.see(str(keep))
        self._on_select()

    def _selected_iid(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _selected_hwnd(self) -> int | None:
        iid = self._selected_iid()
        if not iid or iid == SCREEN_ROW or not iid.isdigit():
            return None
        return int(iid)

    def _selected_target(self) -> Target | None:
        selection = self.tree.selection()
        if not selection:
            return None
        iid = selection[0]
        if iid == SCREEN_ROW:
            return Target(kind=TARGET_SCREEN)
        if not iid.isdigit():
            return None
        hwnd = int(iid)
        for info in self.window_list:
            if info.hwnd == hwnd:
                return Target(kind=TARGET_WINDOW, hwnd=hwnd, title=info.title,
                              app_label=info.process_name or info.title)
        return Target(kind=TARGET_WINDOW, hwnd=hwnd)

    def _on_select(self) -> None:
        target = self._selected_target()
        if target is None:
            self.selection_var.set("尚未选择目标")
        elif target.kind == TARGET_SCREEN:
            self.selection_var.set("当前目标：整个屏幕（所有显示器）")
        else:
            self.selection_var.set(f"当前目标：{target.title or hex(target.hwnd)}（{target.app_label}）")

    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择截图保存目录",
                                         initialdir=self.output_var.get() or str(Path.home()))
        if chosen:
            self.output_var.set(chosen)

    def _open_output_dir(self) -> None:
        path = Path(self.output_var.get())
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("无法打开目录", f"{path}\n{exc}")
                return
        try:
            import os
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", f"{path}\n{exc}")

    # ------------------------------------------------------------------
    # 开始 / 停止
    # ------------------------------------------------------------------
    def _collect_config(self) -> CaptureConfig:
        target = self._selected_target()
        if target is None:
            raise ConfigError("请先在左侧列表中选择一个应用窗口（或“整个屏幕”）")

        def number(var: tk.StringVar, name: str) -> float:
            text = var.get().strip()
            try:
                return float(text)
            except ValueError:
                raise ConfigError(f"「{name}」需要填写数字，当前是：{text or '空'}")

        interval = number(self.interval_var, "截图间隔")
        delay = number(self.delay_var, "首张延迟")
        duration = number(self.duration_var, "最长运行时长")
        count_raw = self.count_var.get().strip() or "0"
        try:
            max_shots = int(float(count_raw))
        except ValueError:
            raise ConfigError(f"「截图数量上限」需要填写整数，当前是：{count_raw}")
        try:
            quality = int(float(self.quality_var.get().strip() or "90"))
        except ValueError:
            raise ConfigError("「质量」需要填写 1~100 的整数")

        method = dict(METHOD_CHOICES).get(self.method_var.get(), w.METHOD_AUTO)
        config = CaptureConfig(
            target=target,
            output_dir=self.output_var.get().strip(),
            interval=interval,
            start_delay=delay,
            max_shots=max_shots,
            max_duration=duration,
            method=method,
            skip_unchanged=self.skip_unchanged_var.get(),
            folder_mode="session" if self.session_subdir_var.get() else "flat",
            image_format=self.format_var.get(),
            jpeg_quality=quality,
            filename_pattern=self.pattern_var.get().strip() or "{app}_{date}_{time}_{index:04d}",
            write_manifest=self.manifest_var.get(),
        )
        config.validate()

        if target.kind == TARGET_WINDOW and target.hwnd:
            info = next((i for i in self.window_list if i.hwnd == target.hwnd), None)
            if info is not None and info.minimized:
                if not messagebox.askyesno(
                    "窗口已最小化",
                    "该窗口当前处于最小化状态，截不到有效画面（最小化窗口没有可绘制内容）。\n"
                    "建议先还原窗口；也可以给「首张延迟」留几秒，点开始后再手动还原。\n\n仍要继续吗？",
                ):
                    raise _Cancelled()
        return config

    def _on_test_shot(self) -> None:
        """按当前选择立刻截一张，只做预览、不写入保存目录。"""
        target = self._selected_target()
        if target is None:
            messagebox.showwarning("还没有选择目标", "请先在左侧列表中选择一个应用窗口。")
            return
        method = dict(METHOD_CHOICES).get(self.method_var.get(), w.METHOD_AUTO)
        self.test_button.configure(state="disabled")
        self._log("正在试截一张…")

        def work() -> None:
            try:
                if target.kind == TARGET_SCREEN:
                    image = w.capture_screen_full()
                    used = w.METHOD_SCREEN
                else:
                    image, used = w.capture_window(target.hwnd, method)
                path = Path(tempfile.gettempdir()) / "screen_capture_preview.png"
                image.save(path, "PNG")
                self.events.put({
                    "type": "test-shot", "path": str(path), "filename": "试截预览（未保存到目录）",
                    "width": image.width, "height": image.height, "method": used,
                    "bytes": path.stat().st_size, "timestamp": datetime.now().strftime("%H:%M:%S"),
                })
            except Exception as exc:
                self.events.put({"type": "error", "message": f"试截失败：{exc}", "fatal": False})
            finally:
                self.events.put({"type": "test-done"})

        threading.Thread(target=work, name="test-shot", daemon=True).start()

    def _on_start(self) -> None:
        if self.engine is not None and self.engine.running:
            return
        try:
            config = self._collect_config()
        except _Cancelled:
            self._log("已取消启动。")
            return
        except ConfigError as exc:
            messagebox.showerror("无法开始截图", str(exc))
            return

        self._counts = {"saved": 0, "skipped": 0, "attempts": 0}
        self._started_at = time.monotonic()
        self._next_at = None
        self.engine = CaptureEngine(config, self.events.put)
        try:
            self.engine.start()
        except Exception as exc:
            self.engine = None
            messagebox.showerror("启动失败", str(exc))
            return

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.test_button.configure(state="disabled")
        unknown = unknown_placeholders(config.filename_pattern)
        if unknown:
            self._log("提示：文件名模板中无法识别的占位符 "
                      + "、".join("{" + name + "}" for name in unknown) + "，已改用默认命名。")
        self._log(f"开始截图：间隔 {config.interval:g} 秒，方式 {config.method}，"
                  f"输出到 {config.output_dir}")

    def _on_stop(self) -> None:
        if self.engine is not None and self.engine.running:
            self._log("正在停止…")
            self.engine.stop(timeout=0.05)
        self._set_idle_state()

    def _on_record(self) -> None:
        """打开录屏窗口（沿用当前选中的目标窗口与保存目录）。"""
        from . import recorder_window

        target = self._selected_target()
        try:
            recorder_window.present(self.root, target=target, output_dir=self.output_var.get().strip())
        except Exception as exc:  # pragma: no cover - 界面异常兜底
            messagebox.showerror("无法打开录屏窗口", str(exc))
            return
        self._log("已打开录屏窗口（可调画质；录制中修改分辨率/帧率/码率会自动分段）。")

    def _set_idle_state(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.test_button.configure(state="normal")
        self._next_at = None

    # ------------------------------------------------------------------
    # 事件循环
    # ------------------------------------------------------------------
    def _drain_events(self) -> None:
        if self._closed:
            return
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        except tk.TclError:  # 窗口正在销毁
            return
        # 重复调度不需要登记：_closed 会在退出时分发链自然终止
        self.root.after(120, self._drain_events)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "started":
            self.status_var.set(f"正在截图：{event['target']}｜间隔 {event['interval']:g} 秒"
                                f"｜方式 {event['method']}")
            self._log(f"目标：{event['target']}")
            self._log(f"保存目录：{event['output_dir']}")
        elif kind == "shot":
            self._counts["saved"] = event.get("total_saved", self._counts["saved"] + 1)
            self._counts["attempts"] = event.get("index", self._counts["attempts"])
            self._log(f"[{event['timestamp']}] 已保存第 {event['total_saved']} 张：{event['filename']} "
                      f"（{event['width']}x{event['height']}，{event['method']}，{event['took_ms']} ms）")
            self._show_preview(event)
        elif kind == "skip":
            self._counts["skipped"] = event.get("total_skipped", self._counts["skipped"] + 1)
            self._log(f"[跳过] 第 {event['index']} 次：{event['reason']}")
        elif kind == "test-shot":
            self._log(f"试截成功：{event['width']}x{event['height']}（{event['method']}）")
            self._show_preview(event)
        elif kind == "test-done":
            self.test_button.configure(state="normal")
        elif kind == "waiting":
            self._next_at = event.get("next_at")
        elif kind == "error":
            prefix = "严重错误" if event.get("fatal") else "警告"
            self._log(f"[{prefix}] {event['message']}")
            if event.get("fatal"):
                messagebox.showerror("截图失败", event["message"])
        elif kind == "finished":
            self._set_idle_state()
            summary = (f"已结束（{event['reason']}）：保存 {event.get('saved', 0)} 张，"
                       f"跳过 {event.get('skipped', 0)} 张")
            self.status_var.set(summary)
            self._log(summary)
            if event.get("manifest"):
                self._log(f"截图清单：{event['manifest']}")
            self._log(f"保存目录：{event.get('output_dir', '')}")

    def _tick(self) -> None:
        if self._closed:
            return
        if self.engine is not None and self.engine.running:
            remaining = ""
            if self._next_at is not None:
                left = max(0.0, self._next_at - time.monotonic())
                remaining = f"｜下次截图 {left:.1f} 秒后"
            elapsed = time.monotonic() - (self._started_at or time.monotonic())
            try:
                self.status_var.set(
                    f"运行中 {elapsed:,.0f} 秒｜保存 {self._counts['saved']} 张"
                    f"｜跳过 {self._counts['skipped']} 张{remaining}"
                )
            except tk.TclError:
                return
        self.root.after(200, self._tick)

    # ------------------------------------------------------------------
    # 预览与日志
    # ------------------------------------------------------------------
    def _show_preview(self, event: dict) -> None:
        try:
            with Image.open(event["path"]) as image:
                image.load()
                thumb = image.copy()
            thumb.thumbnail((400, 240), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(thumb)
            self.preview_label.configure(image=self.preview_photo, text="")
            size_kb = event.get("bytes", 0) / 1024
            self.preview_caption.configure(
                text=f"{event['filename']}　{event['width']}x{event['height']}　"
                     f"{size_kb:.1f} KB　{event['method']}"
            )
        except Exception as exc:
            self._log(f"预览失败：{exc}")

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {message}\n")
        # 控制日志长度，避免长时间运行内存膨胀
        if int(self.log_text.index("end-1c").split(".")[0]) > 800:
            self.log_text.delete("1.0", "200.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.engine is not None and self.engine.running:
            if not messagebox.askyesno("仍在截图", "截图任务还在运行，确定要退出吗？"):
                return
            self.engine.stop(timeout=1.0)
        self._closed = True
        for after_id in self._after_ids:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()
        self.root.destroy()


def launch() -> int:
    w.enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无桌面环境
        print(f"无法创建图形界面：{exc}", file=sys.stderr)
        return 1
    CaptureApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(launch())
