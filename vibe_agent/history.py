"""모델에 보내기 전에 대화 기록을 점검하고 고친다 (저장된 기록은 바꾸지 않고 복사본만 고친다).

한 번 잘못된 내용이 기록에 들어가면 이후 모든 요청이 같은 이유로 거절되므로,
제공자들이 공통으로 싫어하는 형태를 미리 정리한다.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

KEEP_RECENT = 8  # 공격적으로 줄일 때도 최근 메시지는 그대로 둔다
OLD_TOOL_OUTPUT_LIMIT = 1500
OLD_ARG_STRING_LIMIT = 800
_BAD_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def safe_id(call_id: str) -> str:
    """도구 호출 id는 영문/숫자/_/- 만, 64자 이하여야 하는 제공자가 있다."""
    cleaned = _BAD_ID_CHARS.sub("_", call_id or "")[:64]
    return cleaned or "call_0"


def _valid_json_object(raw: str) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except (TypeError, ValueError):
        return False


def _as_parts(content: Any) -> list[dict]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = limit * 3 // 5, limit * 2 // 5
    return f"{text[:head]}\n... [이전 내용 {len(text) - head - tail}자 생략] ...\n{text[-tail:]}"


def _shrink_arguments(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return "{}"
    if isinstance(data, dict):
        data = {k: (_truncate(v, OLD_ARG_STRING_LIMIT) if isinstance(v, str) else v) for k, v in data.items()}
    return json.dumps(data, ensure_ascii=False)


def repair_history(messages: list[dict], aggressive: bool = False) -> list[dict]:
    msgs = copy.deepcopy(messages)

    # 1) 내용이 전혀 없는 assistant 메시지 제거
    msgs = [m for m in msgs if not (m["role"] == "assistant" and not m.get("content") and not m.get("tool_calls"))]

    # 2) 도구 호출 id/인자 정리 (assistant 호출과 tool 결과에 같은 변환을 적용)
    for m in msgs:
        if m["role"] == "assistant":
            for call in m.get("tool_calls") or []:
                call["id"] = safe_id(call["id"])
                if not _valid_json_object(call["function"].get("arguments", "")):
                    call["function"]["arguments"] = "{}"
        elif m["role"] == "tool":
            m["tool_call_id"] = safe_id(m["tool_call_id"])
            if not m.get("content"):
                m["content"] = "(출력 없음)"

    # 3) 짝이 안 맞는 도구 결과 정리: 고아 결과 제거, 결과가 없는 호출에는 자리표시 결과 추가
    fixed: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m["role"] == "tool":  # 앞선 assistant 호출에 속하지 않는 고아
            i += 1
            continue
        fixed.append(m)
        i += 1
        if m["role"] == "assistant" and m.get("tool_calls"):
            wanted = {c["id"] for c in m["tool_calls"]}
            seen: set[str] = set()
            while i < len(msgs) and msgs[i]["role"] == "tool":
                if msgs[i]["tool_call_id"] in wanted and msgs[i]["tool_call_id"] not in seen:
                    seen.add(msgs[i]["tool_call_id"])
                    fixed.append(msgs[i])
                i += 1
            for missing in [c["id"] for c in m["tool_calls"] if c["id"] not in seen]:
                fixed.append({"role": "tool", "tool_call_id": missing, "content": "(결과 없음: 실행이 중단되었어요)"})
    msgs = fixed

    # 4) 연속된 user 메시지 합치기
    merged: list[dict] = []
    for m in msgs:
        if merged and m["role"] == "user" and merged[-1]["role"] == "user":
            prev = merged[-1]
            if isinstance(prev["content"], str) and isinstance(m["content"], str):
                prev["content"] = prev["content"] + "\n\n" + m["content"]
            else:
                prev["content"] = _as_parts(prev["content"]) + _as_parts(m["content"])
        else:
            merged.append(m)
    msgs = merged

    if aggressive:
        old = msgs[: max(len(msgs) - KEEP_RECENT, 1)]
        last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
        for idx, m in enumerate(msgs):
            if m["role"] == "user" and isinstance(m["content"], list) and idx != last_user:
                m["content"] = [
                    {"type": "text", "text": "[이전 이미지는 생략됨]"} if p.get("type") == "image_url" else p
                    for p in m["content"]
                ]
        for m in old:
            if m["role"] == "tool":
                m["content"] = _truncate(m["content"], OLD_TOOL_OUTPUT_LIMIT)
            elif m["role"] == "assistant":
                for call in m.get("tool_calls") or []:
                    call["function"]["arguments"] = _shrink_arguments(call["function"]["arguments"])
    return msgs
