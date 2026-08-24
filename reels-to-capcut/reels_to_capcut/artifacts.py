from __future__ import annotations

import json
from pathlib import Path

from .capcut import (
    DEFAULT_DRAFT_DIR,
    EFFECT_DISPLAY_NAMES,
    EFFECT_TRACK_NAME,
    plan_effect_segments,
)
from .capcut_resources import available_effect_kinds, harvest_local_resources
from .models import Analysis, Cue
from .utils import format_timestamp


def write_srt(path: Path, cues: list[Cue]) -> Path:
    blocks = []
    for index, cue in enumerate(sorted(cues, key=lambda item: (item.start, item.end)), 1):
        blocks.append(
            f"{index}\n{format_timestamp(cue.start, srt=True)} --> "
            f"{format_timestamp(cue.end, srt=True)}\n{cue.text.strip()}"
        )
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return path


def write_analysis(path: Path, analysis: Analysis) -> Path:
    path.write_text(json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_dependency_manifest(
    path: Path,
    project_name: str | None,
    video_path: Path,
    *,
    reference_video_path: Path | None = None,
) -> Path:
    payload = {
        "warning": "이 결과 폴더를 삭제하면 연결된 CapCut 초안의 원본 영상이 사라집니다.",
        "capcut_project": project_name,
        "source_video": str(video_path.resolve()),
        "reference_video": str(reference_video_path.resolve()) if reference_video_path else None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_guide(
    path: Path,
    *,
    source: str,
    video_path: Path,
    analysis: Analysis,
    project_name: str | None,
    capcut_error: str | None,
    draft_dir: Path | None = None,
) -> Path:
    usable_kinds = available_effect_kinds(
        harvest_local_resources(draft_dir or DEFAULT_DRAFT_DIR)
    )
    scenes = "\n".join(f"- `{format_timestamp(value)}` 컷 전환 후보" for value in analysis.scenes) or "- 감지된 컷 없음"
    peaks = "\n".join(f"- `{format_timestamp(value)}` 효과음 후보" for value in analysis.audio_peaks) or "- 감지된 피크 없음"
    screen = "\n".join(
        f"- `{format_timestamp(cue.start)}` {cue.text.replace(chr(10), ' / ')}"
        for cue in analysis.screen_text
    ) or "- 별도 화면 글자 없음"
    applied = {
        id(effect): usable_kinds[effect.kind]
        for effect, _start, _end in plan_effect_segments(analysis, usable_kinds)
    }
    visual_effects = "\n".join(
        f"- `{format_timestamp(effect.start)}`~`{format_timestamp(effect.end)}` "
        f"**{effect.label}** · 신뢰도 {round(effect.confidence * 100)}%\n"
        f"  - 근거: {effect.evidence}\n"
        f"  - CapCut 재현 후보: `{effect.capcut_suggestion}`\n"
        f"  - {effect_status(effect, applied, usable_kinds)}"
        for effect in analysis.visual_effects
    ) or "- 뚜렷한 화면 효과 후보 없음"
    caption = analysis.caption_style
    caption_evidence = "\n".join(f"- {item}" for item in caption.evidence) or "- 측정 근거 없음"
    warnings = "\n".join(f"- {warning}" for warning in analysis.warnings) or "- 없음"
    capcut = f"`{project_name}`" if project_name else f"생성 실패 — {capcut_error or '알 수 없는 오류'}"
    caption_events = "\n".join(
        f"- `{format_timestamp(item.start)}`~`{format_timestamp(item.end)}` "
        f"**{item.text}** · {item.kind} · {item.color} · {item.size_scale:.2f}배 · {item.animation}"
        f" · CapCut `{item.capcut_animation or '미매칭'}` ({round(item.match_confidence * 100)}%)"
        for item in analysis.caption_events
    ) or "- 강조 자막 이벤트 없음"
    overlays = "\n".join(
        f"- `{format_timestamp(item.start)}`~`{format_timestamp(item.end)}` "
        f"{item.label} · 위치 ({item.center_x:.2f}, {item.center_y:.2f}) · 크기 {item.width:.2f}×{item.height:.2f}"
        for item in analysis.overlay_events
    ) or "- 교체 가능한 오버레이 없음"
    motions = "\n".join(
        f"- `{format_timestamp(item.start)}`~`{format_timestamp(item.end)}` "
        f"{item.kind} · 배율 {item.scale_from:.2f}→{item.scale_to:.2f} · 신뢰도 {round(item.confidence * 100)}%"
        f" · CapCut `{item.capcut_animation or '미매칭'}` ({round(item.match_confidence * 100)}%)"
        for item in analysis.motion_events
    ) or "- 키프레임 화면 움직임 없음"
    template_sounds = [item for item in analysis.sound_events if item.confidence >= 0.68]
    sounds = "\n".join(
        f"- `{format_timestamp(item.start)}` {item.kind} · 신뢰도 {round(item.confidence * 100)}% "
        f"· 근거 {'화면 이벤트 동기화' if item.basis == 'visual_sync' else '오디오 피크'}"
        for item in analysis.sound_events
    ) or "- 효과음 슬롯 없음"
    text = f"""# 릴스 분석 · CapCut 편집 가이드

## 결과

- 원본 입력: `{source}`
- 로컬 영상: `{video_path.resolve()}`
- 길이: `{analysis.duration:.2f}초`
- CapCut 초안: {capcut}
- 말 자막: `{len(analysis.speech)}개`
- 별도 화면 글자: `{len(analysis.screen_text)}개`
- 화면 효과 후보: `{len(analysis.visual_effects)}개`
- 초안 효과 트랙에 기록된 후보: `{len(applied)}개` (`{EFFECT_TRACK_NAME}` 트랙, CapCut 실행 미검수)
- 강조 자막 이벤트: `{len(analysis.caption_events)}개`
- 교체 오버레이 슬롯: `{len(analysis.overlay_events)}개`
- 화면 키프레임 이벤트: `{len(analysis.motion_events)}개`
- 효과음 템플릿 슬롯: `{len(template_sounds)}개` (저신뢰 오디오 피크 후보 `{len(analysis.sound_events) - len(template_sounds)}개`는 가이드에만 표시)

> **삭제 주의:** 이 폴더를 삭제하면 위 영상을 참조하는 CapCut 초안이 깨집니다.

## CapCut에서 시작하는 순서

1. `교체할 영상 · 템플릿` 트랙에서 원본 조각을 내 영상으로 교체합니다. 컷과 펀치 줌 키프레임은 그대로 남깁니다.
2. `말 자막`의 문구를 수정하고 `강조 자막 · 교체가능`의 핵심 단어를 내 문구로 바꿉니다.
3. `오버레이 교체 N` 카드에 내 계정·상품·사례 정보를 넣습니다.
4. `효과음 템플릿 · 교체가능`에는 분석된 타이밍과 종류에 맞춘 소리가 들어 있습니다. 원하는 음원으로 교체할 수 있습니다.
5. `화면 글자`, `화면 효과 분석`, `효과음 후보`는 화면을 가리지 않도록 숨긴 참고 메모입니다.
6. 원본 영상·음원·효과를 그대로 재배포하지 말고 편집 구조만 사용합니다.

## 교체 가능한 편집 문법

### 강조 자막

{caption_events}

### 오버레이 슬롯

{overlays}

### 화면 키프레임

{motions}

### 효과음 템플릿

{sounds}

> 완성 MP4는 목소리와 효과음이 한 오디오 트랙으로 합쳐져 있어 원 효과음을 깨끗하게 분리할 수 없습니다. 화면 등장·확대와 동기화된 고신뢰 슬롯만 초안에 넣고, 단순 음량 피크는 참고 후보로만 남깁니다.

## 레퍼런스 자막 스타일

- 위치: **{caption.position}**
- 크기: **{caption.size}**
- 글자색: **{caption.color}**
- 외곽선: **{caption.outline}**
- 배경: **{caption.background}**
- 등장 방식: **{caption.animation}**
- 종합 신뢰도: **{round(caption.confidence * 100)}%**

{caption_evidence}

> 정확한 폰트명과 원 제작자가 누른 CapCut 프리셋은 MP4에 남지 않습니다. 위 결과는
> 프레임에 실제로 보이는 위치·크기·색·움직임을 측정한 재현 후보입니다.

## 화면 효과 분석

{visual_effects}

## 컷 전환 후보

{scenes}

## 효과음 후보

{peaks}

## 화면 글자

{screen}

## 처리 중 경고

{warnings}
"""
    path.write_text(text, encoding="utf-8")
    return path


def effect_status(effect, applied: dict, usable_kinds: dict) -> str:
    entry = applied.get(id(effect))
    if entry is not None:
        return (
            f"**초안에 리소스 기록됨** — `{EFFECT_TRACK_NAME}` 트랙의 "
            f"`{entry.get('name')}` 후보. 실제 재생은 CapCut 실행 검수가 필요합니다."
        )
    if effect.kind == "cut":
        return "초안 효과로 기록하지 않음 — 하드컷은 효과가 아니라 자르는 지점입니다."
    if effect.kind not in usable_kinds:
        name = EFFECT_DISPLAY_NAMES.get(effect.kind, effect.kind)
        return (
            f"초안 효과로 기록하지 않음 — 이 Mac의 CapCut에서 {name} 계열 효과를 아직 찾지 못했습니다. "
            f"CapCut에서 그 효과를 한 번 써 보시면 다음 실행부터 자동으로 걸립니다."
        )
    return "초안 효과로 기록하지 않음 — 신뢰도가 기준(80%)에 못 미칩니다. 필요하면 직접 넣으세요."
