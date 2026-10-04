# -*- coding: utf-8 -*-
"""每日归档测试：按天打包、删除原图、保留当天文件。

运行：python tests/test_archive.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from screen_capture.archive import (  # noqa: E402
    ARCHIVE_DIR_NAME, DailyArchiver, archive_before, archive_day, collect_day,
)

WORK = ROOT / "_test_out" / "archive"


def make_shot(path: Path, when: datetime, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(WORK, ignore_errors=True)
        self.today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.yesterday = self.today - timedelta(days=1)
        self.day_before = self.today - timedelta(days=2)
        # 昨天两条、前天一条、今天一条
        self.old_files = [
            make_shot(WORK / "app_20261003_120000" / "shot_0001.png", self.yesterday),
            make_shot(WORK / "app_20261003_120000" / "shot_0002.png", self.yesterday),
            make_shot(WORK / "app_20261002_120000" / "shot_0001.png", self.day_before),
        ]
        self.today_file = make_shot(WORK / "app_today" / "shot_0001.png", self.today)

    def test_collect_by_day(self):
        yesterday_key = self.yesterday.strftime("%Y-%m-%d")
        found = collect_day(WORK, yesterday_key)
        self.assertEqual(len(found), 2, [p.name for p in found])

    def test_archive_previous_days_and_delete(self):
        today_key = self.today.strftime("%Y-%m-%d")
        zips = archive_before(WORK, today_key, delete_originals=True)
        names = sorted(p.stem for p in zips)
        self.assertEqual(names, [(self.today - timedelta(days=2)).strftime("%Y-%m-%d"),
                                 self.yesterday.strftime("%Y-%m-%d")])

        yesterday_zip = WORK / ARCHIVE_DIR_NAME / f"{self.yesterday.strftime('%Y-%m-%d')}.zip"
        self.assertTrue(yesterday_zip.is_file())
        with zipfile.ZipFile(yesterday_zip) as zf:
            inside = zf.namelist()
        self.assertIn("app_20261003_120000/shot_0001.png", inside)
        self.assertIn("app_20261003_120000/shot_0002.png", inside)

        # 原图删除、空目录清掉；今天的文件不动
        for path in self.old_files:
            self.assertFalse(path.exists(), f"原图没有被删除：{path}")
        self.assertTrue(self.today_file.is_file(), "当天的文件不应被归档")
        self.assertFalse((WORK / "app_20261003_120000").exists(), "空目录应当被清理")

    def test_archive_keeps_originals(self):
        today_key = self.today.strftime("%Y-%m-%d")
        archive_before(WORK, today_key, delete_originals=False)
        for path in self.old_files:
            self.assertTrue(path.exists(), "选择保留原图时不应删除")

    def test_archive_day_without_files(self):
        self.assertIsNone(archive_day(WORK, "2000-01-01"))

    def test_second_run_is_idempotent(self):
        today_key = self.today.strftime("%Y-%m-%d")
        first = archive_before(WORK, today_key, delete_originals=True)
        second = archive_before(WORK, today_key, delete_originals=True)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [], "第二次不应再产生归档")
        self.assertEqual(len(list((WORK / ARCHIVE_DIR_NAME).glob("*.zip"))), 2)

    def test_midnight_countdown(self):
        seconds = DailyArchiver.seconds_until_midnight(datetime(2026, 10, 4, 23, 59, 0))
        self.assertGreaterEqual(seconds, 60)
        self.assertLessEqual(seconds, 70)


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    unittest.main(verbosity=2)
