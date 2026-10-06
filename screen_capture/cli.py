# -*- coding: utf-8 -*-
"""命令行模式：列窗口、按窗口/屏幕定时截图、录屏，便于脚本化与自动化验证。"""
from __future__ import annotations

import argparse
import queue
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
    """按 --hwnd / --title / --index / --screen 选定目标窗口。"""
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
    raise ConfigError("请用 --screen / --hwnd / --title / --index 指定目标")


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


def run_record(args) -> int:
    """命令行录屏：录制指定时长后自动停止。"""
    from . import recorder as rec

    target = resolve_target(pick_window(args))

    if args.record_duration <= 0:
        _print("录制时长必须大于 0 秒（--record-duration）", file=sys.stderr)
        return 2

    config = rec.RecordingConfig(
        target=target,
        output_dir=args.out,
        resolution=args.record_resolution,
        fps=args.record_fps,
        quality=args.record_quality,
        bitrate_kbps=args.record_bitrate,
        profile=args.record_profile,
        input_format=args.record_input,
        scale_mode=args.record_scale,
        filename_pattern=args.record_pattern,
        start_delay=args.delay,
        max_duration=args.record_duration,
        capture_cursor=args.cursor,
    )
    try:
        config.validate()
    except ConfigError as exc:
        _print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    ok, message = rec.probe_encoder()
    _print(f"编码器自检：{message}")
    if not ok:
        return 3

    events: "queue.Queue[dict]" = queue.Queue()
    engine = rec.RecorderEngine(config, on_event=events.put)

    _print(f"目标：{target.describe()}")
    _print(f"画质：{config.describe()}")
    _print(f"输出：{config.output_dir}")
    try:
        engine.start(countdown=config.start_delay > 0)
    except ConfigError as exc:
        _print(f"无法开始录制：{exc}", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.record_duration + max(0.0, config.start_delay) + 10.0
    last_progress = -1.0
    try:
        while engine.running and time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                event = None
            if event is not None:
                kind = event.get("type")
                if kind == "started":
                    _print(f"开始录制：{event.get('quality')}")
                elif kind == "countdown":
                    _print(f"倒计时 {event.get('remaining')} 秒…")
                elif kind == "file":
                    _print(f"写入：{event.get('path')}")
                elif kind == "quality":
                    _print(f"画质：{event.get('text')}（输入 {event.get('input_format')}）")
                elif kind == "error":
                    _print(f"[错误] {event.get('message')}", file=sys.stderr)
                elif kind == "finished":
                    pass   # 结束汇总统一在下面输出，避免重复
            stats = engine.snapshot()
            if stats.elapsed - last_progress >= 2.0:
                last_progress = stats.elapsed
                _print(f"  … 已录制 {stats.elapsed_text}，{stats.frames} 帧，"
                       f"{stats.actual_fps:.1f} fps，{stats.size_text}")
    except KeyboardInterrupt:
        _print("\n收到中断，正在停止…")
    finally:
        result = engine.stop_and_wait(timeout=30.0)

    _print(result.summary())
    for path in result.files:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        _print(f"  文件：{path}（{size / 1024 / 1024:.1f} MB）")
    return 0 if result.frames > 0 and result.total_bytes > 1024 else 3


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
        description="按应用窗口定时截图 + 录屏（Windows / Python）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py --list\n"
            "  python run.py --cli --title 记事本 --interval 2 --count 5 --out shots\n"
            "  python run.py --cli --index 3 --interval 0.5 --duration 10 --out shots --method screen\n"
            "  python run.py --cli --screen --interval 60 --count 3 --out shots\n"
            "  python run.py --record --screen --record-fps 30 --record-resolution 1080p "
            "--record-quality high --record-duration 10 --out videos\n"
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
    parser.add_argument("--duration", type=float, default=0.0, help="截图最长运行秒数，0 = 不限")
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
                        help="界面实现：qt（PyQt6，默认）/ tk（tkinter）")
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

    # ---- 录屏 ----
    parser.add_argument("--record", action="store_true",
                        help="录屏模式：把目标录成 MP4（可调画质），配合下方参数使用")
    parser.add_argument("--record-duration", type=float, default=10.0,
                        help="录制时长（秒），默认 10")
    parser.add_argument("--record-resolution", default="1080p",
                        choices=["native", "2160p", "1440p", "1080p", "720p", "480p", "360p"],
                        help="录制分辨率档位，默认 1080p（native = 原始分辨率）")
    parser.add_argument("--record-fps", type=int, default=30, help="录制帧率，默认 30")
    parser.add_argument("--record-quality", default="high",
                        choices=["low", "standard", "high", "ultra"],
                        help="清晰度档位，默认 high（同一分辨率下越高越清晰、文件越大）")
    parser.add_argument("--record-bitrate", type=int, default=0,
                        help="码率 kbps；0 = 按分辨率与清晰度自动计算（默认）")
    parser.add_argument("--record-profile", default="high", choices=["high", "main", "baseline"],
                        help="H.264 档位，默认 high")
    parser.add_argument("--record-input", default="auto", choices=["auto", "nv12"],
                        help="编码输入色彩格式：auto（系统转换，省 CPU）/ nv12（程序转换）")
    parser.add_argument("--record-scale", default="quality", choices=["quality", "fast"],
                        help="取帧缩放方式：quality（更细腻，帧率上限约 30fps）/ "
                             "fast（更快，720p 可达 50fps，适合 60fps 录制）")
    parser.add_argument("--record-pattern", default="{app}_{date}_{time}",
                        help="录像文件名模板，可用 {app} {date} {time} {datetime} {resolution} {fps}")
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

    if args.record:
        try:
            return run_record(args)
        except ConfigError as exc:
            _print(f"参数错误：{exc}", file=sys.stderr)
            return 2

    if not args.cli:
        return launch_ui(args.ui)

    try:
        return run_capture(args)
    except ConfigError as exc:
        _print(f"参数错误：{exc}", file=sys.stderr)
        return 2


def launch_ui(prefer: str = "qt") -> int:
    """打开图形界面：默认 PyQt6，缺库时自动退回 tkinter。"""
    applog.setup_logging()
    if prefer == "qt":
        try:
            from .gui_qt import launch as launch_qt

            return launch_qt()
        except ImportError as exc:
            _print(f"没有可用的 PyQt6（{exc}），改用 tkinter 界面。"
                   f"可执行：pip install PyQt6 -i https://pypi.tuna.tsinghua.edu.cn/simple",
                   file=sys.stderr)
    from .gui import launch as launch_tk

    return launch_tk()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
