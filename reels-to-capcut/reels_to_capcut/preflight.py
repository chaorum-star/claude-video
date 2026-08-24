from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from .capcut import DEFAULT_DRAFT_DIR
from .transcribe import transcriber_status


def status() -> dict[str, object]:
    capcut_app = next(
        (str(path) for path in (Path("/Applications/CapCut 2.app"), Path("/Applications/CapCut.app")) if path.exists()),
        None,
    )
    whisper = transcriber_status()
    tools = {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "yt_dlp": shutil.which("yt-dlp"),
        "tesseract": shutil.which("tesseract"),
        "xcrun": shutil.which("xcrun"),
    }
    return {
        "ready": bool(tools["ffmpeg"] and tools["ffprobe"] and tools["yt_dlp"] and capcut_app),
        "tools": tools,
        "whisper": whisper,
        "ocr_available": bool(tools["tesseract"] or tools["xcrun"] or importlib.util.find_spec("Vision")),
        "capcut_app": capcut_app,
        "draft_dir": str(DEFAULT_DRAFT_DIR),
        "draft_dir_exists": DEFAULT_DRAFT_DIR.is_dir(),
    }
