---
name: replicate
version: "0.1.0"
description: Analyze a reference video's subtitle effects for CapCut replication. Detects subtitle spans from the transcript, samples high-fps frame bursts around each entrance/exit, has Claude describe the animation features, and maps them to CapCut text-animation candidates with confidence ratings. Produces the M1 analysis report (JSON + markdown); CapCut draft generation lands in M3.
argument-hint: "<video-url-or-path>"
allowed-tools: Bash, Read, Write
license: MIT
user-invocable: true
---

# /replicate — 레퍼런스 자막 효과 분석 (M1)

레퍼런스 영상의 자막 효과(등장/유지/퇴장 애니메이션)를 관찰해 캡컷 내장 효과 후보로
매핑한 **분석 리포트**를 만든다. 근거 스펙: `docs/SPEC-subtitle-effect-replication.md`.

> **범위 주의(M1):** 이 스킬은 분석·매핑·리포트까지만 한다. 캡컷 드래프트 생성(M3)은
> 캡컷 실행 검증(M3 스파이크의 남은 절차)이 끝난 뒤 추가된다. 리포트의 효과 후보는 전부
> `verified: false` — 국제판 캡컷에서 실제 재생을 확인한 적 없다는 뜻이므로, 사용자에게
> 확정이 아니라 **후보**로 전달한다.

## Resolve `SKILL_DIR`

`/watch`와 같은 규칙: 방금 Read한 이 SKILL.md가 있는 디렉토리의 절대 경로를
`SKILL_DIR`로 삼는다. 스크립트는 `SKILL_DIR/scripts/`, 자산은 `SKILL_DIR/assets/`에 있다.

## Step 1 — 영상과 트랜스크립트 확보

- **로컬 파일**이면 그대로 쓴다. 트랜스크립트가 따로 없으면 `/watch` 스킬로 Whisper
  전사를 받고, 그 세그먼트(`[{start, end, text}]`)를 작업 디렉토리에
  `segments.json`으로 Write 해 둔다.
- **URL**이면 `/watch` 스킬을 `--detail efficient --out-dir <workdir>`로 먼저 실행해
  영상 다운로드 + 트랜스크립트를 받는다 (`--detail transcript`는 캡션이 있으면 영상을
  내려받지 않으므로 쓰지 않는다). 작업 디렉토리는 지우지 말 것 — 여기서 계속 쓴다.
  - 캡션 경로였다면 workdir에 `video*.vtt`가 남는다 → Step 2에 그 파일을 준다.
  - Whisper 경로였다면 파일이 없다 → 트랜스크립트 세그먼트를 `segments.json`으로 Write.

## Step 2 — 자막 구간 후보 (F1-1)

```bash
python3 "${SKILL_DIR}/scripts/spans.py" <video.vtt 또는 segments.json>
```

출력: `spans`(구간 후보) + `burst_events`(등장/퇴장 시각) + `burst_events_arg`
(다음 단계에 그대로 넣을 콤마 문자열). 구간이 명백히 이상하면(예: 롤링 캡션이 한
구간으로 뭉침) `--gap`을 줄여 재실행한다.

이 구간은 **말소리 기준 후보**다. 화면 자막과 다를 수 있으므로(정적 자막, 타이틀 카드),
최종 판단은 Step 3의 프레임으로 한다.

## Step 3 — 고밀도 버스트 샘플링 (F1-2)

```bash
python3 "${SKILL_DIR}/scripts/bursts.py" <video> <workdir>/bursts \
  --events "<burst_events_arg>" --window 0.5 --fps 10
```

각 등장/퇴장 시각 ±0.5초를 10fps로 추출한다 (`/watch`의 2fps 캡은 애니메이션을 못
잡으므로 이 창 안에서만 의도적으로 해제). 겹치는 창은 자동 병합되고, 총 프레임이
`--max-total`(기본 240)을 넘으면 뒤쪽 창을 **버리고 경고를 출력**한다 — 그 경우 버린
구간만 두 번째 실행으로 나눠 뽑는다. 글자를 읽어야 하면 `--resolution 1024`.

**모든 버스트 프레임을 Read 한다** (한 메시지에 병렬로). 창 하나의 프레임 시퀀스가
곧 애니메이션 타임랩스다.

## Step 4 — 효과 특징 기술 (F1-3)

