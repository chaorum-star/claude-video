from __future__ import annotations

import json
import time
import uuid
from copy import deepcopy
from pathlib import Path
from shutil import copytree
from typing import Any


GENERATED_PROJECT_PREFIX = "릴스분석_"
MANIFEST_NAME = "reels_to_capcut_manifest.json"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
PRIMARY_MATERIAL_KEYS = ("videos", "audios", "texts", "video_effects")


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 객체가 아닙니다: {path}")
    return payload


def version_tuple(value: Any) -> tuple[int, ...]:
    return tuple(int(part) for part in str(value or "0").split(".") if part.isdigit())


def native_timeline(project_dir: Path) -> tuple[str, Path, dict[str, Any]] | None:
    project_path = project_dir / "Timelines" / "project.json"
    root_info = project_dir / "draft_info.json"
    if not project_path.is_file() or not root_info.is_file():
        return None
    try:
        project = read_json(project_path)
        timeline_id = str(project.get("main_timeline_id") or "").strip()
        nested_info = project_dir / "Timelines" / timeline_id / "draft_info.json"
        root = read_json(root_info)
        nested = read_json(nested_info)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not timeline_id or root.get("id") != timeline_id or nested.get("id") != timeline_id:
        return None
    return timeline_id, nested_info, project


def native_projects(draft_dir: Path, *, exclude: Path | None = None) -> list[Path]:
    projects: list[Path] = []
    if not draft_dir.is_dir():
        return projects
    for project_dir in draft_dir.iterdir():
        if not project_dir.is_dir() or project_dir == exclude:
            continue
        if project_dir.name.startswith((".", GENERATED_PROJECT_PREFIX)):
            continue
        if native_timeline(project_dir) is not None:
            projects.append(project_dir)
    return projects


def find_native_project(
    draft_dir: Path,
    width: int,
    height: int,
    *,
    exclude: Path | None = None,
) -> Path | None:
    """Pick a small, current, native CapCut project as the structural shell.

    A native shell is used only for CapCut-owned sidecars and schema defaults.
    Its timeline contents are replaced; the source project is never modified.
    """
    candidates: list[tuple[tuple[Any, ...], Path]] = []
    wanted_portrait = height >= width
    for project_dir in native_projects(draft_dir, exclude=exclude):
        try:
            payload = read_json(project_dir / "draft_info.json")
            canvas = payload.get("canvas_config") if isinstance(payload.get("canvas_config"), dict) else {}
            candidate_width = int(canvas.get("width") or 0)
            candidate_height = int(canvas.get("height") or 0)
            same_orientation = (candidate_height >= candidate_width) == wanted_portrait
            track_count = len(payload.get("tracks") or [])
            score = (
                1 if same_orientation else 0,
                version_tuple(payload.get("new_version")),
                -track_count,
                (project_dir / "draft_info.json").stat().st_mtime,
            )
            candidates.append((score, project_dir))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def collect_schema_prototypes(draft_dir: Path) -> dict[str, Any]:
    """Collect one CapCut-authored track/segment/material example per type."""
    library: dict[str, Any] = {"tracks": {}, "segments": {}, "materials": {}, "supports": {}}
    ordered = sorted(
        native_projects(draft_dir),
        key=lambda path: (
            version_tuple(read_json(path / "draft_info.json").get("new_version")),
            (path / "draft_info.json").stat().st_mtime,
        ),
        reverse=True,
    )
    for project_dir in ordered:
        try:
            payload = read_json(project_dir / "draft_info.json")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        materials = payload.get("materials") if isinstance(payload.get("materials"), dict) else {}
        material_index: dict[str, tuple[str, dict[str, Any]]] = {}
        for key, values in materials.items():
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict) and item.get("id"):
                    material_index[str(item["id"])] = (key, item)
                    if key in PRIMARY_MATERIAL_KEYS and key not in library["materials"]:
                        library["materials"][key] = deepcopy(item)
        for track in payload.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            track_type = str(track.get("type") or "")
            segments = [item for item in track.get("segments") or [] if isinstance(item, dict)]
            if not track_type or not segments or track_type in library["segments"]:
                continue
            track_copy = deepcopy(track)
            track_copy["segments"] = []
            library["tracks"][track_type] = track_copy
            library["segments"][track_type] = deepcopy(segments[0])
            support_items: list[tuple[str, dict[str, Any]]] = []
            for reference in segments[0].get("extra_material_refs") or []:
                resolved = material_index.get(str(reference))
                if resolved is not None:
                    support_items.append((resolved[0], deepcopy(resolved[1])))
            library["supports"][track_type] = support_items
    return library


