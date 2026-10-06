# -*- coding: utf-8 -*-
"""录屏子模块：把屏幕/窗口录成 MP4，画质可调。

* :mod:`screen_capture.recorder.quality` —— 参数模型、画质档位与配置持久化
* :mod:`screen_capture.recorder.capture` —— GDI 取帧（含缩放与鼠标光标）
* :mod:`screen_capture.recorder.mf`      —— Windows Media Foundation 编码（H.264/MP4）
* :mod:`screen_capture.recorder.engine`  —— 录制线程、暂停/继续、录制中改画质
"""
from __future__ import annotations

from .quality import (
    DEFAULT_RECORD_PATTERN,
    FPS_CHOICES,
    INPUT_LABELS,
    PROFILE_CHOICES,
    QUALITY_LABELS,
    QUALITY_LEVELS,
    QUALITY_PRESETS,
    RESOLUTION_LABELS,
    RESOLUTION_PRESETS,
    QualityPreset,
    RecordingConfig,
    default_record_dir,
    describe_quality,
    load_record_config,
    save_record_config,
)
from .engine import (
    RecorderEngine,
    RecordingResult,
    RecordingStats,
    STATE_COUNTDOWN,
    STATE_IDLE,
    STATE_LABELS,
    STATE_PAUSED,
    STATE_RECORDING,
    STATE_STOPPING,
    make_encoder,
    probe_encoder,
)

__all__ = [
    "RecordingConfig", "QualityPreset", "QUALITY_PRESETS", "QUALITY_LEVELS", "QUALITY_LABELS",
    "RESOLUTION_PRESETS", "RESOLUTION_LABELS", "FPS_CHOICES", "PROFILE_CHOICES", "INPUT_LABELS",
    "DEFAULT_RECORD_PATTERN", "default_record_dir", "load_record_config", "save_record_config",
    "describe_quality",
    "RecorderEngine", "RecordingStats", "RecordingResult", "make_encoder", "probe_encoder",
    "STATE_IDLE", "STATE_COUNTDOWN", "STATE_RECORDING", "STATE_PAUSED", "STATE_STOPPING",
    "STATE_LABELS",
]
