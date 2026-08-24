from __future__ import annotations

import csv
import io
import math
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from .models import CaptionStyle, Cue, TitleCard, VisualEffect
from .utils import ToolError, run


SUGGESTIONS = {
    "flash": "전환 > White Flash(화이트 플래시) 또는 영상 효과 > Blinking",
    "blur": "영상 효과 > 동감 모호(동적 블러) / 전환 > 밝은 점 모호",
    "shake": "영상 효과 > Shaky Dolly 또는 전환 > 흔들림",
    "zoom_in": "영상 효과 > 줌 푸시 미러 / 전환 > 밀어 넣기",
    "zoom_out": "전환 > 멀리 당기기",
    "cut": "전환 없음(하드컷), 필요 시 카메라·고장 계열 비교",
}


def analyze_visual_language(
    video_path: Path,
    work_dir: Path,
    duration: float,
    scenes: list[float],
    speech: list[Cue],
) -> tuple[list[VisualEffect], CaptionStyle, TitleCard | None]:
    frame_dir = work_dir / "effect_frames"
    frame_dir.mkdir(exist_ok=True)
    fps = min(6.0, max(2.0, 600.0 / max(duration, 1.0)))
    run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
            "-vf", f"fps={fps:.4f},scale=256:-2", "-q:v", "4",
            str(frame_dir / "frame-%06d.jpg"),
        ],
        timeout=1800,
    )
    paths = sorted(frame_dir.glob("frame-*.jpg"))
    if len(paths) < 2:
        return [], CaptionStyle(evidence=["분석할 프레임이 충분하지 않습니다."]), None
    frames = [_load_gray(path) for path in paths]
    effects = classify_effects(frames, fps, duration, scenes)
    style = measure_caption_style(video_path, work_dir, duration)
    title_card = measure_title_card(video_path, work_dir)
    if style.center_y is None:
        style = analyze_caption_style(extract_caption_frames(video_path, work_dir, speech), duration)
    return effects, style, title_card


def extract_caption_frames(
    video_path: Path,
    work_dir: Path,
    speech: list[Cue],
    limit: int = 40,
) -> list[tuple[Path, float]]:
    """말 자막이 떠 있을 시점을 원본 해상도로 뽑는다.

    효과 분석용 256px 썸네일로는 작은 자막의 색·크기를 잴 수 없다.
    자막이 뜬 직후와 안정된 뒤를 각각 잡아 등장 움직임도 볼 수 있게 한다.
    """
    frame_dir = work_dir / "caption_frames"
    frame_dir.mkdir(exist_ok=True)
    moments: list[float] = []
    for cue in speech:
        for offset in (0.12, 0.4):
            moment = cue.start + offset
            if moment < cue.end:
                moments.append(round(moment, 2))
    moments = sorted(set(moments))[:limit]
    collected: list[tuple[Path, float]] = []
    for index, moment in enumerate(moments):
        target = frame_dir / f"caption-{index:03d}.jpg"
        try:
            run(
                [
                    "ffmpeg", "-y", "-v", "error", "-ss", f"{moment:.2f}",
                    "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(target),
                ],
                timeout=60,
            )
        except (ToolError, OSError):
            continue
        if target.is_file():
            collected.append((target, moment))
    return collected


def _load_gray(path: Path) -> np.ndarray:
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def frame_metrics(frames: list[np.ndarray]) -> dict[str, list[float]]:
    brightness = [float(frame.mean() / 255.0) for frame in frames]
    sharpness = [
        float((np.abs(np.diff(frame, axis=0)).mean() + np.abs(np.diff(frame, axis=1)).mean()) / 255.0)
        for frame in frames
    ]
    shifts: list[float] = [0.0]
    correlations: list[float] = [0.0]
    differences: list[float] = [0.0]
    for previous, current in zip(frames, frames[1:]):
        dx, dy, correlation = phase_shift(previous, current)
        aligned = np.roll(current, (int(round(dy)), int(round(dx))), axis=(0, 1))
        shifts.append(float(math.hypot(dx, dy)))
        correlations.append(correlation)
        differences.append(float(np.mean(np.abs(previous - aligned)) / 255.0))
    return {
        "brightness": brightness,
        "sharpness": sharpness,
        "shift": shifts,
        "correlation": correlations,
        "difference": differences,
    }


