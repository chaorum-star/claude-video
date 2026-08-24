from __future__ import annotations

import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .analyze import analyze_video
from .artifacts import write_analysis, write_dependency_manifest, write_guide, write_srt
from .capcut import DEFAULT_DRAFT_DIR, create_capcut_draft
from .download import acquire_video
from .editing_grammar import apply_replacement_text
from .models import Analysis, JobResult, analysis_from_dict
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
    draft_video_path = video_path
    try:
        draft_video_path = create_replaceable_video_placeholder(work_dir, analysis)
        capcut_project = create_capcut_draft(
            draft_video_path, analysis, title, draft_dir, reference_video_path=video_path
        )
        write_analysis(work_dir / "분석결과.json", analysis)
    except Exception as exc:
        capcut_error = str(exc)
        analysis.warnings.append(f"CapCut 초안 생성 실패: {capcut_error}")
        write_analysis(work_dir / "분석결과.json", analysis)

    dependency_path = write_dependency_manifest(
        work_dir / "초안-원본-의존관계.json", capcut_project, draft_video_path,
        reference_video_path=video_path,
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


def process_template(
    template_work_dir: Path | str,
    output_root: Path,
    progress: Progress | None = None,
    replacement_text: str | None = None,
    project_title: str | None = None,
) -> JobResult:
    """Create a new editable CapCut draft from saved timing/effect analysis."""
    report = progress or (lambda _stage, _percent, _message: None)
    template_dir = Path(template_work_dir).expanduser().resolve()
    analysis_path = template_dir / "분석결과.json"
    if not analysis_path.is_file():
        raise ValueError("선택한 레퍼런스의 분석결과.json을 찾지 못했습니다.")
    video_path = find_template_video(template_dir)

    report("template-load", 10, "저장된 자막·효과·타이밍을 불러옵니다.")
    payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("저장된 분석 결과 형식이 올바르지 않습니다.")
    analysis = analysis_from_dict(payload)
    if replacement_text and replacement_text.strip():
        apply_replacement_text(analysis, replacement_text.strip())

    work_dir = make_job_dir(output_root, "template_reuse")
    video_path = materialize_template_video(video_path, work_dir)
    write_analysis(work_dir / "분석결과.json", analysis)
    planning_path = write_planning_report(work_dir / "기획분석.md", analysis.planning)
    srt_path = write_srt(work_dir / "말 자막.srt", analysis.speech)

    topic = str(analysis.planning.get("topic") or template_dir.name)
    title = (project_title or "").strip() or f"{topic} 재사용"
    report("template-capcut", 62, "기존 편집 문법으로 새 CapCut 초안을 만듭니다.")
    draft_dir = Path(os.getenv("REELS_CAPCUT_DRAFT_DIR", str(DEFAULT_DRAFT_DIR))).expanduser()
    capcut_project: str | None = None
    capcut_error: str | None = None
    draft_video_path = video_path
    try:
        draft_video_path = create_replaceable_video_placeholder(work_dir, analysis)
        capcut_project = create_capcut_draft(
            draft_video_path, analysis, title, draft_dir, reference_video_path=video_path
        )
        write_analysis(work_dir / "분석결과.json", analysis)
    except Exception as exc:
        capcut_error = str(exc)
        analysis.warnings.append(f"CapCut 템플릿 생성 실패: {capcut_error}")
        write_analysis(work_dir / "분석결과.json", analysis)

    dependency_path = write_dependency_manifest(
        work_dir / "초안-원본-의존관계.json", capcut_project, draft_video_path,
        reference_video_path=video_path,
    )
    source = str(analysis.planning.get("source") or template_dir)
    guide_path = write_guide(
        work_dir / "편집가이드.md",
        source=source,
        video_path=video_path,
        analysis=analysis,
        project_name=capcut_project,
        capcut_error=capcut_error,
        draft_dir=draft_dir,
    )
    (work_dir / "템플릿-재사용.json").write_text(
        json.dumps({
            "template_work_dir": str(template_dir),
            "source": source,
            "project_title": title,
            "capcut_project": capcut_project,
            "replacement_text": (replacement_text or "").strip(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report("done", 100, "템플릿 초안이 준비됐습니다. CapCut에서 영상 트랙만 교체하세요.")
    return JobResult(
        source=source,
        work_dir=work_dir,
        video_path=video_path,
        analysis=analysis,
        srt_path=srt_path,
        guide_path=guide_path,
        dependency_path=dependency_path,
        planning_path=planning_path,
        capcut_project=capcut_project,
        capcut_error=capcut_error,
    )


def find_template_video(template_dir: Path) -> Path:
    preferred = [
        path for path in template_dir.glob("source.*")
        if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
    ]
    if not preferred:
        raise ValueError("선택한 레퍼런스의 원본 영상을 찾지 못했습니다.")
    return max(preferred, key=lambda path: path.stat().st_size).resolve()


def materialize_template_video(video_path: Path, work_dir: Path) -> Path:
    """Keep a reviewable, stable source video inside every reuse job.

    A hard link avoids duplicating a potentially large reel on the same disk.
    Filesystems that do not support hard links fall back to an ordinary copy.
    """
    source = video_path.expanduser().resolve()
    suffix = source.suffix.lower() or ".mp4"
    target = work_dir / f"source{suffix}"
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target.resolve()


def create_replaceable_video_placeholder(work_dir: Path, analysis: Analysis) -> Path:
    """번인 자막이 없는 교체용 메인 영상을 만든다.

    레퍼런스 MP4의 글자는 화소에 이미 합성돼 있어 CapCut 텍스트를
    수정해도 사라지지 않는다. 메인 트랙은 무자막 그리드로 두고,
    레퍼런스는 별도 숨김 트랙으로 보존한다.
    """
    target = work_dir / "교체용-무자막-영상.mp4"
    duration = max(0.1, float(analysis.duration))
    width = max(16, int(analysis.width) // 2 * 2)
    height = max(16, int(analysis.height) // 2 * 2)
    source = f"color=c=#202027:s={width}x{height}:r=30:d={duration:.6f}"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source,
            "-vf", "drawgrid=w=iw/6:h=ih/10:t=2:c=white@0.10",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ],
        check=True,
        timeout=300,
    )
    return target.resolve()


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
