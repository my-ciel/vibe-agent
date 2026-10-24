"""에이전트가 호출하는 도구. 읽기 도구는 자동 실행, 쓰기/명령 도구는 승인이 필요하다."""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import platform_utils as pu
from .errors import SandboxError, ToolError
from .sandbox import (
    IGNORED_DIRS,
    check_command,
    check_writable,
    relative_display,
    resolve_in_root,
)

MAX_READ_BYTES = 2_000_000
MAX_WRITE_CHARS = 1_000_000
MAX_LIST_ENTRIES = 300
MAX_GREP_MATCHES = 100
MAX_GREP_FILE_BYTES = 1_000_000
MAX_PREVIEW_LINES = 200
DEFAULT_READ_LINES = 400
MAX_READ_LINES = 2000
DEFAULT_COMMAND_TIMEOUT = 60
MAX_COMMAND_TIMEOUT = 300

_ENCODINGS = ("utf-8", "cp949")  # 한국어 윈도우의 기존 파일도 읽을 수 있게 한다.


@dataclass(frozen=True)
class ToolContext:
    root: Path


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], str]
    needs_approval: bool = False
    # 승인 화면에 보여줄 미리보기. 인자가 잘못됐으면 ToolError로 승인 전에 거절한다.
    preview: Callable[[ToolContext, dict[str, Any]], str] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------- 인자 처리


def _str_arg(args: dict, name: str, default: str | None = None) -> str:
    value = args.get(name, default)
    if value is None:
        raise ToolError(f"'{name}' 인자가 필요합니다.")
    if not isinstance(value, str):
        raise ToolError(f"'{name}' 인자는 문자열이어야 합니다.")
    return value


def _int_arg(args: dict, name: str, default: int, lo: int, hi: int) -> int:
    value = args.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{name}' 인자는 정수여야 합니다.") from None
    return max(lo, min(hi, number))


