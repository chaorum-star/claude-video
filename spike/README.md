# M3 스파이크 결과 (2026-08-25)

목표: "효과 1개 하드코딩 드래프트를 캡컷이 여는지" 검증 (SPEC 7절)

## 결과 요약

| 검증 항목 | 결과 |
|---|---|
| 드래프트 생성 (`draft_content.json`) | ✅ pyCapCut으로 생성 성공 (`out/m3-spike/`) |
| 텍스트-효과 구조 분리 (F3-2 근거) | ✅ 텍스트는 `materials.texts[].content`, 효과는 `materials.material_animations`를 세그먼트 `extra_material_refs`로 참조 — 구조적으로 분리됨 |
| 프로그램적 텍스트 교체 후 효과 유지 | ✅ 템플릿 모드 `replace_text()`로 교체, 애니메이션 참조 유지 확인 (`out/m3-spike-replaced/`) |
| **실제 캡컷에서 열림 검증** | ❌ **실패 (2026-08-26, CapCut 9.3.0 macOS).** 프로젝트 목록 등록은 되지만 열기는 조용히 실패. 원인: 캡컷 9.x는 네이티브 `Timelines` 구조(루트 `draft_info.json` + `Timelines/<id>/draft_info.json` + 부속 파일들)를 요구하는데 pyCapCut은 구형 `draft_content.json`만 생성 |

## D-1 (라이브러리 채택) 판단 재료

- 국제판 캡컷용은 `pyJianYingDraft`가 아니라 같은 개발자의 **pyCapCut** (Apache-2.0 계열 확인 필요 → LICENSE 확인: pyJianYingDraft는 MIT).
- 텍스트 애니메이션 메타데이터 내장: **입장 182 · 유지 81 · 퇴장 100종** — 효과 이름과 resource ID가 이미 수집돼 있어 **M2(효과 ID 부트스트랩) 작업이 대폭 줄어듦**. 수동 역추출은 pyCapCut에 없는 효과에만 필요.
- 템플릿 모드(`duplicate_as_template` + `replace_text`)가 F3-2 요구를 그대로 구현.
- 주의: README에 "배치 **내보내기(export)** 자동화는 Windows 캡컷 필요" — 드래프트 생성·열기와는 무관 (내보내기는 사용자가 캡컷에서 직접 하면 됨).

## 남은 검증 절차 (캡컷 설치된 환경에서)

1. `out/m3-spike/` 폴더를 캡컷 드래프트 폴더로 복사
   - macOS: `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/`
   - Windows: `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\`
2. 캡컷 실행(이미 실행 중이면 재시작) → 프로젝트 목록에 `m3-spike` 확인 → 열기
3. 확인할 것:
   - [ ] 오류 없이 로드되는가
   - [ ] 자막1에 Wiping In(입장)/Wiping Out(퇴장) 애니메이션이 재생되는가
   - [ ] 자막2에 Golden Dust 입장이 재생되는가
   - [ ] 캡컷 UI에서 자막1 텍스트를 직접 수정해도 애니메이션이 유지되는가
4. 결과를 이 파일에 기록하고, 성공 시 SPEC의 D-1을 "pyCapCut 채택"으로 확정

## 재현 방법

```bash
# pyCapCut 클론 + 의존성 (pymediainfo, imageio)
git clone https://github.com/GuanYixuan/pyCapCut
python3 -m venv .venv && .venv/bin/pip install pymediainfo imageio

# 드래프트 생성
PYCAPCUT_DIR=./pyCapCut .venv/bin/python3 m3_spike_draft.py
```

## 2026-08-26 검증 결과 (캡컷 9.3.0 설치 후)

- **D-1 확정: pyCapCut 드래프트 생성 불채택.** 위 표대로 구형 구조라 열리지 않음.
  네이티브 Timelines 구조 생성기(`codex/reels-to-capcut-app` 브랜치의 `capcut_native.py` 접근)로 수렴할 것.
- pyCapCut의 효과 메타데이터도 국제판 캡컷 카탈로그와 **15/363만 effect_id 일치** —
  카탈로그 원천을 캡컷 로컬 캐시 인덱스(`tools/index_capcut_resources.py` → `capcut-ui-catalog.json`,
  텍스트 애니메이션 800종 영문 UI명 포함)로 교체.
- 네이티브 드래프트(0826)는 정상 열림 확인 → 스키마 참조본으로 사용 가능.
