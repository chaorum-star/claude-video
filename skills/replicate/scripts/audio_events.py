#!/usr/bin/env python3
"""Detect short sound-effect events and cut them into wav clips (SPEC F4, M4).

Pure-stdlib + ffmpeg (D-2 resolved: no librosa). ffmpeg decodes the audio to
mono 16 kHz PCM; detection runs on the **first-difference** (high-pass) energy
of short frames, so steady background music and speech contribute little while
broadband transients — whoosh, pop, hit — spike the novelty ratio against the
trailing median. This is a v1 heuristic tuned for 쇼츠 transition sounds, not a
general SFX detector; misses/false-positives are expected and the report says
so via per-event scores.

Given scene-change timestamps (``--scenes``), events within ±``--corr-window``
of a cut are labeled ``transition``, the rest ``accent`` (F4-2).

Each event's audio is cut to ``sfx_NNN.wav`` from the original media (F4-3
1차 — 원본 클립 재사용). Per F4-4 the report carries a copyright notice: these
clips are for personal study / prototyping only.

Usage:
    audio_events.py <video-or-audio> <out-dir> [--scenes T1,T2,...]
        [--corr-window 0.3] [--sensitivity 3.0] [--floor-db -48]
        [--min-sep 0.15] [--max-events 40] [--clip-pre 0.05] [--no-clips]

Prints a JSON report.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
from array import array
from pathlib import Path

RATE = 16000
WIN = 512     # 32 ms analysis window
HOP = 256     # 16 ms hop → event timing resolution
DEFAULT_SENSITIVITY = 3.0   # HF energy must exceed 3× the trailing median
DEFAULT_FLOOR_DB = -48.0    # ...and clear an absolute dBFS floor
DEFAULT_MIN_SEP = 0.15
DEFAULT_CORR_WINDOW = 0.3   # F4-2: 전환 ±0.3초 내 onset = 전환음
DEFAULT_MAX_EVENTS = 40
DEFAULT_CLIP_PRE = 0.05
MAX_EVENT_DUR = 1.2
MIN_CLIP_LEN = 0.2
HISTORY_S = 0.5

COPYRIGHT_NOTICE = (
    "추출된 효과음 클립은 레퍼런스 원본의 일부입니다. 개인 학습·프로토타입 용도로만 "
    "사용하고, 배포물에는 캡컷 내장 효과음 또는 라이선스 확보된 SFX로 교체하세요 (SPEC F4-4)."
)


def parse_time(value: str) -> float:
    s = str(value).strip()
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time value: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def parse_times(value: str | None) -> list[float]:
    if not value:
        return []
    out = [parse_time(tok) for tok in value.split(",") if tok.strip()]
    return sorted(set(round(t, 3) for t in out))


def probe_audio(path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(Path(path).resolve())],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    duration = float(data.get("format", {}).get("duration") or 0)
    return {"has_audio": has_audio, "duration_seconds": duration}


def decode_pcm(path: str, rate: int = RATE) -> array:
    """Decode to mono s16le samples. Raises SystemExit when there is no audio."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(Path(path).resolve()),
        "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio decode failed: {result.stderr.decode(errors='replace').strip()}")
    samples = array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def hf_energies(samples: array, win: int = WIN, hop: int = HOP) -> list[float]:
    """Per-frame RMS of the first-difference signal (crude high-pass).

    Differencing suppresses DC and low-frequency content (bass lines, voiced
    speech), so steady music beds stay low while broadband transients spike.
    """
    diff = [samples[k + 1] - samples[k] for k in range(len(samples) - 1)]
    out: list[float] = []
    for start in range(0, max(0, len(diff) - win + 1), hop):
        chunk = diff[start:start + win]
        out.append((sum(d * d for d in chunk) / len(chunk)) ** 0.5)
    return out


def dbfs(rms: float) -> float:
    import math
    if rms <= 0:
        return -120.0
    return round(20 * math.log10(rms / 32768.0), 1)


def detect_onsets(
    hf: list[float],
    hop_s: float = HOP / RATE,
    sensitivity: float = DEFAULT_SENSITIVITY,
    floor_db: float = DEFAULT_FLOOR_DB,
    min_sep: float = DEFAULT_MIN_SEP,
    history_s: float = HISTORY_S,
) -> list[dict]:
    """Novelty-ratio onset picking over the HF energy envelope.

    A frame is a candidate when its HF energy exceeds ``sensitivity`` × the
    median of the trailing ``history_s`` window *and* clears the absolute
    ``floor_db`` (so near-silence noise never triggers). Candidate frames
    within ``min_sep`` of each other collapse into one event whose time is the
    **first** candidate frame (the rise), and whose score is the group max.
    """
    hist = max(3, int(round(history_s / hop_s)))
    floor = 32768.0 * 10 ** (floor_db / 20)
    eps = 1e-6

    candidates: list[tuple[int, float]] = []
    for i in range(len(hf)):
        lo = max(0, i - hist)
        if i - lo < 3:
            continue  # not enough history to judge novelty yet
        base = statistics.median(hf[lo:i])
        score = hf[i] / (base + eps)
        if score >= sensitivity and hf[i] >= floor:
            candidates.append((i, score))

    groups: list[list[tuple[int, float]]] = []
    for i, score in candidates:
        if groups and (i - groups[-1][-1][0]) * hop_s <= min_sep:
            groups[-1].append((i, score))
        else:
            groups.append([(i, score)])

    events = []
    for group in groups:
        first = group[0][0]
        peak_frame = max(group, key=lambda g: hf[g[0]])[0]
        events.append({
            "frame": first,
            "time": round(first * hop_s, 3),
            "score": round(max(s for _, s in group), 1),
            "dbfs": dbfs(hf[peak_frame]),
        })
    return events


def estimate_end(
    hf: list[float],
    onset_frame: int,
    hop_s: float = HOP / RATE,
    history_s: float = HISTORY_S,
    max_dur: float = MAX_EVENT_DUR,
) -> float:
    """Time (relative to stream start) where the event decays back to its
    pre-onset baseline — two consecutive frames at/below 1.5× baseline — capped
    at ``max_dur`` past the onset."""
    hist = max(3, int(round(history_s / hop_s)))
    lo = max(0, onset_frame - hist)
    baseline = statistics.median(hf[lo:onset_frame]) if onset_frame > lo else 0.0
    thresh = baseline * 1.5 + 1e-6
    max_frame = min(len(hf), onset_frame + int(round(max_dur / hop_s)))

    below = 0
    for j in range(onset_frame + 1, max_frame):
        below = below + 1 if hf[j] <= thresh else 0
        if below >= 2:
            return round((j - 1) * hop_s, 3)
    return round(max_frame * hop_s, 3)


def correlate_scenes(
    events: list[dict], scenes: list[float], window: float = DEFAULT_CORR_WINDOW
) -> None:
    """Label each event in place: within ±window of a scene cut → transition."""
    for event in events:
        near = [s for s in scenes if abs(s - event["time"]) <= window]
        event["near_scene"] = min(near, key=lambda s: abs(s - event["time"])) if near else None
        event["type"] = "transition" if near else "accent"


def cut_clip(source: str, out_path: Path, start: float, end: float) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start):.3f}", "-to", f"{end:.3f}",
        "-i", str(Path(source).resolve()),
        "-vn", "-acodec", "pcm_s16le",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and out_path.exists()


def analyze(
    source: str,
    out_dir: Path,
    scenes: list[float] | None = None,
    sensitivity: float = DEFAULT_SENSITIVITY,
    floor_db: float = DEFAULT_FLOOR_DB,
    min_sep: float = DEFAULT_MIN_SEP,
    corr_window: float = DEFAULT_CORR_WINDOW,
    max_events: int = DEFAULT_MAX_EVENTS,
    clip_pre: float = DEFAULT_CLIP_PRE,
    clips: bool = True,
) -> dict:
    probe = probe_audio(source)
    if not probe["has_audio"]:
        raise SystemExit(f"{source} has no audio stream — nothing to detect.")

    samples = decode_pcm(source)
    hf = hf_energies(samples)
    hop_s = HOP / RATE
    events = detect_onsets(
        hf, hop_s=hop_s, sensitivity=sensitivity,
        floor_db=floor_db, min_sep=min_sep,
    )

    dropped_low_score = 0
    if len(events) > max_events:
        keep = set(
            id(e) for e in sorted(events, key=lambda e: e["score"], reverse=True)[:max_events]
        )
        dropped_low_score = len(events) - max_events
        events = [e for e in events if id(e) in keep]  # keeps chronological order

    correlate_scenes(events, scenes or [], window=corr_window)

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("sfx_*.wav"):
        existing.unlink()

    duration = probe["duration_seconds"] or len(samples) / RATE
    for i, event in enumerate(events):
        event["index"] = i
        end = estimate_end(hf, event.pop("frame"), hop_s=hop_s)
        end = min(duration, max(end, event["time"] + MIN_CLIP_LEN))
        event["end"] = round(end, 3)
        event["clip"] = None
        if clips:
            path = out_dir / f"sfx_{i:03d}.wav"
            if cut_clip(source, path, event["time"] - clip_pre, end):
                event["clip"] = str(path)

    return {
        "source": str(source),
        "duration_seconds": round(duration, 3),
        "sample_rate": RATE,
        "params": {
            "sensitivity": sensitivity, "floor_db": floor_db, "min_sep": min_sep,
            "corr_window": corr_window, "hop_seconds": round(hop_s, 4),
        },
        "scenes": scenes or [],
        "event_count": len(events),
        "dropped_low_score": dropped_low_score,
        "events": events,
        "copyright_notice": COPYRIGHT_NOTICE,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect short SFX events in a video/audio file and cut them to wav clips."
    )
    parser.add_argument("source")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--scenes", default=None,
                        help="Comma-separated scene-change timestamps for transition labeling")
    parser.add_argument("--corr-window", type=float, default=DEFAULT_CORR_WINDOW)
    parser.add_argument("--sensitivity", type=float, default=DEFAULT_SENSITIVITY,
                        help=f"HF novelty ratio required (default {DEFAULT_SENSITIVITY})")
    parser.add_argument("--floor-db", type=float, default=DEFAULT_FLOOR_DB,
                        help=f"Absolute dBFS floor (default {DEFAULT_FLOOR_DB})")
    parser.add_argument("--min-sep", type=float, default=DEFAULT_MIN_SEP)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument("--clip-pre", type=float, default=DEFAULT_CLIP_PRE)
    parser.add_argument("--no-clips", action="store_true")
    args = parser.parse_args()

    report = analyze(
        args.source, args.out_dir,
        scenes=parse_times(args.scenes),
        sensitivity=args.sensitivity, floor_db=args.floor_db,
        min_sep=args.min_sep, corr_window=args.corr_window,
        max_events=args.max_events, clip_pre=args.clip_pre,
        clips=not args.no_clips,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["dropped_low_score"]:
        print(
            f"WARNING: kept top {args.max_events} events by score; "
            f"{report['dropped_low_score']} low-score event(s) dropped. "
            "Raise --max-events or --sensitivity to change this.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