def _bool_arg(args: dict, name: str) -> bool:
    value = args.get(name, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


# ---------------------------------------------------------------- 텍스트 파일


@dataclass
class TextDoc:
    """줄바꿈은 \\n으로 정규화해 다루고, 저장할 때 원래 형식(CRLF, BOM, 인코딩)을 복원한다."""

    text: str = ""
    encoding: str = "utf-8"
    bom: bool = False
    crlf: bool = False


def load_doc(path: Path) -> TextDoc:
    if path.stat().st_size > MAX_READ_BYTES:
        raise ToolError(f"파일이 너무 큽니다 (최대 {MAX_READ_BYTES // 1_000_000}MB).")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError("바이너리 파일은 읽을 수 없습니다.")
    bom = data.startswith(b"\xef\xbb\xbf")
    for encoding in _ENCODINGS:
        try:
            raw = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if bom and encoding == "utf-8":
            raw = raw[1:]
        crlf = raw.count("\r\n") * 2 >= raw.count("\n") > 0
        return TextDoc(raw.replace("\r\n", "\n"), encoding, bom and encoding == "utf-8", crlf)
    raise ToolError("지원하지 않는 인코딩의 파일입니다 (UTF-8, CP949만 지원).")


def encode_doc(doc: TextDoc, text: str) -> bytes:
    text = text.replace("\r\n", "\n")
    if doc.crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode(doc.encoding)
    return (b"\xef\xbb\xbf" + data) if doc.bom else data


def _write_doc(path: Path, doc: TextDoc, text: str) -> None:
    try:
        data = encode_doc(doc, text)
    except UnicodeEncodeError:
        # cp949 파일에 cp949로 표현 못 하는 글자가 들어온 경우 UTF-8로 저장한다.
        data = encode_doc(TextDoc(encoding="utf-8", crlf=doc.crlf), text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _diff(rel: str, old: str, new: str) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm=""
        )
    )
    if not lines:
        return "(변경 없음)"
    if len(lines) > MAX_PREVIEW_LINES:
        extra = len(lines) - MAX_PREVIEW_LINES
        lines = lines[:MAX_PREVIEW_LINES] + [f"... (diff {extra}줄 더 있음)"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 읽기 도구


def _list_dir(ctx: ToolContext, args: dict) -> str:
    target = resolve_in_root(ctx.root, _str_arg(args, "path", "."))
    if not target.exists():
        raise ToolError(f"존재하지 않는 경로입니다: {args.get('path', '.')}")
    if not target.is_dir():
        raise ToolError("폴더가 아닙니다. 파일은 read_file로 읽으세요.")
    depth = _int_arg(args, "depth", 1, 1, 3)

    lines: list[str] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError as exc:
            lines.append(f"{'  ' * level}(읽기 실패: {exc.strerror})")
            return
        for entry in entries:
            if len(lines) >= MAX_LIST_ENTRIES:
                truncated = True
                return
            is_dir = entry.is_dir() and not entry.is_symlink()
            if is_dir and entry.name in IGNORED_DIRS:
                continue
            lines.append(f"{'  ' * level}{entry.name}{'/' if is_dir else ''}")
            if is_dir and level + 1 < depth:
                walk(entry, level + 1)

    walk(target, 0)
    if not lines:
        return "(빈 폴더)"
    if truncated:
        lines.append(f"... (항목이 많아 {MAX_LIST_ENTRIES}개까지만 표시)")
    return "\n".join(lines)


def _read_file(ctx: ToolContext, args: dict) -> str:
    path = resolve_in_root(ctx.root, _str_arg(args, "path"))
    if not path.exists():
        hint = ""
        attached = ctx.root / "attachments" / Path(args["path"]).name
        if attached.is_file():
            hint = f" (첨부 파일은 attachments/{attached.name} 에 있어요)"
        raise ToolError(f"존재하지 않는 파일입니다: {args['path']}{hint}")
    if path.is_dir():
        raise ToolError("폴더입니다. list_dir를 사용하세요.")
    offset = _int_arg(args, "offset", 1, 1, 10**9)
    limit = _int_arg(args, "limit", DEFAULT_READ_LINES, 1, MAX_READ_LINES)

    lines = load_doc(path).text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        return "(빈 파일)"
    if offset > total:
        raise ToolError(f"offset({offset})이 파일 전체 줄 수({total})보다 큽니다.")
    chunk = lines[offset - 1 : offset - 1 + limit]
    body = "\n".join(f"{offset + i:>6}\t{line}" for i, line in enumerate(chunk))
    end = offset + len(chunk) - 1
    if end < total:
        body += f"\n(총 {total}줄 중 {offset}-{end}줄 표시. offset={end + 1}로 이어서 읽으세요)"
    return body


def _grep(ctx: ToolContext, args: dict) -> str:
    flags = re.IGNORECASE if _bool_arg(args, "ignore_case") else 0
    try:
        regex = re.compile(_str_arg(args, "pattern"), flags)
    except re.error as exc:
        raise ToolError(f"정규식 오류: {exc}") from None
    base = resolve_in_root(ctx.root, _str_arg(args, "path", "."))
    if not base.exists():
        raise ToolError(f"존재하지 않는 경로입니다: {args.get('path', '.')}")
    pattern = args.get("glob")
    if pattern is not None and not isinstance(pattern, str):
        raise ToolError("'glob' 인자는 문자열이어야 합니다.")

    def files() -> Any:
        if base.is_file():
            yield base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in IGNORED_DIRS and not Path(dirpath, d).is_symlink()
            )
            for name in sorted(filenames):
                yield Path(dirpath, name)

    matches: list[str] = []
    for file in files():
        rel = relative_display(ctx.root, file)
        if pattern and not (fnmatch.fnmatch(file.name, pattern) or fnmatch.fnmatch(rel, pattern)):
            continue
        try:
            if file.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            text = load_doc(file).text
        except (OSError, ToolError):
            continue
        for number, line in enumerate(text.split("\n"), 1):
            if regex.search(line):
                matches.append(f"{rel}:{number}: {line.strip()[:300]}")
                if len(matches) >= MAX_GREP_MATCHES:
                    matches.append(f"... (결과가 많아 {MAX_GREP_MATCHES}개까지만 표시)")
                    return "\n".join(matches)
    return "\n".join(matches) if matches else "(일치하는 결과 없음)"


# ---------------------------------------------------------------- 쓰기 도구


def _plan_write(ctx: ToolContext, args: dict) -> tuple[Path, TextDoc, str, str]:
    path = resolve_in_root(ctx.root, _str_arg(args, "path"))
    check_writable(ctx.root, path)
    content = _str_arg(args, "content")
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError("내용이 너무 큽니다. 여러 파일로 나누세요.")
    doc = load_doc(path) if path.exists() else TextDoc()
    return path, doc, doc.text, content.replace("\r\n", "\n")


def _preview_write(ctx: ToolContext, args: dict) -> str:
    path, _, old, new = _plan_write(ctx, args)
    rel = relative_display(ctx.root, path)
    if not path.exists():
        lines = new.split("\n")
        if lines[-1] == "":
            lines.pop()
        shown = [f"+{line}" for line in lines[:MAX_PREVIEW_LINES]]
        if len(lines) > MAX_PREVIEW_LINES:
            shown.append(f"... ({len(lines) - MAX_PREVIEW_LINES}줄 더 있음)")
        return f"새 파일: {rel} ({len(lines)}줄)\n" + "\n".join(shown)
    return _diff(rel, old, new)


def _write_file(ctx: ToolContext, args: dict) -> str:
    path, doc, _, new = _plan_write(ctx, args)
    existed = path.exists()
    _write_doc(path, doc, new)
    return f"{'수정' if existed else '생성'} 완료: {relative_display(ctx.root, path)}"