def phase_shift(previous: np.ndarray, current: np.ndarray) -> tuple[float, float, float]:
    left = previous - previous.mean()
    right = current - current.mean()
    cross = np.fft.fft2(left) * np.conj(np.fft.fft2(right))
    magnitude = np.abs(cross)
    normalized = cross / np.maximum(magnitude, 1e-8)
    correlation = np.abs(np.fft.ifft2(normalized))
    y, x = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    if x > correlation.shape[1] // 2:
        x -= correlation.shape[1]
    if y > correlation.shape[0] // 2:
        y -= correlation.shape[0]
    confidence = float(correlation.max() / max(correlation.mean(), 1e-8))
    return float(x), float(y), confidence


def classify_effects(
    frames: list[np.ndarray],
    fps: float,
    duration: float,
    scenes: list[float],
) -> list[VisualEffect]:
    metrics = frame_metrics(frames)
    brightness = np.asarray(metrics["brightness"])
    sharpness = np.asarray(metrics["sharpness"])
    shifts = np.asarray(metrics["shift"])
    correlations = np.asarray(metrics["correlation"])
    differences = np.asarray(metrics["difference"])
    effects: list[VisualEffect] = []

    bright_median = float(np.median(brightness))
    bright_mad = float(np.median(np.abs(brightness - bright_median)))
    flash_gate = bright_median + max(0.13, bright_mad * 4.0)
    flash_indices: list[int] = []
    for index, value in enumerate(brightness):
        neighbors = np.concatenate((brightness[max(0, index - 3):index], brightness[index + 1:index + 4]))
        local = float(np.median(neighbors)) if len(neighbors) else bright_median
        if value >= flash_gate and value >= 0.62 and value - local >= 0.11:
            flash_indices.append(index)
    for first, last in group_indices(flash_indices, max_gap=1):
        peak = float(brightness[first:last + 1].max())
        effects.append(
            VisualEffect(
                max(0.0, (first - 1) / fps), min(duration, (last + 2) / fps),
                "flash", "화이트 플래시/노출 번쩍임 후보",
                round(min(0.98, 0.58 + max(0.0, peak - flash_gate) * 1.8), 2),
                f"주변 중앙 밝기 {bright_median:.2f} 대비 최고 {peak:.2f}", SUGGESTIONS["flash"],
            )
        )

    sharp_median = float(np.median(sharpness))
    blur_indices = [
        index for index, value in enumerate(sharpness)
        if sharp_median > 0.012 and value < sharp_median * 0.52 and differences[index] > 0.035
    ]
    for first, last in group_indices(blur_indices, max_gap=1):
        if last - first + 1 < 2:
            continue
        low = float(sharpness[first:last + 1].min())
        effects.append(
            VisualEffect(
                first / fps, min(duration, (last + 1) / fps), "blur", "블러/포커스 전환 후보", 0.66,
                f"선명도 기준 {sharp_median:.3f}에서 {low:.3f}로 하락", SUGGESTIONS["blur"],
            )
        )

    shake_indices = [
        index for index in range(1, len(frames))
        if shifts[index] >= 3.2 and correlations[index] >= 5.0 and differences[index] < 0.38
    ]
    for first, last in group_indices(shake_indices, max_gap=2):
        if last - first + 1 < 2:
            continue
        peak = float(shifts[first:last + 1].max())
        effects.append(
            VisualEffect(
                max(0.0, (first - 1) / fps), min(duration, (last + 1) / fps), "shake",
                "카메라 흔들림/휘핑 후보", round(min(0.9, 0.55 + peak / 40.0), 2),
                f"프레임 전역 이동량 최고 {peak:.1f}px", SUGGESTIONS["shake"],
            )
        )

    for point in scenes:
        index = max(1, min(len(frames) - 1, int(round(point * fps))))
        if any(effect.kind == "flash" and effect.start <= point <= effect.end for effect in effects):
            continue
        scale, improvement = estimate_scale_change(frames[index - 1], frames[index])
        if improvement >= 0.10 and abs(scale - 1.0) >= 0.08 and differences[index] < 0.32:
            kind = "zoom_in" if scale > 1.0 else "zoom_out"
            label = "펀치 인/점프 줌 후보" if kind == "zoom_in" else "줌 아웃 후보"
            effects.append(
                VisualEffect(
                    max(0.0, point - 0.18), min(duration, point + 0.22), kind, label,
                    round(min(0.9, 0.58 + improvement), 2),
                    f"배율 {scale:.2f} 정합 시 오차 {improvement * 100:.0f}% 개선", SUGGESTIONS[kind],
                )
            )
        else:
            effects.append(
                VisualEffect(
                    max(0.0, point - 0.08), min(duration, point + 0.08), "cut", "하드컷/전환 후보",
                    round(min(0.95, 0.55 + differences[index]), 2),
                    f"프레임 변화량 {differences[index]:.2f}", SUGGESTIONS["cut"],
                )
            )
    return deduplicate_effects(effects)


