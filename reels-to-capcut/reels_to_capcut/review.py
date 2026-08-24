from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .capcut import (
    DEFAULT_DRAFT_DIR,
    EFFECT_APPLY_CONFIDENCE,
    EFFECT_DISPLAY_NAMES,
    EFFECT_TRACK_NAME,
    EMPHASIS_TRACK_NAME,
    SFX_TRACK_NAME,
    TEMPLATE_VIDEO_TRACK,
    TITLE_TRACK_NAME,
)
from .capcut_resources import available_effect_kinds, harvest_local_resources
from .capcut_native import MANIFEST_NAME, validate_native_project
from .models import Analysis, analysis_from_dict
from .performance import load_archive, validated_template_dir, write_archive
from .pipeline import find_template_video


REVIEW_STATUSES = {"runtime_passed", "needs_changes"}
RUNTIME_CHECKS = ("editable", "playback", "text_effect", "replace_persist")
REVIEW_LOCK = threading.Lock()
MICROSECONDS = 1_000_000


def build_review(
    output_root: Path,
    work_dir: Path | str,
    *,
    draft_dir: Path = DEFAULT_DRAFT_DIR,
) -> dict[str, Any]:
    """Build an evidence-led review of what made it into the CapCut draft.

    The downloaded reference is previewed separately from the draft.  Applied
    status is derived from the saved draft structure, not merely from the
    detector's intent.  It deliberately does not claim that CapCut rendered it.
    """
    target = validated_template_dir(output_root, work_dir)
    analysis_path = target / "분석결과.json"
    if not analysis_path.is_file():
        raise ValueError("선택한 작업의 분석 결과를 찾지 못했습니다.")
    try:
        raw_analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        if not isinstance(raw_analysis, dict):
            raise ValueError("analysis payload must be an object")
        analysis = analysis_from_dict(raw_analysis)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("분석 결과 파일을 읽지 못했습니다.") from exc

    record = archive_record(output_root, target)
    project_name = capcut_project_name(target, record)
    draft_payload, draft_path = load_draft_payload(draft_dir, project_name)
    draft = DraftEvidence(draft_payload)
    usable_effects = available_effect_kinds(harvest_local_resources(draft_dir))

    events: list[dict[str, Any]] = []
    events.extend(caption_review_events(analysis, draft))
    events.extend(motion_review_events(analysis, draft))
    events.extend(visual_review_events(analysis, draft, usable_effects))
    events.extend(overlay_review_events(analysis, draft))
    events.extend(sound_review_events(analysis, draft))
    events.sort(key=lambda item: (item["start"], category_order(item["category"]), item["id"]))

    counts = {status: sum(event["status"] == status for event in events) for status in ("recorded", "review", "missing")}
    counts["total"] = len(events)
    counts["recorded_ratio"] = round(counts["recorded"] / len(events) * 100) if events else 0
    counts["project_found"] = bool(draft_payload)

    warnings = list(dict.fromkeys(str(item) for item in analysis.warnings if str(item).strip()))
    if not project_name:
        warnings.insert(0, "연결된 CapCut 프로젝트 이름을 찾지 못했습니다.")
    elif not draft_payload:
        warnings.insert(0, f"CapCut 초안 `{project_name}`의 실제 프로젝트 파일을 찾지 못했습니다.")
    if counts["missing"]:
        warnings.append(f"로컬 CapCut 리소스 부족 또는 초안 누락으로 {counts['missing']}개 항목이 기록되지 않았습니다.")
    if counts["review"]:
        warnings.append(f"저신뢰 후보 등 {counts['review']}개 항목은 사람이 확인해야 합니다.")

    video = find_template_video(target)
    saved_review = load_saved_review(target)
    project_validation = (
        validate_native_project(draft_path.parent)
        if draft_path is not None and draft_path.name == "draft_info.json"
        else {"status": "invalid", "runtime_status": "not_checked", "blockers": ["CapCut 프로젝트 구조를 찾지 못했습니다."]}
    )
    planning = analysis.planning if isinstance(analysis.planning, dict) else {}
    return {
        "work_dir": str(target),
        "topic": record.get("topic") or planning.get("topic") or target.name,
        "source": record.get("source") or planning.get("source") or "",
        "video_name": video.name,
        "duration": round(analysis.duration, 3),
        "dimensions": {"width": analysis.width, "height": analysis.height},
        "capcut_project": project_name,
        "draft_path": str(draft_path) if draft_path else None,
        "summary": counts,
        "project_validation": project_validation,
        "caption_style": caption_style_payload(analysis),
        "category_counts": category_counts(events),
        "events": events,
        "warnings": warnings,
        "limitations": [
            "왼쪽 영상은 다운로드한 레퍼런스 원본입니다. CapCut 렌더 결과가 아닙니다.",
            "초안 기록은 JSON과 네이티브 프로젝트 구조가 연결됐다는 뜻입니다. CapCut에서 재생·편집·교체를 확인하기 전에는 적용 완료가 아닙니다.",
            "완성 MP4에는 원 제작자가 사용한 정확한 폰트명과 CapCut 프리셋 ID가 남지 않아, 화면 측정값과 이 Mac의 로컬 리소스로 가장 가까운 구성을 매칭합니다.",
            "효과음은 원본 음성과 합쳐진 상태라 완전 분리할 수 없습니다. 화면 이벤트와 맞고 신뢰도 68% 이상인 슬롯만 초안에 넣습니다.",
        ],
        "saved_review": saved_review,
    }


