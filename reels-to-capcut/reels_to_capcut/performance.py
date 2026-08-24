from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .obsidian_archive import discover_obsidian_vault


PERFORMANCE_START = "<!-- reels-to-capcut:performance:start -->"
PERFORMANCE_END = "<!-- reels-to-capcut:performance:end -->"
METRIC_KEYS = ("views", "likes", "comments", "saves", "shares", "follows", "conversions")
ARCHIVE_LOCK = threading.Lock()


def save_publication(
    output_root: Path,
    template_work_dir: Path | str,
    payload: dict[str, Any],
    *,
    vault: Path | str | None = None,
) -> dict[str, Any]:
    target = validated_template_dir(output_root, template_work_dir)
    published_url = str(payload.get("published_url") or "").strip()
    parsed = urlparse(published_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("발행한 콘텐츠의 전체 URL을 입력해주세요.")
    published_at = str(payload.get("published_at") or date.today().isoformat()).strip()
    try:
        date.fromisoformat(published_at)
    except ValueError as exc:
        raise ValueError("발행일을 YYYY-MM-DD 형식으로 입력해주세요.") from exc

    raw_metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    metrics = {key: nonnegative_integer(raw_metrics.get(key), key) for key in METRIC_KEYS}
    publication = {
        "id": uuid.uuid4().hex,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "published_at": published_at,
        "published_url": published_url,
        "title": clean_text(payload.get("title"), 160),
        "metrics": metrics,
        "rates": calculate_rates(metrics),
        "notes": clean_text(payload.get("notes"), 1000, preserve_lines=True),
    }

    with ARCHIVE_LOCK:
        path, records = load_archive(output_root)
        record = find_record(records, target)
        publications = [
            item for item in record.get("publications", [])
            if isinstance(item, dict) and item.get("published_url") != published_url
        ]
        publications.append(publication)
        publications.sort(key=lambda item: (item.get("published_at", ""), item.get("recorded_at", "")))
        record["publications"] = publications
        record["published_count"] = len(publications)
        record["latest_publication"] = publications[-1]
        record["performance_updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_archive(path, records)

    obsidian = sync_publications_to_obsidian(
        record.get("obsidian_content_angle"), publications, vault=vault
    )
    return {"publication": publication, "published_count": len(publications), "obsidian": obsidian}


def record_template_use(
    output_root: Path,
    template_work_dir: Path | str,
    *,
    capcut_project: str | None,
    result_work_dir: Path | str,
) -> None:
    target = validated_template_dir(output_root, template_work_dir)
    with ARCHIVE_LOCK:
        path, records = load_archive(output_root)
        record = find_record(records, target)
        record["template_run_count"] = int(record.get("template_run_count") or 0) + 1
        record["last_template_used_at"] = datetime.now().isoformat(timespec="seconds")
        record["last_template_project"] = capcut_project
        record["last_template_work_dir"] = str(result_work_dir)
        write_archive(path, records)


def calculate_rates(metrics: dict[str, int | None]) -> dict[str, float]:
    views = metrics.get("views") or 0
    if views <= 0:
        return {}
    engagement = sum(metrics.get(key) or 0 for key in ("likes", "comments", "saves", "shares"))
    rates = {"engagement_rate": round(engagement / views * 100, 2)}
    for key, label in (("saves", "save_rate"), ("shares", "share_rate"), ("conversions", "conversion_rate")):
        if metrics.get(key) is not None:
            rates[label] = round((metrics.get(key) or 0) / views * 100, 2)
    return rates


def sync_publications_to_obsidian(
    note_path: Any,
    publications: list[dict[str, Any]],
    *,
    vault: Path | str | None = None,
) -> dict[str, Any]:
    vault_path = discover_obsidian_vault(vault)
    if vault_path is None or not note_path:
        return {"enabled": False, "message": "연결된 콘텐츠각 노트가 없어 앱 보관함에만 저장했습니다."}
    root = vault_path.expanduser().resolve()
    target = Path(str(note_path)).expanduser().resolve()
    if target != root and root not in target.parents:
        return {"enabled": False, "message": "옵시디언 보관함 밖의 노트는 수정하지 않았습니다."}
    if not target.is_file():
        return {"enabled": False, "message": "연결된 콘텐츠각 노트를 찾지 못해 앱 보관함에만 저장했습니다."}

    try:
        text = target.read_text(encoding="utf-8")
        block = render_performance_block(publications)
        if PERFORMANCE_START in text and PERFORMANCE_END in text:
            text = re.sub(
                re.escape(PERFORMANCE_START) + r".*?" + re.escape(PERFORMANCE_END),
                block,
                text,
                flags=re.DOTALL,
            )
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
        text = re.sub(r"(?m)^status: idea$", "status: published", text, count=1)
        target.write_text(text, encoding="utf-8")
        return {"enabled": True, "note_path": str(target), "message": "콘텐츠각 노트에 발행 성과를 연결했습니다."}
    except OSError as exc:
        return {"enabled": False, "message": f"옵시디언 성과 기록 실패: {exc}"}


def render_performance_block(publications: list[dict[str, Any]]) -> str:
    sections = []
    labels = {
        "views": "조회", "likes": "좋아요", "comments": "댓글", "saves": "저장",
        "shares": "공유", "follows": "팔로우", "conversions": "전환",
    }
    for item in sorted(publications, key=lambda value: value.get("published_at", ""), reverse=True):
        metrics = item.get("metrics", {})
        metric_text = " · ".join(
            f"{labels[key]} **{metrics[key]:,}**" for key in METRIC_KEYS if metrics.get(key) is not None
        ) or "입력된 수치 없음"
        rates = item.get("rates", {})
        rate_text = " · ".join(
            f"{label} **{rates[key]:.2f}%**"
            for key, label in (("engagement_rate", "참여율"), ("save_rate", "저장률"), ("share_rate", "공유율"), ("conversion_rate", "전환율"))
            if key in rates
        )
        notes = str(item.get("notes") or "").strip()
        title = str(item.get("title") or "발행 콘텐츠").strip()
        lines = [
            f"### {item.get('published_at', '')} · {title}",
            "",
            f"- 링크: [{item.get('published_url')}]({item.get('published_url')})",
            f"- 성과: {metric_text}",
        ]
        if rate_text:
            lines.append(f"- 비율: {rate_text}")
        if notes:
            lines.extend(["- 회고:", *[f"  - {line}" for line in notes.splitlines() if line.strip()]])
        sections.append("\n".join(lines))
    return f"{PERFORMANCE_START}\n## 발행 성과 기록\n\n" + "\n\n".join(sections) + f"\n{PERFORMANCE_END}"


def validated_template_dir(output_root: Path, value: Path | str) -> Path:
    root = output_root.expanduser().resolve()
    target = Path(value).expanduser().resolve()
    if target == root or root not in target.parents:
        raise ValueError("결과 보관함 밖의 템플릿은 사용할 수 없습니다.")
    return target


def load_archive(output_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    path = output_root / "기획분석_아카이브" / "index.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, ValueError) as exc:
        raise ValueError("기획 분석 보관함을 읽지 못했습니다.") from exc
    if not isinstance(records, list):
        raise ValueError("기획 분석 보관함 형식이 올바르지 않습니다.")
    return path, records


def find_record(records: list[dict[str, Any]], target: Path) -> dict[str, Any]:
    for record in records:
        try:
            if Path(str(record.get("work_dir", ""))).expanduser().resolve() == target:
                return record
        except (OSError, RuntimeError):
            continue
    raise ValueError("선택한 레퍼런스를 기획 보관함에서 찾지 못했습니다.")


def write_archive(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def nonnegative_integer(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 지표는 0 이상의 숫자로 입력해주세요.") from exc
    if number < 0:
        raise ValueError(f"{field} 지표는 0 이상의 숫자로 입력해주세요.")
    return number


def clean_text(value: Any, limit: int, *, preserve_lines: bool = False) -> str:
    text = str(value or "").replace(PERFORMANCE_START, "").replace(PERFORMANCE_END, "").strip()
    if not preserve_lines:
        text = re.sub(r"\s+", " ", text)
    return text[:limit]
