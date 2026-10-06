# -*- coding: utf-8 -*-
"""录屏参数模型：来源、画质档位、码率估算与配置读写。

界面与命令行共用同一份配置对象，`RecordingConfig.validate()` 负责把
用户填错的参数尽早拦下来（和截图模块的 `CaptureConfig` 保持一致风格）。
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .. import paths
from ..engine import ConfigError, Target, TARGET_SCREEN, TARGET_WINDOW, sanitize_filename_part, unique_path

__all__ = [
    "QUALITY_LEVELS", "QUALITY_LABELS", "RESOLUTION_PRESETS", "RESOLUTION_LABELS",
    "FPS_CHOICES", "QUALITY_PRESETS", "QualityPreset", "RecordingConfig",
    "load_record_config", "save_record_config", "default_record_dir",
    "DEFAULT_RECORD_PATTERN", "describe_quality", "even",
]

# 清晰度档位 →（内部名, 界面文字, 每像素每帧比特数）
QUALITY_LEVELS = ("low", "standard", "high", "ultra")
QUALITY_LABELS = {
    "low": "低（体积最小）",
    "standard": "标准",
    "high": "高",
    "ultra": "极高（最清晰）",
}
_BITS_PER_PIXEL = {"low": 0.07, "standard": 0.12, "high": 0.20, "ultra": 0.32}

# 分辨率档位 →（内部名, 目标高度, 界面文字）；0 表示沿用原始分辨率
RESOLUTION_PRESETS = (
    ("native", 0, "原始分辨率"),
    ("2160p", 2160, "2160p（4K）"),
    ("1440p", 1440, "1440p（2K）"),
    ("1080p", 1080, "1080p（全高清）"),
    ("720p", 720, "720p（高清）"),
    ("480p", 480, "480p（标清）"),
    ("360p", 360, "360p（最小）"),
)
RESOLUTION_LABELS = {name: label for name, _h, label in RESOLUTION_PRESETS}
_RESOLUTION_HEIGHTS = {name: h for name, h, _l in RESOLUTION_PRESETS}

FPS_CHOICES = (5, 10, 15, 20, 24, 25, 30, 50, 60)

DEFAULT_RECORD_PATTERN = "{app}_{date}_{time}"

# H.264 档位
PROFILE_CHOICES = (("high", "High（推荐）"), ("main", "Main"), ("baseline", "Baseline（兼容性最好）"))
PROFILE_VALUES = {"high": 100, "main": 77, "baseline": 66}

# 输入像素格式
INPUT_LABELS = {"auto": "自动（推荐）", "nv12": "NV12（程序转换）"}


def even(value: int) -> int:
    """H.264 要求宽高为偶数。"""
    return int(value) // 2 * 2


def describe_quality(level: str) -> str:
    return QUALITY_LABELS.get(level, level)


@dataclass(frozen=True)
class QualityPreset:
    """一键套用的画质组合。"""

    key: str
    label: str
    resolution: str
    fps: int
    quality: str
    bitrate_kbps: int = 0     # 0 = 自动

    def describe(self) -> str:
        rate = f"{self.bitrate_kbps} kbps" if self.bitrate_kbps else "自动码率"
        return f"{RESOLUTION_LABELS.get(self.resolution, self.resolution)} / {self.fps}fps / {describe_quality(self.quality)} / {rate}"


QUALITY_PRESETS = (
    QualityPreset("original", "原画（原始分辨率 30fps）", "native", 30, "ultra"),
    QualityPreset("hd1080", "高清 1080p 30fps", "1080p", 30, "high"),
    QualityPreset("sd720", "标清 720p 30fps", "720p", 30, "standard"),
    QualityPreset("smooth", "流畅 720p 15fps", "720p", 15, "standard"),
    QualityPreset("tiny", "极小 480p 15fps", "480p", 15, "low"),
)


def default_record_dir() -> Path:
    """默认录像目录：截图目录下的「录像」。"""
    return paths.default_capture_dir() / "录像"


@dataclass
class RecordingConfig:
    """一次录制的全部参数。"""

    target: Target = field(default_factory=Target)
    output_dir: str = ""
    resolution: str = "1080p"
    fps: int = 30
    quality: str = "high"
    bitrate_kbps: int = 0              # 0 = 按分辨率与清晰度自动计算
    profile: str = "high"
    input_format: str = "auto"
    scale_mode: str = "quality"        # quality（缩放更细腻）/ fast（帧率上限更高）
    filename_pattern: str = DEFAULT_RECORD_PATTERN
    start_delay: float = 3.0           # 点击开始后的倒计时秒数
    max_duration: float = 0.0          # 最长录制秒数，0 = 不限
    capture_cursor: bool = True
    hide_window: bool = True           # 倒计时/录制时把界面最小化
    preferred_encoder: str = ""        # 记录上次选用的编码器名（仅展示）

    # ---------------- 校验 ----------------
    def validate(self) -> None:
        if self.target.kind == TARGET_WINDOW and not self.target.hwnd:
            raise ConfigError("请先选择一个要录制的应用窗口")
        if not str(self.output_dir).strip():
            raise ConfigError("请先选择录像保存目录")
        if self.resolution not in _RESOLUTION_HEIGHTS:
            raise ConfigError(f"未知的分辨率档位：{self.resolution}")
        if not (1 <= int(self.fps) <= 120):
            raise ConfigError("帧率需要在 1~120 之间")
        if self.quality not in _BITS_PER_PIXEL:
            raise ConfigError(f"未知的清晰度档位：{self.quality}")
        if self.bitrate_kbps < 0:
            raise ConfigError("码率不能为负数")
        if self.bitrate_kbps and self.bitrate_kbps < 100:
            raise ConfigError("码率太小（至少 100 kbps），或填 0 让它自动计算")
        if self.profile not in PROFILE_VALUES:
            raise ConfigError(f"未知的 H.264 档位：{self.profile}")
        if self.input_format not in INPUT_LABELS:
            raise ConfigError(f"未知的输入格式：{self.input_format}")
        from .capture import SCALE_CHOICES
        if self.scale_mode not in SCALE_CHOICES:
            raise ConfigError(f"未知的缩放方式：{self.scale_mode}")
        if self.start_delay < 0:
            raise ConfigError("开始延迟不能为负数")
        if self.max_duration < 0:
            raise ConfigError("最长录制时长不能为负数")
        if not str(self.filename_pattern).strip():
            raise ConfigError("文件名模板不能为空")

    # ---------------- 分辨率与码率 ----------------
    def match_preset(self, presets=None) -> int:
        """当前参数命中了哪个预设，返回下标；没命中返回 -1。"""
        candidates = QUALITY_PRESETS if presets is None else presets
        for index, preset in enumerate(candidates):
            if (preset.resolution == self.resolution
                    and int(preset.fps) == int(self.fps)
                    and preset.quality == self.quality
                    and int(preset.bitrate_kbps) == int(self.bitrate_kbps)):
                return index
        return -1

    @property
    def target_height(self) -> int:
        return _RESOLUTION_HEIGHTS.get(self.resolution, 0)

    def output_size(self, source_width: int, source_height: int) -> Tuple[int, int]:
        """按来源尺寸算出实际输出尺寸（保持比例、宽高取偶、不放大）。"""
        target_h = self.target_height
        if target_h <= 0 or target_h >= source_height:
            return even(source_width), even(source_height)
        scale = target_h / float(source_height)
        height = even(target_h)
        width = even(round(source_width * scale))
        return max(16, width), max(16, height)

    def bitrate_for(self, width: int, height: int) -> int:
        """返回实际使用的码率（kbps）。"""
        if self.bitrate_kbps > 0:
            return int(self.bitrate_kbps)
        bpp = _BITS_PER_PIXEL.get(self.quality, 0.12)
        kbps = width * height * int(self.fps) * bpp / 1000.0
        return int(max(800.0, min(80000.0, kbps)))

    @property
    def profile_value(self) -> int:
        return PROFILE_VALUES.get(self.profile, 100)

    def describe(self, source_size: Optional[Tuple[int, int]] = None) -> str:
        parts = [
            f"{RESOLUTION_LABELS.get(self.resolution, self.resolution)}",
            f"{self.fps}fps",
            describe_quality(self.quality),
            self.profile,
        ]
        if self.bitrate_kbps:
            parts.append(f"{self.bitrate_kbps} kbps")
        else:
            parts.append("自动码率")
        if source_size:
            out = self.output_size(*source_size)
            parts.append(f"输出 {out[0]}x{out[1]}")
        return " / ".join(parts)

    # ---------------- 文件名 ----------------
    def build_stem(self, target: Target, moment: Optional[datetime] = None) -> str:
        """生成输出文件名（不含扩展名）。"""
        moment = moment or datetime.now()
        app = target.app_label or target.title or "屏幕"
        if target.kind == TARGET_SCREEN:
            app = "屏幕"
        stem = str(self.filename_pattern)
        stem = (stem
                .replace("{app}", sanitize_filename_part(app, 40))
                .replace("{date}", moment.strftime("%Y%m%d"))
                .replace("{time}", moment.strftime("%H%M%S"))
                .replace("{datetime}", moment.strftime("%Y%m%d_%H%M%S"))
                .replace("{resolution}", self.resolution)
                .replace("{fps}", str(self.fps)))
        return sanitize_filename_part(stem, 80)

    def next_output_path(self, target: Target, moment: Optional[datetime] = None) -> Path:
        directory = Path(self.output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return unique_path(directory, self.build_stem(target, moment) + ".mp4")


# ---------------------------------------------------------------------------
# 配置持久化（与界面设置放在同一个 settings.ini 的 [record] 段）
# ---------------------------------------------------------------------------
_SECTION = "record"


def _to_bool(text: str, default: bool = False) -> bool:
    if text is None:
        return default
    return str(text).strip().lower() in ("1", "true", "yes", "on")


def load_record_config() -> RecordingConfig:
    """从 settings.ini 读取录屏参数（缺失时返回默认值）。"""
    config = RecordingConfig()
    config.output_dir = str(default_record_dir())
    path = paths.settings_file()
    if not path.exists():
        return config
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        return config
    if not parser.has_section(_SECTION):
        return config
    section = parser[_SECTION]

    def get(name: str, fallback: str) -> str:
        return section.get(name, fallback).strip()

    config.output_dir = get("output_dir", config.output_dir) or config.output_dir
    config.resolution = get("resolution", config.resolution)
    config.quality = get("quality", config.quality)
    config.profile = get("profile", config.profile)
    config.input_format = get("input_format", config.input_format)
    config.scale_mode = get("scale_mode", config.scale_mode)
    config.filename_pattern = section.get("filename_pattern", config.filename_pattern)
    config.preferred_encoder = section.get("preferred_encoder", "")
    try:
        config.fps = int(get("fps", str(config.fps)))
        config.bitrate_kbps = int(get("bitrate_kbps", str(config.bitrate_kbps)))
        config.start_delay = float(get("start_delay", str(config.start_delay)))
        config.max_duration = float(get("max_duration", str(config.max_duration)))
    except ValueError:
        pass
    config.capture_cursor = _to_bool(section.get("capture_cursor"), config.capture_cursor)
    config.hide_window = _to_bool(section.get("hide_window"), config.hide_window)
    return config


def save_record_config(config: RecordingConfig) -> None:
    """把录屏参数写回 settings.ini 的 [record] 段（保留其它段）。"""
    path = paths.settings_file()
    parser = configparser.ConfigParser()
    try:
        if path.exists():
            parser.read(path, encoding="utf-8")
    except Exception:
        parser = configparser.ConfigParser()
    if not parser.has_section(_SECTION):
        parser.add_section(_SECTION)
    section = parser[_SECTION]
    section["output_dir"] = str(config.output_dir)
    section["resolution"] = config.resolution
    section["fps"] = str(int(config.fps))
    section["quality"] = config.quality
    section["bitrate_kbps"] = str(int(config.bitrate_kbps))
    section["profile"] = config.profile
    section["input_format"] = config.input_format
    section["scale_mode"] = config.scale_mode
    section["filename_pattern"] = config.filename_pattern
    section["start_delay"] = str(config.start_delay)
    section["max_duration"] = str(config.max_duration)
    section["capture_cursor"] = "1" if config.capture_cursor else "0"
    section["hide_window"] = "1" if config.hide_window else "0"
    section["preferred_encoder"] = config.preferred_encoder
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
    except Exception:
        pass
