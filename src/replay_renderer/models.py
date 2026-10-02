from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChartNote:
    id: int
    lane: int
    time_ms: float
    end_time_ms: float | None = None

    @property
    def is_hold_head(self) -> bool:
        return self.end_time_ms is not None


@dataclass
class ChartData:
    source_format: str
    title: str
    artist: str
    creator: str
    version: str
    key_count: int
    chart_md5: str
    notes: list[ChartNote]
    audio_name: str | None
    audio_bytes: bytes | None
    audio_mime: str | None
    audio_offset_ms: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class InputEvent:
    id: int
    lane: int
    time_ms: int
    matched_judgement_id: int | None = None
    release_time_ms: int | None = None
    release_breaks_combo: bool = False
    is_hold_regrab: bool = False


@dataclass
class JudgementEvent:
    id: int
    lane: int
    time_ms: int
    event_kind: int
    event_flags: int
    running_accuracy: float
    input_id: int | None
    judgement: str = ""


@dataclass
class ReplaySummary:
    perfect: int
    cool: int
    good: int
    miss: int
    accuracy: float


@dataclass
class ReplayData:
    title: str
    artist: str
    creator: str
    version: str
    chart_md5: str
    player: str
    format_marker: int
    mod_flags: int
    inputs: list[InputEvent]
    judgements: list[JudgementEvent]
    summary: ReplaySummary
    start_ms: int
    end_ms: int
    warnings: list[str] = field(default_factory=list)
