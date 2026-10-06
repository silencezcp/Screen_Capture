# -*- coding: utf-8 -*-
"""程序入口。

直接双击 / 运行本文件即打开图形界面：
    python run.py

命令行用法：
    python run.py --list
    python run.py --cli --title 记事本 --interval 2 --count 5 --out shots
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _setup_frozen_qt() -> None:
    """打包成 exe 后，把 Qt 的插件目录与 DLL 目录显式指出来。

    为什么要这一步：PyQt6 是「pip 装的包」还是「conda 装的包」，Qt6 的 DLL 与
    plugins 位置不一样（conda 放在 Library\\lib\\qt6\\...）。打包后 Qt 不一定能自己
    找到 plugins\\platforms\\qwindows.dll，界面就会**静默退出**（windowed 模式看不到
    任何报错）。这里主动设置环境变量，让 Qt 一定找得到。
    """
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    candidates = [
        base / "PyQt6" / "Qt6",
        base / "PyQt6",
        base,
    ]
    dll_dirs = []
    for root in candidates:
        for sub in ("bin", "plugins", ""):
            directory = root / sub if sub else root
            if directory.is_dir():
                if sub == "bin":
                    dll_dirs.append(directory)
                plugin_root = directory if sub == "plugins" else directory / "plugins"
                if (plugin_root / "platforms").is_dir():
                    os.environ.setdefault("QT_PLUGIN_PATH", str(plugin_root))
                    os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH",
                                          str(plugin_root / "platforms"))
                if sub == "bin" and (root / "plugins").is_dir():
                    os.environ.setdefault("QT_PLUGIN_PATH", str(root / "plugins"))
    for directory in dll_dirs:
        try:
            os.add_dll_directory(str(directory))
        except Exception:
            pass


def _startup_log(message: str) -> None:
    """把启动过程写进 logs/startup.log。

    打包成 windowed exe 之后 stdout / stderr 是黑洞，启动期一旦出错就是"双击没反应"；
    在最早的时刻把关键步骤落盘，排查时才有据可查。
    """
    try:
        target = Path(__file__).resolve().parent / "logs"
        target.mkdir(parents=True, exist_ok=True)
        with (target / "startup.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


_startup_log(f"启动 frozen={getattr(sys, 'frozen', False)} argv={sys.argv[1:]}")
_setup_frozen_qt()
_startup_log(f"Qt 路径已设置：QT_PLUGIN_PATH={os.environ.get('QT_PLUGIN_PATH', '')}")
# 控制台重定向到文件时，避免个别字符导致 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from screen_capture.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
