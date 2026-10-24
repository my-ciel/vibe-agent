import copy

from vibe_agent.cost import Usage
from vibe_agent.llm import Finished, TextChunk, ToolCall, ToolCallsReady, UsageInfo


def call(call_id: str, name: str, /, **args) -> ToolCall:
    import json

    return ToolCall(call_id, name, json.dumps(args))


class ScriptedLLM:
    """미리 정해 둔 응답을 차례로 내놓는 가짜 LLM."""

    def __init__(self, *turns: dict):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def stream(self, messages, tools, model):
        self.seen.append(copy.deepcopy(messages))
        turn = self.turns.pop(0)
        if turn.get("text"):
            yield TextChunk(turn["text"])
        if turn.get("calls"):
            yield ToolCallsReady(turn["calls"])
        yield UsageInfo(turn.get("usage", Usage(100, 20, 0.001)))
        yield Finished("tool_calls" if turn.get("calls") else "stop")
