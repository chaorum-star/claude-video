from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reels_to_capcut.models import (
    Analysis,
    CaptionEvent,
    CaptionStyle,
    Cue,
    MotionEvent,
    OverlayEvent,
    SoundEvent,
    TitleCard,
    VisualEffect,
)
from reels_to_capcut.review import build_review, save_review


class ReviewTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        output = root / "output"
        work = output / "2026-08-24" / "reference"
        work.mkdir(parents=True)
        (work / "source.mp4").write_bytes(b"video")
        overlay = work / "overlay.png"
        overlay.write_bytes(b"png")
        sound = work / "pop.wav"
        sound.write_bytes(b"wav")
        analysis = Analysis(
            duration=3.0,
            width=1080,
            height=1920,
            speech=[Cue(0.0, 3.0, "말 자막")],
            caption_style=CaptionStyle(
                position="중앙", size="작게", color="흰색", animation="즉시 등장",
                confidence=0.85, center_y=0.58, height_ratio=0.027,
            ),
            title_card=TitleCard(0.0, 0.8, 0.25, 0.2, "흰색", "#AB2518"),
            caption_events=[CaptionEvent(
                1.0, 1.6, "핵심 단어", "color_then_pop", "노랑→흰색", 1.65,
                "pop", 0.96, "팝 업", 0.94,
            )],
            motion_events=[MotionEvent(
                1.0, 1.5, "punch_in", scale_to=1.12, confidence=0.9,
                capcut_animation="줌 2", match_confidence=0.86,
            )],
            visual_effects=[
                VisualEffect(1.0, 1.16, "cut", "하드컷", 0.8, "프레임 변화", "전환 없음"),
                VisualEffect(1.6, 2.0, "shake", "화면 흔들림", 0.9, "이동량 14px", "흔들림"),
                VisualEffect(2.2, 2.6, "flash", "화이트 플래시", 0.9, "밝기 급증", "플래시"),
            ],
            overlay_events=[OverlayEvent(
                0.8, 1.4, "profile_card", 0.5, 0.2, 0.4, 0.1,
                "교체할 카드 1", 0.82, str(overlay),
            )],
            sound_events=[
                SoundEvent(0.4, 0.7, "impact", 0.55, 1.2, 1300, str(sound), "audio_peak"),
                SoundEvent(2.0, 2.2, "pop", 0.72, 0, 0, str(sound), "visual_sync"),
            ],
            planning={"topic": "검수 테스트", "source": "https://example.com/reel"},
        )
        (work / "분석결과.json").write_text(
            json.dumps(analysis.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        (work / "초안-원본-의존관계.json").write_text(
            json.dumps({"capcut_project": "검수초안"}, ensure_ascii=False), encoding="utf-8"
        )
        archive = output / "기획분석_아카이브" / "index.json"
        archive.parent.mkdir(parents=True)
        archive.write_text(json.dumps([{
            "source": "https://example.com/reel",
            "topic": "검수 테스트",
            "work_dir": str(work),
            "capcut_project": "검수초안",
        }], ensure_ascii=False), encoding="utf-8")

        draft_dir = root / "drafts"
        project = draft_dir / "검수초안"
        project.mkdir(parents=True)
        payload = {
            "tracks": [
                {"name": "교체할 영상 · 템플릿", "segments": [
                    self.segment(0, 1_000_000, "video"),
                    self.segment(1_000_000, 2_000_000, "video", ["zoom"]),
                ]},
                {"name": "말 자막", "segments": [self.segment(0, 3_000_000, "text")]},
                {"name": "강조 자막 · 교체가능", "segments": [self.segment(1_000_000, 600_000, "text", ["pop"])]},
                {"name": "제목 카드", "segments": [self.segment(0, 800_000, "title")]},
                {"name": "오버레이 교체 1", "segments": [self.segment(800_000, 600_000, "overlay")]},
                {"name": "효과음 템플릿 · 교체가능", "segments": [self.segment(2_000_000, 200_000, "audio")]},
                {"name": "화면 효과 재현", "segments": [self.segment(1_600_000, 400_000, "effect")]},
            ],
            "materials": {
                "material_animations": [
                    {"id": "zoom", "animations": [{"name": "줌 2", "type": "group"}]},
                    {"id": "pop", "animations": [{"name": "팝 업", "type": "in"}]},
                ],
                "video_effects": [{"id": "effect", "name": "흔들림"}],
                "audios": [{"id": "audio", "name": "pop · 교체가능", "path": str(sound)}],
                "videos": [{"id": "overlay", "path": str(overlay)}],
            },
        }
        (project / "draft_info.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return output, work, draft_dir

    @staticmethod
    def segment(start: int, duration: int, material_id: str, refs: list[str] | None = None) -> dict:
        return {
            "target_timerange": {"start": start, "duration": duration},
            "material_id": material_id,
            "extra_material_refs": refs or [],
        }

    def test_review_uses_saved_capcut_tracks_as_draft_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, work, draft_dir = self.make_fixture(Path(temporary))
            review = build_review(output, work, draft_dir=draft_dir)
            self.assertTrue(review["summary"]["project_found"])
            self.assertEqual(review["summary"]["total"], 10)
            self.assertEqual(review["summary"]["recorded"], 8)
            self.assertEqual(review["summary"]["review"], 1)
            self.assertEqual(review["summary"]["missing"], 1)
            by_id = {item["id"]: item for item in review["events"]}
            self.assertEqual(by_id["caption-1"]["matched_resource"], "강조 자막 · 팝 업")
            self.assertEqual(by_id["motion-1"]["matched_resource"], "줌 2")
            self.assertEqual(by_id["visual-2"]["matched_resource"], "흔들림")
            self.assertEqual(by_id["visual-3"]["status"], "missing")
            self.assertEqual(by_id["sound-1"]["status"], "review")
            self.assertEqual(by_id["sound-2"]["matched_resource"], "pop · 교체가능")

    def test_saved_review_is_written_and_reflected_in_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, work, _draft_dir = self.make_fixture(Path(temporary))
            result = save_review(output, work, {"status": "needs_changes", "note": "자막 확인\n효과음 확인"})
            saved = json.loads((work / "검수결과.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "needs_changes")
            self.assertEqual(saved["note"], "자막 확인\n효과음 확인")
            archive = json.loads((output / "기획분석_아카이브" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(archive[0]["review_status"], "needs_changes")
            self.assertEqual(result["review"]["status_label"], "수정 필요")

    def test_runtime_pass_requires_every_manual_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, work, _draft_dir = self.make_fixture(Path(temporary))
            with self.assertRaisesRegex(ValueError, "모두 확인"):
                save_review(output, work, {
                    "status": "runtime_passed",
                    "runtime_checks": {"editable": True, "playback": True},
                })

    def test_reused_template_review_updates_original_archive_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, work, _draft_dir = self.make_fixture(Path(temporary))
            archive_path = output / "기획분석_아카이브" / "index.json"
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            archive[0]["work_dir"] = str(output / "original-reference")
            archive[0]["last_template_work_dir"] = str(work)
            archive_path.write_text(json.dumps(archive, ensure_ascii=False), encoding="utf-8")

            save_review(output, work, {"status": "needs_changes", "note": "재사용 초안 확인"})

            updated = json.loads(archive_path.read_text(encoding="utf-8"))
            self.assertEqual(updated[0]["review_status"], "needs_changes")
            self.assertEqual(updated[0]["review_note"], "재사용 초안 확인")

    def test_review_rejects_unknown_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, work, _draft_dir = self.make_fixture(Path(temporary))
            with self.assertRaisesRegex(ValueError, "검수 상태"):
                save_review(output, work, {"status": "done"})


if __name__ == "__main__":
    unittest.main()
