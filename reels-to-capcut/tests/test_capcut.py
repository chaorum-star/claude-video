from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from reels_to_capcut.capcut import (
    TEMPLATE_VIDEO_TRACK,
    create_capcut_draft,
    find_native_template,
    plan_effect_segments,
)
from reels_to_capcut.models import Analysis, CaptionEvent, CaptionStyle, Cue, MotionEvent, VisualEffect


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class CapCutDraftTests(unittest.TestCase):
    def make_native_shell(self, draft_dir: Path, effects: dict[str, str] | None = None) -> Path:
        shell = draft_dir / "네이티브세로"
        timeline_id = "11111111-1111-4111-8111-111111111111"
        project_id = "22222222-2222-4222-8222-222222222222"
        effects = effects or {}
        payload = {
            "id": timeline_id,
            "new_version": "181.0.0",
            "platform": {"os": "mac"},
            "last_modified_platform": {"os": "mac"},
            "canvas_config": {"ratio": "9:16", "width": 1080, "height": 1920},
            "duration": 1_000_000,
            "tracks": [
                {"id": "native-video-track", "name": "", "type": "video", "attribute": 0, "segments": [{
                    "id": "native-video-segment", "material_id": "native-video",
                    "target_timerange": {"start": 0, "duration": 1_000_000},
                    "source_timerange": {"start": 0, "duration": 1_000_000},
                    "extra_material_refs": ["native-speed", "native-placeholder", "native-canvas", "native-video-animation", "native-channel", "native-color", "native-vocal"],
                    "visible": True, "track_attribute": 0, "enable_adjust": True,
                }]},
                {"id": "native-text-track", "name": "", "type": "text", "attribute": 0, "segments": [{
                    "id": "native-text-segment", "material_id": "native-text",
                    "target_timerange": {"start": 0, "duration": 1_000_000},
                    "source_timerange": None, "extra_material_refs": ["native-text-animation"],
                    "visible": True, "track_attribute": 0, "enable_adjust": False,
                }]},
                {"id": "native-audio-track", "name": "", "type": "audio", "attribute": 0, "segments": [{
                    "id": "native-audio-segment", "material_id": "native-audio",
                    "target_timerange": {"start": 0, "duration": 1_000_000},
                    "source_timerange": {"start": 0, "duration": 1_000_000},
                    "extra_material_refs": ["native-audio-speed", "native-audio-placeholder", "native-beat", "native-audio-channel", "native-audio-vocal"],
                    "visible": True, "track_attribute": 0,
                }]},
            ],
            "materials": {
                "videos": [{"id": "native-video", "path": "/tmp/native.mp4", "duration": 1_000_000, "width": 1080, "height": 1920, "local_material_id": "native-local", "check_flag": 62978047}],
                "texts": [{"id": "native-text", "type": "text", "content": "{}"}],
                "audios": [{"id": "native-audio", "path": "/tmp/native.wav", "duration": 1_000_000, "local_material_id": "native-audio-local", "check_flag": 62978047}],
                "video_effects": [{"id": resource_id, "name": name, "resource_id": resource_id, "effect_id": resource_id, "category_id": "video_effect", "category_name": "effect", "source_platform": 1, "third_resource_id": resource_id, "path": ""} for name, resource_id in effects.items()],
                "speeds": [{"id": "native-speed", "type": "speed", "mode": 0, "speed": 1.0}, {"id": "native-audio-speed", "type": "speed", "mode": 0, "speed": 1.0}],
                "placeholder_infos": [{"id": "native-placeholder", "type": "placeholder_info"}, {"id": "native-audio-placeholder", "type": "placeholder_info"}],
                "canvases": [{"id": "native-canvas", "type": "canvas_color", "color": ""}],
                "material_animations": [
                    {"id": "native-video-animation", "type": "sticker_animation", "animations": []},
                    {"id": "native-text-animation", "type": "sticker_animation", "animations": [{"type": "in", "name": "팝 업", "resource_id": "7145435451946439170", "third_resource_id": "7145435451946439170", "path": "/tmp/팝업", "category_id": "ruchang", "category_name": "text", "source_platform": 1}]},
                    {"id": "native-zoom-animation", "type": "sticker_animation", "animations": [{"type": "group", "name": "줌 2", "resource_id": "6779083172429697544", "third_resource_id": "6779083172429697544", "path": "/tmp/줌2", "category_id": "combo", "category_name": "video", "source_platform": 1}]},
                ],
                "sound_channel_mappings": [{"id": "native-channel", "type": "none"}, {"id": "native-audio-channel", "type": "none"}],
                "material_colors": [{"id": "native-color", "is_color_clip": False}],
                "vocal_separations": [{"id": "native-vocal", "type": "vocal_separation"}, {"id": "native-audio-vocal", "type": "vocal_separation"}],
                "beats": [{"id": "native-beat", "type": "beats", "beat_speed_infos": []}],
            },
        }
        timeline = shell / "Timelines" / timeline_id
        timeline.mkdir(parents=True)
        encoded = json.dumps(payload, ensure_ascii=False)
        (shell / "draft_info.json").write_text(encoded, encoding="utf-8")
        (timeline / "draft_info.json").write_text(encoded, encoding="utf-8")
        (shell / "Timelines" / "project.json").write_text(json.dumps({
            "id": project_id, "main_timeline_id": timeline_id,
            "timelines": [{"id": timeline_id, "name": "타임라인 01"}],
        }), encoding="utf-8")
        (shell / "timeline_layout.json").write_text(json.dumps({"dockItems": [{"timelineIds": [timeline_id], "timelineNames": ["타임라인 01"]}]}), encoding="utf-8")
        (shell / "draft_meta_info.json").write_text(json.dumps({
            "draft_id": "33333333-3333-4333-8333-333333333333", "draft_name": shell.name,
            "draft_materials": [], "draft_fold_path": str(shell), "draft_root_path": str(draft_dir),
        }), encoding="utf-8")
        return shell

    def test_native_template_prefers_highest_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, version in (("newer-file", "175.0.0"), ("current-schema", "181.0.0")):
                folder = root / name
                folder.mkdir()
                (folder / "draft_info.json").write_text(
                    json.dumps({"new_version": version, "platform": {"os": "mac"}}),
                    encoding="utf-8",
                )
            selected = find_native_template(root)
            self.assertEqual(selected["new_version"], "181.0.0")

    def test_generated_draft_has_tracks_and_safety_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mp4"
            draft_dir = root / "drafts"
            draft_dir.mkdir()
            self.make_native_shell(draft_dir)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=360x640:d=3:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video),
                ],
                check=True,
            )
            analysis = Analysis(
                duration=3.0,
                width=360,
                height=640,
                speech=[Cue(0.0, 1.2, "첫 자막"), Cue(1.2, 2.7, "두 번째 자막")],
                screen_text=[Cue(0.2, 1.0, "화면 글자", "screen")],
                scenes=[1.5],
                audio_peaks=[0.8],
                visual_effects=[
                    VisualEffect(0.8, 1.1, "flash", "화이트 플래시 후보", 0.82, "밝기 급증", "White Flash")
                ],
                caption_style=CaptionStyle(
                    position="중앙", size="크게", color="노랑 계열",
                    outline="검정 외곽선/그림자 추정", background="반투명·단색 박스 추정",
                    animation="팝업/확대 등장 추정",
                ),
            )
            project_name = create_capcut_draft(video, analysis, "테스트", draft_dir)
            project_dir = draft_dir / project_name
            self.assertTrue((project_dir / "draft_info.json").is_file())
            payload = json.loads((project_dir / "draft_info.json").read_text(encoding="utf-8"))
            tracks = {track["name"]: track for track in payload["tracks"]}
            self.assertEqual(len(tracks[TEMPLATE_VIDEO_TRACK]["segments"]), 2)
            self.assertEqual(tracks[TEMPLATE_VIDEO_TRACK]["attribute"], 0)
            self.assertTrue(tracks[TEMPLATE_VIDEO_TRACK]["segments"][0]["visible"])
            self.assertEqual(len(tracks["말 자막"]["segments"]), 2)
            self.assertEqual(len(tracks["화면 글자"]["segments"]), 1)
            self.assertEqual(len(tracks["화면 효과 분석"]["segments"]), 1)
            self.assertEqual(tracks["효과음 후보 · 숨김"]["attribute"], 1)
            self.assertFalse(payload["materials"]["videos"][0].get("object_locked", {}).get("locked", False))
            meta = json.loads((project_dir / "draft_meta_info.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["tm_duration"], 3_000_000)
            self.assertEqual(meta["draft_name"], project_name)


    def test_segments_are_clipped_to_material_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mp4"
            draft_dir = root / "drafts"
            draft_dir.mkdir()
            self.make_native_shell(draft_dir)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=360x640:d=3:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video),
                ],
                check=True,
            )
            # 컨테이너 길이가 CapCut이 읽는 소재 길이보다 길게 측정된 상황을 재현한다.
            analysis = Analysis(duration=3.4, width=360, height=640, scenes=[1.5])
            project_name = create_capcut_draft(video, analysis, "길이초과", draft_dir)
            payload = json.loads(
                (draft_dir / project_name / "draft_info.json").read_text(encoding="utf-8")
            )
            material = payload["materials"]["videos"][0]
            segments = [
                segment for segment in payload["tracks"][0]["segments"]
                if segment["material_id"] == material["id"]
            ]
            self.assertEqual(len(segments), 2)
            for segment in segments:
                source = segment["source_timerange"]
                self.assertLessEqual(source["start"] + source["duration"], material["duration"])


    def make_capcut_home(self, root: Path, effects: dict[str, str]) -> Path:
        """리소스가 내려받힌 CapCut 설치를 흉내 낸다. effects는 {이름: 리소스번호}."""
        draft_dir = root / "User Data" / "Projects" / "com.lveditor.draft"
        draft_dir.mkdir(parents=True)
        cache = root / "User Data" / "Cache" / "effect"
        for resource_id in effects.values():
            (cache / resource_id).mkdir(parents=True)
        (cache / "7145435451946439170").mkdir(parents=True, exist_ok=True)
        self.make_native_shell(draft_dir, effects)
        return draft_dir

    def test_emphasis_and_video_motion_use_local_capcut_animations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mp4"
            self.make_sample_video(video)
            draft_dir = self.make_capcut_home(root, {})
            (root / "User Data" / "Cache" / "effect" / "6779083172429697544").mkdir(parents=True)
            analysis = Analysis(
                duration=3.0, width=360, height=640,
                speech=[Cue(0.0, 3.0, "일반 자막")],
                caption_events=[CaptionEvent(
                    1.0, 1.8, "강조 자막", "color_then_pop", "노랑→흰색", 1.65, "pop", 0.96
                )],
                motion_events=[MotionEvent(1.0, 1.6, "punch_in", scale_to=1.12, confidence=0.9)],
                caption_style=CaptionStyle(center_y=0.58, height_ratio=0.027, animation="즉시 등장"),
            )
            project = create_capcut_draft(video, analysis, "로컬효과", draft_dir)
            payload = json.loads((draft_dir / project / "draft_info.json").read_text())
            tracks = {track["name"]: track for track in payload["tracks"]}
            template_video = tracks[TEMPLATE_VIDEO_TRACK]["segments"]
            motion_segments = [segment for segment in template_video if segment.get("common_keyframes")]
            self.assertTrue(motion_segments)
            motion_properties = {
                group.get("property_type")
                for segment in motion_segments
                for group in segment.get("common_keyframes", [])
            }
            self.assertIn("KFTypePositionX", motion_properties)
            self.assertIn("KFTypePositionY", motion_properties)
            self.assertIn("KFTypeScaleX", motion_properties)
            emphasis = tracks["강조 자막 · 교체가능"]["segments"]
            # 강조는 지역별 프리셋 ID에 의존하지 않고 색상 레이어와 표준
            # 키프레임으로 재현돼야 문구를 바꿔도 그대로 유지된다.
            self.assertEqual(len(emphasis), 1)
            self.assertTrue(any(segment.get("common_keyframes") for segment in emphasis))
            self.assertIn("오버슈트", analysis.caption_events[0].capcut_animation)
            self.assertIn("강조 반짝임 · 자동", tracks)
            self.assertIn("키프레임", analysis.motion_events[0].capcut_animation)

    def make_sample_video(self, path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "color=c=blue:s=360x640:d=3:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path),
            ],
            check=True,
        )

    def test_effects_use_resource_ids_that_exist_on_this_mac(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mp4"
            self.make_sample_video(video)
            draft_dir = self.make_capcut_home(root, {"렌즈 줌": "7399465441057328389"})
            analysis = Analysis(
                duration=3.0, width=360, height=640,
                speech=[Cue(0.0, 1.0, "자막")],
                caption_style=CaptionStyle(animation="팝업/확대 등장 추정"),
                visual_effects=[
                    VisualEffect(0.5, 1.0, "zoom_in", "펀치 인 후보", 0.9, "배율 1.2", "줌"),
                    VisualEffect(1.5, 2.0, "shake", "흔들림 후보", 0.9, "이동량 17px", "흔들림"),
                    VisualEffect(2.2, 2.4, "cut", "하드컷 후보", 0.95, "변화량 0.4", "전환 없음"),
                ],
            )
            project_name = create_capcut_draft(video, analysis, "효과적용", draft_dir)
            payload = json.loads(
                (draft_dir / project_name / "draft_info.json").read_text(encoding="utf-8")
            )
            # 이 Mac에 있는 줌 효과만 걸리고, 흔들림은 리소스가 없으므로 건너뛴다.
            effects = payload["materials"]["video_effects"]
            self.assertEqual([item["name"] for item in effects], ["렌즈 줌"])
            self.assertEqual(effects[0]["resource_id"], "7399465441057328389")
            # 자막 애니메이션도 이 Mac에서 열리는 번호로 바뀌어야 "애니메이션 분실"이 안 뜬다.
            animation = payload["materials"]["material_animations"][0]["animations"][0]
            self.assertEqual(animation["name"], "팝 업")
            self.assertEqual(animation["resource_id"], "7145435451946439170")
            self.assertGreater(animation["duration"], 0)

    def test_undetected_animation_is_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mp4"
            self.make_sample_video(video)
            draft_dir = self.make_capcut_home(root, {})
            analysis = Analysis(
                duration=3.0, width=360, height=640,
                speech=[Cue(0.0, 1.0, "자막"), Cue(1.0, 2.0, "두 번째")],
                caption_style=CaptionStyle(animation="즉시 등장 또는 판별 보류"),
            )
            project_name = create_capcut_draft(video, analysis, "판별보류", draft_dir)
            payload = json.loads(
                (draft_dir / project_name / "draft_info.json").read_text(encoding="utf-8")
            )
            # 원본에서 등장 움직임을 못 읽었으면 애니메이션을 지어내지 않는다.
            animations = [
                animation
                for group in payload["materials"].get("material_animations", [])
                for animation in group.get("animations", [])
            ]
            self.assertEqual(animations, [])

    def test_nothing_is_applied_when_capcut_has_no_matching_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mp4"
            self.make_sample_video(video)
            draft_dir = self.make_capcut_home(root, {})
            analysis = Analysis(
                duration=3.0, width=360, height=640,
                visual_effects=[
                    VisualEffect(0.5, 1.0, "shake", "흔들림 후보", 0.9, "이동량 17px", "흔들림"),
                ],
            )
            project_name = create_capcut_draft(video, analysis, "효과없음", draft_dir)
            payload = json.loads(
                (draft_dir / project_name / "draft_info.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["materials"].get("video_effects", []), [])
            self.assertFalse(any(t.get("type") == "effect" for t in payload["tracks"]))

    def test_low_confidence_and_hard_cuts_are_never_planned(self) -> None:
        analysis = Analysis(
            duration=3.0, width=360, height=640,
            visual_effects=[
                VisualEffect(0.5, 1.0, "shake", "흔들림 후보", 0.62, "이동량 4px", "흔들림"),
                VisualEffect(2.2, 2.4, "cut", "하드컷 후보", 0.95, "변화량 0.4", "전환 없음"),
            ],
        )
        usable = {"shake": {"name": "흔들림", "resource_id": "1"}, "cut": {"name": "x", "resource_id": "2"}}
        self.assertEqual(plan_effect_segments(analysis, usable), [])


if __name__ == "__main__":
    unittest.main()
