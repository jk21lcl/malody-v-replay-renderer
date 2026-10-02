from __future__ import annotations

from collections import Counter, defaultdict, deque

from .errors import ReplayRendererError
from .models import InputEvent, JudgementEvent, ReplayData, ReplaySummary
from .protobuf import (
    decode_text,
    first_bytes,
    first_varint,
    float32,
    parse_fields,
    signed_int64,
)


MAX_REPLAY_BYTES = 128 * 1024 * 1024
JUDGEMENT_WEIGHTS = {"perfect": 100.0, "cool": 75.0, "good": 40.0, "miss": 0.0}
ACCURACY_TOLERANCE = 0.001
PASSIVE_MISS_EVENT_KIND = 0
INPUT_SETTLEMENT_EVENT_KIND = 1
RELEASE_EVENT_KIND = 2
HOLD_EVENT_KIND = 4
NORMAL_RELEASE_FLAGS = 0x800
COMBO_BREAK_RELEASE_FLAGS = 0x40000
HOLD_REGRAB_FLAGS = 0x1000000
FLIP_MOD_FLAG = 0x4
CONST_MOD_FLAG = 0x8
SUPPORTED_MOD_FLAGS = FLIP_MOD_FLAG | CONST_MOD_FLAG


def _summary(raw: bytes) -> tuple[ReplaySummary, int]:
    fields = parse_fields(raw)
    accuracy_field = next(
        (field for field in fields if field.number == 11 and field.wire_type == 5), None
    )
    if accuracy_field is None:
        raise ReplayRendererError("missing_replay_summary", "Replay summary has no final accuracy.")
    return (
        ReplaySummary(
            perfect=first_varint(fields, 7, 0),
            cool=first_varint(fields, 8, 0),
            good=first_varint(fields, 9, 0),
            miss=first_varint(fields, 10, 0),
            accuracy=float32(bytes(accuracy_field.value)),
        ),
        first_varint(fields, 2, 0),
    )


def _recover_judgements(events: list[JudgementEvent], summary: ReplaySummary) -> None:
    points = 0.0
    for index, event in enumerate(events, start=1):
        choices = {
            name: abs((points + weight) / index - event.running_accuracy)
            for name, weight in JUDGEMENT_WEIGHTS.items()
        }
        judgement, error = min(choices.items(), key=lambda item: item[1])
        if error > ACCURACY_TOLERANCE:
            raise ReplayRendererError(
                "unsupported_accuracy_sequence",
                f"Cannot recover judgement {index}: cumulative accuracy differs by {error:.6f}.",
            )
        event.judgement = judgement
        points += JUDGEMENT_WEIGHTS[judgement]

    actual = Counter(event.judgement for event in events)
    expected = {
        "perfect": summary.perfect,
        "cool": summary.cool,
        "good": summary.good,
        "miss": summary.miss,
    }
    if any(actual[name] != count for name, count in expected.items()):
        raise ReplayRendererError(
            "replay_summary_mismatch",
            f"Recovered judgement counts {dict(actual)} do not match replay summary {expected}.",
        )
    if events and abs(points / len(events) - summary.accuracy) > ACCURACY_TOLERANCE:
        raise ReplayRendererError(
            "replay_accuracy_mismatch", "Recovered judgements do not match final replay accuracy."
        )


