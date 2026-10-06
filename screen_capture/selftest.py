# -*- coding: utf-8 -*-
"""自检：确认窗口枚举、截图、录屏编码、tkinter / Pillow 界面组件在当前环境可用。

打包成 exe 之后最需要验证的就是这些运行库是否被正确打进去，
所以在源码和 exe 里都可以运行：

    python run.py --selftest            # 报告写到系统临时目录
    python run.py --selftest D:\\out     # 报告写到指定目录

全部通过返回 0，任意一项失败返回 1。
"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import capture_wgc as wgc
from . import win32 as w


def _safe_print(text: str) -> None:
    """打包成 windowed exe 时没有控制台，stdout 可能是 None，这里静默跳过。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(text, flush=True)
    except Exception:
        pass


def run_selftest(output_dir: Optional[str] = None) -> int:
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, func) -> None:
        try:
            checks.append((name, True, str(func())))
        except Exception as exc:
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    def tk_preview() -> str:
        import tkinter as tk

        from PIL import ImageTk

        root = tk.Tk()
        root.withdraw()
        try:
            image = w.capture_region((0, 0, 160, 120))
            image.thumbnail((120, 90))
            photo = ImageTk.PhotoImage(image)
            return (f"tkinter {root.tk.call('info', 'patchlevel')}，"
                    f"缩略图 {photo.width()}x{photo.height()}")
        finally:
            root.destroy()

    def window_capture() -> str:
        infos = [i for i in w.enum_windows(include_own=True) if not i.minimized]
        if not infos:
            raise RuntimeError("当前没有可用于测试的可见窗口")
        info = infos[0]
        image, method = w.capture_window(info.hwnd, method=w.METHOD_AUTO)
        return f"{info.process_name} {image.width}x{image.height}（方式：{method}）"

    def wgc_capture() -> str:
        from . import capture_wgc as wgc

        if not wgc.available():
            raise RuntimeError(wgc.unavailable_reason())
        infos = [i for i in w.enum_windows(include_own=True) if not i.minimized]
        if not infos:
            raise RuntimeError("当前没有可用于测试的可见窗口")
        info = infos[0]
        try:
            session = wgc.WgcSession(info.title, info.class_name, capture_cursor=False,
                                     hwnd=info.hwnd)
        except Exception as exc:
            advice = w.integrity_advice()
            raise RuntimeError(f"{exc}" + (f"；{advice}" if advice else ""))
        try:
            image = session.grab(1.2)
        finally:
            session.close()
        if image is None:
            raise RuntimeError("WGC 抓帧返回空（窗口可能停止了渲染）")
        return f"{info.process_name} {image.width}x{image.height}（WGC 无视遮挡）"

    def qt_check() -> str:
        from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        from PyQt6.QtWidgets import QApplication, QLabel

        app = QApplication.instance() or QApplication([])
        label = QLabel("自检")
        label.resize(80, 30)
        pixmap = label.grab()
        del app
        return f"PyQt6 {PYQT_VERSION_STR}（Qt {QT_VERSION_STR}），控件渲染 {pixmap.width()}x{pixmap.height()}"

    def video_encoder() -> str:
        """录屏自检：确认能用 Media Foundation 编出 H.264 的 MP4。"""
        from . import recorder as rec

        ok, message = rec.probe_encoder()
        if not ok:
            raise RuntimeError(message)
        return message

    def video_record() -> str:
        """真实录一小段屏幕（0.6 秒）并检查产出的 MP4。"""
        from . import recorder as rec
        from .engine import Target, TARGET_SCREEN

        config = rec.RecordingConfig(
            target=Target(kind=TARGET_SCREEN),
            output_dir=str(target_dir),
            resolution="480p",
            fps=15,
            quality="low",
            filename_pattern="selftest_record",
            start_delay=0.0,
            max_duration=0.6,
            capture_cursor=True,
        )
        engine = rec.RecorderEngine(config)
        engine.start(countdown=False)
        deadline = time.time() + 20
        while engine.running and time.time() < deadline:
            time.sleep(0.1)
        result = engine.stop_and_wait(timeout=30)
        if not result.files:
            raise RuntimeError("没有生成录像文件" + (f"：{engine.error}" if engine.error else ""))
        path = Path(result.files[0])
        size = path.stat().st_size if path.exists() else 0
        if size < 1024:
            raise RuntimeError(f"录像文件过小（{size} 字节）")
        return f"{path.name}，{result.frames} 帧，{size / 1024:.0f} KB"

    target_dir = Path(output_dir).expanduser() if output_dir else Path(tempfile.gettempdir())
    target_dir.mkdir(parents=True, exist_ok=True)

    def save_png() -> str:
        image = w.capture_region((0, 0, 200, 150))
        path = target_dir / "selftest_capture.png"
        image.save(path, "PNG")
        return f"{path}（{path.stat().st_size} 字节）"

    def os_info() -> str:
        version = sys.getwindowsversion()
        build, server = version.build, getattr(version, "product_type", 1) == 3
        if server and build == 14393:
            name = f"Windows Server 2016（build {build}）"
        elif server and build == 17763:
            name = f"Windows Server 2019（build {build}）"
        elif build >= 22000:
            name = f"Windows 11（build {build}）"
        elif build >= 10240:
            name = f"Windows 10（build {build}）"
        else:
            name = f"Windows {version.major}.{version.minor}（build {build}）"
        if wgc.os_build() and build < wgc.WGC_MIN_BUILD:
            name += f" —— 不支持 WGC（需 build {wgc.WGC_MIN_BUILD}+），将自动使用 GDI 方式"
        return name

    check("程序版本", lambda: __import__("screen_capture").__version__)
    check("DPI 感知", lambda: w.enable_dpi_awareness())
    check("系统版本", os_info)
    check("运行权限", lambda: f"完整性级别 {w.process_integrity()}"
                             + ("（偏低，WGC 可能被拒绝）" if w.process_integrity() == "Low" else ""))
    check("窗口枚举", lambda: f"找到 {len(w.enum_windows())} 个可见窗口")
    check("屏幕区域截图", lambda: f"{w.capture_region((0, 0, 160, 120)).size}")
    check("全屏截图", lambda: f"{w.capture_screen_full().size}")
    check("窗口截图（GDI）", window_capture)
    check("WGC 窗口捕获", wgc_capture)
    check("保存 PNG 文件", save_png)
    check("tkinter / Pillow 界面组件", tk_preview)
    check("Qt 界面组件（PyQt6）", qt_check)
    check("录屏编码器（H.264 / MP4）", video_encoder)
    check("录屏实拍（0.6 秒）", video_record)

    frozen = bool(getattr(sys, "frozen", False))
    lines = [
        f"自检报告    {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"运行方式：  {'打包后的 exe' if frozen else 'Python 源码'}",
        f"可执行文件：{sys.executable}",
        f"Python：    {sys.version.split()[0]}",
        "",
    ]
    lines += [f"[{'通过' if ok else '失败'}] {name}：{detail}" for name, ok, detail in checks]
    failed = [c for c in checks if not c[1]]
    lines += ["", f"结果：{'全部通过' if not failed else str(len(failed)) + ' 项失败'}"]

    for line in lines:
        _safe_print(line)

    try:
        report = target_dir / "selftest_report.txt"
        report.write_text("\n".join(lines), encoding="utf-8")
        _safe_print(f"报告文件：{report}")
    except Exception as exc:  # pragma: no cover - 目录不可写时
        _safe_print(f"写入报告失败：{exc}")
    return 0 if not failed else 1
