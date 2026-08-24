from __future__ import annotations

import json
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .capcut_resources import (
    available_effect_kinds,
    copy_resource_fields,
    harvest_local_resources,
    pick_sound,
    pick_text_intro,
    pick_video_group,
)
from .capcut_native import (
    collect_schema_prototypes,
    find_native_project,
    hydrate_native_payload,
    install_native_project,
    read_json,
)
from .models import Analysis, CaptionEvent, CaptionStyle, Cue, OverlayEvent, TitleCard, VisualEffect
from .utils import safe_slug


DEFAULT_DRAFT_DIR = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"

EFFECT_TRACK_NAME = "화면 효과 재현"
TITLE_TRACK_NAME = "제목 카드"
TEMPLATE_VIDEO_TRACK = "교체할 영상 · 템플릿"
EMPHASIS_TRACK_NAME = "강조 자막 · 교체가능"
SPARKLE_TRACK_NAME = "강조 반짝임 · 자동"
SFX_TRACK_NAME = "효과음 템플릿 · 교체가능"
REFERENCE_VIDEO_TRACK = "참고 원본 · 숨김"

# 편집 참고용 메모 트랙. 화면에 찍히면 방해가 되므로 숨김 처리한다.
MEMO_TRACK_PREFIXES = ("참고 원본", "효과음 후보", "화면 글자", "화면 효과 분석")
EFFECT_APPLY_CONFIDENCE = 0.80
EFFECT_MIN_LENGTH = 0.2

# 검출한 화면 효과를 실제 CapCut 영상 효과에 잇는 표.
# 하드컷(cut)은 효과가 아니라 그냥 자른 지점이므로 자동 적용하지 않는다.
EFFECT_TYPE_NAMES = {
    "flash": "闪白",
    "shake": "抖动",
    "zoom_in": "快速缩放",
    "zoom_out": "变焦推镜",
    "blur": "动感模糊",
}

EFFECT_DISPLAY_NAMES = {
    "flash": "화이트 플래시",
    "shake": "흔들림",
    "zoom_in": "빠른 줌 인",
    "zoom_out": "줌 아웃",
    "blur": "동적 블러",
}


def plan_effect_segments(
    analysis: Analysis,
    usable_kinds: dict | None = None,
) -> list[tuple[VisualEffect, float, float]]:
    """신뢰도가 충분하고 이 Mac의 CapCut에 실제로 있는 효과만 겹치지 않게 배치한다."""
    allowed = EFFECT_TYPE_NAMES if usable_kinds is None else usable_kinds
    planned: list[tuple[VisualEffect, float, float]] = []
    last_end = 0.0
    for effect in sorted(analysis.visual_effects, key=lambda item: item.start):
        if effect.kind not in allowed:
            continue
        if effect.confidence < EFFECT_APPLY_CONFIDENCE:
            continue
        start = max(effect.start, last_end)
        end = min(effect.end, analysis.duration)
        if end - start < EFFECT_MIN_LENGTH:
            continue
        planned.append((effect, start, end))
        last_end = end
    return planned


