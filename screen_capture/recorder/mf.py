# -*- coding: utf-8 -*-
"""mf.py —— 通过 ctypes 调用 Windows Media Foundation，把画面编码成 MP4(H.264)。

设计要点：
* 只依赖 Windows 自带组件（mfplat.dll / mfreadwrite.dll），不需要 ffmpeg / OpenCV；
* 连续内存（NV12）用 numpy 切片做颜色转换，速度快；
* 每个"分段"（segment）对应一个 MP4 文件，段内编码参数固定；
  改变分辨率/帧率/码率时结束当前段并开启新段，时间戳按帧序推进，互不影响。
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import POINTER, byref, c_int, c_longlong, c_uint32, c_void_p
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover - 运行时才需要
    np = None  # type: ignore

from .crt import GUID, HRESULT, check, declare_interface, describe_hr, guid, ole32, _release

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MF_VERSION = 0x00020070          # MF_SDK_VERSION << 16 | MF_API_VERSION
MFSTARTUP_FULL = 0
MFSTARTUP_NOSOCKET = 1

MFT_ENUM_FLAG_ALL = 0x0000003F
MFT_ENUM_FLAG_SORTANDFILTER = 0x00000040

COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850   # 0x80010106

MEDIA_TYPE_VIDEO = guid("{73646976-0000-0010-8000-00AA00389B71}")
MEDIA_TYPE_AUDIO = guid("{73647561-0000-0010-8000-00AA00389B71}")
VIDEO_FORMAT_NV12 = guid("{3231564E-0000-0010-8000-00AA00389B71}")
VIDEO_FORMAT_RGB32 = guid("{00000016-0000-0010-8000-00AA00389B71}")
VIDEO_FORMAT_H264 = guid("{34363248-0000-0010-8000-00AA00389B71}")
VIDEO_FORMAT_YUY2 = guid("{32595559-0000-0010-8000-00AA00389B71}")

MFT_CATEGORY_VIDEO_ENCODER = guid("{f79eac7d-e545-4387-bdee-d647d7bde42a}")

MT_MAJOR_TYPE = guid("{48eba18e-f8c9-4687-bf11-0a74c9f96a8f}")
MT_SUBTYPE = guid("{f7e34c9a-42e8-4714-b74b-cb29d72c35e5}")
MT_FRAME_SIZE = guid("{1652c33d-d6b2-4012-b834-72030849a37d}")
MT_FRAME_RATE = guid("{c459a2e8-3d2c-4e44-b132-fee5156c7bb0}")
MT_PIXEL_ASPECT_RATIO = guid("{c6376a1e-8d0a-4027-be45-6d9a0ad39bb6}")
MT_INTERLACE_MODE = guid("{e2724bb8-e676-4806-b4b2-a8d6efb44ccd}")
MT_AVG_BITRATE = guid("{20332624-fb0d-4d9e-bd0d-cbf6786c102e}")
MT_MPEG2_PROFILE = guid("{ad76a80b-2d5c-4e0b-b375-64e520137036}")
MT_ALL_SAMPLES_INDEPENDENT = guid("{c9173739-5e56-461c-b713-46fb995cb95f}")
TRANSFORM_FRIENDLY_NAME = guid("{314ffbae-5b41-4c95-9c19-4e7d586face3}")
TRANSFORM_ASYNC = guid("{ea1e2b3c-4d5e-4f60-8a9b-0c1d2e3f4a5b}")

MFVideoInterlace_Progressive = 2

PROFILE_BASELINE = 66
PROFILE_MAIN = 77
PROFILE_HIGH = 100

# 输入像素格式
INPUT_AUTO = "auto"
INPUT_NV12 = "nv12"


# ---------------------------------------------------------------------------
# COM 接口
# ---------------------------------------------------------------------------
class IMFAttributes(ctypes.Structure):
    pass


class IMFMediaType(ctypes.Structure):
    pass


class IMFMediaBuffer(ctypes.Structure):
    pass


class IMFSample(ctypes.Structure):
    pass


class IMFSinkWriter(ctypes.Structure):
    pass


class IMFActivate(ctypes.Structure):
    pass


def _struct(name: str, extra):
    """生成一个带 lpVtbl 的 ctypes 结构体。"""
    return type(name, (ctypes.Structure,), {"_fields_": [("lpVtbl", POINTER(c_void_p))]})


IMFAttributes = declare_interface("IMFAttributes")
IMFMediaType = declare_interface("IMFMediaType", [("GetMajorType", 30)])
IMFMediaBuffer = declare_interface("IMFMediaBuffer", [])
IMFSample = declare_interface("IMFSample", [])
IMFSinkWriter = declare_interface("IMFSinkWriter", [])
IMFActivate = declare_interface("IMFActivate", [("ActivateObject", 30)])

# --- 补充各接口自己的方法（槽位同样是实测值，见 crt.ATTR_METHODS 的说明）---
from .crt import bind  # noqa: E402

# IMFMediaType = IMFAttributes(33 个槽位，含 IUnknown 3 个) + 以下
bind(IMFMediaType, "GetMajorType", 33, [POINTER(GUID)])

# IMFMediaBuffer（声明内 5 个方法 → 原生槽位 3..7，同样是 +3 偏移）
bind(IMFMediaBuffer, "Lock", 3, [POINTER(c_void_p), POINTER(c_int), POINTER(c_int)])
bind(IMFMediaBuffer, "Unlock", 4, [])
bind(IMFMediaBuffer, "GetCurrentLength", 5, [POINTER(c_int)])
bind(IMFMediaBuffer, "SetCurrentLength", 6, [c_int])
bind(IMFMediaBuffer, "GetMaxLength", 7, [POINTER(c_int)])

# IMFSample：属性部分为 3..32，之后的方法（实测，同样是 +3 偏移）
bind(IMFSample, "GetSampleTime", 35, [POINTER(c_longlong)])
bind(IMFSample, "SetSampleTime", 36, [c_longlong])
bind(IMFSample, "GetSampleDuration", 37, [POINTER(c_longlong)])
bind(IMFSample, "SetSampleDuration", 38, [c_longlong])
bind(IMFSample, "GetBufferCount", 39, [POINTER(c_int)])
bind(IMFSample, "GetBufferByIndex", 40, [c_int, POINTER(POINTER(IMFMediaBuffer))])
bind(IMFSample, "AddBuffer", 42, [POINTER(IMFMediaBuffer)])

# IMFSinkWriter：这里同样是**实测槽位**，注意它与 SDK 头文件的方法序号不一致
# （头文件里 AddStream 是第 1 个方法，即槽位 3，实测确实是 3；
#   下面的号是逐个调用验证出来的，0/1/2 号槽位分别返回 E_NOINTERFACE / S_FALSE / S_OK 但都不是 AddStream）。
bind(IMFSinkWriter, "AddStream", 3, [POINTER(IMFMediaType), POINTER(c_int)])
bind(IMFSinkWriter, "SetInputMediaType", 4, [c_int, POINTER(IMFMediaType), c_void_p])
bind(IMFSinkWriter, "BeginWriting", 5, [])
bind(IMFSinkWriter, "WriteSample", 6, [c_int, POINTER(IMFSample)])
bind(IMFSinkWriter, "Finalize", 11, [])

# IMFActivate（只用到属性读取与激活）
bind(IMFActivate, "ActivateObject", 33, [POINTER(GUID), POINTER(c_void_p)])


class RegisterTypeInfo(ctypes.Structure):
    _fields_ = [("major_type", GUID), ("sub_type", GUID)]


# ---------------------------------------------------------------------------
# DLL 绑定
# ---------------------------------------------------------------------------
_mfplat = ctypes.WinDLL("mfplat.dll")
_mfreadwrite = ctypes.WinDLL("mfreadwrite.dll")

_mfplat.MFStartup.argtypes = [c_int, c_int]
_mfplat.MFStartup.restype = HRESULT
_mfplat.MFShutdown.restype = HRESULT
_mfplat.MFCreateMediaType.argtypes = [POINTER(POINTER(IMFMediaType))]
_mfplat.MFCreateMediaType.restype = HRESULT
_mfplat.MFCreateMemoryBuffer.argtypes = [c_int, POINTER(POINTER(IMFMediaBuffer))]
_mfplat.MFCreateMemoryBuffer.restype = HRESULT
_mfplat.MFCreateSample.argtypes = [POINTER(POINTER(IMFSample))]
_mfplat.MFCreateSample.restype = HRESULT
_mfplat.MFTEnumEx.argtypes = [POINTER(GUID), c_uint32, c_void_p, c_void_p,
                              POINTER(c_void_p), POINTER(c_int)]
_mfplat.MFTEnumEx.restype = HRESULT
_mfreadwrite.MFCreateSinkWriterFromURL.argtypes = [
    ctypes.c_wchar_p, c_void_p, c_void_p, POINTER(POINTER(IMFSinkWriter))]
_mfreadwrite.MFCreateSinkWriterFromURL.restype = HRESULT

_startup_lock = threading.Lock()
_startup_count = 0
_bindings_verified = False
_com_ready = False


def startup() -> None:
    """初始化 Media Foundation（进程内幂等），并做一次 vtable 绑定自检。"""
    global _startup_count
    with _startup_lock:
        if _startup_count == 0:
            check(_mfplat.MFStartup(MF_VERSION, MFSTARTUP_FULL), "初始化 Media Foundation")
        _startup_count += 1
    verify_bindings()


def verify_bindings() -> None:
    """运行时自检：确认 IMFAttributes 的槽位索引与当前系统一致。

    做法是在一个临时媒体类型上写入已知值再读回；只要索引错位（例如系统版本
    变化导致 vtable 顺序不同），这里就会立刻抛出明确的错误，而不是等到录制
    中途才崩溃。
    """
    global _bindings_verified
    if _bindings_verified:
        return
    if np is None:  # pragma: no cover - 没有 numpy 时仍可用，但错误更难定位
        _bindings_verified = True
        return

    media_type = POINTER(IMFMediaType)()
    check(_mfplat.MFCreateMediaType(byref(media_type)), "创建媒体类型（自检）")
    try:
        t = media_type.contents
        key = MT_AVG_BITRATE
        check(t.SetUINT32(byref(key), 180150001), "自检：SetUINT32")
        got = c_uint32(0)
        check(t.GetUINT32(byref(key), byref(got)), "自检：GetUINT32")
        if got.value != 180150001:
            raise OSError(
                "Media Foundation 接口绑定自检失败（UINT32 属性读写不一致，"
                f"读到 {got.value}）。请把这一行反馈给开发者："
                "screen_capture/recorder/crt.py 的 ATTR_METHODS 槽位表需要按当前系统更新。")

        key_major = MT_MAJOR_TYPE
        check(t.SetGUID(byref(key_major), byref(MEDIA_TYPE_VIDEO)), "自检：SetGUID")
        got_guid = GUID()
        check(t.GetGUID(byref(key_major), byref(got_guid)), "自检：GetGUID")
        if str(got_guid).upper() != str(MEDIA_TYPE_VIDEO).upper():
            raise OSError(
                f"Media Foundation 接口绑定自检失败（GUID 属性读写不一致，读到 {got_guid}）。")

        size_key = MT_FRAME_SIZE
        check(t.SetUINT64(byref(size_key), (1280 << 32) | 720), "自检：SetUINT64")
        got64 = c_longlong(0)
        check(t.GetUINT64(byref(size_key), byref(got64)), "自检：GetUINT64")
        if got64.value != ((1280 << 32) | 720):
            raise OSError("Media Foundation 接口绑定自检失败（UINT64 属性读写不一致）。")
    finally:
        _release(media_type.contents)
    _bindings_verified = True


def shutdown() -> None:
    global _startup_count
    with _startup_lock:
        if _startup_count > 0:
            _startup_count -= 1
            if _startup_count == 0:
                try:
                    _mfplat.MFShutdown()
                except Exception:
                    pass


def com_initialize() -> bool:
    """在当前线程初始化 COM（Media Foundation 需要）。

    返回 True 表示本函数成功初始化、退出时应调用 com_uninitialize()。
    """
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr == RPC_E_CHANGED_MODE:
        return False
    if hr < 0:
        check(hr, "初始化 COM")
    return True


def com_uninitialize() -> None:
    try:
        ole32.CoUninitialize()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 编码器枚举
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EncoderInfo:
    name: str
    hardware: bool
    index: int

    def __str__(self) -> str:  # pragma: no cover - 仅用于界面显示
        return self.name


def list_h264_encoders() -> List[EncoderInfo]:
    """枚举可用的 H.264 视频编码器（NV12 → H.264）。"""
    result: List[EncoderInfo] = []
    startup()
    info_in = RegisterTypeInfo(MEDIA_TYPE_VIDEO, VIDEO_FORMAT_NV12)
    info_out = RegisterTypeInfo(MEDIA_TYPE_VIDEO, VIDEO_FORMAT_H264)
    activates = c_void_p()
    count = c_int()
    try:
        hr = _mfplat.MFTEnumEx(
            byref(MFT_CATEGORY_VIDEO_ENCODER),
            MFT_ENUM_FLAG_ALL | MFT_ENUM_FLAG_SORTANDFILTER,
            ctypes.cast(byref(info_in), c_void_p),
            ctypes.cast(byref(info_out), c_void_p),
            byref(activates), byref(count))
        if hr < 0 or count.value <= 0 or not activates.value:
            return result
        array = ctypes.cast(activates, POINTER(c_void_p))
        for i in range(count.value):
            ptr = array[i]
            if not ptr:
                continue
            name = _friendly_name(ptr) or f"H.264 编码器 #{i + 1}"
            hardware = _is_async(ptr)
            result.append(EncoderInfo(name=name, hardware=hardware, index=i))
    except Exception:
        return result
    finally:
        if activates.value:
            ole32.CoTaskMemFree(activates)
    return result


def _friendly_name(ptr: int) -> Optional[str]:
    """读取编码器激活对象的友好名（失败则返回 None，不影响录制）。"""
    try:
        act = ctypes.cast(ptr, POINTER(IMFActivate)).contents
        name = _read_allocated_string(act, TRANSFORM_FRIENDLY_NAME)
        if name:
            return name
        name = _read_allocated_string(act, TRANSFORM_FRIENDLY_NAME.lower())
        return name
    except Exception:
        return None


def _read_allocated_string(attributes, key: GUID) -> Optional[str]:
    """用 IMFAttributes::GetAllocatedString 读取字符串属性。"""
    key_value = key
    buffer = ctypes.c_void_p()
    length = ctypes.c_uint32(0)
    hr = attributes.GetAllocatedString(byref(key_value), byref(buffer), byref(length))
    if hr < 0 or not buffer.value:
        return None
    try:
        return ctypes.cast(buffer, ctypes.c_wchar_p).value
    finally:
        ole32.CoTaskMemFree(buffer)


def _is_async(ptr: int) -> bool:
    try:
        act = ctypes.cast(ptr, POINTER(IMFActivate)).contents
        key = TRANSFORM_ASYNC
        value = c_int()
        return act.GetUINT32(byref(key), byref(value)) == 0 and value.value != 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 媒体类型
# ---------------------------------------------------------------------------
def _create_video_type(subtype: GUID, width: int, height: int, fps: int,
                       bitrate_kbps: int = 0) -> POINTER(IMFMediaType):
    mt = POINTER(IMFMediaType)()
    check(_mfplat.MFCreateMediaType(byref(mt)), "创建媒体类型")
    t = mt.contents
    check(t.SetGUID(byref(MT_MAJOR_TYPE), byref(MEDIA_TYPE_VIDEO)), "设置主类型")
    check(t.SetGUID(byref(MT_SUBTYPE), byref(subtype)), "设置子类型")
    check(t.SetUINT32(byref(MT_INTERLACE_MODE), MFVideoInterlace_Progressive), "设置扫描方式")
    check(t.SetUINT64(byref(MT_FRAME_SIZE), (width << 32) | height), "设置分辨率")
    check(t.SetUINT64(byref(MT_FRAME_RATE), (fps << 32) | 1), "设置帧率")
    check(t.SetUINT64(byref(MT_PIXEL_ASPECT_RATIO), (1 << 32) | 1), "设置像素宽高比")
    if bitrate_kbps:
        check(t.SetUINT32(byref(MT_AVG_BITRATE), int(bitrate_kbps) * 1000), "设置码率")
    return mt


# ---------------------------------------------------------------------------
# RGB → NV12
# ---------------------------------------------------------------------------
def rgb_to_nv12(rgb: "np.ndarray", out: Optional["np.ndarray"] = None) -> "np.ndarray":
    """把 HxWx4 的 BGRA 图像转换为 NV12（BT.601 有限范围）。

    输入约定为自上而下、每行连续 4 字节/像素（B, G, R, A）。
    """
    if np is None:  # pragma: no cover
        raise RuntimeError("需要 numpy 才能进行颜色转换")
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError("输入必须是 HxWx4 的 BGRA 数组")
    height, width = rgb.shape[0], rgb.shape[1]
    if out is None:
        out = np.empty((width * height * 3) // 2, dtype=np.uint8)

    # RGB（先算出需要的通道，避免整幅浮点运算）
    src = rgb[:, :, :3]
    b = src[:, :, 0].astype(np.int32, copy=False)
    g = src[:, :, 1].astype(np.int32, copy=False)
    r = src[:, :, 2].astype(np.int32, copy=False)
    y = ((66 * r + 129 * g + 25 * b + 128) >> 8) + 16
    np.clip(y, 0, 255, out=y)
    y_size = width * height
    out[:y_size] = y.astype(np.uint8).reshape(y_size)

    # 2x2 平均后计算 U / V（宽高均为偶数，直接分块平均）
    b4 = (b[0::2, 0::2] + b[0::2, 1::2] + b[1::2, 0::2] + b[1::2, 1::2]) >> 2
    g4 = (g[0::2, 0::2] + g[0::2, 1::2] + g[1::2, 0::2] + g[1::2, 1::2]) >> 2
    r4 = (r[0::2, 0::2] + r[0::2, 1::2] + r[1::2, 0::2] + r[1::2, 1::2]) >> 2

    u = ((-38 * r4 - 74 * g4 + 112 * b4 + 128) >> 8) + 128
    v = ((112 * r4 - 94 * g4 - 18 * b4 + 128) >> 8) + 128
    np.clip(u, 0, 255, out=u)
    np.clip(v, 0, 255, out=v)

    uv = np.empty((u.shape[0], u.shape[1] * 2), dtype=np.uint8)
    uv[:, 0::2] = u.astype(np.uint8)
    uv[:, 1::2] = v.astype(np.uint8)
    out[y_size:] = uv.reshape(-1)
    return out


# ---------------------------------------------------------------------------
# MP4 编码器
# ---------------------------------------------------------------------------
class Mp4Encoder:
    """把一帧帧画面写进 MP4 文件。

    用法::

        enc = Mp4Encoder()
        with enc.segment("out.mp4", 1920, 1080, 30, 8000):
            enc.write(frame, timestamp_ms=0)
            enc.write(frame, timestamp_ms=33.3)
    """

    def __init__(self, input_format: str = INPUT_AUTO, profile: int = PROFILE_HIGH) -> None:
        self._writer: Optional[POINTER(IMFSinkWriter)] = None
        self._stream = -1
        self._width = 0
        self._height = 0
        self._fps = 30
        self._bitrate = 0
        self._fed_nv12 = False
        self._nv12 = None
        self._last_time = -1
        self._frames = 0
        self._duration_hns = 0
        self._path = ""
        self._started = False
        self.input_format = input_format
        self.profile = profile
        self.input_description = ""
        startup()

    # -- 属性 --
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def is_writing(self) -> bool:
        return self._writer is not None

    @property
    def frames_written(self) -> int:
        return self._frames

    @property
    def duration_hns(self) -> int:
        return self._duration_hns

    @property
    def path(self) -> str:
        return self._path

    # -- 生命周期 --
    def start_segment(self, path: str, width: int, height: int, fps: int,
                      bitrate_kbps: int) -> None:
        """开始写一个 MP4 分段。"""
        if self._writer is not None:
            raise RuntimeError("上一分段尚未结束")
        width = int(width) & ~1
        height = int(height) & ~1
        if width < 16 or height < 16:
            raise ValueError(f"分辨率过小：{width}x{height}")
        path = os.path.abspath(path)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self._width, self._height, self._fps = width, height, int(fps)
        self._bitrate = int(bitrate_kbps)
        self._path = path
        self._frames = 0
        self._duration_hns = 0
        self._last_time = -1

        out_type = _create_video_type(VIDEO_FORMAT_H264, width, height, fps, self._bitrate)
        in_type = None
        try:
            check(out_type.contents.SetUINT32(byref(MT_MPEG2_PROFILE), int(self.profile)),
                  "设置 H.264 Profile")
            writer = POINTER(IMFSinkWriter)()
            check(_mfreadwrite.MFCreateSinkWriterFromURL(path, None, None, byref(writer)),
                  "创建 MP4 写入器")
            self._writer = writer
            stream = c_int()
            check(writer.contents.AddStream(out_type, byref(stream)), "添加视频流")
            self._stream = stream.value

            if self.input_format == INPUT_NV12:
                self._set_input_nv12()
            else:
                # auto：优先让系统做颜色转换（CPU 占用低，实测最稳）
                try:
                    in_type = _create_video_type(VIDEO_FORMAT_RGB32, width, height, fps)
                    check(in_type.contents.SetUINT32(byref(MT_ALL_SAMPLES_INDEPENDENT), 1),
                          "RGB32 标记")
                    check(writer.contents.SetInputMediaType(self._stream, in_type, None),
                          "设置输入格式 RGB32")
                    self._fed_nv12 = False
                    self.input_description = "RGB32（系统转换）"
                except OSError:
                    in_type = None
                    # 系统不接受 RGB32 时退到自己转 NV12，再不行就把真实错误抛出去
                    self._set_input_nv12(silent=True)

            check(self._writer.contents.BeginWriting(), "开始写入")
            self._started = True
        except Exception:
            self.stop_segment()
            # 失败的尝试会留下一个 0 字节文件，清掉以免误导用户
            try:
                if os.path.exists(path) and os.path.getsize(path) == 0:
                    os.remove(path)
            except OSError:
                pass
            raise
        finally:
            if in_type is not None:
                _release(in_type.contents)
            _release(out_type.contents)

    def _set_input_nv12(self, silent: bool = False) -> None:
        """把输入格式设为 NV12（由本模块做 RGB→NV12 转换）。

        silent=True 时返回 HRESULT 而不抛异常，供 auto 模式判断"到底哪种格式
        被系统接受"，避免把中间失败的媒体类型错误暴露给用户。
        """
        in_type = _create_video_type(VIDEO_FORMAT_NV12, self._width, self._height, self._fps)
        try:
            check(in_type.contents.SetUINT32(byref(MT_ALL_SAMPLES_INDEPENDENT), 1), "NV12 标记")
            hr = self._writer.contents.SetInputMediaType(self._stream, in_type, None)
            if hr < 0:
                if silent:
                    return
                raise OSError("设置输入格式 NV12 失败：" + describe_hr(hr))
            self._fed_nv12 = True
            self.input_description = "NV12（程序转换）"
            size = (self._width * self._height * 3) // 2
            if self._nv12 is None or self._nv12.size != size:
                self._nv12 = np.empty(size, dtype=np.uint8) if np is not None else None
        finally:
            _release(in_type.contents)

    def write(self, frame, timestamp_ms: float) -> None:
        """写入一帧（frame 为 HxWx4 的 BGRA numpy 数组，尺寸需与分段一致）。"""
        if self._writer is None:
            raise RuntimeError("尚未开始分段")
        if np is None:  # pragma: no cover
            raise RuntimeError("需要 numpy")
        height, width = frame.shape[0], frame.shape[1]
        if width != self._width or height != self._height:
            raise ValueError(f"帧尺寸 {width}x{height} 与编码尺寸 {self._width}x{self._height} 不一致")

        hns = int(round(timestamp_ms * 10000.0))
        if hns <= self._last_time:
            hns = self._last_time + 1
        delta = 0 if self._last_time < 0 else hns - self._last_time
        self._last_time = hns

        if self._fed_nv12:
            data = rgb_to_nv12(frame, self._nv12)
            payload = data
        else:
            # RGBA → 直接写原始字节（alpha 通道内容无关紧要）
            payload = frame.reshape(-1)

        buf = POINTER(IMFMediaBuffer)()
        sample = POINTER(IMFSample)()
        try:
            check(_mfplat.MFCreateMemoryBuffer(payload.size, byref(buf)), "创建帧缓冲")
            ptr = c_void_p()
            max_len = c_int()
            cur_len = c_int()
            check(buf.contents.Lock(byref(ptr), byref(max_len), byref(cur_len)), "锁定帧缓冲")
            try:
                ctypes.memmove(ptr, payload.ctypes.data, payload.size)
            finally:
                buf.contents.Unlock()
            check(buf.contents.SetCurrentLength(payload.size), "设置帧长度")

            check(_mfplat.MFCreateSample(byref(sample)), "创建样本")
            check(sample.contents.AddBuffer(buf), "挂载帧缓冲")
            check(sample.contents.SetSampleTime(hns), "设置时间戳")
            if delta > 0:
                check(sample.contents.SetSampleDuration(delta), "设置帧间隔")
            check(self._writer.contents.WriteSample(self._stream, sample), "写入帧")
        finally:
            if sample:
                _release(sample.contents)
            if buf:
                _release(buf.contents)

        self._frames += 1
        if hns > self._duration_hns:
            self._duration_hns = hns

    def stop_segment(self) -> None:
        """结束当前分段并封盘（写出 moov，文件此时才完整）。"""
        writer = self._writer
        if writer is None:
            return
        try:
            if self._started:
                check(writer.contents.Finalize(), "完成录制 (Finalize)")
        finally:
            _release(writer.contents)
            self._writer = None
            self._stream = -1
            self._started = False

    def close(self) -> None:
        try:
            self.stop_segment()
        finally:
            shutdown()

    def __enter__(self) -> "Mp4Encoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def h264_available() -> Tuple[bool, str]:
    """检测 H.264 编码链路是否可用。返回 (是否可用, 说明)。"""
    try:
        startup()
    except Exception as exc:
        return False, f"无法初始化 Media Foundation：{exc}"
    try:
        import tempfile

        enc = Mp4Encoder()
        path = os.path.join(tempfile.gettempdir(), "_screen_capture_mf_check.mp4")
        if os.path.exists(path):
            os.remove(path)
        enc.start_segment(path, 320, 240, 30, 800)
        frame = np.zeros((240, 320, 4), dtype=np.uint8)
        frame[:, :, 3] = 255
        try:
            enc.write(frame, 0.0)
            enc.write(frame, 33.3)
        finally:
            enc.stop_segment()
            enc.close()
        ok = os.path.exists(path) and os.path.getsize(path) > 512
        size = os.path.getsize(path) if os.path.exists(path) else 0
        try:
            os.remove(path)
        except OSError:
            pass
        if ok:
            return True, f"H.264 编码可用（测试文件 {size} 字节）"
        return False, "编码器没有产生有效数据"
    except Exception as exc:
        return False, f"H.264 编码不可用：{exc}"
