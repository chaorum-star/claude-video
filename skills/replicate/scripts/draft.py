#!/usr/bin/env python3
"""Generate a native-structure CapCut draft with animated subtitles (SPEC M3).

CapCut 9.x (macOS) requires the native ``Timelines`` project structure — the
old single ``draft_content.json`` layout registers in the project list but
silently fails to open (verified on-device 2026-08-26, D-1). This generator
follows the approach proven by the codex branch's ``capcut_native.py``:

  1. clone a CapCut-authored native project as a structural shell
     (CapCut-owned sidecars, schema defaults, current ``new_version``),
  2. replace its timeline payload with our subtitle track — text materials and
     ``material_animations`` referencing effect ids from the local UI catalog
     (capcut-ui-catalog.json), so every animation is one the user's own CapCut
     actually ships,
  3. rewrite ids/metadata so CapCut lists it as a fresh project.

The source shell project is never modified. Text styling/segment shapes come
from the M3 spike's pycapcut output (spike/out/m3-spike) — kept inline here as
templates so the skill stays self-contained.

Usage:
    draft.py create --name <project-name> --subtitles '<json>' [--draft-dir DIR]
        # subtitles: [{"text": "...", "start": 0.0, "end": 2.5,
        #              "in": "Wiping In", "out": "Fade Out", "loop": null}, ...]
        #  in/out/loop are display titles from capcut-ui-catalog.json (optional)
    draft.py validate <project-dir>
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path
from shutil import copytree

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
UI_CATALOG_PATH = ASSETS_DIR / "capcut-ui-catalog.json"
MANIFEST_NAME = "replicate_manifest.json"
DEFAULT_DRAFT_DIRS = [
    "~/Library/Containers/com.lemon.lvoverseas/Data/Movies/CapCut/User Data/Projects/com.lveditor.draft",
    "~/Movies/CapCut/User Data/Projects/com.lveditor.draft",
]
ANIM_DURATION_US = 500_000
PHASE_TO_CATEGORY = {"in": "In", "out": "Out", "loop": "Loop"}

# pycapcut-shape templates (from spike/out/m3-spike, fields CapCut accepts).
TEXT_MATERIAL_TEMPLATE = {
    "alignment": 1, "check_flag": 7, "force_apply_line_max_width": False,
    "global_alpha": 1.0, "letter_spacing": 0.0, "line_feed": 1,
    "line_max_width": 0.82, "line_spacing": 0.02, "type": "text",
    "typesetting": 0,
}
TEXT_STYLE_TEMPLATE = {
    "fill": {"alpha": 1.0, "content": {"render_type": "solid",
             "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}}},
    "range": [0, 1], "size": 15.0, "bold": True, "italic": False,
    "underline": False,
    "strokes": [{"content": {"solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}},
                 "width": 0.08}],
}
TEXT_SEGMENT_TEMPLATE = {
    "clip": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
             "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
             "transform": {"x": 0.0, "y": -0.7}},
    "common_keyframes": [], "enable_adjust": False,
    "enable_color_correct_adjust": False, "enable_color_curves": True,
    "enable_color_match_adjust": False, "enable_color_wheels": True,
    "enable_lut": False, "enable_smart_color_adjust": False,
    "keyframe_refs": [], "last_nonzero_volume": 1.0, "render_index": 14000,
    "reverse": False, "source_timerange": None, "speed": 1.0,
    "track_attribute": 0, "track_render_index": 1, "uniform_scale": None,
    "visible": True, "volume": 1.0,
}
TEXT_TRACK_TEMPLATE = {
    "attribute": 0, "flag": 0, "is_default_name": True, "name": "", "type": "text",
}

AUDIO_TRACK_TEMPLATE = {
    "attribute": 0, "flag": 0, "is_default_name": True, "name": "", "type": "audio",
}
AUDIO_SEGMENT_TEMPLATE = {
    "clip": None, "hdr_settings": None, "common_keyframes": [], "keyframe_refs": [],
    "enable_adjust": True, "enable_color_correct_adjust": False,
    "enable_color_curves": True, "enable_color_match_adjust": False,
    "enable_color_wheels": True, "enable_lut": True,
    "enable_smart_color_adjust": False, "last_nonzero_volume": 1.0,
    "render_index": 0, "reverse": False, "speed": 1.0,
    "track_attribute": 0, "track_render_index": 0, "visible": True, "volume": 1.0,
}
SFX_DIR_NAME = "replicate_sfx"


def probe_audio_duration(path: Path) -> float:
    """Clip length in seconds via ffprobe (needed for source/target timeranges)."""
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
         str(path.resolve())],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed for {path}: {result.stderr.strip()}")
    duration = float(json.loads(result.stdout or "{}").get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise SystemExit(f"오디오 길이를 읽지 못했습니다: {path}")
    return duration


def make_audio_material(name: str, dest_path: str, duration_us: int) -> dict:
    material_id = uuid.uuid4().hex
    return {
        "app_id": 0, "category_id": "", "category_name": "local",
        "check_flag": 3, "copyright_limit_type": "none",
        "duration": duration_us, "effect_id": "", "formula_id": "",
        "id": material_id, "local_material_id": material_id,
        "music_id": material_id, "name": name, "path": dest_path,
        "source_platform": 0, "type": "extract_music", "wave_points": [],
    }


def make_speed_material() -> dict:
    return {"curve_speed": None, "id": uuid.uuid4().hex, "mode": 0,
            "speed": 1.0, "type": "speed"}


def read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON 객체가 아닙니다: {path}")
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


def find_draft_dir(override: str | None) -> Path:
    roots = [override] if override else DEFAULT_DRAFT_DIRS
    for root in roots:
        path = Path(root).expanduser()
        if path.is_dir():
            return path
    raise SystemExit("캡컷 드래프트 폴더를 찾지 못했습니다 — 캡컷이 설치·실행된 적 있나요?")


def native_timeline(project_dir: Path) -> tuple[str, Path, dict] | None:
    project_path = project_dir / "Timelines" / "project.json"
    root_info = project_dir / "draft_info.json"
    if not project_path.is_file() or not root_info.is_file():
        return None
    try:
        project = read_json(project_path)
        timeline_id = str(project.get("main_timeline_id") or "").strip()
        nested = read_json(project_dir / "Timelines" / timeline_id / "draft_info.json")
        root = read_json(root_info)
    except (OSError, ValueError, json.JSONDecodeError, SystemExit):
        return None
    if not timeline_id or root.get("id") != timeline_id or nested.get("id") != timeline_id:
        return None
    return timeline_id, project_dir / "Timelines" / timeline_id / "draft_info.json", project


def find_shell_project(draft_dir: Path, shell_name: str | None = None) -> Path:
    """Pick a CapCut-authored native project to clone as the structural shell."""
    if shell_name:
        candidate = draft_dir / shell_name
        if native_timeline(candidate) is None:
            raise SystemExit(f"{shell_name}은(는) 네이티브 캡컷 프로젝트가 아닙니다.")
        return candidate
    candidates = []
    for project_dir in draft_dir.iterdir():
        if not project_dir.is_dir() or (project_dir / MANIFEST_NAME).exists():
            continue
        if native_timeline(project_dir) is not None:
            candidates.append((project_dir.stat().st_mtime, project_dir))
    if not candidates:
        raise SystemExit(
            "셸로 쓸 네이티브 캡컷 프로젝트가 없습니다. 캡컷에서 빈 프로젝트를 하나 만든 뒤 재실행하세요."
        )
    return max(candidates)[1]


def load_ui_catalog() -> dict:
    return read_json(UI_CATALOG_PATH)


def resolve_animation(catalog: dict, phase: str, title: str) -> dict:
    category = PHASE_TO_CATEGORY[phase]
    for entry in catalog["text_animations"].get(category, []):
        if entry["title"] == title:
            return entry
    raise SystemExit(
        f"{category} 카테고리에 {title!r} 효과가 없습니다 — capcut-ui-catalog.json의 표시명을 그대로 쓰세요."
    )


def make_text_material(text: str) -> dict:
    style = deepcopy(TEXT_STYLE_TEMPLATE)
    style["range"] = [0, len(text)]
    material = deepcopy(TEXT_MATERIAL_TEMPLATE)
    material["id"] = uuid.uuid4().hex
    material["content"] = json.dumps({"styles": [style], "text": text}, ensure_ascii=False)
    return material


def _anim_entry(entry: dict, phase: str, start_us: int, duration_us: int) -> dict:
    """One animation entry in the shape native CapCut 9.3 writes (역추출 2026-08-26).

    ``path``는 의도적으로 빈 값 — 로컬 효과 캐시 경로는 이 기기에서 해당 효과를
    받아본 적이 있어야 존재하고, 캡컷이 드래프트를 열 때 resource_id로 알아서
    내려받아 채워준다. 그 전까지 세그먼트에 "애니메이션 분실" 경고가 떠 있는 것은
    정상이며, 세그먼트를 선택하거나 재생하면 해소된다 (실기기 확인).
    """
    return {
        "id": entry["effect_id"], "type": phase,
        "start": start_us, "duration": duration_us,
        "path": "", "platform": "all",
        "resource_id": entry["resource_id"], "third_resource_id": "",
        "source_platform": 0, "name": entry["title"],
        "category_id": "", "category_name": "", "panel": "",
        "material_type": "sticker", "anim_adjust_params": None,
        "request_id": "",
    }


def make_animation_material(
    catalog: dict, duration_us: int,
    in_title: str | None, out_title: str | None, loop_title: str | None,
) -> dict | None:
    animations = []
    if in_title:
        entry = resolve_animation(catalog, "in", in_title)
        animations.append(_anim_entry(entry, "in", 0, min(ANIM_DURATION_US, duration_us)))
    if loop_title:
        entry = resolve_animation(catalog, "loop", loop_title)
        start = ANIM_DURATION_US if in_title else 0
        end_reserved = ANIM_DURATION_US if out_title else 0
        animations.append(
            _anim_entry(entry, "loop", start, max(0, duration_us - start - end_reserved)))
    if out_title:
        entry = resolve_animation(catalog, "out", out_title)
        animations.append(_anim_entry(
            entry, "out", max(0, duration_us - ANIM_DURATION_US),
            min(ANIM_DURATION_US, duration_us)))
    if not animations:
        return None
    return {
        "id": uuid.uuid4().hex, "type": "sticker_animation",
        "multi_language_current": "none", "animations": animations,
    }


def build_payload(shell_payload: dict, catalog: dict, subtitles: list[dict],
                  sfx: list[dict] | None = None) -> tuple[dict, float]:
    """Shell schema + our text track. Every list in materials is emptied first
    so nothing from the shell's own edit survives; only 자막이 남는다."""
    payload = deepcopy(shell_payload)
    materials = payload.get("materials")
    if not isinstance(materials, dict):
        raise SystemExit("셸 프로젝트에 materials 구조가 없습니다.")
    for key, value in materials.items():
        if isinstance(value, list):
            materials[key] = []
    materials.setdefault("texts", [])
    materials.setdefault("material_animations", [])

    track = deepcopy(TEXT_TRACK_TEMPLATE)
    track["id"] = uuid.uuid4().hex
    track["segments"] = []

    total_end = 0.0
    for sub in subtitles:
        text = str(sub["text"])
        start_us = int(float(sub["start"]) * 1_000_000)
        duration_us = int((float(sub["end"]) - float(sub["start"])) * 1_000_000)
        if duration_us <= 0:
            raise SystemExit(f"자막 길이가 0 이하입니다: {sub!r}")
        total_end = max(total_end, float(sub["end"]))

        text_material = make_text_material(text)
        materials["texts"].append(text_material)
        refs = []
        anim = make_animation_material(
            catalog, duration_us, sub.get("in"), sub.get("out"), sub.get("loop"),
        )
        if anim is not None:
            materials["material_animations"].append(anim)
            refs.append(anim["id"])

        segment = deepcopy(TEXT_SEGMENT_TEMPLATE)
        segment["id"] = uuid.uuid4().hex
        segment["material_id"] = text_material["id"]
        segment["target_timerange"] = {"start": start_us, "duration": duration_us}
        segment["extra_material_refs"] = refs
        track["segments"].append(segment)

    tracks = [track]

    # F4-3 배치: 검출·매칭된 효과음 클립을 오디오 트랙에 상대 시각 그대로 놓는다.
    # ``dest_path``는 install()이 프로젝트 안(Resources/replicate_sfx/)으로 복사할
    # 최종 경로 — 원본이 임시 폴더에 있어도 드래프트는 자립적으로 유지된다.
    if sfx:
        materials.setdefault("audios", [])
        materials.setdefault("speeds", [])
        audio_track = deepcopy(AUDIO_TRACK_TEMPLATE)
        audio_track["id"] = uuid.uuid4().hex
        audio_track["segments"] = []
        for item in sfx:
            start_us = int(float(item["time"]) * 1_000_000)
            duration_us = int(float(item["duration"]) * 1_000_000)
            material = make_audio_material(
                item.get("name") or Path(item["path"]).stem,
                item["dest_path"], duration_us,
            )
            materials["audios"].append(material)
            speed = make_speed_material()
            materials["speeds"].append(speed)
            segment = deepcopy(AUDIO_SEGMENT_TEMPLATE)
            segment["id"] = uuid.uuid4().hex
            segment["material_id"] = material["id"]
            segment["source_timerange"] = {"start": 0, "duration": duration_us}
            segment["target_timerange"] = {"start": start_us, "duration": duration_us}
            segment["extra_material_refs"] = [speed["id"]]
            audio_track["segments"].append(segment)
            total_end = max(total_end, float(item["time"]) + float(item["duration"]))
        tracks.append(audio_track)

    payload["tracks"] = tracks
    payload["duration"] = int(total_end * 1_000_000)
    return payload, total_end


