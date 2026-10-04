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

__all__ = ["app_dir", "resource_dir", "is_writable", "default_capture_dir", "default_log_dir",
           "multi_user_mode", "user_data_dir", "MULTI_USER_MARKER"]

CAPTURE_DIR_NAME = "ScreenCapture"
# 这个文件由「安装到所有用户」脚本放在程序目录里：表示程序被多个用户共用，
# 日志与默认截图目录必须按用户分开，否则多人同时写同一个日志文件会互相打架。
MULTI_USER_MARKER = "multi_user.txt"


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


def user_data_dir() -> Path:
    """当前用户的私有数据目录：%LOCALAPPDATA%\\ScreenCaptureTool。"""
    path = _local_appdata() / "ScreenCaptureTool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def multi_user_mode() -> bool:
    """程序是否被多个用户共用（服务器 / 多人同时使用）。

    判定顺序：环境变量 ``SCREEN_CAPTURE_MULTIUSER`` 显式指定（0/false 关闭、1/true 开启），
    否则看程序目录里是否存在 ``multi_user.txt``（由「安装到所有用户」脚本放置）。
    多人共用时日志和默认截图目录都按用户分开，避免互相覆盖/争用同一个文件。
    """
    env = os.environ.get("SCREEN_CAPTURE_MULTIUSER", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return (app_dir() / MULTI_USER_MARKER).exists()


def default_capture_dir() -> Path:
    """默认截图保存目录。

    单机/便携：程序目录下的 ``ScreenCapture``；
    多人共用或程序目录不可写：当前用户目录下的 ``ScreenCapture``。
    """
    if not multi_user_mode():
        preferred = app_dir() / CAPTURE_DIR_NAME
        if is_writable(preferred):
            return preferred
    fallback = user_data_dir() / CAPTURE_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def default_log_dir() -> Path:
    """默认日志目录（多人共用时按用户分开，避免同时写同一个日志文件）。"""
    if not multi_user_mode():
        preferred = app_dir() / "logs"
        if is_writable(preferred):
            return preferred
    fallback = user_data_dir() / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback
