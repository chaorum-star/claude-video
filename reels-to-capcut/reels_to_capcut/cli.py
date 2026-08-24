from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .pipeline import process_source


def main() -> None:
    parser = argparse.ArgumentParser(description="릴스 또는 로컬 영상을 CapCut 초안으로 변환")
    parser.add_argument("source")
    parser.add_argument("--text", help="레퍼런스 타이밍에 넣을 새 자막 원고")
    parser.add_argument("--script-file", help="새 자막 원고 UTF-8 파일")
    parser.add_argument(
        "--output-root",
        default=os.getenv("REELS_CAPCUT_OUTPUT_DIR", str(Path.home() / "Movies/ReelsToCapCut")),
    )
    args = parser.parse_args()
    replacement_text = args.text
    if args.script_file:
        replacement_text = Path(args.script_file).expanduser().read_text(encoding="utf-8")
    result = process_source(
        args.source,
        Path(args.output_root).expanduser(),
        lambda stage, percent, message: print(f"[{percent:3d}%] {stage}: {message}"),
        replacement_text,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
