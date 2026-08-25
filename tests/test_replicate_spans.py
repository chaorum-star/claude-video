"""Subtitle span proposal from VTT / JSON transcripts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import spans

SEGMENTS = [
    {"start": 1.0, "end": 2.0, "text": "안녕하세요"},
    {"start": 2.1, "end": 3.0, "text": "오늘은"},        # 0.1s gap → same span
    {"start": 5.0, "end": 6.5, "text": "다음 장면"},      # 2.0s gap → new span
    {"start": 8.0, "end": 8.05, "text": "노이즈"},        # 0.05s → dropped by min_dur
]


def test_propose_spans_merges_small_gaps_and_drops_noise():
    out = spans.propose_spans(SEGMENTS, gap=0.3, min_dur=0.2)
    assert len(out) == 2
    assert out[0] == {"index": 0, "start": 1.0, "end": 3.0, "text": "안녕하세요 오늘은"}
    assert out[1]["text"] == "다음 장면"


def test_burst_events_dedup_within_min_sep():
    proposed = spans.propose_spans(
        [
            {"start": 1.0, "end": 3.0, "text": "a"},
            {"start": 3.1, "end": 5.0, "text": "b"},
        ],
        gap=0.05,  # keep them as two spans
    )
    events = spans.burst_events(proposed, min_sep=0.2)
    assert events == [1.0, 3.0, 5.0]  # 3.0/3.1 collapse into one event


def test_load_segments_json_roundtrip(tmp_path: Path):
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(SEGMENTS, ensure_ascii=False), encoding="utf-8")
    loaded = spans.load_segments(path)
    assert [s["text"] for s in loaded] == ["안녕하세요", "오늘은", "다음 장면", "노이즈"]


def test_load_segments_json_rejects_bad_shape(tmp_path: Path):
    path = tmp_path / "segments.json"
    path.write_text(json.dumps([{"start": 0}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        spans.load_segments(path)


def test_parse_vtt_cues_tags_and_rolling_dedupe(tmp_path: Path):
    vtt = tmp_path / "video.ko.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\n<c>첫 자막</c>\n\n"
        "00:00:02.000 --> 00:00:03.000\n첫 자막\n\n"          # rolling repeat → merged
        "00:00:03.000 --> 00:00:04.500\n첫 자막 이어짐\n\n"    # rolling extension → merged
        "00:00:06.000 --> 00:00:07.000\n두 번째\n",
        encoding="utf-8",
    )
    segments = spans.load_segments(vtt)
    assert segments == [
        {"start": 1.0, "end": 4.5, "text": "첫 자막 이어짐"},
        {"start": 6.0, "end": 7.0, "text": "두 번째"},
    ]


def test_vtt_to_burst_pipeline(tmp_path: Path):
    vtt = tmp_path / "video.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.500\n하나\n\n"
        "00:00:04.000 --> 00:00:05.000\n둘\n",
        encoding="utf-8",
    )
    proposed = spans.propose_spans(spans.load_segments(vtt))
    events = spans.burst_events(proposed)
    assert events == [1.0, 2.5, 4.0, 5.0]