def create_capcut_draft(
    video_path: Path,
    analysis: Analysis,
    title: str,
    draft_dir: Path = DEFAULT_DRAFT_DIR,
    *,
    reference_video_path: Path | None = None,
) -> str:
    try:
        import pycapcut as cc
    except ImportError as exc:
        raise RuntimeError("pycapcut이 설치되지 않았습니다. 설치.command를 먼저 실행해주세요.") from exc
    if not draft_dir.is_dir():
        raise RuntimeError("CapCut 초안 폴더를 찾지 못했습니다. CapCut을 한 번 실행한 뒤 다시 시도해주세요.")

    native_project = find_native_project(draft_dir, analysis.width, analysis.height)
    if native_project is None:
        raise RuntimeError(
            "현재 CapCut 버전이 만든 네이티브 프로젝트를 찾지 못했습니다. "
            "CapCut에서 세로 프로젝트를 하나 만든 뒤 앱을 다시 실행해주세요."
        )
    native_template = read_json(native_project / "draft_info.json")
    native_prototypes = collect_schema_prototypes(draft_dir)

    resources = harvest_local_resources(draft_dir)
    usable_kinds = available_effect_kinds(resources)
    text_intro = pick_text_intro(resources, analysis.caption_style.animation)
    # 강조 자막은 지역/버전에 종속된 프리셋 하나로 뭉개지 않고, 표준 텍스트
    # 키프레임과 색상 레이어를 조합한다. 그래야 문구를 바꿔도 효과가 유지된다.
    emphasis_intro = None
    sound_resources = {
        kind: source for kind in {event.kind for event in analysis.sound_events}
        if (source := pick_sound(resources, kind)) is not None
    }
    apply_local_sound_matches(analysis, sound_resources)
    motion_groups = {
        kind: group for kind in {event.kind for event in analysis.motion_events}
        if (group := pick_video_group(resources, kind)) is not None
    }
    missing_motion = sorted({event.kind for event in analysis.motion_events} - set(motion_groups))
    if missing_motion:
        analysis.warnings.append(f"로컬 CapCut 영상 애니메이션 미매칭: {', '.join(missing_motion)}")

    name = unique_project_name(draft_dir, title)
    with tempfile.TemporaryDirectory(prefix="reels-capcut-payload-") as temporary:
        temporary_draft_dir = Path(temporary)
        folder = cc.DraftFolder(str(temporary_draft_dir))
        script = folder.create_draft(name, analysis.width, analysis.height, fps=30, allow_replace=False)
        material = cc.VideoMaterial(str(video_path.resolve()))
        # CapCut이 읽는 소재 길이는 ffprobe 컨테이너 길이보다 짧을 수 있으므로 여기에 맞춰 자른다.
        material_limit = int(material.duration)
        minimum_length = int(0.05 * cc.SEC)
        add_template_video_track(
            script, cc, material, material_limit, minimum_length, analysis, motion_groups
        )
        if reference_video_path is not None and reference_video_path.is_file():
            add_reference_video_track(script, cc, reference_video_path, analysis)

        speech_position = caption_position(analysis.caption_style)
        add_caption_track(
            script, cc, "말 자막", base_caption_cues(analysis.speech, analysis.caption_events), position=speech_position,
            animated=text_intro is not None, reference_style=analysis.caption_style,
        )
        add_emphasis_caption_track(script, cc, analysis)
        add_title_card(script, cc, analysis.title_card)
        add_overlay_slots(script, cc, analysis.overlay_events)
        add_caption_track(script, cc, "화면 글자", analysis.screen_text, position=0.58, animated=False)
        effect_cues = [
            Cue(effect.start, effect.end, f"{effect.label} · {round(effect.confidence * 100)}%", "visual-effect")
            for effect in analysis.visual_effects
        ]
        add_caption_track(script, cc, "화면 효과 분석", effect_cues, position=0.82, animated=False)
        peak_cues = [
            Cue(max(0, point - 0.12), min(analysis.duration, point + 0.28), "🔊 효과음 후보", "peak")
            for point in analysis.audio_peaks
        ]
        add_caption_track(script, cc, "효과음 후보 · 숨김", peak_cues, position=0.82, animated=False)
        add_sound_template_track(script, cc, analysis)
        applied_kinds = apply_detected_effects(script, cc, analysis, usable_kinds)
        script.save()

        generated_dir = temporary_draft_dir / name
        write_macos_compatibility(generated_dir, native_template)
        retarget_local_resources(
            generated_dir, applied_kinds, usable_kinds,
            text_intro, emphasis_intro, next(iter(motion_groups.values()), None),
        )
        postprocess_helper_tracks(generated_dir)
        generated_payload = read_json(generated_dir / "draft_info.json")
        hydrated_payload = hydrate_native_payload(
            generated_payload, native_template, native_prototypes
        )
        validation = install_native_project(
            native_project,
            draft_dir / name,
            hydrated_payload,
            name,
            analysis.duration,
        )
        if validation.get("status") != "structure_ready":
            blockers = ", ".join(validation.get("blockers") or ["원인 미상"])
            raise RuntimeError(f"CapCut 네이티브 초안 구조 검증 실패: {blockers}")
    return name


