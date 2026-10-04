# -*- coding: utf-8 -*-
"""按天归档截图：把「前一天」的图片打包成 zip，节省磁盘空间。

* 归档文件放在 ``<输出目录>\\_archive\\YYYY-MM-DD.zip``，内部保留原有相对路径；
* 默认压缩后删除原图；正在被写入的文件会跳过，不会影响正在进行的截图；
* ``DailyArchiver`` 是个后台线程，每天 00:00 自动跑一次，程序启动时也会补做一次
  （比如昨晚关机了，今天开机照样把前一天的归档掉）。
"""
from __future__ import annotations

import logging
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

ARCHIVE_DIR_NAME = "_archive"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

logger = logging.getLogger("screen_capture")


def archive_dir(root: Path) -> Path:
    return Path(root) / ARCHIVE_DIR_NAME


def _iter_candidates(root: Path) -> List[Path]:
    """输出目录下所有可归档的文件（跳过归档目录自身）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if ARCHIVE_DIR_NAME in path.relative_to(root).parts:
                continue
        except ValueError:  # pragma: no cover
            continue
        files.append(path)
    return files


def day_key(path: Path) -> str:
    """按文件的修改时间算「哪一天」（截图落盘时间）。"""
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def collect_day(root: Path, day: str) -> List[Path]:
    return [p for p in _iter_candidates(root) if day_key(p) == day]


def archive_day(root: Path, day: str, delete_originals: bool = True) -> Optional[Path]:
    """把某一天的截图打包成 zip；没有文件就返回 None。"""
    root = Path(root)
    files = collect_day(root, day)
    if not files:
        return None
    target_dir = archive_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir / f"{day}.zip"

    added = 0
    skipped = 0
    mode = "a" if zip_path.exists() else "w"
    with zipfile.ZipFile(zip_path, mode, zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        existing = set(zf.namelist())
        for path in files:
            try:
                arcname = str(path.relative_to(root)).replace("\\", "/")
                if arcname in existing:
                    continue
                zf.write(path, arcname)
                added += 1
            except (OSError, ValueError) as exc:
                # 文件正被写入 / 被占用：留给下一次
                skipped += 1
                logger.debug("跳过 %s：%s", path, exc)

    if delete_originals:
        for path in files:
            try:
                arcname = str(path.relative_to(root)).replace("\\", "/")
                with zipfile.ZipFile(zip_path) as zf:
                    if arcname not in zf.namelist():
                        continue
                path.unlink()
            except OSError:
                skipped += 1
    # 清掉压缩后变空的目录
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name != ARCHIVE_DIR_NAME:
            try:
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass
    logger.info("已归档 %s：%d 个文件 -> %s（跳过 %d）", day, added, zip_path, skipped)
    return zip_path


def archive_before(root: Path, day: str, delete_originals: bool = True) -> List[Path]:
    """归档所有早于指定日期的截图。"""
    root = Path(root)
    days = sorted({day_key(p) for p in _iter_candidates(root) if day_key(p) < day})
    results = []
    for item in days:
        result = archive_day(root, item, delete_originals=delete_originals)
        if result:
            results.append(result)
    return results


class DailyArchiver(threading.Thread):
    """后台线程：每天 00:00 归档前一天（顺带补做之前漏掉的）。"""

    def __init__(self, root_provider, delete_originals: bool = True, on_event=None):
        super().__init__(name="daily-archiver", daemon=True)
        self._root_provider = root_provider      # 返回当前输出目录的可调用对象
        self._delete = delete_originals
        self._on_event = on_event or (lambda *_a, **_k: None)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def seconds_until_midnight(now: Optional[datetime] = None) -> float:
        now = now or datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return max(5.0, (tomorrow - now).total_seconds())

    def run_once(self, day: Optional[str] = None) -> List[Path]:
        root = Path(self._root_provider())
        day = day or datetime.now().strftime("%Y-%m-%d")
        try:
            results = archive_before(root, day, delete_originals=self._delete)
        except Exception as exc:  # pragma: no cover - 归档失败不影响截图
            logger.warning("自动归档失败：%s", exc)
            return []
        for path in results:
            try:
                self._on_event(path)
            except Exception:  # 界面已关闭等情况，忽略
                pass
        return results

    def run(self) -> None:  # pragma: no cover - 线程循环
        try:
            # 启动时先补做一次（覆盖「昨天没开机」的情况）
            self.run_once()
            while not self._stop.is_set():
                if self._stop.wait(self.seconds_until_midnight()):
                    return
                self.run_once()
        except Exception as exc:  # 归档线程绝不能把主程序带崩
            logger.warning("归档线程异常退出：%s", exc)
