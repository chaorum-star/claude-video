"""UI effect catalog + curated feature map v2: consistency and lookup contract."""
from __future__ import annotations

import effects
import pytest


def test_assets_validate_clean():
    assert effects.validate() == []


def test_catalog_covers_all_ui_categories():
    catalog = effects.load_catalog()
    counts = catalog["text_animation_counts"]
    for category in ("In", "Out", "Loop", "Caption"):
        assert counts[category] > 0
        assert counts[category] == len(catalog["text_animations"][category])
    for entries in catalog["text_animations"].values():
        for entry in entries:
            assert entry["title"]
            assert entry["effect_id"].isdigit()


def test_lookup_matched_feature_resolves_ui_titles():
    result = effects.lookup("entrance", "typewriter")
    assert result["matched"] is True
    assert result["fallback"] is None
    top = result["candidates"][0]
    assert top["title"] == "Preview Type"  # 실제 캡컷 UI 검색창에 그대로 입력 가능한 이름
    assert top["confidence"] == "high"
    assert top["effect_id"].isdigit()
    assert top["verified"] is True  # 2026-08-26 실기기 재생 검증 완료


def test_lookup_unknown_feature_is_explicit_miss_not_silent():
    result = effects.lookup("exit", "rainbow")
    assert result["matched"] is False
    assert result["candidates"] == []
    assert result["fallback"] is None  # v2: 기본은 무애니메이션(하드컷)
    assert "수동 확인 필요" in result["note"]


def test_lookup_rejects_unknown_phase():
    with pytest.raises(ValueError):
        effects.lookup("midroll", "fade")


def test_every_vocabulary_phase_matches_map_phases():
    fmap = effects.load_map()
    assert set(fmap["map"]) <= set(effects.PHASES)
    assert set(fmap["feature_vocabulary"]) == set(effects.PHASES)


def test_validate_flags_dangling_candidate():
    catalog = effects.load_catalog()
    fmap = effects.load_map()
    fmap["map"]["entrance"]["fade"] = [
        {"title": "존재하지 않는 효과", "confidence": "high", "verified": False}
    ]
    errors = effects.validate(catalog=catalog, fmap=fmap)
    assert any("존재하지 않는 효과" in err for err in errors)
