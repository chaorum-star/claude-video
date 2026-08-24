from __future__ import annotations

import array
import json
import math
import re
import statistics
import subprocess
import sys
import shutil
from difflib import SequenceMatcher
from pathlib import Path

from .models import Analysis, CaptionStyle, Cue
from .editing_grammar import analyze_editing_grammar
from .transcribe import transcribe
from .utils import ToolError, probe_video, run
from .visual_effects import analyze_visual_language


SCENE_RE = re.compile(r"pts_time:([0-9.]+)")


def analyze_video(video_path: Path, work_dir: Path) -> Analysis:
    duration, width, height = probe_video(video_path)
    warnings: list[str] = []

    try:
        speech, warning = transcribe(video_path, work_dir)
        if warning:
            warnings.append(warning)
    except ToolError as exc:
        speech = []
        warnings.append(str(exc))

    try:
        scenes = detect_scenes(video_path, duration)
    except ToolError as exc:
        scenes = []
        warnings.append(f"장면 전환 분석 실패: {exc}")

    try:
        audio_peaks = detect_audio_peaks(video_path)
    except ToolError as exc:
        audio_peaks = []
        warnings.append(f"오디오 피크 분석 실패: {exc}")

    try:
        screen_text = detect_screen_text(video_path, work_dir, duration)
    except ToolError as exc:
        screen_text = []
        warnings.append(f"화면 글자 분석 실패: {exc}")

    try:
        visual_effects, caption_style, title_card = analyze_visual_language(
            video_path, work_dir, duration, scenes, speech
        )
    except (ToolError, OSError, ValueError) as exc:
        visual_effects = []
        caption_style = CaptionStyle(evidence=[f"시각 효과 분석 실패: {exc}"])
        title_card = None
        warnings.append(f"시각 효과 분석 실패: {exc}")

    try:
        caption_events, overlay_events, motion_events, sound_events = analyze_editing_grammar(
            video_path, work_dir, duration, scenes, speech, caption_style, audio_peaks
        )
    except (ToolError, OSError, ValueError) as exc:
        caption_events, overlay_events, motion_events, sound_events = [], [], [], []
        warnings.append(f"편집 문법 분석 실패: {exc}")

    return Analysis(
        duration=duration,
        width=width,
        height=height,
        speech=speech,
        screen_text=remove_speech_duplicates(screen_text, speech),
        scenes=scenes,
        audio_peaks=audio_peaks,
        visual_effects=visual_effects,
        caption_style=caption_style,
        title_card=title_card,
        caption_events=caption_events,
        overlay_events=overlay_events,
        motion_events=motion_events,
        sound_events=sound_events,
        warnings=warnings,
    )


def detect_scenes(video_path: Path, duration: float, threshold: float = 0.32) -> list[float]:
    completed = run([
        "ffmpeg", "-hide_banner", "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ], check=False)
    combined = f"{completed.stdout}\n{completed.stderr}"
    points = sorted({round(float(value), 3) for value in SCENE_RE.findall(combined)})
    return [point for point in points if 0.25 < point < duration - 0.25]


def detect_audio_peaks(video_path: Path, sample_rate: int = 16000) -> list[float]:
    completed = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-",
        ],
        capture_output=True,
        timeout=900,
    )
    if completed.returncode != 0:
        raise ToolError(completed.stderr.decode("utf-8", "replace")[-2000:])
    samples = array.array("h")
    samples.frombytes(completed.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    window = max(1, sample_rate // 10)
    rms: list[float] = []
    for offset in range(0, len(samples), window):
        chunk = samples[offset:offset + window]
        if not chunk:
            break
        rms.append(math.sqrt(sum(value * value for value in chunk) / len(chunk)))
    nonzero = [value for value in rms if value > 1]
    if len(nonzero) < 3:
        return []
    median = statistics.median(nonzero)
    ordered = sorted(nonzero)
    percentile = ordered[min(len(ordered) - 1, int(len(ordered) * 0.90))]
    gate = max(percentile, median * 2.0, 900)
    candidates = [
        index / 10
        for index in range(1, len(rms) - 1)
        if rms[index] >= gate and rms[index] >= rms[index - 1] and rms[index] >= rms[index + 1]
    ]
    selected: list[float] = []
    for point in candidates:
        if not selected or point - selected[-1] >= 0.7:
            selected.append(round(point, 2))
    return selected[:30]


def detect_screen_text(video_path: Path, work_dir: Path, duration: float) -> list[Cue]:
    frame_dir = work_dir / "ocr_frames"
    frame_dir.mkdir(exist_ok=True)
    interval = max(1.0, duration / 120.0)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
        "-vf", f"fps=1/{interval:.4f},scale='min(1280,iw)':-2",
        "-q:v", "3", str(frame_dir / "frame-%05d.jpg"),
    ], timeout=1800)
    python_helper = Path(__file__).with_name("vision_ocr.py")
    # Tesseract is installed by the app setup and is the most reliable path on
    # current macOS releases. Only build the heavier Vision helper as a fallback.
    swift_helper = None if shutil.which("tesseract") else build_swift_ocr_helper(work_dir)
    snapshots: list[tuple[float, str]] = []
    failures: list[str] = []
    for index, frame in enumerate(sorted(frame_dir.glob("frame-*.jpg"))):
        payload = recognize_frame(frame, python_helper, swift_helper)
        if payload.get("error"):
            failures.append(str(payload["error"]))
            continue
        lines = [clean_text(line) for line in payload.get("lines", [])]
        text = "\n".join(line for line in lines if len(normalize_text(line)) >= 2)
        if text:
            snapshots.append((round(index * interval, 3), text[:500]))
    if failures and not snapshots:
        raise ToolError(f"OCR 엔진이 이미지를 읽지 못했습니다: {failures[-1]}")
    return merge_ocr_snapshots(snapshots, interval, duration)