def add_template_video_track(
    script, cc, material, material_limit: int, minimum_length: int,
    analysis: Analysis, motion_groups: dict[str, dict] | None = None,
) -> None:
    """레퍼런스 컷과 펀치 줌을 가진, 사용자가 영상만 교체할 주 트랙."""
    script.add_track(cc.TrackType.video, TEMPLATE_VIDEO_TRACK)
    boundaries = sorted({
        0.0, *analysis.scenes, analysis.duration,
        *(event.start for event in analysis.motion_events),
        *(event.end for event in analysis.motion_events),
    })
    for start, end in zip(boundaries, boundaries[1:]):
        start_us = min(int(start * cc.SEC), material_limit)
        end_us = min(int(end * cc.SEC), material_limit)
        length_us = end_us - start_us
        if length_us < minimum_length:
            continue
        segment = cc.VideoSegment(
            material,
            cc.trange(start_us, length_us),
            source_timerange=cc.trange(start_us, length_us),
        )
        matching_motion = next(
            (motion for motion in analysis.motion_events
             if abs(motion.start - start) <= 0.002 and abs(motion.end - end) <= 0.002),
            None,
        )
        if matching_motion:
            # 클라우드/지역별 영상 애니메이션 프리셋에 기대지 않는다. 레퍼런스에서
            # 잰 배율과 이동량을 표준 키프레임으로 넣어 영상 교체 뒤에도 유지한다.
            punch_us = min(length_us - 1, int(min(0.10, max(0.04, length_us / cc.SEC * 0.16)) * cc.SEC))
            recoil_us = min(length_us - 1, max(punch_us + 1, int(min(0.19, length_us / cc.SEC * 0.34) * cc.SEC)))
            segment.add_keyframe(cc.KeyframeProperty.uniform_scale, 0, matching_motion.scale_from)
            segment.add_keyframe(cc.KeyframeProperty.uniform_scale, punch_us, matching_motion.scale_to)
            segment.add_keyframe(cc.KeyframeProperty.uniform_scale, recoil_us, max(1.0, matching_motion.scale_to * 0.985))
            segment.add_keyframe(cc.KeyframeProperty.position_x, 0, 0.0)
            segment.add_keyframe(cc.KeyframeProperty.position_x, punch_us, matching_motion.shift_x)
            segment.add_keyframe(cc.KeyframeProperty.position_x, recoil_us, -matching_motion.shift_x * 0.18)
            segment.add_keyframe(cc.KeyframeProperty.position_y, 0, 0.0)
            segment.add_keyframe(cc.KeyframeProperty.position_y, punch_us, matching_motion.shift_y)
            segment.add_keyframe(cc.KeyframeProperty.position_y, recoil_us, -matching_motion.shift_y * 0.18)
            matching_motion.capcut_animation = "사용자 정의 펀치 줌·흔들림 키프레임"
            matching_motion.match_confidence = 0.92
        script.add_segment(segment, TEMPLATE_VIDEO_TRACK)


def add_reference_video_track(script, cc, video_path: Path, analysis: Analysis) -> None:
    """자막이 구워진 레퍼런스는 비교용 숨김 트랙으로만 보존한다."""
    material = cc.VideoMaterial(
        str(video_path.resolve()), material_name="참고 원본 · 자막 번인"
    )
    duration_us = min(int(analysis.duration * cc.SEC), int(material.duration))
    if duration_us <= 0:
        return
    script.add_track(cc.TrackType.video, REFERENCE_VIDEO_TRACK)
    segment = cc.VideoSegment(
        material,
        cc.trange(0, duration_us),
        source_timerange=cc.trange(0, duration_us),
    )
    script.add_segment(segment, REFERENCE_VIDEO_TRACK)


def add_emphasis_caption_track(script, cc, analysis: Analysis) -> None:
    """레퍼런스의 강조 자막을 편집 가능한 CapCut 레이어로 만든다.

    문구를 한 번만 바꿔도 효과가 따라가야 하므로, 본문 자막은 항상
    하나의 텍스트 소재로 만들고 움직임을 키프레임으로 기록한다.
    """
    if not analysis.caption_events:
        return
    script.add_track(cc.TrackType.text, EMPHASIS_TRACK_NAME)
    if any(event.kind == "color_then_pop" for event in analysis.caption_events):
        script.add_track(cc.TrackType.text, SPARKLE_TRACK_NAME)
    base_size = caption_size(analysis.caption_style)
    position = caption_position(analysis.caption_style)
    border = cc.TextBorder(alpha=0.9, color=(0.0, 0.0, 0.0), width=32.0)
    for event in analysis.caption_events:
        duration = max(0.10, event.end - event.start)
        if event.kind == "color_then_pop" and duration >= 0.24:
            add_color_flash_caption(script, cc, event, base_size, position, border)
        else:
            add_keyword_caption(script, cc, event, base_size, position, border)


