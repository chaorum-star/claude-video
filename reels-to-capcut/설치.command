#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p .models/huggingface

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew가 필요합니다: https://brew.sh"
  read -r -p "설치 후 다시 실행해주세요. Enter를 누르면 닫힙니다."
  exit 1
fi

echo "[1/2] 영상·OCR 도구 확인"
for formula in ffmpeg yt-dlp tesseract tesseract-lang; do
  if ! brew list --versions "$formula" >/dev/null 2>&1; then
    brew install "$formula"
  fi
done

echo "[2/2] CapCut·Whisper 파이썬 도구 설치"
if [ ! -d ".venv" ]; then
  "$(brew --prefix)/bin/python3" -m venv .venv
fi
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install pycapcut faster-whisper

chmod +x "릴스캡컷실행.command" "설치.command"
echo
echo "설치 완료. 릴스캡컷실행.command를 더블클릭하세요."
echo "첫 음성 분석 때 정확도 우선 large-v3 모델을 한 번 내려받습니다."
read -r -p "Enter를 누르면 닫힙니다." || true
