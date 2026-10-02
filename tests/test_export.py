from __future__ import annotations

import io
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from replay_renderer.errors import ReplayRendererError
from replay_renderer.export import ExportOptions, FrameRenderer, ffmpeg_details, realtime_stats, render_export, safe_video_filename


def payload() -> dict:
    return {
        "meta": {"title": "Test", "artist": "Artist", "creator": "Maker", "version": "Hard", "player": "Player", "keyCount": 4, "chartFormat": "malody", "activeMods": ["FLIP"]},
        "timeline": {"startMs": 0, "endMs": 100, "audioOffsetMs": 0, "maxHoldDurationMs": 100, "maxInputDurationMs": 100},
        "notes": [
            {"id": 0, "lane": 3, "timeMs": 25, "judgementTimeMs": 20, "isHoldHead": False, "endTimeMs": None, "judgement": "perfect", "releaseTimeMs": None, "releaseBreaksCombo": False},
            {"id": 1, "lane": 0, "timeMs": 45, "judgementTimeMs": 50, "isHoldHead": True, "endTimeMs": 90, "judgement": "good", "releaseTimeMs": 60, "releaseBreaksCombo": True, "regrabs": [{"timeMs": 70, "releaseTimeMs": None, "releaseBreaksCombo": False}]},
        ],
        "inputs": [
            {"id": 0, "lane": 3, "timeMs": 20, "releaseTimeMs": None, "judgement": "perfect"},
            {"id": 1, "lane": 0, "timeMs": 50, "releaseTimeMs": 60, "judgement": "good"},
        ],
    }


def options(left: bool = False, right: bool = False) -> ExportOptions:
    return ExportOptions(left, right, 240, 360, 600, 0)


class ExportTests(unittest.TestCase):
    def test_options_and_safe_filename(self) -> None:
        parsed = ExportOptions.from_json({"showChartInfo": False, "showRealtimeJudgements": True, "trackWidth": 480, "trackHeight": 720, "fallSpeed": 600, "audioDelayMs": -300})
        self.assertTrue(parsed.show_realtime_judgements)
        self.assertEqual(parsed.track_width, 480)
        with self.assertRaises(ReplayRendererError):
            ExportOptions.from_json({"trackWidth": 481, "trackHeight": 720, "fallSpeed": 600, "audioDelayMs": 0})
        self.assertEqual(safe_video_filename('A<B', 'H:ard'), 'A_B - H_ard.mp4')

    def test_realtime_stats_boundaries_and_weights(self) -> None:
        notes = [
            {"judgementTimeMs": 10, "judgement": "perfect"},
            {"judgementTimeMs": 20, "judgement": "cool"},
            {"judgementTimeMs": 30, "judgement": "good"},
            {"judgementTimeMs": 40, "judgement": "miss"},
        ]
        self.assertEqual(realtime_stats(notes, 9)["accuracy"], 100)
        self.assertEqual(realtime_stats(notes, 10)["perfect"], 1)
        self.assertAlmostEqual(realtime_stats(notes, 40)["accuracy"], 53.75)

    def test_frame_widths_and_track_pixels(self) -> None:
        plain = FrameRenderer(payload(), options()).render(25)
        left = FrameRenderer(payload(), options(True)).render(25)
        both = FrameRenderer(payload(), options(True, True)).render(25)
        self.assertEqual(plain.size, (240, 360))
        self.assertEqual(left.size, (560, 360))
        self.assertEqual(both.size, (800, 360))
        # Flip has already been applied by analysis: the first note is drawn in the rightmost lane.
        self.assertNotEqual(plain.getpixel((225, 297)), (8, 11, 14))
        # A broken hold is red between release and re-grab.
        broken_y = 307
        broken = FrameRenderer(payload(), options()).render(65)
        self.assertEqual(broken.getpixel((4, broken_y)), (237, 88, 88))

    def test_long_chart_information_wraps_inside_left_panel(self) -> None:
        long_payload = payload()
        long_value = "VeryLongUnbrokenSongInformation" * 8
        for key in ("title", "artist", "version", "player"):
            long_payload["meta"][key] = long_value
        plain = FrameRenderer(long_payload, options()).render(25)
        with_info = FrameRenderer(long_payload, options(True)).render(25)
        # Text must never modify the track area beginning at x=320.
        self.assertEqual(with_info.crop((320, 0, 560, 360)).tobytes(), plain.tobytes())

    def test_short_mp4_contains_h264_and_aac(self) -> None:
        audio_buffer = io.BytesIO()
        with wave.open(audio_buffer, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000); wav.writeframes(b"\0\0" * 1600)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "test.mp4"
            progress = []
            render_export(payload(), audio_buffer.getvalue(), "audio/wav", options(), target, threading.Event(), lambda done, total: progress.append((done, total)))
            self.assertTrue(target.is_file())
            ffmpeg, _ = ffmpeg_details()
            probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(target)], capture_output=True, text=True)
            details = probe.stderr
            self.assertIn("Video: h264", details)
            self.assertIn("Audio: aac", details)
            self.assertIn("60 fps", details)
            self.assertEqual(progress[-1], (6, 6))


if __name__ == "__main__":
    unittest.main()
