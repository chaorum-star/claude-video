#!/usr/bin/env python3
"""Match extracted SFX clips against a sound-effect library (SPEC F4-3 2차).

The point is killing catalog-search time: after `audio_events.py` cuts a
reference video's sound effects into clips, this tool answers "which effect in
MY library is that?" — where the library is any folder of audio files, e.g.
CapCut's locally cached sound-effect resources.

Pure stdlib + ffmpeg. Two features per sound, computed from its onset:
  - a 32-point RMS **energy envelope** (attack/decay shape over the first 1s)
  - a 24-bin log-spaced **spectral profile** (Goertzel magnitudes, 100–7000 Hz,
    over the loudest 256 ms window)
Similarity = 0.45·cos(envelope) + 0.45·cos(spectrum) + 0.1·duration closeness,
reported 0–100. Below --threshold (default 60) a match is reported as
NOT confident — never silently pick one (same F2-2 rule as effect mapping).

Usage:
    sfx_match.py index <library-dir> --out <index.json> [--names <names.json>]
    sfx_match.py match <clip.wav> [<clip2> ...] --index <index.json>
                 [--top 3] [--threshold 60]

`--names` is an optional {relative_path: display_name} JSON so opaque cache
filenames (resource IDs) can surface as the names shown in the CapCut UI.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
from array import array
from pathlib import Path

RATE = 16000
HOP = 256                  # 16 ms
WIN = 512
ENVELOPE_POINTS = 32
ENVELOPE_SPAN_S = 1.0
SPECTRUM_BINS = 24
SPECTRUM_LO_HZ = 100.0
SPECTRUM_HI_HZ = 7000.0
SPECTRUM_WINDOW = 4096     # 256 ms at 16 kHz
ONSET_FRAC = 0.05          # onset = first frame above 5% of peak RMS
DECAY_FRAC = 0.10
MAX_ANALYZE_S = 3.0        # a "short SFX" longer than this is analyzed truncated
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
DEFAULT_THRESHOLD = 60.0
DEFAULT_MIN_GAP = 3.0   # top1-top2 점수 차가 이보다 작으면 변별력 없음으로 판정
INDEX_VERSION = 1


def decode_pcm(path: str, rate: int = RATE, max_seconds: float = MAX_ANALYZE_S) -> array:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(Path(path).resolve()),
        "-t", f"{max_seconds:.3f}",
        "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            f"ffmpeg decode failed for {path}: {result.stderr.decode(errors='replace').strip()}"
        )
    samples = array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def frame_rms(samples: array, win: int = WIN, hop: int = HOP) -> list[float]:
    out: list[float] = []
    for start in range(0, max(0, len(samples) - win + 1), hop):
        chunk = samples[start:start + win]
        out.append((sum(x * x for x in chunk) / len(chunk)) ** 0.5)
    return out


def _resample(values: list[float], n: int) -> list[float]:
    """Linear-interpolate ``values`` onto ``n`` evenly spaced points."""
    if not values:
        return [0.0] * n
    if len(values) == 1:
        return [values[0]] * n
    out = []
    for i in range(n):
        pos = i * (len(values) - 1) / (n - 1)
        lo = int(pos)
        frac = pos - lo
        hi = min(lo + 1, len(values) - 1)
        out.append(values[lo] * (1 - frac) + values[hi] * frac)
    return out


def goertzel(samples: list[int] | array, rate: int, freq: float) -> float:
    coeff = 2.0 * math.cos(2.0 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return math.sqrt(max(s1 * s1 + s2 * s2 - coeff * s1 * s2, 0.0))


def _spectrum_freqs() -> list[float]:
    lo, hi = math.log(SPECTRUM_LO_HZ), math.log(SPECTRUM_HI_HZ)
    return [math.exp(lo + (hi - lo) * i / (SPECTRUM_BINS - 1)) for i in range(SPECTRUM_BINS)]


def extract_features(samples: array) -> dict | None:
    """Envelope + spectrum + duration from the onset. None = effectively silent."""
    rms = frame_rms(samples)
    if not rms:
        return None
    peak = max(rms)
    if peak <= 1.0:  # int16 scale — essentially digital silence
        return None
    hop_s = HOP / RATE

    onset = next(i for i, v in enumerate(rms) if v >= peak * ONSET_FRAC)

    # Envelope: first ENVELOPE_SPAN_S from onset, peak-normalized.
    span = rms[onset:onset + int(round(ENVELOPE_SPAN_S / hop_s))]
    envelope = _resample([v / peak for v in span], ENVELOPE_POINTS)

    # Duration: time from onset until decay below DECAY_FRAC of peak (capped).
    dur_frames = len(span)
    for j, v in enumerate(span):
        if j > 0 and v < peak * DECAY_FRAC:
            dur_frames = j
            break
    duration = dur_frames * hop_s

    # Spectrum over the loudest window: center a SPECTRUM_WINDOW slice on the
    # peak frame so the profile describes the effect body, not tail silence.
    peak_frame = max(range(onset, onset + len(span)), key=lambda i: rms[i])
    center = peak_frame * HOP + WIN // 2
    lo = max(0, min(center - SPECTRUM_WINDOW // 2, len(samples) - SPECTRUM_WINDOW))
    window = samples[lo:lo + SPECTRUM_WINDOW]
    mags = [math.log1p(goertzel(window, RATE, f)) for f in _spectrum_freqs()]
    mean = statistics.fmean(mags)
    centered = [m - mean for m in mags]
    norm = math.sqrt(sum(c * c for c in centered)) or 1.0
    spectrum = [c / norm for c in centered]

    return {"envelope": envelope, "spectrum": spectrum, "duration": round(duration, 3)}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def similarity(fa: dict, fb: dict) -> float:
    """0–100. Cosines are mapped from [-1, 1] to [0, 1] before weighting."""
    env = (_cosine(fa["envelope"], fb["envelope"]) + 1) / 2
    spec = (_cosine(fa["spectrum"], fb["spectrum"]) + 1) / 2
    dmax = max(fa["duration"], fb["duration"], 1e-3)
    dur = 1.0 - abs(fa["duration"] - fb["duration"]) / dmax
    return round(100 * (0.45 * env + 0.45 * spec + 0.10 * dur), 1)


def build_index(library_dir: Path, names: dict[str, str] | None = None) -> dict:
    files = sorted(
        p for p in library_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    )
    if not files:
        raise SystemExit(f"No audio files ({', '.join(sorted(AUDIO_SUFFIXES))}) under {library_dir}")

    entries, skipped = [], []
    for path in files:
        rel = str(path.relative_to(library_dir))
        try:
            features = extract_features(decode_pcm(str(path)))
        except SystemExit:
            skipped.append(rel)
            continue
        if features is None:
            skipped.append(rel)
            continue
        entries.append({
            "path": rel,
            "name": (names or {}).get(rel, path.stem),
            **features,
        })
    return {
        "version": INDEX_VERSION,
        "library_dir": str(library_dir.resolve()),
        "entry_count": len(entries),
        "skipped": skipped,
        "entries": entries,
    }


def match_clip(clip: Path, index: dict, top: int, threshold: float,
               min_gap: float = DEFAULT_MIN_GAP) -> dict:
    features = extract_features(decode_pcm(str(clip)))
    if features is None:
        return {"clip": str(clip), "error": "clip is silent/undecodable", "matches": []}
    ranked = sorted(
        (
            {"name": e["name"], "path": e["path"], "score": similarity(features, e)}
            for e in index["entries"]
        ),
        key=lambda m: m["score"], reverse=True,
    )[:top]
    best = ranked[0]["score"] if ranked else 0.0
    # 점수 갭: 내레이션·음악이 섞인 실클립은 모든 후보 점수가 비슷하게 몰린다
    # (2026-08-26 리허설: 82~84 클러스터). 절대 점수만으로는 확신할 수 없으므로
    # 1위가 2위를 min_gap 이상 앞설 때만 confident로 판정한다.
    gap = round(best - ranked[1]["score"], 1) if len(ranked) > 1 else None
    discriminative = gap is None or gap >= min_gap
    note = None
    if best < threshold:
        note = (
            f"최고 점수 {best} < {threshold} — 확신 없는 매칭. 라이브러리에 해당 효과음이 "
            "없거나(캡컷 캐시 미다운로드 포함) 원본 클립에 음성/음악이 섞였을 수 있음. "
            "후보를 수동 청취로 확인할 것."
        )
    elif not discriminative:
        note = (
            f"점수 갭 {gap} < {min_gap} — 상위 후보들이 몰려 있어 변별력 없음. "
            "원본 클립에 음성/음악이 섞였을 가능성이 큼. 후보를 수동 청취로 확인할 것."
        )
    return {
        "clip": str(clip),
        "confident": best >= threshold and discriminative,
        "score_gap": gap,
        "note": note,
        "matches": ranked,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a SFX library and match clips against it.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Precompute features for every audio file in a directory")
    p_index.add_argument("library_dir", type=Path)
    p_index.add_argument("--out", type=Path, required=True)
    p_index.add_argument("--names", type=Path, default=None,
                         help="Optional {relative_path: display_name} JSON")

    p_match = sub.add_parser("match", help="Match one or more clips against an index")
    p_match.add_argument("clips", nargs="+", type=Path)
    p_match.add_argument("--index", type=Path, required=True)
    p_match.add_argument("--top", type=int, default=3)
    p_match.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p_match.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP,
                         help=f"confident 판정에 필요한 1·2위 점수 차 (default {DEFAULT_MIN_GAP})")

    args = parser.parse_args()

    if args.cmd == "index":
        names = json.loads(args.names.read_text(encoding="utf-8")) if args.names else None
        index = build_index(args.library_dir, names=names)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(
            {"indexed": index["entry_count"], "skipped": index["skipped"], "out": str(args.out)},
            ensure_ascii=False,
        ))
        if index["skipped"]:
            print(f"WARNING: {len(index['skipped'])} file(s) skipped (silent/undecodable).",
                  file=sys.stderr)
        return

    index = json.loads(args.index.read_text(encoding="utf-8"))
    if index.get("version") != INDEX_VERSION:
        raise SystemExit(f"Index version mismatch (expected {INDEX_VERSION}) — re-run `index`.")
    results = [match_clip(clip, index, args.top, args.threshold, min_gap=args.min_gap)
               for clip in args.clips]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
