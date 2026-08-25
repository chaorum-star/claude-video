"""Effect catalog + curated feature map: consistency and lookup contract."""
from __future__ import annotations

import effects
import pytest


def test_assets_validate_clean():
    assert effects.validate() == []


def test_catalog_counts_match_pycapcut_inventory():
    catalog = effects.load_catalog()
    assert catalog["counts"] == {"intro": 182, "loop": 81, "outro": 100}
    for category, entries in catalog["categories"].items():
        assert len(entries) == catalog["counts"][category]
        for entry in entries:
            assert entry["effect_id"].isdigit()
            assert entry["resource_id"].isdigit()
            assert entry["lang"] in ("zh", "en")


def test_lookup_matched_feature_resolves_catalog_ids():
    result = effects.lookup("entrance", "typewriter")
    assert result["matched"] is True
    assert result["fallback"] is None
    top = result["candidates"][0]
    assert top["name"] == "打字机"
    assert top["confidence"] == "high"
    assert top["effect_id"].isdigit()
    assert top["verified"] is False  # unverified until the M3 CapCut check


def test_lookup_unknown_feature_is_explicit_fallback_not_silent():
    result = effects.lookup("exit", "rainbow")
    assert result["matched"] is False
    assert result["candidates"] == []
    assert result["fallback"]["name"] == "渐隐"
    assert result["fallback"]["effect_id"].isdigit()
    assert "수동 확인 필요" in result["note"]


def test_lookup_unknown_loop_feature_has_no_default():
    result = effects.lookup("loop", "sparkle")
    assert result["matched"] is False
    assert result["fallback"] is None  # a subtitle with no loop effect is normal


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
        {"name": "존재하지 않는 효과", "label_ko": "x", "confidence": "high", "verified": False}
    ]
    errors = effects.validate(catalog=catalog, fmap=fmap)
    assert any("존재하지 않는 효과" in err for err in errors)
