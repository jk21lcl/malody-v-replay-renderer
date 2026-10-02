from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import threading
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from .errors import ReplayRendererError


FPS = 60
LEFT_PANEL_WIDTH = 320
RIGHT_PANEL_WIDTH = 240
COLORS = {
    "perfect": "#ffffff",
    "cool": "#f1c84b",
    "good": "#51c878",
    "miss": "#ed5858",
}
WEIGHTS = {"perfect": 100, "cool": 75, "good": 40, "miss": 0}


@dataclass(frozen=True)
class ExportOptions:
    show_chart_info: bool
    show_realtime_judgements: bool
    track_width: int
    track_height: int
    fall_speed: float
    audio_delay_ms: float

    @classmethod
    def from_json(cls, value: object) -> "ExportOptions":
        if not isinstance(value, dict):
            raise ReplayRendererError("invalid_export_options", "Export options must be an object.")

        def boolean(name: str) -> bool:
            result = value.get(name, False)
            if not isinstance(result, bool):
                raise ReplayRendererError("invalid_export_options", f"{name} must be true or false.")
            return result

        def number(name: str, minimum: float, maximum: float) -> float:
            result = value.get(name)
            if isinstance(result, bool) or not isinstance(result, (int, float)) or not math.isfinite(result):
                raise ReplayRendererError("invalid_export_options", f"{name} must be a finite number.")
            if not minimum <= result <= maximum:
                raise ReplayRendererError(
                    "invalid_export_options", f"{name} must be between {minimum:g} and {maximum:g}."
                )
            return float(result)

        track_width = number("trackWidth", 240, 1200)
        track_height = number("trackHeight", 360, 1000)
        if not track_width.is_integer() or not track_height.is_integer() or int(track_width) % 2 or int(track_height) % 2:
            raise ReplayRendererError(
                "invalid_export_options", "trackWidth and trackHeight must be even whole numbers."
            )
        return cls(
            show_chart_info=boolean("showChartInfo"),
            show_realtime_judgements=boolean("showRealtimeJudgements"),
            track_width=int(track_width),
            track_height=int(track_height),
            fall_speed=number("fallSpeed", 100, 2000),
            audio_delay_ms=number("audioDelayMs", -1000, 1000),
        )


def realtime_stats(notes: list[dict], time_ms: float) -> dict:
    counts = {name: 0 for name in WEIGHTS}
    for note in notes:
        if note["judgementTimeMs"] <= time_ms:
            counts[note["judgement"]] += 1
    total = sum(counts.values())
    accuracy = (
        sum(counts[name] * weight for name, weight in WEIGHTS.items()) / total
        if total
        else 100.0
    )
    return {"accuracy": accuracy, **counts}


def safe_video_filename(title: str, version: str) -> str:
    stem = " - ".join(part.strip() for part in (title, version) if part.strip()) or "replay"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).rstrip(" .")
    return f"{stem[:180] or 'replay'}.mp4"


