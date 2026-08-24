from __future__ import annotations

import math
import wave
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .models import (
    Analysis,
    CaptionEvent,
    CaptionStyle,
    Cue,
    MotionEvent,
    OverlayEvent,
    SoundEvent,
    WordTiming,
)
from .utils import ToolError, run
from .visual_effects import estimate_scale_change, phase_shift


CAPTION_BAND_HEIGHT = 0.12


def analyze_editing_grammar(
    video_path: Path,
    work_dir: Path,
    duration: float,
    scenes: list[float],
    speech: list[Cue],
    style: CaptionStyle,
    audio_peaks: list[float],
) -> tuple[list[CaptionEvent], list[OverlayEvent], list[MotionEvent], list[SoundEvent]]:
    motions = detect_motion_events(work_dir, duration, scenes)
    captions = detect_caption_events(video_path, work_dir, duration, speech, style, motions=motions)
    overlays = detect_overlay_events(work_dir, duration)
    attach_overlay_placeholders(overlays, work_dir)
    sounds = detect_sound_events(
        work_dir, duration, speech, audio_peaks,
        captions=captions, overlays=overlays, motions=motions,
    )
    attach_synthetic_sfx(sounds, work_dir)
    return captions, overlays, motions, sounds


def detect_caption_events(
    video_path: Path,
    work_dir: Path,
    duration: float,
    speech: list[Cue],
    style: CaptionStyle,
    fps: float = 20.0,
    motions: list[MotionEvent] | None = None,
) -> list[CaptionEvent]:
    if style.center_y is None:
        return []
    frame_dir = work_dir / "caption_motion"
    paths = sorted(frame_dir.glob("motion-*.jpg"))
    if not paths:
        top = max(0.0, style.center_y - CAPTION_BAND_HEIGHT / 2)
        frame_dir.mkdir(exist_ok=True)
        try:
            run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
                    "-vf", f"crop=iw:ih*{CAPTION_BAND_HEIGHT}:0:ih*{top:.4f},fps={fps:g},scale=540:-2",
                    "-q:v", "2", str(frame_dir / "motion-%05d.jpg"),
                ],
                timeout=1800,
            )
        except (ToolError, OSError):
            return []
        paths = sorted(frame_dir.glob("motion-*.jpg"))
    if len(paths) < 8:
        return []

    masks: list[np.ndarray] = []
    yellow_masks: list[np.ndarray] = []
    for path in paths:
        image = Image.open(path).convert("RGB")
        rgb = np.asarray(image, dtype=np.uint8)
        gray_image = image.convert("L")
        gray = np.asarray(gray_image, dtype=np.float32)
        local = np.asarray(gray_image.filter(ImageFilter.BoxBlur(4)), dtype=np.float32)
        yellow = (
            (rgb[..., 0] > 210) & (rgb[..., 1] > 185) & (rgb[..., 2] < 180)
            & ((rgb[..., 0].astype(np.float32) - rgb[..., 2]) > 55)
            & ((rgb[..., 1].astype(np.float32) - rgb[..., 2]) > 35)
            & (np.abs(rgb[..., 0].astype(np.float32) - rgb[..., 1]) < 70)
        )
        textlike = ((gray > 190) & ((gray - local) > 24)) | yellow
        height, width = textlike.shape
        textlike[:int(height * 0.15)] = False
        textlike[int(height * 0.85):] = False
        textlike[:, :int(width * 0.15)] = False
        textlike[:, int(width * 0.85):] = False
        masks.append(textlike)
        yellow_masks.append(yellow)

    heights: list[float] = []
    widths: list[float] = []
    areas: list[float] = []
    yellow_ratios: list[float] = []
    for index in range(len(paths)):
        active = masks[index]
        row_strength = active.sum(axis=1)
        lower, upper = int(len(row_strength) * 0.15), int(len(row_strength) * 0.85)
        peak = lower + int(np.argmax(row_strength[lower:upper]))
        rows = np.where(
            (row_strength >= max(3, row_strength[peak] * 0.25))
            & (np.abs(np.arange(len(row_strength)) - peak) <= 20)
        )[0]
        if len(rows) == 0:
            heights.append(0.0)
            widths.append(0.0)
            areas.append(0.0)
            yellow_ratios.append(0.0)
            continue
        top, bottom = int(rows.min()), int(rows.max())
        band = active[top:bottom + 1]
        columns = np.where(band.sum(axis=0) >= 2)[0]
        if len(columns) == 0:
            heights.append(0.0)
            widths.append(0.0)
            areas.append(0.0)
            yellow_ratios.append(0.0)
            continue
        left, right = int(columns.min()), int(columns.max())
        region = active[top:bottom + 1, left:right + 1]
        heights.append((bottom - top + 1) / active.shape[0] * CAPTION_BAND_HEIGHT)
        widths.append((right - left + 1) / active.shape[1])
        areas.append(float(region.sum()))
        yellow_ratios.append(
            float(yellow_masks[index][top:bottom + 1, left:right + 1].sum() / max(region.sum(), 1))
        )

    nonzero_areas = [value for value in areas if value > 0]
    if not nonzero_areas:
        return []
    base_height = style.height_ratio or float(np.median([value for value in heights if value > 0]))
    motion_points = [event.start for event in (motions or [])]
    emphasis: list[int] = []
    for index, (height, area, yellow_ratio) in enumerate(zip(heights, areas, yellow_ratios)):
        previous = [value for value in areas[max(0, index - 8):index] if value > 40]
        previous_heights = [value for value in heights[max(0, index - 8):index] if value > 0]
        if not previous or area <= 0:
            continue
        area_ratio = area / max(float(np.median(previous)), 1.0)
        height_ratio = height / max(float(np.median(previous_heights)) if previous_heights else base_height, 1e-5)
        near_motion = any(abs(index / fps - point) <= 0.55 for point in motion_points)
        # 색 강조는 밝은 노랑 픽셀로, 크기 강조는 펀치 줌과 함께 일어난
        # 급격한 자막 면적/높이 변화로만 판정한다. 인물의 흰 옷이나 긴 일반
        # 문장을 강조 자막으로 오인하지 않도록 두 번째 조건은 엄격히 묶는다.
        if yellow_ratio >= 0.18 or (
            near_motion and area_ratio >= 1.75 and (area_ratio >= 2.20 or height_ratio >= 1.12)
        ):
            emphasis.append(index)
    events: list[CaptionEvent] = []
    consumed_until = -1
    for first in emphasis:
        if first <= consumed_until:
            continue
        start = max(0.0, first / fps)
        # 도입 제목 카드는 별도 TitleCard 트랙으로 재현한다. 같은 픽셀을
        # 강조 자막으로도 만들면 CapCut에서 글자가 이중으로 겹친다.
        if start < 2.0 and not any(abs(start - point) <= 0.55 for point in motion_points):
            continue
        cue_candidates = [
            item for item in speech
            if item.end > start + 0.04 and item.start <= start + 0.20
        ]
        cue = min(cue_candidates, key=lambda item: abs(item.start - start)) if cue_candidates else None
        proposed_end = cue.end if cue else start + 0.9
        end = min(duration, max(start + 0.30, proposed_end), start + 1.45)

        # 강조 단어 뒤에 일반 자막이 바로 이어지는 경우, 음성 cue의 끝까지 강조
        # 세그먼트를 늘리면 서로 다른 두 자막을 하나의 효과로 합쳐버린다. 첫 0.2초
        # 뒤의 안정된 글자 폭/면적을 기준으로 다음 문구 교체 지점에서 끊는다.
        settle_first = min(len(paths) - 1, first + max(3, int(0.16 * fps)))
        settle_last = min(len(paths) - 1, first + max(6, int(0.34 * fps)))
        stable_areas = [value for value in areas[settle_first:settle_last + 1] if value > 40]
        stable_widths = [value for value in widths[settle_first:settle_last + 1] if value > 0]
        if stable_areas and stable_widths:
            stable_area = float(np.median(stable_areas))
            stable_width = float(np.median(stable_widths))
            search_first = first + max(7, int(0.38 * fps))
            search_last = min(len(paths), int(end * fps) + 1)
            for index in range(search_first, search_last):
                area_ratio = areas[index] / max(stable_area, 1.0)
                width_ratio = widths[index] / max(stable_width, 1e-5)
                if area_ratio < 0.34 or width_ratio < 0.48 or width_ratio > 1.72:
                    end = max(start + 0.30, index / fps)
                    break
        last = min(len(paths) - 1, max(first + 2, int(end * fps)))
        consumed_until = last
        event_heights = [value for value in heights[first:last + 1] if value > 0]
        measured_scale = float(max(event_heights) / max(base_height, 1e-5)) if event_heights else 1.0
        yellow_share = float(max(yellow_ratios[first:last + 1]))
        grows = areas[min(last, first + 4)] > max(areas[first], 1.0) * 1.25
        near_motion = any(abs(start - point) <= 0.55 for point in motion_points)
        if yellow_share >= 0.10:
            kind, color = "color_then_pop", "노랑→흰색"
            scale = max(1.65, measured_scale)
        elif near_motion:
            kind, color = "keyword_scale", "흰색"
            scale = max(1.45, measured_scale)
        else:
            kind, color = "keyword_scale", "흰색"
            scale = measured_scale
        text = text_for_interval(speech, start, end)
        events.append(
            CaptionEvent(
                round(start, 3), round(end, 3), text, kind, color,
                round(max(1.0, min(2.8, scale)), 2),
                # 큰 글자가 한 프레임에 즉시 교체된 것은 팝 애니메이션이 아니다.
                # 실제로 첫 프레임 뒤 면적이 자라난 경우에만 팝/오버슈트로 만든다.
                "pop" if grows else "instant",
                round(min(0.96, 0.62 + min(0.24, abs(scale - 1.0) * 0.3) + yellow_share * 0.2), 2),
            )
        )
    return merge_caption_events(events)


