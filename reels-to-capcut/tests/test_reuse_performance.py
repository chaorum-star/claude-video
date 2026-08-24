from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reels_to_capcut.models import Analysis, CaptionEvent, CaptionStyle, Cue, analysis_from_dict
from reels_to_capcut.performance import PERFORMANCE_START, record_template_use, save_publication
from reels_to_capcut.pipeline import process_template


class TemplateReuseTests(unittest.TestCase):
    def test_saved_analysis_round_trip_keeps_effect_timing(self) -> None:
        analysis = Analysis(
            duration=4.0,
            width=1080,
            height=1920,
            speech=[Cue(0.0, 2.0, "기존 문구")],
            caption_style=CaptionStyle(position="중앙", animation="팝업 확대", confidence=0.9),
            caption_events=[CaptionEvent(0.4, 1.0, "강조", "emphasis", "노랑", 1.2, "팝업 확대", 0.9)],
            planning={"source": "https://www.instagram.com/reel/ABC/", "topic": "테스트 주제"},
        )
        restored = analysis_from_dict(analysis.to_dict())
        self.assertEqual(restored.caption_events[0].start, 0.4)
        self.assertEqual(restored.caption_style.animation, "팝업 확대")
        self.assertEqual(restored.planning["topic"], "테스트 주제")

    def test_template_reuse_skips_analysis_and_rewrites_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            template = output / "instagram_reel_reference"
            template.mkdir()
            (template / "source.mp4").write_bytes(b"video")
            analysis = Analysis(
                duration=3.0,
                width=1080,
                height=1920,
                speech=[Cue(0.0, 1.5, "기존 문구"), Cue(1.5, 3.0, "두 번째")],
                planning={
                    "source": "https://www.instagram.com/reel/ABC/",
                    "topic": "테스트 주제",
                    "hook": {}, "timeliness": {}, "cta": {}, "engagement": {},
                },
            )
            (template / "분석결과.json").write_text(
                json.dumps(analysis.to_dict(), ensure_ascii=False), encoding="utf-8"
            )

            def fake_guide(path: Path, **_kwargs: object) -> Path:
                path.write_text("guide", encoding="utf-8")
                return path

            with (
                patch("reels_to_capcut.pipeline.create_capcut_draft", return_value="새 프로젝트") as create,
                patch("reels_to_capcut.pipeline.write_guide", side_effect=fake_guide),
            ):
                result = process_template(
                    template, output, replacement_text="새로운 문구로 교체합니다", project_title="복제 초안"
                )

            self.assertEqual(result.capcut_project, "새 프로젝트")
            self.assertEqual(" ".join(cue.text for cue in result.analysis.speech), "새로운 문구로 교체합니다")
            self.assertTrue((result.work_dir / "템플릿-재사용.json").is_file())
            self.assertEqual(result.video_path.parent.resolve(), result.work_dir.resolve())
            self.assertEqual(result.video_path.read_bytes(), b"video")
            create.assert_called_once()


class PerformanceArchiveTests(unittest.TestCase):
    def test_publication_updates_archive_and_obsidian_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            template = output / "instagram_reel_reference"
            archive = output / "기획분석_아카이브" / "index.json"
            vault = root / "vault"
            note = vault / "30_CONTENT ANGLES/Instagram Reels/콘텐츠각.md"
            template.mkdir(parents=True)
            archive.parent.mkdir(parents=True)
            (vault / ".obsidian").mkdir(parents=True)
            note.parent.mkdir(parents=True)
            note.write_text("---\nstatus: idea\n---\n\n# 콘텐츠각\n", encoding="utf-8")
            archive.write_text(json.dumps([{
                "source": "https://www.instagram.com/reel/ABC/",
                "topic": "테스트 주제",
                "work_dir": str(template),
                "obsidian_content_angle": str(note),
            }], ensure_ascii=False), encoding="utf-8")

            payload = {
                "published_url": "https://www.instagram.com/reel/OWN/",
                "published_at": "2026-08-24",
                "title": "내 발행물",
                "metrics": {"views": 1000, "likes": 80, "comments": 10, "saves": 30, "shares": 20, "conversions": 5},
                "notes": "저장 유도 문구가 잘 작동함",
            }
            first = save_publication(output, template, payload, vault=vault)
            payload["metrics"]["views"] = 1200
            second = save_publication(output, template, payload, vault=vault)
            record_template_use(
                output, template, capcut_project="복제 프로젝트", result_work_dir=output / "template_reuse_1"
            )

            records = json.loads(archive.read_text(encoding="utf-8"))
            record = records[0]
            self.assertEqual(first["publication"]["rates"]["save_rate"], 3.0)
            self.assertEqual(second["published_count"], 1)
            self.assertEqual(record["publications"][0]["metrics"]["views"], 1200)
            self.assertEqual(record["template_run_count"], 1)
            note_text = note.read_text(encoding="utf-8")
            self.assertEqual(note_text.count(PERFORMANCE_START), 1)
            self.assertIn("status: published", note_text)
            self.assertIn("저장 유도 문구가 잘 작동함", note_text)


if __name__ == "__main__":
    unittest.main()
