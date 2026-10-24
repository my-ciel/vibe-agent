#!/bin/bash
# macOS: 더블클릭으로 실행 (처음에는 우클릭 → 열기가 필요할 수 있어요)
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "[Vibe Agent] 'uv'라는 도우미 도구가 아직 없어요."
  echo "uv는 파이썬과 필요한 패키지를 자동으로 준비해 줍니다. 한 번만 설치하며 1분 정도 걸려요."
  echo "출처: https://astral.sh/uv (공식 설치 스크립트, 오픈소스)"
  echo
  read -r -p "지금 설치할까요? y 를 입력하고 Enter: " answer
  case "$answer" in
    [yY]*) curl -LsSf https://astral.sh/uv/install.sh | sh ;;
    *) echo "취소했어요. 강사에게 수동 설치 방법을 물어보세요."; read -r -p "Enter를 누르면 닫혀요"; exit 1 ;;
  esac
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv를 자동으로 설치하지 못했어요. 이 창을 강사에게 보여주세요."
    read -r -p "Enter를 누르면 닫혀요"
    exit 1
  fi
fi

uv run python launch.py "$@"
status=$?
if [ $status -ne 0 ]; then read -r -p "Enter를 누르면 닫혀요"; fi
exit $status
