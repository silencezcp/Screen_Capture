# -*- coding: utf-8 -*-
"""录屏引擎：独立线程取帧 → 编码 MP4，支持暂停、继续与录制中调节画质。

设计上刻意和截图引擎（`engine.CaptureEngine`）保持一致的用法：
构造时给出配置与事件回调，`start()` 后台跑，界面通过 `snapshot()` 轮询状态。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .. import applog
from ..engine import ConfigError, Target, TARGET_SCREEN, TARGET_WINDOW
from . import capture as cap
from . import mf
from .quality import RecordingConfig, describe_quality

logger = applog.get_logger()

__all__ = ["RecorderEngine", "RecordingStats", "make_encoder", "probe_encoder"]

# 状态
STATE_IDLE = "idle"
STATE_COUNTDOWN = "countdown"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"
STATE_STOPPING = "stopping"

STATE_LABELS = {
    STATE_IDLE: "未开始",
    STATE_COUNTDOWN: "倒计时",
    STATE_RECORDING: "录制中",
    STATE_PAUSED: "已暂停",
    STATE_STOPPING: "正在封盘",
}

# 编码器不可用时的提示
_ENCODER_HINT = (
    "无法使用 Windows 自带的 H.264 编码器（Media Foundation）。\n"
    "常见原因：系统缺少媒体功能包、远程桌面会话中禁用了硬件编码、"
    "或 Python 位数与系统不匹配。"
)

MAX_LAG_FRAMES = 3          # 落后超过这么多帧就丢帧，避免时间轴错乱


@dataclass
class RecordingStats:
    """录制过程中的实时状态（供界面显示）。"""

    state: str = STATE_IDLE
    elapsed: float = 0.0
    frames: int = 0
    dropped: int = 0
    actual_fps: float = 0.0
    output_width: int = 0
    output_height: int = 0
    fps: int = 0
    bitrate_kbps: int = 0
    bytes_written: int = 0
    current_file: str = ""
    segment: int = 1
    input_format: str = ""

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def size_text(self) -> str:
        return _human_size(self.bytes_written)

    @property
    def elapsed_text(self) -> str:
        seconds = int(self.elapsed)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    @property
    def average_bitrate(self) -> float:
        if self.elapsed <= 0:
            return 0.0
        return self.bytes_written * 8.0 / 1000.0 / self.elapsed


@dataclass
class RecordingResult:
    """录制结束后的汇总。"""

    files: List[Path] = field(default_factory=list)
    seconds: float = 0.0
    frames: int = 0
    dropped: int = 0
    reason: str = ""

    @property
    def total_bytes(self) -> int:
        total = 0
        for path in self.files:
            try:
                total += Path(path).stat().st_size
            except OSError:
                pass
        return total

    def summary(self) -> str:
        seconds = int(self.seconds)
        duration = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        return (f"{self.reason}：时长 {duration}，共 {self.frames} 帧"
                f"（丢 {self.dropped} 帧），{len(self.files)} 个文件，"
                f"{_human_size(self.total_bytes)}")


def _human_size(size: float) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size:.0f} B"


def probe_encoder() -> Tuple[bool, str]:
    """检测 H.264 编码链路是否可用，返回 (是否可用, 说明)。"""
    return mf.h264_available()


def make_encoder(config: RecordingConfig) -> mf.Mp4Encoder:
    """按配置创建编码器（输入格式与 H.264 档位）。"""
    return mf.Mp4Encoder(input_format=config.input_format, profile=config.profile_value)


class RecorderEngine:
    """把屏幕/窗口录成 MP4。

    事件（`on_event` 收到的字典）的 `type` 取值：
    ``started`` / ``countdown`` / ``recording`` / ``progress`` / ``file`` /
    ``paused`` / ``resumed`` / ``quality`` / ``error`` / ``finished``。
    """

    def __init__(self, config: RecordingConfig,
                 on_event: Optional[Callable[[Dict], None]] = None):
        self._config = config
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()      # set 表示"暂停中"
        self._lock = threading.Lock()
        self._state = STATE_IDLE
        self._stats = RecordingStats()
        self._result: Optional[RecordingResult] = None
        self._error: Optional[str] = None
        self._files: List[Path] = []

    # ---------------- 状态访问 ----------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._state in (STATE_COUNTDOWN, STATE_RECORDING, STATE_PAUSED, STATE_STOPPING)

    @property
    def result(self) -> Optional[RecordingResult]:
        return self._result

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def config(self) -> RecordingConfig:
        return self._config

    def snapshot(self) -> RecordingStats:
        return self._stats

    # ---------------- 控制 ----------------
    def apply_config(self, config: RecordingConfig) -> None:
        """录制过程中更新参数（分辨率/帧率/清晰度/码率变化会自动分段）。"""
        with self._lock:
            self._config = config

    def start(self, countdown: bool = True) -> None:
        """开始录制（后台线程）。"""
        if self.running:
            raise ConfigError("已经在录制中")
        self._config.validate()
        self._stop_event.clear()
        self._pause_event.clear()
        self._result = None
        self._error = None
        self._files = []
        self._stats = RecordingStats(
            state=STATE_COUNTDOWN if countdown and self._config.start_delay > 0 else STATE_RECORDING,
            fps=int(self._config.fps),
        )
        self._set_state(self._stats.state)
        self._thread = threading.Thread(target=self._run, name="ScreenRecorder", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self._state == STATE_RECORDING:
            self._pause_event.set()

    def resume(self) -> None:
        if self._state == STATE_PAUSED:
            self._pause_event.clear()

    def stop(self) -> None:
        """请求停止（不阻塞）。"""
        self._stop_event.set()
        self._pause_event.clear()

    def join(self, timeout: Optional[float] = None) -> Optional[RecordingResult]:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self._result

    def stop_and_wait(self, timeout: float = 30.0) -> RecordingResult:
        self.stop()
        result = self.join(timeout)
        if result is None:
            return RecordingResult(reason="停止超时（编码器仍在封盘）", files=list(self._files))
        return result

    # ---------------- 内部 ----------------
    def _set_state(self, state: str) -> None:
        self._state = state
        self._stats.state = state

    def _emit(self, event_type: str, **payload) -> None:
        if self._on_event is None:
            return
        event = {"type": event_type, "timestamp": datetime.now().strftime("%H:%M:%S")}
        event.update(payload)
        try:
            self._on_event(event)
        except Exception:  # pragma: no cover - 回调不应该影响录制
            logger.debug("录屏事件回调异常", exc_info=True)

    def _current_config(self) -> RecordingConfig:
        with self._lock:
            return self._config

    def _run(self) -> None:
        config = self._current_config()
        target = config.target
        com_ready = mf.com_initialize()
        encoder = make_encoder(config)
        grabber: Optional[cap.FrameGrabber] = None
        current_rect: Optional[Tuple[int, int, int, int]] = None
        base_stem = config.build_stem(target)
        part = 1
        frame_index = 0
        dropped = 0
        last_rect: Optional[Tuple[int, int, int, int]] = None
        start_time = 0.0
        mode = "encoder"
        timer = cap.HighResTimer() if cap.HighResTimer else None

        try:
            self._emit("started", target=target.describe(), output_dir=str(config.output_dir),
                       quality=config.describe())
            logger.info("开始录屏：目标=%s，参数=%s", target.describe(), config.describe())

            if config.start_delay > 0:
                remaining = int(round(config.start_delay))
                while remaining > 0 and not self._stop_event.is_set():
                    self._emit("countdown", remaining=remaining)
                    for _ in range(10):
                        if self._stop_event.is_set():
                            break
                        time.sleep(0.1)
                    remaining -= 1
                if self._stop_event.is_set():
                    self._finish("已取消")
                    return

            # 首次取帧：确定尺寸与输出路径
            rect = cap.source_rect_for(config, target)
            if rect is None:
                raise ConfigError("录制目标不可用（窗口可能已关闭）")
            current_rect = rect
            out_w, out_h = config.output_size(rect[2] - rect[0], rect[3] - rect[1])
            path = self._segment_path(config, base_stem, part)
            encoder.start_segment(str(path), out_w, out_h, config.fps,
                                  config.bitrate_for(out_w, out_h))
            grabber = cap.FrameGrabber(out_w, out_h, draw_cursor=config.capture_cursor,
                                       scale_mode=config.scale_mode)
            self._files.append(path)
            self._stats.current_file = str(path)
            self._stats.segment = part
            self._stats.output_width = out_w
            self._stats.output_height = out_h
            self._stats.bitrate_kbps = config.bitrate_for(out_w, out_h)
            self._stats.input_format = encoder.input_description
            self._emit("file", path=str(path))
            self._emit("quality", text=self._quality_text(config, out_w, out_h),
                       width=out_w, height=out_h, bitrate=self._stats.bitrate_kbps,
                       input_format=encoder.input_description)
            self._set_state(STATE_RECORDING)
            self._emit("recording")

            interval = 1.0 / max(1, int(config.fps))
            start_time = time.perf_counter()
            last_report = 0.0
            last_fps_at = 0.0
            last_fps_frames = 0

            while not self._stop_event.is_set():
                # 1) 暂停
                if self._pause_event.is_set():
                    if self._state != STATE_PAUSED:
                        self._set_state(STATE_PAUSED)
                        self._emit("paused")
                    time.sleep(0.02)
                    continue
                if self._state == STATE_PAUSED:
                    self._set_state(STATE_RECORDING)
                    self._emit("resumed")

                # 2) 参数热更新
                latest = self._current_config()
                if self._needs_restart(config, latest):
                    config = latest
                    part += 1
                    encoder.stop_segment()
                    rect = cap.source_rect_for(config, target)
                    if rect is None:
                        raise ConfigError("录制目标不可用（窗口可能已关闭）")
                    current_rect = rect
                    last_rect = None
                    out_w, out_h = config.output_size(rect[2] - rect[0], rect[3] - rect[1])
                    path = self._segment_path(config, base_stem, part)
                    encoder.start_segment(str(path), out_w, out_h, config.fps,
                                          config.bitrate_for(out_w, out_h))
                    grabber.resize(out_w, out_h)
                    grabber.draw_cursor = config.capture_cursor
                    grabber.set_scale_mode(config.scale_mode)
                    self._files.append(path)
                    interval = 1.0 / max(1, int(config.fps))
                    self._stats.current_file = str(path)
                    self._stats.segment = part
                    self._stats.output_width = out_w
                    self._stats.output_height = out_h
                    self._stats.bitrate_kbps = config.bitrate_for(out_w, out_h)
                    self._stats.input_format = encoder.input_description
                    self._stats.fps = int(config.fps)
                    self._emit("file", path=str(path))
                    self._emit("quality", text=self._quality_text(config, out_w, out_h),
                               width=out_w, height=out_h, bitrate=self._stats.bitrate_kbps,
                               input_format=encoder.input_description)
                else:
                    config = latest
                    grabber.draw_cursor = config.capture_cursor
                    grabber.set_scale_mode(config.scale_mode)

                # 3) 目标矩形（窗口会移动/缩放）
                rect = cap.source_rect_for(config, target)
                if rect is None:
                    self._emit("error", message="录制目标已关闭，自动停止录制")
                    break
                if rect != current_rect:
                    current_rect = rect
                    out_w, out_h = config.output_size(rect[2] - rect[0], rect[3] - rect[1])
                    if (out_w, out_h) != (grabber.width, grabber.height):
                        # 输出分辨率变化 → 换新分段
                        part += 1
                        encoder.stop_segment()
                        path = self._segment_path(config, base_stem, part)
                        encoder.start_segment(str(path), out_w, out_h, config.fps,
                                              config.bitrate_for(out_w, out_h))
                        grabber.resize(out_w, out_h)
                        self._files.append(path)
                        self._stats.current_file = str(path)
                        self._stats.segment = part
                        self._stats.output_width = out_w
                        self._stats.output_height = out_h
                        self._stats.bitrate_kbps = config.bitrate_for(out_w, out_h)
                        self._stats.input_format = encoder.input_description
                        self._emit("file", path=str(path))
                        self._emit("quality", text=self._quality_text(config, out_w, out_h),
                                   width=out_w, height=out_h, bitrate=self._stats.bitrate_kbps,
                                   input_format=encoder.input_description)
                last_rect = rect

                # 4) 限时
                if config.max_duration > 0 and frame_index * interval >= config.max_duration:
                    break

                # 5) 取帧 + 编码（时间戳按帧序推进，保证恒定帧率）
                elapsed = frame_index * interval
                now = time.perf_counter() - start_time
                if elapsed > now + MAX_LAG_FRAMES * interval:
                    dropped += 1
                    frame_index += 1
                    self._stats.dropped = dropped
                    continue
                frame = grabber.grab(rect)
                encoder.write(frame, elapsed * 1000.0)
                frame_index += 1

                # 6) 统计与事件
                if now - last_fps_at >= 0.5:
                    delta_frames = frame_index - last_fps_frames
                    delta_time = now - last_fps_at
                    if delta_time > 0:
                        self._stats.actual_fps = delta_frames / delta_time
                    last_fps_at, last_fps_frames = now, frame_index
                self._stats.elapsed = elapsed
                self._stats.frames = frame_index
                if now - last_report >= 0.25:
                    last_report = now
                    self._stats.bytes_written = self._files_size()
                    self._emit("progress", stats=self._stats)

                # 7) 帧率节流：用高精度等待定时器，避免 time.sleep 的 ~15ms 粒度把 60fps 拖成 30fps
                next_at = start_time + frame_index * interval
                sleep_for = next_at - time.perf_counter()
                if sleep_for > 0.0005:
                    if sleep_for > 0.002 and timer is not None:
                        timer.wait(sleep_for - 0.001)
                    while True:
                        remaining = next_at - time.perf_counter()
                        if remaining <= 0 or self._stop_event.is_set():
                            break
                        if remaining > 0.002:
                            time.sleep(0.001)
                        else:
                            time.sleep(0)

            self._set_state(STATE_STOPPING)
            self._emit("stopping")
        except Exception as exc:  # pragma: no cover - 运行期异常统一上报
            self._error = str(exc)
            message = f"{exc}"
            if "Media Foundation" in str(exc) or "编码" in str(exc):
                message = f"{message}\n{_ENCODER_HINT}"
            logger.exception("录屏失败")
            self._emit("error", message=message)
        finally:
            try:
                encoder.stop_segment()
            except Exception:
                logger.exception("结束录制分段失败")
            try:
                encoder.close()
            except Exception:
                pass
            if grabber is not None:
                grabber.close()
            if timer is not None:
                timer.close()
            try:
                self._stats.bytes_written = self._files_size()
            except Exception:
                pass
            reason = "已停止" if self._stop_event.is_set() else "已完成"
            if self._error:
                reason = "出错"
            self._finish(reason, frame_index, dropped)
            if com_ready:
                mf.com_uninitialize()

    def _finish(self, reason: str, frames: int = 0, dropped: int = 0) -> None:
        result = RecordingResult(
            files=list(self._files), seconds=self._stats.elapsed,
            frames=frames or self._stats.frames,
            dropped=dropped or self._stats.dropped, reason=reason)
        self._result = result
        self._stats.elapsed = result.seconds
        self._stats.bytes_written = result.total_bytes
        self._set_state(STATE_IDLE)
        self._emit("finished", result=result, summary=result.summary())

    def _needs_restart(self, current: RecordingConfig, latest: RecordingConfig) -> bool:
        return (int(latest.fps) != int(current.fps)
                or latest.resolution != current.resolution
                or latest.quality != current.quality
                or int(latest.bitrate_kbps) != int(current.bitrate_kbps))

    @staticmethod
    def _quality_text(config: RecordingConfig, width: int, height: int) -> str:
        rate = config.bitrate_for(width, height)
        return (f"{width}x{height} · {int(config.fps)}fps · "
                f"{describe_quality(config.quality)} · {rate} kbps")

    def _segment_path(self, config: RecordingConfig, base_stem: str, part: int) -> Path:
        from ..engine import unique_path

        directory = Path(config.output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stem = base_stem if part <= 1 else f"{base_stem}_part{part}"
        return unique_path(directory, stem + ".mp4")

    def _files_size(self) -> int:
        total = 0
        for path in self._files:
            try:
                total += Path(path).stat().st_size
            except OSError:
                pass
        return total
