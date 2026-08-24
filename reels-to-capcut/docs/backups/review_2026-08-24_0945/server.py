from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import traceback
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .pipeline import notify_complete, open_outputs, process_source, process_template
from .obsidian_archive import discover_obsidian_vault, obsidian_status, obsidian_uri
from .performance import record_template_use, save_publication, validated_template_dir
from .preflight import status as preflight_status
from .transcribe import transcriber_status
from .utils import is_instagram_reel_url, is_instagram_url


OUTPUT_ROOT = Path(os.getenv("REELS_CAPCUT_OUTPUT_DIR", str(Path.home() / "Movies/ReelsToCapCut"))).expanduser()
STATIC_DIR = Path(__file__).with_name("static")


@dataclass
class Job:
    id: str
    source: str
    kind: str = "analysis"
    replacement_text: str = ""
    template_work_dir: str | None = None
    project_title: str = ""
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    message: str = "대기 중"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    result: dict[str, Any] | None = None
    error: str | None = None


class JobManager:
    def __init__(self, output_root: Path = OUTPUT_ROOT) -> None:
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.jobs: list[Job] = []
        self.pending: queue.Queue[Job] = queue.Queue()
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._work, name="reels-capcut-worker", daemon=True)
        self.worker.start()

    def submit(self, sources: list[str], replacement_text: str = "") -> list[Job]:
        created = []
        for source in sources[:20]:
            job = Job(id=uuid.uuid4().hex, source=source, replacement_text=replacement_text)
            with self.lock:
                self.jobs.append(job)
            self.pending.put(job)
            created.append(job)
        self._persist()
        return created

    def submit_template(
        self, template_work_dir: Path | str, replacement_text: str = "", project_title: str = ""
    ) -> Job:
        target = validated_template_dir(self.output_root, template_work_dir)
        if not (target / "분석결과.json").is_file():
            raise ValueError("선택한 레퍼런스의 분석 결과를 찾지 못했습니다.")
        job = Job(
            id=uuid.uuid4().hex,
            source=f"template:{target.name}",
            kind="template",
            replacement_text=replacement_text,
            template_work_dir=str(target),
            project_title=project_title,
            message="템플릿 복제 대기 중",
        )
        with self.lock:
            self.jobs.append(job)
        self.pending.put(job)
        self._persist()
        return job

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            jobs = [asdict(job) for job in reversed(self.jobs)]
        return {
            "jobs": jobs,
            "output_root": str(self.output_root),
            "transcriber": transcriber_status(),
            "preflight": preflight_status(),
            "planning_archive": self.planning_archive(),
            "obsidian": obsidian_status(),
        }

    def planning_archive(self) -> list[dict[str, Any]]:
        path = self.output_root / "기획분석_아카이브" / "index.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            return list(reversed(payload[-50:])) if isinstance(payload, list) else []
        except (OSError, ValueError):
            return []

    def _update(self, job: Job, stage: str, percent: int, message: str) -> None:
        with self.lock:
            job.stage = stage
            job.progress = percent
            job.message = message
        self._persist()

    def _work(self) -> None:
        while True:
            job = self.pending.get()
            try:
                with self.lock:
                    job.status = "running"
                if job.kind == "template" and job.template_work_dir:
                    result = process_template(
                        job.template_work_dir,
                        self.output_root,
                        lambda stage, percent, message: self._update(job, stage, percent, message),
                        job.replacement_text,
                        job.project_title,
                    )
                    record_template_use(
                        self.output_root,
                        job.template_work_dir,
                        capcut_project=result.capcut_project,
                        result_work_dir=result.work_dir,
                    )
                else:
                    result = process_source(
                        job.source,
                        self.output_root,
                        lambda stage, percent, message: self._update(job, stage, percent, message),
                        job.replacement_text,
                    )
                with self.lock:
                    job.status = "done" if not result.capcut_error else "partial"
                    job.result = result.to_dict()
                    job.message = "완료" if not result.capcut_error else "안전본 완료 · CapCut 확인 필요"
                notify_complete("릴스→캡컷", not result.capcut_error)
                open_outputs(result)
            except Exception as exc:
                with self.lock:
                    job.status = "failed"
                    job.stage = "failed"
                    job.error = str(exc)
                    job.message = "처리 실패"
                    job.progress = 100
                (self.output_root / "last-error.log").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
            finally:
                self._persist()
                self.pending.task_done()

    def _persist(self) -> None:
        try:
            snapshot = self.snapshot_without_status()
            temporary = self.output_root / "queue.json.tmp"
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.output_root / "queue.json")
        except OSError:
            pass

    def snapshot_without_status(self) -> list[dict[str, Any]]:
        with self.lock:
            return [asdict(job) for job in self.jobs]