def _plan_edit(ctx: ToolContext, args: dict) -> tuple[Path, TextDoc, str, str]:
    path = resolve_in_root(ctx.root, _str_arg(args, "path"))
    check_writable(ctx.root, path)
    if not path.exists():
        raise ToolError(f"존재하지 않는 파일입니다: {args['path']} (새 파일은 write_file 사용)")
    old = _str_arg(args, "old_string").replace("\r\n", "\n")
    new = _str_arg(args, "new_string").replace("\r\n", "\n")
    if not old:
        raise ToolError("old_string이 비어 있습니다.")
    if old == new:
        raise ToolError("old_string과 new_string이 같습니다.")
    doc = load_doc(path)
    count = doc.text.count(old)
    if count == 0:
        raise ToolError("old_string을 파일에서 찾지 못했습니다. read_file로 정확한 내용(공백, 들여쓰기)을 확인하세요.")
    if count > 1 and not _bool_arg(args, "replace_all"):
        raise ToolError(
            f"old_string이 {count}곳에서 발견됩니다. 주변 줄을 더 포함해 유일하게 만들거나 replace_all=true를 사용하세요."
        )
    updated = doc.text.replace(old, new) if count > 1 else doc.text.replace(old, new, 1)
    return path, doc, doc.text, updated


def _preview_edit(ctx: ToolContext, args: dict) -> str:
    path, _, old, updated = _plan_edit(ctx, args)
    return _diff(relative_display(ctx.root, path), old, updated)


def _edit_file(ctx: ToolContext, args: dict) -> str:
    path, doc, _, updated = _plan_edit(ctx, args)
    _write_doc(path, doc, updated)
    return f"수정 완료: {relative_display(ctx.root, path)}"


# ---------------------------------------------------------------- 명령 실행


def _command_timeout(args: dict) -> int:
    return _int_arg(args, "timeout", DEFAULT_COMMAND_TIMEOUT, 1, MAX_COMMAND_TIMEOUT)


def _preview_command(ctx: ToolContext, args: dict) -> str:
    command = _str_arg(args, "command")
    reason = check_command(command)
    if reason:
        raise SandboxError(reason)
    return f"$ {command}\n(작업 폴더: {ctx.root}, 제한시간 {_command_timeout(args)}초)"


def _run_command(ctx: ToolContext, args: dict) -> str:
    command = _str_arg(args, "command")
    reason = check_command(command)
    if reason:
        raise SandboxError(reason)
    result = pu.run_shell(command, str(ctx.root), timeout=_command_timeout(args))
    if result.timed_out:
        return f"[시간 초과로 중단됨]\n{result.output}"
    return f"[종료 코드 {result.exit_code}]\n{result.output}"


# ---------------------------------------------------------------- 등록


def build_tools() -> dict[str, Tool]:
    path_prop = {"type": "string", "description": "Path relative to the project root."}
    tools = [
        Tool(
            "list_dir",
            "List files and folders in a directory (skips .git, node_modules, .venv, __pycache__). Folders end with '/'.",
            {
                "type": "object",
                "properties": {
                    "path": {**path_prop, "description": "Directory path. Default '.'"},
                    "depth": {"type": "integer", "description": "Levels to show, 1-3. Default 1."},
                },
            },
            _list_dir,
        ),
        Tool(
            "read_file",
            "Read a text file with line numbers. Long files are paged with offset/limit.",
            {
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "offset": {"type": "integer", "description": "First line to read (1-based). Default 1."},
                    "limit": {"type": "integer", "description": f"Max lines. Default {DEFAULT_READ_LINES}."},
                },
                "required": ["path"],
            },
            _read_file,
        ),
        Tool(
            "grep",
            "Search file contents with a regular expression. Returns 'path:line: text'.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {**path_prop, "description": "File or directory to search. Default '.'"},
                    "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'."},
                    "ignore_case": {"type": "boolean"},
                },
                "required": ["pattern"],
            },
            _grep,
        ),
        Tool(
            "write_file",
            "Create a file or fully overwrite an existing one. Prefer edit_file for small changes. Requires user approval.",
            {
                "type": "object",
                "properties": {"path": path_prop, "content": {"type": "string", "description": "Full file content."}},
                "required": ["path", "content"],
            },
            _write_file,
            needs_approval=True,
            preview=_preview_write,
        ),
        Tool(
            "edit_file",
            "Replace exact text in an existing file. old_string must match exactly once (include surrounding lines to make it unique) unless replace_all is true. Requires user approval.",
            {
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "old_string": {"type": "string", "description": "Exact text to replace, including whitespace."},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            _edit_file,
            needs_approval=True,
            preview=_preview_edit,
        ),
        Tool(
            "run_command",
            f"Run a shell command in the project root and return its output. Non-interactive only (stdin is closed); long-running servers will be killed at the timeout. Requires user approval. Shell: {pu.shell_description()}",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": f"Seconds, max {MAX_COMMAND_TIMEOUT}. Default {DEFAULT_COMMAND_TIMEOUT}."},
                },
                "required": ["command"],
            },
            _run_command,
            needs_approval=True,
            preview=_preview_command,
        ),
    ]
    return {tool.name: tool for tool in tools}
