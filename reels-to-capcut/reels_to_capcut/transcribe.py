from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import Cue, WordTiming
from .utils import ToolError, run


def transcriber_status() -> dict[str, object]:
    faster = importlib.util.find_spec("faster_whisper") is not None
    whisper_cli = shutil.which("whisper-cli") or shutil.which("whisper")
    return {
        "available": bool(faster or whisper_cli),
        "faster_whisper": faster,
        "cli": whisper_cli,
        "model": os.getenv("REELS_CAPCUT_WHISPER_MODEL", "large-v3"),
    }


def transcribe(video_path: Path, work_dir: Path) -> tuple[list[Cue], str | None]:
    audio_path = work_dir / "speech.wav"
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", str(audio_path),
    ])
    if importlib.util.find_spec("faster_whisper") is not None:
        return _faster_whisper(audio_path), None
    cli = shutil.which("whisper-cli")
    model_path = os.getenv("REELS_CAPCUT_WHISPER_CPP_MODEL")
    if cli and model_path and Path(model_path).is_file():
        return _whisper_cpp(cli, Path(model_path), audio_path, work_dir), None
    return [], (
        "로컬 Whisper가 없어 말 자막을 만들지 못했습니다. "
        "설치.command를 실행한 뒤 다시 처리하면 large-v3 자막을 생성합니다."
    )


def _faster_whisper(audio_path: Path) -> list[Cue]:
    from faster_whisper import WhisperModel  # type: ignore

    model_name = os.getenv("REELS_CAPCUT_WHISPER_MODEL", "large-v3")
    compute_type = os.getenv("REELS_CAPCUT_WHISPER_COMPUTE", "int8")
    try:
        model = WhisperModel(model_name, device="auto", compute_type=compute_type)
        segments, _ = model.transcribe(
            str(audio_path),
            language=os.getenv("REELS_CAPCUT_LANGUAGE", "ko"),
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
        )
        cues: list[Cue] = []
        for segment in segments:
            cues.extend(segment_to_cues(segment))
        return cues
    except Exception as exc:
        raise ToolError(f"Whisper 자막 생성 실패: {exc}") from exc


def segment_to_cues(segment: Any, *, max_chars: int = 18, max_duration: float = 2.2) -> list[Cue]:
    """Turn Whisper's sentence-sized segments into effect-caption-sized phrases."""
    words = [word for word in (getattr(segment, "words", None) or []) if getattr(word, "word", "").strip()]
    if not words:
        text = str(getattr(segment, "text", "")).strip()
        return [Cue(float(segment.start), float(segment.end), text, "speech")] if text else []

    cues: list[Cue] = []
    group: list[Any] = []

    def flush() -> None:
        if not group:
            return
        text = "".join(str(word.word) for word in group).strip()
        if text:
            cues.append(
                Cue(
                    float(group[0].start),
                    float(group[-1].end),
                    text,
                    "speech",
                    [
                        WordTiming(float(word.start), float(word.end), str(word.word).strip())
                        for word in group
                    ],
                )
            )
        group.clear()

    for word in words:
        if group and float(word.start) - float(group[-1].end) >= 0.55:
            flush()
        if group:
            candidate_text = "".join(str(item.word) for item in [*group, word]).strip()
            candidate_duration = float(word.end) - float(group[0].start)
            if len(candidate_text.replace(" ", "")) > max_chars or candidate_duration > max_duration:
                flush()
        group.append(word)
        text = "".join(str(item.word) for item in group).strip()
        duration = float(group[-1].end) - float(group[0].start)
        sentence_end = text.endswith((".", "!", "?", "。", "！", "？"))
        if len(text.replace(" ", "")) >= max_chars or duration >= max_duration or (sentence_end and duration >= 0.8):
            flush()
    flush()
    return cues


def _whisper_cpp(cli: str, model_path: Path, audio_path: Path, work_dir: Path) -> list[Cue]:
    prefix = work_dir / "whisper"
    completed = subprocess.run(
        [cli, "-m", str(model_path), "-f", str(audio_path), "-l", "ko", "-oj", "-of", str(prefix)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if completed.returncode != 0:
        raise ToolError((completed.stderr or completed.stdout or "whisper-cli 실패")[-2000:])
    payload = json.loads((work_dir / "whisper.json").read_text(encoding="utf-8"))
    cues: list[Cue] = []
    for item in payload.get("transcription", []):
        offsets = item.get("offsets", {})
        text = str(item.get("text", "")).strip()
        if text:
            cues.append(Cue(offsets.get("from", 0) / 1000, offsets.get("to", 0) / 1000, text))
    return cues