def ffmpeg_details() -> tuple[str, str]:
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    completed = subprocess.run(
        [executable, "-hide_banner", "-version"], capture_output=True, text=True, check=True
    )
    encoders = subprocess.run(
        [executable, "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
    ).stdout
    if "libx264" not in encoders or re.search(r"\baac\s", encoders) is None:
        raise ReplayRendererError(
            "missing_ffmpeg_encoder", "Bundled FFmpeg must provide libx264 and AAC encoders."
        )
    return executable, completed.stdout.strip()


def _font(size: int, bold: bool = False):
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [
        windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
        windows / ("arialbd.ttf" if bold else "arial.ttf"),
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            pass
    return ImageFont.load_default()


class FrameRenderer:
    def __init__(self, payload: dict, options: ExportOptions) -> None:
        self.payload = payload
        self.options = options
        self.left = LEFT_PANEL_WIDTH if options.show_chart_info else 0
        self.right = RIGHT_PANEL_WIDTH if options.show_realtime_judgements else 0
        self.width = self.left + options.track_width + self.right
        self.height = options.track_height
        self.note_times = [row["timeMs"] for row in payload["notes"]]
        self.input_times = [row["timeMs"] for row in payload["inputs"]]
        events = sorted((row["judgementTimeMs"], row["judgement"]) for row in payload["notes"])
        self.judgement_times = [item[0] for item in events]
        running = {name: 0 for name in WEIGHTS}
        self.prefix = [{**running}]
        for _, judgement in events:
            running = {**running}
            running[judgement] += 1
            self.prefix.append(running)
        self.font_small = _font(15)
        self.font_medium = _font(20, True)
        self.font_large = _font(38, True)

    def stats_at(self, time_ms: float) -> dict:
        index = bisect_right(self.judgement_times, time_ms)
        counts = self.prefix[index]
        total = sum(counts.values())
        accuracy = sum(counts[name] * WEIGHTS[name] for name in WEIGHTS) / total if total else 100.0
        return {"accuracy": accuracy, **counts}

    def _y(self, event_ms: float, chart_ms: float) -> float:
        judge_y = self.height - 55
        return judge_y - (event_ms - chart_ms) * self.options.fall_speed / 1000

    def _hold_transitions(self, note: dict) -> list[tuple[float, str]]:
        raw: list[tuple[float, str, int]] = []

        def add(time_ms: float, color: str) -> None:
            raw.append((max(note["timeMs"], time_ms), color, len(raw)))

        add(note["timeMs"], COLORS[note["judgement"]])
        if note["releaseBreaksCombo"] and note["releaseTimeMs"] < note["endTimeMs"]:
            add(note["releaseTimeMs"], COLORS["miss"])
        for regrab in note.get("regrabs", []):
            if regrab["timeMs"] < note["endTimeMs"]:
                add(regrab["timeMs"], COLORS["perfect"])
            if regrab["releaseBreaksCombo"] and regrab["releaseTimeMs"] < note["endTimeMs"]:
                add(regrab["releaseTimeMs"], COLORS["miss"])
        raw.sort(key=lambda item: (item[0], item[2]))
        transitions: list[tuple[float, str]] = []
        for time_ms, color, _ in raw:
            if transitions and transitions[-1][0] == time_ms:
                transitions[-1] = (time_ms, color)
            else:
                transitions.append((time_ms, color))
        return transitions

    def render(self, chart_ms: float) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), "#0a0e12")
        draw = ImageDraw.Draw(image)
        x0 = self.left
        track_right = x0 + self.options.track_width
        draw.rectangle((x0, 0, track_right - 1, self.height - 1), fill="#080b0e", outline="#2c3742")
        key_count = self.payload["meta"]["keyCount"]
        lane_w = self.options.track_width / key_count
        judge_y = self.height - 55
        speed = self.options.fall_speed / 1000
        for lane in range(1, key_count):
            x = round(x0 + lane * lane_w)
            draw.line((x, 0, x, self.height), fill="#29323b", width=1)

        visible_start = chart_ms - (self.height - judge_y + 30) / speed
        visible_end = chart_ms + (judge_y + 30) / speed
        notes = self.payload["notes"]
        begin = bisect_left(self.note_times, visible_start - self.payload["timeline"]["maxHoldDurationMs"])
        end = bisect_right(self.note_times, visible_end)
        for note in notes[begin:end]:
            end_ms = note["endTimeMs"] if note["endTimeMs"] is not None else note["timeMs"]
            if end_ms < visible_start:
                continue
            left = x0 + note["lane"] * lane_w + 4
            right = left + lane_w - 8
            color = COLORS[note["judgement"]]
            if note["endTimeMs"] is None:
                y = self._y(note["timeMs"], chart_ms)
                draw.rectangle((round(left), round(y - 8), round(right), round(y + 8)), outline=color, width=2 if note["judgement"] == "miss" else 1)
                continue
            transitions = self._hold_transitions(note)
            for index, (from_ms, segment_color) in enumerate(transitions):
                to_ms = min(note["endTimeMs"], transitions[index + 1][0] if index + 1 < len(transitions) else note["endTimeMs"])
                if to_ms <= from_ms:
                    continue
                y1, y2 = self._y(from_ms, chart_ms), self._y(to_ms, chart_ms)
                width = 2 if segment_color == COLORS["miss"] else 1
                draw.line((left, y1, left, y2), fill=segment_color, width=width)
                draw.line((right, y1, right, y2), fill=segment_color, width=width)
            draw.line((left, self._y(note["timeMs"], chart_ms), right, self._y(note["timeMs"], chart_ms)), fill=color, width=2)
            final_color = transitions[-1][1]
            draw.line((left, self._y(note["endTimeMs"], chart_ms), right, self._y(note["endTimeMs"], chart_ms)), fill=final_color, width=2 if final_color == COLORS["miss"] else 1)

        inputs = self.payload["inputs"]
        begin = bisect_left(self.input_times, visible_start - self.payload["timeline"]["maxInputDurationMs"])
        end = bisect_right(self.input_times, visible_end)
        for event in inputs[begin:end]:
            release = event["releaseTimeMs"]
            if (release if release is not None else event["timeMs"]) < visible_start:
                continue
            lane_left = x0 + event["lane"] * lane_w
            if release is not None:
                x = lane_left + lane_w / 2
                y1, y2 = self._y(event["timeMs"], chart_ms), self._y(release, chart_ms)
                length = abs(y2 - y1)
                direction = 1 if y2 >= y1 else -1
                for offset in range(0, round(length) + 1, 12):
                    start = y1 + direction * offset
                    finish = y1 + direction * min(offset + 6, length)
                    draw.line((x, start, x, finish), fill="#ffffff", width=2)
            y = self._y(event["timeMs"], chart_ms)
            margin = lane_w * 0.2
            draw.line((lane_left + margin, y, lane_left + lane_w - margin, y), fill=COLORS[event["judgement"]], width=5)
        draw.line((x0, judge_y, track_right, judge_y), fill="#e6edf3", width=2)

        if self.options.show_chart_info:
            # Draw onto a separate panel so even unexpected font metrics can
            # never place chart information over the track.
            panel = Image.new("RGB", (LEFT_PANEL_WIDTH, self.height), "#0a0e12")
            self._draw_chart_info(ImageDraw.Draw(panel))
            image.paste(panel, (0, 0))
        if self.options.show_realtime_judgements:
            self._draw_realtime(draw, chart_ms, track_right)
        return image

    def _draw_chart_info(self, draw: ImageDraw.ImageDraw) -> None:
        meta = self.payload["meta"]
        draw.text((24, 24), "TRACK", fill="#66d9c4", font=self.font_small)
        max_width = LEFT_PANEL_WIDTH - 48
        y = 52
        for line in self._wrapped_lines(draw, meta["title"], self.font_medium, max_width, 4):
            draw.text((24, y), line, fill="#e8edf2", font=self.font_medium)
            y += 26
        format_name = "osu!mania" if meta["chartFormat"] == "osu-mania" else "Malody"
        mods = " + ".join(meta["activeMods"]) or "No Mod"
        lines = [meta["artist"], f'{meta["keyCount"]}K · {format_name}', meta["version"], mods, meta["player"]]
        y += 8
        for value in lines:
            for line in self._wrapped_lines(draw, str(value), self.font_small, max_width, 3):
                draw.text((24, y), line, fill="#8e9cab", font=self.font_small)
                y += 21
            y += 4

    @staticmethod
    def _wrapped_lines(
        draw: ImageDraw.ImageDraw,
        value: object,
        font: ImageFont.ImageFont,
        max_width: int,
        max_lines: int,
    ) -> list[str]:
        """Wrap Latin, CJK, and unbroken names without crossing the info panel."""
        result: list[str] = []
        paragraphs = str(value).splitlines() or [""]
        for paragraph in paragraphs:
            current = ""
            for character in paragraph:
                candidate = current + character
                if current and draw.textlength(candidate, font=font) > max_width:
                    break_at = current.rfind(" ")
                    if break_at > 0:
                        result.append(current[:break_at].rstrip())
                        current = current[break_at + 1 :].lstrip() + character
                    else:
                        result.append(current.rstrip())
                        current = character.lstrip()
                else:
                    current = candidate
            if current or not paragraph:
                result.append(current.rstrip())
        if len(result) <= max_lines:
            return result
        result = result[:max_lines]
        last = result[-1].rstrip()
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        result[-1] = last + "…"
        return result

    def _draw_realtime(self, draw: ImageDraw.ImageDraw, chart_ms: float, x0: float) -> None:
        stats = self.stats_at(chart_ms)
        x = x0 + 24
        draw.text((x, 28), "ACCURACY", fill="#66d9c4", font=self.font_small)
        draw.text((x, 54), f'{stats["accuracy"]:.2f}%', fill="#e8edf2", font=self.font_large)
        y = 125
        for label, key in (("Perfect", "perfect"), ("Cool", "cool"), ("Good", "good"), ("Miss", "miss")):
            draw.text((x, y), label, fill=COLORS[key], font=self.font_small)
            draw.text((x + 125, y), str(stats[key]), fill="#e8edf2", font=self.font_medium)
            y += 38


