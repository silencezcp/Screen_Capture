# -*- coding: utf-8 -*-
"""crt.py —— 最小 COM/ctypes 工具层（被 mf.py 使用）。

**方法索引约定**：这里给出的是 COM **原生 vtable 的绝对槽位**，不是接口声明里的
相对序号。IUnknown 固定占 0/1/2（QueryInterface / AddRef / Release），所以
IMFAttributes 的第一个方法 GetItem 在槽位 3 而不是 0。

索引不是照 SDK 头文件推算的，而是在 Windows 10（mfplat.dll 10.0.19041）上
**逐个槽位实测**确认（写入值后用对应的 Get 读回比对），与
`ScreenRecorder\\src\\MFInterop.cs` 的完整声明顺序一致。系统版本变化时，
`mf.verify_bindings()` 的运行时自检会立刻发现偏差。
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, c_double, c_longlong, c_uint32, c_void_p

ole32 = ctypes.WinDLL("ole32")

HRESULT = ctypes.c_long
UINT32 = c_uint32
UINT64 = c_longlong


class GUID(ctypes.Structure):
    """Windows GUID。"""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str = "") -> None:
        super().__init__()
        if text:
            hr = ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(self))
            if hr < 0:
                raise ValueError(f"无效的 GUID 字符串：{text}")

    def __str__(self) -> str:
        buf = ctypes.c_wchar_p()
        ole32.StringFromCLSID(ctypes.byref(self), ctypes.byref(buf))
        try:
            return buf.value or ""
        finally:
            ole32.CoTaskMemFree(buf)


def guid(text: str) -> GUID:
    return GUID(text)


def check(hr: int, what: str) -> int:
    """HRESULT 检查，失败抛出 OSError。"""
    if hr < 0:
        raise OSError(f"{what} 失败：{describe_hr(hr)}")
    return hr


_HR_MESSAGES = {
    0x80070005: "拒绝访问 (E_ACCESSDENIED)",
    0x80070002: "找不到文件 (ERROR_FILE_NOT_FOUND)",
    0x80070003: "找不到路径 (ERROR_PATH_NOT_FOUND)",
    0x80070057: "参数错误 (E_INVALIDARG)",
    0x80004001: "未实现 (E_NOTIMPL)",
    0x80004002: "不支持该接口 (E_NOINTERFACE)",
    0x80004005: "未指定的错误 (E_FAIL)",
    0xC00D36B4: "不支持的媒体类型 (MF_E_INVALIDMEDIATYPE)",
    0xC00D36BA: "无效的流序号 (MF_E_INVALIDSTREAMNUMBER)",
    0xC00D36BB: "没有可用的编解码器 (MF_E_TRANSFORM_TYPE_NOT_SET)",
    0xC00D36BD: "找不到该属性 (MF_E_ATTRIBUTENOTFOUND)",
    0xC00D36C4: "不受支持的字节流类型",
    0xC00D36C8: "样本没有时间戳 (MF_E_NO_SAMPLE_TIMESTAMP)",
    0xC00D36C9: "样本没有时长 (MF_E_NO_SAMPLE_DURATION)",
    0xC00D36D5: "对象尚未初始化 (MF_E_NOT_INITIALIZED)",
    0xC00D36E6: "属性类型不匹配 (MF_E_INVALIDTYPE)",
    0xC00D3E85: "当前状态不允许该操作 (MF_E_INVALIDREQUEST)",
    0xC00D3704: "媒体类型已被占用 (MF_E_INVALIDTYPE)",
    0xC00D5212: "没有找到可用的编解码器 (MF_E_TOPO_CODEC_NOT_FOUND)",
    0xC00D6D72: "需要更多输入数据 (MF_E_TRANSFORM_NEED_MORE_INPUT)",
}


def describe_hr(hr: int) -> str:
    code = hr & 0xFFFFFFFF
    msg = _HR_MESSAGES.get(code)
    if msg:
        return f"{msg} [0x{code:08X}]"
    return f"HRESULT 0x{code:08X}"


def _release(iface) -> int:
    """调用 COM 的 Release（vtable 索引 2）。"""
    vtbl = ctypes.cast(iface.lpVtbl, POINTER(c_void_p))
    proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
    return proto(vtbl[2])(ctypes.addressof(iface))


def _addref(iface) -> int:
    """调用 COM 的 AddRef（vtable 索引 1）。"""
    vtbl = ctypes.cast(iface.lpVtbl, POINTER(c_void_p))
    proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
    return proto(vtbl[1])(ctypes.addressof(iface))


def bind(iface_cls, name: str, index: int, argtypes) -> None:
    """按原生 vtable 槽位给接口绑定一个方法。"""

    def caller(self, *args):
        vtbl = ctypes.cast(self.lpVtbl, POINTER(c_void_p))
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
        return proto(vtbl[index])(ctypes.addressof(self), *args)

    caller.__name__ = name
    setattr(iface_cls, name, caller)


# IMFAttributes 的 29 个方法：槽位 = 3 + 声明序号（实测）。
ATTR_METHODS = [
    ("GetItem", 3), ("GetItemType", 4), ("CompareItem", 5), ("Compare", 6),
    ("GetUINT32", 7), ("GetUINT64", 8), ("GetDouble", 9), ("GetGUID", 10),
    ("GetStringLength", 11), ("GetString", 12), ("GetAllocatedString", 13),
    ("GetBlobSize", 14), ("GetBlob", 15), ("GetAllocatedBlob", 16),
    ("GetUnknown", 17), ("SetItem", 18), ("DeleteItem", 19), ("DeleteAllItems", 20),
    ("SetUINT32", 21), ("SetUINT64", 22), ("SetDouble", 23), ("SetGUID", 24),
    ("SetString", 25), ("SetBlob", 26), ("SetUnknown", 27), ("LockStore", 28),
    ("UnlockStore", 29), ("GetCount", 30), ("GetItemByIndex", 31), ("CopyAllItems", 32),
]

# IMFAttributes 各方法的完整参数签名（不含 this 指针）。
_ATTR_SIGS = {
    "GetItem": [POINTER(GUID), c_void_p],
    "GetItemType": [POINTER(GUID), POINTER(c_uint32)],
    "CompareItem": [POINTER(GUID), c_void_p, POINTER(c_uint32)],
    "Compare": [c_void_p, c_uint32, POINTER(c_uint32)],
    "GetUINT32": [POINTER(GUID), POINTER(c_uint32)],
    "GetUINT64": [POINTER(GUID), POINTER(c_longlong)],
    "GetDouble": [POINTER(GUID), POINTER(c_double)],
    "GetGUID": [POINTER(GUID), POINTER(GUID)],
    "GetStringLength": [POINTER(GUID), POINTER(c_uint32)],
    "GetString": [POINTER(GUID), ctypes.c_wchar_p, c_uint32, POINTER(c_uint32)],
    "GetAllocatedString": [POINTER(GUID), POINTER(c_void_p), POINTER(c_uint32)],
    "GetBlobSize": [POINTER(GUID), POINTER(c_uint32)],
    "GetBlob": [POINTER(GUID), c_void_p, c_uint32, POINTER(c_uint32)],
    "GetAllocatedBlob": [POINTER(GUID), POINTER(c_void_p), POINTER(c_uint32)],
    "GetUnknown": [POINTER(GUID), POINTER(GUID), POINTER(c_void_p)],
    "SetItem": [POINTER(GUID), c_void_p],
    "DeleteItem": [POINTER(GUID)],
    "DeleteAllItems": [],
    "SetUINT32": [POINTER(GUID), c_uint32],
    "SetUINT64": [POINTER(GUID), c_longlong],
    "SetDouble": [POINTER(GUID), c_double],
    "SetGUID": [POINTER(GUID), POINTER(GUID)],
    "SetString": [POINTER(GUID), ctypes.c_wchar_p],
    "SetBlob": [POINTER(GUID), c_void_p, c_uint32],
    "SetUnknown": [POINTER(GUID), c_void_p],
    "LockStore": [],
    "UnlockStore": [],
    "GetCount": [POINTER(c_uint32)],
    "GetItemByIndex": [c_uint32, POINTER(GUID), c_void_p],
    "CopyAllItems": [c_void_p],
}


def declare_interface(name: str, extra_methods=()):
    """声明一个 COM 接口结构体（仅 vtable 指针），并绑定指定方法。"""
    cls = type(name, (ctypes.Structure,), {"_fields_": [("lpVtbl", POINTER(c_void_p))]})
    for mname, index in list(extra_methods) + list(ATTR_METHODS):
        bind(cls, mname, index, _ATTR_SIGS.get(mname, []))
    return cls
