"""SFX onset detection, scene correlation, and clip extraction (SPEC F4)."""
from __future__ import annotations

import wave
from pathlib import Path

import audio_events
import pytest

from conftest import SFX_BURSTS

HOP_S = audio_events.HOP / audio_events.RATE
TOL = 0.08  # one hop (16 ms) + window smear; bursts start exactly on the second


# --- pure-function tests (no ffmpeg) ---------------------------------------

def _envelope(events: dict[int, float], length: int = 200, base: float = 5.0) -> list[float]:
    """Synthetic HF envelope: constant base with spikes at given frame indices."""
    hf = [base] * length
    for frame, value in events.items():
        hf[frame] = value
    return hf


def test_detect_onsets_finds_spikes_over_base():
    hf = _envelope({50: 400.0, 51: 380.0, 120: 500.0})
    out = audio_events.detect_onsets(hf, hop_s=HOP_S)
    assert [e["frame"] for e in out] == [50, 120]
    assert out[0]["time"] == pytest.approx(50 * HOP_S, abs=1e-6)
    assert out[0]["score"] > 3.0


def test_detect_onsets_groups_sustained_burst_into_one_event():
    hf = _envelope({i: 400.0 for i in range(50, 58)})  # 8 consecutive loud frames
    out = audio_events.detect_onsets(hf, hop_s=HOP_S)
    assert len(out) == 1
    assert out[0]["frame"] == 50  # onset = the rise, not the peak


def test_detect_onsets_absolute_floor_blocks_quiet_novelty():
    # Huge *relative* jump but far below the dBFS floor → not an event.
    hf = _envelope({50: 2.0}, base=0.01)
    assert audio_events.detect_onsets(hf, hop_s=HOP_S) == []


def test_detect_onsets_ignores_steady_loud_bed():
    hf = [300.0] * 200  # loud but constant (music bed) → no novelty
    assert audio_events.detect_onsets(hf, hop_s=HOP_S) == []


def test_correlate_scenes_labels_transition_vs_accent():
    events = [{"time": 1.0}, {"time": 3.0}]
    audio_events.correlate_scenes(events, scenes=[1.2, 8.0], window=0.3)
    assert events[0]["type"] == "transition"
    assert events[0]["near_scene"] == 1.2
    assert events[1]["type"] == "accent"
    assert events[1]["near_scene"] is None


def test_estimate_end_decays_to_baseline_and_caps():
    hf = _envelope({i: 400.0 for i in range(50, 55)})
    end = audio_events.estimate_end(hf, 50, hop_s=HOP_S)
    assert 55 * HOP_S <= end <= 58 * HOP_S
    # Never-decaying event is capped at MAX_EVENT_DUR past the onset.
    hf2 = [5.0] * 50 + [400.0] * 200
    end2 = audio_events.estimate_end(hf2, 50, hop_s=HOP_S)
    assert end2 <= (50 * HOP_S) + audio_events.MAX_EVENT_DUR + HOP_S


# --- end-to-end on the synthesized clip ------------------------------------

def test_analyze_finds_bursts_at_known_times(sfx_clip: Path, tmp_path: Path):
    report = audio_events.analyze(str(sfx_clip), tmp_path / "sfx", clips=False)
    times = [e["time"] for e in report["events"]]
    assert len(times) == len(SFX_BURSTS)
    for expected, got in zip(SFX_BURSTS, times):
        assert got == pytest.approx(expected, abs=TOL)
    assert all(e["type"] == "accent" for e in report["events"])  # no scenes given
    assert "개인 학습" in report["copyright_notice"]


def test_analyze_scene_correlation_end_to_end(sfx_clip: Path, tmp_path: Path):
    report = audio_events.analyze(
        str(sfx_clip), tmp_path / "sfx", scenes=[1.1, 5.5], clips=False
    )
    by_time = {round(e["time"], 1): e for e in report["events"]}
    assert by_time[1.0]["type"] == "transition"
    assert by_time[2.5]["type"] == "accent"


def test_analyze_extracts_playable_clips(sfx_clip: Path, tmp_path: Path):
    report = audio_events.analyze(str(sfx_clip), tmp_path / "sfx")
    assert report["event_count"] == len(SFX_BURSTS)
    for event in report["events"]:
        assert event["clip"] is not None
        with wave.open(event["clip"]) as w:
            clip_dur = w.getnframes() / w.getframerate()
        assert clip_dur >= audio_events.MIN_CLIP_LEN
        assert clip_dur <= audio_events.MAX_EVENT_DUR + 0.2
    assert len(list((tmp_path / "sfx").glob("sfx_*.wav"))) == len(SFX_BURSTS)


def test_analyze_max_events_keeps_top_scores_chronologically(sfx_clip: Path, tmp_path: Path):
    report = audio_events.analyze(str(sfx_clip), tmp_path / "sfx", max_events=2, clips=False)
    assert report["event_count"] == 2
    assert report["dropped_low_score"] == 1
    times = [e["time"] for e in report["events"]]
    assert times == sorted(times)


def test_analyze_rejects_video_without_audio(cut_clip: Path, tmp_path: Path):
    with pytest.raises(SystemExit, match="no audio"):
        audio_events.analyze(str(cut_clip), tmp_path / "sfx")


def test_parse_times_formats():
    assert audio_events.parse_times("3,0:05, 1:00:01") == [3.0, 5.0, 3601.0]
    assert audio_events.parse_times(None) == []
