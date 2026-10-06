# -*- coding: utf-8 -*-
"""录屏取帧：用 GDI 把屏幕区域快速抓成 numpy 图像（BGRA）。

为什么不用 WGC：录制要求**稳定、可缩放、低延迟**的连续取帧，
GDI 的 StretchBlt 可以在一次拷贝里同时完成"截取 + 缩小"，
并且不受窗口遮挡之外的额外限制；窗口内容用屏幕位块传输
(BitBlt + CAPTUREBLT) 取，兼容性最好。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

from .. import win32 as w
from .quality import even

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

__all__ = ["FrameGrabber", "GrabError", "get_cursor_pos"]

gdi32 = w.gdi32
user32 = w.user32

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HALFTONE = 4
COLORONCOLOR = 3
DIB_RGB_COLORS = 0
DI_NORMAL = 0x0003
CURSOR_SHOWING = 0x0001

w._proto(gdi32.StretchBlt, [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD,
], wintypes.BOOL)
w._proto(gdi32.SetStretchBltMode, [wintypes.HDC, ctypes.c_int], ctypes.c_int)
w._proto(gdi32.GdiFlush, [], wintypes.BOOL)
w._proto(user32.DrawIconEx, [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
    ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT,
], wintypes.BOOL)
w._proto(user32.CopyIcon, [wintypes.HICON], wintypes.HICON)
w._proto(user32.DestroyIcon, [wintypes.HICON], wintypes.BOOL)
w._proto(user32.GetIconInfo, [wintypes.HICON, ctypes.c_void_p], wintypes.BOOL)
w._proto(user32.GetCursorInfo, [ctypes.c_void_p], wintypes.BOOL)


# 缩放/合成方式（影响画面质量与帧率上限）
#
# 下面是本机（2560x1440 单屏，GDI 取帧）实测的"取帧 + 编码"耗时上限，供界面提示用：
#   输出        高画质   高帧率
#   1920x1080   19 fps   29 fps
#   1280x720    29 fps   45 fps
#   854x480     30 fps   52 fps
# 可见 GDI 拷贝本身就有约 20~33ms 的固定开销，60fps 只在较低分辨率下才可能达到。
SCALE_QUALITY = "quality"   # HALFTONE + CAPTUREBLT：缩小时抗锯齿更好（文字更清晰），较慢
SCALE_FAST = "fast"         # COLORONCOLOR + 纯 SRCCOPY：更快，适合高帧率或低配机器
SCALE_CHOICES = (SCALE_QUALITY, SCALE_FAST)
SCALE_LABELS = {
    SCALE_QUALITY: "高画质（文字更清晰）",
    SCALE_FAST: "高帧率（更快）",
}


class GrabError(RuntimeError):
    """取帧失败。"""


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


def get_cursor_pos() -> Tuple[int, int]:
    """当前鼠标位置（物理像素）。"""
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if user32.GetCursorInfo(ctypes.byref(info)):
        return int(info.ptScreenPos.x), int(info.ptScreenPos.y)
    return 0, 0


# ---------------------------------------------------------------------------
# 高精度等待
# ---------------------------------------------------------------------------
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_ALL_ACCESS = 0x1F0003
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000

kernel32 = w.kernel32
w._proto(kernel32.CreateWaitableTimerExW,
         [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE)
w._proto(kernel32.SetWaitableTimer,
         [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), ctypes.c_long, ctypes.c_void_p,
          ctypes.c_void_p, wintypes.BOOL], wintypes.BOOL)
w._proto(kernel32.WaitForSingleObject, [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD)
w._proto(kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
w._proto(kernel32.CreateWaitableTimerW, [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR],
         wintypes.HANDLE)


class HighResTimer:
    """高精度等待定时器：把帧率节流做到毫秒级。

    直接 `time.sleep` 在 Windows 上受系统时钟粒度限制（常见 15.6 ms），
    录 60fps 时会导致每帧都睡过头、实际帧率掉到 30fps 左右；
    这里用创建时带 HIGH_RESOLUTION 标志的可等待定时器，精度可达毫秒以下。
    """

    def __init__(self) -> None:
        self._handle = kernel32.CreateWaitableTimerExW(
            None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS)
        if not self._handle:
            # 老系统（Windows 10 1803 以前）没有这个标志，退回普通定时器
            self._handle = kernel32.CreateWaitableTimerW(None, False, None)

    @property
    def available(self) -> bool:
        return bool(self._handle)

    def wait(self, seconds: float) -> None:
        """等待指定秒数（阻塞）。"""
        if not self._handle or seconds <= 0:
            return
        due = ctypes.c_longlong(-int(seconds * 10_000_000))   # 负值 = 相对时间（100ns 单位）
        if not kernel32.SetWaitableTimer(self._handle, ctypes.byref(due), 0, None, None, False):
            time.sleep(seconds)
            return
        result = kernel32.WaitForSingleObject(self._handle, INFINITE)
        if result != WAIT_OBJECT_0:
            time.sleep(seconds)

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "HighResTimer":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class FrameGrabber:
    """一块可复用的 DIB 画布：把屏幕任意矩形缩放到固定输出尺寸。

    每次 `grab()` 返回的 numpy 数组**复用同一块内存**，调用方需要在
    下一次 grab 之前用掉（编码器是同步写入的，所以没问题）。
    """

    def __init__(self, width: int, height: int, draw_cursor: bool = True,
                 scale_mode: str = SCALE_QUALITY):
        if np is None:  # pragma: no cover
            raise GrabError("取帧需要 numpy 支持")
        self.width = even(width)
        self.height = even(height)
        if self.width < 16 or self.height < 16:
            raise GrabError(f"输出尺寸过小：{self.width}x{self.height}")
        self.draw_cursor = draw_cursor
        self.scale_mode = scale_mode if scale_mode in SCALE_CHOICES else SCALE_QUALITY
        self._memdc = None
        self._hbitmap = None
        self._old = None
        self._bits = ctypes.c_void_p()
        self._frame = None
        self._open()

    # ---------------- 资源 ----------------
    def _open(self) -> None:
        self._memdc = gdi32.CreateCompatibleDC(None)
        if not self._memdc:
            raise GrabError("CreateCompatibleDC 失败")
        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.width
        bmi.bmiHeader.biHeight = -self.height      # 负值 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0            # BI_RGB
        self._hbitmap = gdi32.CreateDIBSection(
            self._memdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(self._bits), None, 0)
        if not self._hbitmap:
            self.close()
            raise GrabError("CreateDIBSection 失败（分辨率可能过大）")
        self._old = gdi32.SelectObject(self._memdc, self._hbitmap)
        gdi32.SetStretchBltMode(self._memdc, HALFTONE if self.scale_mode == SCALE_QUALITY
                                else COLORONCOLOR)
        size = self.width * self.height * 4
        raw = ctypes.string_at(self._bits, size)
        self._frame = np.frombuffer(raw, dtype=np.uint8).reshape(self.height, self.width, 4)

    def set_scale_mode(self, mode: str) -> None:
        """切换缩放方式（录制过程中也能立即生效）。"""
        if mode not in SCALE_CHOICES:
            return
        self.scale_mode = mode
        if self._memdc:
            gdi32.SetStretchBltMode(self._memdc, HALFTONE if mode == SCALE_QUALITY
                                    else COLORONCOLOR)

    def resize(self, width: int, height: int) -> None:
        """输出尺寸变化时重建画布。"""
        width, height = even(width), even(height)
        if width == self.width and height == self.height:
            return
        self.close()
        self.width, self.height = width, height
        self._open()

    def close(self) -> None:
        if self._memdc:
            if self._old:
                gdi32.SelectObject(self._memdc, self._old)
            if self._hbitmap:
                gdi32.DeleteObject(self._hbitmap)
            gdi32.DeleteDC(self._memdc)
        self._memdc = None
        self._hbitmap = None
        self._old = None
        self._frame = None

    def __enter__(self) -> "FrameGrabber":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------- 取帧 ----------------
    def grab(self, rect: Tuple[int, int, int, int]) -> "np.ndarray":
        """抓取屏幕矩形 (left, top, right, bottom) 并缩放到输出尺寸。"""
        left, top, right, bottom = (int(v) for v in rect)
        src_w, src_h = right - left, bottom - top
        if src_w <= 0 or src_h <= 0:
            raise GrabError("抓取区域为空（窗口可能已最小化）")
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise GrabError("获取屏幕设备上下文失败")
        try:
            # CAPTUREBLT 能抓到分层窗口，但每次拷贝都要过一遍 DWM 合成，明显更慢；
            # 高帧率模式因此退化为纯 SRCCOPY（实测 720p 从 29ms 降到 19ms）。
            rop = SRCCOPY | (CAPTUREBLT if self.scale_mode == SCALE_QUALITY else 0)
            if src_w == self.width and src_h == self.height:
                ok = gdi32.BitBlt(self._memdc, 0, 0, self.width, self.height,
                                  screen_dc, left, top, rop)
            else:
                ok = gdi32.StretchBlt(self._memdc, 0, 0, self.width, self.height,
                                      screen_dc, left, top, src_w, src_h, rop)
            if not ok:
                raise GrabError("屏幕拷贝失败（BitBlt/StretchBlt 返回 0）")
            if self.draw_cursor:
                self._draw_cursor(left, top, src_w, src_h)
            gdi32.GdiFlush()
        finally:
            user32.ReleaseDC(None, screen_dc)
        return self._frame

    def _draw_cursor(self, left: int, top: int, src_w: int, src_h: int) -> None:
        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(info)):
            return
        if not (info.flags & CURSOR_SHOWING) or not info.hCursor:
            return
        icon = user32.CopyIcon(info.hCursor)
        if not icon:
            return
        try:
            icon_info = ICONINFO()
            if not user32.GetIconInfo(icon, ctypes.byref(icon_info)):
                return
            if icon_info.hbmColor:
                gdi32.DeleteObject(icon_info.hbmColor)
            if icon_info.hbmMask:
                gdi32.DeleteObject(icon_info.hbmMask)
            # 光标位置从屏幕坐标换算到输出坐标
            scale_x = self.width / float(src_w)
            scale_y = self.height / float(src_h)
            x = int(round((info.ptScreenPos.x - left - icon_info.xHotspot) * scale_x))
            y = int(round((info.ptScreenPos.y - top - icon_info.yHotspot) * scale_y))
            user32.DrawIconEx(self._memdc, x, y, icon, 0, 0, 0, None, DI_NORMAL)
        finally:
            user32.DestroyIcon(icon)


def source_rect_for(config, target) -> Optional[Tuple[int, int, int, int]]:
    """按配置解析当前要抓取的屏幕矩形。"""
    from ..engine import TARGET_SCREEN, TARGET_WINDOW

    if target.kind == TARGET_SCREEN:
        return w.get_virtual_screen_rect()
    if target.kind == TARGET_WINDOW:
        hwnd = int(target.hwnd)
        if not w.is_window(hwnd):
            return None
        rect = w.get_window_rect_extended(hwnd)
        return _clamp_to_desktop(rect)
    return None


def _clamp_to_desktop(rect: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """把矩形裁剪到虚拟桌面范围内。"""
    vx1, vy1, vx2, vy2 = w.get_virtual_screen_rect()
    left, top, right, bottom = (int(v) for v in rect)
    left, top = max(left, vx1), max(top, vy1)
    right, bottom = min(right, vx2), min(bottom, vy2)
    if right - left < 16:
        right = min(vx2, left + 16)
    if bottom - top < 16:
        bottom = min(vy2, top + 16)
    return left, top, right, bottom
