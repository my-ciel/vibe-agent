"""agent.messages를 화면에 그리기 좋은 구조로 바꾼다 (Streamlit에 의존하지 않음)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .attachments import AttachmentView, parse_user_content
from .loop import DENIED_MESSAGE

TOOL_ICONS = {
    "list_dir": "📁", "read_file": "📄", "grep": "🔍",
    "write_file": "✏️", "edit_file": "✏️", "run_command": "⚙️",
    "start_process": "▶️", "read_process_output": "📜", "stop_process": "⏹️", "list_processes": "📋",
}
TOOL_LABELS = {
    "list_dir": "폴더 보기", "read_file": "파일 읽기", "grep": "검색",
    "write_file": "파일 쓰기", "edit_file": "파일 수정", "run_command": "명령 실행",
    "start_process": "백그라운드 실행", "read_process_output": "실행 로그 보기", "stop_process": "실행 중지", "list_processes": "실행 목록",
}


@dataclass
class ToolEntry:
    id: str
    name: str
    args: dict[str, Any]
    output: str | None = None  # None이면 아직 결과 없음(승인 대기 등)

    @property
    def status(self) -> str:
        if self.output is None:
            return "pending"
        if self.output == DENIED_MESSAGE:
            return "denied"
        return "error" if self.output.startswith("오류:") else "ok"


@dataclass
class Block:
    text: str = ""
    tool: ToolEntry | None = None
    attachment: AttachmentView | None = None


@dataclass
class Turn:
    role: str  # user | assistant
    blocks: list[Block] = field(default_factory=list)


def tool_title(entry: ToolEntry) -> str:
    icon = TOOL_ICONS.get(entry.name, "🔧")
    label = TOOL_LABELS.get(entry.name, entry.name)
    args = entry.args
    detail = ""
    if entry.name in ("run_command", "start_process"):
        detail = str(args.get("command", ""))
    elif "id" in args:
        detail = str(args.get("id", ""))
    elif entry.name == "grep":
        detail = str(args.get("pattern", ""))
    elif "path" in args:
        detail = str(args.get("path", ""))
    elif entry.name == "list_dir":
        detail = "."
    detail = detail.replace("\n", " ")
    if len(detail) > 70:
        detail = detail[:67] + "..."
    mark = {"ok": "✅", "error": "❌", "denied": "🚫", "pending": "⏳"}[entry.status]
    return f"{mark} {icon} {label}" + (f" · {detail}" if detail else "")


def build_turns(messages: list[dict]) -> list[Turn]:
    """연속된 assistant/tool 메시지는 하나의 assistant 턴으로 묶는다."""
    results = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    turns: list[Turn] = []
    for message in messages:
        role = message["role"]
        if role == "user":
            text, attachments = parse_user_content(message["content"])
            blocks = [Block(text=text)] if text else []
            blocks += [Block(attachment=a) for a in attachments]
            turns.append(Turn("user", blocks))
        elif role == "assistant":
            if not turns or turns[-1].role != "assistant":
                turns.append(Turn("assistant"))
            if message.get("content"):
                turns[-1].blocks.append(Block(text=message["content"]))
            for call in message.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                entry = ToolEntry(
                    call["id"],
                    call["function"]["name"],
                    args if isinstance(args, dict) else {},
                    results.get(call["id"]),
                )
                turns[-1].blocks.append(Block(tool=entry))
    return turns
