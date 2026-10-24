"""강사가 수업에 맞게 에이전트 지침을 덧붙이는 파일(instructions.md) 읽기."""
from __future__ import annotations

import re
from pathlib import Path

INSTRUCTIONS_FILE = Path(__file__).resolve().parent.parent / "instructions.md"
MAX_CHARS = 4000


def load_instructions(path: Path = INSTRUCTIONS_FILE) -> str:
    """<!-- 주석 -->을 제외한 내용. 파일이 없거나 비어 있으면 빈 문자열."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()[:MAX_CHARS]