MANAGER = JobManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "ReelsToCapCut/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self.send_json(MANAGER.snapshot())
            return
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path == "/":
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/fonts/"):
            target = (STATIC_DIR / path.lstrip("/")).resolve()
            root = STATIC_DIR.resolve()
            if root in target.parents and target.is_file():
                content_type = "font/woff2" if target.suffix == ".woff2" else "font/ttf"
                self.send_file(target, content_type)
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = self.read_json()
        if path == "/api/jobs":
            raw = payload.get("sources", "")
            sources = raw if isinstance(raw, list) else str(raw).splitlines()
            cleaned = [str(value).strip() for value in sources if str(value).strip()]
            if not cleaned:
                self.send_json({"error": "릴스 링크를 한 줄에 하나씩 넣어주세요."}, HTTPStatus.BAD_REQUEST)
                return
            profile_urls = [value for value in cleaned if is_instagram_url(value) and not is_instagram_reel_url(value)]
            if profile_urls:
                self.send_json({
                    "error": (
                        "Instagram 프로필 주소는 분석할 영상을 특정할 수 없습니다. "
                        "프로필에서 원하는 릴스를 연 뒤 공유 → 링크 복사로 받은 "
                        "https://www.instagram.com/reel/.../ 주소를 넣어주세요."
                    )
                }, HTTPStatus.BAD_REQUEST)
                return
            replacement_text = str(payload.get("replacement_text", "")).strip()
            jobs = MANAGER.submit(cleaned, replacement_text)
            self.send_json({"jobs": [asdict(job) for job in jobs]}, HTTPStatus.CREATED)
            return
        if path == "/api/template-jobs":
            replacement_text = str(payload.get("replacement_text", "")).strip()
            project_title = str(payload.get("project_title", "")).strip()[:100]
            if len(replacement_text) > 20_000:
                self.send_json({"error": "자막 원고가 너무 깁니다. 20,000자 이내로 입력해주세요."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                job = MANAGER.submit_template(
                    str(payload.get("template_work_dir", "")), replacement_text, project_title
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"job": asdict(job)}, HTTPStatus.CREATED)
            return
        if path == "/api/publications":
            try:
                result = save_publication(
                    MANAGER.output_root,
                    str(payload.get("template_work_dir", "")),
                    payload,
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json(result, HTTPStatus.CREATED)
            return
        if path == "/api/open-folder":
            target = Path(str(payload.get("path", ""))).expanduser().resolve()
            root = MANAGER.output_root.resolve()
            if target != root and root not in target.parents:
                self.send_json({"error": "결과 폴더 밖의 경로는 열 수 없습니다."}, HTTPStatus.BAD_REQUEST)
                return
            subprocess.run(["open", str(target)], capture_output=True, timeout=10)
            self.send_json({"ok": True})
            return
        if path == "/api/open-capcut":
            app = next((Path(value) for value in ("/Applications/CapCut 2.app", "/Applications/CapCut.app") if Path(value).exists()), None)
            if app is None:
                self.send_json({"error": "CapCut 앱을 찾지 못했습니다."}, HTTPStatus.NOT_FOUND)
                return
            subprocess.run(["open", str(app)], capture_output=True, timeout=10)
            self.send_json({"ok": True})
            return
        if path == "/api/open-obsidian":
            vault = discover_obsidian_vault()
            if vault is None:
                self.send_json({"error": "연결된 옵시디언 보관함을 찾지 못했습니다."}, HTTPStatus.NOT_FOUND)
                return
            requested = str(payload.get("path") or "10_SOURCE NOTES/릴스 콘텐츠 인사이트 허브.md")
            target = Path(requested).expanduser()
            target = target.resolve() if target.is_absolute() else (vault / target).resolve()
            root = vault.resolve()
            if target != root and root not in target.parents:
                self.send_json({"error": "옵시디언 보관함 밖의 경로는 열 수 없습니다."}, HTTPStatus.BAD_REQUEST)
                return
            relative = target.relative_to(root)
            subprocess.run(["open", obsidian_uri(root, relative)], capture_output=True, timeout=10)
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("요청이 너무 큽니다.")
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_file(self, path: Path, content_type: str) -> None:
        encoded = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[웹] {fmt % args}")


def run_server(host: str = "127.0.0.1", port: int = 8876, *, open_browser: bool = True) -> None:
    server = None
    for candidate in range(port, port + 15):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(f"사용 가능한 포트를 찾지 못했습니다: {port}~{port + 14}")
    url = f"http://{host}:{port}"
    print(f"\n릴스→캡컷 실행 중: {url}")
    print("종료: Ctrl+C\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server(port=int(os.getenv("REELS_CAPCUT_PORT", "8876")))
