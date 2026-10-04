# -*- coding: utf-8 -*-
"""端到端冒烟测试：真实创建窗口 -> 截图 -> 定时循环保存文件。

运行：python tests/test_smoke.py
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from screen_capture import win32 as w  # noqa: E402
from screen_capture.engine import (  # noqa: E402
    CaptureConfig,
    CaptureEngine,
    Target,
    TARGET_SCREEN,
    TARGET_WINDOW,
    build_filename,
    sanitize_filename_part,
)

TEST_COLOR = (18, 164, 255)          # #12A4FF
TEST_COLOR_HEX = "#12A4FF"
TEST_TITLE = "ScreenCaptureSelfTest"
OUT_DIR = ROOT / "_test_out"


def _pump(root, seconds: float) -> None:
    """在等待后台线程时保持 tkinter 事件循环运转。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)


class CliTests(unittest.TestCase):
    """命令行模式：出错时必须把原因打到 stderr。

    （曾经因为 _print() 不接受 file 参数，异常被引擎吞掉，用户什么也看不到。）
    """

    @classmethod
    def setUpClass(cls):
        import tkinter as tk

        cls.root = tk.Tk()
        cls.root.title(TEST_TITLE + "_cli")
        cls.root.geometry("300x200+220+220")
        cls.root.update()
        _pump(cls.root, 0.4)
        matches = w.find_windows_by_title(TEST_TITLE + "_cli", exact=True)
        cls.hwnd = matches[0].hwnd if matches else 0
        cls.root.iconify()
        cls.root.update()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_print_accepts_file(self):
        from screen_capture import cli

        buffer = io.StringIO()
        cli._print("写进这个流", file=buffer)
        self.assertIn("写进这个流", buffer.getvalue())
        cli._print("没有输出流时也不能抛异常")

    def test_capture_error_is_reported(self):
        from screen_capture import cli

        if not self.hwnd or not w.is_minimized(self.hwnd):
            self.skipTest("测试窗口没有进入最小化状态，跳过")
        out = OUT_DIR / "cli_error"
        shutil.rmtree(out, ignore_errors=True)
        args = cli.build_parser().parse_args([
            "--cli", "--hwnd", hex(self.hwnd), "--interval", "0.3", "--count", "1",
            "--out", str(out), "--no-subdir", "--method", "auto",
        ])
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = cli.run_capture(args)
        self.assertNotEqual(code, 0, "截图失败时应该返回非 0")
        self.assertIn("最小化", buffer.getvalue(), f"错误信息没有输出：{buffer.getvalue()!r}")
        print(f"  [info] CLI 错误输出：{buffer.getvalue().strip().splitlines()[-1]}")


class FileNameTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_filename_part('a<b>c:d/e\\f|g?h*i'), "a_b_c_d_e_f_g_h_i")
        self.assertEqual(sanitize_filename_part("   "), "capture")

    def test_pattern(self):
        from datetime import datetime

        name = build_filename("{app}_{date}_{time}_{index:04d}", "记事本", 3, 0x1A2B,
                              datetime(2026, 10, 4, 20, 6, 7), "png")
        self.assertEqual(name, "记事本_20261004_200607_0003.png")

    def test_bad_pattern_falls_back(self):
        from datetime import datetime

        name = build_filename("{nope}_{index}", "app", 1, 0, datetime.now(), "png")
        self.assertTrue(name.endswith("_0001.png"), name)
        self.assertTrue(name.startswith("app_"), name)
        self.assertNotIn("{", name)

    def test_unknown_placeholders(self):
        from screen_capture.engine import unknown_placeholders

        self.assertEqual(unknown_placeholders("{app}_{date}_{index:04d}"), [])
        self.assertEqual(unknown_placeholders("{nope}_{app}"), ["nope"])


