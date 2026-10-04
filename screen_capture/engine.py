# -*- coding: utf-8 -*-
"""截图引擎：按设定的时间间隔循环截图、命名、保存、去重并记录清单。

引擎运行在独立线程中，通过回调（事件字典）向界面 / 命令行汇报进度，
因此可以在 tkinter 界面里做到“截图不卡界面”。
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from PIL import Image

from . import applog
from . import capture_wgc as wgc
from . import win32 as w

logger = applog.get_logger()

# 目标类型
TARGET_WINDOW = "window"
TARGET_SCREEN = "screen"

# 子目录方式
FOLDER_APP = "app"          # 按应用分文件夹，同一个应用复用同一个文件夹（默认）
FOLDER_SESSION = "session"  # 每次开始新建带时间戳的文件夹
FOLDER_FLAT = "flat"        # 直接放在输出目录里，不再分子文件夹

MAX_CONSECUTIVE_FAILURES = 3
MAX_WGC_FAILURES = 3          # WGC 连续抓帧失败多少次就重建会话
_VALID_EXT = {"png", "jpg", "jpeg", "bmp", "webp"}


class ConfigError(ValueError):
    """用户填写的参数不合法。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class Target:
    """截图目标：某个窗口，或整个虚拟屏幕。"""

    kind: str = TARGET_WINDOW
    hwnd: int = 0
    title: str = ""
    app_label: str = ""
    class_name: str = ""

    def describe(self) -> str:
        if self.kind == TARGET_SCREEN:
            return "整个屏幕（所有显示器）"
        if self.title:
            return f"{self.title}（0x{self.hwnd:X}）"
        return f"窗口 0x{self.hwnd:X}"


@dataclass
class CaptureConfig:
    target: Target = field(default_factory=Target)
    output_dir: str = ""
    interval: float = 5.0            # 两次截图之间的间隔（秒）
    start_delay: float = 0.0         # 点击开始后等多久再截第一张
    max_shots: int = 0               # 最多保存多少张，0 = 不限
    max_duration: float = 0.0        # 最长运行时长（秒），0 = 不限
    method: str = w.METHOD_AUTO      # auto / wgc / printwindow / screen
    skip_unchanged: bool = False     # 画面与上一张相同则不保存
    folder_mode: str = FOLDER_APP    # app（按应用复用）/ session（每次新建）/ flat（不建）
    image_format: str = "png"
    jpeg_quality: int = 90
    filename_pattern: str = "{app}_{date}_{time}_{index:04d}"
    write_manifest: bool = True
    client_only: bool = False        # 只截客户区（不含标题栏、边框）
    capture_cursor: bool = False     # 画面是否包含鼠标光标
    activate_before_capture: bool = True   # 退化到屏幕区域前先把窗口切到前台

    def validate(self) -> None:
        if self.target.kind == TARGET_WINDOW and not self.target.hwnd:
            raise ConfigError("请先选择一个要截图的应用窗口")
        if not str(self.output_dir).strip():
            raise ConfigError("请先选择截图保存目录")
        if self.interval < 0.1:
            raise ConfigError("间隔时间不能小于 0.1 秒")
        if self.interval > 86400:
            raise ConfigError("间隔时间过大（最大 86400 秒）")
        if self.start_delay < 0:
            raise ConfigError("首张延迟不能为负数")
        if self.max_shots < 0:
            raise ConfigError("截图数量不能为负数")
        if self.max_duration < 0:
            raise ConfigError("最长运行时长不能为负数")
        if self.method not in (w.METHOD_AUTO, w.METHOD_WGC, w.METHOD_PRINTWINDOW, w.METHOD_SCREEN):
            raise ConfigError(f"未知的截图方式：{self.method}")
        if self.folder_mode not in (FOLDER_APP, FOLDER_SESSION, FOLDER_FLAT):
            raise ConfigError(f"未知的子目录方式：{self.folder_mode}")
        fmt = self.image_format.lower().lstrip(".")
        if fmt not in _VALID_EXT:
            raise ConfigError(f"不支持的图片格式：{self.image_format}")
        if not (1 <= self.jpeg_quality <= 100):
            raise ConfigError("JPG 质量需要在 1~100 之间")

    @property
    def normalized_format(self) -> str:
        fmt = self.image_format.lower().lstrip(".")
        return "jpg" if fmt == "jpeg" else fmt


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^{}]*)?\}")
KNOWN_PLACEHOLDERS = ("app", "index", "hwnd", "date", "time", "datetime", "ms")
DEFAULT_PATTERN = "{app}_{date}_{time}_{index:04d}"