def merge_native_json(native: Any, generated: Any) -> Any:
    if isinstance(native, dict) and isinstance(generated, dict):
        merged = deepcopy(native)
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
    return deepcopy(generated)


def hydrate_native_payload(
    generated: dict[str, Any],
    shell_payload: dict[str, Any],
    prototypes: dict[str, Any],
) -> dict[str, Any]:
    """Fill pycapcut's sparse objects with fields CapCut itself normally writes."""
    payload = merge_native_json(shell_payload, generated)
    materials = payload.setdefault("materials", {})
    for key, prototype in prototypes.get("materials", {}).items():
        values = materials.get(key)
        if not isinstance(values, list):
            continue
        hydrated = []
        for item in values:
            current = merge_native_json(prototype, item) if isinstance(item, dict) else item
            if isinstance(current, dict) and key in {"videos", "audios"}:
                path = Path(str(current.get("path") or ""))
                current["check_flag"] = int(prototype.get("check_flag") or 62978047)
                current["is_ai_generate_content"] = False
                current["local_material_id"] = (
                    str(uuid.uuid4()) if key == "audios" or path.suffix.lower() in VIDEO_SUFFIXES else ""
                )
                if isinstance(current.get("object_locked"), dict):
                    current["object_locked"]["locked"] = False
            hydrated.append(current)
        materials[key] = hydrated

    def material_index() -> dict[str, str]:
        index: dict[str, str] = {}
        for key, values in materials.items():
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict) and item.get("id"):
                        index[str(item["id"])] = key
        return index

    for track in payload.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        track_type = str(track.get("type") or "")
        original_segments = [item for item in track.get("segments") or [] if isinstance(item, dict)]
        track_proto = prototypes.get("tracks", {}).get(track_type)
        if track_proto:
            merged_track = merge_native_json(track_proto, track)
            track.clear()
            track.update(merged_track)
        hydrated_segments = []
        for original in original_segments:
            segment_proto = prototypes.get("segments", {}).get(track_type)
            segment = merge_native_json(segment_proto, original) if segment_proto else deepcopy(original)
            index = material_index()
            # pycapcut occasionally leaves a reference ID without the corresponding
            # material object.  Keeping that dangling ID makes current CapCut load
            # the segment as incomplete, so only preserve resolvable references.
            existing_refs = [
                str(value) for value in original.get("extra_material_refs") or []
                if str(value) in index
            ]
            existing_collections = {index[ref] for ref in existing_refs if ref in index}
            for collection, support_proto in prototypes.get("supports", {}).get(track_type, []):
                if collection in existing_collections:
                    continue
                support = deepcopy(support_proto)
                support_id = str(uuid.uuid4()).upper()
                support["id"] = support_id
                if collection == "material_animations":
                    support["animations"] = []
                materials.setdefault(collection, []).append(support)
                existing_refs.append(support_id)
                existing_collections.add(collection)
            segment["extra_material_refs"] = existing_refs
            if track_type == "text":
                segment["enable_adjust"] = False
                segment["enable_lut"] = False
                segment["hdr_settings"] = None
            segment["track_attribute"] = int(original.get("track_attribute") or 0)
            segment["visible"] = bool(original.get("visible", True))
            hydrated_segments.append(segment)
        track["segments"] = hydrated_segments
    return payload


