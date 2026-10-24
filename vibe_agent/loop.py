"""재개 가능한 에이전트 루프.

run()은 승인이 필요한 도구를 만나면 ApprovalRequest를 내보내고 멈춘다.
decide()로 결정을 기록한 뒤 run()을 다시 호출하면 이어서 진행한다.
상태는 모두 messages/decisions에 있으므로 Streamlit처럼 매 상호작용마다
스크립트가 다시 실행되는 UI에서도 session_state에 Agent만 보관하면 된다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .cost import CostTracker, Usage
from .errors import ToolError
from .llm import (
    Finished,
    LLMClient,
    LLMError,
    TextChunk,
    ToolCall,
    ToolCallsReady,
    UsageInfo,
)
from .platform_utils import truncate_middle
from .project_rules import load_project_rules
from .prompts import build_system_prompt
from .tools import Tool, ToolContext, build_tools

MAX_TOOL_OUTPUT = 30_000
DENIED_MESSAGE = "사용자가 이 작업을 거절했습니다. 같은 작업을 반복하지 말고, 무엇을 원하는지 물어보세요."


# ---------------------------------------------------------------- 이벤트


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolStart:
    call: ToolCall
    args: dict[str, Any]


@dataclass
class ApprovalRequest:
    call: ToolCall
    args: dict[str, Any]
    preview: str


@dataclass
class ToolResult:
    call: ToolCall
    args: dict[str, Any]
    output: str
    ok: bool = True
    denied: bool = False


@dataclass
class UsageUpdate:
    usage: Usage
    tracker: CostTracker


@dataclass
class ErrorEvent:
    message: str


@dataclass
class Done:
    reason: str = "complete"  # complete | max_steps | budget
    message: str = ""


Event = TextDelta | ToolStart | ApprovalRequest | ToolResult | UsageUpdate | ErrorEvent | Done


# ---------------------------------------------------------------- 에이전트


@dataclass
class Agent:
    llm: LLMClient
    model: str
    root: Path
    tracker: CostTracker = field(default_factory=CostTracker)
    max_steps: int = 20  # 사용자 메시지 1개당 LLM 호출 상한
    auto_approve: set[str] = field(default_factory=set)  # 승인 없이 실행할 도구 이름
    instructions: str = ""
    tools: dict[str, Tool] = field(default_factory=build_tools)
    messages: list[dict] = field(default_factory=list)
    decisions: dict[str, bool] = field(default_factory=dict)
    previews: dict[str, str] = field(default_factory=dict)  # call_id -> 승인 때 보여준 미리보기
    steps: int = 0

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        if not self.messages:
            self.messages.append({"role": "system", "content": ""})
        self._refresh_system_prompt()

    def _refresh_system_prompt(self) -> None:
        """프로젝트 지침 파일(GEMINI.md 등)은 에이전트가 직접 고치기도 하므로 LLM 호출 전에 다시 읽는다."""
        self.messages[0]["content"] = build_system_prompt(
            self.root, self.instructions, load_project_rules(self.root)
        )

    # -- 입력

    def submit(self, content: str | list[dict[str, Any]]) -> None:
        self.messages.append({"role": "user", "content": content})
        self.steps = 0

    def decide(self, call_id: str, approved: bool) -> None:
        self.decisions[call_id] = approved

    # -- 실행

    def run(self) -> Iterator[Event]:
        schemas = [t.schema() for t in self.tools.values()]
        while True:
            for call in self._pending_calls():
                if not (yield from self._handle_call(call)):
                    return  # 승인 대기
            if self._finished():
                yield Done("complete")
                return
            reason = self.tracker.check()
            if reason:
                yield Done("budget", reason)
                return
            if self.steps >= self.max_steps:
                yield Done(
                    "max_steps",
                    f"한 번의 요청에서 실행할 수 있는 최대 단계({self.max_steps})에 도달했습니다. "
                    "'계속해줘'라고 입력하면 이어서 진행합니다.",
                )
                return
            self.steps += 1
            self._refresh_system_prompt()
            try:
                yield from self._call_llm(schemas)
            except LLMError as exc:
                yield ErrorEvent(str(exc))
                return

    def _call_llm(self, schemas: list[dict]) -> Iterator[Event]:
        text = ""
        calls: list[ToolCall] = []
        for event in self.llm.stream(self.messages, schemas, self.model):
            if isinstance(event, TextChunk):
                text += event.text
                yield TextDelta(event.text)
            elif isinstance(event, ToolCallsReady):
                calls = event.calls
            elif isinstance(event, UsageInfo):
                self.tracker.add(event.usage)
                yield UsageUpdate(event.usage, self.tracker)
            elif isinstance(event, Finished) and event.reason == "length":
                yield ErrorEvent("모델의 응답 길이 제한에 걸려 답변이 중간에 끊겼습니다.")
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ]
        elif not text:
            message["content"] = ""
        self.messages.append(message)

    # -- 도구 호출 처리

    def _handle_call(self, call: ToolCall) -> Iterator[Event]:
        """도구 호출 하나를 처리한다. 승인 대기로 멈추면 False를 반환한다."""
        args: dict[str, Any] = {}
        tool = self.tools.get(call.name)
        try:
            if tool is None:
                raise ToolError(f"알 수 없는 도구입니다: {call.name}")
            args = self._parse_args(call.arguments)

            if tool.needs_approval and call.name not in self.auto_approve:
                decision = self.decisions.get(call.id)
                if decision is None:
                    preview = tool.preview(ToolContext(self.root), args) if tool.preview else ""
                    self.previews[call.id] = preview
                    yield ApprovalRequest(call, args, preview)
                    return False
                if not decision:
                    self._finish_call(call, DENIED_MESSAGE)
                    yield ToolResult(call, args, DENIED_MESSAGE, ok=False, denied=True)
                    return True

            yield ToolStart(call, args)
            output = tool.run(ToolContext(self.root), args)
            ok = True
        except ToolError as exc:
            output, ok = f"오류: {exc}", False
        except Exception as exc:  # 도구 버그가 대화 전체를 죽이지 않도록 모델에게 돌려준다
            output, ok = f"오류: 예상하지 못한 문제 ({type(exc).__name__}: {exc})", False
        self._finish_call(call, output)
        yield ToolResult(call, args, output, ok=ok)
        return True

    @staticmethod
    def _parse_args(raw: str) -> dict[str, Any]:
        try:
            args = json.loads(raw) if raw and raw.strip() else {}
        except json.JSONDecodeError:
            raise ToolError("도구 인자가 올바른 JSON이 아닙니다. 다시 호출하세요.") from None
        if not isinstance(args, dict):
            raise ToolError("도구 인자는 JSON 객체여야 합니다.")
        return args

    def _finish_call(self, call: ToolCall, output: str) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": truncate_middle(output, MAX_TOOL_OUTPUT),
            }
        )

    # -- 상태 조회

    def _pending_calls(self) -> list[ToolCall]:
        for i in range(len(self.messages) - 1, -1, -1):
            message = self.messages[i]
            if message["role"] == "user":
                return []
            if message["role"] == "assistant":
                answered = {m["tool_call_id"] for m in self.messages[i + 1 :] if m["role"] == "tool"}
                return [
                    ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"])
                    for c in message.get("tool_calls") or []
                    if c["id"] not in answered
                ]
        return []

    def _finished(self) -> bool:
        last = self.messages[-1]
        return last["role"] == "assistant" and not last.get("tool_calls")
