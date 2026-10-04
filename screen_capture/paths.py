# -*- coding: utf-8 -*-
"""统一的路径规则：程序目录、可写判断、默认截图目录。

默认截图目录固定为「程序目录\\ScreenCapture」：
* 源码运行时是项目根目录下的 ``ScreenCapture\\``；
* 打包成 exe 后是 exe 所在目录下的 ``ScreenCapture\\``（目录版就是 dist 里那个文件夹）；
* 如果程序目录不可写（例如放到了 Program Files），自动退到
  ``%LOCALAPPDATA%\\ScreenCaptureTool\\ScreenCapture``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["app_dir", "resource_dir", "is_writable", "default_capture_dir", "default_log_dir"]

CAPTURE_DIR_NAME = "ScreenCapture"


def app_dir() -> Path:
    """打包成 exe 后是 exe 所在目录，源码运行时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """随程序分发的只读资源目录（图标、下拉箭头等）。

    单文件 exe 会把资源解压到临时目录（sys._MEIPASS），目录版则和 exe 同级；
    源码运行时就是项目根目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def is_writable(directory: Path) -> bool:
    """目录能不能写（不存在就试着建出来）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) if base else Path.home()


def default_capture_dir() -> Path:
    """默认截图保存目录：程序目录下的 ScreenCapture。"""
    preferred = app_dir() / CAPTURE_DIR_NAME
    if is_writable(preferred):
        return preferred
    fallback = _local_appdata() / "ScreenCaptureTool" / CAPTURE_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def default_log_dir() -> Path:
    """默认日志目录：程序目录下的 logs（不可写时退到用户目录）。"""
    preferred = app_dir() / "logs"
    if is_writable(preferred):
        return preferred
    fallback = _local_appdata() / "ScreenCaptureTool" / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback
