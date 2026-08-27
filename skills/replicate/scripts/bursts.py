#!/usr/bin/env python3
"""High-fps frame bursts around subtitle entrance/exit moments (SPEC F1-2).

/watch caps sampling at 2 fps, which cannot capture a 0.5s text animation.
This script deliberately lifts that cap — but only inside narrow windows
(default ±0.5s) around caller-supplied event timestamps, so token cost stays
bounded (SPEC 리스크: "등장/퇴장 ±0.5초 창에만 고밀도 적용").

Frames are named burst_<event>_<frame>.jpg. Overlapping windows are merged so
the same moment is never extracted twice; a merged window keeps the frames of
every event index it absorbed (reported in `merged_into`).

Usage:
    bursts.py <video> <out-dir> --events T1,T2,... [--window 0.5] [--fps 10]
              [--resolution 512] [--max-total 240]

Times accept SS, MM:SS, or HH:MM:SS (with optional .ms). Prints a JSON report.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_WINDOW = 0.5
DEFAULT_FPS = 10.0
DEFAULT_MAX_TOTAL = 240
MAX_READ_DIMENSION = 1998


def parse_time(value: str) -> float:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
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


def parse_events(value: str) -> list[float]:
    out = []
    for token in value.split(","):
        token = token.strip()
        if token:
            out.append(parse_time(token))
    if not out:
        raise SystemExit("--events produced no timestamps")
    return sorted(set(round(t, 3) for t in out))


def _scale_filter(resolution: int) -> str:
    return (
        f"scale=w='min({resolution},iw)':h='min({MAX_READ_DIMENSION},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def probe_duration(video_path: str) -> float:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
         str(Path(video_path).resolve())],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    return float(data.get("format", {}).get("duration") or 0)


def plan_windows(
    events: list[float], duration: float, window: float
) -> list[dict]:
    """Clamp each event's ±window to the video and merge overlapping windows.

    Returns [{event_indices, start, end}]; windows fully outside the video are
    dropped (their event indices are reported by the caller as skipped).
    """
    raw = []
    for i, t in enumerate(events):
        start = max(0.0, t - window)
        end = min(duration, t + window)
        if end - start <= 0:
            continue
        raw.append({"event_indices": [i], "start": start, "end": end})

    merged: list[dict] = []
    for win in raw:
        if merged and win["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], win["end"])
            merged[-1]["event_indices"] += win["event_indices"]
        else:
            merged.append(win)
    return merged


def extract_burst(
    video_path: str,
    out_dir: Path,
    window_index: int,
    start: float,
    end: float,
    fps: float,
    resolution: int,
) -> list[dict]:
    """Extract one window at high fps. Frames: burst_<window>_<frame>.jpg."""
    output_pattern = str(out_dir / f"burst_{window_index:03d}_%03d.jpg")
    # -ss before -i with a re-encoded image output is frame-accurate in modern
    # ffmpeg (it decodes forward from the prior keyframe), so no slow seek needed.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", str(Path(video_path).resolve()),
        "-vf", f"fps={fps},{_scale_filter(resolution)}",
        "-q:v", "4",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg burst extraction failed: {result.stderr.strip()}")

    frames = sorted(out_dir.glob(f"burst_{window_index:03d}_*.jpg"))
    return [
        {
            "window": window_index,
            "frame": i,
            "timestamp_seconds": round(start + i / fps, 3),
            "path": str(p),
        }
        for i, p in enumerate(frames)
    ]


def extract_bursts(
    video_path: str,
    out_dir: Path,
    events: list[float],
    window: float = DEFAULT_WINDOW,
    fps: float = DEFAULT_FPS,
    resolution: int = 512,
    max_total: int = DEFAULT_MAX_TOTAL,
) -> dict:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")
    if fps <= 0 or window <= 0:
        raise SystemExit("--fps and --window must be positive")

    duration = probe_duration(video_path)
    windows = plan_windows(events, duration, window)
    covered = {i for win in windows for i in win["event_indices"]}
    skipped_events = [events[i] for i in range(len(events)) if i not in covered]

    # Budget guard: trim whole windows from the end rather than thinning fps,
    # and say so explicitly — a silently lowered fps would defeat the point.
    per_window = [max(1, int(round((w["end"] - w["start"]) * fps))) for w in windows]
    dropped_windows: list[dict] = []
    while windows and sum(per_window) > max_total:
        dropped_windows.insert(0, windows.pop())
        per_window.pop()

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("burst_*.jpg"):
        existing.unlink()

    frames: list[dict] = []
    for i, win in enumerate(windows):
        frames += extract_burst(
            video_path, out_dir, i, win["start"], win["end"], fps, resolution
        )

    return {
        "video": str(video_path),
        "duration_seconds": round(duration, 3),
        "fps": fps,
        "window_seconds": window,
        "events": events,
        "windows": [
            {
                "index": i,
                "start": round(w["start"], 3),
                "end": round(w["end"], 3),
                "events": [round(events[j], 3) for j in w["event_indices"]],
            }
            for i, w in enumerate(windows)
        ],
        "skipped_out_of_range": skipped_events,
        "dropped_over_budget": [
            {"start": round(w["start"], 3), "end": round(w["end"], 3),
             "events": [round(events[j], 3) for j in w["event_indices"]]}
            for w in dropped_windows
        ],
        "frame_count": len(frames),
        "frames": frames,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract high-fps frame bursts around event timestamps."
    )
    parser.add_argument("video")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--events", required=True,
                        help="Comma-separated timestamps (SS, MM:SS, or HH:MM:SS)")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW,
                        help=f"Half-window in seconds around each event (default {DEFAULT_WINDOW})")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help=f"Burst sampling rate (default {DEFAULT_FPS})")
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--max-total", type=int, default=DEFAULT_MAX_TOTAL,
                        help=f"Total frame budget across all bursts (default {DEFAULT_MAX_TOTAL})")
    args = parser.parse_args()

    report = extract_bursts(
        args.video, args.out_dir, parse_events(args.events),
        window=args.window, fps=args.fps,
        resolution=args.resolution, max_total=args.max_total,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["dropped_over_budget"]:
        print(
            f"WARNING: {len(report['dropped_over_budget'])} window(s) dropped to stay "
            f"under --max-total {args.max_total}. Re-run on those ranges separately.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