def _replace_text_ids(project_dir: Path, replacements: dict[str, str]) -> None:
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        if path.suffix not in {".json", ".bak"} and path.name not in {"draft_settings"}:
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _draft_materials(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = [{"type": value, "value": []} for value in (0, 1, 2, 3, 6, 7, 8)]
    by_type = {item["type"]: item["value"] for item in groups}
    for material in payload.get("materials", {}).get("videos", []) or []:
        path = str(material.get("path") or "")
        if not path:
            continue
        by_type[0].append({
            "ai_group_type": "",
            "create_time": 0,
            "duration": int(material.get("duration") or 0),
            "enter_from": 0,
            "extra_info": Path(path).name,
            "file_Path": path,
            "height": int(material.get("height") or 0),
            "id": str(material.get("local_material_id") or uuid.uuid4()),
            "import_time": 0,
            "import_time_ms": 0,
            "item_source": 1,
            "material_color_tag": "",
            "md5": "",
            "metetype": "video" if Path(path).suffix.lower() in VIDEO_SUFFIXES else "photo",
            "roughcut_time_range": {"duration": int(material.get("duration") or 0), "start": 0},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0,
            "width": int(material.get("width") or 0),
        })
    return groups


def install_native_project(
    shell_project: Path,
    target_dir: Path,
    payload: dict[str, Any],
    project_name: str,
    duration: float,
) -> dict[str, Any]:
    if target_dir.exists():
        raise FileExistsError(f"이미 존재하는 CapCut 프로젝트입니다: {target_dir.name}")
    copytree(shell_project, target_dir)
    timeline = native_timeline(target_dir)
    if timeline is None:
        raise RuntimeError("복제한 CapCut 네이티브 프로젝트 구조를 읽지 못했습니다.")
    old_timeline_id, _nested_info, project = timeline
    old_project_id = str(project.get("id") or "")
    timeline_id = str(uuid.uuid4()).upper()
    project_id = str(uuid.uuid4()).upper()
    draft_id = str(uuid.uuid4()).upper()
    old_timeline_dir = target_dir / "Timelines" / old_timeline_id
    timeline_dir = target_dir / "Timelines" / timeline_id
    old_timeline_dir.rename(timeline_dir)
    _replace_text_ids(target_dir, {old_timeline_id: timeline_id, old_project_id: project_id})

    project_path = target_dir / "Timelines" / "project.json"
    project = read_json(project_path)
    project["id"] = project_id
    project["main_timeline_id"] = timeline_id
    now_us = int(time.time() * 1_000_000)
    project["create_time"] = now_us
    project["update_time"] = now_us
    for item in project.get("timelines") or []:
        if isinstance(item, dict):
            item["id"] = timeline_id
            item["name"] = "타임라인 01"
            item["create_time"] = now_us
            item["update_time"] = now_us
    _write_json(project_path, project)
    _write_json(target_dir / "Timelines" / "project.json.bak", project)

    payload = deepcopy(payload)
    payload["id"] = timeline_id
    payload["duration"] = int(max(0.0, duration) * 1_000_000)
    encoded_paths = (
        target_dir / "draft_info.json",
        target_dir / "draft_info.json.bak",
        timeline_dir / "draft_info.json",
        timeline_dir / "draft_info.json.bak",
    )
    for path in encoded_paths:
        _write_json(path, payload)

    layout_path = target_dir / "timeline_layout.json"
    try:
        layout = read_json(layout_path)
        for dock in layout.get("dockItems") or []:
            if isinstance(dock, dict):
                dock["timelineIds"] = [timeline_id]
                dock["timelineNames"] = ["타임라인 01"]
        _write_json(layout_path, layout)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    meta_path = target_dir / "draft_meta_info.json"
    meta = read_json(meta_path)
    meta.update({
        "draft_id": draft_id,
        "draft_name": project_name,
        "draft_fold_path": str(target_dir),
        "draft_root_path": str(target_dir.parent),
        "draft_new_version": payload.get("new_version", ""),
        "draft_need_rename_folder": False,
        "draft_is_invisible": False,
        "draft_materials": _draft_materials(payload),
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_duration": int(max(0.0, duration) * 1_000_000),
        "draft_timeline_materials_size_": (target_dir / "draft_info.json").stat().st_size,
    })
    _write_json(meta_path, meta)
    validation = validate_native_project(target_dir)
    manifest = {
        "generator": "reels-to-capcut",
        "schema": 2,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "shell_project": shell_project.name,
        "runtime_status": "not_checked",
        "validation": validation,
    }
    _write_json(target_dir / MANIFEST_NAME, manifest)
    return validation


def validate_native_project(project_dir: Path) -> dict[str, Any]:
    blockers: list[str] = []
    timeline = native_timeline(project_dir)
    if timeline is None:
        return {
            "status": "invalid",
            "runtime_status": "not_checked",
            "blockers": ["CapCut 181 네이티브 Timelines 구조가 없습니다."],
        }
    timeline_id, nested_path, _project = timeline
    root_path = project_dir / "draft_info.json"
    try:
        root = read_json(root_path)
        nested = read_json(nested_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "runtime_status": "not_checked",
            "blockers": ["CapCut 타임라인 JSON을 읽지 못했습니다."],
        }
    if root != nested:
        blockers.append("루트와 Timelines 내부 편집 데이터가 다릅니다.")
    if root.get("id") != timeline_id:
        blockers.append("메인 타임라인 ID가 일치하지 않습니다.")
    materials = root.get("materials") if isinstance(root.get("materials"), dict) else {}
    known_ids: set[str] = set()
    for values in materials.values():
        if isinstance(values, list):
            known_ids.update(str(item.get("id")) for item in values if isinstance(item, dict) and item.get("id"))
    editable_segments = 0
    missing_materials: set[str] = set()
    missing_refs: set[str] = set()
    for track in root.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        hidden = int(track.get("attribute") or 0) != 0
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            if not hidden and bool(segment.get("visible", True)) and int(segment.get("track_attribute") or 0) == 0:
                editable_segments += 1
            material_id = str(segment.get("material_id") or "")
            if material_id and material_id not in known_ids:
                missing_materials.add(material_id)
            for reference in segment.get("extra_material_refs") or []:
                if str(reference) not in known_ids:
                    missing_refs.add(str(reference))
    if editable_segments == 0:
        blockers.append("클릭 가능한 영상·자막 세그먼트가 없습니다.")
    if missing_materials:
        blockers.append(f"연결되지 않은 주 소재가 {len(missing_materials)}개 있습니다.")
    if missing_refs:
        blockers.append(f"연결되지 않은 보조 소재 참조가 {len(missing_refs)}개 있습니다.")
    missing_local_ids = 0
    for material in materials.get("videos") or []:
        if not isinstance(material, dict):
            continue
        path = Path(str(material.get("path") or ""))
        if path.suffix.lower() in VIDEO_SUFFIXES and not str(material.get("local_material_id") or "").strip():
            missing_local_ids += 1
    if missing_local_ids:
        blockers.append(f"CapCut 로컬 소재 ID가 없는 영상이 {missing_local_ids}개 있습니다.")
    return {
        "status": "structure_ready" if not blockers else "invalid",
        "runtime_status": "not_checked",
        "timeline_id": timeline_id,
        "track_count": len(root.get("tracks") or []),
        "editable_segment_count": editable_segments,
        "missing_material_count": len(missing_materials),
        "missing_reference_count": len(missing_refs),
        "missing_local_id_count": missing_local_ids,
        "blockers": blockers,
    }