def add_color_flash_caption(script, cc, event: CaptionEvent, base_size: float, position: float, border) -> None:
    """편집할 글자를 하나의 소재에만 두고 오버슈트를 재현한다."""
    duration = max(0.24, event.end - event.start)
    duration_us = int(duration * cc.SEC)
    segment = cc.TextSegment(
        event.text,
        cc.trange(int(event.start * cc.SEC), duration_us),
        style=cc.TextStyle(
            size=round(base_size * max(1.18, min(1.42, event.size_scale)), 1),
            bold=True, color=(1.0, 1.0, 1.0), align=1,
        ),
        border=border,
        clip_settings=cc.ClipSettings(transform_y=position),
    )
    # 레퍼런스는 첫 0.1초에 아주 작게 시작해 크게 튄 뒤 바로 정착한다.
    segment.add_keyframe(cc.KeyframeProperty.uniform_scale, 0, 0.12)
    segment.add_keyframe(cc.KeyframeProperty.uniform_scale, min(duration_us - 1, int(0.075 * cc.SEC)), 1.32)
    segment.add_keyframe(cc.KeyframeProperty.uniform_scale, min(duration_us - 1, int(0.20 * cc.SEC)), 1.0)
    segment.add_keyframe(cc.KeyframeProperty.alpha, 0, 0.12)
    segment.add_keyframe(cc.KeyframeProperty.alpha, min(duration_us - 1, int(0.045 * cc.SEC)), 1.0)
    script.add_segment(segment, EMPHASIS_TRACK_NAME)

    sparkle_duration = min(0.32, duration)
    sparkle_us = int(sparkle_duration * cc.SEC)
    sparkle = cc.TextSegment(
        "✦        ✦",
        cc.trange(int(event.start * cc.SEC), sparkle_us),
        style=cc.TextStyle(size=round(base_size * 0.9, 1), bold=True, color=(1.0, 0.88, 0.28), align=1),
        clip_settings=cc.ClipSettings(transform_y=round(min(0.95, position + 0.055), 4)),
    )
    sparkle.add_keyframe(cc.KeyframeProperty.uniform_scale, 0, 0.35)
    sparkle.add_keyframe(cc.KeyframeProperty.uniform_scale, min(sparkle_us - 1, int(0.10 * cc.SEC)), 1.25)
    sparkle.add_keyframe(cc.KeyframeProperty.uniform_scale, max(1, sparkle_us - 1), 0.72)
    sparkle.add_keyframe(cc.KeyframeProperty.alpha, 0, 0.0)
    sparkle.add_keyframe(cc.KeyframeProperty.alpha, min(sparkle_us - 1, int(0.065 * cc.SEC)), 1.0)
    sparkle.add_keyframe(cc.KeyframeProperty.alpha, max(1, sparkle_us - 1), 0.0)
    script.add_segment(sparkle, SPARKLE_TRACK_NAME)

    event.capcut_animation = "단일 편집 자막 + 오버슈트 키프레임 + 반짝임"
    event.match_confidence = max(event.match_confidence, 0.90)


def add_keyword_caption(script, cc, event: CaptionEvent, base_size: float, position: float, border) -> None:
    duration = max(0.10, event.end - event.start)
    color = (1.0, 0.82, 0.08) if event.color == "노랑" else (1.0, 1.0, 1.0)
    segment = cc.TextSegment(
        event.text,
        cc.trange(int(event.start * cc.SEC), int(duration * cc.SEC)),
        style=cc.TextStyle(
            size=round(base_size * max(1.0, min(1.8, event.size_scale)), 1),
            bold=True, color=color, align=1,
        ),
        border=border,
        clip_settings=cc.ClipSettings(transform_y=position),
    )
    if event.animation == "pop" and duration >= 0.18:
        duration_us = int(duration * cc.SEC)
        segment.add_keyframe(cc.KeyframeProperty.uniform_scale, 0, 0.64)
        segment.add_keyframe(cc.KeyframeProperty.uniform_scale, min(duration_us - 1, int(0.10 * cc.SEC)), 1.12)
        segment.add_keyframe(cc.KeyframeProperty.uniform_scale, min(duration_us - 1, int(0.19 * cc.SEC)), 1.0)
        event.capcut_animation = "확대 오버슈트 키프레임"
        event.match_confidence = max(event.match_confidence, 0.90)
    else:
        event.capcut_animation = "즉시 대형 교체"
        event.match_confidence = max(event.match_confidence, 0.88)
    script.add_segment(segment, EMPHASIS_TRACK_NAME)


