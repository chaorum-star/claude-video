from __future__ import annotations

import json
import shutil
from pathlib import Path

from .utils import ToolError, is_instagram_reel_url, run, safe_slug


def acquire_video(source: str, work_dir: Path) -> tuple[Path, str]:
    local = Path(source).expanduser()
    if local.is_file():
        destination = work_dir / f"source{local.suffix.lower()}"
        shutil.copy2(local, destination)
        return destination, local.stem
    if not is_instagram_reel_url(source):
        raise ToolError(
            "프로필 주소가 아니라 개별 릴스 주소를 넣어주세요. "
            "올바른 예: https://www.instagram.com/reel/릴스번호/"
        )
    return _download_reel(source, work_dir)


def _download_reel(source: str, work_dir: Path) -> tuple[Path, str]:
    output = str(work_dir / "source.%(ext)s")
    base = [
        "yt-dlp", "--no-playlist", "--restrict-filenames",
        "--write-info-json", "--no-write-playlist-metafiles",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
        "-o", output,
        "--print", "after_move:filepath",
        "--print", "title",
        source,
    ]
    first = run(base, timeout=1200, check=False)
    completed = first
    used_cookie_fallback = False
    if first.returncode != 0:
        completed = run(base[:-1] + ["--cookies-from-browser", "chrome", source], timeout=1200, check=False)
        used_cookie_fallback = True
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "다운로드 실패").strip()
        hint = (
            "\nChrome에서 instagram.com에 로그인한 뒤 링크를 다시 넣어주세요. "
            "쿠키 재시도까지 실패했습니다."
            if used_cookie_fallback else ""
        )
        raise ToolError(detail[-1500:] + hint)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    candidates = [Path(line) for line in lines if Path(line).is_file()]
    if not candidates:
        candidates = sorted(work_dir.glob("source.*"), key=lambda path: path.stat().st_size, reverse=True)
    if not candidates:
        raise ToolError("yt-dlp 실행은 끝났지만 내려받은 영상 파일을 찾지 못했습니다.")
    title = next((line for line in lines if line != str(candidates[0])), "instagram_reel")
    sanitize_reference_metadata(work_dir, source, title)
    return candidates[0], safe_slug(title, "instagram_reel")


def sanitize_reference_metadata(work_dir: Path, source: str, title: str) -> Path:
    """yt-dlp 원본 정보에서 기획 분석에 필요한 공개 항목만 남긴다."""
    info_files = sorted(work_dir.glob("source*.info.json"))
    payload: dict = {}
    if info_files:
        try:
            payload = json.loads(info_files[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
    comments = []
    for item in payload.get("comments", []) or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        comments.append({
            "text": str(item.get("text"))[:500],
            "like_count": item.get("like_count"),
            "timestamp": item.get("timestamp"),
        })
    sanitized = {
        "source": source,
        "webpage_url": payload.get("webpage_url") or source,
        "id": payload.get("id"),
        "title": payload.get("title") or title,
        "description": payload.get("description") or "",
        "channel": payload.get("channel"),
        "uploader": payload.get("uploader"),
        "timestamp": payload.get("timestamp"),
        "upload_date": payload.get("upload_date"),
        "like_count": payload.get("like_count"),
        "comment_count": payload.get("comment_count"),
        "view_count": payload.get("view_count"),
        "comments": comments[:100],
    }
    path = work_dir / "reference-metadata.json"
    path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")
    for info_file in info_files:
        info_file.unlink(missing_ok=True)
    return path
