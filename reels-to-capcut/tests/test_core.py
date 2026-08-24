from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import numpy as np
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reels_to_capcut.analyze import merge_ocr_snapshots, recognize_frame, remove_speech_duplicates, similar_text
from reels_to_capcut.artifacts import write_srt
from reels_to_capcut.capcut import base_caption_cues
from reels_to_capcut.download import _download_reel
from reels_to_capcut.editing_grammar import apply_replacement_text
from reels_to_capcut.models import Analysis, CaptionEvent, CaptionStyle, Cue
from reels_to_capcut.obsidian_archive import ObsidianArchiveResult, archive_to_obsidian
from reels_to_capcut.pipeline import process_source
from reels_to_capcut.planning import analyze_planning, archive_planning, write_planning_report
from reels_to_capcut.transcribe import segment_to_cues
from reels_to_capcut.utils import format_timestamp, is_instagram_reel_url, is_instagram_url, make_job_dir, safe_slug
from reels_to_capcut.visual_effects import classify_effects, describe_text_pixels


class UtilityTests(unittest.TestCase):
    def test_instagram_url_validation(self) -> None:
        self.assertTrue(is_instagram_url("https://www.instagram.com/reel/ABC/"))
        self.assertFalse(is_instagram_url("https://example.com/reel/ABC/"))
        self.assertTrue(is_instagram_reel_url("https://www.instagram.com/reel/ABC/"))
        self.assertFalse(is_instagram_reel_url("https://www.instagram.com/blabla_lizzypark/"))

    def test_slug_and_timestamp(self) -> None:
        self.assertEqual(safe_slug(" 릴스: 테스트! "), "릴스_테스트")
        self.assertEqual(format_timestamp(65.432, srt=True), "00:01:05,432")

    def test_ocr_merge_and_speech_dedupe(self) -> None:
        merged = merge_ocr_snapshots([(0, "같은 화면"), (1, "같은 화면"), (3, "새 화면")], 1, 5)
        self.assertEqual(len(merged), 2)
        screen = [Cue(0, 2, "안녕하세요 여러분", "screen"), Cue(2, 3, "화면 전용", "screen")]
        speech = [Cue(0, 2, "안녕하세요 여러분", "speech")]
        self.assertEqual([cue.text for cue in remove_speech_duplicates(screen, speech)], ["화면 전용"])
        self.assertGreater(similar_text("안녕하세요!", "안녕하세요"), 0.9)

    def test_srt_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_srt(Path(temporary) / "captions.srt", [Cue(0, 1.25, "테스트")])
            self.assertIn("00:00:00,000 --> 00:00:01,250", path.read_text(encoding="utf-8"))

    def test_whisper_segment_is_split_into_effect_captions(self) -> None:
        words = [
            SimpleNamespace(start=0.0, end=0.5, word="안녕하세요"),
            SimpleNamespace(start=0.5, end=1.0, word=" 여러분"),
            SimpleNamespace(start=1.0, end=1.5, word=" 오늘은"),
            SimpleNamespace(start=1.5, end=2.0, word=" 자동화를"),
            SimpleNamespace(start=2.0, end=2.5, word=" 시작합니다."),
        ]
        cues = segment_to_cues(SimpleNamespace(start=0.0, end=2.5, text="", words=words), max_chars=12)
        self.assertGreaterEqual(len(cues), 2)
        self.assertEqual(" ".join(cue.text for cue in cues), "안녕하세요 여러분 오늘은 자동화를 시작합니다.")
        self.assertTrue(all(cue.end - cue.start <= 2.2 for cue in cues))

    def test_visual_effect_classifier_finds_flash_and_cut(self) -> None:
        pattern = np.tile(np.arange(96, dtype=np.float32), (64, 1)) * 2
        frames = [pattern.copy() for _ in range(12)]
        frames[5] = np.full_like(pattern, 255)
        frames[8:] = [np.flip(pattern, axis=1).copy() for _ in range(4)]
        effects = classify_effects(frames, fps=4.0, duration=3.0, scenes=[2.0])
        self.assertIn("flash", [effect.kind for effect in effects])
        self.assertTrue(any(effect.start <= 1.25 <= effect.end for effect in effects if effect.kind == "flash"))
        self.assertTrue(any(effect.kind in {"cut", "zoom_in", "zoom_out"} for effect in effects))

    def test_caption_pixel_style_detects_white_with_dark_outline(self) -> None:
        crop = np.full((40, 120, 3), 120, dtype=np.uint8)
        crop[:, :30] = 10
        crop[:, 30:70] = 245
        color, outline, _ = describe_text_pixels(crop)
        self.assertEqual(color, "흰색 계열")
        self.assertTrue(outline)

    def test_replacement_script_keeps_emphasis_slots_without_duplicate_caption(self) -> None:
        analysis = Analysis(
            4.0, 1080, 1920,
            speech=[Cue(0.0, 2.0, "원래 첫 문장"), Cue(2.0, 4.0, "원래 둘째 문장")],
            caption_events=[
                CaptionEvent(1.0, 1.8, "강조", "color_then_pop", "노랑→흰색", 1.65, "pop", 0.9)
            ],
        )
        apply_replacement_text(analysis, "새로운 이야기를 오늘 바로 시작해 보세요")
        self.assertTrue(all(cue.words for cue in analysis.speech))
        self.assertTrue(analysis.caption_events[0].text)
        base = base_caption_cues(analysis.speech, analysis.caption_events)
        self.assertTrue(base)
        self.assertFalse(any(cue.start < 1.8 and 1.0 < cue.end for cue in base))

    def test_planning_analysis_uses_hook_comments_cta_and_archives(self) -> None:
        analysis = Analysis(
            20.0, 1080, 1920,
            speech=[
                Cue(0.0, 2.5, "요즘 이분들이 대세입니다"),
                Cue(3.0, 6.0, "왜 사랑받는 걸까요?"),
                Cue(12.0, 15.0, "진정성이 느껴지기 때문입니다"),
            ],
        )
        metadata = {
            "description": "요즘 이분들이 정말 대세입니다\n진정성이 중요합니다\n자세한 내용은 프로필 링크에서 확인하세요",
            "upload_date": date.today().strftime("%Y%m%d"),
            "like_count": 100,
            "comment_count": 2,
            "comments": [
                {"text": "진정성에 너무 공감해요"},
                {"text": "좋은 내용 감사합니다"},
            ],
        }
        planning = analyze_planning("https://www.instagram.com/reel/test/", metadata, analysis)
        self.assertGreaterEqual(planning["timeliness"]["score"], 4)
        self.assertIn("프로필 이동", planning["cta"]["types"])
        self.assertIn("전환·판매", planning["cta"]["purposes"])
        self.assertIn("트렌드 선언형", planning["hook"]["categories"])
        self.assertTrue(any(item["point"] == "진정성과 신뢰" for item in planning["loved_points"]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "job"
            work.mkdir()
            report = write_planning_report(work / "기획분석.md", planning)
            archive = archive_planning(root, work, planning, "템플릿")
            self.assertTrue(report.is_file())
            records = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(len(records), 1)
            self.assertIn("트렌드 선언형", records[0]["hook_categories"])
            self.assertIn("전환·판매", records[0]["publication_purposes"])
            self.assertIn("진정성과 신뢰", records[0]["insights"])

    def test_obsidian_archive_builds_second_brain_chain_and_preserves_notes(self) -> None:
        analysis = Analysis(
            20.0, 1080, 1920,
            speech=[Cue(0.0, 2.5, "요즘 이분들이 대세입니다")],
            caption_events=[CaptionEvent(
                1.0, 1.8, "대세", "color_then_pop", "노랑→흰색", 1.65,
                "pop", 0.9, capcut_animation="팝 업", match_confidence=0.95,
            )],
        )
        planning = {
            "source": "https://www.instagram.com/reel/DcRtVswzIu_/",
            "creator": "테스트 작성자",
            "topic": "고객이 사랑하는 브랜드의 공통점",
            "keywords": ["브랜드", "진정성"],
            "engagement": {"likes": 100, "comments": 4, "analyzed_comments": 2},
            "timeliness": {"score": 4, "upload_date": "2026-08-20"},
            "hook": {
                "score": 5,
                "first_3_seconds": "요즘 이분들이 대세입니다",
                "techniques": ["시의성 후킹"],
                "insight": "대상을 빠르게 특정합니다.",
            },
            "retention": [{"time": "00:03", "device": "사례", "evidence": "실제 사례 제시"}],
            "cta": {"types": ["저장 유도"], "evidence": ["저장해두세요"], "insight": "후반 전환"},
            "loved_points": [{
                "point": "진정성과 신뢰",
                "insight": "진심 관련 표현이 반복됩니다.",
                "evidence": ["진정성이 느껴져요"],
            }],
            "reusable_formula": ["현상을 선언", "사례를 제시", "CTA로 마감"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "Vault"
            (vault / ".obsidian").mkdir(parents=True)
            (vault / "00_HOME.md").write_text("# Home\n", encoding="utf-8")
            work = root / "job"
            work.mkdir()
            first = archive_to_obsidian(planning, analysis, work, "CapCut 초안", vault=vault)
            self.assertTrue(first.enabled)
            source_note = Path(first.source_note_path or "")
            insight_card = Path(first.insight_card_path or "")
            content_angle = Path(first.content_angle_path or "")
            self.assertTrue(source_note.is_file())
            self.assertTrue(insight_card.is_file())
            self.assertTrue(content_angle.is_file())
            self.assertIn("다음 노트", source_note.read_text(encoding="utf-8"))
            self.assertIn("제작 체크리스트", content_angle.read_text(encoding="utf-8"))

            source_note.write_text(
                source_note.read_text(encoding="utf-8") + "\n- 사용자 메모는 남아야 함\n",
                encoding="utf-8",
            )
            second = archive_to_obsidian(planning, analysis, work, "CapCut 초안", vault=vault)
            self.assertTrue(second.enabled)
            self.assertIn("사용자 메모는 남아야 함", source_note.read_text(encoding="utf-8"))
            registry = json.loads((vault / ".reels-to-capcut/index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(registry), 1)
            self.assertIn("릴스 콘텐츠 인사이트 허브", (vault / "00_HOME.md").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract required")
    def test_ocr_falls_back_to_tesseract(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            image_path = Path(temporary) / "ocr.png"
            image = Image.new("RGB", (900, 400), "white")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("/System/Library/Fonts/AppleSDGothicNeo.ttc", 72)
            draw.text((50, 140), "HELLO 123 테스트", font=font, fill="black")
            image.save(image_path)
            payload = recognize_frame(
                image_path,
                Path("reels_to_capcut/vision_ocr.py").resolve(),
                None,
            )
            self.assertIn("HELLO", " ".join(payload.get("lines", [])))

    def test_instagram_download_retries_with_chrome_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_dir = Path(temporary)
            downloaded = work_dir / "source.mp4"
            downloaded.write_bytes(b"video")
            failed = subprocess.CompletedProcess([], 1, "", "login required")
            succeeded = subprocess.CompletedProcess([], 0, f"{downloaded}\n제목\n", "")
            with patch("reels_to_capcut.download.run", side_effect=[failed, succeeded]) as mocked:
                path, title = _download_reel("https://www.instagram.com/reel/ABC/", work_dir)
            self.assertEqual(path, downloaded)
            self.assertEqual(title, "제목")
            self.assertIn("--cookies-from-browser", mocked.call_args_list[1].args[0])


class PipelineSafetyTests(unittest.TestCase):
    def test_safe_artifacts_survive_capcut_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            source.write_bytes(b"sample")
            analysis = Analysis(3.0, 1080, 1920, speech=[Cue(0, 1, "안녕하세요")])
            with (
                patch("reels_to_capcut.pipeline.acquire_video", return_value=(source, "테스트")),
                patch("reels_to_capcut.pipeline.analyze_video", return_value=analysis),
                patch("reels_to_capcut.pipeline.create_capcut_draft", side_effect=RuntimeError("draft failed")),
                patch(
                    "reels_to_capcut.pipeline.archive_to_obsidian",
                    return_value=ObsidianArchiveResult(enabled=False),
                ),
            ):
                result = process_source(str(source), root / "outputs")
            self.assertTrue(result.srt_path.is_file())
            self.assertTrue(result.guide_path.is_file())
            self.assertTrue(result.dependency_path.is_file())
            self.assertEqual(result.capcut_error, "draft failed")
            dependency = json.loads(result.dependency_path.read_text(encoding="utf-8"))
            self.assertIn("삭제", dependency["warning"])


if __name__ == "__main__":
    unittest.main()
