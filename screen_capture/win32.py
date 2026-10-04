# -*- coding: utf-8 -*-
"""Windows 底层能力封装：窗口枚举、窗口矩形、窗口 / 屏幕截图。

设计目标：
* 只依赖标准库 + Pillow，不需要安装 pywin32；
* 支持两种截图方式：
  - PrintWindow：让窗口自己重绘到内存 DC，即使窗口被其它窗口遮挡也能截到内容；
  - 屏幕区域 BitBlt：直接从屏幕 DC 拷贝，所见即所得，对 GPU 渲染的窗口更可靠。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional, Tuple

if os.name != "nt":  # pragma: no cover - 明确报错，避免在非 Windows 上静默失败
    raise ImportError("screen_capture 只能在 Windows 上运行")

from PIL import Image

# ---------------------------------------------------------------------------
# DLL 与常量
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:  # pragma: no cover
    dwmapi = None
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:  # pragma: no cover
    shcore = None

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# 读窗口标题用的消息与超时
WM_NULL = 0x0000
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
WINDOW_TEXT_TIMEOUT_MS = 200

# 截图方式
METHOD_AUTO = "auto"
METHOD_PRINTWINDOW = "printwindow"
METHOD_SCREEN = "screen"
METHOD_WGC = "wgc"

# 窗口激活相关
SW_SHOW = 5
SW_RESTORE = 9
SW_MINIMIZE = 6


class CaptureError(RuntimeError):
    """截图过程中的可预期错误（窗口已关闭、区域无效、GDI 调用失败等）。"""


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _proto(func, argtypes, restype):
    func.argtypes = argtypes
    func.restype = restype
    return func


_HWND = wintypes.HWND
_HDC = wintypes.HDC
_HANDLE = ctypes.c_void_p

_proto(user32.EnumWindows, [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
_proto(user32.IsWindow, [_HWND], wintypes.BOOL)
_proto(user32.IsWindowVisible, [_HWND], wintypes.BOOL)
_proto(user32.IsIconic, [_HWND], wintypes.BOOL)
_proto(user32.GetWindowTextLengthW, [_HWND], ctypes.c_int)
_proto(user32.GetWindowTextW, [_HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_proto(
    user32.SendMessageTimeoutW,
    [_HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ssize_t)],
    ctypes.c_ssize_t,
)
_proto(user32.GetClassNameW, [_HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_proto(user32.GetWindowRect, [_HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
_proto(user32.GetWindowThreadProcessId, [_HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
_proto(user32.GetForegroundWindow, [], _HWND)
_proto(user32.PrintWindow, [_HWND, _HDC, wintypes.UINT], wintypes.BOOL)
_proto(user32.GetDC, [_HWND], _HDC)
_proto(user32.ReleaseDC, [_HWND, _HDC], ctypes.c_int)
_proto(user32.ShowWindow, [_HWND, ctypes.c_int], wintypes.BOOL)
_proto(user32.BringWindowToTop, [_HWND], wintypes.BOOL)
_proto(user32.SetForegroundWindow, [_HWND], wintypes.BOOL)
_proto(user32.GetForegroundWindow, [], _HWND)
_proto(user32.AttachThreadInput, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
_proto(kernel32.GetCurrentThreadId, [], wintypes.DWORD)
_proto(user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
_proto(user32.SetProcessDPIAware, [], wintypes.BOOL)
_proto(user32.GetDpiForSystem, [], wintypes.UINT)
_proto(
    user32.SetProcessDpiAwarenessContext, [ctypes.c_void_p], wintypes.BOOL
)
if hasattr(user32, "GetWindowLongPtrW"):
    _get_window_long = _proto(
        user32.GetWindowLongPtrW, [_HWND, ctypes.c_int], ctypes.c_ssize_t
    )
else:  # pragma: no cover - 32 位 Python
    _get_window_long = _proto(
        user32.GetWindowLongW, [_HWND, ctypes.c_int], ctypes.c_long
    )

_proto(gdi32.CreateCompatibleDC, [_HDC], _HDC)
_proto(gdi32.DeleteDC, [_HDC], wintypes.BOOL)
_proto(
    gdi32.CreateDIBSection,
    [_HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(_HANDLE), _HANDLE, wintypes.DWORD],
    _HANDLE,
)
_proto(gdi32.SelectObject, [_HDC, _HANDLE], _HANDLE)
_proto(gdi32.DeleteObject, [_HANDLE], wintypes.BOOL)
_proto(
    gdi32.BitBlt,
    [_HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     _HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD],
    wintypes.BOOL,
)

_proto(
    kernel32.OpenProcess,
    [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
    _HANDLE,
)
_proto(kernel32.CloseHandle, [_HANDLE], wintypes.BOOL)
_proto(kernel32.GetCurrentProcess, [], _HANDLE)
_proto(kernel32.LocalFree, [_HANDLE], _HANDLE)
_proto(
    advapi32.OpenProcessToken,
    [_HANDLE, wintypes.DWORD, ctypes.POINTER(_HANDLE)],
    wintypes.BOOL,
)
_proto(
    advapi32.GetTokenInformation,
    [_HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
    wintypes.BOOL,
)
_proto(
    advapi32.ConvertSidToStringSidW,
    [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)],
    wintypes.BOOL,
)
_proto(
    kernel32.QueryFullProcessImageNameW,
    [_HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
    wintypes.BOOL,
)

if dwmapi is not None:
    _proto(
        dwmapi.DwmGetWindowAttribute,
        [_HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD],
        ctypes.c_long,
    )


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> str:
    """让进程按物理像素工作，避免多显示器 / 缩放下坐标错位。"""
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        try:
            if user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            ):
                return "PerMonitorV2"
        except OSError:
            pass
    if shcore is not None:
        try:
            if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
                return "PerMonitor"
        except OSError:
            pass
    try:
        if user32.SetProcessDPIAware():
            return "System"
    except OSError:
        pass
    return "none"


def system_dpi() -> int:
    try:
        dpi = int(user32.GetDpiForSystem())
        if dpi > 0:
            return dpi
    except OSError:
        pass
    return 96


# ---------------------------------------------------------------------------
# 进程权限（完整性级别）
# ---------------------------------------------------------------------------
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25
_INTEGRITY_NAMES = {
    "S-1-16-0": "Untrusted",
    "S-1-16-4096": "Low",
    "S-1-16-8192": "Medium",
    "S-1-16-8448": "MediumPlus",
    "S-1-16-12288": "High",
    "S-1-16-16384": "System",
    "S-1-16-20480": "Protected",
}


def process_integrity() -> str:
    """当前进程的完整性级别：Low / Medium / High …

    WGC（Windows Graphics Capture）要求调用方完整性不低于被截窗口；程序如果因为
    文件带了「低完整性标签」而以 Low 权限运行，抓窗口就会报 0x80070005 没有权限。
    """
    token = _HANDLE()
    try:
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            return "unknown"
        need = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(need))
        if not need.value:
            return "unknown"
        buffer = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(
            token, TokenIntegrityLevel, buffer, need.value, ctypes.byref(need)
        ):
            return "unknown"
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            return "unknown"
        try:
            return _INTEGRITY_NAMES.get(sid_text.value, sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    except OSError:  # pragma: no cover
        return "unknown"
    finally:
        if token:
            kernel32.CloseHandle(token)


def integrity_advice() -> str:
    """低权限运行时给出的可操作建议（WGC 会被系统拒绝）。"""
    level = process_integrity()
    if level == "Low":
        return ("当前程序以「低完整性权限」运行（程序文件所在目录带 Low 完整性标签），"
                "Windows 会拒绝 WGC 抓窗口：把程序文件夹复制到桌面 / 文档等普通目录再运行即可。")
    return ""


# ---------------------------------------------------------------------------
# 窗口信息
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    process_name: str
    pid: int
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool = False

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def label(self) -> str:
        return self.title or self.process_name or f"0x{self.hwnd:X}"

    @property
    def size_text(self) -> str:
        return f"{self.width} x {self.height}"


_process_name_cache: dict = {}


def get_process_name(pid: int) -> str:
    if pid in _process_name_cache:
        return _process_name_cache[pid]
    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    _process_name_cache[pid] = name
    return name


def _window_text(hwnd: int, timeout_ms: int = WINDOW_TEXT_TIMEOUT_MS) -> str:
    """读取窗口标题，带超时。

    这里绝对不能用 GetWindowTextW：它会向目标窗口发送 WM_GETTEXT 并一直等下去。
    只要目标窗口所在线程没有在取消息（窗口“未响应”，或者我们自己进程的主线程正忙），
    调用就会永久阻塞——枚举窗口和截图线程都会被它拖死。
    SendMessageTimeoutW + SMTO_ABORTIFHUNG 会在目标无响应时立刻返回，最坏等 timeout_ms。
    """
    result = ctypes.c_ssize_t(0)
    flags = SMTO_ABORTIFHUNG | SMTO_BLOCK
    try:
        ok = user32.SendMessageTimeoutW(
            hwnd, WM_GETTEXTLENGTH, 0, 0, flags, timeout_ms, ctypes.byref(result)
        )
        if not ok:
            return ""
        length = int(result.value)
        if length <= 0:
            return ""
        length = min(length, 4096)  # 有些程序会返回异常大的值
        buf = ctypes.create_unicode_buffer(length + 1)
        ok = user32.SendMessageTimeoutW(
            hwnd, WM_GETTEXT, length + 1, ctypes.cast(buf, ctypes.c_void_p).value,
            flags, timeout_ms, ctypes.byref(result),
        )
        if not ok:
            return ""
        return buf.value
    except OSError:  # pragma: no cover - 窗口正好被销毁
        return ""


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def _is_cloaked(hwnd: int) -> bool:
    """UWP 应用会保留一些“被挂起”的隐身窗口，需要过滤掉。"""
    if dwmapi is None:
        return False
    value = wintypes.DWORD(0)
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
    except OSError:  # pragma: no cover
        return False
    return hr == 0 and value.value != 0


def get_window_rect_raw(hwnd: int) -> Tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError(f"无法获取窗口位置（hwnd=0x{int(hwnd):X}）")
    return rect.left, rect.top, rect.right, rect.bottom


def get_window_rect_extended(hwnd: int) -> Tuple[int, int, int, int]:
    """DWM 真实可见边框；Win10/11 的 GetWindowRect 会包含一圈不可见的拖拽边框。"""
    if dwmapi is None:
        return get_window_rect_raw(hwnd)
    rect = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if hr != 0:
        return get_window_rect_raw(hwnd)
    return rect.left, rect.top, rect.right, rect.bottom


def window_info(hwnd: int) -> WindowInfo:
    hwnd = int(hwnd)
    if not is_window(hwnd):
        raise CaptureError("窗口不存在或已关闭")
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    left, top, right, bottom = get_window_rect_extended(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        title=_window_text(hwnd),
        class_name=_class_name(hwnd),
        process_name=get_process_name(pid.value),
        pid=pid.value,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        minimized=is_minimized(hwnd),
    )


def enum_windows(include_own: bool = False, include_untitled: bool = False) -> List[WindowInfo]:
    """枚举当前桌面上可截图的顶层窗口。"""
    results: List[WindowInfo] = []
    own_pid = os.getpid()

    def _callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _get_window_long(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            title = _window_text(hwnd)
            if not title.strip() and not include_untitled:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid and not include_own:
                return True
            if _is_cloaked(hwnd):
                return True
            left, top, right, bottom = get_window_rect_extended(hwnd)
            if right - left <= 0 or bottom - top <= 0:
                return True
            results.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title,
                    class_name=_class_name(hwnd),
                    process_name=get_process_name(pid.value),
                    pid=pid.value,
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    minimized=is_minimized(hwnd),
                )
            )
        except Exception:
            # 枚举期间窗口可能正好被销毁，忽略即可
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    results.sort(key=lambda w: (w.process_name.lower(), w.title.lower()))
    return results


def find_windows_by_title(title: str, exact: bool = False) -> List[WindowInfo]:
    needle = title.strip().lower()
    if not needle:
        return []
    found = []
    for info in enum_windows(include_own=True, include_untitled=True):
        hay = info.title.lower()
        if (hay == needle) if exact else (needle in hay):
            found.append(info)
    return found


def get_virtual_screen_rect() -> Tuple[int, int, int, int]:
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, left + width, top + height


def is_foreground(hwnd: int) -> bool:
    try:
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:  # pragma: no cover
        return False


def is_window_responsive(hwnd: int, timeout_ms: int = 250) -> bool:
    """目标窗口所在线程是否还在处理消息。

    窗口线程忙 / 未响应时，WGC 建立会话、PrintWindow 之类的跨线程调用都可能一直等，
    所以动手之前先用 WM_NULL 探一下（这也是判断窗口“未响应”的标准做法）。
    """
    result = ctypes.c_ssize_t(0)
    try:
        ok = user32.SendMessageTimeoutW(
            int(hwnd), WM_NULL, 0, 0, SMTO_ABORTIFHUNG | SMTO_BLOCK,
            int(timeout_ms), ctypes.byref(result),
        )
        return bool(ok)
    except OSError:  # pragma: no cover
        return False


def activate_window(hwnd: int) -> bool:
    """把目标窗口切到最前面。

    屏幕区域截图时，如果窗口被别的窗口挡住，截到的就是遮挡物，
    所以抓帧前先把它带到前台。正常情况下当前进程持有前台窗口，
    SetForegroundWindow 会成功；失败时用 AttachThreadInput 兜底。
    """
    hwnd = int(hwnd)
    if not is_window(hwnd):
        return False
    try:
        if is_minimized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        user32.BringWindowToTop(hwnd)
        if user32.SetForegroundWindow(hwnd) and is_foreground(hwnd):
            return True

        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()
        foreground = user32.GetForegroundWindow()
        foreground_thread = (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        attached = []
        for thread_id in (foreground_thread, target_thread):
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            for thread_id in attached:
                user32.AttachThreadInput(current_thread, thread_id, False)
        return is_foreground(hwnd)
    except Exception:  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
class _DibCanvas:
    """一块 32 位自上而下的 DIB 位图，可以直接当画布用，也能直接读成 PIL 图像。"""

    def __init__(self, width: int, height: int):
        self.width = int(width)
        self.height = int(height)
        self.memdc = None
        self.hbitmap = None
        self.bits = ctypes.c_void_p()
        self._old = None

    def __enter__(self) -> "_DibCanvas":
        if self.width <= 0 or self.height <= 0:
            raise CaptureError("截取区域宽高必须大于 0")
        self.memdc = gdi32.CreateCompatibleDC(None)
        if not self.memdc:
            raise CaptureError("CreateCompatibleDC 失败")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.width
        bmi.bmiHeader.biHeight = -self.height  # 负值 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        self.hbitmap = gdi32.CreateDIBSection(
            self.memdc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(self.bits), None, 0,
        )
        if not self.hbitmap:
            self.__exit__()
            raise CaptureError("CreateDIBSection 失败（显存或 GDI 资源不足）")
        self._old = gdi32.SelectObject(self.memdc, self.hbitmap)
        return self

    def image(self) -> Image.Image:
        raw = ctypes.string_at(self.bits, self.width * self.height * 4)
        return Image.frombytes("RGB", (self.width, self.height), raw, "raw", "BGRX", 0, 1)

    def __exit__(self, *_exc) -> None:
        if self.memdc:
            if self._old:
                gdi32.SelectObject(self.memdc, self._old)
            if self.hbitmap:
                gdi32.DeleteObject(self.hbitmap)
            gdi32.DeleteDC(self.memdc)
        self.memdc = None
        self.hbitmap = None
        self._old = None


def _print_window(hwnd: int, width: int, height: int, client_only: bool):
    """返回 (图像 或 None, 失败时的 Win32 错误码)。"""
    try:
        with _DibCanvas(width, height) as canvas:
            flags = PW_CLIENTONLY if client_only else PW_RENDERFULLCONTENT
            ctypes.set_last_error(0)
            if not user32.PrintWindow(hwnd, canvas.memdc, flags):
                last_error = ctypes.get_last_error()
                ctypes.set_last_error(0)
                if not user32.PrintWindow(hwnd, canvas.memdc, 0):
                    return None, ctypes.get_last_error() or last_error
            return canvas.image(), 0
    except CaptureError:
        return None, 0


def describe_printwindow_error(code: int) -> str:
    if code == 5:
        return "系统拒绝访问（部分游戏、UWP 应用、以管理员身份运行的窗口会如此）"
    if code == 87:
        return "参数不正确（窗口尺寸异常）"
    if code == 0:
        return "窗口没有响应或尚未绘制完成"
    return f"Win32 错误码 {code}"


def _looks_blank(image: Image.Image) -> bool:
    """整幅图只有一种颜色，通常意味着 PrintWindow 没有真正画上内容。"""
    try:
        low, high = image.convert("L").getextrema()
    except Exception:  # pragma: no cover
        return False
    return low == high


def capture_region(rect: Tuple[int, int, int, int]) -> Image.Image:
    """从屏幕 DC 直接拷贝指定矩形（物理像素坐标）。"""
    left, top, right, bottom = (int(v) for v in rect)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise CaptureError("截取区域为空（窗口可能已最小化）")
    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise CaptureError("GetDC 失败")
    try:
        with _DibCanvas(width, height) as canvas:
            ok = gdi32.BitBlt(
                canvas.memdc, 0, 0, width, height,
                screen_dc, left, top, SRCCOPY | CAPTUREBLT,
            )
            if not ok:
                raise CaptureError("BitBlt 拷贝屏幕失败")
            return canvas.image()
    finally:
        user32.ReleaseDC(None, screen_dc)


def capture_screen_full() -> Image.Image:
    """截取整个虚拟桌面（所有显示器）。"""
    return capture_region(get_virtual_screen_rect())


def capture_window(
    hwnd: int,
    method: str = METHOD_AUTO,
    client_only: bool = False,
    activate: bool = False,
) -> Tuple[Image.Image, str]:
    """截取指定窗口，返回 (图像, 实际使用的方式)。

    activate=True 时，只有在需要退回到「屏幕区域」方式前，才会把目标窗口
    切到前台（PrintWindow 能截的窗口不需要抢焦点）。
    """
    hwnd = int(hwnd)
    if not is_window(hwnd):
        raise CaptureError("窗口不存在或已关闭")
    if is_minimized(hwnd):
        # 最小化时窗口没有可绘制的内容，截图只会得到黑图或图标，直接给出明确提示
        raise CaptureError("窗口当前已最小化，无法截图，请先还原窗口")

    raw = get_window_rect_raw(hwnd)
    extended = get_window_rect_extended(hwnd)
    raw_w, raw_h = raw[2] - raw[0], raw[3] - raw[1]

    if method in (METHOD_AUTO, METHOD_PRINTWINDOW):
        image, err = _print_window(hwnd, raw_w, raw_h, client_only)
        if image is not None:
            # GetWindowRect 比 DWM 真实可见边框大一圈（不可见的拖拽边框），裁掉它，
            # 这样 PrintWindow 的输出尺寸与「屏幕区域」方式完全一致。
            box = (
                max(0, extended[0] - raw[0]),
                max(0, extended[1] - raw[1]),
                min(raw_w, raw_w - max(0, raw[2] - extended[2])),
                min(raw_h, raw_h - max(0, raw[3] - extended[3])),
            )
            if box[2] > box[0] and box[3] > box[1] and box != (0, 0, raw_w, raw_h):
                image = image.crop(box)
            if method == METHOD_PRINTWINDOW or not _looks_blank(image):
                return image, METHOD_PRINTWINDOW
        elif method == METHOD_PRINTWINDOW:
            raise CaptureError(
                f"PrintWindow 截图失败（{describe_printwindow_error(err)}），"
                "请改用「屏幕区域」或「自动」方式"
            )

    # 退回到屏幕区域：先把窗口带到前台，否则会截到挡住它的窗口
    if activate and not is_foreground(hwnd):
        activate_window(hwnd)
        time.sleep(0.12)
        extended = get_window_rect_extended(hwnd)
    return capture_region(extended), METHOD_SCREEN