class Win32Tests(unittest.TestCase):
    def test_dpi_and_screen(self):
        mode = w.enable_dpi_awareness()
        self.assertIn(mode, {"PerMonitorV2", "PerMonitor", "System", "none"})
        left, top, right, bottom = w.get_virtual_screen_rect()
        self.assertGreater(right - left, 0)
        self.assertGreater(bottom - top, 0)

    def test_enum_windows(self):
        infos = w.enum_windows()
        self.assertIsInstance(infos, list)
        for info in infos:
            self.assertGreater(info.hwnd, 0)
            self.assertGreater(info.width, 0)
            self.assertGreater(info.height, 0)
        print(f"  [info] 可见窗口数量 = {len(infos)}")

    def test_capture_region(self):
        image = w.capture_region((0, 0, 160, 120))
        self.assertEqual(image.size, (160, 120))
        self.assertEqual(image.mode, "RGB")


class WindowCaptureTests(unittest.TestCase):
    """创建一个已知颜色的窗口，验证两种截图方式都能拿到正确画面。"""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk

        cls.root = tk.Tk()
        cls.root.title(TEST_TITLE)
        cls.root.geometry("360x240+140+140")
        cls.root.configure(bg=TEST_COLOR_HEX)
        cls.root.attributes("-topmost", True)
        cls.root.update()
        _pump(cls.root, 0.6)
        # winfo_id 返回的是子窗口，这里按标题取顶层窗口句柄
        cls.info = None
        for candidate in w.find_windows_by_title(TEST_TITLE, exact=True):
            cls.info = candidate
            break
        if cls.info is None:
            raise unittest.SkipTest("未能找到测试窗口，跳过窗口截图测试")
        cls.hwnd = cls.info.hwnd

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _center_color(self, image: Image.Image):
        width, height = image.size
        return image.convert("RGB").getpixel((width // 2, height // 2))

    def test_window_geometry(self):
        info = w.window_info(self.hwnd)
        self.assertGreater(info.width, 100)
        self.assertGreater(info.height, 100)
        self.assertEqual(info.title, TEST_TITLE)

    def test_printwindow_capture(self):
        image, method = w.capture_window(self.hwnd, method=w.METHOD_PRINTWINDOW)
        self.assertEqual(method, w.METHOD_PRINTWINDOW)
        self.assertGreater(image.width, 100)
        color = self._center_color(image)
        print(f"  [info] PrintWindow 尺寸={image.size} 中心像素={color}")
        self.assertLessEqual(max(abs(a - b) for a, b in zip(color, TEST_COLOR)), 12,
                             f"PrintWindow 截到的颜色不正确：{color}")

    def test_minimized_window_raises(self):
        """最小化窗口应当给出明确错误，而不是截到黑图。"""
        self.root.iconify()
        self.root.update()
        time.sleep(0.4)
        try:
            if not w.is_minimized(self.hwnd):
                self.skipTest("窗口未进入最小化状态，跳过")
            with self.assertRaises(w.CaptureError) as ctx:
                w.capture_window(self.hwnd, method=w.METHOD_AUTO)
            self.assertIn("最小化", str(ctx.exception))
            print(f"  [info] 最小化窗口报错信息：{ctx.exception}")
        finally:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.update()
            time.sleep(0.5)

    def test_screen_capture(self):
        image, method = w.capture_window(self.hwnd, method=w.METHOD_SCREEN)
        self.assertEqual(method, w.METHOD_SCREEN)
        color = self._center_color(image)
        print(f"  [info] 屏幕区域 尺寸={image.size} 中心像素={color}")
        self.assertLessEqual(max(abs(a - b) for a, b in zip(color, TEST_COLOR)), 12,
                             f"屏幕区域截到的颜色不正确：{color}")

    def test_printwindow_matches_screen_size(self):
        """两种方式应当得到同样大小的画面（窗口不可见边框已被裁掉）。"""
        printwindow, _ = w.capture_window(self.hwnd, method=w.METHOD_PRINTWINDOW)
        screen, _ = w.capture_window(self.hwnd, method=w.METHOD_SCREEN)
        self.assertEqual(printwindow.size, screen.size,
                         f"PrintWindow={printwindow.size} 屏幕区域={screen.size}")

    def test_auto_capture(self):
        image, method = w.capture_window(self.hwnd, method=w.METHOD_AUTO)
        self.assertIn(method, (w.METHOD_PRINTWINDOW, w.METHOD_SCREEN))
        color = self._center_color(image)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(color, TEST_COLOR)), 12)


