from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Cue:
    start: float
    end: float
    text: str
    source: str = "speech"
    words: list[WordTiming] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WordTiming:
    start: float
    end: float
    text: str


@dataclass
class CaptionEvent:
    start: float
    end: float
    text: str
    kind: str
    color: str
    size_scale: float
    animation: str
    confidence: float
    capcut_animation: str | None = None
    match_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OverlayEvent:
    start: float
    end: float
    kind: str
    center_x: float
    center_y: float
    width: float
    height: float
    label: str
    confidence: float
    asset_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MotionEvent:
    start: float
    end: float
    kind: str
    scale_from: float = 1.0
    scale_to: float = 1.0
    shift_x: float = 0.0
    shift_y: float = 0.0
    confidence: float = 0.0
    capcut_animation: str | None = None
    match_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SoundEvent:
    start: float
    end: float
    kind: str
    confidence: float
    energy: float
    spectral_centroid: float
    asset_path: str | None = None
    basis: str = "audio_peak"
    capcut_sound: str | None = None
    match_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VisualEffect:
    start: float
    end: float
    kind: str
    label: str
    confidence: float
    evidence: str
    capcut_suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaptionStyle:
    position: str = "판별 불가"
    size: str = "판별 불가"
    color: str = "판별 불가"
    outline: str = "판별 불가"
    background: str = "판별 불가"
    animation: str = "판별 불가"
    confidence: float = 0.0
    center_y: float | None = None
    height_ratio: float | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TitleCard:
    """도입부에 떠 있는 제목·후킹 문구 카드."""
    start: float
    end: float
    center_y: float
    height_ratio: float
    color: str
    box_color: str | None = None
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Analysis:
    duration: float
    width: int
    height: int
    speech: list[Cue] = field(default_factory=list)
    screen_text: list[Cue] = field(default_factory=list)
    scenes: list[float] = field(default_factory=list)
    audio_peaks: list[float] = field(default_factory=list)
    visual_effects: list[VisualEffect] = field(default_factory=list)
    caption_style: CaptionStyle = field(default_factory=CaptionStyle)
    title_card: TitleCard | None = None
    caption_events: list[CaptionEvent] = field(default_factory=list)
    overlay_events: list[OverlayEvent] = field(default_factory=list)
    motion_events: list[MotionEvent] = field(default_factory=list)
    sound_events: list[SoundEvent] = field(default_factory=list)
    planning: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "speech": [item.to_dict() for item in self.speech],
            "screen_text": [item.to_dict() for item in self.screen_text],
            "scenes": self.scenes,
            "audio_peaks": self.audio_peaks,
            "visual_effects": [item.to_dict() for item in self.visual_effects],
            "caption_style": self.caption_style.to_dict(),
            "title_card": self.title_card.to_dict() if self.title_card else None,
            "caption_events": [item.to_dict() for item in self.caption_events],
            "overlay_events": [item.to_dict() for item in self.overlay_events],
            "motion_events": [item.to_dict() for item in self.motion_events],
            "sound_events": [item.to_dict() for item in self.sound_events],
            "planning": self.planning,
            "warnings": self.warnings,
        }


def analysis_from_dict(payload: dict[str, Any]) -> Analysis:
    """Restore a saved analysis so its timing and effect grammar can be reused."""
    def cue(item: dict[str, Any]) -> Cue:
        return Cue(
            start=float(item.get("start", 0)),
            end=float(item.get("end", 0)),
            text=str(item.get("text", "")),
            source=str(item.get("source", "speech")),
            words=[
                WordTiming(float(word.get("start", 0)), float(word.get("end", 0)), str(word.get("text", "")))
                for word in item.get("words", []) if isinstance(word, dict)
            ],
        )

    style_payload = payload.get("caption_style") if isinstance(payload.get("caption_style"), dict) else {}
    title_payload = payload.get("title_card") if isinstance(payload.get("title_card"), dict) else None
    return Analysis(
        duration=float(payload.get("duration", 0)),
        width=int(payload.get("width", 1080)),
        height=int(payload.get("height", 1920)),
        speech=[cue(item) for item in payload.get("speech", []) if isinstance(item, dict)],
        screen_text=[cue(item) for item in payload.get("screen_text", []) if isinstance(item, dict)],
        scenes=[float(item) for item in payload.get("scenes", [])],
        audio_peaks=[float(item) for item in payload.get("audio_peaks", [])],
        visual_effects=[VisualEffect(**item) for item in payload.get("visual_effects", []) if isinstance(item, dict)],
        caption_style=CaptionStyle(**style_payload),
        title_card=TitleCard(**title_payload) if title_payload else None,
        caption_events=[CaptionEvent(**item) for item in payload.get("caption_events", []) if isinstance(item, dict)],
        overlay_events=[OverlayEvent(**item) for item in payload.get("overlay_events", []) if isinstance(item, dict)],
        motion_events=[MotionEvent(**item) for item in payload.get("motion_events", []) if isinstance(item, dict)],
        sound_events=[SoundEvent(**item) for item in payload.get("sound_events", []) if isinstance(item, dict)],
        planning=payload.get("planning", {}) if isinstance(payload.get("planning"), dict) else {},
        warnings=[str(item) for item in payload.get("warnings", [])],
    )


@dataclass
class JobResult:
    source: str
    work_dir: Path
    video_path: Path
    analysis: Analysis
    srt_path: Path
    guide_path: Path
    dependency_path: Path
    planning_path: Path | None = None
    archive_path: Path | None = None
    obsidian: dict[str, Any] = field(default_factory=dict)
    capcut_project: str | None = None
    capcut_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "work_dir": str(self.work_dir),
            "video_path": str(self.video_path),
            "analysis": self.analysis.to_dict(),
            "srt_path": str(self.srt_path),
            "guide_path": str(self.guide_path),
            "dependency_path": str(self.dependency_path),
            "planning_path": str(self.planning_path) if self.planning_path else None,
            "archive_path": str(self.archive_path) if self.archive_path else None,
            "obsidian": self.obsidian,
            "capcut_project": self.capcut_project,
            "capcut_error": self.capcut_error,
        }
