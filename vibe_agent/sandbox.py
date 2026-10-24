"""작업 폴더 경계와 명령 정책.

경로 검사는 확실한 경계지만, 명령 차단 목록은 실수 방지용 안전망일 뿐이다.
명령은 작업 폴더 밖에도 영향을 줄 수 있으므로 실제 방어선은 사용자 승인이다.
"""
from __future__ import annotations

import re
from pathlib import Path

from .errors import SandboxError

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea"}
PROTECTED_DIRS = {".git"}


def resolve_in_root(root: Path, user_path: str) -> Path:
    """user_path를 절대 경로로 풀고 root 하위인지 검사한다 (symlink, .. 포함)."""
    if not isinstance(user_path, str) or "\x00" in user_path:
        raise SandboxError("잘못된 경로입니다.")
    root = root.resolve()
    candidate = Path(user_path.strip() or ".")
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise SandboxError(f"작업 폴더 밖의 경로는 접근할 수 없습니다: {user_path}")
    return resolved


def relative_display(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    return rel.as_posix() or "."


def check_writable(root: Path, path: Path) -> None:
    rel = path.resolve().relative_to(root.resolve())
    if PROTECTED_DIRS.intersection(rel.parts):
        raise SandboxError(f"보호된 폴더는 수정할 수 없습니다: {rel.as_posix()}")
    if path.is_dir():
        raise SandboxError(f"폴더에는 쓸 수 없습니다: {rel.as_posix()}")


_BLOCKED_COMMANDS: list[tuple[str, str]] = [
    (r"\bsudo\b", "sudo(관리자 권한) 명령은 허용되지 않습니다."),
    (
        r"\brm\s+(-\S+\s+)*-\S*[rR]\S*\s+(/|~|\$HOME|%USERPROFILE%|\*|[A-Za-z]:\\?)(\s|$)",
        "홈/루트/드라이브 전체를 지우는 명령은 허용되지 않습니다.",
    ),
    (
        r"\b(rd|rmdir|del|erase)\b.*\s[A-Za-z]:\\?(\s|$)",
        "드라이브 전체를 지우는 명령은 허용되지 않습니다.",
    ),
    (r"\bformat\s+[A-Za-z]:", "디스크 포맷 명령은 허용되지 않습니다."),
    (r"\bmkfs\b|\bdd\s+.*of=/dev/", "디스크를 직접 쓰는 명령은 허용되지 않습니다."),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "시스템 종료/재부팅은 허용되지 않습니다."),
    (r"\breg\s+(delete|add)\b", "레지스트리 변경은 허용되지 않습니다."),
    (
        r"(curl|wget|iwr|Invoke-WebRequest)[^|]*\|\s*(sh|bash|zsh|iex|Invoke-Expression|powershell)",
        "내려받은 스크립트를 바로 실행하는 명령은 허용되지 않습니다.",
    ),
    (r":\(\)\s*\{", "포크 폭탄은 허용되지 않습니다."),
    (r"\bchmod\s+-R\s+\S+\s+/(\s|$)", "루트 권한 변경은 허용되지 않습니다."),
]


def check_command(command: str) -> str | None:
    """차단해야 하면 사유를, 아니면 None을 반환한다."""
    if not command.strip():
        return "빈 명령입니다."
    for pattern, reason in _BLOCKED_COMMANDS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None