def estimate_scale_change(previous: np.ndarray, current: np.ndarray) -> tuple[float, float]:
    base = float(np.mean(np.abs(previous - current)))
    if base < 1e-6:
        return 1.0, 0.0
    scores: list[tuple[float, float]] = []
    image = Image.fromarray(np.clip(previous, 0, 255).astype(np.uint8))
    for scale in (0.82, 0.90, 1.0, 1.10, 1.22):
        candidate = center_scale(image, scale, previous.shape[1], previous.shape[0])
        scores.append((scale, float(np.mean(np.abs(candidate - current)))))
    scale, error = min(scores, key=lambda item: item[1])
    return scale, max(0.0, (base - error) / base)


def center_scale(image: Image.Image, scale: float, width: int, height: int) -> np.ndarray:
    if scale >= 1.0:
        crop_w, crop_h = max(2, round(width / scale)), max(2, round(height / scale))
        left, top = (width - crop_w) // 2, (height - crop_h) // 2
        output = image.crop((left, top, left + crop_w, top + crop_h)).resize((width, height), Image.Resampling.BILINEAR)
    else:
        small = image.resize((max(2, round(width * scale)), max(2, round(height * scale))), Image.Resampling.BILINEAR)
        output = Image.new("L", (width, height), int(np.asarray(image).mean()))
        output.paste(small, ((width - small.width) // 2, (height - small.height) // 2))
    return np.asarray(output, dtype=np.float32)


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


def deduplicate_effects(effects: list[VisualEffect]) -> list[VisualEffect]:
    output: list[VisualEffect] = []
    for effect in sorted(effects, key=lambda item: (item.start, -item.confidence)):
        duplicate = next(
            (item for item in output if item.kind == effect.kind and effect.start <= item.end + 0.2),
            None,
        )
        if duplicate:
            duplicate.end = max(duplicate.end, effect.end)
            if effect.confidence > duplicate.confidence:
                duplicate.confidence = effect.confidence
                duplicate.evidence = effect.evidence
        else:
            output.append(effect)
    return output[:60]



def measure_caption_style(video_path: Path, work_dir: Path, duration: float) -> CaptionStyle:
    """OCR에 기대지 않고 자막 띠를 직접 찾아 위치·크기·색·등장 움직임을 잰다.

    글자는 "가끔 켜지고 자주 바뀌는 밝은 픽셀"이다. 흰 옷처럼 계속 켜져 있는 것,
    책장처럼 늘 꺼져 있는 것과 이 성질로 구분한다. OCR이 한글을 못 읽어도 통한다.
    """
    frame_dir = work_dir / "caption_frames"
    frame_dir.mkdir(exist_ok=True)
    run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
            "-vf", "fps=4,scale=540:-2", "-q:v", "2", str(frame_dir / "band-%05d.jpg"),
        ],
        timeout=1800,
    )
    paths = sorted(frame_dir.glob("band-*.jpg"))
    if len(paths) < 8:
        return CaptionStyle(evidence=["자막 띠를 잴 프레임이 부족합니다."])
    rgb = np.stack([np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) for path in paths])
    luminance = rgb.mean(axis=3)
    bright = (luminance > 215).astype(np.float32)
    on_ratio = bright.mean(axis=0)
    toggle = np.abs(np.diff(bright, axis=0)).mean(axis=0)
    mask = (on_ratio > 0.03) & (on_ratio < 0.6) & (toggle > 0.10)
    height, width = mask.shape
    row_strength = mask.mean(axis=1)
    if float(row_strength.max()) <= 0.004:
        return CaptionStyle(evidence=["화면에서 반복해 바뀌는 글자 띠를 찾지 못했습니다."])
    rows = np.where(row_strength > row_strength.max() * 0.25)[0]
    top, bottom = int(rows.min()), int(rows.max())
    column_strength = mask[top:bottom + 1].mean(axis=0)
    columns = np.where(column_strength > max(column_strength.max() * 0.25, 1e-6))[0]
    left, right = int(columns.min()), int(columns.max())

    center_y = (top + bottom) / 2 / height
    height_ratio = (bottom - top + 1) / height
    band_rgb = rgb[:, top:bottom + 1, left:right + 1]
    band_lum = luminance[:, top:bottom + 1, left:right + 1]
    strokes = band_rgb[band_lum > 215]
    if len(strokes) < 200:
        return CaptionStyle(evidence=["자막 띠에서 글자 획을 충분히 모으지 못했습니다."])
    mean_rgb = strokes.mean(axis=0)
    color = classify_stroke_color(mean_rgb)
    dark_ratio = float((band_lum < 70).mean())
    plain = band_lum[band_lum <= 215]
    background_spread = float(plain.std()) if plain.size else 99.0
    animation = measure_caption_animation(video_path, work_dir, center_y)
    center_x = (left + right) / 2 / width
    return CaptionStyle(
        position="상단" if center_y < 0.36 else "중앙" if center_y < 0.66 else "하단",
        size=f"{'작게' if height_ratio < 0.035 else '보통' if height_ratio < 0.065 else '크게'}"
             f" (화면 높이의 약 {height_ratio * 100:.1f}%)",
        color=color,
        outline="검정 외곽선/그림자 추정" if dark_ratio > 0.12 else "뚜렷한 외곽선 미검출",
        background="반투명·단색 박스 추정" if background_spread < 18 else "배경 박스 미검출",
        animation=animation,
        confidence=0.85,
        center_y=round(center_y, 4),
        height_ratio=round(height_ratio, 4),
        evidence=[
            f"자막 띠 세로 {top / height * 100:.1f}%~{bottom / height * 100:.1f}%"
            f" (중심 {center_y * 100:.1f}%, 가로 중심 {center_x * 100:.0f}%)",
            f"글자 획 평균 RGB {mean_rgb.round(0).astype(int).tolist()}",
            "완성 영상만으로 정확한 폰트명·CapCut 프리셋명은 확정할 수 없습니다.",
        ],
    )