각 자막 구간에 대해 프레임 시퀀스를 보고 구조화된 특징을 산출한다:

- `entrance` / `loop` / `exit`: 아래 vocabulary에서 고른다 (모르면 `none`이 아니라
  "판별 불가"로 기록 — 조용히 아무거나 고르지 않는다).
- `style`: 폰트 계열(고딕/명조/손글씨), 색, 외곽선·그림자·배경 박스 유무, 화면 내 위치.

허용 feature 값 목록:

```bash
python3 "${SKILL_DIR}/scripts/effects.py" features
```

## Step 5 — 캡컷 효과 매핑 (F2)

관찰한 feature마다:

```bash
python3 "${SKILL_DIR}/scripts/effects.py" lookup <entrance|loop|exit> <feature>
```

- `matched: true` → 후보 목록(신뢰도 high/medium/low + 캡컷 effect_id/resource_id).
- `matched: false` → 매핑 없음. 반환된 fallback은 임시 배치용 기본값일 뿐이며, 리포트에
  반드시 **"수동 확인 필요"** 로 표기한다 (F2-2 — 조용히 확정 금지).

자산이 의심되면 `effects.py validate`로 무결성을 확인한다 (카탈로그 재생성은
`tools/build_effect_catalog.py`, pyCapCut 체크아웃 필요).

## Step 6 — 리포트 산출 (M1 Definition of Done)

작업 디렉토리에 두 파일을 Write 한다:

**`report.json`** — 기계용:

```json
{
  "source": "<url-or-path>",
  "analyzed_at": "<ISO8601>",
  "spans": [
    {
      "index": 0,
      "start": 1.2, "end": 3.4,
      "text": "자막 원문",
      "features": {"entrance": "typewriter", "loop": "none", "exit": "fade",
                    "style": {"font": "고딕 계열", "color": "#FFFFFF", "outline": true,
                               "shadow": false, "box": false, "position": "하단 중앙"}},
      "mapping": {
        "entrance": {"feature": "typewriter", "matched": true,
                      "candidates": [{"name": "打字机", "label_ko": "타자기",
                                       "confidence": "high", "effect_id": "…",
                                       "resource_id": "…", "verified": false}]},
        "loop": null,
        "exit": {"feature": "fade", "matched": true, "candidates": ["…"]}
      },
      "needs_review": false
    }
  ],
  "unmapped": []
}
```

`matched: false`였던 항목은 해당 span의 `needs_review: true` + 최상위 `unmapped`에
집계한다.

**`report.md`** — 사람용: 구간별로 시각 / 원문 / 추정 효과(한국어 라벨 + 신뢰도) /
수동 확인 필요 여부를 표로. 마지막에 저작권 주의 문구(레퍼런스 분석 결과는 개인
학습·프로토타입 용도)와 "효과 후보는 캡컷 실행 검증 전(unverified)" 고지를 넣는다.

사용자에게는 report.md 내용을 요약해 전달하고 두 파일 경로를 알려준다.

## 실패 처리

- 트랜스크립트가 전혀 없으면(무음 영상 등) span 후보를 만들 수 없다 — 이때는 `/watch`
  프레임에서 자막이 보이는 시각을 직접 골라 `bursts.py --events`에 수동으로 넣는다.
- 버스트 창이 전부 예산 초과로 잘리면 이벤트를 나눠 여러 번 실행한다 (스크립트 경고 참조).
- 매핑 실패가 절반을 넘으면 매핑 테이블(`assets/capcut-effect-map.json`)의 커버리지
  문제일 가능성이 크다 — 리포트에 그대로 적고, 새 feature→효과 항목 추가를 제안한다.

## Security & Permissions

- 이 스킬 자체는 네트워크에 나가지 않는다 — 다운로드/전사는 `/watch`가 담당하고, 여기
  스크립트들은 로컬 ffmpeg/ffprobe 실행과 로컬 JSON 읽기뿐이다.
- Bundled scripts: `scripts/spans.py` (자막 구간 후보), `scripts/bursts.py` (고밀도
  프레임 버스트), `scripts/effects.py` (효과 카탈로그/매핑 조회). Assets:
  `assets/capcut-effect-catalog.json` (pyCapCut에서 생성), `assets/capcut-effect-map.json`
  (수작업 큐레이션).
