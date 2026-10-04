# -*- coding: utf-8 -*-
"""命令行模式：列窗口、按窗口/屏幕定时截图，便于脚本化与自动化验证。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import applog
from . import paths
from . import win32 as w
from .engine import (
    CaptureConfig,
    CaptureEngine,
    ConfigError,
    Target,
    TARGET_SCREEN,
    TARGET_WINDOW,
    resolve_target,
    unknown_placeholders,
)


def _print(text: str = "", file=None) -> None:
    """安全输出：打包成无控制台的 exe 时 sys.stdout / sys.stderr 可能是 None。"""
    stream = file if file is not None else sys.stdout
    if stream is None:
        return
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - 极端控制台编码
        print(text.encode("utf-8", "replace").decode("ascii", "replace"), file=stream, flush=True)
    except Exception:  # pragma: no cover - 输出流不可用
        pass


def list_windows(show_all: bool = False, keyword: str = "") -> int:
    infos = w.enum_windows(include_own=show_all, include_untitled=show_all)
    if keyword:
        needle = keyword.lower()
        infos = [i for i in infos if needle in i.title.lower() or needle in i.process_name.lower()]
    if not infos:
        _print("没有找到符合条件的可见窗口。")
        return 0
    _print(f"共 {len(infos)} 个可见窗口：")
    _print(f"{'序号':<6}{'进程':<22}{'尺寸':<14}{'句柄':<12}标题")
    _print("-" * 96)
    for i, info in enumerate(infos, start=1):
        title = info.title if len(info.title) <= 44 else info.title[:43] + "…"
        flag = " [最小化]" if info.minimized else ""
        _print(f"{i:<6}{info.process_name[:20]:<22}{info.size_text:<14}0x{info.hwnd:<10X}{title}{flag}")
    return 0


def pick_window(args) -> Target:
    """按 --hwnd / --title / --index 选定目标窗口。"""
    if args.screen:
        return Target(kind=TARGET_SCREEN)
    if args.hwnd:
        hwnd = int(str(args.hwnd), 0)
        if not w.is_window(hwnd):
            raise ConfigError(f"句柄 0x{hwnd:X} 不是有效窗口")
        info = w.window_info(hwnd)
        return Target(kind=TARGET_WINDOW, hwnd=hwnd, title=info.title,
                      app_label=info.process_name or info.title)
    if args.title:
        matches = w.find_windows_by_title(args.title, exact=args.exact)
        if not matches:
            raise ConfigError(f"没有找到标题包含「{args.title}」的窗口")
        info = matches[0]
        return Target(kind=TARGET_WINDOW, hwnd=info.hwnd, title=info.title,
                      app_label=info.process_name or info.title)
    if args.index:
        infos = w.enum_windows(include_own=True, include_untitled=True)
        if not 1 <= args.index <= len(infos):
            raise ConfigError(f"序号 {args.index} 超出范围（1~{len(infos)}）")
        info = infos[args.index - 1]
        return Target(kind=TARGET_WINDOW, hwnd=info.hwnd, title=info.title,
                      app_label=info.process_name or info.title)
    raise ConfigError("请用 --screen / --hwnd / --title / --index 指定截图目标")


def run_capture(args) -> int:
    target = resolve_target(pick_window(args))
    config = CaptureConfig(
        target=target,
        output_dir=args.out,
        interval=args.interval,
        start_delay=args.delay,
        max_shots=args.count,
        max_duration=args.duration,
        method=args.method,
        skip_unchanged=args.skip_unchanged,
        folder_mode="flat" if args.no_subdir else args.folder_mode,
        image_format=args.format,
        jpeg_quality=args.quality,
        filename_pattern=args.pattern,
        write_manifest=not args.no_manifest,
        client_only=args.client_only,
        capture_cursor=args.cursor,
        activate_before_capture=not args.no_activate,
    )
    config.validate()

    quiet = args.quiet
    events = []

    unknown = unknown_placeholders(config.filename_pattern)
    if unknown and not quiet:
        _print("提示：文件名模板中无法识别的占位符 "
               + "、".join("{" + name + "}" for name in unknown) + "，已改用默认命名。")

    def on_event(event: dict) -> None:
        events.append(event)
        kind = event.get("type")
        if kind == "started":
            if not quiet:
                _print(f"目标：{event['target']}")
                _print(f"输出：{event['output_dir']}")
                _print(f"间隔：{event['interval']:g} 秒，方式：{event['method']}")
        elif kind == "shot":
            _print(f"[{event['timestamp']}] 第 {event['index']} 次 -> {event['filename']} "
                   f"({event['width']}x{event['height']}, {event['method']}, "
                   f"{event['took_ms']} ms, {event['bytes'] / 1024:.1f} KB)")
        elif kind == "skip":
            if not quiet:
                _print(f"[跳过] 第 {event['index']} 次：{event['reason']}")
        elif kind == "error":
            _print(f"[错误] {event['message']}", file=sys.stderr)
        elif kind == "finished":
            _print(f"结束：{event['reason']}；共保存 {event['saved']} 张，"
                   f"跳过 {event['skipped']} 张，尝试 {event['attempts']} 次")

    engine = CaptureEngine(config, on_event)
    engine.start()
    try:
        while engine.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        _print("\n收到中断，正在停止…")
        engine.stop()
    engine.join(timeout=5)

    saved = sum(1 for e in events if e.get("type") == "shot")
    return 0 if saved > 0 else 2


def archive_now(directory: str, delete_originals: bool = True) -> int:
    """立刻归档「早于今天」的截图（供 --archive-now 和定时任务使用）。"""
    from datetime import datetime

    from .archive import archive_before

    root = Path(directory).expanduser()
    today = datetime.now().strftime("%Y-%m-%d")
    if not root.is_dir():
        _print(f"目录不存在：{root}", file=sys.stderr)
        return 2
    results = archive_before(root, today, delete_originals=delete_originals)
    if not results:
        _print(f"没有需要归档的内容（目录：{root}）")
        return 0
    for path in results:
        _print(f"已归档：{path}（{path.stat().st_size / 1024 / 1024:.1f} MB）")
    _print(f"共 {len(results)} 个归档包，目录：{root}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screen-capture",
        description="按应用窗口定时截图（Windows / Python）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py --list\n"
            "  python run.py --cli --title 记事本 --interval 2 --count 5 --out shots\n"
            "  python run.py --cli --index 3 --interval 0.5 --duration 10 --out shots --method screen\n"
            "  python run.py --cli --screen --interval 60 --count 3 --out shots\n"
            "  python run.py --selftest\n"
        ),
    )
    parser.add_argument("--list", action="store_true", help="列出当前可见窗口后退出")
    parser.add_argument("--all", action="store_true", help="列窗口时包含无标题窗口和本程序自身")
    parser.add_argument("--keyword", default="", help="列窗口时按关键字过滤")
    parser.add_argument("--cli", action="store_true", help="以命令行方式运行截图（不带界面）")
    parser.add_argument("--archive-now", nargs="?", const="", default=None, metavar="目录",
                        help="立刻把指定目录里「早于今天」的截图压缩成 zip 后退出"
                             "（不指定目录则用默认保存目录）")
    parser.add_argument("--archive-keep", action="store_true",
                        help="归档时保留原图（默认压缩后删除原图）")
    parser.add_argument("--selftest", nargs="?", const="", default=None, metavar="目录",
                        help="自检环境与运行库（打包 exe 后建议先跑一次），可指定报告输出目录")

    parser.add_argument("--screen", action="store_true", help="截取整个屏幕（所有显示器）")
    parser.add_argument("--hwnd", default="", help="按窗口句柄指定目标，如 0x00010A2C")
    parser.add_argument("--title", default="", help="按标题（模糊匹配）指定目标窗口")
    parser.add_argument("--exact", action="store_true", help="--title 使用完全匹配")
    parser.add_argument("--index", type=int, default=0, help="按 --list 中的序号指定目标窗口")

    parser.add_argument("--interval", type=float, default=5.0, help="截图间隔秒数，默认 5")
    parser.add_argument("--count", type=int, default=0, help="最多保存多少张，0 = 不限")
    parser.add_argument("--duration", type=float, default=0.0, help="最长运行秒数，0 = 不限")
    parser.add_argument("--delay", type=float, default=0.0, help="开始后延迟多少秒截第一张")
    parser.add_argument("--method", default=w.METHOD_AUTO,
                        choices=[w.METHOD_AUTO, w.METHOD_WGC, w.METHOD_PRINTWINDOW, w.METHOD_SCREEN],
                        help="截图方式：auto（WGC 优先）/ wgc / printwindow / screen")
    parser.add_argument("--client-only", action="store_true",
                        help="只截客户区（去掉标题栏和边框）")
    parser.add_argument("--cursor", action="store_true", help="画面包含鼠标光标")
    parser.add_argument("--no-activate", action="store_true",
                        help="屏幕区域方式截图前不把目标窗口切到前台（默认会切）")
    parser.add_argument("--ui", default="qt", choices=["qt", "tk"],
                        help="界面实现：qt（PyQt5，默认）/ tk（旧版 tkinter）")
    parser.add_argument("--out", default=str(paths.default_capture_dir()),
                        help="保存目录，默认「程序目录\\ScreenCapture」")
    parser.add_argument("--format", default="png", choices=["png", "jpg", "bmp", "webp"],
                        help="图片格式，默认 png")
    parser.add_argument("--quality", type=int, default=90, help="JPG/WEBP 质量，默认 90")
    parser.add_argument("--pattern", default="{app}_{date}_{time}_{index:04d}",
                        help="文件名模板，可用 {app} {date} {time} {datetime} {index} {hwnd}")
    parser.add_argument("--no-subdir", action="store_true", help="不建子目录，截图直接放在 --out 目录里")
    parser.add_argument("--folder-mode", default="app", choices=["app", "session", "flat"],
                        help="子目录方式：app=按窗口标题分文件夹（默认）/ session=每次新建时间戳文件夹 / flat=不建")
    parser.add_argument("--skip-unchanged", action="store_true", help="画面与上一张相同则不保存")
    parser.add_argument("--no-manifest", action="store_true", help="不写 capture_manifest.csv")
    parser.add_argument("--quiet", action="store_true", help="只输出必要的保存信息")
    return parser


def main(argv=None) -> int:
    w.enable_dpi_awareness()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return list_windows(show_all=args.all or bool(args.keyword), keyword=args.keyword)

    if args.selftest is not None:
        from .selftest import run_selftest
        return run_selftest(args.selftest or None)

    if args.archive_now is not None:
        return archive_now(args.archive_now or str(paths.default_capture_dir()),
                           delete_originals=not args.archive_keep)

    if not args.cli:
        return launch_ui(args.ui)

    try:
        return run_capture(args)
    except ConfigError as exc:
        _print(f"参数错误：{exc}", file=sys.stderr)
        return 2


def launch_ui(prefer: str = "qt") -> int:
    """打开图形界面：默认 PyQt5，缺库时自动退回 tkinter。"""
    applog.setup_logging()
    if prefer == "qt":
        try:
            from .gui_qt import launch as launch_qt

            return launch_qt()
        except ImportError as exc:
            _print(f"没有可用的 PyQt5（{exc}），改用 tkinter 界面。", file=sys.stderr)
    from .gui import launch as launch_tk

    return launch_tk()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