class EngineTests(unittest.TestCase):
    """定时循环：0.4 秒间隔截 3 张，检查文件、尺寸与清单。"""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk

        cls.root = tk.Tk()
        cls.root.title(TEST_TITLE)
        cls.root.geometry("320x200+180+180")
        cls.root.configure(bg=TEST_COLOR_HEX)
        cls.root.attributes("-topmost", True)
        cls.root.update()
        _pump(cls.root, 0.5)
        matches = w.find_windows_by_title(TEST_TITLE, exact=True)
        cls.hwnd = matches[0].hwnd if matches else 0

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_timed_capture_writes_files(self):
        if not self.hwnd:
            self.skipTest("没有可用的测试窗口")
        shutil.rmtree(OUT_DIR / "engine", ignore_errors=True)
        events = []
        config = CaptureConfig(
            target=Target(kind=TARGET_WINDOW, hwnd=self.hwnd, title=TEST_TITLE, app_label="selftest"),
            output_dir=str(OUT_DIR / "engine"),
            interval=0.4,
            max_shots=3,
            method=w.METHOD_SCREEN,
            folder_mode="flat",
        )
        engine = CaptureEngine(config, events.append)
        started = time.monotonic()
        engine.start()
        while engine.running and time.monotonic() - started < 20:
            _pump(self.root, 0.1)
        engine.join(timeout=5)

        shots = [e for e in events if e["type"] == "shot"]
        finished = [e for e in events if e["type"] == "finished"]
        self.assertEqual(len(shots), 3, f"事件：{events}")
        self.assertTrue(finished and finished[0]["reason"].startswith("已达到设定的截图数量"))

        files = sorted((OUT_DIR / "engine").glob("*.png"))
        self.assertEqual(len(files), 3, f"实际文件：{files}")
        for path in files:
            with Image.open(path) as image:
                self.assertGreater(image.width, 100)
                self.assertGreater(image.height, 100)
        manifest = OUT_DIR / "engine" / "capture_manifest.csv"
        self.assertTrue(manifest.exists())
        rows = [line for line in manifest.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        self.assertEqual(len(rows), 4)  # 表头 + 3 行数据
        elapsed = time.monotonic() - started
        print(f"  [info] 3 张截图用时 {elapsed:.2f} 秒，文件：{[p.name for p in files]}")
        self.assertGreaterEqual(elapsed, 0.8)


class WgcCompatibilityTests(unittest.TestCase):
    """老系统（Server 2016 / build 14393）上没有 Windows.Graphics.Capture，必须直接判定不可用。"""

    def test_build_gate(self):
        from screen_capture import capture_wgc as wgc

        self.assertEqual(wgc.WGC_MIN_BUILD, 17134, "WGC 最低构建号应为 1803 / 17134")
        self.assertGreater(wgc.os_build(), 0, "应当能取到系统构建号")
        if wgc.os_build() < wgc.WGC_MIN_BUILD:
            self.assertFalse(wgc.available(), "老系统上 WGC 必须判定为不可用")
            self.assertIn("系统版本过低", wgc.unavailable_reason())
        else:
            self.assertTrue(wgc.unavailable_reason() == "" or "wgc_python" in wgc.unavailable_reason())
        print(f"  [info] 系统 build {wgc.os_build()}，WGC 可用={wgc.available()}，"
              f"原因={wgc.unavailable_reason() or '无（可用）'}")


class SettingsLocationTests(unittest.TestCase):
    """配置必须存在用户目录的 .Screen_Capture 下（不再写注册表）。"""

    def test_settings_path_in_user_profile(self):
        from screen_capture import paths

        config_dir = paths.user_config_dir()
        self.assertEqual(config_dir.name, ".Screen_Capture")
        self.assertTrue(config_dir.exists(), "配置目录应当自动创建")
        self.assertTrue(str(config_dir).lower().startswith(str(Path.home()).lower()),
                        f"配置目录应在用户主目录下：{config_dir}")
        self.assertEqual(paths.settings_file().name, "settings.ini")
        self.assertEqual(paths.settings_file().parent, config_dir)
        print(f"  [info] 配置文件位置：{paths.settings_file()}")

    def test_settings_use_ini_file_not_registry(self):
        import os as _os

        from PyQt5.QtCore import QSettings

        from screen_capture.gui_qt import make_settings, settings_path

        target = Path(OUT_DIR) / "settings_check" / "settings.ini"
        _os.environ["SCREEN_CAPTURE_SETTINGS_FILE"] = str(target)
        try:
            self.assertEqual(settings_path(), target)
            settings = make_settings()
            settings.setValue("probe", "ok")
            settings.sync()
            self.assertTrue(target.exists(), "设置应写入 ini 文件")
            self.assertIn("[General]", target.read_text(encoding="utf-8"))
            self.assertEqual(QSettings(str(target), QSettings.IniFormat).value("probe"), "ok")
        finally:
            _os.environ.pop("SCREEN_CAPTURE_SETTINGS_FILE", None)
        print("  [info] 设置写入 ini 文件（不碰注册表）")


class BatchFileTests(unittest.TestCase):
    """批处理的格式约束（踩过坑，必须守住）。

    * 换行必须是 CRLF：裸 LF 会让 cmd 把多行粘在一起执行，报一堆
      「'xxx' 不是内部或外部命令」；
    * 编码必须是本机代码页（GBK/936）：文件里有中文提示，UTF-8 会被 cmd 读成乱码；
    * 文件名不要带括号：cmd 解析路径里的 () 会出错。
    """

    def test_bat_files_are_cmd_safe(self):
        bats = sorted(ROOT.glob("*.bat"))
        self.assertTrue(bats, "项目里应当有 .bat 启动脚本")
        for path in bats:
            data = path.read_bytes()
            with self.subTest(bat=path.name):
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""),
                                 f"{path.name} 有裸 LF 换行，cmd 会解析错乱")
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"),
                                 f"{path.name} 不应带 UTF-8 BOM")
                try:
                    text = data.decode("gbk")
                except UnicodeDecodeError as exc:
                    self.fail(f"{path.name} 不是 GBK/936 编码：{exc}")
                # 必须是正常文本：开头是 @echo off，且没有替换字符（乱码痕迹）
                self.assertTrue(text.lstrip().startswith("@echo off"),
                                f"{path.name} 第一行应当是 @echo off")
                self.assertNotIn("\ufffd", text, f"{path.name} 内容有乱码")
                self.assertIn("rem", text[:400], f"{path.name} 缺少注释头，疑似损坏")
                self.assertNotIn("(", path.name, f"{path.name} 文件名不要带括号，cmd 会解析失败")
        print(f"  [info] {len(bats)} 个 .bat 检查通过（CRLF + GBK + 无括号 + 无乱码）")


