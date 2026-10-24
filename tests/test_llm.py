from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from vibe_agent.llm import (
    Finished, LLMError, OpenRouterClient, TextChunk, ToolCall, ToolCallsReady, UsageInfo,
    parse_stream, translate_error,
)


def chunk(content=None, tool_calls=None, finish=None, usage=None, error=None):
    choices = [NS(delta=NS(content=content, tool_calls=tool_calls), finish_reason=finish)]
    return NS(choices=choices, usage=usage, error=error)


def tc(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


def test_text_stream_and_usage():
    events = list(parse_stream([
        chunk("안"), chunk("녕"), chunk(finish="stop"),
        NS(choices=[], usage=NS(prompt_tokens=12, completion_tokens=3, cost=0.0004), error=None),
    ]))
    assert events[:2] == [TextChunk("안"), TextChunk("녕")]
    usage = next(e for e in events if isinstance(e, UsageInfo)).usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cost) == (12, 3, 0.0004)
    assert events[-1] == Finished("stop")


def test_tool_call_deltas_are_assembled():
    events = list(parse_stream([
        chunk(tool_calls=[tc(0, "c1", "read_file", '{"pa')]),
        chunk(tool_calls=[tc(0, None, None, 'th": "a.txt"}')]),
        chunk(tool_calls=[tc(1, "c2", "list_dir", "{}")]),
        chunk(finish="tool_calls"),
    ]))
    calls = next(e for e in events if isinstance(e, ToolCallsReady)).calls
    assert calls == [ToolCall("c1", "read_file", '{"path": "a.txt"}'), ToolCall("c2", "list_dir", "{}")]


def test_missing_tool_call_id_gets_generated():
    events = list(parse_stream([chunk(tool_calls=[tc(0, None, "list_dir", "{}")]), chunk(finish="tool_calls")]))
    calls = next(e for e in events if isinstance(e, ToolCallsReady)).calls
    assert calls[0].id.startswith("call_")


def test_usage_without_cost():
    events = list(parse_stream([NS(choices=[], usage=NS(prompt_tokens=1, completion_tokens=1), error=None)]))
    assert next(e for e in events if isinstance(e, UsageInfo)).usage.cost is None


def test_mid_stream_error():
    with pytest.raises(LLMError, match="boom"):
        list(parse_stream([chunk("a"), NS(choices=[], usage=None, error={"message": "boom"})]))


def status_error(code, message="x"):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(code, request=request)
    return openai.APIStatusError(message, response=response, body=None)


@pytest.mark.parametrize("code,word", [(402, "크레딧"), (429, "너무 많"), (503, "서버"), (404, "도구 호출")])
def test_status_errors_are_translated(code, word):
    assert word in str(translate_error(status_error(code)))


def test_auth_and_connection_errors():
    request = httpx.Request("POST", "https://x")
    assert "API 키" in str(translate_error(openai.AuthenticationError("x", response=httpx.Response(401, request=request), body=None)))
    assert "연결" in str(translate_error(openai.APIConnectionError(request=request)))


def test_client_translates_sdk_errors():
    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise status_error(402)

    client = OpenRouterClient("k", client=Boom())
    with pytest.raises(LLMError, match="크레딧"):
        list(client.stream([], [], "m"))


def test_client_sends_expected_request():
    seen = {}

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen.update(kw)
                    return iter([chunk("hi", finish="stop")])

    events = list(OpenRouterClient("k", client=Fake()).stream([{"role": "user", "content": "x"}], [{"t": 1}], "a/b"))
    assert seen["model"] == "a/b" and seen["stream"] is True and seen["tools"] == [{"t": 1}]
    assert seen["extra_body"] == {"usage": {"include": True}}
    assert events[0] == TextChunk("hi")


def test_error_detail_uses_body_message_only():
    request = httpx.Request("POST", "https://x")
    inner = {"message": "nope/x is not a valid model ID", "code": 400}
    for body in (inner, {"error": inner, "user_id": "secret-id"}):  # SDK 형태 / 전체 본문 형태
        exc = openai.BadRequestError("Error code: 400 - {...}", response=httpx.Response(400, request=request), body=body)
        message = str(translate_error(exc))
        assert "모델 ID가 올바르지 않습니다" in message and "not a valid model ID" in message
        assert "secret-id" not in message and "Error code" not in message
