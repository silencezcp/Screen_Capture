# -*- coding: utf-8 -*-
"""程序入口。

直接双击 / 运行本文件即打开图形界面：
    python run.py

命令行用法：
    python run.py --list
    python run.py --cli --title 记事本 --interval 2 --count 5 --out shots
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 控制台重定向到文件时，避免个别字符导致 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from screen_capture.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