class MultiUserTests(unittest.TestCase):
    """服务器 / 多人同时使用：路径按用户分开、单实例名按用户+会话区分。"""

    def test_multiuser_paths_are_per_user(self):
        import os
        import tempfile
        from screen_capture import paths

        with tempfile.TemporaryDirectory(prefix="mu_") as tmp:
            fake_app = Path(tmp)
            # 没有标记时：仍用程序目录（单机/便携行为不变）
            old_app_dir = paths.app_dir
            paths.app_dir = lambda: fake_app
            try:
                os.environ.pop("SCREEN_CAPTURE_MULTIUSER", None)
                self.assertFalse(paths.multi_user_mode())
                self.assertEqual(paths.default_capture_dir(), fake_app / "ScreenCapture")

                # 有 multi_user.txt 时：日志与截图目录都退到当前用户目录
                (fake_app / paths.MULTI_USER_MARKER).write_text("x", encoding="utf-8")
                self.assertTrue(paths.multi_user_mode())
                capture = paths.default_capture_dir()
                logs = paths.default_log_dir()
                self.assertNotEqual(capture.parent, fake_app, "多人模式下截图目录不能放在共用程序目录")
                self.assertNotEqual(logs.parent, fake_app, "多人模式下日志目录不能放在共用程序目录")
                self.assertIn("ScreenCaptureTool", str(capture))
                self.assertEqual(capture.name, "ScreenCapture")
                self.assertEqual(logs.name, "logs")

                # 环境变量可以强制关闭多人模式（覆盖标记）
                os.environ["SCREEN_CAPTURE_MULTIUSER"] = "0"
                self.assertFalse(paths.multi_user_mode())
            finally:
                paths.app_dir = old_app_dir
                os.environ.pop("SCREEN_CAPTURE_MULTIUSER", None)
        print("  [info] 多人模式路径：日志/截图按用户分开，程序目录只读")

    def test_instance_name_per_user_session(self):
        from screen_capture.gui_qt import instance_name
        from screen_capture import win32 as w

        name = instance_name()
        self.assertIn(f".s{w.current_session_id()}", name)
        self.assertTrue(name.startswith("ScreenCaptureTool."))
        # 名字里不能出现中文/空格等（命名管道名要安全）
        self.assertTrue(name.isascii(), name)
        self.assertNotIn(" ", name)
        print(f"  [info] 单实例名按用户+会话区分：{name}")