def parse_mrv(data: bytes) -> ReplayData:
    if not data:
        raise ReplayRendererError("empty_replay", "The selected replay is empty.")
    if len(data) > MAX_REPLAY_BYTES:
        raise ReplayRendererError("replay_too_large", "The replay exceeds the 128 MiB limit.")

    fields = parse_fields(data)
    chart_md5 = decode_text(fields, 5).lower()
    if len(chart_md5) != 32 or any(character not in "0123456789abcdef" for character in chart_md5):
        raise ReplayRendererError("invalid_chart_hash", "Replay field 5 is not a valid MD5 hash.")
    raw_summary = first_bytes(fields, 8)
    summary, mod_flags = _summary(raw_summary)
    unknown_mod_flags = mod_flags & ~SUPPORTED_MOD_FLAGS
    if unknown_mod_flags:
        raise ReplayRendererError(
            "unsupported_replay_mods",
            f"The replay uses unsupported mod flags: 0x{unknown_mod_flags:X}.",
        )

    inputs: list[InputEvent] = []
    judgements: list[JudgementEvent] = []
    frame_times: list[int] = []
    active_downs: dict[int, deque[InputEvent]] = defaultdict(deque)
    orphan_release_count = 0
    unknown_release_flags: set[int] = set()
    for frame_field in fields:
        if frame_field.number != 9 or frame_field.wire_type != 2:
            continue
        frame = parse_fields(bytes(frame_field.value))
        timestamp = signed_int64(first_varint(frame, 1))
        frame_times.append(timestamp)
        wrappers = [
            parse_fields(bytes(field.value))
            for field in frame
            if field.number == 3 and field.wire_type == 2
        ]

        available_downs: dict[int, deque[InputEvent]] = defaultdict(deque)
        released_inputs: dict[int, list[InputEvent]] = defaultdict(list)
        for wrapper in wrappers:
            for field in wrapper:
                if field.number == 1 and field.wire_type == 0:
                    event = InputEvent(len(inputs), int(field.value), timestamp)
                    inputs.append(event)
                    available_downs[event.lane].append(event)
                    active_downs[event.lane].append(event)
                elif field.number == 2 and field.wire_type == 0:
                    lane = int(field.value)
                    if not active_downs[lane]:
                        orphan_release_count += 1
                        continue
                    event = active_downs[lane].popleft()
                    event.release_time_ms = timestamp
                    released_inputs[lane].append(event)

        for wrapper_index, wrapper in enumerate(wrappers):
            for field in wrapper:
                if field.number != 31 or field.wire_type != 2:
                    continue
                result_fields = parse_fields(bytes(field.value))
                lane = first_varint(result_fields, 1, 0)
                event_kind = first_varint(result_fields, 2, 0)
                event_flags = first_varint(result_fields, 3, 0)
                if event_kind == RELEASE_EVENT_KIND:
                    if event_flags not in (NORMAL_RELEASE_FLAGS, COMBO_BREAK_RELEASE_FLAGS):
                        unknown_release_flags.add(event_flags)
                    if released_inputs[lane] and event_flags == COMBO_BREAK_RELEASE_FLAGS:
                        released_inputs[lane][-1].release_breaks_combo = True
                    continue
                if event_kind == HOLD_EVENT_KIND:
                    continue
                if event_kind not in (PASSIVE_MISS_EVENT_KIND, INPUT_SETTLEMENT_EVENT_KIND):
                    continue

                accuracy = None
                for following in wrappers[wrapper_index:]:
                    candidate = next(
                        (item for item in following if item.number == 43 and item.wire_type == 5),
                        None,
                    )
                    if candidate is not None:
                        accuracy = float32(bytes(candidate.value))
                        break
                    if following is not wrapper and any(
                        item.number == 31 and item.wire_type == 2 for item in following
                    ):
                        break
                if accuracy is None:
                    raise ReplayRendererError(
                        "unsupported_replay_structure",
                        "A note settlement event has no cumulative accuracy field.",
                    )

                # In the validated Malody V samples, kind 1 settlements are
                # triggered by a press while kind 0 settlements are passive
                # misses.  A passive miss can share a frame with the press for
                # the following note, so it must not consume that input.
                if event_kind == INPUT_SETTLEMENT_EVENT_KIND:
                    if not available_downs[lane]:
                        raise ReplayRendererError(
                            "unsupported_replay_structure",
                            "An input-triggered note settlement has no same-frame key-down event.",
                        )
                    matched = available_downs[lane].popleft()
                else:
                    matched = None

                if event_kind == INPUT_SETTLEMENT_EVENT_KIND and event_flags == HOLD_REGRAB_FLAGS:
                    if not judgements or abs(accuracy - judgements[-1].running_accuracy) > ACCURACY_TOLERANCE:
                        raise ReplayRendererError(
                            "unsupported_replay_structure",
                            "A hold re-grab event unexpectedly changes cumulative accuracy.",
                        )
                    matched.is_hold_regrab = True
                    continue
                judgement = JudgementEvent(
                    id=len(judgements),
                    lane=lane,
                    time_ms=timestamp,
                    event_kind=event_kind,
                    event_flags=event_flags,
                    running_accuracy=accuracy,
                    input_id=matched.id if matched else None,
                )
                judgements.append(judgement)
                if matched:
                    matched.matched_judgement_id = judgement.id

    if not frame_times:
        raise ReplayRendererError("missing_replay_frames", "Replay contains no frame records.")
    if not judgements:
        raise ReplayRendererError(
            "unsupported_replay_structure", "Replay contains no supported note settlement events."
        )
    _recover_judgements(judgements, summary)
    if any(
        event.event_kind == PASSIVE_MISS_EVENT_KIND and event.judgement != "miss"
        for event in judgements
    ):
        raise ReplayRendererError(
            "unsupported_replay_structure",
            "A passive settlement resolved to a non-Miss judgement.",
        )

    warnings: list[str] = []
    if orphan_release_count:
        warnings.append(
            f"The replay contains {orphan_release_count} key-up event(s) without a matching key-down."
        )
    unclosed_input_count = sum(len(events) for events in active_downs.values())
    if unclosed_input_count:
        warnings.append(
            f"The replay contains {unclosed_input_count} key-down event(s) without a matching key-up."
        )
    if unknown_release_flags:
        values = ", ".join(f"0x{value:X}" for value in sorted(unknown_release_flags))
        warnings.append(
            f"The replay contains unrecognized hold release flags ({values}); those releases are shown as normal."
        )

    return ReplayData(
        title=decode_text(fields, 1),
        artist=decode_text(fields, 2),
        creator=decode_text(fields, 3),
        version=decode_text(fields, 4, required=False),
        chart_md5=chart_md5,
        player=decode_text(fields, 6),
        format_marker=first_varint(fields, 7),
        mod_flags=mod_flags,
        inputs=inputs,
        judgements=judgements,
        summary=summary,
        start_ms=min(frame_times),
        end_ms=max(frame_times),
        warnings=warnings,
    )
