"""이 Mac의 CapCut이 실제로 가진 효과·애니메이션만 골라 쓰기 위한 조회 도구.

pycapcut이 들고 있는 리소스 번호는 중국판 剪映 기준이라 해외판 CapCut에서는
"애니메이션 분실"로 뜬다. 그래서 사용자가 이미 만든 CapCut 프로젝트를 훑어
실제로 열리는 리소스만 수집해 두고, 그 안에서만 골라 쓴다.
"""

from __future__ import annotations

import json
from pathlib import Path


# 검출한 화면 효과별로 받아들일 수 있는 CapCut 효과 이름. 앞쪽이 우선이다.
# 눈에 보이는 현상이 실제로 같은 것만 넣는다. 비슷해 보인다고 다른 효과를 대신 넣지 않는다.
EFFECT_CANDIDATES = {
    "zoom_in": ["렌즈 줌", "줌 인", "빠른 줌", "Lens Zoom", "Zoom In"],
    "zoom_out": ["렌즈 줌", "줌 아웃", "Zoom Out"],
    "shake": ["흔들림", "쉐이크", "카메라 흔들림", "지터", "Shake", "Jitter"],
    "flash": ["화이트 플래시", "플래시", "번쩍임", "White Flash", "Flash"],
    "blur": ["동적 블러", "블러", "Blur", "Motion Blur"],
}

TEXT_INTRO_CANDIDATES = {
    "슬라이드": ["슬라이드 인", "플라이 인", "Slide In"],
    "페이드": ["글로우 페이드 인", "페이드 인", "Fade In"],
    "타자": ["중앙 타이핑 인", "타자기", "Typewriter"],
    "타이핑": ["중앙 타이핑 인", "타자기", "Typewriter"],
    "글로우": ["글로우 페이드 인", "클라우드 나인", "Glow Fade In"],
}

TEXT_INTRO_FALLBACK = ["팝 업", "슬라이드 인", "플라이 인", "서라운드 인", "Pop"]

VIDEO_GROUP_CANDIDATES = {
    "punch_in": ["줌 2", "왼쪽 줌", "줌", "Zoom 2", "Zoom"],
    "shake": ["동적 흔들림", "흔들림", "Shake"],
}

# 사용자가 CapCut에서 이미 내려받은 실제 효과음 우선순위. 합성음보다 먼저 쓴다.
# 이름은 지역/언어에 따라 달라지므로 부분 문자열로도 매칭한다.
SOUND_CANDIDATES = {
    "sparkle": [
        "[Kirarin] Cute glitter chime", "Glitter glitter", "Glitter sound",
        "glitter", "sparkle", "chime", "반짝",
    ],
    "pop": ["Bubble 03", "Point", "Picon", "bubble", "pop", "버블"],
    "click": ["Click_Mouse_Click_02", "鼠标单击1", "鼠标点击声合集", "click", "클릭"],
    "impact": ["Finger_Snap", "Point", "Picon", "impact", "snap"],
    "whoosh": ["Something rolls", "whoosh", "swoosh", "휘익"],
}


def effect_cache_dir(draft_dir: Path) -> Path:
    """CapCut이 내려받은 리소스를 보관하는 폴더. `.../User Data/Cache/effect`."""
    return draft_dir.parent.parent / "Cache" / "effect"


