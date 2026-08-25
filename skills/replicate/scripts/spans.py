#!/usr/bin/env python3
"""Propose subtitle spans + burst events from a transcript (SPEC F1-1).

Input is either a WebVTT file (the `video*.vtt` yt-dlp leaves in the /watch
work dir) or a JSON file containing ``[{"start", "end", "text"}, ...]`` — the
same segment shape /watch uses internally. When the transcript came from
Whisper (no file on disk), write the segments you already have to a JSON file
and pass that.

Output: span candidates (adjacent segments merged across sub-``--gap`` pauses)
plus the de-duplicated entrance/exit timestamps to feed ``bursts.py --events``.
These are *candidates* from speech timing — on-screen text can differ (static
captions, title cards), so the frames decide, not this script.

Usage:
    spans.py <transcript.vtt|transcript.json> [--gap 0.3] [--min-dur 0.2]
             [--min-event-sep 0.2]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_GAP = 0.3
DEFAULT_MIN_DUR = 0.2
DEFAULT_MIN_EVENT_SEP = 0.2

VTT_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
)
VTT_TAG_RE = re.compile(r"<[^>]+>")


def _to_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(path: Path) -> list[dict]:
    """Minimal WebVTT cue parser (also accepts SRT-style comma timestamps)."""
    segments: list[dict] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        match = VTT_TIME_RE.search(lines[i])
        if not match:
            i += 1
            continue
        start = _to_seconds(*match.groups()[:4])
        end = _to_seconds(*match.groups()[4:])
        i += 1
        cue_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = VTT_TAG_RE.sub("", lines[i]).strip()
            if cleaned:
                cue_lines.append(cleaned)
            i += 1
        text = " ".join(cue_lines).strip()
        if text and end > start:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return _dedupe(segments)


def _dedupe(segments: list[dict]) -> list[dict]:
    """Collapse the repeated/rolling cues auto-captions produce."""
    out: list[dict] = []
    for seg in segments:
        if out and seg["text"] == out[-1]["text"]:
            out[-1]["end"] = seg["end"]
            continue
        if out and seg["text"].startswith(out[-1]["text"] + " "):
            out[-1]["text"] = seg["text"]
            out[-1]["end"] = seg["end"]
            continue
        out.append(dict(seg))
    return out


def load_segments(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SystemExit(f"{path}: expected a JSON list of segments")
        segments = []
        for seg in data:
            try:
                segments.append({
                    "start": float(seg["start"]),
                    "end": float(seg["end"]),
                    "text": str(seg["text"]).strip(),
                })
            except (KeyError, TypeError, ValueError):
                raise SystemExit(f"{path}: bad segment {seg!r} (need start/end/text)")
        return _dedupe([s for s in segments if s["text"] and s["end"] > s["start"]])
    return parse_vtt(path)


def propose_spans(
    segments: list[dict],
    gap: float = DEFAULT_GAP,
    min_dur: float = DEFAULT_MIN_DUR,
) -> list[dict]:
    """Merge segments separated by <= ``gap`` seconds into span candidates.

    Auto-caption segmentation splits mid-sentence; a subtitle that stays on
    screen across that split is one span, not two. Spans shorter than
    ``min_dur`` are noise (stray cue fragments) and dropped.
    """
    spans: list[dict] = []
    for seg in sorted(segments, key=lambda s: s["start"]):
        if spans and seg["start"] - spans[-1]["end"] <= gap:
            spans[-1]["end"] = max(spans[-1]["end"], seg["end"])
            spans[-1]["text"] += " " + seg["text"]
        else:
            spans.append(dict(seg))

    spans = [s for s in spans if s["end"] - s["start"] >= min_dur]
    for i, span in enumerate(spans):
        span["index"] = i
        span["start"] = round(span["start"], 3)
        span["end"] = round(span["end"], 3)
    return spans


def burst_events(spans: list[dict], min_sep: float = DEFAULT_MIN_EVENT_SEP) -> list[float]:
    """Entrance + exit timestamps, deduplicated within ``min_sep`` seconds.

    When one span's exit and the next span's entrance nearly coincide (rolling
    subtitles), a single burst window covers both — bursts.py merges the
    windows anyway, but deduping here keeps the event list honest.
    """
    times = sorted({t for span in spans for t in (span["start"], span["end"])})
    out: list[float] = []
    for t in times:
        if not out or t - out[-1] >= min_sep:
            out.append(round(t, 3))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Propose subtitle spans and burst events from a transcript."
    )
    parser.add_argument("transcript", type=Path, help="WebVTT file or JSON segment list")
    parser.add_argument("--gap", type=float, default=DEFAULT_GAP,
                        help=f"Merge segments with gaps <= this many seconds (default {DEFAULT_GAP})")
    parser.add_argument("--min-dur", type=float, default=DEFAULT_MIN_DUR,
                        help=f"Drop spans shorter than this (default {DEFAULT_MIN_DUR})")
    parser.add_argument("--min-event-sep", type=float, default=DEFAULT_MIN_EVENT_SEP,
                        help=f"Merge burst events closer than this (default {DEFAULT_MIN_EVENT_SEP})")
    args = parser.parse_args()

    if not args.transcript.is_file():
        raise SystemExit(f"Transcript not found: {args.transcript}")
    segments = load_segments(args.transcript)
    if not segments:
        raise SystemExit(f"No usable segments in {args.transcript}")

    spans = propose_spans(segments, gap=args.gap, min_dur=args.min_dur)
    events = burst_events(spans, min_sep=args.min_event_sep)
    print(json.dumps(
        {
            "segment_count": len(segments),
            "spans": spans,
            "burst_events": events,
            "burst_events_arg": ",".join(f"{t:g}" for t in events),
        },
        ensure_ascii=False, indent=2,
    ))
    if not spans:
        print("WARNING: every span was filtered out — check --gap/--min-dur.", file=sys.stderr)


if __name__ == "__main__":
    main()
