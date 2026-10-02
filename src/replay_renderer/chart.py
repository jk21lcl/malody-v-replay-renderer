from __future__ import annotations

import hashlib
import io
import json
import math
import mimetypes
import zipfile
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import PurePosixPath

from .errors import ReplayRendererError
from .models import ChartData, ChartNote


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def beat_value(beat: object) -> float:
    if not isinstance(beat, list) or len(beat) != 3:
        raise ReplayRendererError("invalid_chart", "A beat must contain [whole, numerator, denominator].")
    whole, numerator, denominator = beat
    if not all(isinstance(value, (int, float)) for value in beat) or denominator == 0:
        raise ReplayRendererError("invalid_chart", "A beat contains invalid numeric values.")
    return float(whole) + float(numerator) / float(denominator)


@dataclass(frozen=True)
class _TimingPoint:
    beat: float
    time_ms: float
    bpm: float


class BeatTimeline:
    def __init__(self, raw_points: object):
        if not isinstance(raw_points, list) or not raw_points:
            raise ReplayRendererError("invalid_chart", "The chart has no BPM timing points.")

        source: list[tuple[float, float]] = []
        for item in raw_points:
            if not isinstance(item, dict):
                raise ReplayRendererError("invalid_chart", "A BPM timing point is not an object.")
            beat = beat_value(item.get("beat"))
            bpm = item.get("bpm")
            if not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or bpm <= 0:
                raise ReplayRendererError("invalid_chart", "A BPM timing point has an invalid BPM.")
            source.append((beat, float(bpm)))
        source.sort(key=lambda item: item[0])
        if len({beat for beat, _ in source}) != len(source):
            raise ReplayRendererError("invalid_chart", "Multiple BPM values share the same beat.")

        points: list[_TimingPoint] = []
        first_beat, first_bpm = source[0]
        first_time = first_beat * 60_000.0 / first_bpm
        points.append(_TimingPoint(first_beat, first_time, first_bpm))
        for beat, bpm in source[1:]:
            previous = points[-1]
            time_ms = previous.time_ms + (beat - previous.beat) * 60_000.0 / previous.bpm
            points.append(_TimingPoint(beat, time_ms, bpm))
        self._points = points
        self._beats = [point.beat for point in points]

    def milliseconds(self, beat: object) -> float:
        value = beat_value(beat)
        index = bisect_right(self._beats, value) - 1
        if index < 0:
            point = self._points[0]
        else:
            point = self._points[index]
        return point.time_ms + (value - point.beat) * 60_000.0 / point.bpm


def _audio_mime(name: str) -> str:
    extension = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    known = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}
    return known.get(extension) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _find_archive_file(
    archive: zipfile.ZipFile, chart_name: str, resource_name: str
) -> zipfile.ZipInfo | None:
    normalized_resource = resource_name.replace("\\", "/")
    chart_directory = PurePosixPath(chart_name.replace("\\", "/")).parent
    relative_resource = str(chart_directory / PurePosixPath(normalized_resource))
    archive_files = {
        entry.filename.replace("\\", "/").casefold(): entry
        for entry in archive.infolist()
        if not entry.is_dir()
    }
    return archive_files.get(relative_resource.casefold()) or archive_files.get(
        normalized_resource.casefold()
    )


def _load_audio(
    archive: zipfile.ZipFile, chart_name: str, audio_name: str | None, warnings: list[str]
) -> tuple[bytes | None, str | None]:
    if not audio_name:
        return None, None
    matching_audio = _find_archive_file(archive, chart_name, audio_name)
    if matching_audio is None:
        warnings.append(f"The chart references missing audio: {audio_name}")
        return None, None
    if matching_audio.file_size > MAX_ARCHIVE_BYTES:
        warnings.append("The referenced audio exceeds the 256 MiB limit and was not loaded.")
        return None, None
    return archive.read(matching_audio), _audio_mime(audio_name)


def _parse_osu_sections(text: str) -> dict[str, list[str]]:
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("osu file format v"):
        raise ReplayRendererError("invalid_osu_chart", "The matching .osu has no format header.")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    required = {"General", "Metadata", "Difficulty", "TimingPoints", "HitObjects"}
    missing = sorted(required - sections.keys())
    if missing:
        raise ReplayRendererError(
            "invalid_osu_chart", f"The matching .osu is missing sections: {', '.join(missing)}."
        )
    return sections


