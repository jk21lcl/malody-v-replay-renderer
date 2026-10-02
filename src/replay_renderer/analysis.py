from __future__ import annotations

from collections import defaultdict

from .chart import parse_chart_package
from .errors import ReplayRendererError
from .replay import CONST_MOD_FLAG, FLIP_MOD_FLAG, parse_mrv


ANALYSIS_SCHEMA_VERSION = 52
JUDGEMENT_WEIGHTS = {"perfect": 100.0, "cool": 75.0, "good": 40.0, "miss": 0.0}
DENSITY_WINDOW_MS = 1000.0
DENSITY_SAMPLE_INTERVAL_MS = 100.0


def build_density(
    note_times: list[float],
    start_ms: float,
    end_ms: float,
    window_ms: float = DENSITY_WINDOW_MS,
    sample_interval_ms: float = DENSITY_SAMPLE_INTERVAL_MS,
) -> dict:
    """Count note heads in centered, half-open rolling windows."""
    sorted_times = sorted(note_times)
    sample_times = []
    index = 0
    while start_ms + index * sample_interval_ms < end_ms:
        sample_times.append(start_ms + index * sample_interval_ms)
        index += 1
    if not sample_times or sample_times[-1] != end_ms:
        sample_times.append(end_ms)

    half_window = window_ms / 2
    left = right = 0
    samples = []
    max_count = 0
    for sample_time in sample_times:
        window_start = sample_time - half_window
        window_end = sample_time + half_window
        while left < len(sorted_times) and sorted_times[left] < window_start:
            left += 1
        if right < left:
            right = left
        while right < len(sorted_times) and sorted_times[right] < window_end:
            right += 1
        count = right - left
        max_count = max(max_count, count)
        samples.append({"timeMs": round(sample_time, 6), "count": count})
    return {
        "windowMs": window_ms,
        "sampleIntervalMs": sample_interval_ms,
        "maxCount": max_count,
        "samples": samples,
    }