def add_overlay_slots(script, cc, overlays: list[OverlayEvent]) -> None:
    for index, overlay in enumerate(overlays, 1):
        name = f"오버레이 교체 {index}"
        duration = max(0.15, overlay.end - overlay.start)
        if overlay.asset_path and Path(overlay.asset_path).is_file():
            script.add_track(cc.TrackType.video, name)
            material = cc.VideoMaterial(overlay.asset_path, material_name=f"{overlay.label} · 교체가능")
            segment = cc.VideoSegment(
                material,
                cc.trange(int(overlay.start * cc.SEC), int(duration * cc.SEC)),
                source_timerange=cc.trange(0, int(duration * cc.SEC)),
                clip_settings=cc.ClipSettings(
                    scale_x=max(0.12, overlay.width), scale_y=max(0.12, overlay.width),
                    transform_x=round((overlay.center_x - 0.5) * 2.0, 4),
                    transform_y=round(1.0 - overlay.center_y * 2.0, 4),
                ),
            )
        else:
            script.add_track(cc.TrackType.text, name)
            segment = cc.TextSegment(
                overlay.label,
                cc.trange(int(overlay.start * cc.SEC), int(duration * cc.SEC)),
                style=cc.TextStyle(size=4.5, bold=True, color=(0.08, 0.08, 0.08), align=1),
                background=cc.TextBackground(
                    color="#FFFFFF", alpha=0.96, round_radius=0.16,
                    height=max(0.12, overlay.height * 1.4), width=max(0.20, overlay.width * 0.55),
                ),
                clip_settings=cc.ClipSettings(
                    transform_x=round((overlay.center_x - 0.5) * 2.0, 4),
                    transform_y=round(1.0 - overlay.center_y * 2.0, 4),
                ),
            )
        segment.add_keyframe(cc.KeyframeProperty.uniform_scale, 0, 0.72)
        segment.add_keyframe(cc.KeyframeProperty.uniform_scale, int(min(0.16, duration / 3) * cc.SEC), 1.0)
        script.add_segment(segment, name)


def base_caption_cues(speech: list[Cue], emphasis: list[CaptionEvent]) -> list[Cue]:
    """강조 단어를 빼고, 레퍼런스처럼 짧은 호흡의 교체형 자막으로 나눈다."""
    output: list[Cue] = []
    for cue in speech:
        if not cue.text.strip():
            continue
        if cue.words:
            groups: list[list] = []
            group: list = []
            for word in cue.words:
                if any(event.start <= (word.start + word.end) / 2 < event.end for event in emphasis):
                    if group:
                        groups.append(group)
                        group = []
                    continue
                proposed = [*group, word]
                character_count = len("".join(item.text for item in proposed).replace(" ", ""))
                span = word.end - proposed[0].start
                gap = word.start - group[-1].end if group else 0.0
                if group and (
                    gap > 0.28 or len(proposed) > 3 or character_count > 12 or span > 0.92
                ):
                    groups.append(group)
                    group = [word]
                else:
                    group = proposed
            if group:
                groups.append(group)
            for index, group in enumerate(groups):
                # 다음 문구가 등장하기 직전까지 현재 문구를 유지해 깜빡이는 공백을 없앤다.
                natural_end = group[-1].end
                next_start = groups[index + 1][0].start if index + 1 < len(groups) else cue.end
                end = max(natural_end, min(cue.end, next_start))
                start = group[0].start
                for event in emphasis:
                    if start < event.end and event.start < end:
                        if group[-1].end <= event.end:
                            end = min(end, event.start)
                        else:
                            start = max(start, event.end)
                if end - start >= 0.05:
                    output.append(Cue(
                        start, end, " ".join(item.text for item in group), cue.source, group.copy()
                    ))
        elif not any(cue.start < event.end and event.start < cue.end for event in emphasis):
            output.append(cue)
    compacted: list[Cue] = []
    for cue in output:
        if (
            compacted
            and cue.source == compacted[-1].source
            and 0 <= cue.start - compacted[-1].end <= 0.12
            and compacted[-1].end - compacted[-1].start <= 0.22
            and cue.end - cue.start <= 0.50
            and len((compacted[-1].text + cue.text).replace(" ", "")) <= 12
        ):
            previous = compacted[-1]
            previous.end = cue.end
            previous.text = f"{previous.text} {cue.text}".strip()
            previous.words.extend(cue.words)
        else:
            compacted.append(cue)
    return compacted