def classify_stroke_color(mean_rgb: np.ndarray) -> str:
    red, green, blue = (float(value) for value in mean_rgb)
    if max(red, green, blue) - min(red, green, blue) < 26:
        return "흰색 계열" if (red + green + blue) / 3 >= 128 else "검정 계열"
    if red > blue * 1.25 and green > blue * 1.25:
        return "노랑 계열"
    if red > green * 1.22 and red > blue * 1.22:
        return "빨강 계열"
    if blue > red * 1.2:
        return "파랑 계열"
    return "컬러 강조"


def measure_caption_animation(video_path: Path, work_dir: Path, center_y: float) -> str:
    """자막이 바뀌는 순간을 20fps로 들여다보고 등장 움직임을 판정한다."""
    band_dir = work_dir / "caption_motion"
    band_dir.mkdir(exist_ok=True)
    top = max(0.0, center_y - 0.06)
    try:
        run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(video_path),
                "-vf", f"crop=iw:ih*0.12:0:ih*{top:.4f},fps=20,scale=540:-2",
                "-q:v", "2", str(band_dir / "motion-%05d.jpg"),
            ],
            timeout=1800,
        )
    except (ToolError, OSError):
        return "즉시 등장 또는 판별 보류"
    paths = sorted(band_dir.glob("motion-*.jpg"))
    if len(paths) < 20:
        return "즉시 등장 또는 판별 보류"
    frames = np.stack([np.asarray(Image.open(path).convert("L"), dtype=np.float32) for path in paths])
    glyph = frames > 215
    area = glyph.reshape(len(glyph), -1).sum(axis=1).astype(np.float32)
    events: list[int] = []
    for index in range(1, len(glyph) - 4):
        if area[index] < 40:
            continue
        changed = float(np.logical_xor(glyph[index], glyph[index - 1]).sum())
        if changed > max(area[index], area[index - 1]) * 0.55:
            if not events or index - events[-1] >= 4:
                events.append(index)
    if len(events) < 6:
        return "즉시 등장 또는 판별 보류"
    votes: Counter[str] = Counter()
    for index in events:
        window = area[index:index + 4]
        if window[0] <= 0:
            continue
        centers = []
        for step in range(4):
            hit = np.where(glyph[index + step].sum(axis=1) > 0)[0]
            centers.append(float(hit.mean()) if len(hit) else np.nan)
        drift = 0.0 if np.all(np.isnan(centers)) else float(np.nanmax(centers) - np.nanmin(centers))
        first = float(frames[index][glyph[index]].mean()) if glyph[index].any() else 0.0
        later = float(frames[index + 2][glyph[index + 2]].mean()) if glyph[index + 2].any() else first
        if float(np.max(window)) > window[0] * 1.35:
            votes["팝업/확대 등장 추정"] += 1
        elif drift > frames.shape[1] * 0.10:
            votes["위·아래 슬라이드 등장 추정"] += 1
        elif later - first > 12:
            votes["페이드 등장 추정"] += 1
        else:
            votes["즉시 등장"] += 1
    if not votes:
        return "즉시 등장 또는 판별 보류"
    winner, count = votes.most_common(1)[0]
    # 과반이 아니면 단정하지 않는다.
    return winner if count / sum(votes.values()) >= 0.5 else "즉시 등장 또는 판별 보류"


