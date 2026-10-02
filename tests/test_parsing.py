from __future__ import annotations

import hashlib
import io
import json
import struct
import unittest
import zipfile

from replay_renderer.analysis import analyze_files, build_density
from replay_renderer.chart import MAX_ARCHIVE_BYTES, parse_chart_package, parse_mcz
from replay_renderer.errors import ReplayRendererError
from replay_renderer.protobuf import parse_fields, signed_int64
from replay_renderer.replay import parse_mrv


def varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def vint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def blob(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def fixed32(number: int, value: float) -> bytes:
    return varint((number << 3) | 5) + struct.pack("<f", value)


def text(number: int, value: str) -> bytes:
    return blob(number, value.encode())


def make_chart(
    include_audio: bool = True, nested: bool = False, reverse_hold: bool = False
) -> tuple[bytes, str]:
    chart = {
        "meta": {"mode": 0, "mode_ext": {"column": 2}, "song": {"title": "Test", "artist": "Artist"}, "creator": "Maker", "version": "Hard"},
        "time": [{"beat": [0, 0, 1], "bpm": 120}, {"beat": [2, 0, 1], "bpm": 240}],
        "note": [
            {"beat": [0, 0, 1], "column": 0},
            {
                "beat": [1, 0, 1],
                "column": 1,
                "endbeat": [0, 0, 1] if reverse_hold else [2, 0, 1],
            },
            {"beat": [3, 0, 1], "column": 0},
            {"beat": [4, 0, 1], "column": 1},
        ],
    }
    if include_audio:
        chart["note"].append({"beat": [0, 0, 1], "type": 1, "sound": "song.ogg", "offset": 123})
    raw = json.dumps(chart, separators=(",", ":")).encode()
    digest = hashlib.md5(raw).hexdigest()
    package = io.BytesIO()
    prefix = "0/" if nested else ""
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(f"{prefix}chart.mc", raw)
        if include_audio:
            archive.writestr(f"{prefix}song.ogg", b"OggS-test")
    return package.getvalue(), digest


def make_osu_package(
    *, mode: str = "3", circle_size: str = "4", include_audio: bool = True,
    object_type: int = 1, hold_end: int = 450, version: str = "Test difficulty",
) -> tuple[bytes, str]:
    lines = [
        "osu file format v14", "", "// leading comment", "[General]",
        "AudioFilename: song.ogg", "AudioLeadIn: 250", f"Mode: {mode}", "",
        "[Metadata]", "Title:ASCII title", "TitleUnicode:Unicode title",
        "Artist:Artist", "Creator:Mapper", f"Version:{version}", "",
        "[Difficulty]", f"CircleSize:{circle_size}", "", "[TimingPoints]",
        "0,500,4,2,0,100,1,0", "1000,-50,4,2,0,100,0,0", "", "[HitObjects]",
        f"0,192,100,{object_type},0,0:0:0:0:",
        "128,192,200,1,0,0:0:0:0:",
        f"256,192,300,128,0,{hold_end}:0:0:0:0:",
        "512,192,400,1,0,0:0:0:0:",
    ]
    raw = ("\ufeff" + "\r\n".join(lines)).encode("utf-8")
    digest = hashlib.md5(raw).hexdigest()
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("maps/decoy.osu", b"osu file format v14\n")
        archive.writestr("maps/chart.osu", raw)
        if include_audio:
            archive.writestr("maps/song.ogg", b"OggS-osu-test")
    return package.getvalue(), digest


def wrapper(payload: bytes) -> bytes:
    return blob(3, payload)


def make_replay(
    digest: str, bad_accuracy: bool = False, mod_flags: int | None = None,
    include_version: bool = True,
) -> bytes:
    weights = [100, 75, 0, 40]
    lanes = [0, 1, 0, 1]
    times = [-25, 510, 1260, 1510]
    points = 0
    frames = []
    # An unmatched early press remains visible as a red extra input.
    frames.append(blob(9, vint(1, -100) + wrapper(vint(1, 1)) + wrapper(vint(2, 1))))
    for index, (weight, lane, timestamp) in enumerate(zip(weights, lanes, times), 1):
        parts = [vint(1, timestamp)]
        if weight:
            parts.append(wrapper(vint(1, lane)))
        result = (vint(1, lane) if lane else b"") + (vint(2, 1) if weight else b"")
        points += weight
        accuracy = points / index
        if bad_accuracy and index == 2:
            accuracy = 12.34
        parts.append(wrapper(blob(31, result)))
        parts.append(wrapper(fixed32(43, accuracy)))
        if weight:
            parts.append(wrapper(vint(2, lane)))
        frames.append(blob(9, b"".join(parts)))
    summary = b"".join([
        vint(2, mod_flags) if mod_flags is not None else b"",
        vint(7, 1), vint(8, 1), vint(9, 1), vint(10, 1), fixed32(11, points / 4),
    ])
    header = b"".join([
        text(1, "Test"), text(2, "Artist"), text(3, "Maker"),
        text(4, "Hard") if include_version else b"",
        text(5, digest), text(6, "Player"), vint(7, 1330902), blob(8, summary),
    ])
    return header + b"".join(frames)


class ProtobufTests(unittest.TestCase):
    def test_varint_negative_time_and_zero_default(self) -> None:
        fields = parse_fields(vint(1, -25) + blob(2, b"ok"))
        self.assertEqual(signed_int64(fields[0].value), -25)
        nested = parse_fields(b"")
        self.assertEqual(next((field.value for field in nested if field.number == 1), 0), 0)

    def test_truncated_varint_is_rejected(self) -> None:
        with self.assertRaises(ReplayRendererError):
            parse_fields(b"\x80")


class ChartTests(unittest.TestCase):
    def test_chart_package_limit_is_256_mib(self) -> None:
        self.assertEqual(MAX_ARCHIVE_BYTES, 256 * 1024 * 1024)

    def test_md5_multi_bpm_hold_head_and_audio(self) -> None:
        package, digest = make_chart()
        chart = parse_mcz(package, digest)
        self.assertEqual([note.time_ms for note in chart.notes], [0, 500, 1250, 1500])
        self.assertTrue(chart.notes[1].is_hold_head)
        self.assertEqual(chart.notes[1].end_time_ms, 1000)
        self.assertEqual(chart.audio_offset_ms, 123)
        self.assertEqual(chart.audio_bytes, b"OggS-test")

    def test_audio_path_is_relative_to_chart_directory(self) -> None:
        package, digest = make_chart(nested=True)
        chart = parse_mcz(package, digest)
        self.assertEqual(chart.audio_bytes, b"OggS-test")

    def test_hash_mismatch_and_damaged_package(self) -> None:
        package, _ = make_chart()
        with self.assertRaisesRegex(ReplayRendererError, "matches the MD5"):
            parse_mcz(package, "0" * 32)
        with self.assertRaisesRegex(ReplayRendererError, "valid ZIP"):
            parse_mcz(b"broken", "0" * 32)

    def test_hold_end_before_start_is_rejected(self) -> None:
        package, digest = make_chart(reverse_hold=True)
        with self.assertRaisesRegex(ReplayRendererError, "hold ends before"):
            parse_chart_package(package, digest)
        package, digest = make_osu_package(hold_end=250)
        with self.assertRaisesRegex(ReplayRendererError, "hold ends before"):
            parse_chart_package(package, digest)

    def test_osu_mania_md5_metadata_lanes_hold_audio_and_sv(self) -> None:
        package, digest = make_osu_package()
        chart = parse_chart_package(package, digest)
        self.assertEqual(chart.source_format, "osu-mania")
        self.assertEqual((chart.title, chart.artist, chart.creator, chart.version),
                         ("Unicode title", "Artist", "Mapper", "Test difficulty"))
        self.assertEqual(chart.key_count, 4)
        self.assertEqual([note.lane for note in chart.notes], [0, 1, 2, 3])
        self.assertEqual([note.time_ms for note in chart.notes], [100, 200, 300, 400])
        self.assertEqual([note.is_hold_head for note in chart.notes], [False, False, True, False])
        self.assertEqual(chart.notes[2].end_time_ms, 450)
        self.assertEqual(chart.audio_offset_ms, 250)
        self.assertEqual(chart.audio_bytes, b"OggS-osu-test")
        self.assertEqual(chart.audio_mime, "audio/ogg")
        self.assertTrue(any("inherited timing points/SV" in warning for warning in chart.warnings))

    def test_osu_mania_missing_audio_is_a_warning(self) -> None:
        package, digest = make_osu_package(include_audio=False)
        chart = parse_chart_package(package, digest)
        self.assertIsNone(chart.audio_bytes)
        self.assertTrue(any("missing audio" in warning for warning in chart.warnings))

    def test_osu_mania_six_key_lane_mapping(self) -> None:
        package, digest = make_osu_package(circle_size="6")
        chart = parse_chart_package(package, digest)
        self.assertEqual(chart.key_count, 6)
        self.assertEqual([note.lane for note in chart.notes], [0, 1, 3, 5])

    def test_empty_osu_version_uses_chart_filename(self) -> None:
        package, digest = make_osu_package(version="")

        chart = parse_chart_package(package, digest)

        self.assertEqual(chart.version, "chart")

    def test_non_mania_and_non_integer_key_count_are_rejected(self) -> None:
        package, digest = make_osu_package(mode="0")
        with self.assertRaisesRegex(ReplayRendererError, "Mode: 3"):
            parse_chart_package(package, digest)
        package, digest = make_osu_package(circle_size="4.5")
        with self.assertRaisesRegex(ReplayRendererError, "positive integer"):
            parse_chart_package(package, digest)

    def test_unsupported_osu_hit_object_is_rejected(self) -> None:
        package, digest = make_osu_package(object_type=2)
        with self.assertRaisesRegex(ReplayRendererError, "non-note HitObject"):
            parse_chart_package(package, digest)


class DensityTests(unittest.TestCase):
    def test_centered_half_open_windows_and_final_sample(self) -> None:
        density = build_density([0, 500, 500, 999, 1000, 1500], 0, 1050)

        self.assertEqual(density["windowMs"], 1000)
        self.assertEqual(density["sampleIntervalMs"], 100)
        self.assertEqual(density["maxCount"], 4)
        self.assertEqual(density["samples"][0], {"timeMs": 0, "count": 1})
        self.assertEqual(density["samples"][5], {"timeMs": 500, "count": 4})
        self.assertEqual(density["samples"][10], {"timeMs": 1000, "count": 4})
        self.assertEqual(density["samples"][-1], {"timeMs": 1050, "count": 3})
        self.assertEqual(len(density["samples"]), 12)

    def test_empty_regions_and_hold_heads_are_counted_once(self) -> None:
        # Only the hold head time enters this list; its tail is deliberately absent.
        density = build_density([1000], 0, 2000)

        by_time = {sample["timeMs"]: sample["count"] for sample in density["samples"]}
        self.assertEqual(by_time[0], 0)
        self.assertEqual(by_time[500], 0)
        self.assertEqual(by_time[600], 1)
        self.assertEqual(by_time[1400], 1)
        self.assertEqual(by_time[1500], 1)
        self.assertEqual(by_time[1600], 0)
        self.assertEqual(density["maxCount"], 1)


class ReplayTests(unittest.TestCase):
    def test_missing_empty_version_field_is_supported(self) -> None:
        package, digest = make_chart()

        result, _, _ = analyze_files(package, make_replay(digest, include_version=False))

        self.assertEqual(result["meta"]["version"], "Hard")

    def test_replay_mod_flags_and_unknown_mod_rejection(self) -> None:
        _, digest = make_chart()

        self.assertEqual(parse_mrv(make_replay(digest)).mod_flags, 0)
        self.assertEqual(parse_mrv(make_replay(digest, mod_flags=0x8)).mod_flags, 0x8)
        self.assertEqual(parse_mrv(make_replay(digest, mod_flags=0xC)).mod_flags, 0xC)
        with self.assertRaisesRegex(ReplayRendererError, "0x10"):
            parse_mrv(make_replay(digest, mod_flags=0x10))

    def test_flip_mirrors_chart_lanes_before_matching_replay(self) -> None:
        chart = {
            "meta": {
                "mode": 0,
                "mode_ext": {"column": 2},
                "song": {"title": "Flip Test", "artist": "Artist"},
                "creator": "Maker",
                "version": "Hard",
            },
            "time": [{"beat": [0, 0, 1], "bpm": 120}],
            "note": [
                {"beat": [0, 0, 1], "column": 0},
                {"beat": [1, 0, 1], "column": 0},
                {"beat": [2, 0, 1], "column": 1},
            ],
        }
        raw_chart = json.dumps(chart, separators=(",", ":")).encode()
        digest = hashlib.md5(raw_chart).hexdigest()
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("chart.mc", raw_chart)

        frames = []
        for timestamp, lane in [(-25, 1), (510, 1), (1010, 0)]:
            result = (vint(1, lane) if lane else b"") + vint(2, 1)
            frames.append(blob(9, b"".join([
                vint(1, timestamp), wrapper(vint(1, lane)),
                wrapper(blob(31, result)), wrapper(fixed32(43, 100.0)),
                wrapper(vint(2, lane)),
            ])))
        summary = vint(2, 0xC) + vint(7, 3) + fixed32(11, 100.0)
        replay = b"".join([
            text(1, "Flip Test"), text(2, "Artist"), text(3, "Maker"),
            text(4, "Hard"), text(5, digest), text(6, "Player"),
            vint(7, 1), blob(8, summary), *frames,
        ])

        result, _, _ = analyze_files(package.getvalue(), replay)

        self.assertEqual(result["meta"]["activeMods"], ["CONST", "FLIP"])
        self.assertEqual([note["lane"] for note in result["notes"]], [1, 1, 0])
        self.assertEqual([item["lane"] for item in result["inputs"]], [1, 1, 0])
        self.assertEqual([note["inputId"] for note in result["notes"]], [0, 1, 2])
        self.assertEqual(result["summary"]["totalNotes"], 3)
        self.assertEqual(result["summary"]["byLane"]["totalNotes"], [1, 2])
        self.assertEqual(result["summary"]["byLane"]["perfect"], [1, 2])

    def test_simultaneous_inputs_match_their_own_lanes(self) -> None:
        _, digest = make_chart()
        summary = vint(7, 2) + fixed32(11, 100.0)
        frame = b"".join([
            vint(1, 500), wrapper(vint(1, 0)), wrapper(vint(1, 1)),
            wrapper(blob(31, vint(2, 1))), wrapper(fixed32(43, 100.0)),
            wrapper(blob(31, vint(1, 1) + vint(2, 1))), wrapper(fixed32(43, 100.0)),
        ])
        replay = b"".join([
            text(1,"x"),text(2,"x"),text(3,"x"),text(4,"x"),text(5,digest),text(6,"x"),
            vint(7,1),blob(8,summary),blob(9,frame),
        ])
        parsed = parse_mrv(replay)
        self.assertEqual([event.input_id for event in parsed.judgements], [0, 1])
        self.assertEqual([event.lane for event in parsed.inputs], [0, 1])

    def test_passive_miss_does_not_consume_following_hit_input(self) -> None:
        _, digest = make_chart()
        summary = vint(7, 1) + vint(10, 1) + fixed32(11, 50.0)
        frame = b"".join([
            vint(1, 1000),
            wrapper(vint(1, 0)),
            wrapper(blob(31, b"")),
            wrapper(fixed32(43, 0.0)),
            wrapper(blob(31, vint(2, 1))),
            wrapper(fixed32(43, 50.0)),
        ])
        replay = b"".join([
            text(1, "x"), text(2, "x"), text(3, "x"), text(4, "x"),
            text(5, digest), text(6, "x"), vint(7, 1), blob(8, summary), blob(9, frame),
        ])

        parsed = parse_mrv(replay)

        self.assertEqual(
            [(event.event_kind, event.judgement, event.input_id) for event in parsed.judgements],
            [(0, "miss", None), (1, "perfect", 0)],
        )
        self.assertEqual(parsed.inputs[0].matched_judgement_id, 1)

    def test_input_settlement_without_key_down_is_rejected(self) -> None:
        _, digest = make_chart()
        summary = vint(7, 1) + fixed32(11, 100.0)
        frame = b"".join([
            vint(1, 1000),
            wrapper(blob(31, vint(2, 1))),
            wrapper(fixed32(43, 100.0)),
        ])
        replay = b"".join([
            text(1, "x"), text(2, "x"), text(3, "x"), text(4, "x"),
            text(5, digest), text(6, "x"), vint(7, 1), blob(8, summary), blob(9, frame),
        ])

        with self.assertRaisesRegex(ReplayRendererError, "same-frame key-down"):
            parse_mrv(replay)

    def test_hold_releases_and_combo_break_are_recorded(self) -> None:
        _, digest = make_chart()
        summary = vint(7, 2) + fixed32(11, 100.0)
        hit_frame = b"".join([
            vint(1, 100),
            wrapper(vint(1, 0)),
            wrapper(blob(31, vint(2, 1) + vint(3, 34))),
            wrapper(fixed32(43, 100.0)),
            wrapper(vint(1, 1)),
            wrapper(blob(31, vint(1, 1) + vint(2, 1) + vint(3, 34))),
            wrapper(fixed32(43, 100.0)),
        ])
        release_frame = b"".join([
            vint(1, 200),
            wrapper(vint(2, 0)),
            wrapper(blob(31, vint(2, 2) + vint(3, 0x800))),
            wrapper(blob(31, vint(2, 4) + vint(3, 0x20000080))),
            wrapper(vint(2, 1)),
            wrapper(blob(31, vint(1, 1) + vint(2, 2) + vint(3, 0x40000))),
        ])
        replay = b"".join([
            text(1, "x"), text(2, "x"), text(3, "x"), text(4, "x"),
            text(5, digest), text(6, "x"), vint(7, 1), blob(8, summary),
            blob(9, hit_frame), blob(9, release_frame),
        ])

        parsed = parse_mrv(replay)

        self.assertEqual(len(parsed.judgements), 2)
        self.assertEqual(
            [(event.release_time_ms, event.release_breaks_combo) for event in parsed.inputs],
            [(200, False), (200, True)],
        )

    def test_hold_regrab_does_not_create_a_note_judgement(self) -> None:
        _, digest = make_chart()
        summary = vint(7, 1) + fixed32(11, 100.0)
        frames = [
            blob(9, b"".join([
                vint(1, 100), wrapper(vint(1, 0)),
                wrapper(blob(31, vint(2, 1) + vint(3, 34))),
                wrapper(fixed32(43, 100.0)),
            ])),
            blob(9, b"".join([
                vint(1, 150), wrapper(vint(2, 0)),
                wrapper(blob(31, vint(2, 2) + vint(3, 0x40000))),
            ])),
            blob(9, b"".join([
                vint(1, 175), wrapper(vint(1, 0)),
                wrapper(blob(31, vint(2, 1) + vint(3, 0x1000000))),
                wrapper(fixed32(43, 100.0)),
            ])),
            blob(9, vint(1, 225) + wrapper(vint(2, 0))),
        ]
        replay = b"".join([
            text(1, "x"), text(2, "x"), text(3, "x"), text(4, "x"),
            text(5, digest), text(6, "x"), vint(7, 1), blob(8, summary), *frames,
        ])

        parsed = parse_mrv(replay)

        self.assertEqual(len(parsed.judgements), 1)
        self.assertEqual(len(parsed.inputs), 2)
        self.assertEqual(parsed.inputs[0].matched_judgement_id, 0)
        self.assertIsNone(parsed.inputs[1].matched_judgement_id)
        self.assertTrue(parsed.inputs[1].is_hold_regrab)
        self.assertEqual(parsed.inputs[1].release_time_ms, 225)

    def test_repeated_hold_regrabs_are_associated_and_keep_release_durations(self) -> None:
        package, digest = make_chart()
        frames = []

        def settlement(timestamp: int, lane: int) -> bytes:
            result = (vint(1, lane) if lane else b"") + vint(2, 1) + vint(3, 34)
            return blob(9, b"".join([
                vint(1, timestamp), wrapper(vint(1, lane)), wrapper(blob(31, result)),
                wrapper(fixed32(43, 100.0)),
            ]))

        frames.append(settlement(-25, 0))
        frames.append(settlement(510, 1))
        frames.append(blob(9, b"".join([
            vint(1, 600), wrapper(vint(2, 1)),
            wrapper(blob(31, vint(1, 1) + vint(2, 2) + vint(3, 0x40000))),
        ])))
        for press, release, flags in [(700, 800, 0x40000), (900, 1010, 0x800)]:
            frames.append(blob(9, b"".join([
                vint(1, press), wrapper(vint(1, 1)),
                wrapper(blob(31, vint(1, 1) + vint(2, 1) + vint(3, 0x1000000))),
                wrapper(fixed32(43, 100.0)),
            ])))
            frames.append(blob(9, b"".join([
                vint(1, release), wrapper(vint(2, 1)),
                wrapper(blob(31, vint(1, 1) + vint(2, 2) + vint(3, flags))),
            ])))
        frames.append(settlement(1260, 0))
        frames.append(settlement(1510, 1))
        summary = vint(7, 4) + fixed32(11, 100.0)
        replay = b"".join([
            text(1, "Test"), text(2, "Artist"), text(3, "Maker"), text(4, "Hard"),
            text(5, digest), text(6, "Player"), vint(7, 1), blob(8, summary), *frames,
        ])

        result, _, _ = analyze_files(package, replay)

        self.assertEqual(result["schemaVersion"], 52)
        self.assertEqual([note["judgementTimeMs"] for note in result["notes"]], [-25, 510, 1260, 1510])
        self.assertEqual(result["density"]["windowMs"], 1000)
        self.assertEqual(result["density"]["sampleIntervalMs"], 100)
        self.assertGreater(result["density"]["maxCount"], 0)
        hold = result["notes"][1]
        self.assertEqual(
            [(item["timeMs"], item["releaseTimeMs"], item["releaseBreaksCombo"])
             for item in hold["regrabs"]],
            [(700, 800, True), (900, 1010, False)],
        )
        regrabs = [item for item in result["inputs"] if item["isHoldRegrab"]]
        self.assertEqual([item["judgement"] for item in regrabs], ["miss", "miss"])
        self.assertEqual([item["matchedNoteId"] for item in regrabs], [1, 1])
        self.assertFalse(any(item["isExtra"] for item in regrabs))
        self.assertEqual(result["summary"]["holdRegrabs"], 2)
        self.assertEqual(result["summary"]["extraInputs"], 0)

    def test_hold_regrab_after_early_miss_is_associated(self) -> None:
        package, digest = make_chart()

        def settlement(timestamp: int, lane: int, accuracy: float, flags: int = 34) -> bytes:
            result = (vint(1, lane) if lane else b"") + vint(2, 1) + vint(3, flags)
            return blob(9, b"".join([
                vint(1, timestamp), wrapper(vint(1, lane)), wrapper(blob(31, result)),
                wrapper(fixed32(43, accuracy)),
            ]))

        frames = [
            settlement(-25, 0, 100.0),
            settlement(350, 1, 50.0, 0x41),
            blob(9, vint(1, 400) + wrapper(vint(2, 1))),
            blob(9, b"".join([
                vint(1, 525), wrapper(vint(1, 1)),
                wrapper(blob(31, vint(1, 1) + vint(2, 1) + vint(3, 0x1000000))),
                wrapper(fixed32(43, 50.0)),
            ])),
            blob(9, vint(1, 1010) + wrapper(vint(2, 1))),
            settlement(1260, 0, 200.0 / 3.0),
            settlement(1510, 1, 75.0),
        ]
        summary = vint(7, 3) + vint(10, 1) + fixed32(11, 75.0)
        replay = b"".join([
            text(1, "Test"), text(2, "Artist"), text(3, "Maker"), text(4, "Hard"),
            text(5, digest), text(6, "Player"), vint(7, 1), blob(8, summary), *frames,
        ])

        result, _, _ = analyze_files(package, replay)

        hold = result["notes"][1]
        self.assertEqual(hold["judgement"], "miss")
        self.assertFalse(hold["releaseBreaksCombo"])
        self.assertEqual(
            [(item["timeMs"], item["releaseTimeMs"]) for item in hold["regrabs"]],
            [(525, 1010)],
        )
        self.assertEqual(result["summary"]["holdRegrabs"], 1)

    def test_hold_regrab_before_head_after_early_combo_break_is_associated(self) -> None:
        package, digest = make_chart()

        def settlement(timestamp: int, lane: int, accuracy: float) -> bytes:
            result = (vint(1, lane) if lane else b"") + vint(2, 1) + vint(3, 34)
            return blob(9, b"".join([
                vint(1, timestamp), wrapper(vint(1, lane)), wrapper(blob(31, result)),
                wrapper(fixed32(43, accuracy)),
            ]))

        frames = [
            settlement(-25, 0, 100.0),
            settlement(350, 1, 70.0),
            blob(9, b"".join([
                vint(1, 400), wrapper(vint(2, 1)),
                wrapper(blob(31, vint(1, 1) + vint(2, 2) + vint(3, 0x40000))),
            ])),
            blob(9, b"".join([
                vint(1, 475), wrapper(vint(1, 1)),
                wrapper(blob(31, vint(1, 1) + vint(2, 1) + vint(3, 0x1000000))),
                wrapper(fixed32(43, 70.0)),
            ])),
            blob(9, vint(1, 1010) + wrapper(vint(2, 1))),
            settlement(1260, 0, 80.0),
            settlement(1510, 1, 85.0),
        ]
        summary = vint(7, 3) + vint(9, 1) + fixed32(11, 85.0)
        replay = b"".join([
            text(1, "Test"), text(2, "Artist"), text(3, "Maker"), text(4, "Hard"),
            text(5, digest), text(6, "Player"), vint(7, 1), blob(8, summary), *frames,
        ])

        result, _, _ = analyze_files(package, replay)

        hold = result["notes"][1]
        self.assertEqual(hold["inputTimeMs"], 350)
        self.assertTrue(hold["releaseBreaksCombo"])
        self.assertEqual(
            [(item["timeMs"], item["releaseTimeMs"]) for item in hold["regrabs"]],
            [(475, 1010)],
        )
        self.assertEqual(result["summary"]["holdRegrabs"], 1)

    def test_judgements_miss_and_extra_input(self) -> None:
        package, digest = make_chart()
        result, audio, mime = analyze_files(package, make_replay(digest))
        self.assertEqual(result["summary"]["accuracy"], 53.75)
        self.assertEqual(result["summary"]["byLane"]["accuracy"], [50.0, 57.5])
        expected_totals = {
            "totalNotes": 4,
            "perfect": 1,
            "cool": 1,
            "good": 1,
            "miss": 1,
            "extraInputs": 1,
            "holdRegrabs": 0,
        }
        self.assertEqual(
            {key: result["summary"][key] for key in expected_totals},
            expected_totals,
        )
        for key, lane_counts in result["summary"]["byLane"].items():
            if key != "accuracy":
                self.assertEqual(sum(lane_counts), result["summary"][key])
        self.assertEqual([note["judgement"] for note in result["notes"]], ["perfect", "cool", "miss", "good"])
        self.assertEqual(result["notes"][1]["endTimeMs"], 1000)
        self.assertEqual(result["notes"][1]["releaseTimeMs"], 510)
        self.assertFalse(result["notes"][1]["releaseBreaksCombo"])
        hold_input = next(item for item in result["inputs"] if item["matchedNoteId"] == 1)
        self.assertEqual(hold_input["releaseTimeMs"], 510)
        tap_input = next(item for item in result["inputs"] if item["matchedNoteId"] == 0)
        self.assertIsNone(tap_input["releaseTimeMs"])
        extra_input = next(item for item in result["inputs"] if item["isExtra"])
        self.assertIsNone(extra_input["releaseTimeMs"])
        self.assertEqual(sum(item["isExtra"] for item in result["inputs"]), 1)
        self.assertIsNotNone(audio)
        self.assertEqual(mime, "audio/ogg")

    def test_missing_audio_is_supported(self) -> None:
        package, digest = make_chart(include_audio=False)
        result, audio, mime = analyze_files(package, make_replay(digest))
        self.assertFalse(result["timeline"]["audioAvailable"])
        self.assertIsNone(audio)
        self.assertIsNone(mime)

    def test_unknown_accuracy_sequence_is_rejected(self) -> None:
        _, digest = make_chart()
        with self.assertRaisesRegex(ReplayRendererError, "Cannot recover judgement"):
            parse_mrv(make_replay(digest, bad_accuracy=True))

    def test_unknown_replay_structure_is_rejected(self) -> None:
        _, digest = make_chart()
        summary = fixed32(11, 100.0) + vint(7, 1)
        replay = b"".join([text(1,"x"),text(2,"x"),text(3,"x"),text(4,"x"),text(5,digest),text(6,"x"),vint(7,1),blob(8,summary),blob(9,vint(1,0))])
        with self.assertRaisesRegex(ReplayRendererError, "settlement"):
            parse_mrv(replay)


if __name__ == "__main__":
    unittest.main()
