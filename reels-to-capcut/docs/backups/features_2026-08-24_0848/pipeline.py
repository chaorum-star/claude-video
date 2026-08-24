from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
from typing import Callable

from .analyze import analyze_video
from .artifacts import write_analysis, write_dependency_manifest, write_guide, write_srt
from .capcut import DEFAULT_DRAFT_DIR, create_capcut_draft
from .download import acquire_video
from .editing_grammar import apply_replacement_text
from .models import JobResult
from .obsidian_archive import archive_to_obsidian
from .planning import (
    analyze_planning,
    archive_planning,
    load_reference_metadata,
    write_planning_report,
)
from .utils import make_job_dir


Progress = Callable[[str, int, str], None]


def process_source(
    source: str,
    output_root: Path,
    progress: Progress | None = None,
    replacement_text: str | None = None,
) -> JobResult:
    report = progress or (lambda _stage, _percent, _message: None)
    work_dir = make_job_dir(output_root, "instagram_reel")
    report("download", 5, "릴스 영상을 내려받고 있습니다.")
    video_path, title = acquire_video(source, work_dir)

    report("analyze", 20, "음성·장면·화면 글자·오디오 피크를 분석합니다.")
    analysis = analyze_video(video_path, work_dir)
    metadata = load_reference_metadata(work_dir, source, title)
    analysis.planning = analyze_planning(source, metadata, analysis)
    if replacement_text and replacement_text.strip():
        apply_replacement_text(analysis, replacement_text.strip())
    write_analysis(work_dir / "분석결과.json", analysis)
    planning_path = write_planning_report(work_dir / "기획분석.md", analysis.planning)

    report("safe-copy", 75, "SRT와 편집 가이드 안전본을 먼저 저장합니다.")
    srt_path = write_srt(work_dir / "말 자막.srt", analysis.speech)

    capcut_project: str | None = None
    capcut_error: str | None = None
    report("capcut", 82, "CapCut 초안을 생성합니다.")
    draft_dir = Path(os.getenv("REELS_CAPCUT_DRAFT_DIR", str(DEFAULT_DRAFT_DIR))).expanduser()
    try:
        capcut_project = create_capcut_draft(video_path, analysis, title, draft_dir)
        write_analysis(work_dir / "분석결과.json", analysis)
    except Exception as exc:
        capcut_error = str(exc)
        analysis.warnings.append(f"CapCut 초안 생성 실패: {capcut_error}")
        write_analysis(work_dir / "분석결과.json", analysis)

    dependency_path = write_dependency_manifest(
        work_dir / "초안-원본-의존관계.json", capcut_project, video_path
    )
    guide_path = write_guide(
        work_dir / "편집가이드.md",
        source=source,
        video_path=video_path,
        analysis=analysis,
        project_name=capcut_project,
        capcut_error=capcut_error,
        draft_dir=draft_dir,
    )
    report("obsidian", 94, "기획 인사이트를 옵시디언 세컨드 브레인에 연결합니다.")
    obsidian = archive_to_obsidian(
        analysis.planning,
        analysis,
        work_dir,
        capcut_project,
    )
    if obsidian.error:
        analysis.warnings.append(obsidian.error)
        write_analysis(work_dir / "분석결과.json", analysis)
    archive_path = archive_planning(
        output_root, work_dir, analysis.planning, capcut_project, obsidian.to_dict()
    )
    result = JobResult(
        source=source,
        work_dir=work_dir,
        video_path=video_path,
        analysis=analysis,
        srt_path=srt_path,
        guide_path=guide_path,
        dependency_path=dependency_path,
        planning_path=planning_path,
        archive_path=archive_path,
        obsidian=obsidian.to_dict(),
        capcut_project=capcut_project,
        capcut_error=capcut_error,
    )
    report("done", 100, "완료했습니다.")
    return result


def notify_complete(title: str, success: bool) -> None:
    if os.getenv("REELS_CAPCUT_NOTIFY", "1") == "0":
        return
    message = "CapCut 초안과 안전본이 준비됐습니다." if success else "안전본은 저장했지만 CapCut 초안을 확인해주세요."
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def open_outputs(result: JobResult) -> None:
    if os.getenv("REELS_CAPCUT_AUTO_OPEN", "1") == "0":
        return
    subprocess.run(["open", str(result.work_dir)], capture_output=True, timeout=10)
    if result.capcut_project:
        for app in ("/Applications/CapCut 2.app", "/Applications/CapCut.app"):
            if Path(app).exists():
                subprocess.run(["open", app], capture_output=True, timeout=10)
                break