def analyze_caption_style(frames: list[tuple[Path, float]], duration: float) -> CaptionStyle:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return CaptionStyle(evidence=["Tesseract가 없어 자막 스타일을 측정하지 못했습니다."])
    if not frames:
        return CaptionStyle(evidence=["자막 스타일을 잴 프레임을 만들지 못했습니다."])
    observations: list[dict[str, object]] = []
    for path, moment in frames:
        observations.extend(read_text_observations(path, tesseract, moment))
    observations = [item for item in observations if 0.012 <= float(item["height_ratio"]) <= 0.14]
    if not observations:
        return CaptionStyle(evidence=["화면에서 자막으로 볼 만한 글자 영역을 찾지 못했습니다."])

    # OCR이 한글을 정확히 읽지 못해도 글자 상자의 위치·크기·색은 쓸 수 있다.
    # 그래서 글자 내용이 아니라 "영상 내내 같은 높이에서 반복되는가"로 말 자막 띠를 고른다.
    bands: dict[int, list[dict[str, object]]] = defaultdict(list)
    for item in observations:
        bands[round(float(item["center_y"]) * 20)].append(item)

    def band_score(items: list[dict[str, object]]) -> tuple[int, float]:
        moments = {round(float(item["time"]), 2) for item in items}
        spread = (max(moments) - min(moments)) / max(duration, 1e-6) if moments else 0.0
        return len(moments), spread

    band, selected = max(bands.items(), key=lambda entry: band_score(entry[1]))
    times, spread = band_score(selected)
    # 제목 카드는 앞부분에만 잠깐 나온다. 영상 전체에 걸쳐 반복되는 띠만 말 자막으로 인정한다.
    if times < 5 or spread < 0.35:
        return CaptionStyle(
            confidence=0.2,
            evidence=[
                f"반복되는 자막 띠를 확정하지 못했습니다 (관측 {times}회, 분포 {spread * 100:.0f}%).",
                "자막 스타일을 추측해서 적용하지 않았습니다.",
            ],
        )

    centers = [float(item["center_y"]) for item in selected]
    heights = [float(item["height_ratio"]) for item in selected]
    center = float(np.median(centers))
    height = float(np.median(heights))
    position = "상단" if center < 0.36 else "중앙" if center < 0.66 else "하단"
    size = "작게" if height < 0.035 else "보통" if height < 0.065 else "크게"
    colors = Counter(str(item["color"]) for item in selected)
    color, color_votes = colors.most_common(1)[0]
    outline_ratio = sum(bool(item["outline"]) for item in selected) / len(selected)
    background_ratio = sum(bool(item["background"]) for item in selected) / len(selected)
    animation = infer_caption_animation(selected)
    confidence = min(0.9, 0.4 + times / 40.0)
    return CaptionStyle(
        position=position,
        size=f"{size} (화면 높이의 약 {height * 100:.1f}%)",
        color=color if color_votes / len(selected) >= 0.5 else "판별 불가",
        outline="검정 외곽선/그림자 추정" if outline_ratio >= 0.45 else "뚜렷한 외곽선 미검출",
        background="반투명·단색 박스 추정" if background_ratio >= 0.45 else "배경 박스 미검출",
        animation=animation,
        confidence=round(confidence, 2),
        evidence=[
            f"{times}개 시점에서 세로 중심 {center * 100:.0f}% 부근에 반복 등장",
            f"글자색 판정 {color} ({color_votes}/{len(selected)} 관측)",
            "완성 영상만으로 정확한 폰트명·CapCut 프리셋명은 확정할 수 없습니다.",
        ],
    )