class FolderModeTests(unittest.TestCase):
    """子目录命名：按「窗口标题」分目录，不同标题不同目录。"""

    def _engine(self, output_dir: Path, folder_mode: str) -> CaptureEngine:
        config = CaptureConfig(
            target=Target(kind=TARGET_WINDOW),
            output_dir=str(output_dir),
            folder_mode=folder_mode,
        )
        return CaptureEngine(config, lambda _event: None)

    def test_folder_named_by_window_title(self):
        out = OUT_DIR / "folders"
        engine = self._engine(out, "app")
        cases = [
            ("鸣潮  ", "Client-Win64-Shipping.exe"),
            ("番茄免费小说", "Androws.exe"),
            ("a/b:c*?\"<>|d", "evil.exe"),          # 非法字符要被替换
            ("", "bare.exe"),                        # 没标题 → 退回进程名
        ]
        names = []
        for title, app in cases:
            engine._target = Target(kind=TARGET_WINDOW, hwnd=0x11108A, title=title, app_label=app)
            names.append(engine._resolve_directory().name)
        self.assertEqual(names[0], "鸣潮", names)
        self.assertEqual(names[1], "番茄免费小说", names)
        self.assertNotIn("/", names[2])
        self.assertNotIn(":", names[2])
        self.assertNotIn("*", names[2])
        self.assertEqual(names[3], "bare.exe", names)
        self.assertEqual(len(set(names)), len(names), f"不同标题必须落不同目录：{names}")

        # 同一个标题（含标题末尾空格差异）必须落到同一个目录
        engine._target = Target(kind=TARGET_WINDOW, hwnd=0x222222, title="鸣潮  ", app_label="other.exe")
        self.assertEqual(engine._resolve_directory().name, "鸣潮")
        print(f"  [info] 文件夹按窗口标题命名：{names}")

    def test_screen_target_folder_and_session_mode(self):
        out = OUT_DIR / "folders2"
        engine = self._engine(out, "app")
        engine._target = Target(kind=TARGET_SCREEN, title="整个屏幕")
        self.assertEqual(engine._resolve_directory().name, "整个屏幕")

        engine = self._engine(out, "session")
        engine._target = Target(kind=TARGET_WINDOW, hwnd=1, title="鸣潮", app_label="x.exe")
        name = engine._resolve_directory().name
        self.assertTrue(name.startswith("鸣潮_"), name)

        engine = self._engine(out, "flat")
        engine._target = Target(kind=TARGET_WINDOW, hwnd=1, title="鸣潮", app_label="x.exe")
        self.assertEqual(engine._resolve_directory(), Path(str(out)))


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    unittest.main(verbosity=2)