def render_export(
    payload: dict,
    audio: bytes | None,
    audio_mime: str | None,
    options: ExportOptions,
    output_path: Path,
    cancel_event: threading.Event,
    progress: Callable[[int, int], None],
) -> str | None:
    ffmpeg, _ = ffmpeg_details()
    renderer = FrameRenderer(payload, options)
    start_ms = payload["timeline"]["startMs"]
    end_ms = payload["timeline"]["endMs"]
    duration = (end_ms - start_ms) / 1000
    total_frames = max(1, math.ceil(duration * FPS))
    warning = None if audio is not None else "谱面不含可用音频，已导出静音 MP4。"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="malody-export-") as temp_dir:
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{renderer.width}x{renderer.height}",
            "-r", str(FPS), "-i", "pipe:0",
        ]
        if audio is not None:
            extension = {"audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav"}.get(audio_mime or "", ".audio")
            audio_path = Path(temp_dir) / f"audio{extension}"
            audio_path.write_bytes(audio)
            audio_start_seconds = (payload["timeline"]["audioOffsetMs"] + options.audio_delay_ms - start_ms) / 1000
            if audio_start_seconds < 0:
                command += ["-ss", f"{-audio_start_seconds:.6f}"]
            command += ["-i", str(audio_path)]
            if audio_start_seconds > 0:
                command += ["-filter_complex", f"[1:a]adelay={round(audio_start_seconds * 1000)}:all=1,apad[a]", "-map", "0:v", "-map", "[a]"]
            else:
                command += ["-filter_complex", "[1:a]apad[a]", "-map", "0:v", "-map", "[a]"]
        command += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
        if audio is not None:
            command += ["-c:a", "aac", "-b:a", "192k"]
        command += ["-t", f"{duration:.6f}", "-movflags", "+faststart", str(output_path)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert process.stdin is not None
            for frame_index in range(total_frames):
                if cancel_event.is_set():
                    raise InterruptedError("Export cancelled.")
                chart_ms = start_ms + frame_index * 1000 / FPS
                process.stdin.write(renderer.render(chart_ms).tobytes())
                progress(frame_index + 1, total_frames)
            process.stdin.close()
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            if process.stderr is not None:
                process.stderr.close()
            code = process.wait()
            if code != 0:
                raise ReplayRendererError("ffmpeg_failed", stderr.strip() or "FFmpeg encoding failed.")
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            output_path.unlink(missing_ok=True)
            raise
    return warning
