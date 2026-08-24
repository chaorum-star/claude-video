from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import Analysis
from .utils import format_timestamp


TIMELY_WORDS = ("요즘", "대세", "지금", "최근", "새로운", "트렌드", "올해", "현재")
CTA_WORDS = ("팔로우", "저장", "댓글", "프로필", "링크", "확인", "구매", "신청", "보여주세요", "해보세요")
QUESTION_WORDS = ("왜", "어떻게", "비결", "이유", "아시나요", "궁금")
CONTRAST_WORDS = ("하지만", "그런데", "아니", "보다", "오히려", "그럼에도")
STOP_WORDS = {
    "이분들", "사장님", "정말", "이렇게", "것", "거", "하는", "있는", "영상", "콘텐츠",
    "사람들", "같아요", "그리고", "그래서", "입니다", "있습니다", "됩니다", "모습",
}


def load_reference_metadata(work_dir: Path, source: str, title: str) -> dict[str, Any]:
    path = work_dir / "reference-metadata.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            pass
    return {"source": source, "title": title, "description": "", "comments": []}


def analyze_planning(source: str, metadata: dict[str, Any], analysis: Analysis) -> dict[str, Any]:
    description = str(metadata.get("description") or "").strip()
    transcript = " ".join(cue.text for cue in analysis.speech).strip()
    combined = f"{transcript}\n{description}".strip()
    hook_text = " ".join(cue.text for cue in analysis.speech if cue.start < 3.0).strip()
    hook_techniques: list[str] = []
    if any(word in hook_text for word in TIMELY_WORDS):
        hook_techniques.append("현재 유행을 지목하는 시의성 후킹")
    if analysis.overlay_events and analysis.overlay_events[0].start < 3.2:
        hook_techniques.append("말 직후 실제 사례를 화면에 제시하는 시각적 증거")
    if any(word in hook_text for word in QUESTION_WORDS) or "?" in hook_text:
        hook_techniques.append("답을 뒤로 미루는 질문형 오픈 루프")
    if not hook_techniques:
        hook_techniques.append("결론을 먼저 제시하는 선언형 후킹")

    timely_hits = sorted({word for word in TIMELY_WORDS if word in combined})
    upload_date = parse_upload_date(metadata.get("upload_date") or metadata.get("timestamp"))
    age_days = (date.today() - upload_date).days if upload_date else None
    timeliness_score = min(5, 2 + len(timely_hits) + (1 if age_days is not None and age_days <= 14 else 0))

    retention: list[dict[str, Any]] = []
    if analysis.overlay_events:
        retention.append({
            "time": format_timestamp(min(event.start for event in analysis.overlay_events)),
            "device": "연속 사례 제시",
            "evidence": f"{len(analysis.overlay_events)}개의 카드가 순차 등장해 추상적인 주장에 즉시 근거를 붙입니다.",
        })
    questions = [cue for cue in analysis.speech if "?" in cue.text or any(word in cue.text for word in QUESTION_WORDS)]
    for cue in questions[:2]:
        retention.append({"time": format_timestamp(cue.start), "device": "오픈 루프", "evidence": cue.text})
    if analysis.caption_events:
        retention.append({
            "time": format_timestamp(analysis.caption_events[0].start),
            "device": "핵심 결론 시각 강조",
            "evidence": f"결론 단어를 {len(analysis.caption_events)}차례 색·크기 변화와 펀치 줌으로 다시 깨웁니다.",
        })
    contrast_cues = [cue for cue in analysis.speech if any(word in cue.text for word in CONTRAST_WORDS)]
    if contrast_cues:
        retention.append({"time": format_timestamp(contrast_cues[0].start), "device": "상식 반전", "evidence": contrast_cues[0].text})
    retention.append({
        "time": "전체",
        "device": "빠른 정보 갱신",
        "evidence": f"약 {analysis.duration:.1f}초 동안 하드컷 {len(analysis.scenes)}회, 화면 움직임 {len(analysis.motion_events)}회를 사용합니다.",
    })

    cta_sentences = find_sentences(description or transcript, CTA_WORDS)
    cta_types = []
    for word, label in (("댓글", "댓글 유도"), ("저장", "저장 유도"), ("팔로우", "팔로우 유도"),
                        ("프로필", "프로필 이동"), ("링크", "링크 이동"), ("책", "상품/콘텐츠 전환"),
                        ("보여주세요", "행동 촉구"), ("해보세요", "행동 촉구")):
        if word in description or word in transcript:
            cta_types.append(label)
    publication_purposes = infer_publication_purposes(cta_types)

    comments = [item for item in metadata.get("comments", []) if isinstance(item, dict)]
    loved_points = infer_loved_points(comments, combined)
    top_keywords = content_keywords(combined)
    engagement = {
        "likes": integer_or_none(metadata.get("like_count")),
        "comments": integer_or_none(metadata.get("comment_count")),
        "views": integer_or_none(metadata.get("view_count")),
        "analyzed_comments": len(comments),
    }

    return {
        "version": 1,
        "source": source,
        "creator": metadata.get("uploader") or metadata.get("channel") or "판별 불가",
        "topic": topic_summary(description, transcript, top_keywords),
        "keywords": top_keywords,
        "engagement": engagement,
        "timeliness": {
            "score": timeliness_score,
            "signals": timely_hits,
            "upload_date": upload_date.isoformat() if upload_date else None,
            "age_days": age_days,
            "insight": "현재 화제어와 업로드 시점을 결합한 주제라 즉시성이 높습니다." if timeliness_score >= 4 else "현재성 신호는 보통이며 타깃의 지속 문제 해결형 주제에 가깝습니다.",
        },
        "hook": {
            "score": min(5, 2 + len(hook_techniques)),
            "first_3_seconds": hook_text,
            "techniques": hook_techniques,
            "categories": hook_categories(hook_techniques),
            "insight": "첫 문장에서 유행을 선언하고 곧바로 구체 사례를 보여줘 '누구 이야기인지'를 빠르게 이해시킵니다.",
        },
        "retention": retention,
        "cta": {
            "types": sorted(set(cta_types)),
            "purposes": publication_purposes,
            "evidence": cta_sentences[:4],
            "insight": "정보·공감으로 신뢰를 만든 뒤 프로필/상품으로 연결하는 후반 전환형 CTA입니다." if cta_types else "명시적인 전환 CTA가 약해 저장·팔로우·프로필 이동 중 하나를 보강할 수 있습니다.",
        },
        "loved_points": loved_points,
        "reusable_formula": [
            "0~3초: 지금 뜨는 현상 또는 타깃의 변화를 한 문장으로 선언",
            "2~6초: 실제 인물·사례·수치 3~4개를 빠르게 제시",
            "6~15초: '왜 그런가' 질문으로 결론을 지연",
            "중반: 핵심 가치 한 단어를 색·크기·줌·효과음으로 강조",
            "후반: 시청자가 자기 상황에 적용할 행동을 제안",
            "마지막: 저장·팔로우·프로필 이동 중 목적에 맞는 CTA 하나로 마감",
        ],
        "limitations": [
            "반응 지표는 분석 시점의 공개 수치이며 이후 바뀔 수 있습니다.",
            "댓글에서 반복된 표현은 사랑받은 이유의 근거지만 인과관계를 확정하지는 않습니다.",
        ],
    }