def harvest_local_resources(draft_dir: Path, *, exclude: Path | None = None) -> dict[str, dict]:
    """사용자의 기존 CapCut 프로젝트에서 실제로 열리는 리소스 항목을 모은다."""
    found: dict[str, dict] = {
        "text_in": {}, "text_loop": {}, "text_out": {},
        "video_group": {}, "video_effect": {}, "sound": {},
    }
    if not draft_dir.is_dir():
        return found
    cache = effect_cache_dir(draft_dir)

    def downloaded(resource_id: str | None) -> bool:
        # 이 Mac에 실제로 내려받힌 리소스만 인정한다.
        # 우리가 만든 초안이 남긴 잘못된 번호를 다시 주워오지 않기 위한 관문이기도 하다.
        return bool(resource_id) and (cache / str(resource_id)).is_dir()
    for project_dir in draft_dir.iterdir():
        info = project_dir / "draft_info.json"
        if project_dir == exclude or not info.is_file():
            continue
        try:
            payload = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        materials = payload.get("materials", {})
        for group in materials.get("material_animations", []) or []:
            for animation in group.get("animations", []) or []:
                name = animation.get("name")
                if animation.get("type") == "in" and name and downloaded(animation.get("resource_id")):
                    found["text_in"].setdefault(name, animation)
                if animation.get("type") == "loop" and name and downloaded(animation.get("resource_id")):
                    found["text_loop"].setdefault(name, animation)
                if animation.get("type") == "out" and name and downloaded(animation.get("resource_id")):
                    found["text_out"].setdefault(name, animation)
                if animation.get("type") == "group" and name and downloaded(animation.get("resource_id")):
                    found["video_group"].setdefault(name, animation)
        for effect in materials.get("video_effects", []) or []:
            name = effect.get("name")
            if name and downloaded(effect.get("resource_id")):
                found["video_effect"].setdefault(name, effect)
        for audio in materials.get("audios", []) or []:
            path = Path(str(audio.get("path") or "")).expanduser()
            name = str(audio.get("name") or audio.get("material_name") or path.name).strip()
            if name and path.is_file() and audio.get("type") in {"sound", "music"}:
                found["sound"].setdefault(name, {**audio, "path": str(path.resolve())})
    return found


def pick(available: dict, candidates: list[str]) -> dict | None:
    for name in candidates:
        if name in available:
            return available[name]
    return None


def available_effect_kinds(resources: dict[str, dict]) -> dict[str, dict]:
    """검출 종류별로 이 Mac에서 실제로 걸 수 있는 효과 항목을 돌려준다."""
    catalog = resources.get("video_effect", {})
    usable = {}
    for kind, candidates in EFFECT_CANDIDATES.items():
        entry = pick(catalog, candidates)
        if entry is not None:
            usable[kind] = entry
    return usable


def pick_text_intro(resources: dict[str, dict], animation_hint: str) -> dict | None:
    """레퍼런스에서 실제로 읽어낸 등장 움직임에 맞는 애니메이션을 고른다.

    움직임을 판별하지 못했으면 아무것도 고르지 않는다. 원본에 없는 애니메이션을
    24개 자막에 똑같이 씌우면 레퍼런스와 다른 영상이 되기 때문이다.
    """
    catalog = resources.get("text_in", {})
    if "판별" in animation_hint:
        return None
    for keyword, candidates in TEXT_INTRO_CANDIDATES.items():
        if keyword in animation_hint:
            return pick(catalog, candidates)
    if "팝업" in animation_hint or "확대" in animation_hint or "pop" in animation_hint.lower():
        return pick(catalog, TEXT_INTRO_FALLBACK)
    return None


def pick_video_group(resources: dict[str, dict], kind: str) -> dict | None:
    return pick(resources.get("video_group", {}), VIDEO_GROUP_CANDIDATES.get(kind, []))


def pick_sound(resources: dict[str, dict], kind: str) -> dict | None:
    """효과음 종류에 가장 가까운, 이 Mac에서 실제 재생되는 CapCut 음원을 고른다."""
    catalog = resources.get("sound", {})
    candidates = SOUND_CANDIDATES.get(kind, [])
    exact = pick(catalog, candidates)
    if exact is not None:
        return exact
    lowered = [(name.lower(), item) for name, item in catalog.items()]
    for candidate in candidates:
        needle = candidate.lower()
        for name, item in lowered:
            if needle in name:
                return item
    return None


def copy_resource_fields(target: dict, source: dict) -> None:
    """리소스를 가리키는 필드만 이 Mac에서 열리는 값으로 바꾼다."""
    for key in (
        "resource_id", "third_resource_id", "path", "name",
        "category_id", "category_name", "source_platform", "effect_id",
    ):
        if key in source:
            target[key] = source[key]
    if "resource_id" in source:
        target["id"] = source["resource_id"]