def save_review(
    output_root: Path,
    work_dir: Path | str,
    payload: dict[str, Any],
    *,
    draft_dir: Path = DEFAULT_DRAFT_DIR,
) -> dict[str, Any]:
    target = validated_template_dir(output_root, work_dir)
    if not (target / "분석결과.json").is_file():
        raise ValueError("선택한 작업의 분석 결과를 찾지 못했습니다.")
    status = str(payload.get("status") or "").strip()
    if status not in REVIEW_STATUSES:
        raise ValueError("검수 상태는 실행 통과 또는 수정 필요 중 하나여야 합니다.")
    note = clean_note(payload.get("note"))
    runtime_checks = {
        key: bool((payload.get("runtime_checks") or {}).get(key))
        for key in RUNTIME_CHECKS
    }
    record = archive_record(output_root, target)
    project_name = capcut_project_name(target, record)
    project_dir = draft_dir / str(project_name or "")
    validation: dict[str, Any] | None = None
    if status == "runtime_passed":
        if not all(runtime_checks.values()):
            raise ValueError("CapCut 실행 검수 항목을 모두 확인해야 통과로 저장할 수 있습니다.")
        validation = validate_native_project(project_dir)
        if validation.get("status") != "structure_ready":
            raise ValueError("CapCut 네이티브 초안 구조가 유효하지 않아 실행 통과로 저장할 수 없습니다.")
    review = {
        "status": status,
        "status_label": "실행 검수 통과" if status == "runtime_passed" else "수정 필요",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "runtime_checks": runtime_checks,
    }

    with REVIEW_LOCK:
        path = target / "검수결과.json"
        temporary = target / "검수결과.json.tmp"
        temporary.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

        update_runtime_manifest(project_dir, review, validation)

        try:
            archive_path, records = load_archive(output_root)
            record = next(
                (
                    item for item in records
                    if same_path(item.get("work_dir"), target)
                    or same_path(item.get("last_template_work_dir"), target)
                ),
                None,
            )
            if record is not None:
                record["review_status"] = status
                record["reviewed_at"] = review["reviewed_at"]
                record["review_note"] = note
                write_archive(archive_path, records)
        except ValueError:
            # Reused-template jobs are not always archived as planning records.
            pass
    return {"review": review, "path": str(path)}


def update_runtime_manifest(
    project_dir: Path,
    review: dict[str, Any],
    validation: dict[str, Any] | None,
) -> None:
    """Persist manual CapCut evidence separately from structural validation."""
    manifest_path = project_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(manifest, dict):
        return
    manifest["runtime_status"] = review["status"]
    manifest["runtime_reviewed_at"] = review["reviewed_at"]
    manifest["runtime_checks"] = review["runtime_checks"]
    if validation is not None:
        manifest["validation"] = validation
    temporary = project_dir / f"{MANIFEST_NAME}.tmp"
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)
    except OSError:
        temporary.unlink(missing_ok=True)


