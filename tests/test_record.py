# -*- coding: utf-8 -*-
"""录屏功能测试：参数计算、路径生成、编码器可用性与真实录制产出。

运行：python tests/test_record.py
没有图形界面也能跑（录制屏幕本身不需要窗口），需要 Windows + Media Foundation。
"""
from __future__ import annotations

import shutil
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from screen_capture import recorder as rec  # noqa: E402
from screen_capture.engine import Target, TARGET_SCREEN  # noqa: E402

WORK = ROOT / "_test_out" / "record"


class RecordingConfigTests(unittest.TestCase):
    """纯计算部分，不需要编码器。"""

    def test_output_size_keeps_aspect_and_even_numbers(self):
        config = rec.RecordingConfig(resolution="720p")
        width, height = config.output_size(2560, 1440)
        self.assertEqual(height, 720)
        self.assertEqual(width, 1280)
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)

    def test_output_size_never_upscales(self):
        config = rec.RecordingConfig(resolution="2160p")
        self.assertEqual(config.output_size(1280, 720), (1280, 720))
        config = rec.RecordingConfig(resolution="native")
        self.assertEqual(config.output_size(1920, 1080), (1920, 1080))

    def test_odd_source_size_becomes_even(self):
        config = rec.RecordingConfig(resolution="native")
        self.assertEqual(config.output_size(1921, 1081), (1920, 1080))

    def test_bitrate_grows_with_resolution_and_quality(self):
        small = rec.RecordingConfig(resolution="480p", quality="low", fps=15)
        large = rec.RecordingConfig(resolution="1080p", quality="ultra", fps=30)
        self.assertLess(small.bitrate_for(854, 480), large.bitrate_for(1920, 1080))

    def test_manual_bitrate_wins(self):
        config = rec.RecordingConfig(bitrate_kbps=4321)
        self.assertEqual(config.bitrate_for(1920, 1080), 4321)

    def test_all_presets_are_valid(self):
        for preset in rec.QUALITY_PRESETS:
            config = rec.RecordingConfig(resolution=preset.resolution, fps=preset.fps,
                                         quality=preset.quality,
                                         bitrate_kbps=preset.bitrate_kbps)
            config.target = Target(kind=TARGET_SCREEN)
            config.output_dir = str(WORK)
            config.validate()

    def test_validate_rejects_bad_values(self):
        from screen_capture.engine import ConfigError

        config = rec.RecordingConfig(output_dir=str(WORK), fps=0)
        config.target = Target(kind=TARGET_SCREEN)
        with self.assertRaises(ConfigError):
            config.validate()

        config = rec.RecordingConfig(output_dir=str(WORK), resolution="999p")
        config.target = Target(kind=TARGET_SCREEN)
        with self.assertRaises(ConfigError):
            config.validate()

        config = rec.RecordingConfig(output_dir="")
        config.target = Target(kind=TARGET_SCREEN)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_filename_template(self):
        config = rec.RecordingConfig(filename_pattern="{app}_{date}_{time}",
                                     resolution="720p", fps=25)
        target = Target(kind=TARGET_SCREEN)
        stem = config.build_stem(target)
        self.assertTrue(stem.startswith("屏幕_"), stem)
        self.assertNotIn("{", stem)

    def test_config_roundtrip(self):
        from unittest.mock import patch

        settings = WORK / "settings.ini"
        config = rec.RecordingConfig(output_dir=str(WORK), resolution="720p", fps=25,
                                     quality="ultra", bitrate_kbps=3000, profile="main",
                                     capture_cursor=False, max_duration=12.5)
        with patch("screen_capture.paths.settings_file", return_value=settings):
            rec.save_record_config(config)
            self.assertTrue(settings.exists())
            loaded = rec.load_record_config()
        self.assertEqual(loaded.resolution, "720p")
        self.assertEqual(loaded.fps, 25)
        self.assertEqual(loaded.quality, "ultra")
        self.assertEqual(loaded.bitrate_kbps, 3000)
        self.assertEqual(loaded.profile, "main")
        self.assertFalse(loaded.capture_cursor)
        self.assertAlmostEqual(loaded.max_duration, 12.5)


