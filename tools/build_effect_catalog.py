#!/usr/bin/env python3
"""Bootstrap skills/replicate/assets/capcut-effect-catalog.json from pyCapCut.

pyCapCut (https://github.com/GuanYixuan/pyCapCut) ships CapCut text-animation
metadata (name, VIP flag, default duration, effect/resource IDs) as Python
enums. This tool regex-parses those enum files — no import, so pyCapCut's own
dependencies are never needed — and writes the catalog asset that
skills/replicate/scripts/effects.py serves lookups from.

Usage:
    python3 tools/build_effect_catalog.py --pycapcut-dir /path/to/pyCapCut

Re-run whenever pyCapCut updates its metadata. The curated feature map
(capcut-effect-map.json) is a separate, hand-maintained file and is never
touched by this tool.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CATEGORY_FILES = {
    "intro": "text_intro.py",
    "loop": "text_loop.py",
    "outro": "text_outro.py",
}

ENTRY_RE = re.compile(
    r"^\s*(?P<member>\w+)\s*=\s*AnimationMeta\(\s*"
    r'"(?P<name>[^"]+)"\s*,\s*'
    r"(?P<vip>True|False)\s*,\s*"
    r"(?P<duration>[0-9.]+)\s*,\s*"
    r'"(?P<effect_id>\d+)"\s*,\s*'
    r'"(?P<resource_id>\d+)"\s*,\s*'
    r'"(?P<md5>[0-9a-f]+)"\s*\)',
    re.MULTILINE,
)

CJK_RE = re.compile(r"[㐀-鿿]")


def parse_category(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    entries = []
    for m in ENTRY_RE.finditer(text):
        name = m.group("name")
        entries.append({
            "name": name,
            "member": m.group("member"),
            "vip": m.group("vip") == "True",
            "duration_s": float(m.group("duration")),
            "effect_id": m.group("effect_id"),
            "resource_id": m.group("resource_id"),
            "md5": m.group("md5"),
            "lang": "zh" if CJK_RE.search(name) else "en",
        })
    if not entries:
        raise SystemExit(f"No AnimationMeta entries parsed from {path} — format changed?")
    return entries


def source_commit(pycapcut_dir: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(pycapcut_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pycapcut-dir", required=True, help="Path to a pyCapCut checkout")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent
                    / "skills" / "replicate" / "assets" / "capcut-effect-catalog.json"),
        help="Output catalog path (default: skills/replicate/assets/capcut-effect-catalog.json)",
    )
    args = parser.parse_args()

    meta_dir = Path(args.pycapcut_dir).resolve() / "pycapcut" / "metadata"
    if not meta_dir.is_dir():
        raise SystemExit(f"pycapcut/metadata not found under {args.pycapcut_dir}")

    categories = {
        cat: parse_category(meta_dir / fname) for cat, fname in CATEGORY_FILES.items()
    }
    catalog = {
        "schema_version": 1,
        "source": {
            "library": "pyCapCut",
            "url": "https://github.com/GuanYixuan/pyCapCut",
            "commit": source_commit(Path(args.pycapcut_dir)),
        },
        "counts": {cat: len(entries) for cat, entries in categories.items()},
        "categories": categories,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {out_path} ({', '.join(f'{c}={n}' for c, n in catalog['counts'].items())})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