def read_text_observations(path: Path, tesseract: str, timecode: float) -> list[dict[str, object]]:
    completed = subprocess.run(
        [tesseract, str(path), "stdout", "-l", "kor+eng", "--psm", "6", "tsv"],
        capture_output=True, text=True, timeout=30,
    )
    if completed.returncode != 0:
        return []
    image = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    height, width = image.shape[:2]
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    try:
        rows = csv.DictReader(io.StringIO(completed.stdout), delimiter="\t")
        for row in rows:
            text = (row.get("text") or "").strip()
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                continue
            if text and confidence >= 35:
                groups[(row.get("block_num", ""), row.get("par_num", ""), row.get("line_num", ""))].append(row)
    except csv.Error:
        return []
    observations: list[dict[str, object]] = []
    for words in groups.values():
        left = min(int(word["left"]) for word in words)
        top = min(int(word["top"]) for word in words)
        right = max(int(word["left"]) + int(word["width"]) for word in words)
        bottom = max(int(word["top"]) + int(word["height"]) for word in words)
        if right <= left or bottom <= top:
            continue
        crop = image[top:bottom, left:right]
        color, outline, background = describe_text_pixels(crop)
        observations.append(
            {
                "time": timecode,
                "text": " ".join(word["text"].strip() for word in words),
                "center_y": ((top + bottom) / 2) / height,
                "height_ratio": (bottom - top) / height,
                "area_ratio": ((right - left) * (bottom - top)) / (width * height),
                "color": color,
                "outline": outline,
                "background": background,
            }
        )
    return observations


