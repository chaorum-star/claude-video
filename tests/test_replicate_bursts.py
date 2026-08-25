"""High-fps burst extraction: window planning, budget guard, real extraction."""
from __future__ import annotations

from pathlib import Path

import bursts
import pytest


def test_parse_events_formats_and_dedup():
    assert bursts.parse_events("3, 0:05,1:00:01,3") == [3.0, 5.0, 3601.0]


def test_parse_events_rejects_empty():
    with pytest.raises(SystemExit):
        bursts.parse_events(" , ")


def test_plan_windows_clamps_to_video_bounds():
    wins = bursts.plan_windows([0.1, 4.9], duration=5.0, window=0.5)
    assert wins[0]["start"] == 0.0
    assert wins[0]["end"] == pytest.approx(0.6)
    assert wins[-1]["end"] == 5.0


def test_plan_windows_merges_overlaps_and_drops_out_of_range():
    wins = bursts.plan_windows([1.0, 1.3, 3.0, 99.0], duration=5.0, window=0.5)
    assert len(wins) == 2  # 1.0 and 1.3 share a window; 99.0 is gone
    assert wins[0]["event_indices"] == [0, 1]
    assert wins[0]["end"] == pytest.approx(1.8)


def test_extract_bursts_on_cut_clip(cut_clip: Path, tmp_path: Path):
    report = bursts.extract_bursts(
        str(cut_clip), tmp_path / "b", events=[1.0, 3.0], window=0.5, fps=10.0
    )
    assert len(report["windows"]) == 2
    assert report["skipped_out_of_range"] == []
    assert report["dropped_over_budget"] == []
    # ±0.5s at 10fps ≈ 10 frames per window — well past the 2fps /watch cap.
    assert 8 <= report["frame_count"] <= 24
    for frame in report["frames"]:
        assert Path(frame["path"]).exists()
        win = report["windows"][frame["window"]]
        assert win["start"] <= frame["timestamp_seconds"] <= win["end"] + 0.11
    assert report["frame_count"] == len(list((tmp_path / "b").glob("burst_*.jpg")))


def test_extract_bursts_reports_out_of_range_events(cut_clip: Path, tmp_path: Path):
    report = bursts.extract_bursts(
        str(cut_clip), tmp_path / "b", events=[1.0, 500.0], window=0.5, fps=10.0
    )
    assert report["skipped_out_of_range"] == [500.0]
    assert len(report["windows"]) == 1


def test_extract_bursts_budget_drops_tail_windows_loudly(cut_clip: Path, tmp_path: Path):
    report = bursts.extract_bursts(
        str(cut_clip), tmp_path / "b",
        events=[1.0, 3.0, 4.5], window=0.5, fps=10.0, max_total=15,
    )
    assert report["dropped_over_budget"]  # trimmed, and says which ranges
    assert report["frame_count"] <= 15
    kept_events = {t for w in report["windows"] for t in w["events"]}
    dropped_events = {t for w in report["dropped_over_budget"] for t in w["events"]}
    assert kept_events.isdisjoint(dropped_events)
    assert kept_events | dropped_events == {1.0, 3.0, 4.5}


def test_extract_bursts_rejects_bad_params(cut_clip: Path, tmp_path: Path):
    with pytest.raises(SystemExit):
        bursts.extract_bursts(str(cut_clip), tmp_path / "b", events=[1.0], fps=0)
    with pytest.raises(SystemExit):
        bursts.extract_bursts(str(cut_clip), tmp_path / "b", events=[1.0], window=-1)