def text_for_interval(speech: list[Cue], start: float, end: float) -> str:
    words = [
        word.text
        for cue in speech
        for word in cue.words
        if start <= (word.start + word.end) / 2 < end
    ]
    if words:
        return " ".join(words)
    overlapping = [
        cue for cue in speech if cue.start < end and start < cue.end
    ]
    return max(overlapping, key=lambda cue: min(cue.end, end) - max(cue.start, start)).text if overlapping else "강조 문구"


def merge_caption_events(events: list[CaptionEvent]) -> list[CaptionEvent]:
    output: list[CaptionEvent] = []
    for event in events:
        if output and event.start - output[-1].end <= 0.12 and event.kind == output[-1].kind:
            output[-1].end = event.end
            output[-1].text = event.text or output[-1].text
            output[-1].size_scale = max(output[-1].size_scale, event.size_scale)
            output[-1].confidence = max(output[-1].confidence, event.confidence)
        else:
            output.append(event)
    return output[:80]


def detect_overlay_events(work_dir: Path, duration: float, fps: float = 6.0) -> list[OverlayEvent]:
    paths = sorted((work_dir / "effect_frames").glob("frame-*.jpg"))
    if not paths:
        return []
    tracks: list[list[tuple[int, tuple[float, float, float, float]]]] = []
    for index, path in enumerate(paths):
        image = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        luminance = image.mean(axis=2)
        chroma = image.max(axis=2) - image.min(axis=2)
        mask = (luminance > 205) & (chroma < 55)
        candidates = rectangle_candidates(mask)
        used: set[int] = set()
        for box in candidates:
            best = None
            best_iou = 0.0
            for track_index, track in enumerate(tracks):
                if track_index in used or index - track[-1][0] > 2:
                    continue
                score = box_iou(track[-1][1], box)
                if score > best_iou:
                    best, best_iou = track_index, score
            if best is not None and best_iou >= 0.38:
                tracks[best].append((index, box))
                used.add(best)
            else:
                tracks.append([(index, box)])

    output: list[OverlayEvent] = []
    for track in tracks:
        if len(track) < 3:
            continue
        first, last = track[0][0], track[-1][0]
        if (last + 1) / fps <= 2.0:
            continue
        boxes = np.asarray([box for _index, box in track])
        x, y, width, height = [float(value) for value in np.median(boxes, axis=0)]
        output.append(
            OverlayEvent(
                round(first / fps, 3), round(min(duration, (last + 1) / fps), 3),
                "profile_card", round(x + width / 2, 4), round(y + height / 2, 4),
                round(width, 4), round(height, 4), f"교체할 카드 {len(output) + 1}",
                round(min(0.94, 0.62 + len(track) / 60), 2),
            )
        )
    return sorted(output, key=lambda item: (item.start, item.center_y))[:12]


