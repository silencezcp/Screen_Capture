# -*- coding: utf-8 -*-
"""基于 Windows Graphics Capture（WGC）的窗口捕获后端。

WGC 是 Windows 10 1903+ 系统自带的窗口捕获接口，直接抓 GPU 合成输出，
所以「窗口被遮挡 / 在后台 / 最小化」都能截到窗口自己的画面——这正是
GDI 的 PrintWindow / BitBlt 做不到的事（会被遮挡物或黑屏污染）。

本模块用 wgc_python（MIT，包内自带 wgc_python.dll）实现，思路与
ok-script / ok-ww 这类游戏自动化框架一致：
* 会话常驻：一次创建，之后按需 capture_one()，内部自动 Pause/Resume，
  待机时 GPU 零开销，避免频繁创建/销毁 WGC 会话的 50ms 开销；
* 返回 BGRA numpy 帧，这里转成 PIL Image 交给引擎保存。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Optional

from PIL import Image

from . import win32 as w

__all__ = ["WGC_AVAILABLE", "WgcError", "WgcSession", "available", "unavailable_reason"]

# Windows.Graphics.Capture 从 Windows 10 1803（build 17134）开始提供：
# Windows Server 2016 = 14393、Server 2012 R2 / Win8.1 更低，都没有这个 API。
# 在这些系统上必须直接判定为不可用，否则会去激活不存在的 WinRT 类。
WGC_MIN_BUILD = 17134


def os_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except Exception:  # pragma: no cover - 非 Windows
        return 0


_IMPORT_ERROR: Optional[BaseException] = None
try:  # pragma: no cover - 取决于运行环境是否装了 wgc_python
    from wgc_python import WindowCapture, get_last_error  # type: ignore

    WGC_AVAILABLE = True
except BaseException as exc:  # pragma: no cover
    WindowCapture = None  # type: ignore
    WGC_AVAILABLE = False
    _IMPORT_ERROR = exc

_OS_TOO_OLD = bool(WGC_AVAILABLE and 0 < os_build() < WGC_MIN_BUILD)
if _OS_TOO_OLD:
    WGC_AVAILABLE = False


class WgcError(RuntimeError):
    """WGC 捕获失败。"""


def available() -> bool:
    return WGC_AVAILABLE


def unavailable_reason() -> str:
    if WGC_AVAILABLE:
        return ""
    if _OS_TOO_OLD:
        return (f"系统版本过低：Windows.Graphics.Capture 需要 Windows 10 1803"
                f"（build {WGC_MIN_BUILD}）及以上，当前 build {os_build()}"
                "（Windows Server 2016 / 早期 Win10 都不支持）")
    return f"未安装或不兼容 wgc_python（{_IMPORT_ERROR}）"


class WgcSession:
    """常驻的 WGC 捕获会话，可被截图线程反复调用。

    只能在同一个线程里使用（引擎的截图线程负责创建、抓帧和关闭）。
    """

    def __init__(
        self,
        title: str,
        class_name: str,
        client_only: bool = False,
        capture_cursor: bool = False,
        timeout: float = 0.6,
        hwnd: int = 0,
    ):
        if not WGC_AVAILABLE:
            raise WgcError(unavailable_reason())
        if not title and not class_name:
            raise WgcError("WGC 需要窗口标题或窗口类名")
        if hwnd and not w.is_window_responsive(int(hwnd)):
            # 目标线程不取消息时，建立会话会一直等下去（而且必须同线程建，没法用超时兜底）
            raise WgcError("目标窗口当前没有响应，跳过 WGC")
        self.title = title
        self.class_name = class_name
        self.client_only = client_only
        self.capture_cursor = capture_cursor
        self.timeout = timeout
        self.frames = 0
        self.failures = 0
        self._lock = threading.Lock()
        self._streaming = False
        self._capture = self._create()
        self.start_stream()

    # -- 内部 ------------------------------------------------------------
    def _create(self):
        """建立 WGC 会话。

        ⚠ WGC / WinRT 对象是 COM 单元线程绑定的：**必须**在哪个线程创建，
        就在哪个线程抓帧和 close。实测把创建动作放到子线程里（哪怕只是为了加超时），
        close() 时会直接触发 0xC0000005 访问违例把整个进程带走。
        目标窗口不响应时的卡死问题，交给调用方先用 win32.is_window_responsive() 探活。
        """
        try:
            return WindowCapture(
                self.title,
                self.class_name,
                client_area_only=self.client_only,
                capture_cursor=self.capture_cursor,
            )
        except Exception as exc:
            raise WgcError(f"创建 WGC 会话失败：{exc}") from exc

    @staticmethod
    def _to_image(frame) -> Image.Image:
        """BGRA numpy 帧 -> PIL Image。"""
        height, width = int(frame.shape[0]), int(frame.shape[1])
        return Image.frombytes("RGB", (width, height), frame.tobytes(), "raw", "BGRX", 0, 1)

    # -- 对外 ------------------------------------------------------------
    def matches(self, title: str, class_name: str) -> bool:
        """会话是按「标题 + 类名」绑定的，窗口标题变了就要重建。"""
        return self.title == title and self.class_name == class_name

    def grab(self, timeout: Optional[float] = None) -> Optional[Image.Image]:
        """抓最近一帧；失败返回 None（不抛异常，交给上层决定回退）。

        注意 WGC 是「内容变化才推帧」的：窗口静止时不会有新帧。
        所以这里不用 capture_one()（它每次都会 Pause，静止窗口下一次就拿不到帧），
        而是让会话保持 resume 状态，直接读最后一次缓存的帧——这正是
        ok-script 这类框架「常驻会话 + 随时取帧」的做法。
        """
        deadline = time.monotonic() + (timeout if timeout else self.timeout)
        with self._lock:
            if self._capture is None:
                return None
            while True:
                image = self._read_latest()
                if image is not None:
                    self.failures = 0
                    self.frames += 1
                    return image
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            # get_frame 拿不到时，退回库自带的一键抓帧
            image = self._capture_one(0.3)
            if image is not None:
                self.failures = 0
                self.frames += 1
                return image
            self.failures += 1
            return None

    def _read_latest(self) -> Optional[Image.Image]:
        """零拷贝读一帧（读完立刻 release），失败返回 None。"""
        try:
            result = self._capture.get_frame()
        except Exception:
            return None
        if not result:
            return None
        try:
            pointer, width, height, pitch = result
            data = ctypes.string_at(pointer, int(pitch) * int(height))
            return Image.frombytes(
                "RGB", (int(width), int(height)), data, "raw", "BGRX", int(pitch), 1
            )
        except Exception:
            return None
        finally:
            try:
                self._capture.release_frame()
            except Exception:  # pragma: no cover
                pass

    def _capture_one(self, timeout: float) -> Optional[Image.Image]:
        """备用路径：库里的一键抓帧（会自动 Pause/Resume）。"""
        try:
            frame = self._capture.capture_one(timeout)
        except Exception:
            return None
        if frame is None:
            return None
        try:
            return self._to_image(frame)
        except Exception:
            return None

    def start_stream(self) -> bool:
        """让会话保持运行，避免「静止窗口取不到帧」。"""
        try:
            self._capture.resume()
            self._streaming = True
            return True
        except Exception:
            self._streaming = False
            return False

    def last_error(self) -> str:
        if not WGC_AVAILABLE:
            return unavailable_reason()
        try:
            return str(get_last_error() or "")
        except Exception:  # pragma: no cover
            return ""

    def close(self) -> None:
        with self._lock:
            capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.close()
            except Exception:  # pragma: no cover
                pass
