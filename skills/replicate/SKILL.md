---
name: replicate
version: "0.1.0"
description: Analyze a reference video's subtitle effects and sound-effect events for CapCut replication. Detects subtitle spans from the transcript, samples high-fps frame bursts around each entrance/exit, has Claude describe the animation features, maps them to CapCut text-animation candidates with confidence ratings, and detects transition/accent SFX onsets with original-audio clip extraction. Produces the analysis report (JSON + markdown) and generates a native CapCut draft with the effects and SFX placed on the timeline.
argument-hint: "<video-url-or-path>"
allowed-tools: Bash, Read, Write
license: MIT
user-invocable: true
---

# /replicate — 레퍼런스 자막 효과 분석 + 캡컷 드래프트 생성

레퍼런스 영상의 자막 효과(등장/유지/퇴장 애니메이션)와 효과음을 관찰해 캡컷 실제 UI
효과로 매핑한 **분석 리포트**를 만들고(Step 1~7), 효과·효과음이 배치된 **캡컷 드래프트**
까지 생성한다(Step 8). 근거 스펙: `docs/SPEC-subtitle-effect-replication.md`.

> **후보 신뢰도:** 매핑 후보의 `verified: false`는 "표시명·ID는 실제 캡컷 카탈로그의 것이
> 확실하나, 그 효과의 시각적 느낌이 관찰 특징과 닮았는지는 재생 검증 전"이라는 뜻이다.
> 확정이 아니라 **후보**로 전달하고, 재생으로 확인한 효과는 verified를 올린다.

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
  - `/watch`가 workdir에 **`transcript.json`**(세그먼트 리스트)을 항상 남긴다 → Step 2에 그 파일을 그대로 준다.
    (구버전 watch라면 캡션 경로의 `video*.vtt`를 쓰거나 세그먼트를 직접 Write.)

## Step 2 — 자막 구간 후보 (F1-1)

```bash
python3 "${SKILL_DIR}/scripts/spans.py" <video.vtt 또는 segments.json>
```

출력: `spans`(구간 후보) + `burst_events`(등장/퇴장 시각) + `burst_events_arg`
(다음 단계에 그대로 넣을 콤마 문자열). **연속 내레이션 영상**(캡션 큐가 빈틈없이
이어져 구간이 하나로 뭉개짐 — 스크립트가 경고를 출력함)은 `--segment-boundaries`로
재실행해 세그먼트 경계를 이벤트로 쓴다.

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

## Step 6 — 효과음 이벤트 검출 (F4, M4)

```bash
python3 "${SKILL_DIR}/scripts/audio_events.py" <video> <workdir>/sfx \
  --scenes "<장면 전환 시각들>"
```

- 오디오의 고주파 트랜지언트(우쉬·팝·히트)를 onset으로 검출하고, `--scenes`로 준 장면
  전환 시각 ±0.3초 안에 있으면 `transition`, 아니면 `accent`로 분류한다 (F4-2).
  장면 전환 시각은 `/watch`의 scene-change 프레임 타임스탬프(`reason: scene-change`)를
  그대로 쓰면 된다. 없으면 `--scenes` 생략 — 전부 `accent`로 나온다.
- 검출된 각 이벤트는 원본에서 잘라낸 `sfx_NNN.wav`로 저장된다 (F4-3 1차 — 드래프트
  배치는 M3 검증 뒤). 클립이 필요 없으면 `--no-clips`.
- 무음 영상이면 스크립트가 명시적으로 실패한다 — 리포트에 "오디오 없음"으로 적는다.
- 검출은 v1 휴리스틱이다: 점수(`score`)가 낮은 이벤트는 의심하고, 놓친 효과음이
  의심되면 `--sensitivity`를 낮춰(예: 2.0) 재실행한다. 이벤트가 `--max-events`(기본
  40)를 넘으면 점수 상위만 남기고 경고를 출력한다.
- **저작권 (F4-4)**: 추출 클립은 레퍼런스 원본의 일부다. 리포트의 `copyright_notice`를
  report.md에 그대로 옮긴다 — 개인 학습·프로토타입 용도 한정, 배포물에는 내장/라이선스
  SFX로 교체.

## Step 7 — 리포트 산출 (M1 Definition of Done)

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
  "unmapped": [],
  "audio_events": [
    {"index": 0, "time": 3.1, "end": 3.6, "type": "transition", "near_scene": 3.0,
     "score": 8.2, "clip": "<workdir>/sfx/sfx_000.wav"}
  ]
}
```

`matched: false`였던 항목은 해당 span의 `needs_review: true` + 최상위 `unmapped`에
집계한다.

**`report.md`** — 사람용: 구간별로 시각 / 원문 / 추정 효과(한국어 라벨 + 신뢰도) /
수동 확인 필요 여부를 표로. 마지막에 저작권 주의 문구(레퍼런스 분석 결과는 개인
학습·프로토타입 용도)와 "효과 후보는 캡컷 실행 검증 전(unverified)" 고지를 넣는다.

사용자에게는 report.md 내용을 요약해 전달하고 두 파일 경로를 알려준다.

## Step 8 — 캡컷 드래프트 생성 (M3/M4 출력)

리포트가 확정되면(효과 후보·효과음 배치까지) 캡컷이 바로 여는 드래프트를 생성한다:

```bash
python3 "${SKILL_DIR}/scripts/draft.py" create --name <프로젝트명> \
  --subtitles '[{"text":"자막","start":0,"end":2.5,"in":"Preview Type","out":"Fade Out"}]' \
  --sfx '[{"path":"<workdir>/sfx/sfx_001.wav","time":2.96,"name":"전환음"}]'
```

- `in`/`out`/`loop`는 `capcut-ui-catalog.json`의 표시명 그대로 (effects.py lookup 결과의 title).
- 효과음 클립은 프로젝트 안(`Resources/replicate_sfx/`)으로 복사되므로 원본이 임시 폴더여도 된다.
- 셸로 쓸 네이티브 프로젝트가 하나 필요하다(캡컷에서 만든 아무 프로젝트). 없으면 스크립트가 안내한다.
- 생성 직후 캡컷을 재시작하면 목록에 나타난다. 미캐시 효과는 "애니메이션 분실" 경고가 떴다가
  세그먼트 선택/재생 시 캡컷이 리소스를 내려받아 해소된다 — 정상 동작이므로 사용자에게 안내할 것.
  (2026-08-26 관찰: 리소스 스토어 응답이 느린 세션에서는 자동 다운로드가 지연/실패할 수 있다 —
  그 경우 애니메이션 패널에서 해당 효과를 한 번 클릭하면 확실히 받아진다. 무료/VIP 여부와 무관.)
- 텍스트는 캡컷에서 자유롭게 교체 가능하며 효과는 유지된다 (실기기 확증).

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
  프레임 버스트), `scripts/effects.py` (효과 카탈로그/매핑 조회), `scripts/audio_events.py`
  (효과음 onset 검출 + 원본 클립 추출), `scripts/sfx_match.py` (효과음 라이브러리 매칭),
  `scripts/draft.py` (네이티브 캡컷 드래프트 생성 — 로컬 파일 조작만). Assets:
  `assets/capcut-effect-catalog.json` (pyCapCut에서 생성), `assets/capcut-effect-map.json`
  (수작업 큐레이션).
