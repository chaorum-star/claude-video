#!/usr/bin/env python3
"""Index the locally-installed CapCut's resource caches into JSON assets.

CapCut desktop caches its effect-market API responses in SQLite databases
(`Cache/ressdk_db/*/rp.db`, table `http_cache`). Those responses carry the
catalog the user actually sees in THEIR CapCut UI — localized display titles,
effect ids, md5s, and (for sound effects) preview URLs. This tool extracts:

  - text animations   (panel with categories In / Out / Loop / Caption)
  - sound-effect collections + every cached collection song list

into `skills/replicate/assets/capcut-ui-catalog.json`, which effects.py /
sfx_match.py use to answer "which effect in YOUR CapCut is this?" — the whole
point being to kill catalog-search time.

Coverage note: text animations arrive via a get_all_resource prefetch (complete
catalog); sound-effect song lists are cached ONLY for collections that were
browsed at least once in the CapCut UI. The output records which collections
have no cached songs so nobody mistakes partial coverage for complete.

Usage:
    python3 tools/index_capcut_resources.py [--capcut-data DIR] [--out FILE]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DATA_DIRS = [
    "~/Library/Containers/com.lemon.lvoverseas/Data/Movies/CapCut/User Data",
    "~/Movies/CapCut/User Data",
]
TEXT_ANIM_CATEGORIES = {"in", "out", "loop", "caption"}


def find_dbs(data_dir: str | None) -> list[str]:
    roots = [data_dir] if data_dir else DEFAULT_DATA_DIRS
    dbs: list[str] = []
    for root in roots:
        pattern = os.path.expanduser(os.path.join(root, "Cache", "ressdk_db", "*", "rp.db"))
        dbs.extend(glob.glob(pattern))
    return sorted(set(dbs))


def iter_cached_responses(dbs: list[str]):
    for db in dbs:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for url, body in con.execute("SELECT url, response_body FROM http_cache"):
                try:
                    yield url, json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    continue
        finally:
            con.close()


def extract_text_animations(payload: dict) -> dict | None:
    """A panel whose categories are exactly In/Out/Loop/Caption(±) is the
    text-animation panel. Returns {category_name: [items]} or None."""
    data = payload.get("data") or {}
    categories = data.get("categories")
    resources = data.get("category_resources")
    if not categories or not resources:
        return None
    names = {str(c.get("category_id") or c.get("id")): (c.get("category_name") or c.get("name") or "").strip()
             for c in categories}
    if not names or not {n.lower() for n in names.values()} <= TEXT_ANIM_CATEGORIES:
        return None

    out: dict[str, list] = {}
    for cid, cres in resources.items():
        cat = names.get(str(cid))
        if not cat:
            continue
        bucket = out.setdefault(cat, [])
        seen = {e["effect_id"] for e in bucket}
        for item in cres.get("effect_item_list", []):
            ca = item.get("common_attr", {})
            eid = ca.get("effect_id")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            bucket.append({
                "title": ca.get("title"),
                "effect_id": eid,
                "resource_id": ca.get("resource_id") or eid,
                "md5": ca.get("md5"),
                "is_vip": bool(ca.get("is_vip")),
            })
    return out or None


def extract_sfx(payloads: list[tuple[str, dict]]) -> dict:
    collections: list[dict] = []
    songs_by_key: dict[str, list] = {}
    for url, payload in payloads:
        data = payload.get("data") or {}
        if "get_music_effect_collections" in url:
            for coll in data.get("collections", []):
                collections.append({"id": coll.get("id"), "name": coll.get("name")})
        elif "get_collection_songs" in url:
            songs = [
                {
                    "id": s.get("id"),
                    "title": s.get("title"),
                    "author": s.get("author"),
                    "duration_s": s.get("duration"),
                    "preview_url": s.get("preview_url"),
                }
                for s in data.get("songs", [])
            ]
            if songs:
                songs_by_key[url.rsplit("_", 1)[-1]] = songs

    # Collection ↔ song-list joins are by browsing order, which the cache does
    # not record; expose lists keyed by cache id and let the consumer match by
    # inspecting titles. What matters most is title + preview_url + duration.
    return {"collections": collections, "cached_song_lists": songs_by_key}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capcut-data", default=None, help="CapCut 'User Data' dir override")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent
                    / "skills" / "replicate" / "assets" / "capcut-ui-catalog.json"),
    )
    args = parser.parse_args()

    dbs = find_dbs(args.capcut_data)
    if not dbs:
        raise SystemExit(
            "No CapCut resource DB (ressdk_db/*/rp.db) found. Is CapCut installed and "
            "has it been launched at least once?"
        )

    payloads = list(iter_cached_responses(dbs))
    text_animations = None
    for _, payload in payloads:
        found = extract_text_animations(payload)
        if found and (text_animations is None
                      or sum(map(len, found.values())) > sum(map(len, text_animations.values()))):
            text_animations = found
    sfx = extract_sfx(payloads)

    catalog = {
        "schema_version": 1,
        "source": {"kind": "capcut-local-cache", "dbs": dbs},
        "coverage_note": (
            "text_animations는 get_all_resource 프리페치라 전체 카탈로그. "
            "sound_effects.cached_song_lists는 캡컷 UI에서 브라우즈한 컬렉션만 캐시됨 — "
            "빠진 컬렉션은 캡컷에서 한 번 열어본 뒤 이 도구를 재실행할 것."
        ),
        "text_animations": text_animations or {},
        "text_animation_counts": {k: len(v) for k, v in (text_animations or {}).items()},
        "sound_effects": sfx,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    anim_total = sum(catalog["text_animation_counts"].values())
    song_total = sum(len(v) for v in sfx["cached_song_lists"].values())
    print(
        f"Wrote {out_path} — text animations {anim_total} "
        f"({catalog['text_animation_counts']}), SFX collections {len(sfx['collections'])}, "
        f"cached SFX items {song_total}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