class RecordingIntegrationTests(unittest.TestCase):
    """真实录制（需要 H.264 编码器）。"""

    @classmethod
    def setUpClass(cls):
        cls.available, cls.message = rec.probe_encoder()
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True, exist_ok=True)

    def _record(self, seconds: float, **kwargs) -> tuple:
        """按 max_duration 录一段并等它自然结束。"""
        config = rec.RecordingConfig(
            target=Target(kind=TARGET_SCREEN),
            output_dir=str(WORK),
            filename_pattern=kwargs.pop("filename_pattern", "rec_{date}_{time}"),
            start_delay=0.0,
            max_duration=seconds,
            capture_cursor=True,
        )
        for key, value in kwargs.items():
            setattr(config, key, value)

        events = []
        engine = rec.RecorderEngine(config, on_event=events.append)
        engine.start(countdown=False)
        deadline = time.time() + max(20.0, seconds * 5)
        while engine.running and time.time() < deadline:
            time.sleep(0.1)
        result = engine.stop_and_wait(timeout=max(30.0, seconds * 4))
        return result, events

    def test_records_playable_mp4(self):
        if not self.available:
            self.skipTest(f"编码器不可用：{self.message}")
        result, events = self._record(2.0, resolution="480p", fps=15, quality="low")
        self.assertEqual(len(result.files), 1, f"事件：{[e.get('type') for e in events]}")
        path = Path(result.files[0])
        self.assertTrue(path.exists(), path)
        # 静态画面压缩后可能很小，所以只校验"结构完整"而不是文件大小
        self.assertGreater(path.stat().st_size, 1024, "文件过小，可能没写入有效数据")
        data = path.read_bytes()
        self.assertIn(b"ftyp", data[:64], "不是有效的 MP4（缺少 ftyp box）")
        self.assertIn(b"moov", data, "缺少 moov（未封盘）")
        self.assertIn(b"mdat", data, "缺少 mdat（没有视频数据）")
        # 与设置一致的帧数（15fps × 2 秒，允许少量误差）
        self.assertGreaterEqual(result.frames, 20, f"帧数太少：{result.frames}")
        self.assertLessEqual(result.frames, 40, f"帧数异常偏多：{result.frames}")

    def test_quality_settings_change_encoder_parameters(self):
        """清晰度/分辨率/帧率必须真的作用到编码参数上（不依赖屏幕内容）。"""
        low, low_events = self._record(1.5, resolution="480p", fps=15, quality="low",
                                       filename_pattern="qlow_{time}")
        high, high_events = self._record(1.5, resolution="480p", fps=15, quality="ultra",
                                         filename_pattern="qhigh_{time}")

        def bitrate_of(events):
            for event in events:
                if event.get("type") == "quality":
                    return event.get("bitrate")
            return None

        low_rate = bitrate_of(low_events)
        high_rate = bitrate_of(high_events)
        self.assertIsNotNone(low_rate, "没有收到画质事件")
        self.assertIsNotNone(high_rate, "没有收到画质事件")
        self.assertGreater(high_rate, low_rate,
                           f"极高清晰度的码率应大于低清晰度：{high_rate} vs {low_rate}")

        # 分辨率档位作用到输出尺寸上
        def size_of(events):
            for event in events:
                if event.get("type") == "quality":
                    return (event.get("width"), event.get("height"))
            return None

        size = size_of(high_events)
        self.assertIsNotNone(size)
        self.assertLessEqual(size[1], 480, f"选择 480p 却输出了 {size}")

    def test_manual_bitrate_is_respected(self):
        if not self.available:
            self.skipTest(f"编码器不可用：{self.message}")
        _, events = self._record(1.5, resolution="480p", fps=15, bitrate_kbps=1234,
                                 filename_pattern="manual_{time}")
        rates = [e.get("bitrate") for e in events if e.get("type") == "quality"]
        self.assertTrue(rates, "没有收到画质事件")
        self.assertEqual(rates[0], 1234, f"手动码率没有生效：{rates[0]}")

    def test_mid_recording_quality_change_starts_new_segment(self):
        if not self.available:
            self.skipTest(f"编码器不可用：{self.message}")
        config = rec.RecordingConfig(
            target=Target(kind=TARGET_SCREEN),
            output_dir=str(WORK),
            filename_pattern="multi_{time}",
            resolution="480p", fps=15, quality="low",
            start_delay=0.0, max_duration=4.0,
        )
        events = []
        engine = rec.RecorderEngine(config, on_event=events.append)
        engine.start(countdown=False)
        time.sleep(1.2)
        engine.apply_config(replace(config, resolution="720p", quality="high"))
        # 给引擎一点时间处理"参数变化 → 开始新分段"，再主动停止
        time.sleep(1.0)
        result = engine.stop_and_wait(timeout=40.0)
        segments = [(e.get("type"), e.get("text") or e.get("path")) for e in events
                    if e.get("type") in ("quality", "file", "error")]
        self.assertGreaterEqual(result.frames, 10, f"帧数太少：{result.frames}")
        # 分辨率变化必须产生新分段（两个文件），且旧分段不能丢
        self.assertGreaterEqual(
            len(result.files), 2,
            f"改画质后没有分段：files={result.files} 事件={segments} 错误={engine.error}")
        for path in result.files:
            self.assertTrue(Path(path).exists(), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
