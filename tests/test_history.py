import json

import httpx
import openai

from tests.fakes import ScriptedLLM, call
from vibe_agent.cost import Usage
from vibe_agent.history import repair_history, safe_id
from vibe_agent.llm import Finished, LLMError, TextChunk, ToolCallsReady, UsageInfo, translate_error
from vibe_agent.loop import Agent, ErrorEvent, Notice, TextDelta


def asst(cid, args='{"path": "a"}', content=None):
    return {"role": "assistant", "content": content, "tool_calls": [{"id": cid, "type": "function", "function": {"name": "read_file", "arguments": args}}]}


def tool(cid, content="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


S, U = {"role": "system", "content": "s"}, {"role": "user", "content": "u"}


def test_safe_id():
    assert safe_id("call:1|abc/def") == "call_1_abc_def"
    assert len(safe_id("x" * 200)) == 64 and safe_id("") == "call_0"


def test_empty_assistant_messages_are_dropped():
    out = repair_history([S, U, {"role": "assistant", "content": ""}, {"role": "assistant", "content": None}, {"role": "user", "content": "계속"}])
    assert [m["role"] for m in out] == ["system", "user"] and out[1]["content"] == "u\n\n계속"


def test_ids_and_arguments_are_normalized_consistently():
    out = repair_history([S, U, asst("a:b|c", args="{broken"), tool("a:b|c", "")])
    call_msg, result = out[2], out[3]
    assert call_msg["tool_calls"][0]["id"] == "a_b_c" == result["tool_call_id"]
    assert call_msg["tool_calls"][0]["function"]["arguments"] == "{}" and result["content"] == "(출력 없음)"


def test_orphan_results_removed_and_missing_results_filled():
    out = repair_history([S, U, tool("ghost"), asst("c1"), U])
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[3]["tool_call_id"] == "c1" and "결과 없음" in out[3]["content"]


def test_original_history_is_not_modified():
    original = [S, U, {"role": "assistant", "content": ""}, {"role": "user", "content": "x"}]
    snapshot = json.dumps(original)
    repair_history(original, aggressive=True)
    assert json.dumps(original) == snapshot


def test_aggressive_trims_old_content_but_keeps_recent_and_images_of_last_user():
    big = "x" * 20000
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    msgs = [S, {"role": "user", "content": [{"type": "text", "text": "옛 이미지"}, img]}]
    msgs += [asst("old", args=json.dumps({"path": "a", "content": big})), tool("old", big)]
    for i in range(6):
        msgs += [asst(f"c{i}"), tool(f"c{i}", big if i == 5 else "ok")]
    msgs += [{"role": "user", "content": [{"type": "text", "text": "새 이미지"}, img]}]
    out = repair_history(msgs, aggressive=True)
    assert out[1]["content"][1]["type"] == "text" and "이미지" in out[1]["content"][1]["text"]  # 옛 이미지는 글로 대체
    assert out[-1]["content"][1]["type"] == "image_url"                                        # 마지막 사용자 이미지는 유지
    old_args = json.loads(out[2]["tool_calls"][0]["function"]["arguments"])
    assert len(old_args["content"]) < 2000 and len(out[3]["content"]) < 2000                   # 오래된 큰 내용은 줄임
    assert len(out[-2]["content"]) == 20000                                                    # 최근 도구 결과는 그대로


class Flaky:
    """처음 호출은 400, 두 번째부터 정상 응답."""

    def __init__(self, fail_times=1):
        self.fail_times, self.seen = fail_times, []

    def stream(self, messages, tools, model):
        self.seen.append(messages)
        if len(self.seen) <= self.fail_times:
            raise LLMError("요청이 거절되었습니다 (400): Provider returned error", status=400)
        yield TextChunk("복구됨")
        yield UsageInfo(Usage(1, 1, 0.0))
        yield Finished("stop")


def test_agent_retries_once_on_400_with_trimmed_history(tmp_path):
    agent = Agent(llm=Flaky(1), model="m", root=tmp_path)
    agent.submit("안녕")
    events = list(agent.run())
    assert any(isinstance(e, Notice) for e in events) and any(isinstance(e, TextDelta) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert len(agent.llm.seen) == 2 and agent.messages[-1]["content"] == "복구됨"


def test_agent_reports_error_if_retry_also_fails_and_keeps_history_clean(tmp_path):
    agent = Agent(llm=Flaky(5), model="m", root=tmp_path)
    agent.submit("안녕")
    events = list(agent.run())
    assert isinstance(events[-1], ErrorEvent) and "400" in events[-1].message
    assert len(agent.llm.seen) == 2                       # 재시도는 한 번만
    assert agent.messages[-1]["role"] == "user"           # 실패한 응답이 기록에 남지 않는다


def test_non_400_errors_are_not_retried(tmp_path):
    class Down:
        calls = 0

        def stream(self, messages, tools, model):
            Down.calls += 1
            raise LLMError("서버 오류", status=503)
            yield

    agent = Agent(llm=Down(), model="m", root=tmp_path)
    agent.submit("x")
    list(agent.run())
    assert Down.calls == 1


def test_empty_model_response_is_not_stored(tmp_path):
    class Silent:
        def stream(self, messages, tools, model):
            yield UsageInfo(Usage(5, 0, 0.0))
            yield Finished("stop")

    agent = Agent(llm=Silent(), model="m", root=tmp_path)
    agent.submit("x")
    events = list(agent.run())
    assert isinstance(events[-1], ErrorEvent) and "빈 응답" in events[-1].message
    assert [m["role"] for m in agent.messages] == ["system", "user"]


def test_provider_name_and_raw_error_are_shown():
    request = httpx.Request("POST", "https://x")
    body = {"message": "Provider returned error", "code": 400, "metadata": {"provider_name": "Google", "raw": "{\"error\": \"invalid tool schema\"}"}}
    exc = openai.BadRequestError("Error code: 400", response=httpx.Response(400, request=request), body=body)
    err = translate_error(exc)
    assert err.status == 400
    assert "제공자: Google" in str(err) and "invalid tool schema" in str(err) and "새 대화" in str(err)


def test_invalid_model_error_is_not_retried():
    request = httpx.Request("POST", "https://x")
    exc = openai.BadRequestError("x", response=httpx.Response(400, request=request), body={"message": "nope/x is not a valid model ID", "code": 400})
    assert translate_error(exc).status is None
