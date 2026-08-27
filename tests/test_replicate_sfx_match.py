"""SFX library matching: feature extraction, ranking, confidence gate."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import sfx_match


def _render(path: Path, graph: str, duration: float) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-t", f"{duration:.3f}", "-i", graph,
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


# Three distinct "library effects": a low thump, a high ding, a noise whoosh.
LIBRARY = {
    "thump": ("sine=frequency=150:sample_rate=16000,afade=t=out:d=0.25", 0.25),
    "ding": ("sine=frequency=2500:sample_rate=16000,afade=t=out:d=0.5", 0.5),
    "whoosh": ("anoisesrc=r=16000:colour=pink:seed=7,afade=t=in:d=0.15,afade=t=out:st=0.15:d=0.25", 0.4),
}


@pytest.fixture(scope="session")
def sfx_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    lib = tmp_path_factory.mktemp("sfxlib")
    for name, (graph, dur) in LIBRARY.items():
        _render(lib / f"{name}.wav", graph, dur)
    return lib


@pytest.fixture(scope="session")
def sfx_index(sfx_library: Path) -> dict:
    return sfx_match.build_index(sfx_library)


def test_index_covers_all_library_files(sfx_index: dict):
    assert sfx_index["entry_count"] == len(LIBRARY)
    assert sfx_index["skipped"] == []
    for entry in sfx_index["entries"]:
        assert len(entry["envelope"]) == sfx_match.ENVELOPE_POINTS
        assert len(entry["spectrum"]) == sfx_match.SPECTRUM_BINS
        assert entry["duration"] > 0


def test_exact_clip_matches_itself_first(sfx_library: Path, sfx_index: dict):
    for name in LIBRARY:
        result = sfx_match.match_clip(sfx_library / f"{name}.wav", sfx_index, top=3, threshold=60)
        assert result["matches"][0]["name"] == name
        assert result["matches"][0]["score"] > 95
        assert result["confident"] is True


def test_variant_clip_still_matches_right_effect(sfx_index: dict, tmp_path: Path):
    # Same ding, quieter and slightly detuned — as it would sound mixed into a video.
    variant = tmp_path / "ding_variant.wav"
    _render(variant, "sine=frequency=2400:sample_rate=16000,afade=t=out:d=0.45,volume=0.4", 0.45)
    result = sfx_match.match_clip(variant, sfx_index, top=3, threshold=60)
    assert result["matches"][0]["name"] == "ding"
    assert result["confident"] is True


def test_unknown_sound_is_not_confidently_matched(sfx_index: dict, tmp_path: Path):
    # Sustained mid-tone drone — matches nothing in the library well. The gate
    # must say "not confident" rather than silently picking a best-effort name.
    unknown = tmp_path / "drone.wav"
    _render(unknown, "sine=frequency=650:sample_rate=16000", 2.0)
    result = sfx_match.match_clip(unknown, sfx_index, top=3, threshold=85)
    assert result["confident"] is False
    assert "확신 없는" in result["note"]


def test_silent_clip_reports_error_not_match(sfx_index: dict, tmp_path: Path):
    silent = tmp_path / "silence.wav"
    _render(silent, "anullsrc=r=16000:cl=mono", 0.5)
    result = sfx_match.match_clip(silent, sfx_index, top=3, threshold=60)
    assert result["matches"] == []
    assert "silent" in result["error"]


def test_display_names_override_filenames(sfx_library: Path):
    index = sfx_match.build_index(sfx_library, names={"ding.wav": "반짝임 (Sparkle)"})
    by_path = {e["path"]: e["name"] for e in index["entries"]}
    assert by_path["ding.wav"] == "반짝임 (Sparkle)"
    assert by_path["thump.wav"] == "thump"


def test_ambiguous_top_scores_are_not_confident(sfx_library: Path, tmp_path: Path):
    # 라이브러리에 사실상 같은 소리가 둘 있으면 1·2위 점수가 몰린다 —
    # 절대 점수가 높아도 변별력이 없으므로 confident=False여야 한다 (점수 갭 게이트).
    lib = tmp_path / "duplib"
    lib.mkdir()
    for name in ("ding_a", "ding_b"):
        _render(lib / f"{name}.wav", LIBRARY["ding"][0], LIBRARY["ding"][1])
    index = sfx_match.build_index(lib)
    result = sfx_match.match_clip(sfx_library / "ding.wav", index, top=3, threshold=60)
    assert result["matches"][0]["score"] >= 60
    assert result["score_gap"] is not None and result["score_gap"] < sfx_match.DEFAULT_MIN_GAP
    assert result["confident"] is False
    assert "변별력" in result["note"]