def archive_record(output_root: Path, target: Path) -> dict[str, Any]:
    try:
        _path, records = load_archive(output_root)
    except ValueError:
        return {}
    return next((
        item for item in records
        if same_path(item.get("work_dir"), target)
        or same_path(item.get("last_template_work_dir"), target)
    ), {})


def same_path(value: Any, target: Path) -> bool:
    try:
        return Path(str(value or "")).expanduser().resolve() == target
    except (OSError, RuntimeError):
        return False


def capcut_project_name(target: Path, record: dict[str, Any]) -> str | None:
    for filename in ("초안-원본-의존관계.json", "템플릿-재사용.json"):
        path = target / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            value = str(payload.get("capcut_project") or "").strip()
            if value:
                return value
        except (OSError, ValueError):
            continue
    value = str(record.get("capcut_project") or "").strip()
    return value or None


def load_draft_payload(draft_dir: Path, project_name: str | None) -> tuple[dict[str, Any], Path | None]:
    if not project_name:
        return {}, None
    project = (draft_dir / project_name).resolve()
    root = draft_dir.expanduser().resolve()
    if root not in project.parents:
        return {}, None
    for filename in ("draft_info.json", "draft_content.json"):
        path = project / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload, path
        except (OSError, ValueError):
            continue
    return {}, None


def load_saved_review(target: Path) -> dict[str, Any] | None:
    path = target / "검수결과.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        if not isinstance(payload, dict):
            return None
        if payload.get("status") == "approved":
            payload["status"] = "legacy_approved"
            payload["status_label"] = "이전 JSON 검수 · 실행 미확인"
        return payload
    except (OSError, ValueError):
        return None