def recognize_frame(frame: Path, python_helper: Path, swift_helper: Path | None) -> dict:
    tesseract = shutil.which("tesseract")
    if tesseract:
        completed = subprocess.run(
            [tesseract, str(frame), "stdout", "-l", "kor+eng", "--psm", "6"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if completed.returncode == 0:
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            if lines:
                return {"lines": lines}

    commands: list[list[str]] = []
    if swift_helper is not None:
        commands.append([str(swift_helper), str(frame)])
    commands.append(["/usr/bin/python3", str(python_helper), str(frame)])
    for command in commands:
        completed = subprocess.run(
            command, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if completed.returncode == 0:
            try:
                return json.loads(completed.stdout)
            except json.JSONDecodeError:
                pass
    detail = "Vision 실패"
    if commands:
        detail = (completed.stderr or completed.stdout or detail).strip()[-500:]
    return {"error": detail}


def build_swift_ocr_helper(work_dir: Path) -> Path | None:
    compiler = shutil.which("xcrun")
    source = Path(__file__).with_name("vision_ocr.swift")
    if not compiler or not source.is_file():
        return None
    binary = work_dir / "vision-ocr"
    module_cache = work_dir / "swift-module-cache"
    module_cache.mkdir(exist_ok=True)
    completed = subprocess.run(
        [compiler, "swiftc", "-module-cache-path", str(module_cache), str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return binary if completed.returncode == 0 and binary.is_file() else None


def merge_ocr_snapshots(snapshots: list[tuple[float, str]], interval: float, duration: float) -> list[Cue]:
    cues: list[Cue] = []
    for start, text in snapshots:
        if cues and similar_text(cues[-1].text, text) >= 0.82 and start - cues[-1].end <= interval * 1.5:
            cues[-1].end = min(duration, start + interval)
            if len(text) > len(cues[-1].text):
                cues[-1].text = text
        else:
            cues.append(Cue(start, min(duration, start + interval), text, "screen"))
    return cues


def remove_speech_duplicates(screen: list[Cue], speech: list[Cue]) -> list[Cue]:
    unique: list[Cue] = []
    for screen_cue in screen:
        duplicate = any(
            ranges_overlap(screen_cue, speech_cue)
            and similar_text(screen_cue.text, speech_cue.text) >= 0.70
            for speech_cue in speech
        )
        if not duplicate:
            unique.append(screen_cue)
    return unique


def ranges_overlap(first: Cue, second: Cue) -> bool:
    return first.start < second.end and second.start < first.end


def clean_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value).strip()


def normalize_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", value).lower()


def similar_text(first: str, second: str) -> float:
    left, right = normalize_text(first), normalize_text(second)
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right).ratio()
