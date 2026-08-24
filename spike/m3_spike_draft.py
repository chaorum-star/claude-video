# M3 스파이크 — 자막 효과 1개짜리 최소 캡컷 드래프트 생성
#
# 검증 목표 (SPEC 7절 M3 스파이크):
#   1. 생성한 드래프트가 캡컷(국제판 데스크톱)에서 오류 없이 열리는가
#   2. 텍스트 애니메이션(입장/유지/퇴장)이 실제로 재생되는가
#   3. 캡컷 UI에서 텍스트만 수정해도 효과가 유지되는가 (F3-2)
#
# 사용법:
#   python3 m3_spike_draft.py [드래프트폴더경로]
#   경로 생략 시 ./out/ 아래에 생성 → 캡컷 드래프트 폴더로 복사해서 열기
#   (캡컷 드래프트 폴더 예: ~/Movies/CapCut/User Data/Projects/com.lveditor.draft)
#
# 의존: pyCapCut (https://github.com/GuanYixuan/pyCapCut) + pymediainfo, imageio

import os
import sys

PYCAPCUT_DIR = os.environ.get(
    "PYCAPCUT_DIR",
    os.path.join(os.path.dirname(__file__), "pyCapCut"),
)
sys.path.insert(0, PYCAPCUT_DIR)

import pycapcut as cc  # noqa: E402
from pycapcut import trange, tim  # noqa: E402

out_root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "out")
os.makedirs(out_root, exist_ok=True)

folder = cc.DraftFolder(out_root)
script = folder.create_draft("m3-spike", 1080, 1920, allow_replace=True)  # 쇼츠 9:16

script.add_track(cc.TrackType.text)

# 자막 1: 입장(타자기 계열) + 유지(흔들림 계열) + 퇴장 — 효과 3종 동시 검증
seg1 = cc.TextSegment(
    "[자막1] 텍스트만 바꿔보세요",
    trange("0s", "3s"),
    style=cc.TextStyle(size=15.0, color=(1.0, 1.0, 1.0), bold=True),
    clip_settings=cc.ClipSettings(transform_y=-0.6),
    border=cc.TextBorder(color=(0.0, 0.0, 0.0)),
)
seg1.add_animation(cc.TextIntro.Wiping_In, duration=tim("0.5s"))
seg1.add_animation(cc.TextOutro.Wiping_Out if hasattr(cc.TextOutro, "Wiping_Out") else list(cc.TextOutro)[0], duration=tim("0.5s"))

# 자막 2: 다른 입장 효과 — 구간이 이어져도 각자 독립 적용되는지 확인
seg2 = cc.TextSegment(
    "[자막2] 두 번째 효과",
    trange("3s", "3s"),
    style=cc.TextStyle(size=15.0, color=(1.0, 0.9, 0.2), bold=True),
    clip_settings=cc.ClipSettings(transform_y=-0.6),
    border=cc.TextBorder(color=(0.0, 0.0, 0.0)),
)
seg2.add_animation(cc.TextIntro.Golden_Dust, duration=tim("0.5s"))

script.add_segment(seg1).add_segment(seg2)
script.save()

draft_path = os.path.join(out_root, "m3-spike")
print(f"드래프트 생성 완료: {draft_path}")
print("draft_content.json 크기:", os.path.getsize(os.path.join(draft_path, "draft_content.json")), "bytes")