def apply_local_sound_matches(analysis: Analysis, sound_resources: dict[str, dict]) -> None:
    """합성음 대신 사용자의 CapCut에 이미 내려받힌 실제 효과음을 연결한다."""
    for event in analysis.sound_events:
        source = sound_resources.get(event.kind)
        if source is None:
            continue
        path = Path(str(source.get("path") or "")).expanduser()
        if not path.is_file():
            continue
        event.asset_path = str(path.resolve())
        event.capcut_sound = str(source.get("name") or path.name)
        event.match_confidence = 0.90 if event.basis == "visual_sync" else 0.82


def add_sound_template_track(script, cc, analysis: Analysis) -> None:
    usable = [
        event for event in analysis.sound_events
        if event.confidence >= 0.68 and event.asset_path and Path(event.asset_path).is_file()
    ]
    if not usable:
        return
    lane_ends: list[float] = []
    for event in usable:
        lane = next((index for index, end in enumerate(lane_ends) if end <= event.start), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(0.0)
            track_name = SFX_TRACK_NAME if lane == 0 else f"{SFX_TRACK_NAME} {lane + 1}"
            script.add_track(cc.TrackType.audio, track_name)
        else:
            track_name = SFX_TRACK_NAME if lane == 0 else f"{SFX_TRACK_NAME} {lane + 1}"
        material_name = event.capcut_sound or f"{event.kind} · 교체가능"
        material = cc.AudioMaterial(event.asset_path, material_name=material_name)
        duration = min(int((event.end - event.start) * cc.SEC), int(material.duration))
        if duration <= 0:
            continue
        segment = cc.AudioSegment(
            material,
            cc.trange(int(event.start * cc.SEC), duration),
            source_timerange=cc.trange(0, duration),
            volume=0.55,
        )
        script.add_segment(segment, track_name)
        lane_ends[lane] = event.start + duration / cc.SEC


def apply_detected_effects(script, cc, analysis: Analysis, usable_kinds: dict) -> list[str]:
    """분석된 화면 효과를 CapCut 효과 트랙에 실제로 걸어둔다."""
    planned = plan_effect_segments(analysis, usable_kinds)
    if not planned:
        return []
    script.add_track(cc.TrackType.effect, EFFECT_TRACK_NAME)
    for effect, start, end in planned:
        script.add_effect(
            cc.VideoSceneEffectType[EFFECT_TYPE_NAMES[effect.kind]],
            cc.trange(int(start * cc.SEC), int((end - start) * cc.SEC)),
            EFFECT_TRACK_NAME,
        )
    return [effect.kind for effect, _start, _end in planned]


def add_title_card(script, cc, title_card: TitleCard | None) -> None:
    """레퍼런스의 도입부 제목 카드를 같은 위치·크기·색으로 깔아둔다.

    문구는 원본을 베끼지 않고 직접 쓰도록 빈 자리로 둔다.
    """
    if title_card is None:
        return
    script.add_track(cc.TrackType.text, TITLE_TRACK_NAME)
    color = (1.0, 1.0, 1.0) if title_card.color.startswith("흰색") else (0.05, 0.05, 0.05)
    style = cc.TextStyle(
        size=round(max(6.0, min(24.0, title_card.height_ratio * 100 * 1.1)), 1),
        bold=True,
        color=color,
        align=1,
    )
    background = None
    if title_card.box_color:
        background = cc.TextBackground(
            color=title_card.box_color, alpha=1.0, round_radius=0.1, height=0.16, width=0.24
        )
    duration = max(0.5, title_card.end - title_card.start)
    segment = cc.TextSegment(
        "여기에 제목을 쓰세요",
        cc.trange(int(title_card.start * cc.SEC), int(duration * cc.SEC)),
        style=style,
        border=cc.TextBorder(alpha=0.9, color=(0.0, 0.0, 0.0), width=30.0),
        background=background,
        clip_settings=cc.ClipSettings(transform_y=round(1.0 - title_card.center_y * 2.0, 4)),
    )
    script.add_segment(segment, TITLE_TRACK_NAME)


def add_caption_track(
    script,
    cc,
    name: str,
    cues: list[Cue],
    *,
    position: float,
    animated: bool,
    reference_style: CaptionStyle | None = None,
) -> None:
    if not cues:
        return
    script.add_track(cc.TrackType.text, name)
    reference_style = reference_style or CaptionStyle()
    size = caption_size(reference_style) if name == "말 자막" else 6.0
    color = caption_color(reference_style) if name == "말 자막" else (1.0, 1.0, 1.0)
    style = cc.TextStyle(size=size, bold=True, color=color, align=1)
    has_outline = name != "말 자막" or "외곽선/그림자" in reference_style.outline or reference_style.outline == "판별 불가"
    border = cc.TextBorder(alpha=0.92, color=(0.0, 0.0, 0.0), width=38.0) if has_outline else None
    background = None
    if name == "말 자막" and "박스 추정" in reference_style.background:
        background = cc.TextBackground(color="#000000", alpha=0.68, round_radius=0.12, height=0.18, width=0.22)
    for cue in cues:
        duration = max(0.1, cue.end - cue.start)
        segment = cc.TextSegment(
            cue.text,
            cc.trange(int(cue.start * cc.SEC), int(duration * cc.SEC)),
            style=style,
            border=border,
            background=background,
            clip_settings=cc.ClipSettings(transform_y=position),
        )
        if animated:
            try:
                segment.add_animation(
                    caption_animation(cc, reference_style),
                    int(min(0.28, duration / 3) * cc.SEC),
                )
            except Exception:
                pass
        script.add_segment(segment, name)


def caption_position(style: CaptionStyle) -> float:
    """화면에서 잰 세로 중심을 CapCut 좌표(위 +1 ~ 아래 -1)로 옮긴다."""
    if style.center_y is not None:
        return round(max(-0.95, min(0.95, 1.0 - style.center_y * 2.0)), 4)
    return {"상단": 0.64, "중앙": 0.0, "하단": -0.72}.get(style.position, -0.72)


def caption_size(style: CaptionStyle) -> float:
    """잰 글자 높이 비율을 CapCut 글자 크기로 옮긴다."""
    if style.height_ratio is not None:
        # 화면 높이의 약 2.6%가 CapCut 크기 6 언저리에 해당한다.
        return round(max(4.0, min(16.0, style.height_ratio * 100 * 2.3)), 1)
    if style.size.startswith("작게"):
        return 6.0
    if style.size.startswith("크게"):
        return 10.0
    return 8.0


def caption_color(style: CaptionStyle) -> tuple[float, float, float]:
    if style.color.startswith("노랑"):
        return (1.0, 0.86, 0.1)
    if style.color.startswith("빨강"):
        return (1.0, 0.18, 0.16)
    if style.color.startswith("파랑"):
        return (0.2, 0.58, 1.0)
    if style.color.startswith("검정"):
        return (0.05, 0.05, 0.05)
    return (1.0, 1.0, 1.0)


def caption_animation(cc, style: CaptionStyle):
    if "슬라이드" in style.animation:
        return cc.TextIntro.向上滑动
    if "페이드" in style.animation:
        return cc.TextIntro.渐显
    if "타자" in style.animation:
        return cc.TextIntro.打字机
    return cc.TextIntro.弹出


def unique_project_name(draft_dir: Path, title: str) -> str:
    stem = safe_slug(title, "릴스분석")
    stamp = datetime.now().strftime("%m%d_%H%M")
    base = f"릴스분석_{stem}_{stamp}"[:90]
    candidate = base
    index = 2
    while (draft_dir / candidate).exists():
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def merge_native_json(native, generated):
    if isinstance(native, dict) and isinstance(generated, dict):
        merged = dict(native)
        for key, value in generated.items():
            merged[key] = merge_native_json(merged.get(key), value)
        return merged
    if (
        isinstance(native, list)
        and isinstance(generated, list)
        and len(native) == len(generated)
        and all(isinstance(value, dict) for value in native + generated)
    ):
        return [merge_native_json(left, right) for left, right in zip(native, generated)]
    return generated


def find_native_template(draft_dir: Path, *, exclude: Path | None = None) -> dict | None:
    candidates: list[tuple[tuple[int, ...], float, dict]] = []
    for project_dir in draft_dir.iterdir():
        if project_dir == exclude or not project_dir.is_dir():
            continue
        current = project_dir / "draft_info.json"
        if not current.is_file():
            continue
        try:
            payload = json.loads(current.read_text(encoding="utf-8"))
            platform = payload.get("last_modified_platform") or payload.get("platform") or {}
            if platform.get("os") == "mac":
                version = tuple(
                    int(part) for part in str(payload.get("new_version", "0")).split(".")
                    if part.isdigit()
                )
                candidates.append((version, current.stat().st_mtime, payload))
        except (OSError, ValueError):
            continue
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def write_macos_compatibility(project_dir: Path, native_template: dict | None) -> None:
    legacy = project_dir / "draft_content.json"
    if not legacy.is_file():
        raise RuntimeError("pycapcut이 draft_content.json을 만들지 못했습니다.")
    generated = json.loads(legacy.read_text(encoding="utf-8"))
    current = merge_native_json(native_template, generated) if native_template else generated
    if native_template:
        for key in ("platform", "last_modified_platform", "new_version", "draft_type"):
            if key in native_template:
                current[key] = deepcopy(native_template[key])
    encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
    (project_dir / "draft_info.json").write_text(encoded, encoding="utf-8")
    (project_dir / "draft_info.json.bak").write_text(encoded, encoding="utf-8")


def retarget_local_resources(
    project_dir: Path,
    applied_kinds: list[str],
    usable_kinds: dict,
    text_intro: dict | None,
    emphasis_intro: dict | None,
    video_group: dict | None,
) -> None:
    """pycapcut이 쓴 중국판 리소스 번호를 이 Mac에서 열리는 번호로 바꿔 끼운다."""
    for filename in ("draft_content.json", "draft_info.json", "draft_info.json.bak"):
        path = project_dir / filename
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        materials = payload.get("materials", {})
        animation_sources: dict[str, dict] = {}
        for track in payload.get("tracks", []) or []:
            name = str(track.get("name", ""))
            source = None
            if name == EMPHASIS_TRACK_NAME:
                source = emphasis_intro
            elif name == "말 자막":
                source = text_intro
            elif name == TEMPLATE_VIDEO_TRACK:
                source = video_group
            if source is None:
                continue
            for segment in track.get("segments", []) or []:
                for reference in segment.get("extra_material_refs", []) or []:
                    animation_sources[reference] = source
        for group in materials.get("material_animations", []) or []:
            source = animation_sources.get(group.get("id"))
            if source is None:
                continue
            for animation in group.get("animations", []) or []:
                if animation.get("type") in {"in", "group"}:
                    copy_resource_fields(animation, source)
        effect_track = next(
            (track for track in payload.get("tracks", []) if track.get("type") == "effect"),
            None,
        )
        if effect_track is not None:
            by_id = {item.get("id"): item for item in materials.get("video_effects", []) or []}
            for segment, kind in zip(effect_track.get("segments", []), applied_kinds):
                material = by_id.get(segment.get("material_id"))
                if material is not None and kind in usable_kinds:
                    material_id = material.get("id")
                    copy_resource_fields(material, usable_kinds[kind])
                    # The segment points at this per-draft material ID.  The
                    # downloaded effect's resource_id may change, but replacing
                    # the material ID itself leaves a dangling, disabled segment.
                    material["id"] = material_id
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )


def postprocess_helper_tracks(project_dir: Path) -> None:
    for filename in ("draft_content.json", "draft_info.json", "draft_info.json.bak"):
        path = project_dir / filename
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for track in payload.get("tracks", []):
            name = str(track.get("name", ""))
            if name.startswith(MEMO_TRACK_PREFIXES):
                track["attribute"] = 1
                for segment in track.get("segments", []):
                    segment["visible"] = False
                    segment["track_attribute"] = 1
                    if name.startswith("참고 원본"):
                        material = next(
                            (
                                item for item in payload.get("materials", {}).get("videos", [])
                                if item.get("id") == segment.get("material_id")
                            ),
                            None,
                        )
                        if material is not None:
                            material["object_locked"] = {
                                "id": uuid.uuid4().hex,
                                "locked": True,
                                "type": "lock",
                            }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        path.write_text(encoded, encoding="utf-8")


def update_meta_info(
    project_dir: Path,
    project_name: str,
    duration: float,
    native_template: dict | None,
) -> None:
    meta_path = project_dir / "draft_meta_info.json"
    info_path = project_dir / "draft_info.json"
    if not meta_path.is_file() or not info_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    info = json.loads(info_path.read_text(encoding="utf-8"))
    meta.update(
        {
            "draft_id": info.get("id", meta.get("draft_id", "")),
            "draft_name": project_name,
            "draft_fold_path": str(project_dir),
            "draft_json_file": str(info_path),
            "draft_root_path": str(project_dir.parent),
            "draft_new_version": info.get("new_version", ""),
            "tm_duration": int(duration * 1_000_000),
            "draft_timeline_materials_size_": info_path.stat().st_size,
        }
    )
    if native_template:
        meta["draft_new_version"] = native_template.get("new_version", meta["draft_new_version"])
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
