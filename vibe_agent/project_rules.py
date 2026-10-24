"""작업 폴더의 프로젝트 지침 파일(GEMINI.md, AGENTS.md, CLAUDE.md) 읽기.

수업 프롬프트가 "GEMINI.md에 규칙을 적어 두고 앞으로 매 단계 이 파일을 갱신하라"고 하므로,
에이전트가 이 파일들을 매번 새로 읽어 시스템 프롬프트에 반영한다.
"""
from __future__ import annotations

from pathlib import Path

RULE_FILES = ("GEMINI.md", "AGENTS.md", "CLAUDE.md")
MAX_CHARS_PER_FILE = 6000


def find_rule_files(root: Path) -> list[Path]:
    return [root / name for name in RULE_FILES if (root / name).is_file()]


def load_project_rules(root: Path) -> str:
    parts = []
    for path in find_rule_files(root):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > MAX_CHARS_PER_FILE:
            text = text[:MAX_CHARS_PER_FILE] + f"\n... (이하 생략. 전체는 {path.name}을 read_file로 읽으세요)"
        parts.append(f"### {path.name}\n{text}")
    return "\n\n".join(parts)
