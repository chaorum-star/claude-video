"""Native draft generator: payload construction (no CapCut needed)."""
from __future__ import annotations

import json

import draft
import pytest

FAKE_CATALOG = {
    "text_animations": {
        "In": [{"title": "Wiping In", "effect_id": "111", "resource_id": "111", "md5": "a", "is_vip": False}],
        "Out": [{"title": "Fade Out", "effect_id": "222", "resource_id": "222", "md5": "b", "is_vip": False}],
        "Loop": [{"title": "Jitter", "effect_id": "333", "resource_id": "333", "md5": "c", "is_vip": False}],
        "Caption": [],
    }
}
FAKE_SHELL = {
    "id": "OLD", "duration": 123, "fps": 30.0, "new_version": "181.0.0",
    "materials": {"videos": [{"id": "v1"}], "texts": [], "material_animations": []},
    "tracks": [{"type": "video", "segments": [{"id": "s1"}]}],
    "canvas_config": {"width": 1080, "height": 1920},
}


def test_build_payload_replaces_tracks_and_keeps_schema():
    subs = [
        {"text": "첫 자막", "start": 0, "end": 2.5, "in": "Wiping In", "out": "Fade Out"},
        {"text": "둘째", "start": 2.5, "end": 5.0, "in": "Wiping In", "loop": "Jitter"},
    ]
    payload, duration = draft.build_payload(FAKE_SHELL, FAKE_CATALOG, subs)
    assert duration == 5.0
    assert payload["duration"] == 5_000_000
    assert payload["new_version"] == "181.0.0"          # shell schema kept
    assert payload["materials"]["videos"] == []          # shell content emptied
    assert len(payload["tracks"]) == 1
    track = payload["tracks"][0]
    assert track["type"] == "text"
    assert len(track["segments"]) == 2
    assert len(payload["materials"]["texts"]) == 2
    assert len(payload["materials"]["material_animations"]) == 2

    seg = track["segments"][0]
    assert seg["target_timerange"] == {"start": 0, "duration": 2_500_000}
    anim_ids = {a["id"] for a in payload["materials"]["material_animations"]}
    assert set(seg["extra_material_refs"]) <= anim_ids


def test_animation_entries_use_native_schema_and_timing():
    payload, _ = draft.build_payload(
        FAKE_SHELL, FAKE_CATALOG,
        [{"text": "x", "start": 0, "end": 3.0, "in": "Wiping In", "out": "Fade Out", "loop": "Jitter"}],
    )
    anims = payload["materials"]["material_animations"][0]["animations"]
    by_type = {a["type"]: a for a in anims}
    assert by_type["in"]["start"] == 0 and by_type["in"]["duration"] == 500_000
    assert by_type["loop"]["start"] == 500_000
    assert by_type["loop"]["duration"] == 3_000_000 - 500_000 - 500_000
    assert by_type["out"]["start"] == 3_000_000 - 500_000
    for a in anims:
        assert a["path"] == ""            # CapCut fills this on open (실기기 확인)
        assert a["resource_id"].isdigit()
        assert a["material_type"] == "sticker"


def test_text_material_range_matches_text_length():
    material = draft.make_text_material("안녕하세요")
    content = json.loads(material["content"])
    assert content["text"] == "안녕하세요"
    assert content["styles"][0]["range"] == [0, 5]


def test_unknown_animation_title_fails_loudly():
    with pytest.raises(SystemExit, match="효과가 없습니다"):
        draft.build_payload(
            FAKE_SHELL, FAKE_CATALOG,
            [{"text": "x", "start": 0, "end": 1, "in": "없는 효과"}],
        )


def test_zero_length_subtitle_rejected():
    with pytest.raises(SystemExit):
        draft.build_payload(FAKE_SHELL, FAKE_CATALOG, [{"text": "x", "start": 1, "end": 1}])


def test_build_payload_places_sfx_on_audio_track():
    sfx = [
        {"path": "/tmp/a.wav", "dest_path": "/proj/Resources/replicate_sfx/a.wav",
         "time": 0.5, "duration": 0.4, "name": "Whoosh"},
        {"path": "/tmp/b.wav", "dest_path": "/proj/Resources/replicate_sfx/b.wav",
         "time": 4.8, "duration": 0.6},
    ]
    payload, duration = draft.build_payload(
        FAKE_SHELL, FAKE_CATALOG,
        [{"text": "x", "start": 0, "end": 2.5, "in": "Wiping In"}], sfx=sfx,
    )
    assert duration == pytest.approx(5.4)  # last sfx end extends the timeline
    types = [t["type"] for t in payload["tracks"]]
    assert types == ["text", "audio"]
    audio = payload["tracks"][1]
    assert len(audio["segments"]) == 2
    seg = audio["segments"][0]
    assert seg["target_timerange"] == {"start": 500_000, "duration": 400_000}
    assert seg["source_timerange"] == {"start": 0, "duration": 400_000}
    audios = {m["id"]: m for m in payload["materials"]["audios"]}
    speeds = {m["id"] for m in payload["materials"]["speeds"]}
    assert audios[seg["material_id"]]["name"] == "Whoosh"
    assert audios[seg["material_id"]]["path"].endswith("a.wav")
    assert set(seg["extra_material_refs"]) <= speeds


def test_build_payload_without_sfx_has_no_audio_track():
    payload, _ = draft.build_payload(
        FAKE_SHELL, FAKE_CATALOG, [{"text": "x", "start": 0, "end": 1}])
    assert [t["type"] for t in payload["tracks"]] == ["text"]
