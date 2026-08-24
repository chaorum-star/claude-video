from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Analysis


GENERATOR = "reels-to-capcut"
START_MARKER = "<!-- reels-to-capcut:generated:start -->"
END_MARKER = "<!-- reels-to-capcut:generated:end -->"
HOME_START = "<!-- reels-to-capcut:home:start -->"
HOME_END = "<!-- reels-to-capcut:home:end -->"
SOURCE_FOLDER = Path("10_SOURCE NOTES/Instagram Reels")
INSIGHT_FOLDER = Path("20_INSIGHT CARDS/Instagram Reels")
ANGLE_FOLDER = Path("30_CONTENT ANGLES/Instagram Reels")
HUB_PATH = Path("10_SOURCE NOTES/릴스 콘텐츠 인사이트 허브.md")
REGISTRY_PATH = Path(".reels-to-capcut/index.json")


@dataclass
class ObsidianArchiveResult:
    enabled: bool
    vault_path: str | None = None
    source_note_path: str | None = None
    insight_card_path: str | None = None
    content_angle_path: str | None = None
    hub_path: str | None = None
    source_note_uri: str | None = None
    hub_uri: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_obsidian_vault(explicit: Path | str | None = None) -> Path | None:
    """Find the user's Obsidian vault without requiring a project-specific setup step."""
    if os.getenv("REELS_OBSIDIAN_DISABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        return None

    configured = explicit or os.getenv("REELS_OBSIDIAN_VAULT")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if (candidate / ".obsidian").is_dir() else None

    candidates = [
        Path.home() / "Documents/Obsidian Vault",
        Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents",
    ]
    for candidate in candidates:
        if (candidate / ".obsidian").is_dir():
            return candidate
        if candidate.is_dir():
            children = sorted(
                (child for child in candidate.iterdir() if (child / ".obsidian").is_dir()),
                key=lambda item: item.name,
            )
            if len(children) == 1:
                return children[0]
    return None


def obsidian_status() -> dict[str, Any]:
    vault = discover_obsidian_vault()
    disabled = os.getenv("REELS_OBSIDIAN_DISABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "available": vault is not None,
        "disabled": disabled,
        "vault_path": str(vault) if vault else None,
        "hub_path": str(vault / HUB_PATH) if vault else None,
        "hub_uri": obsidian_uri(vault, HUB_PATH) if vault else None,
        "message": "옵시디언 세컨드 브레인 연결됨" if vault else (
            "옵시디언 적재 비활성화" if disabled else "옵시디언 보관함을 찾지 못함"
        ),
    }


def archive_to_obsidian(
    planning: dict[str, Any],
    analysis: Analysis,
    work_dir: Path,
    capcut_project: str | None,
    *,
    vault: Path | str | None = None,
) -> ObsidianArchiveResult:
    vault_path = discover_obsidian_vault(vault)
    if vault_path is None:
        configured = vault or os.getenv("REELS_OBSIDIAN_VAULT")
        error = "지정한 옵시디언 보관함의 .obsidian 폴더를 찾지 못했습니다." if configured else None
        return ObsidianArchiveResult(enabled=False, error=error)

    try:
        source_id = source_identifier(str(planning.get("source") or ""))
        published = str(planning.get("timeliness", {}).get("upload_date") or date.today().isoformat())
        topic = clean_text(planning.get("topic") or "주제 판별 불가", 100)
        short_topic = safe_filename(topic, 66)
        loved = planning.get("loved_points", [])
        primary_insight = clean_text(
            loved[0].get("point") if loved and isinstance(loved[0], dict) else "릴스 구조 인사이트",
            46,
        )

        source_stem = f"{published} - {short_topic} - {source_id}"
        insight_stem = f"{published} - {safe_filename(primary_insight, 46)} - {source_id}"
        angle_stem = f"{published} - {safe_filename(topic, 52)} 적용안 - {source_id}"
        source_rel = SOURCE_FOLDER / f"{source_stem}.md"
        insight_rel = INSIGHT_FOLDER / f"{insight_stem}.md"
        angle_rel = ANGLE_FOLDER / f"{angle_stem}.md"

        for folder in (SOURCE_FOLDER, INSIGHT_FOLDER, ANGLE_FOLDER, REGISTRY_PATH.parent):
            (vault_path / folder).mkdir(parents=True, exist_ok=True)

        source_link = wiki_link(source_rel, topic)
        insight_link = wiki_link(insight_rel, primary_insight)
        angle_link = wiki_link(angle_rel, f"{topic} 적용안")
        hub_link = wiki_link(HUB_PATH, "릴스 콘텐츠 인사이트 허브")

        write_managed_note(
            vault_path / source_rel,
            source_frontmatter(planning, source_id, published, insight_rel, angle_rel, capcut_project),
            source_body(planning, analysis, work_dir, insight_link, angle_link, hub_link),
            "## 내 메모\n\n- 이 레퍼런스를 보고 내가 떠올린 것:\n- 우리 고객에게 바꿔 적용할 장면:\n- 그대로 쓰지 않고 다르게 만들 부분:\n",
        )
        write_managed_note(
            vault_path / insight_rel,
            insight_frontmatter(planning, source_id, published, source_rel, angle_rel),
            insight_body(planning, source_link, angle_link, hub_link),
            "## 내 해석\n\n- 이 인사이트가 특히 유효한 고객 장면:\n- 실제 콘텐츠에서 검증할 가설:\n",
        )
        write_managed_note(
            vault_path / angle_rel,
            angle_frontmatter(planning, source_id, published, source_rel, insight_rel),
            angle_body(planning, source_link, insight_link, hub_link),
            "## 제작 메모\n\n- 내 실제 주제:\n- 사용할 사례·수치·화면:\n- 발행 후 기록할 지표:\n",
        )

        record = {
            "source_id": source_id,
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "published": published,
            "source": planning.get("source"),
            "creator": planning.get("creator"),
            "topic": topic,
            "keywords": planning.get("keywords", []),
            "hook_score": planning.get("hook", {}).get("score"),
            "timeliness_score": planning.get("timeliness", {}).get("score"),
            "loved_points": [item.get("point") for item in loved if isinstance(item, dict)],
            "source_note": source_rel.as_posix(),
            "insight_card": insight_rel.as_posix(),
            "content_angle": angle_rel.as_posix(),
        }
        records = update_registry(vault_path / REGISTRY_PATH, record)
        write_managed_note(
            vault_path / HUB_PATH,
            hub_frontmatter(),
            hub_body(records),
            "## 내 큐레이션 메모\n\n- 이번 주에 확장할 주제:\n- 서로 연결해 볼 레퍼런스:\n",
        )
        connect_home(vault_path)

        return ObsidianArchiveResult(
            enabled=True,
            vault_path=str(vault_path),
            source_note_path=str(vault_path / source_rel),
            insight_card_path=str(vault_path / insight_rel),
            content_angle_path=str(vault_path / angle_rel),
            hub_path=str(vault_path / HUB_PATH),
            source_note_uri=obsidian_uri(vault_path, source_rel),
            hub_uri=obsidian_uri(vault_path, HUB_PATH),
        )
    except OSError as exc:
        return ObsidianArchiveResult(
            enabled=False,
            vault_path=str(vault_path),
            error=f"옵시디언 노트 저장 실패: {exc}",
        )


def source_frontmatter(
    planning: dict[str, Any],
    source_id: str,
    published: str,
    insight_rel: Path,
    angle_rel: Path,
    capcut_project: str | None,
) -> dict[str, Any]:
    engagement = planning.get("engagement", {})
    return {
        "type": "source-note",
        "status": "processed",
        "platform": "instagram",
        "source_id": source_id,
        "source_url": planning.get("source"),
        "creator": planning.get("creator"),
        "published": published,
        "captured": date.today().isoformat(),
        "topic": planning.get("topic"),
        "hook_score": planning.get("hook", {}).get("score"),
        "hook_categories": planning.get("hook", {}).get("categories", []),
        "timeliness_score": planning.get("timeliness", {}).get("score"),
        "publication_purposes": planning.get("cta", {}).get("purposes", []),
        "likes": engagement.get("likes"),
        "comments": engagement.get("comments"),
        "capcut_project": capcut_project,
        "insight_card": wiki_link(insight_rel),
        "content_angle": wiki_link(angle_rel),
        "generator": GENERATOR,
        "tags": ["source-note", "instagram-reels", "reference-analysis"],
    }


def insight_frontmatter(
    planning: dict[str, Any], source_id: str, published: str, source_rel: Path, angle_rel: Path
) -> dict[str, Any]:
    return {
        "type": "insight-card",
        "status": "distilled",
        "source_id": source_id,
        "published": published,
        "captured": date.today().isoformat(),
        "source_note": wiki_link(source_rel),
        "content_angle": wiki_link(angle_rel),
        "hook_score": planning.get("hook", {}).get("score"),
        "publication_purposes": planning.get("cta", {}).get("purposes", []),
        "generator": GENERATOR,
        "tags": ["insight-card", "content-strategy", "instagram-reels"],
    }


def angle_frontmatter(
    planning: dict[str, Any], source_id: str, published: str, source_rel: Path, insight_rel: Path
) -> dict[str, Any]:
    return {
        "type": "content-angle",
        "status": "idea",
        "source_id": source_id,
        "published": published,
        "captured": date.today().isoformat(),
        "source_note": wiki_link(source_rel),
        "insight_card": wiki_link(insight_rel),
        "generator": GENERATOR,
        "tags": ["content-angle", "idea-pipeline", "instagram-reels"],
    }


def hub_frontmatter() -> dict[str, Any]:
    return {
        "type": "moc",
        "status": "active",
        "updated": datetime.now().isoformat(timespec="seconds"),
        "generator": GENERATOR,
        "tags": ["moc", "content-strategy", "second-brain"],
    }


def source_body(
    planning: dict[str, Any],
    analysis: Analysis,
    work_dir: Path,
    insight_link: str,
    angle_link: str,
    hub_link: str,
) -> str:
    hook = planning.get("hook", {})
    timely = planning.get("timeliness", {})
    cta = planning.get("cta", {})
    engagement = planning.get("engagement", {})
    loved = planning.get("loved_points", [])
    retention = planning.get("retention", [])
    caption_matches = sorted({
        event.capcut_animation for event in analysis.caption_events if event.capcut_animation
    })
    motion_matches = sorted({
        event.capcut_animation for event in analysis.motion_events if event.capcut_animation
    })
    report_uri = (work_dir / "기획분석.md").resolve().as_uri()
    analysis_uri = (work_dir / "분석결과.json").resolve().as_uri()
    loved_text = "\n".join(
        f"- **{clean_text(item.get('point'))}** — {clean_text(item.get('insight'))}\n"
        + "\n".join(f"  - 근거: {clean_text(evidence, 180)}" for evidence in item.get("evidence", []))
        for item in loved if isinstance(item, dict)
    ) or "- 공개 댓글 근거가 부족해 본문 중심으로 해석함"
    retention_text = "\n".join(
        f"- `{clean_text(item.get('time'))}` **{clean_text(item.get('device'))}** — {clean_text(item.get('evidence'), 220)}"
        for item in retention if isinstance(item, dict)
    ) or "- 판별된 이탈방지 장치 없음"
    cta_evidence = "\n".join(f"- {clean_text(item, 200)}" for item in cta.get("evidence", [])) or "- 직접적인 문장 근거 없음"
    formula = "\n".join(
        f"{index}. {clean_text(item, 220)}" for index, item in enumerate(planning.get("reusable_formula", []), 1)
    )
    return f"""# {clean_text(planning.get('topic'))}

> [!summary] 한눈에 보는 레퍼런스
> **시의성 {timely.get('score', 0)}/5 · 3초 후킹 {hook.get('score', 0)}/5**
> {clean_text(hook.get('insight'), 260)}

## 원본 정보

- 출처: [{clean_text(planning.get('creator'))}]({planning.get('source')})
- 업로드일: `{timely.get('upload_date') or '판별 불가'}`
- 공개 반응: 좋아요 `{engagement.get('likes')}`, 댓글 `{engagement.get('comments')}`, 분석 댓글 `{engagement.get('analyzed_comments')}`개
- 키워드: {', '.join(clean_text(item, 30) for item in planning.get('keywords', []))}

## 왜 멈춰 보게 했나

### 첫 3초

> {clean_text(hook.get('first_3_seconds'), 300)}

""" + "\n".join(f"- {clean_text(item, 220)}" for item in hook.get("techniques", [])) + f"""

### 이탈방지 장치

{retention_text}

### CTA

- 유형: {', '.join(clean_text(item) for item in cta.get('types', [])) or '명시적 CTA 약함'}
- 발행 목적: {', '.join(clean_text(item) for item in cta.get('purposes', [])) or '인지·신뢰'}
- 해석: {clean_text(cta.get('insight'), 260)}
{cta_evidence}

## 고객에게 사랑받은 포인트

{loved_text}

## 편집 문법 · CapCut 재현

- 자막 위치/크기: `{analysis.caption_style.position}` / `{analysis.caption_style.size}`
- 자막 색/윤곽/배경: `{analysis.caption_style.color}` / `{analysis.caption_style.outline}` / `{analysis.caption_style.background}`
- 강조 자막 이벤트: `{len(analysis.caption_events)}`개 · CapCut 매칭 `{', '.join(caption_matches) or '없음'}`
- 영상 움직임: `{len(analysis.motion_events)}`개 · CapCut 매칭 `{', '.join(motion_matches) or '없음'}`
- 오버레이: `{len(analysis.overlay_events)}`개 · 효과음 슬롯: `{len(analysis.sound_events)}`개
- 상세 파일: [기획분석.md]({report_uri}) · [분석결과.json]({analysis_uri})

## 재사용 공식

{formula}

## 다음 노트

- 추출된 인사이트: {insight_link}
- 실제 발행 후보: {angle_link}
- 전체 지도: {hub_link}
"""


def insight_body(planning: dict[str, Any], source_link: str, angle_link: str, hub_link: str) -> str:
    loved = [item for item in planning.get("loved_points", []) if isinstance(item, dict)]
    primary = loved[0] if loved else {"point": "구체적 증거가 있는 짧은 구조", "insight": planning.get("hook", {}).get("insight", "")}
    emotions = ", ".join(clean_text(item.get("point")) for item in loved[:3]) or "신뢰, 호기심"
    return f"""# {clean_text(primary.get('point'))}

## 한 문장

> {clean_text(primary.get('insight'), 300)}

## 왜 작동했나

- 첫 3초에 `{clean_text(planning.get('hook', {}).get('first_3_seconds'), 220)}`로 관심 대상을 빠르게 특정했다.
- 말로만 주장하지 않고 사례·화면 변화·강조 자막을 연속 배치해 이해 비용을 낮췄다.
- 고객 반응에서 `{emotions}`가 반복되어 단순 조회보다 공감과 신뢰가 핵심 자산으로 보인다.

## 차오름에서 쓰는 법

- 릴스: 고객이 요즘 체감하는 변화를 먼저 선언하고 실제 고객 장면 3개를 바로 붙인다.
- 캐러셀: 현상 → 사례 → 이유 → 적용법 → CTA 순서로 한 장씩 분리한다.
- 자료나눔: 이 레퍼런스의 구조를 체크리스트나 진단표로 바꾼다.
- 상품: 기능 설명보다 고객이 원하는 변화와 믿을 근거를 먼저 제시한다.

## 연결

- 원본 레퍼런스: {source_link}
- 발행 초안: {angle_link}
- 전체 지도: {hub_link}
"""


def angle_body(planning: dict[str, Any], source_link: str, insight_link: str, hub_link: str) -> str:
    topic = clean_text(planning.get("topic"), 120)
    hook = clean_text(planning.get("hook", {}).get("first_3_seconds"), 180)
    loved = [item.get("point") for item in planning.get("loved_points", []) if isinstance(item, dict)]
    primary = clean_text(loved[0] if loved else "구체적인 실제 사례")
    return f"""# 콘텐츠각 - {topic}

> [!tip] 이 노트의 역할
> 레퍼런스를 복제하는 노트가 아니라, **구조만 가져와 내 고객 문제로 바꾸는 발행 대기열**입니다.

## 원본 인사이트

- 원본: {source_link}
- 인사이트: {insight_link}
- 사랑받은 핵심: **{primary}**
- 추천 발행 목적: **{', '.join(clean_text(item) for item in planning.get('cta', {}).get('purposes', [])) or '인지·신뢰'}**

## 내 주제로 번역

- 타깃 장면: `[어떤 고객이 언제 겪는 문제인가]`
- 한 문장 메시지: `[고객이 기존에 믿던 것]보다 [새 기준]이 더 중요하다`
- 보여줄 근거: `[실제 사례/수치/전후 화면] 3개`

## 훅 후보 3개

1. `[요즘/최근] [내 타깃] 사이에서 [변화]가 대세입니다.`
2. `[겉으로 보이는 현상]이 사랑받는 진짜 이유, 혹시 아시나요?`
3. `[흔한 믿음] 때문이 아닙니다. 실제로는 [반전 기준] 때문입니다.`

> 레퍼런스 첫 문장: {hook}

## 본문 흐름

- 0~3초 공감/현상: 지금 일어나는 변화를 한 문장으로 선언
- 3~7초 증거: 실제 사례·수치·장면 3개를 빠르게 제시
- 7~15초 오픈 루프: "왜 그런가"를 질문하고 답을 잠깐 미룸
- 중반 반전: 핵심 가치를 큰 자막·색 변화·줌·효과음으로 강조
- 후반 적용: 시청자가 오늘 해볼 행동 하나 제시
- CTA: 저장·팔로우·프로필 이동 중 이번 목적에 맞는 하나만 선택

## 제작 체크리스트

- [ ] 내 타깃의 실제 언어로 훅을 다시 씀
- [ ] 저작권 문제 없는 내 사례·영상으로 전부 교체
- [ ] 핵심 근거 3개 확보
- [ ] 3초 안에 타깃과 변화가 보이는지 확인
- [ ] CTA 하나만 선택
- [ ] CapCut 교체형 초안에서 문구·영상 교체
- [ ] 발행 후 3초 유지율·완주율·저장·댓글 기록

## 연결

- 전체 지도: {hub_link}
"""


def hub_body(records: list[dict[str, Any]]) -> str:
    recent = sorted(records, key=lambda item: item.get("archived_at", ""), reverse=True)
    strong = sorted(
        records,
        key=lambda item: (item.get("hook_score") or 0, item.get("timeliness_score") or 0),
        reverse=True,
    )
    points = Counter(
        point for record in records for point in record.get("loved_points", []) if point
    )
    recent_rows = "\n".join(
        f"| {record.get('published', '')} | {markdown_table_cell(wiki_link(Path(record['source_note']), clean_text(record.get('topic'), 58)))} | "
        f"{markdown_table_cell(record.get('creator'))} | {record.get('hook_score') or 0}/5 | {record.get('timeliness_score') or 0}/5 |"
        for record in recent[:30]
    ) or "| - | 아직 없음 | - | - | - |"
    candidates = "\n".join(
        f"- [ ] {wiki_link(Path(record['content_angle']), clean_text(record.get('topic'), 72))} "
        f"— 후킹 {record.get('hook_score') or 0}/5"
        for record in strong[:20]
    ) or "- [ ] 아직 생성된 발행 후보가 없습니다."
    pattern_text = "\n".join(f"- **{point}** · {count}회" for point, count in points.most_common()) or "- 아직 반복 패턴이 없습니다."
    return f"""# 릴스 콘텐츠 인사이트 허브

> [!summary] 세컨드 브레인 파이프라인
> 링크 분석 한 번으로 **Source Note → Insight Card → Content Angle → CapCut 초안**이 연결됩니다.
> 현재 레퍼런스 **{len(records)}개**, 발행 후보 **{len(records)}개**가 쌓여 있습니다.

## 새 콘텐츠 발행 대기열

{candidates}

## 최근 레퍼런스

| 발행일 | 레퍼런스 | 작성자 | 3초 훅 | 시의성 |
|---|---|---|---:|---:|
{recent_rows}

## 반복해서 사랑받은 패턴

{pattern_text}

## 현재 보관함에서 동적으로 찾기

### 아직 아이디어 상태인 콘텐츠각

```query
path:"30_CONTENT ANGLES/Instagram Reels" [status:idea]
```

### 후킹 점수 4점 이상인 원본

```query
path:"10_SOURCE NOTES/Instagram Reels" [hook_score:4] OR [hook_score:5]
```

## 활용 루틴

1. 새 릴스 링크를 분석해 원본 노트를 쌓는다.
2. 인사이트 카드에서 고객이 반응한 원리를 확인한다.
3. 콘텐츠각 노트의 빈칸을 내 고객·제품·사례로 바꾼다.
4. CapCut 초안의 영상과 문구만 교체해 발행한다.
5. 발행 후 지표를 콘텐츠각 노트에 기록하고 `status`를 `published`로 바꾼다.
"""


def write_managed_note(path: Path, frontmatter: dict[str, Any], body: str, default_tail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tail = default_tail.strip() + "\n"
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if END_MARKER in existing:
            preserved = existing.split(END_MARKER, 1)[1].lstrip("\n")
            if preserved.strip():
                tail = preserved.rstrip() + "\n"
        elif existing.strip():
            tail = "## 기존 노트 내용\n\n" + existing.rstrip() + "\n"
    generated = (
        render_frontmatter(frontmatter)
        + "\n"
        + START_MARKER
        + "\n"
        + body.strip()
        + "\n"
        + END_MARKER
        + "\n\n"
        + tail
    )
    path.write_text(generated, encoding="utf-8")


def update_registry(path: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        if not isinstance(records, list):
            records = []
    except (OSError, ValueError):
        records = []
    records = [item for item in records if item.get("source_id") != record.get("source_id")]
    records.append(record)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return records


def connect_home(vault: Path) -> None:
    home = vault / "00_HOME.md"
    if not home.is_file():
        return
    existing = home.read_text(encoding="utf-8")
    block = (
        f"{HOME_START}\n"
        "## 릴스 레퍼런스 파이프라인\n\n"
        f"- {wiki_link(HUB_PATH, '릴스 콘텐츠 인사이트 허브')}\n"
        "- 분석할 때마다 원본 노트·인사이트 카드·발행 후보가 자동 연결됩니다.\n"
        f"{HOME_END}"
    )
    pattern = re.compile(re.escape(HOME_START) + r".*?" + re.escape(HOME_END), re.DOTALL)
    updated = pattern.sub(block, existing) if pattern.search(existing) else existing.rstrip() + "\n\n" + block + "\n"
    home.write_text(updated, encoding="utf-8")


def render_frontmatter(values: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def wiki_link(path: Path, label: str | None = None) -> str:
    target = path.with_suffix("").as_posix()
    safe_label = clean_text(label).replace("|", "／").replace("]]", "")
    return f"[[{target}|{safe_label}]]" if label else f"[[{target}]]"


def markdown_table_cell(value: Any) -> str:
    return clean_text(value).replace("|", "\\|")


def obsidian_uri(vault: Path, relative_path: Path) -> str:
    file_path = relative_path.with_suffix("").as_posix()
    return f"obsidian://open?vault={quote(vault.name)}&file={quote(file_path)}"


def source_identifier(source: str) -> str:
    match = re.search(r"/(?:reel|p)/([^/?#]+)", source)
    if match:
        return safe_filename(match.group(1), 32)
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def clean_text(value: Any, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def safe_filename(value: Any, limit: int = 80) -> str:
    text = clean_text(value, limit * 2)
    text = re.sub(r'[\\/:*?"<>|#\[\]]', "", text).strip(" .-")
    return (text[:limit].rstrip() or "untitled")