def unknown_placeholders(pattern: str) -> list:
    """返回模板里无法识别的占位符名称（用于给出提示）。"""
    used = {m.group(1) for m in _PLACEHOLDER_RE.finditer(str(pattern or ""))}
    return sorted(used.difference(KNOWN_PLACEHOLDERS))


def sanitize_filename_part(text: str, max_length: int = 60) -> str:
    """把窗口标题等文本变成安全的文件名片段。"""
    text = _ILLEGAL_CHARS.sub("_", str(text or "")).strip(" .")
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = "capture"
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text


class _SafeDict(dict):
    """文件名模板里出现未知占位符时原样保留，而不是抛异常。"""

    def __missing__(self, key):  # pragma: no cover - 简单分支
        return "{" + key + "}"


def build_filename(
    pattern: str,
    app: str,
    index: int,
    hwnd: int,
    when: datetime,
    extension: str,
) -> str:
    values = _SafeDict(
        app=sanitize_filename_part(app),
        index=index,
        hwnd=f"{hwnd:X}",
        date=when.strftime("%Y%m%d"),
        time=when.strftime("%H%M%S"),
        datetime=when.strftime("%Y%m%d_%H%M%S"),
        ms=f"{when.microsecond // 1000:03d}",
    )
    try:
        name = str(pattern).format_map(values)
    except (ValueError, IndexError, KeyError):
        name = ""
    if not name or "{" in name or "}" in name:
        # 模板写错（未知占位符或非法格式说明）时回退到默认命名，避免生成奇怪的文件名
        name = DEFAULT_PATTERN.format_map(values)
    name = sanitize_filename_part(name, max_length=150)
    if not name:
        name = f"capture_{index:04d}"
    return f"{name}.{extension}"


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem, ext = os.path.splitext(filename)
    for i in range(1, 10000):
        candidate = directory / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{int(time.time())}{ext}"


def image_digest(image: Image.Image, size: int = 48) -> str:
    """快速感知哈希：用于判断两张截图画面是否基本一致。"""
    small = image.convert("L").resize((size, size), Image.Resampling.BILINEAR)
    return hashlib.md5(small.tobytes()).hexdigest()


def resolve_target(target: Target) -> Target:
    """把目标解析成当前真实存在的窗口句柄。"""
    if target.kind == TARGET_SCREEN:
        return Target(kind=TARGET_SCREEN, title=target.title or "整个屏幕")
    hwnd = int(target.hwnd or 0)
    if hwnd and w.is_window(hwnd):
        info = w.window_info(hwnd)
        return Target(kind=TARGET_WINDOW, hwnd=hwnd, title=info.title,
                      app_label=target.app_label or info.process_name or info.title)
    if target.title:
        matches = w.find_windows_by_title(target.title)
        if matches:
            info = matches[0]
            return Target(kind=TARGET_WINDOW, hwnd=info.hwnd, title=info.title,
                          app_label=target.app_label or info.process_name or info.title)
    raise ConfigError("目标窗口不存在或已关闭，请刷新列表后重新选择")


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
EventCallback = Callable[[Dict], None]


