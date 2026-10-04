# -*- coding: utf-8 -*-
"""日志：实时写入本地文件，并可同时喂给界面显示。

* 日志目录：优先 exe / 项目目录下的 ``logs\\``，不可写时退到
  ``%LOCALAPPDATA%\\ScreenCaptureTool\\logs``，再不行用系统临时目录；
* 每条日志立即 flush（logging.FileHandler 本身就是每条 flush），所以是实时落盘；
* 按 2 MB 滚动、保留 5 个备份，长期挂机不会把磁盘写满。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from .paths import app_dir, default_log_dir as _default_log_dir

LOGGER_NAME = "screen_capture"
_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 5

_log_path: Optional[Path] = None


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def default_log_dir() -> Path:
    """日志目录：程序目录下的 logs，不可写时退到用户目录或临时目录。"""
    try:
        return _default_log_dir()
    except Exception:
        fallback = Path(tempfile.gettempdir()) / "ScreenCaptureTool" / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def setup_logging(log_dir: Optional[str] = None, level: int = logging.INFO) -> Path:
    """初始化文件日志（重复调用只生效一次），返回日志文件路径。"""
    global _log_path
    logger = get_logger()
    if _log_path is not None:
        return _log_path

    directory = Path(log_dir).expanduser() if log_dir else default_log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        directory = Path(tempfile.gettempdir()) / "ScreenCaptureTool" / "logs"
        directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"screen_capture_{datetime.now():%Y%m%d}.log"
    handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    _log_path = path
    logger.info("=" * 60)
    logger.info("应用窗口定时截图工具启动，日志文件：%s", path)
    return path


def get_log_path() -> Optional[Path]:
    return _log_path


class CallbackHandler(logging.Handler):
    """把日志记录转发给一个普通回调（界面用它把日志实时显示出来）。

    回调会在写日志的那个线程里执行，所以界面侧要用线程安全的方式
    （Qt 里就是 emit 一个信号，队列连接会自动切回主线程）。
    """

    def __init__(self, callback: Callable[[str, logging.LogRecord], None], level: int = logging.NOTSET):
        super().__init__(level)
        self._callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", _DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 依赖界面
        try:
            self._callback(self.format(record), record)
        except Exception:
            pass


def attach_callback_handler(callback: Callable[[str, logging.LogRecord], None]) -> CallbackHandler:
    handler = CallbackHandler(callback)
    get_logger().addHandler(handler)
    return handler
