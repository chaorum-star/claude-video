#!/usr/bin/env python3
"""Look up CapCut text-animation candidates for an observed subtitle feature.

Two bundled assets under ../assets/:
  - capcut-effect-catalog.json — generated from pyCapCut metadata
    (tools/build_effect_catalog.py); the full id/resource inventory.
  - capcut-effect-map.json — hand-curated feature→effect candidates with
    confidence, per SPEC F2-1/F2-2.

`lookup` never silently picks an effect for an unknown feature: it returns
``matched: false`` plus the phase default and a needs-review note, so the
caller must surface "수동 확인 필요" in the report (F2-2).

Usage:
    effects.py lookup <entrance|loop|exit> <feature>
    effects.py features            # print the allowed feature vocabulary
    effects.py validate            # assets consistency check (exit 1 on failure)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
CATALOG_PATH = ASSETS_DIR / "capcut-effect-catalog.json"
MAP_PATH = ASSETS_DIR / "capcut-effect-map.json"

# Feature-map phases → catalog categories.
PHASE_TO_CATEGORY = {"entrance": "intro", "loop": "loop", "outro": "outro", "exit": "outro"}
PHASES = ("entrance", "loop", "exit")


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_map(path: Path = MAP_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_index(catalog: dict) -> dict[str, dict[str, dict]]:
    """{category: {effect name: entry}} for O(1) candidate resolution."""
    return {
        category: {entry["name"]: entry for entry in entries}
        for category, entries in catalog["categories"].items()
    }


def lookup(phase: str, feature: str, catalog: dict | None = None, fmap: dict | None = None) -> dict:
    """Resolve a feature to catalog-backed candidates, or an explicit fallback."""
    if phase not in PHASES:
        raise ValueError(f"Unknown phase {phase!r} (expected one of {PHASES})")
    catalog = catalog or load_catalog()
    fmap = fmap or load_map()
    index = catalog_index(catalog)[PHASE_TO_CATEGORY[phase]]

    candidates = fmap["map"].get(phase, {}).get(feature)
    if not candidates:
        default = fmap["defaults"].get(phase)
        return {
            "phase": phase,
            "feature": feature,
            "matched": False,
            "candidates": [],
            "fallback": {**default, **index.get(default["name"], {})} if default else None,
            "note": "매핑 없음 — 리포트에 '수동 확인 필요'로 표기할 것 (F2-2). "
                    "기본 효과는 임시 배치일 뿐 확정이 아님.",
        }

    resolved = []
    for cand in candidates:
        entry = index.get(cand["name"])
        if entry is None:
            raise KeyError(
                f"Feature map references {cand['name']!r} which is not in the "
                f"{PHASE_TO_CATEGORY[phase]!r} catalog — run effects.py validate"
            )
        resolved.append({**cand, **entry})
    return {
        "phase": phase,
        "feature": feature,
        "matched": True,
        "candidates": resolved,
        "fallback": None,
        "note": None,
    }


def validate(catalog: dict | None = None, fmap: dict | None = None) -> list[str]:
    """Return a list of consistency errors (empty = valid)."""
    errors: list[str] = []
    try:
        catalog = catalog or load_catalog()
    except (OSError, json.JSONDecodeError) as exc:
        return [f"catalog unreadable: {exc}"]
    try:
        fmap = fmap or load_map()
    except (OSError, json.JSONDecodeError) as exc:
        return [f"feature map unreadable: {exc}"]

    index = catalog_index(catalog)
    for category, entries in catalog["categories"].items():
        names = [e["name"] for e in entries]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            errors.append(f"catalog {category}: duplicate names {dupes}")

    vocab = fmap.get("feature_vocabulary", {})
    for phase, features in fmap["map"].items():
        if phase not in PHASES:
            errors.append(f"feature map has unknown phase {phase!r}")
            continue
        category = PHASE_TO_CATEGORY[phase]
        for feature, candidates in features.items():
            if vocab.get(phase) and feature not in vocab[phase]:
                errors.append(f"{phase}.{feature}: not in feature_vocabulary")
            if not candidates:
                errors.append(f"{phase}.{feature}: empty candidate list")
            for cand in candidates:
                if cand["name"] not in index[category]:
                    errors.append(
                        f"{phase}.{feature}: candidate {cand['name']!r} not in {category} catalog"
                    )
                if cand.get("confidence") not in ("high", "medium", "low"):
                    errors.append(f"{phase}.{feature}: {cand['name']!r} bad confidence")

    for phase, default in fmap.get("defaults", {}).items():
        if default and default["name"] not in index[PHASE_TO_CATEGORY[phase]]:
            errors.append(f"defaults.{phase}: {default['name']!r} not in catalog")
    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[0]

    if cmd == "validate":
        errors = validate()
        if errors:
            for err in errors:
                print(f"INVALID: {err}", file=sys.stderr)
            return 1
        catalog = load_catalog()
        print(json.dumps({"valid": True, "counts": catalog["counts"]}, ensure_ascii=False))
        return 0

    if cmd == "features":
        print(json.dumps(load_map()["feature_vocabulary"], ensure_ascii=False, indent=2))
        return 0

    if cmd == "lookup":
        if len(argv) != 3:
            print("usage: effects.py lookup <entrance|loop|exit> <feature>", file=sys.stderr)
            return 2
        try:
            result = lookup(argv[1], argv[2])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"Unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