def _replace_ids_in_tree(project_dir: Path, replacements: dict[str, str]) -> None:
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        if path.suffix not in {".json", ".bak"} and path.name != "draft_settings":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = text
        for old, new in replacements.items():
            if old:
                updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def install(shell_project: Path, target_dir: Path, payload: dict, name: str, duration: float,
            sfx_copies: list[tuple[Path, Path]] | None = None) -> dict:
    if target_dir.exists():
        raise SystemExit(f"이미 존재하는 프로젝트입니다: {target_dir.name}")
    copytree(shell_project, target_dir)
    for src, dst in sfx_copies or []:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    timeline = native_timeline(target_dir)
    if timeline is None:
        raise SystemExit("복제한 네이티브 프로젝트 구조를 읽지 못했습니다.")
    old_timeline_id, _nested, project = timeline
    old_project_id = str(project.get("id") or "")
    timeline_id = str(uuid.uuid4()).upper()
    project_id = str(uuid.uuid4()).upper()
    (target_dir / "Timelines" / old_timeline_id).rename(target_dir / "Timelines" / timeline_id)
    _replace_ids_in_tree(target_dir, {old_timeline_id: timeline_id, old_project_id: project_id})

    now_us = int(time.time() * 1_000_000)
    project_path = target_dir / "Timelines" / "project.json"
    project = read_json(project_path)
    project.update({"id": project_id, "main_timeline_id": timeline_id,
                    "create_time": now_us, "update_time": now_us})
    for item in project.get("timelines") or []:
        if isinstance(item, dict):
            item.update({"id": timeline_id, "create_time": now_us, "update_time": now_us})
    write_json(project_path, project)
    write_json(target_dir / "Timelines" / "project.json.bak", project)

    payload = deepcopy(payload)
    payload["id"] = timeline_id
    for path in (
        target_dir / "draft_info.json",
        target_dir / "draft_info.json.bak",
        target_dir / "Timelines" / timeline_id / "draft_info.json",
        target_dir / "Timelines" / timeline_id / "draft_info.json.bak",
    ):
        write_json(path, payload)

    meta_path = target_dir / "draft_meta_info.json"
    meta = read_json(meta_path)
    meta.update({
        "draft_id": str(uuid.uuid4()).upper(),
        "draft_name": name,
        "draft_fold_path": str(target_dir),
        "draft_root_path": str(target_dir.parent),
        "draft_new_version": payload.get("new_version", ""),
        "draft_need_rename_folder": False,
        "draft_is_invisible": False,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_duration": int(duration * 1_000_000),
        "draft_timeline_materials_size_": (target_dir / "draft_info.json").stat().st_size,
    })
    write_json(meta_path, meta)
    # The shell's cover/thumbnail stays — harmless, CapCut regenerates on save.
    validation = validate_project(target_dir)
    write_json(target_dir / MANIFEST_NAME, {
        "generator": "claude-video/replicate", "schema": 1,
        "shell_project": shell_project.name,
        "runtime_status": "not_checked",
        "validation": validation,
    })
    return validation