def _osu_properties(lines: list[str], section: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            raise ReplayRendererError(
                "invalid_osu_chart", f"Invalid property in [{section}]: {line}"
            )
        key, value = line.split(":", 1)
        properties[key.strip()] = value.strip()
    return properties


def _parse_osu_chart(
    archive: zipfile.ZipFile,
    selected: zipfile.ZipInfo,
    raw: bytes,
    expected_md5: str,
) -> ChartData:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReplayRendererError(
            "invalid_osu_chart", "The matching .osu is not valid UTF-8."
        ) from exc
    sections = _parse_osu_sections(text)
    general = _osu_properties(sections["General"], "General")
    metadata = _osu_properties(sections["Metadata"], "Metadata")
    difficulty = _osu_properties(sections["Difficulty"], "Difficulty")

    if general.get("Mode", "0") != "3":
        raise ReplayRendererError(
            "unsupported_chart_mode", "Only osu!mania Mode: 3 charts are supported."
        )
    try:
        raw_key_count = float(difficulty["CircleSize"])
    except (KeyError, ValueError) as exc:
        raise ReplayRendererError(
            "invalid_osu_chart", "The osu!mania chart has no valid CircleSize."
        ) from exc
    if not math.isfinite(raw_key_count) or not raw_key_count.is_integer() or raw_key_count <= 0:
        raise ReplayRendererError(
            "invalid_osu_chart", "The osu!mania CircleSize must be a positive integer."
        )
    key_count = int(raw_key_count)

    notes: list[ChartNote] = []
    for line in sections["HitObjects"]:
        parts = line.split(",")
        if len(parts) < 5:
            raise ReplayRendererError("invalid_osu_hit_object", f"Invalid HitObject: {line}")
        try:
            x = int(parts[0])
            time_ms = int(parts[2])
            object_type = int(parts[3])
        except ValueError as exc:
            raise ReplayRendererError(
                "invalid_osu_hit_object", f"Invalid numeric HitObject field: {line}"
            ) from exc
        is_hold = bool(object_type & 128)
        is_tap = bool(object_type & 1)
        if object_type & (2 | 8) or (not is_hold and not is_tap):
            raise ReplayRendererError(
                "unsupported_osu_hit_object", "The osu!mania chart contains a non-note HitObject."
            )
        end_time_ms = None
        if is_hold:
            if len(parts) < 6:
                raise ReplayRendererError("invalid_osu_hit_object", f"Invalid hold note: {line}")
            try:
                end_time = int(parts[5].split(":", 1)[0])
            except ValueError as exc:
                raise ReplayRendererError(
                    "invalid_osu_hit_object", f"Invalid hold end time: {line}"
                ) from exc
            if end_time < time_ms:
                raise ReplayRendererError(
                    "invalid_osu_hit_object", "An osu!mania hold ends before it starts."
                )
            end_time_ms = float(end_time)
        lane = min(key_count - 1, max(0, math.floor(x * key_count / 512)))
        notes.append(ChartNote(len(notes), lane, float(time_ms), end_time_ms))

    if not notes:
        raise ReplayRendererError("invalid_osu_chart", "The matching .osu has no HitObjects.")
    notes.sort(key=lambda note: (note.time_ms, note.lane, note.id))
    for index, note in enumerate(notes):
        note.id = index

    warnings: list[str] = []
    for timing_point in sections["TimingPoints"]:
        values = timing_point.split(",")
        if len(values) < 8:
            raise ReplayRendererError(
                "invalid_osu_timing_point", f"Invalid TimingPoint: {timing_point}"
            )
        try:
            timing_time = float(values[0])
            beat_length = float(values[1])
            uninherited = int(values[6])
        except ValueError as exc:
            raise ReplayRendererError(
                "invalid_osu_timing_point", f"Invalid TimingPoint: {timing_point}"
            ) from exc
        if (
            not math.isfinite(timing_time)
            or not math.isfinite(beat_length)
            or uninherited not in (0, 1)
        ):
            raise ReplayRendererError(
                "invalid_osu_timing_point", f"Invalid TimingPoint: {timing_point}"
            )
        if uninherited == 0:
            warnings.append(
                "The osu!mania chart contains inherited timing points/SV; the renderer uses a constant time-based scroll speed."
            )
            break

    audio_name = general.get("AudioFilename") or None
    try:
        audio_offset_ms = float(int(general.get("AudioLeadIn", "0")))
    except ValueError as exc:
        raise ReplayRendererError(
            "invalid_osu_chart", "The osu!mania AudioLeadIn must be an integer."
        ) from exc
    audio_bytes, audio_mime = _load_audio(
        archive, selected.filename, audio_name, warnings
    )
    return ChartData(
        source_format="osu-mania",
        title=metadata.get("TitleUnicode") or metadata.get("Title") or "Untitled",
        artist=metadata.get("ArtistUnicode") or metadata.get("Artist") or "Unknown artist",
        creator=metadata.get("Creator") or "Unknown creator",
        version=metadata.get("Version") or PurePosixPath(selected.filename).stem,
        key_count=key_count,
        chart_md5=expected_md5.lower(),
        notes=notes,
        audio_name=audio_name,
        audio_bytes=audio_bytes,
        audio_mime=audio_mime,
        audio_offset_ms=audio_offset_ms,
        warnings=warnings,
    )


def parse_chart_package(data: bytes, expected_md5: str) -> ChartData:
    if not data:
        raise ReplayRendererError("empty_chart", "The selected chart package is empty.")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ReplayRendererError("chart_too_large", "The chart package exceeds the 256 MiB limit.")

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ReplayRendererError(
            "invalid_chart_package", "The selected .mcz/.osz is not a valid ZIP archive."
        ) from exc

    with archive:
        candidates = [
            entry
            for entry in archive.infolist()
            if not entry.is_dir() and entry.filename.lower().endswith((".mc", ".osu"))
        ]
        if not candidates:
            raise ReplayRendererError(
                "missing_chart", "The selected package contains no .mc or .osu chart."
            )

        selected = None
        selected_bytes = b""
        for entry in candidates:
            if entry.file_size > MAX_ARCHIVE_BYTES:
                continue
            raw = archive.read(entry)
            if hashlib.md5(raw).hexdigest().lower() == expected_md5.lower():
                selected = entry
                selected_bytes = raw
                break
        if selected is None:
            raise ReplayRendererError(
                "chart_hash_mismatch",
                "No .mc or .osu chart in the package matches the MD5 stored in the replay.",
            )

        if selected.filename.lower().endswith(".osu"):
            return _parse_osu_chart(archive, selected, selected_bytes, expected_md5)

        try:
            chart = json.loads(selected_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayRendererError("invalid_chart", "The matching .mc file is not valid UTF-8 JSON.") from exc

        meta = chart.get("meta")
        if not isinstance(meta, dict) or meta.get("mode") != 0:
            raise ReplayRendererError("unsupported_chart_mode", "Only Malody Key mode charts are supported.")
        mode_ext = meta.get("mode_ext", {})
        key_count = mode_ext.get("column")
        if not isinstance(key_count, int) or key_count <= 0:
            raise ReplayRendererError("invalid_chart", "The chart has no valid Key column count.")

        timeline = BeatTimeline(chart.get("time"))
        raw_notes = chart.get("note")
        if not isinstance(raw_notes, list):
            raise ReplayRendererError("invalid_chart", "The chart note list is missing.")

        notes: list[ChartNote] = []
        audio_name = None
        audio_offset_ms = 0.0
        for raw_note in raw_notes:
            if not isinstance(raw_note, dict):
                continue
            if raw_note.get("type") == 1 and isinstance(raw_note.get("sound"), str):
                if audio_name is None:
                    audio_name = raw_note["sound"]
                    offset = raw_note.get("offset", 0)
                    if isinstance(offset, (int, float)) and math.isfinite(offset):
                        audio_offset_ms = float(offset)
                continue
            if "column" not in raw_note or "beat" not in raw_note:
                continue
            lane = raw_note["column"]
            if not isinstance(lane, int) or not 0 <= lane < key_count:
                raise ReplayRendererError("invalid_chart", "A note uses a lane outside the chart column range.")
            time_ms = timeline.milliseconds(raw_note["beat"])
            end_time_ms = None
            if "endbeat" in raw_note:
                end_time_ms = timeline.milliseconds(raw_note["endbeat"])
                if end_time_ms < time_ms:
                    raise ReplayRendererError(
                        "invalid_chart", "A Malody hold ends before it starts."
                    )
            notes.append(ChartNote(len(notes), lane, time_ms, end_time_ms))

        if not notes:
            raise ReplayRendererError("invalid_chart", "The matching .mc chart has no playable notes.")
        notes.sort(key=lambda note: (note.time_ms, note.lane, note.id))
        for index, note in enumerate(notes):
            note.id = index

        warnings: list[str] = []
        if chart.get("effect"):
            warnings.append("The chart contains effects/SV; the renderer uses a constant time-based scroll speed.")

        audio_bytes, audio_mime = _load_audio(
            archive, selected.filename, audio_name, warnings
        )

        song = meta.get("song") if isinstance(meta.get("song"), dict) else {}
        return ChartData(
            source_format="malody",
            title=str(song.get("title") or song.get("titleorg") or "Untitled"),
            artist=str(song.get("artist") or song.get("artistorg") or "Unknown artist"),
            creator=str(meta.get("creator") or "Unknown creator"),
            version=str(meta.get("version") or selected.filename),
            key_count=key_count,
            chart_md5=expected_md5.lower(),
            notes=notes,
            audio_name=audio_name,
            audio_bytes=audio_bytes,
            audio_mime=audio_mime,
            audio_offset_ms=audio_offset_ms,
            warnings=warnings,
        )


def parse_mcz(data: bytes, expected_md5: str) -> ChartData:
    """Backward-compatible name for the unified .mcz/.osz package parser."""
    return parse_chart_package(data, expected_md5)
