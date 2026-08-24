from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


class ToolError(RuntimeError):
    pass


def run(command: list[str], *, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "실행 실패").strip()
        raise ToolError(detail[-2000:])
    return completed


def safe_slug(value: str, fallback: str = "reel") -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", value).strip("._-")
    return cleaned[:70] or fallback


def make_job_dir(root: Path, title: str = "reel") -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_slug(title)
    candidate = day_dir / stem
    index = 2
    while candidate.exists():
        candidate = day_dir / f"{stem}_{index:02d}"
        index += 1
    candidate.mkdir()
    return candidate


def is_instagram_url(source: str) -> bool:
    parsed = urlparse(source.strip())
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host == "instagram.com" or host.endswith(".instagram.com")
    )


def is_instagram_reel_url(source: str) -> bool:
    if not is_instagram_url(source):
        return False
    path = urlparse(source.strip()).path
    return re.match(r"^/(?:reel|p)/[^/]+/?$", path) is not None


def probe_video(path: Path) -> tuple[float, int, int]:
    completed = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ])
    payload = json.loads(completed.stdout)
    stream = payload["streams"][0]
    return float(payload["format"]["duration"]), int(stream["width"]), int(stream["height"])


def format_timestamp(seconds: float, *, srt: bool = False) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"