def write_planning_report(path: Path, planning: dict[str, Any]) -> Path:
    hook = planning.get("hook", {})
    timely = planning.get("timeliness", {})
    cta = planning.get("cta", {})
    engagement = planning.get("engagement", {})
    retention = "\n".join(
        f"- `{item.get('time')}` **{item.get('device')}** — {item.get('evidence')}"
        for item in planning.get("retention", [])
    ) or "- 판별된 장치 없음"
    loved = "\n".join(
        f"- **{item.get('point')}** — {item.get('insight')}\n  - 근거: " + " / ".join(item.get("evidence", []))
        for item in planning.get("loved_points", [])
    ) or "- 공개 반응 근거 부족"
    formula = "\n".join(f"{index}. {item}" for index, item in enumerate(planning.get("reusable_formula", []), 1))
    text = f"""# 레퍼런스 기획 분석

## 한눈에 보기

- 주제: **{planning.get('topic', '')}**
- 작성자: `{planning.get('creator', '')}`
- 공개 반응: 좋아요 `{engagement.get('likes')}`, 댓글 `{engagement.get('comments')}`, 분석 댓글 `{engagement.get('analyzed_comments')}`개
- 핵심 키워드: {', '.join(planning.get('keywords', []))}

## 주제 시의성 · {timely.get('score', 0)}/5

{timely.get('insight', '')}

- 시의성 신호: {', '.join(timely.get('signals', [])) or '명시적 신호 없음'}
- 업로드일: {timely.get('upload_date') or '판별 불가'}

## 3초 후킹 · {hook.get('score', 0)}/5

> {hook.get('first_3_seconds', '')}

{hook.get('insight', '')}

""" + "\n".join(f"- {item}" for item in hook.get("techniques", [])) + f"""

## 이탈방지 장치

{retention}

## CTA

- 유형: {', '.join(cta.get('types', [])) or '명시적 CTA 약함'}
- 발행 목적: {', '.join(cta.get('purposes', [])) or '인지·신뢰'}
- 분석: {cta.get('insight', '')}
""" + "\n".join(f"- 근거: {item}" for item in cta.get("evidence", [])) + f"""

## 고객에게 사랑받은 포인트

{loved}

## 재사용 가능한 기획 공식

{formula}

## 해석 한계

""" + "\n".join(f"- {item}" for item in planning.get("limitations", [])) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def archive_planning(
    output_root: Path,
    work_dir: Path,
    planning: dict[str, Any],
    capcut_project: str | None,
    obsidian: dict[str, Any] | None = None,
) -> Path:
    archive_dir = output_root / "기획분석_아카이브"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / "index.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        if not isinstance(records, list):
            records = []
    except (OSError, ValueError):
        records = []
    previous = next((item for item in records if item.get("source") == planning.get("source")), {})
    record = {
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "source": planning.get("source"),
        "creator": planning.get("creator"),
        "topic": planning.get("topic"),
        "keywords": planning.get("keywords", []),
        "hook_score": planning.get("hook", {}).get("score"),
        "timeliness_score": planning.get("timeliness", {}).get("score"),
        "timeliness_signals": planning.get("timeliness", {}).get("signals", []),
        "hook_categories": planning.get("hook", {}).get("categories", []),
        "publication_purposes": planning.get("cta", {}).get("purposes", []),
        "insights": [
            item.get("point") for item in planning.get("loved_points", [])
            if isinstance(item, dict) and item.get("point")
        ],
        "engagement": planning.get("engagement", {}),
        "loved_points": planning.get("loved_points", []),
        "work_dir": str(work_dir),
        "report_path": str(work_dir / "기획분석.md"),
        "capcut_project": capcut_project,
        "obsidian_source_note": (obsidian or {}).get("source_note_path"),
        "obsidian_insight_card": (obsidian or {}).get("insight_card_path"),
        "obsidian_content_angle": (obsidian or {}).get("content_angle_path"),
        "publications": previous.get("publications", []),
        "published_count": previous.get("published_count", 0),
        "latest_publication": previous.get("latest_publication"),
        "performance_updated_at": previous.get("performance_updated_at"),
        "template_run_count": previous.get("template_run_count", 0),
        "last_template_used_at": previous.get("last_template_used_at"),
        "last_template_project": previous.get("last_template_project"),
        "last_template_work_dir": previous.get("last_template_work_dir"),
    }
    records = [item for item in records if item.get("source") != record["source"]]
    records.append(record)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_upload_date(value: Any) -> date | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).date()
        except (OSError, OverflowError, ValueError):
            return None
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def find_sentences(text: str, keywords: tuple[str, ...]) -> list[str]:
    parts = [item.strip() for item in re.split(r"[\n.!?]+", text) if item.strip()]
    return [item[:180] for item in parts if any(keyword in item for keyword in keywords)]