class DraftEvidence:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.tracks = {
            str(track.get("name") or ""): track
            for track in payload.get("tracks", [])
            if isinstance(track, dict)
        }
        materials = payload.get("materials") if isinstance(payload.get("materials"), dict) else {}
        self.animations = {
            str(group.get("id")): [
                str(animation.get("name") or "")
                for animation in group.get("animations", [])
                if isinstance(animation, dict) and animation.get("name")
            ]
            for group in materials.get("material_animations", [])
            if isinstance(group, dict)
        }
        self.video_effects = {
            str(item.get("id")): item
            for item in materials.get("video_effects", [])
            if isinstance(item, dict)
        }
        self.audios = {
            str(item.get("id")): item
            for item in materials.get("audios", [])
            if isinstance(item, dict)
        }
        self.videos = {
            str(item.get("id")): item
            for item in materials.get("videos", [])
            if isinstance(item, dict)
        }

    @property
    def exists(self) -> bool:
        return bool(self.payload)

    def matching_segments(self, track_prefix: str, start: float, end: float | None = None, tolerance: float = 0.08) -> list[dict[str, Any]]:
        matches = []
        wanted_end = start if end is None else end
        for name, track in self.tracks.items():
            if not name.startswith(track_prefix):
                continue
            for segment in track.get("segments", []) or []:
                segment_start, segment_end = segment_range(segment)
                if segment_start - tolerance <= wanted_end and start <= segment_end + tolerance:
                    matches.append(segment)
        return matches

    def animation_names(self, segments: list[dict[str, Any]]) -> list[str]:
        names = []
        for segment in segments:
            for reference in segment.get("extra_material_refs", []) or []:
                names.extend(self.animations.get(str(reference), []))
        return list(dict.fromkeys(name for name in names if name))

    @staticmethod
    def has_keyframes(segments: list[dict[str, Any]]) -> bool:
        return any(bool(segment.get("common_keyframes")) for segment in segments)

    def material_name(self, segment: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> str:
        material = catalog.get(str(segment.get("material_id"))) or {}
        return str(material.get("name") or Path(str(material.get("path") or "")).name or "").strip()


def caption_review_events(analysis: Analysis, draft: DraftEvidence) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    base_track = draft.tracks.get("말 자막", {})
    base_count = len(base_track.get("segments", []) or [])
    events.append(event_payload(
        "caption-base", "caption", 0.0, analysis.duration,
        "기본 말 자막", f"{analysis.caption_style.position} · {analysis.caption_style.color} · {analysis.caption_style.animation}",
        f"스타일 직접 구성 · {base_count}개 자막 구간" if base_count else "말 자막 트랙 없음",
        analysis.caption_style.confidence,
        "recorded" if base_count else "missing",
        "프레임에서 측정한 위치·크기·색이 초안 자막 트랙에 기록됐습니다. 실제 표시는 실행 검수가 필요합니다." if base_count else "CapCut 초안에 말 자막 트랙이 없습니다.",
    ))
    if analysis.title_card:
        segments = draft.matching_segments(TITLE_TRACK_NAME, analysis.title_card.start, analysis.title_card.end)
        events.append(event_payload(
            "caption-title", "caption", analysis.title_card.start, analysis.title_card.end,
            "도입 제목 카드", "원본 문구를 비운 교체형 제목 카드",
            "제목 카드 · 문구 교체 가능" if segments else "제목 카드 트랙 없음",
            analysis.caption_style.confidence,
            "recorded" if segments else "missing",
            "위치·크기·배경색을 유지하고 문구는 직접 바꾸도록 만들었습니다." if segments else "분석에는 제목 카드가 있지만 초안 트랙을 찾지 못했습니다.",
        ))
    for index, item in enumerate(analysis.caption_events, 1):
        segments = draft.matching_segments(EMPHASIS_TRACK_NAME, item.start, item.end)
        names = draft.animation_names(segments)
        keyframed = draft.has_keyframes(segments)
        animation = names[0] if names else item.capcut_animation
        if not segments:
            status, reason = "missing", "분석된 강조 구간과 겹치는 CapCut 자막 세그먼트가 없습니다."
        elif item.animation == "pop" and not (animation or keyframed):
            status, reason = "review", "강조 자막은 들어갔지만 등장 움직임 프리셋이나 키프레임을 찾지 못했습니다."
        else:
            status, reason = "recorded", "한 번만 수정하는 단일 텍스트에 크기·투명도 키프레임이 기록됐습니다."
        events.append(event_payload(
            f"caption-{index}", "caption", item.start, item.end,
            item.text or "강조 자막", f"{item.kind} · {item.color} · {item.size_scale:.2f}배 · {item.animation}",
            f"강조 자막 · {animation}" if animation else ("강조 자막 · 사용자 정의 키프레임" if keyframed else "강조 자막 · 정적"),
            item.match_confidence or item.confidence, status, reason,
        ))
    return events


def motion_review_events(analysis: Analysis, draft: DraftEvidence) -> list[dict[str, Any]]:
    events = []
    for index, item in enumerate(analysis.motion_events, 1):
        segments = draft.matching_segments(TEMPLATE_VIDEO_TRACK, item.start, item.end)
        names = draft.animation_names(segments)
        keyframed = draft.has_keyframes(segments)
        matched = names[0] if names else item.capcut_animation
        status = "recorded" if segments and matched else "missing"
        reason = (
            "교체할 영상 트랙의 해당 구간에 배율·위치 키프레임을 기록했습니다."
            if status == "recorded" else
            "움직임은 분석됐지만 이 Mac에서 열리는 영상 애니메이션을 초안에서 확인하지 못했습니다."
        )
        if segments and keyframed and not matched:
            matched = "사용자 정의 배율·위치 키프레임"
            status = "recorded"
            reason = "교체할 영상 트랙의 해당 구간에 배율·위치 키프레임을 기록했습니다."
        events.append(event_payload(
            f"motion-{index}", "motion", item.start, item.end,
            motion_label(item.kind), f"배율 {item.scale_from:.2f}→{item.scale_to:.2f}",
            matched or "로컬 애니메이션 미매칭", item.match_confidence or item.confidence,
            status, reason,
        ))
    return events


def visual_review_events(
    analysis: Analysis,
    draft: DraftEvidence,
    usable_effects: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events = []
    effect_track = draft.tracks.get(EFFECT_TRACK_NAME, {})
    main_track = draft.tracks.get(TEMPLATE_VIDEO_TRACK, {})
    boundaries = {
        round(segment_range(segment)[0], 3)
        for segment in main_track.get("segments", []) or []
    }
    for index, item in enumerate(analysis.visual_effects, 1):
        if item.kind == "cut":
            is_cut = any(abs(boundary - item.start) <= 0.12 for boundary in boundaries)
            status = "recorded" if is_cut else "review"
            matched = "하드컷 · 전환 효과 없음" if is_cut else "컷 후보 · 직접 확인"
            reason = (
                "효과를 덧씌우지 않고 교체할 영상 트랙을 해당 시점에서 분할했습니다."
                if is_cut else "장면 변화 후보지만 초안의 분할 지점과 정확히 일치하지 않아 사람이 확인해야 합니다."
            )
        else:
            motion_segments = draft.matching_segments(TEMPLATE_VIDEO_TRACK, item.start, item.end)
            motion_properties = {
                group.get("property_type")
                for segment in motion_segments
                for group in segment.get("common_keyframes", []) or []
            }
            custom_shake = item.kind == "shake" and bool(
                {"KFTypePositionX", "KFTypePositionY"} & motion_properties
            )
            segments = [
                segment for segment in effect_track.get("segments", []) or []
                if ranges_overlap(segment_range(segment), (item.start, item.end), 0.08)
            ]
            material_names = [draft.material_name(segment, draft.video_effects) for segment in segments]
            material_names = [name for name in material_names if name]
            if custom_shake:
                status = "recorded"
                matched = "사용자 정의 위치·배율 키프레임"
                reason = "영상을 교체해도 유지되는 위치·배율 키프레임으로 흔들림을 기록했습니다."
            elif segments:
                status = "recorded"
                matched = material_names[0] if material_names else EFFECT_DISPLAY_NAMES.get(item.kind, item.kind)
                reason = "이 Mac의 로컬 CapCut 리소스 ID가 효과 트랙에 연결됐습니다. 실제 재생은 실행 검수가 필요합니다."
            elif item.kind not in usable_effects:
                status = "missing"
                matched = "로컬 CapCut 리소스 없음"
                reason = f"이 Mac의 CapCut에서 {EFFECT_DISPLAY_NAMES.get(item.kind, item.kind)} 계열 효과를 찾지 못해 안전하게 적용하지 않았습니다."
            elif item.confidence < EFFECT_APPLY_CONFIDENCE:
                status = "review"
                matched = "신뢰도 기준 미달"
                reason = f"분석 신뢰도 {round(item.confidence * 100)}%로 자동 적용 기준 80%에 못 미쳤습니다."
            else:
                status = "review"
                matched = "효과 트랙에서 미확인"
                reason = "적용 가능한 후보였지만 겹침 방지 또는 초안 저장 과정에서 효과 트랙에 남지 않았습니다."
        events.append(event_payload(
            f"visual-{index}", "visual", item.start, item.end,
            item.label, item.evidence, matched, item.confidence, status, reason,
        ))
    return events


def overlay_review_events(analysis: Analysis, draft: DraftEvidence) -> list[dict[str, Any]]:
    events = []
    for index, item in enumerate(analysis.overlay_events, 1):
        segments = draft.matching_segments(f"오버레이 교체 {index}", item.start, item.end)
        material = draft.material_name(segments[0], draft.videos) if segments else ""
        if segments and item.asset_path and Path(item.asset_path).is_file():
            status, reason = "recorded", "원본에서 잘라낸 카드 이미지와 위치·크기·팝업 키프레임이 교체형 트랙에 기록됐습니다."
        elif segments:
            status, reason = "review", "자리표시자 트랙은 있지만 연결한 이미지 파일을 찾지 못했습니다."
        else:
            status, reason = "missing", "분석된 오버레이와 일치하는 CapCut 트랙이 없습니다."
        events.append(event_payload(
            f"overlay-{index}", "overlay", item.start, item.end,
            item.label, f"위치 {item.center_x:.2f}, {item.center_y:.2f} · 크기 {item.width:.2f}×{item.height:.2f}",
            material or ("교체형 카드 트랙" if segments else "오버레이 미적용"),
            item.confidence, status, reason,
        ))
    return events


def sound_review_events(analysis: Analysis, draft: DraftEvidence) -> list[dict[str, Any]]:
    events = []
    for index, item in enumerate(analysis.sound_events, 1):
        segments = draft.matching_segments(SFX_TRACK_NAME, item.start, item.end, tolerance=0.04)
        segment = min(segments, key=lambda value: abs(segment_range(value)[0] - item.start)) if segments else None
        material = draft.material_name(segment, draft.audios) if segment else ""
        asset_exists = bool(item.asset_path and Path(item.asset_path).is_file())
        if segment and asset_exists:
            status = "recorded"
            reason = "화면 이벤트와 맞는 효과음을 교체 가능한 오디오 트랙에 넣었습니다."
        elif item.confidence < 0.68:
            status = "review"
            reason = "단순 음량 피크일 가능성이 있어 자동 적용하지 않고 참고 후보로만 남겼습니다."
        elif not asset_exists:
            status = "missing"
            reason = "효과음 파일이 없어 CapCut 초안에 넣지 못했습니다."
        else:
            status = "missing"
            reason = "신뢰도 기준은 통과했지만 CapCut 오디오 트랙에서 해당 슬롯을 확인하지 못했습니다."
        events.append(event_payload(
            f"sound-{index}", "sound", item.start, item.end,
            sound_label(item.kind), "화면 이벤트 동기화" if item.basis == "visual_sync" else "오디오 피크 추정",
            material or ("참고 후보 · 미적용" if status == "review" else "효과음 미적용"),
            item.confidence, status, reason,
        ))
    return events


def event_payload(
    event_id: str,
    category: str,
    start: float,
    end: float,
    title: str,
    inferred: str,
    matched: str,
    confidence: float,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "category": category,
        "category_label": category_label(category),
        "start": round(max(0.0, float(start)), 3),
        "end": round(max(float(start), float(end)), 3),
        "time_label": time_label(float(start), float(end)),
        "title": str(title),
        "inferred": str(inferred),
        "matched_resource": str(matched),
        "confidence": round(max(0.0, min(1.0, float(confidence or 0))), 3),
        "status": status,
        "status_label": {"recorded": "초안 기록", "review": "확인 필요", "missing": "초안 누락"}[status],
        "reason": str(reason),
    }


def caption_style_payload(analysis: Analysis) -> dict[str, Any]:
    style = analysis.caption_style
    return {
        "position": style.position,
        "size": style.size,
        "color": style.color,
        "outline": style.outline,
        "background": style.background,
        "animation": style.animation,
        "confidence": round(style.confidence, 3),
        "evidence": list(style.evidence),
    }


def category_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    return {
        category: sum(event["category"] == category for event in events)
        for category in ("caption", "motion", "visual", "overlay", "sound")
    }


def segment_range(segment: dict[str, Any]) -> tuple[float, float]:
    timerange = segment.get("target_timerange") if isinstance(segment.get("target_timerange"), dict) else {}
    start = float(timerange.get("start") or 0) / MICROSECONDS
    end = start + float(timerange.get("duration") or 0) / MICROSECONDS
    return start, end


def ranges_overlap(left: tuple[float, float], right: tuple[float, float], tolerance: float = 0.0) -> bool:
    return left[0] - tolerance <= right[1] and right[0] <= left[1] + tolerance


def time_label(start: float, end: float) -> str:
    def stamp(value: float) -> str:
        minutes, seconds = divmod(max(0.0, value), 60)
        return f"{int(minutes):02d}:{seconds:04.1f}"
    return f"{stamp(start)}–{stamp(end)}"


def category_order(category: str) -> int:
    return {"caption": 0, "motion": 1, "visual": 2, "overlay": 3, "sound": 4}.get(category, 9)


def category_label(category: str) -> str:
    return {
        "caption": "자막",
        "motion": "영상 움직임",
        "visual": "화면 효과",
        "overlay": "오버레이",
        "sound": "효과음",
    }.get(category, category)


def motion_label(kind: str) -> str:
    return {"punch_in": "펀치 줌 인", "shake": "영상 흔들림"}.get(kind, kind)


def sound_label(kind: str) -> str:
    return {"impact": "임팩트", "pop": "팝", "whoosh": "우시", "sparkle": "반짝임"}.get(kind, kind)


def clean_note(value: Any) -> str:
    return "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())[:1000]
