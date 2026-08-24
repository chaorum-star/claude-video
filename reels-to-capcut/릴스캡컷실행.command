#!/bin/bash
set -e
cd "$(dirname "$0")"
export HF_HOME="$(pwd)/.models/huggingface"

PYTHON="$(pwd)/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "전용 실행 환경을 찾지 못했습니다. 먼저 설치.command를 실행해주세요."
  read -r -p "Enter를 누르면 닫힙니다." || true
  exit 1
fi

exec "$PYTHON" -m reels_to_capcut.server
