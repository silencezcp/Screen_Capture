# -*- coding: utf-8 -*-
"""应用窗口定时截图工具（Windows / Python）。

典型用法：

    from screen_capture import CaptureConfig, CaptureEngine, Target

    config = CaptureConfig(target=Target(kind="window", hwnd=0x1A2B),
                           output_dir="shots", interval=5, max_shots=10)
    engine = CaptureEngine(config, on_event=print)
    engine.start()
    engine.join()
"""
from .engine import (
    CaptureConfig,
    CaptureEngine,
    ConfigError,
    Target,
    TARGET_SCREEN,
    TARGET_WINDOW,
    build_filename,
    resolve_target,
    sanitize_filename_part,
)

__version__ = "1.0.0"
__all__ = [
    "CaptureConfig",
    "CaptureEngine",
    "ConfigError",
    "Target",
    "TARGET_SCREEN",
    "TARGET_WINDOW",
    "build_filename",
    "resolve_target",
    "sanitize_filename_part",
    "__version__",
]