def validate_project(project_dir: Path) -> dict:
    """Structural checks mirroring the codex branch's validator (no CapCut run)."""
    blockers: list[str] = []
    timeline = native_timeline(project_dir)
    if timeline is None:
        return {"status": "invalid", "blockers": ["네이티브 Timelines 구조가 없습니다."]}
    timeline_id, nested_path, _project = timeline
    root = read_json(project_dir / "draft_info.json")
    nested = read_json(nested_path)
    if root != nested:
        blockers.append("루트와 Timelines 내부 편집 데이터가 다릅니다.")
    materials = root.get("materials") if isinstance(root.get("materials"), dict) else {}
    known_ids = {
        str(item.get("id"))
        for values in materials.values() if isinstance(values, list)
        for item in values if isinstance(item, dict) and item.get("id")
    }
    editable = 0
    dangling: set[str] = set()
    for track in root.get("tracks") or []:
        for segment in track.get("segments") or []:
            if bool(segment.get("visible", True)):
                editable += 1
            if str(segment.get("material_id") or "") not in known_ids:
                dangling.add(str(segment.get("material_id")))
            for ref in segment.get("extra_material_refs") or []:
                if str(ref) not in known_ids:
                    dangling.add(str(ref))
    if editable == 0:
        blockers.append("세그먼트가 없습니다.")
    if dangling:
        blockers.append(f"연결되지 않은 소재 참조 {len(dangling)}개: {sorted(dangling)[:3]}")
    return {
        "status": "structure_ready" if not blockers else "invalid",
        "timeline_id": timeline_id,
        "track_count": len(root.get("tracks") or []),
        "segment_count": editable,
        "blockers": blockers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a native CapCut draft with animated subtitles.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--subtitles", required=True,
                          help='JSON list: [{"text","start","end","in","out","loop"}]')
    p_create.add_argument("--sfx", default=None,
                          help='JSON list: [{"path","time","duration"(옵션),"name"(옵션)}] — '
                               '효과음 클립을 오디오 트랙의 해당 시각에 배치')
    p_create.add_argument("--draft-dir", default=None)
    p_create.add_argument("--shell", default=None, help="셸로 쓸 기존 프로젝트 폴더명 (기본: 자동 선택)")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("project_dir", type=Path)

    args = parser.parse_args()
    if args.cmd == "validate":
        print(json.dumps(validate_project(args.project_dir), ensure_ascii=False, indent=2))
        return

    subtitles = json.loads(args.subtitles)
    if not isinstance(subtitles, list) or not subtitles:
        raise SystemExit("--subtitles는 비어있지 않은 JSON 리스트여야 합니다.")
    draft_dir = find_draft_dir(args.draft_dir)
    shell = find_shell_project(draft_dir, args.shell)
    target = draft_dir / args.name

    sfx = json.loads(args.sfx) if args.sfx else []
    sfx_copies: list[tuple[Path, Path]] = []
    for item in sfx:
        src = Path(item["path"]).expanduser()
        if not src.is_file():
            raise SystemExit(f"효과음 파일이 없습니다: {src}")
        if "duration" not in item:
            item["duration"] = round(probe_audio_duration(src), 3)
        dst = target / "Resources" / SFX_DIR_NAME / src.name
        item["dest_path"] = str(dst)
        sfx_copies.append((src, dst))

    payload, duration = build_payload(
        read_json(shell / "draft_info.json"), load_ui_catalog(), subtitles, sfx=sfx)
    validation = install(shell, target, payload, args.name, duration, sfx_copies=sfx_copies)
    print(json.dumps({
        "project": str(target), "shell": shell.name, "duration_s": duration,
        "validation": validation,
        "note": "캡컷을 재시작하면 프로젝트 목록에 나타납니다. 열림·재생 검증 전까지 runtime_status=not_checked.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