def infer_loved_points(comments: list[dict[str, Any]], content: str) -> list[dict[str, Any]]:
    categories = {
        "진정성과 신뢰": ("진정성", "진심", "신뢰", "솔직"),
        "내 상황을 대변하는 공감": ("공감", "맞아요", "고민", "마음"),
        "구체적인 실제 사례": ("사장님", "대표님", "공장", "유명"),
        "도움이 되는 관점": ("도움", "좋은 내용", "감사", "배워"),
        "재미와 친근함": ("재미", "웃", "귀엽", "잘미"),
    }
    output = []
    texts = [str(item.get("text") or "").strip() for item in comments]
    for point, words in categories.items():
        evidence = [text[:120] for text in texts if any(word in text for word in words)][:3]
        if evidence or any(word in content for word in words):
            output.append({
                "point": point,
                "insight": f"공개 댓글과 본문에서 {', '.join(words[:2])} 관련 표현이 반복됩니다.",
                "evidence": evidence or ["본문에서 해당 가치가 반복 강조됨"],
            })
    return output[:4]


def hook_categories(techniques: list[str]) -> list[str]:
    categories: list[str] = []
    for technique in techniques:
        if "시의성" in technique or "유행" in technique:
            categories.append("트렌드 선언형")
        if "시각" in technique or "증거" in technique or "사례" in technique:
            categories.append("시각 증거형")
        if "질문" in technique or "오픈 루프" in technique:
            categories.append("질문·오픈루프형")
        if "결론" in technique or "선언" in technique:
            categories.append("결론 선공개형")
    return list(dict.fromkeys(categories)) or ["기타 선언형"]


def infer_publication_purposes(cta_types: list[str]) -> list[str]:
    purposes: list[str] = []
    for cta_type in cta_types:
        if cta_type in {"프로필 이동", "링크 이동", "상품/콘텐츠 전환"}:
            purposes.append("전환·판매")
        elif cta_type == "댓글 유도":
            purposes.append("참여·댓글")
        elif cta_type == "저장 유도":
            purposes.append("저장·재방문")
        elif cta_type == "팔로우 유도":
            purposes.append("팔로워 성장")
        elif cta_type == "행동 촉구":
            purposes.append("행동 실행")
    return list(dict.fromkeys(purposes)) or ["인지·신뢰"]


def content_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[가-힣A-Za-z]{2,}", text)
    normalized = [token for token in tokens if token not in STOP_WORDS and not token.endswith(("해요", "해도", "하게", "해서", "합니다"))]
    return [word for word, _count in Counter(normalized).most_common(8)]


def topic_summary(description: str, transcript: str, keywords: list[str]) -> str:
    lines = []
    for raw in description.splitlines():
        line = raw.strip()
        if len(line) < 4 or line.startswith("@") or line in lines:
            continue
        lines.append(line)
        if len(lines) == 2:
            break
    if lines:
        return " ".join(lines)[:120]
    if transcript:
        return transcript[:120]
    return " · ".join(keywords) or "주제 판별 불가"


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