def analyze_files(chart_bytes: bytes, replay_bytes: bytes) -> tuple[dict, bytes | None, str | None]:
    replay = parse_mrv(replay_bytes)
    chart = parse_chart_package(chart_bytes, replay.chart_md5)
    flip_enabled = bool(replay.mod_flags & FLIP_MOD_FLAG)

    def display_lane(lane: int) -> int:
        return chart.key_count - 1 - lane if flip_enabled else lane

    notes_by_lane: dict[int, list] = defaultdict(list)
    results_by_lane: dict[int, list] = defaultdict(list)
    for note in chart.notes:
        notes_by_lane[display_lane(note.lane)].append(note)
    for result in replay.judgements:
        if not 0 <= result.lane < chart.key_count:
            raise ReplayRendererError(
                "unsupported_lane_mapping", "Replay uses a lane outside the chart's Key layout."
            )
        results_by_lane[result.lane].append(result)

    for lane in range(chart.key_count):
        if len(notes_by_lane[lane]) != len(results_by_lane[lane]):
            raise ReplayRendererError(
                "note_result_count_mismatch",
                f"Lane {lane + 1} has {len(notes_by_lane[lane])} notes but "
                f"{len(results_by_lane[lane])} settlement events. The replay may use unsupported mods.",
            )

    input_by_id = {event.id: event for event in replay.inputs}
    result_to_note: dict[int, int] = {}
    note_by_id = {note.id: note for note in chart.notes}
    note_rows = []
    for lane in range(chart.key_count):
        for note, result in zip(notes_by_lane[lane], results_by_lane[lane]):
            result_to_note[result.id] = note.id
            key_input = input_by_id.get(result.input_id)
            release_time = (
                key_input.release_time_ms
                if note.is_hold_head and key_input is not None
                else None
            )
            note_rows.append(
                {
                    "id": note.id,
                    "lane": display_lane(note.lane),
                    "timeMs": round(note.time_ms, 6),
                    "isHoldHead": note.is_hold_head,
                    "endTimeMs": round(note.end_time_ms, 6) if note.end_time_ms is not None else None,
                    "judgement": result.judgement,
                    "judgementTimeMs": result.time_ms,
                    "inputId": key_input.id if key_input else None,
                    "inputTimeMs": key_input.time_ms if key_input else None,
                    "offsetMs": round(key_input.time_ms - note.time_ms, 6) if key_input else None,
                    "releaseTimeMs": release_time,
                    "releaseBreaksCombo": bool(
                        release_time is not None and key_input.release_breaks_combo
                    ),
                }
            )
    note_rows.sort(key=lambda row: row["id"])

    hold_rows_by_lane: dict[int, list[dict]] = defaultdict(list)
    for row in note_rows:
        if row["isHoldHead"]:
            row["regrabs"] = []
            hold_rows_by_lane[row["lane"]].append(row)

    regrab_note_by_input: dict[int, int] = {}

    def is_broken_at(row: dict, time_ms: int) -> bool:
        # A hold head can already be broken because it was not pressed at all,
        # or because an early press consumed its Accuracy judgement as Miss.
        # The latter may be released before the head and therefore has no
        # 0x40000 Combo Break release event.
        broken = row["inputId"] is None or row["judgement"] == "miss"
        if (
            row["releaseBreaksCombo"]
            and row["releaseTimeMs"] is not None
            and row["releaseTimeMs"] <= time_ms
        ):
            broken = True
        for regrab in row["regrabs"]:
            if regrab["timeMs"] > time_ms:
                break
            broken = False
            if (
                regrab["releaseBreaksCombo"]
                and regrab["releaseTimeMs"] is not None
                and regrab["releaseTimeMs"] <= time_ms
            ):
                broken = True
        return broken

    for event in replay.inputs:
        if not event.is_hold_regrab:
            continue
        candidates = [
            row
            for row in hold_rows_by_lane[event.lane]
            if min(
                row["timeMs"],
                row["inputTimeMs"] if row["inputTimeMs"] is not None else row["timeMs"],
            )
            <= event.time_ms
            <= row["endTimeMs"]
            and is_broken_at(row, event.time_ms)
        ]
        if not candidates:
            raise ReplayRendererError(
                "unmatched_hold_regrab",
                f"Hold re-grab input {event.id} at {event.time_ms} ms does not match a broken hold.",
            )
        hold = max(candidates, key=lambda row: row["timeMs"])
        hold["regrabs"].append(
            {
                "inputId": event.id,
                "timeMs": event.time_ms,
                "releaseTimeMs": event.release_time_ms,
                "releaseBreaksCombo": bool(
                    event.release_time_ms is not None and event.release_breaks_combo
                ),
            }
        )
        regrab_note_by_input[event.id] = hold["id"]

    input_rows = []
    for event in replay.inputs:
        result = replay.judgements[event.matched_judgement_id] if event.matched_judgement_id is not None else None
        matched_note_id = (
            regrab_note_by_input.get(event.id)
            if event.is_hold_regrab
            else result_to_note.get(result.id) if result else None
        )
        matched_note = note_by_id.get(matched_note_id)
        is_hold_input = matched_note is not None and matched_note.is_hold_head
        input_rows.append(
            {
                "id": event.id,
                "lane": event.lane,
                "timeMs": event.time_ms,
                "judgement": result.judgement if result else "miss",
                "matchedNoteId": matched_note_id,
                "isExtra": result is None and not event.is_hold_regrab,
                "isHoldRegrab": event.is_hold_regrab,
                "releaseTimeMs": event.release_time_ms if is_hold_input else None,
                "releaseBreaksCombo": bool(
                    is_hold_input
                    and event.release_time_ms is not None
                    and event.release_breaks_combo
                ),
            }
        )
    input_rows.sort(key=lambda row: (row["timeMs"], row["id"]))

    summary_by_lane = {
        key: [0] * chart.key_count
        for key in ("totalNotes", "perfect", "cool", "good", "miss", "extraInputs", "holdRegrabs")
    }
    for row in note_rows:
        summary_by_lane["totalNotes"][row["lane"]] += 1
        summary_by_lane[row["judgement"]][row["lane"]] += 1
    for row in input_rows:
        if row["isExtra"]:
            summary_by_lane["extraInputs"][row["lane"]] += 1
        if row["isHoldRegrab"]:
            summary_by_lane["holdRegrabs"][row["lane"]] += 1

    summary = {key: sum(lane_counts) for key, lane_counts in summary_by_lane.items()}
    accuracy_by_lane = []
    for lane, note_count in enumerate(summary_by_lane["totalNotes"]):
        weighted_points = sum(
            summary_by_lane[judgement][lane] * weight
            for judgement, weight in JUDGEMENT_WEIGHTS.items()
        )
        accuracy_by_lane.append(round(weighted_points / note_count, 6) if note_count else 0.0)
    summary["accuracy"] = round(
        sum(
            summary[judgement] * weight
            for judgement, weight in JUDGEMENT_WEIGHTS.items()
        )
        / summary["totalNotes"],
        6,
    )
    summary_by_lane["accuracy"] = accuracy_by_lane
    summary["byLane"] = summary_by_lane

    first_note = min(note.time_ms for note in chart.notes)
    last_note = max(
        note.end_time_ms if note.end_time_ms is not None else note.time_ms
        for note in chart.notes
    )
    hold_releases = [
        row["releaseTimeMs"] for row in note_rows if row["releaseTimeMs"] is not None
    ]
    last_visible_event = max([last_note, *hold_releases])
    max_hold_duration = max(
        (
            note.end_time_ms - note.time_ms
            for note in chart.notes
            if note.end_time_ms is not None
        ),
        default=0.0,
    )
    max_input_duration = max(
        (
            row["releaseTimeMs"] - row["timeMs"]
            for row in input_rows
            if row["releaseTimeMs"] is not None
        ),
        default=0.0,
    )
    timeline_start = min(replay.start_ms, first_note - 1000.0)
    timeline_end = max(replay.end_ms, last_visible_event + 1000.0)
    payload = {
        "schemaVersion": ANALYSIS_SCHEMA_VERSION,
        "meta": {
            "title": chart.title,
            "artist": chart.artist,
            "creator": chart.creator,
            "version": chart.version,
            "player": replay.player,
            "keyCount": chart.key_count,
            "chartMd5": chart.chart_md5,
            "replayFormatMarker": replay.format_marker,
            "chartFormat": chart.source_format,
            "activeMods": [
                name
                for flag, name in ((CONST_MOD_FLAG, "CONST"), (FLIP_MOD_FLAG, "FLIP"))
                if replay.mod_flags & flag
            ],
        },
        "timeline": {
            "startMs": timeline_start,
            "endMs": timeline_end,
            "audioOffsetMs": chart.audio_offset_ms,
            "audioAvailable": chart.audio_bytes is not None,
            "audioName": chart.audio_name,
            "maxHoldDurationMs": round(max_hold_duration, 6),
            "maxInputDurationMs": round(max_input_duration, 6),
        },
        "notes": note_rows,
        "inputs": input_rows,
        "density": build_density(
            [note.time_ms for note in chart.notes], timeline_start, timeline_end
        ),
        "summary": summary,
        "warnings": [*chart.warnings, *replay.warnings],
    }
    return payload, chart.audio_bytes, chart.audio_mime