def glyph_pixels(pixels: np.ndarray, luminance: np.ndarray) -> np.ndarray:
    """글자 상자 안에서 배경이 아닌 획 픽셀만 골라낸다.

    상자에는 글자보다 배경이 훨씬 많으므로, 밝기를 두 무리로 가른 뒤
    적은 쪽을 글자로 본다. 이렇게 해야 따뜻한 조명 배경 때문에 흰 글자가
    노랑으로 잘못 판정되지 않는다.
    """
    counts, edges = np.histogram(luminance, bins=32, range=(0.0, 255.0))
    total = int(counts.sum())
    if total == 0:
        return pixels
    centers = (edges[:-1] + edges[1:]) / 2
    weight = np.cumsum(counts)
    mean = np.cumsum(counts * centers)
    spread = weight * (total - weight)
    with np.errstate(divide="ignore", invalid="ignore"):
        variance = np.where(spread > 0, (mean[-1] * weight - mean * total) ** 2 / np.maximum(spread, 1e-6), 0.0)
    threshold = float(centers[int(np.argmax(variance))])
    brighter = pixels[luminance >= threshold]
    darker = pixels[luminance < threshold]
    if len(brighter) == 0 or len(darker) == 0:
        return pixels
    return brighter if len(brighter) <= len(darker) else darker


def describe_text_pixels(crop: np.ndarray) -> tuple[str, bool, bool]:
    if crop.size == 0:
        return "판별 불가", False, False
    pixels = crop.reshape(-1, 3).astype(np.float32)
    luminance = pixels.mean(axis=1)
    bright = float(np.mean(luminance > 205))
    dark = float(np.mean(luminance < 55))
    strokes = glyph_pixels(pixels, luminance)
    saturated = strokes[np.ptp(strokes, axis=1) > 55]
    if len(saturated):
        mean = saturated.mean(axis=0)
        if mean[0] > mean[1] * 1.22 and mean[0] > mean[2] * 1.22:
            color = "빨강 계열"
        elif mean[0] > mean[2] * 1.25 and mean[1] > mean[2] * 1.25:
            color = "노랑 계열"
        elif mean[2] > mean[0] * 1.2:
            color = "파랑 계열"
        else:
            color = "컬러 강조"
    else:
        stroke_luminance = strokes.mean(axis=1)
        color = "흰색 계열" if float(stroke_luminance.mean()) >= 128 else "검정 계열"
    outline = bright > 0.06 and dark > 0.06
    background = (dark > 0.60 or bright > 0.72) and float(np.std(luminance)) < 78
    return color, outline, background


def infer_caption_animation(observations: list[dict[str, object]]) -> str:
    by_text: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in observations:
        key = str(item["text"]).replace(" ", "").lower()
        if len(key) >= 2:
            by_text[key].append(item)
    votes: Counter[str] = Counter()
    for values in by_text.values():
        values.sort(key=lambda item: float(item["time"]))
        for first, second in zip(values, values[1:]):
            delta = float(second["time"]) - float(first["time"])
            if not 0 < delta <= 0.75:
                continue
            area_ratio = float(second["area_ratio"]) / max(float(first["area_ratio"]), 1e-6)
            center_shift = float(second["center_y"]) - float(first["center_y"])
            if area_ratio >= 1.25:
                votes["팝업/확대 등장 추정"] += 1
            elif abs(center_shift) >= 0.035:
                votes["위·아래 슬라이드 등장 추정"] += 1
            else:
                votes["페이드 또는 즉시 등장 추정"] += 1
    return votes.most_common(1)[0][0] if votes else "즉시 등장 또는 판별 보류"