class CaptureEngine:
    """按固定节奏循环截图，可随时停止。"""

    def __init__(self, config: CaptureConfig, on_event: Optional[EventCallback] = None):
        self.config = config
        self._on_event = on_event or (lambda event: None)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_digest: Optional[str] = None
        self._target: Optional[Target] = None
        self._session = None          # WGC 常驻会话
        self._session_key: tuple = ()
        self._backend = ""
        # —— 运行中热更新 ——
        self._config_lock = threading.Lock()
        self._need_session_reinit = False    # 方式/客户区/光标变了 → 重建 WGC 会话
        self._need_dir_reinit = False        # 输出目录/子目录方式变了 → 换目录
        self._need_timing_reinit = False     # 间隔变了 → 从当前时刻重新计时
        self._directory: Optional[Path] = None
        self._manifest_path: Optional[Path] = None
        self._manifest_file = None
        self._manifest_writer = None

    # -- 生命周期 ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("截图任务已经在运行")
        self.config.validate()
        self._stop_event.clear()
        self._last_digest = None
        self._thread = threading.Thread(target=self._run, name="capture-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- 运行中改参数：下一个循环就生效，不用停止任务 --
    _SESSION_KEYS = ("method", "client_only", "capture_cursor")
    _DIR_KEYS = ("output_dir", "folder_mode")

    def apply_config(self, **changes) -> List[str]:
        """即时修改正在运行的任务参数（间隔、方式、张数、目录…）。

        返回真正发生变化的字段名；界面用它提示"已即时生效"。
        涉及 WGC 会话 / 输出目录的改动会打标记，由截图线程在下一轮安全地重建
        （不能在这里直接动会话：WGC 对象是 COM 单元线程绑定的）。
        """
        applied: List[str] = []
        with self._config_lock:
            for key, value in changes.items():
                if not hasattr(self.config, key):
                    continue
                if getattr(self.config, key) == value:
                    continue
                setattr(self.config, key, value)
                applied.append(key)
            if any(key in applied for key in self._SESSION_KEYS):
                self._need_session_reinit = True
            if any(key in applied for key in self._DIR_KEYS):
                self._need_dir_reinit = True
            if "interval" in applied or "start_delay" in applied:
                self._need_timing_reinit = True
        return applied

    # -- WGC 会话 ---------------------------------------------------------
    def _open_session(self) -> None:
        """窗口目标 + auto/wgc 时，建立常驻的 WGC 会话（可截被遮挡/后台窗口）。"""
        cfg = self.config
        target = self._target
        self._session = None
        self._session_key = ()
        if target is None or target.kind != TARGET_WINDOW:
            return
        if cfg.method not in (w.METHOD_AUTO, w.METHOD_WGC):
            return
        if not wgc.available():
            if cfg.method == w.METHOD_WGC:
                raise ConfigError(f"WGC 不可用：{wgc.unavailable_reason()}")
            logger.info("WGC 不可用，改用其它截图方式：%s", wgc.unavailable_reason())
            return
        if not w.is_window_responsive(target.hwnd):
            message = ("目标窗口当前没有响应（线程未取消息），已改用 GDI 截图方式；"
                       "等窗口恢复后重新开始即可用 WGC。")
            if cfg.method == w.METHOD_WGC:
                raise ConfigError("目标窗口当前没有响应，无法建立 WGC 会话")
            logger.warning(message)
            self._emit({"type": "warning", "message": message})
            return
        try:
            info = w.window_info(target.hwnd)
            target.title = target.title or info.title
            target.class_name = info.class_name
            self._session = wgc.WgcSession(
                info.title, info.class_name,
                client_only=cfg.client_only, capture_cursor=cfg.capture_cursor,
                hwnd=target.hwnd,
            )
            self._session_key = (info.title, info.class_name)
            self._backend = "WGC"
            logger.info("已建立 WGC 捕获会话：%s（%s）", info.title, info.class_name)
        except Exception as exc:
            self._session = None
            if cfg.method == w.METHOD_WGC:
                raise ConfigError(f"WGC 会话创建失败：{exc}")
            logger.warning("WGC 会话创建失败，改用 GDI 方式：%s", exc)
            advice = w.integrity_advice()
            self._emit({
                "type": "warning",
                "message": f"WGC 不可用（{exc}），已改用 GDI 截图：窗口被遮挡时会截到遮挡窗口。"
                           + (advice if advice else
                              "若提示“没有捕获权限”，一般是程序运行权限受限，"
                              "可改用「屏幕区域」方式，或直接用普通用户身份运行程序。"),
            })

    def _recreate_session(self, reason: str) -> None:
        self._close_session()
        try:
            if self._target is None or not wgc.available():
                return
            if not w.is_window_responsive(self._target.hwnd):
                logger.warning("目标窗口没有响应，暂不重建 WGC 会话")
                return
            info = w.window_info(self._target.hwnd)
            self._session = wgc.WgcSession(
                info.title, info.class_name,
                client_only=self.config.client_only,
                capture_cursor=self.config.capture_cursor,
                hwnd=self._target.hwnd,
            )
            self._session_key = (info.title, info.class_name)
            logger.info("已重建 WGC 会话（%s）：%s", reason, info.title)
        except Exception as exc:
            self._session = None
            logger.warning("重建 WGC 会话失败：%s", exc)

    def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # pragma: no cover
                pass

    # -- 输出目录 / 清单 ---------------------------------------------------
    def _folder_label(self) -> str:
        """文件夹名：取「窗口标题」（不同标题 → 不同目录）。

        屏幕目标固定用「整个屏幕」；标题为空（极少数无标题窗口）时退回进程名 / 句柄。
        """
        target = self._target
        if target is None:
            return "capture"
        if target.kind == TARGET_SCREEN:
            return "整个屏幕"
        title = sanitize_filename_part(target.title, 80) if target.title else ""
        if title and title != "capture":
            return title
        label = sanitize_filename_part(target.app_label, 60) if target.app_label else ""
        if label and label != "capture":
            return label
        return f"窗口0x{int(target.hwnd or 0):X}"

    def _app_label(self) -> str:
        """文件名里 {app} 用的标签：进程名（稳定、跨标题不变）。"""
        target = self._target
        if target is None:
            return "capture"
        if target.kind == TARGET_SCREEN:
            return "screen"
        return target.app_label or sanitize_filename_part(target.title, 40) or "window"

    def _resolve_directory(self) -> Path:
        """按子目录方式算出这次的保存目录。"""
        base = Path(self.config.output_dir).expanduser()
        folder = self._folder_label()
        if self.config.folder_mode == FOLDER_SESSION:
            return base / f"{sanitize_filename_part(folder, 40)}_{datetime.now():%Y%m%d_%H%M%S}"
        if self.config.folder_mode == FOLDER_APP:
            # 同一个窗口标题始终用同一个文件夹（已存在就直接用，不再新建）
            return base / folder
        return base

    def _open_manifest(self) -> None:
        self._close_manifest()
        if not self.config.write_manifest or self._directory is None:
            return
        self._manifest_path = self._directory / "capture_manifest.csv"
        try:
            self._manifest_file = open(self._manifest_path, "a", newline="", encoding="utf-8-sig")
            self._manifest_writer = csv.writer(self._manifest_file)
            if self._manifest_file.tell() == 0:
                self._manifest_writer.writerow(
                    ["序号", "时间", "文件名", "宽", "高", "截图方式", "是否保存", "备注"]
                )
                self._manifest_file.flush()
        except Exception as exc:  # pragma: no cover
            logger.warning("无法写入清单：%s", exc)
            self._manifest_file = None
            self._manifest_writer = None

    def _close_manifest(self) -> None:
        if self._manifest_file is not None:
            try:
                self._manifest_file.flush()
                self._manifest_file.close()
            except Exception:  # pragma: no cover
                pass
        self._manifest_file = None
        self._manifest_writer = None

    def _switch_directory(self) -> None:
        """输出目录或子目录方式变了：安全地换到新目录。"""
        old = self._directory
        self._directory = self._resolve_directory()
        if old == self._directory:
            return
        self._close_manifest()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._open_manifest()
        logger.info("保存目录已即时切换：%s", self._directory)
        self._emit({"type": "info", "message": f"保存目录已切换到：{self._directory}"})

    def _reopen_manifest_if_needed(self) -> None:
        """清单开关被改动的处理。"""
        if self.config.write_manifest and self._manifest_writer is None:
            self._open_manifest()
        elif not self.config.write_manifest and self._manifest_file is not None:
            self._close_manifest()

    # -- 内部实现 ---------------------------------------------------------
    def _emit(self, event: Dict) -> None:
        self._log_event(event)
        try:
            self._on_event(event)
        except Exception:  # 回调异常不能拖垮截图线程
            pass

    @staticmethod
    def _log_event(event: Dict) -> None:
        """所有事件同时写进本地日志文件，界面看到什么日志里就有什么。"""
        kind = event.get("type")
        try:
            if kind == "started":
                logger.info("开始截图：目标=%s｜间隔=%s 秒｜方式=%s｜输出=%s",
                            event.get("target"), event.get("interval"),
                            event.get("method"), event.get("output_dir"))
            elif kind == "shot":
                logger.info("已保存第 %s 张：%s（%sx%s，%s，%s ms，%.1f KB）",
                            event.get("total_saved"), event.get("filename"),
                            event.get("width"), event.get("height"), event.get("method"),
                            event.get("took_ms"), event.get("bytes", 0) / 1024)
            elif kind == "skip":
                logger.info("跳过第 %s 次：%s（已跳过 %s 张）",
                            event.get("index"), event.get("reason"), event.get("total_skipped"))
            elif kind == "waiting":
                pass
            elif kind == "error":
                logger.error("%s", event.get("message"))
            elif kind == "info":
                logger.info("%s", event.get("message"))
            elif kind == "warning":
                logger.warning("%s", event.get("message"))
            elif kind == "finished":
                logger.info("结束：%s（保存 %s 张，跳过 %s 张，尝试 %s 次，失败 %s 次）",
                            event.get("reason"), event.get("saved"), event.get("skipped"),
                            event.get("attempts"), event.get("failures"))
        except Exception:  # pragma: no cover
            pass

    def _sleep(self, seconds: float) -> None:
        """可被 stop() 或「参数变更」立刻打断的等待。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop_event.is_set():
            if self._need_timing_reinit:
                return          # 间隔被改了，马上回去重新计时
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop_event.wait(min(remaining, 0.25))

    def _capture_once(self):
        """抓一帧：窗口目标优先用 WGC（无视遮挡），失败再退回到 GDI / 屏幕区域。"""
        target = self._target
        assert target is not None
        if target.kind == TARGET_SCREEN:
            return w.capture_screen_full(), w.METHOD_SCREEN

        cfg = self.config
        if self._session is not None:
            # 窗口标题变了（浏览器换标签、游戏改标题…）WGC 会话要重建
            try:
                info = w.window_info(target.hwnd)
                if not self._session.matches(info.title, info.class_name):
                    self._recreate_session("窗口标题变化")
            except Exception:
                pass
            image = self._session.grab()
            if image is not None:
                return image, w.METHOD_WGC
            if self._session.failures >= MAX_WGC_FAILURES:
                self._recreate_session(f"连续 {self._session.failures} 次抓帧失败")
            if cfg.method == w.METHOD_WGC:
                detail = self._session.last_error() if self._session else ""
                raise w.CaptureError(
                    "WGC 抓帧失败" + (f"（{detail}）" if detail else "")
                    + "；窗口可能已最小化或停止了渲染"
                )
            if self._session is None and cfg.method == w.METHOD_WGC:
                raise w.CaptureError("WGC 会话不可用")

        if cfg.method == w.METHOD_WGC:
            raise w.CaptureError(f"WGC 不可用：{wgc.unavailable_reason()}")

        return w.capture_window(
            target.hwnd,
            method=cfg.method,
            client_only=cfg.client_only,
            activate=cfg.activate_before_capture,
        )

    def _save(self, image: Image.Image, index: int, directory: Path, app: str) -> Path:
        cfg = self.config
        when = datetime.now()
        filename = build_filename(
            cfg.filename_pattern, app, index, self._target.hwnd if self._target else 0,
            when, cfg.normalized_format,
        )
        path = unique_path(directory, filename)
        fmt = cfg.normalized_format
        if fmt == "jpg":
            image.convert("RGB").save(path, "JPEG", quality=int(cfg.jpeg_quality), subsampling=0)
        elif fmt == "bmp":
            image.save(path, "BMP")
        elif fmt == "webp":
            image.save(path, "WEBP", quality=int(cfg.jpeg_quality))
        else:
            image.save(path, "PNG")
        return path

    def _run(self) -> None:  # noqa: C901 - 主循环逻辑集中在此，便于阅读
        cfg = self.config
        stats = {"attempts": 0, "saved": 0, "skipped": 0, "failures": 0}
        reason = "已停止"
        try:
            self._target = resolve_target(cfg.target)
            self._directory = self._resolve_directory()
            self._directory.mkdir(parents=True, exist_ok=True)
            self._open_manifest()
        except Exception as exc:
            self._emit({"type": "error", "message": f"启动失败：{exc}", "fatal": True})
            self._emit({"type": "finished", "reason": "启动失败", **stats})
            return

        try:
            self._open_session()
        except Exception as exc:
            self._emit({"type": "error", "message": f"启动失败：{exc}", "fatal": True})
            self._emit({"type": "finished", "reason": "启动失败", **stats})
            return

        if self._session is not None:
            self._emit({"type": "info",
                        "message": "已启用 WGC 捕获：窗口被遮挡、在后台也能截到它自己的画面。"})
        elif self.config.method == w.METHOD_AUTO and self._target.kind == TARGET_WINDOW:
            reason_text = wgc.unavailable_reason() if not wgc.available() else "创建会话失败"
            self._emit({"type": "warning",
                        "message": f"WGC 不可用（{reason_text}），已改用 GDI 截图，"
                                   "被遮挡时会截到遮挡窗口。"})
        if self.config.method == w.METHOD_SCREEN and self.config.activate_before_capture:
            self._emit({"type": "info",
                        "message": "屏幕区域方式：每次截图前会把目标窗口切到前台，避免被其它窗口挡住。"})

        self._emit({
            "type": "started",
            "target": self._target.describe(),
            "output_dir": str(self._directory),
            "interval": cfg.interval,
            "method": cfg.method,
        })

        started_at = time.monotonic()
        next_deadline = time.monotonic() + cfg.start_delay
        index = 0

        try:
            if cfg.start_delay > 0:
                self._emit({"type": "waiting", "next_at": next_deadline, "interval": cfg.interval})
                self._sleep(cfg.start_delay)

            while not self._stop_event.is_set():
                # —— 运行中被改了参数：在这里安全地应用（不打断任务）——
                if self._need_dir_reinit:
                    self._need_dir_reinit = False
                    self._switch_directory()
                if self._need_session_reinit:
                    self._need_session_reinit = False
                    self._recreate_session("参数变更")
                if self._need_timing_reinit:
                    # 间隔/延迟刚被改过：立刻按新间隔重新计时（下一张马上开始）
                    self._need_timing_reinit = False
                    next_deadline = time.monotonic()
                self._reopen_manifest_if_needed()

                if cfg.max_shots and stats["saved"] >= cfg.max_shots:
                    reason = f"已达到设定的截图数量（{cfg.max_shots} 张）"
                    break
                if cfg.max_duration and (time.monotonic() - started_at) >= cfg.max_duration:
                    reason = f"已达到设定的运行时长（{cfg.max_duration:g} 秒）"
                    break

                index += 1
                stats["attempts"] += 1
                started = time.monotonic()
                try:
                    image, used_method = self._capture_once()
                except w.CaptureError as exc:
                    stats["failures"] += 1
                    self._emit({"type": "error", "message": f"第 {index} 次截图失败：{exc}",
                                "fatal": False})
                    if stats["failures"] >= MAX_CONSECUTIVE_FAILURES:
                        reason = f"连续 {stats['failures']} 次截图失败，已停止"
                        break
                except Exception as exc:  # pragma: no cover - 兜底
                    stats["failures"] += 1
                    self._emit({"type": "error", "message": f"第 {index} 次截图异常：{exc}",
                                "fatal": False})
                    if stats["failures"] >= MAX_CONSECUTIVE_FAILURES:
                        reason = "出现无法恢复的错误，已停止"
                        break
                else:
                    stats["failures"] = 0
                    digest = image_digest(image)
                    unchanged = cfg.skip_unchanged and digest == self._last_digest
                    took_ms = int((time.monotonic() - started) * 1000)
                    if unchanged:
                        stats["skipped"] += 1
                        if self._manifest_writer:
                            self._manifest_writer.writerow([
                                index, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "", image.width, image.height, used_method, 0, "画面无变化，已跳过",
                            ])
                            self._manifest_file.flush()
                        self._emit({
                            "type": "skip", "index": index, "reason": "画面无变化",
                            "total_skipped": stats["skipped"], "took_ms": took_ms,
                        })
                    else:
                        try:
                            path = self._save(image, stats["saved"] + 1, self._directory,
                                              self._app_label())
                        except Exception as exc:
                            stats["failures"] += 1
                            self._emit({"type": "error", "message": f"保存图片失败：{exc}",
                                        "fatal": False})
                            if stats["failures"] >= MAX_CONSECUTIVE_FAILURES:
                                reason = "图片无法写入磁盘，已停止"
                                break
                        else:
                            self._last_digest = digest
                            stats["saved"] += 1
                            size = path.stat().st_size
                            if self._manifest_writer:
                                self._manifest_writer.writerow([
                                    index, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    path.name, image.width, image.height, used_method, 1, "",
                                ])
                                self._manifest_file.flush()
                            self._emit({
                                "type": "shot",
                                "index": index,
                                "path": str(path),
                                "filename": path.name,
                                "width": image.width,
                                "height": image.height,
                                "method": used_method,
                                "took_ms": took_ms,
                                "bytes": size,
                                "total_saved": stats["saved"],
                                "total_skipped": stats["skipped"],
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            })

                now = time.monotonic()
                next_deadline += cfg.interval
                if next_deadline <= now:
                    next_deadline = now  # 单张耗时超过间隔时不再追赶，避免连拍
                self._emit({"type": "waiting", "next_at": next_deadline, "interval": cfg.interval})
                self._sleep(next_deadline - now)
        finally:
            self._close_session()
            self._close_manifest()
            self._emit({
                "type": "finished",
                "reason": reason,
                "output_dir": str(self._directory),
                "manifest": str(self._manifest_path) if cfg.write_manifest and self._manifest_path else "",
                **stats,
            })