def attach_overlay_placeholders(events: list[OverlayEvent], work_dir: Path) -> None:
    asset_dir = work_dir / "template_assets" / "overlays"
    asset_dir.mkdir(parents=True, exist_ok=True)
    for index, event in enumerate(events, 1):
        ratio = max(2.0, event.width / max(event.height, 0.01))
        width, height = 900, max(120, round(900 / ratio))
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((3, 3, width - 4, height - 4), radius=max(18, height // 7), fill="white")
        label = f"REPLACE CARD {index}"
        box = draw.textbbox((0, 0), label)
        draw.text(
            ((width - (box[2] - box[0])) / 2, (height - (box[3] - box[1])) / 2),
            label, fill=(32, 32, 32, 255),
        )
        path = asset_dir / f"overlay-{index:02d}.png"
        image.save(path)
        event.asset_path = str(path.resolve())


def rectangle_candidates(mask: np.ndarray) -> list[tuple[float, float, float, float]]:
    """흰 카드 안의 사진/글자로 생긴 구멍을 허용하며 가로형 박스를 찾는다."""
    height, width = mask.shape
    row_runs: list[tuple[int, int, int]] = []
    for row in range(height):
        columns = np.where(mask[row])[0].tolist()
        for left, right in group_indices(columns, max_gap=max(3, int(width * 0.045))):
            box_width = right - left + 1
            if box_width >= width * 0.25 and mask[row, left:right + 1].mean() > 0.45:
                row_runs.append((row, left, right))

    tracks: list[list[tuple[int, int, int]]] = []
    for item in row_runs:
        row, left, right = item
        best_index: int | None = None
        best_distance = float("inf")
        for index, track in enumerate(tracks):
            previous_row, previous_left, previous_right = track[-1]
            if row - previous_row > 2:
                continue
            overlap = min(right, previous_right) - max(left, previous_left)
            if overlap <= min(right - left, previous_right - previous_left) * 0.55:
                continue
            distance = abs(left - previous_left) + abs(right - previous_right)
            if distance < best_distance:
                best_index, best_distance = index, distance
        if best_index is None:
            tracks.append([item])
        else:
            tracks[best_index].append(item)

    candidates: list[tuple[float, float, float, float]] = []
    for track in tracks:
        rows = [item[0] for item in track]
        left = int(np.median([item[1] for item in track]))
        right = int(np.median([item[2] for item in track]))
        top, bottom = min(rows), max(rows)
        box_width, box_height = right - left + 1, bottom - top + 1
        if box_height < height * 0.035 or box_width / max(box_height, 1) < 2.0:
            continue
        candidates.append((left / width, top / height, box_width / width, box_height / height))
    return candidates


def box_iou(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0


def detect_motion_events(
    work_dir: Path,
    duration: float,
    scenes: list[float],
    fps: float = 6.0,
) -> list[MotionEvent]:
    paths = sorted((work_dir / "effect_frames").glob("frame-*.jpg"))
    frames = [np.asarray(Image.open(path).convert("L"), dtype=np.float32) for path in paths]
    candidates: list[MotionEvent] = []
    for index in range(2, len(frames)):
        point = index / fps
        if any(abs(point - scene) <= 0.38 for scene in scenes):
            continue
        scale, improvement = estimate_scale_change(frames[index - 2], frames[index])
        dx, dy, correlation = phase_shift(frames[index - 2], frames[index])
        if improvement >= 0.16 and scale >= 1.10 and correlation >= 7.0:
            candidates.append(
                MotionEvent(
                    round(max(0.0, point - 2 / fps), 3), round(min(duration, point + 1 / fps), 3),
                    "punch_in", 1.0, round(min(1.28, scale), 2),
                    round(-dx / frames[index].shape[1], 4), round(dy / frames[index].shape[0], 4),
                    round(min(0.97, 0.58 + improvement * 0.65), 2),
                )
            )
    return merge_motion_events(candidates)


def merge_motion_events(events: list[MotionEvent]) -> list[MotionEvent]:
    output: list[MotionEvent] = []
    for event in events:
        if output and event.start - output[-1].end <= 0.4 and event.kind == output[-1].kind:
            output[-1].end = max(output[-1].end, event.end)
            output[-1].scale_to = max(output[-1].scale_to, event.scale_to)
            output[-1].confidence = max(output[-1].confidence, event.confidence)
        else:
            output.append(event)
    return output[:40]


def detect_sound_events(
    work_dir: Path,
    duration: float,
    speech: list[Cue],
    peaks: list[float],
    captions: list[CaptionEvent] | None = None,
    overlays: list[OverlayEvent] | None = None,
    motions: list[MotionEvent] | None = None,
) -> list[SoundEvent]:
    audio = work_dir / "speech.wav"
    if not audio.is_file():
        return []
    try:
        with wave.open(str(audio), "rb") as stream:
            rate = stream.getframerate()
            channels = stream.getnchannels()
            samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32)
    except (wave.Error, OSError):
        return []
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    global_rms = float(np.sqrt(np.mean(samples * samples)) + 1e-6)
    output: list[SoundEvent] = []
    for point in peaks:
        first = max(0, int((point - 0.08) * rate))
        last = min(len(samples), int((point + 0.35) * rate))
        chunk = samples[first:last]
        if len(chunk) < 256:
            continue
        window = np.hanning(len(chunk))
        spectrum = np.abs(np.fft.rfft(chunk * window))
        frequencies = np.fft.rfftfreq(len(chunk), 1 / rate)
        centroid = float((spectrum * frequencies).sum() / max(spectrum.sum(), 1e-6))
        low_ratio = float(spectrum[frequencies < 260].sum() / max(spectrum.sum(), 1e-6))
        energy = float(np.sqrt(np.mean(chunk * chunk)) / global_rms)
        speech_overlap = any(cue.start < point + 0.2 and point - 0.1 < cue.end for cue in speech)
        if low_ratio >= 0.34 and energy >= 1.35:
            kind, length = "impact", 0.36
        elif centroid >= 3200:
            kind, length = "click", 0.10
        elif centroid >= 1500 and energy >= 1.2:
            kind, length = "whoosh", 0.45
        else:
            kind, length = "pop", 0.20
        confidence = 0.58 + min(0.22, max(0.0, energy - 1.0) * 0.12) - (0.08 if speech_overlap else 0.0)
        output.append(
            SoundEvent(
                round(point, 3), round(min(duration, point + length), 3), kind,
                round(max(0.5, min(0.9, confidence)), 2), round(energy, 2), round(centroid, 0),
            )
        )
    inferred: list[SoundEvent] = []
    for event in overlays or []:
        inferred.append(SoundEvent(event.start, min(duration, event.start + 0.20), "pop", 0.72, 0.0, 0.0, basis="visual_sync"))
    for event in captions or []:
        kind = "sparkle" if event.kind == "color_then_pop" else "pop"
        inferred.append(SoundEvent(event.start, min(duration, event.start + 0.30), kind, 0.76, 0.0, 0.0, basis="visual_sync"))
    for event in motions or []:
        inferred.append(SoundEvent(event.start, min(duration, event.start + 0.36), "impact", 0.70, 0.0, 0.0, basis="visual_sync"))
    for event in inferred:
        if any(abs(existing.start - event.start) <= 0.12 for existing in output):
            continue
        output.append(event)
    return sorted(output, key=lambda event: event.start)


def attach_synthetic_sfx(events: list[SoundEvent], work_dir: Path) -> None:
    if not events:
        return
    asset_dir = work_dir / "template_assets" / "sfx"
    asset_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for kind in {event.kind for event in events}:
        path = asset_dir / f"{kind}.wav"
        write_synthetic_sfx(path, kind)
        paths[kind] = path
    for event in events:
        event.asset_path = str(paths[event.kind].resolve())


def write_synthetic_sfx(path: Path, kind: str, rate: int = 44100) -> None:
    durations = {"click": 0.10, "pop": 0.20, "impact": 0.36, "whoosh": 0.45, "sparkle": 0.30}
    duration = durations.get(kind, 0.20)
    count = max(1, int(duration * rate))
    time = np.arange(count, dtype=np.float64) / rate
    seeds = {"click": 101, "pop": 211, "impact": 307, "whoosh": 401, "sparkle": 503}
    rng = np.random.default_rng(seeds.get(kind, 997))
    noise = rng.normal(0.0, 1.0, count)
    if kind == "impact":
        signal = np.sin(2 * math.pi * (92 - 45 * time / duration) * time) * np.exp(-time * 10) + noise * np.exp(-time * 22) * 0.22
    elif kind == "whoosh":
        envelope = np.sin(np.pi * np.clip(time / duration, 0, 1)) ** 1.8
        signal = noise * envelope * 0.42 + np.sin(2 * math.pi * 850 * time) * envelope * 0.08
    elif kind == "sparkle":
        signal = (
            np.sin(2 * math.pi * 1320 * time) * np.exp(-time * 15)
            + np.sin(2 * math.pi * 1980 * time) * np.exp(-time * 21) * 0.55
        )
    elif kind == "click":
        signal = noise * np.exp(-time * 70) * 0.7
    else:
        signal = np.sin(2 * math.pi * (260 - 150 * time / duration) * time) * np.exp(-time * 18) * 0.65
    peak = float(np.max(np.abs(signal)) or 1.0)
    pcm = np.clip(signal / peak * 15000, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.tobytes())


def apply_replacement_text(analysis: Analysis, replacement_text: str) -> None:
    words = replacement_text.split()
    if not words or not analysis.speech:
        return
    weights = [max(1, len(cue.text.replace(" ", ""))) for cue in analysis.speech]
    total = sum(weights)
    cursor = 0
    for index, cue in enumerate(analysis.speech):
        remaining_cues = len(analysis.speech) - index
        remaining_words = len(words) - cursor
        if index == len(analysis.speech) - 1:
            take = remaining_words
        else:
            take = max(1, round(len(words) * weights[index] / total))
            take = min(take, max(1, remaining_words - (remaining_cues - 1)))
        cue.text = " ".join(words[cursor:cursor + take])
        cue_words = words[cursor:cursor + take]
        step = (cue.end - cue.start) / max(1, len(cue_words))
        cue.words = [
            WordTiming(cue.start + word_index * step, cue.start + (word_index + 1) * step, word)
            for word_index, word in enumerate(cue_words)
        ]
        cursor += take
        if cursor >= len(words):
            for rest in analysis.speech[index + 1:]:
                rest.text = ""
                rest.words = []
            break
    for event in analysis.caption_events:
        overlapping = [cue for cue in analysis.speech if cue.text and cue.start < event.end and event.start < cue.end]
        if overlapping:
            chosen = max(overlapping, key=lambda cue: min(cue.end, event.end) - max(cue.start, event.start))
            chosen_words = chosen.text.split()
            event.text = " ".join(chosen_words[-min(3, len(chosen_words)):])


def group_indices(indices: list[int], max_gap: int) -> list[tuple[int, int]]:
    if not indices:
        return []
    groups: list[tuple[int, int]] = []
    first = previous = indices[0]
    for index in indices[1:]:
        if index - previous > max_gap + 1:
            groups.append((first, previous))
            first = index
        previous = index
    groups.append((first, previous))
    return groups