def measure_title_card(video_path: Path, work_dir: Path) -> TitleCard | None:
    """도입부에만 떠 있다가 사라지는 제목 카드를 찾아 위치·크기·색·박스를 잰다.

    말 자막과 달리 한동안 그대로 붙어 있다가 통째로 사라지는 것이 특징이다.
    """
    frame_dir = work_dir / "caption_frames"
    paths = sorted(frame_dir.glob("band-*.jpg"))
    if len(paths) < 12:
        return None
    rgb = np.stack([np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) for path in paths])
    luminance = rgb.mean(axis=3)
    frames, height, width = luminance.shape
    fps = 4.0
    bright = luminance > 215
    top_zone, bottom_zone = int(height * 0.06), int(height * 0.45)
    area = bright[:, top_zone:bottom_zone, :].sum(axis=(1, 2)).astype(np.float32)
    if float(area.max()) < width * 0.02 * height * 0.02:
        return None
    present = np.where(area > area.max() * 0.25)[0]
    if len(present) < 4:
        return None
    # 도입부에는 제목 카드 다음에 프로필 카드 같은 다른 오버레이가 이어지기도 한다.
    # 내용이 크게 바뀌는 지점에서 끊고 맨 앞 덩어리만 제목으로 본다.
    zone_mask = bright[:, top_zone:bottom_zone, :]
    runs: list[list[int]] = [[int(present[0])]]
    for index in present[1:]:
        index = int(index)
        previous = runs[-1][-1]
        gap = index - previous
        changed = float(np.logical_xor(zone_mask[index], zone_mask[previous]).sum())
        reference = max(float(zone_mask[index].sum()), float(zone_mask[previous].sum()), 1.0)
        if gap > 2 or changed > reference * 0.55:
            runs.append([index])
        else:
            runs[-1].append(index)
    first = runs[0]
    start, end = float(first[0]) / fps, float(first[-1] + 1) / fps
    # 영상 내내 떠 있으면 제목 카드가 아니라 고정 자막이다.
    if end - start < 0.8 or (end - start) > frames / fps * 0.6:
        return None
    present = np.array(first)

    # 위치·크기는 축소 프레임에서, 색은 원본 해상도 한 장에서 잰다.
    middle = int(present[len(present) // 2])
    zone = rgb[middle, top_zone:bottom_zone]
    zone_luminance = luminance[middle, top_zone:bottom_zone]
    rows = (zone_luminance > 215).sum(axis=1)
    if not rows.any():
        return None
    hit = np.where(rows > max(rows.max() * 0.15, 1))[0]
    band_top, band_bottom = int(hit.min()), int(hit.max())
    center_y = (top_zone + (band_top + band_bottom) / 2) / height
    height_ratio = (band_bottom - band_top + 1) / height

    sharp = grab_full_frame(video_path, work_dir, middle / fps)
    if sharp is None:
        sharp, sharp_top, sharp_bottom = zone, band_top, band_bottom
    else:
        sharp_height = sharp.shape[0]
        sharp_top = int((top_zone + band_top) / height * sharp_height)
        sharp_bottom = int((top_zone + band_bottom + 1) / height * sharp_height)
        sharp = sharp[max(0, sharp_top):min(sharp_height, sharp_bottom)]
    if sharp.size == 0:
        return None
    sharp_luminance = sharp.mean(axis=2)
    strokes = sharp[sharp_luminance > 215]
    if len(strokes) < 100:
        return None
    color = classify_stroke_color(strokes.mean(axis=0))

    # 박스 색은 가장 진하게 물든 픽셀에서만 고른다. 흐린 가장자리를 섞으면 색이 바랜다.
    saturation = np.ptp(sharp, axis=2)
    box_color = None
    if float(np.mean(saturation > 60)) > 0.02:
        vivid = sharp[saturation > np.percentile(saturation, 98.5)]
        if len(vivid) >= 60:
            red, green, blue = (int(value) for value in np.median(vivid, axis=0))
            box_color = f"#{red:02X}{green:02X}{blue:02X}"
    return TitleCard(
        start=round(start, 2),
        end=round(end, 2),
        center_y=round(center_y, 4),
        height_ratio=round(height_ratio, 4),
        color=color,
        box_color=box_color,
    )


def grab_full_frame(video_path: Path, work_dir: Path, moment: float) -> np.ndarray | None:
    """지정한 시각의 화면을 원본 해상도로 한 장 뽑는다."""
    target = work_dir / "title_frame.jpg"
    try:
        run(
            [
                "ffmpeg", "-y", "-v", "error", "-ss", f"{moment:.2f}",
                "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(target),
            ],
            timeout=60,
        )
    except (ToolError, OSError):
        return None
    if not target.is_file():
        return None
    return np.asarray(Image.open(target).convert("RGB"), dtype=np.float32)
