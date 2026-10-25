from tests.fakes import ScriptedLLM, call
from vibe_agent.cost import CostTracker, Usage
from vibe_agent.llm import LLMError
from vibe_agent.loop import Agent, ApprovalRequest, Done, ErrorEvent, TextDelta, ToolResult


def make(tmp_path, *turns, **kw):
    return Agent(llm=ScriptedLLM(*turns), model="m", root=tmp_path, **kw)


def run_all(agent):
    return list(agent.run())


def kinds(events):
    return [type(e).__name__ for e in events]


def test_plain_answer(tmp_path):
    agent = make(tmp_path, {"text": "안녕하세요"})
    agent.submit("hi")
    events = run_all(agent)
    assert kinds(events) == ["TextDelta", "UsageUpdate", "Done"]
    assert agent.messages[-1] == {"role": "assistant", "content": "안녕하세요"}


def test_read_tool_runs_without_approval(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    agent = make(tmp_path, {"calls": [call("c1", "read_file", path="a.txt")]}, {"text": "done"})
    agent.submit("read it")
    events = run_all(agent)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.ok and "hello" in result.output
    assert isinstance(events[-1], Done) and events[-1].reason == "complete"
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"


def test_write_pauses_for_approval_then_resumes(tmp_path):
    agent = make(
        tmp_path,
        {"calls": [call("c1", "write_file", path="a.txt", content="hi")]},
        {"text": "created"},
    )
    agent.submit("make file")
    events = run_all(agent)
    req = events[-1]
    assert isinstance(req, ApprovalRequest) and "hi" in req.preview
    assert not (tmp_path / "a.txt").exists()  # 승인 전에는 쓰지 않는다

    # 승인 대기 중 다시 run()해도 같은 요청만 반복되고 LLM은 호출되지 않는다
    assert isinstance(run_all(agent)[-1], ApprovalRequest)
    assert len(agent.llm.seen) == 1

    agent.decide("c1", True)
    events = run_all(agent)
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert any(isinstance(e, TextDelta) and e.text == "created" for e in events)
    assert isinstance(events[-1], Done)


def test_denied_call_tells_model(tmp_path):
    agent = make(
        tmp_path,
        {"calls": [call("c1", "write_file", path="a.txt", content="hi")]},
        {"text": "알겠습니다"},
    )
    agent.submit("x")
    run_all(agent)
    agent.decide("c1", False)
    events = run_all(agent)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.denied and not result.ok
    assert not (tmp_path / "a.txt").exists()
    assert "거절" in agent.llm.seen[1][-1]["content"]


def test_auto_approve(tmp_path):
    agent = make(
        tmp_path,
        {"calls": [call("c1", "write_file", path="a.txt", content="hi")]},
        {"text": "ok"},
        auto_approve={"write_file"},
    )
    agent.submit("x")
    events = run_all(agent)
    assert not any(isinstance(e, ApprovalRequest) for e in events)
    assert (tmp_path / "a.txt").exists()


def test_multiple_calls_one_needs_approval(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    agent = make(
        tmp_path,
        {"calls": [call("c1", "read_file", path="a.txt"), call("c2", "write_file", path="b.txt", content="B"),
                   call("c3", "list_dir")]},
        {"text": "ok"},
    )
    agent.submit("x")
    events = run_all(agent)
    assert isinstance(events[-1], ApprovalRequest) and events[-1].call.id == "c2"
    agent.decide("c2", True)
    run_all(agent)
    tool_ids = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2", "c3"]  # 모든 호출에 응답해야 API가 거절하지 않는다


def test_tool_errors_go_back_to_model(tmp_path):
    agent = make(
        tmp_path,
        {"calls": [call("c1", "read_file", path="../secret"), call("c2", "nope"), call("c3", "read_file", path="x")]},
        {"text": "ok"},
    )
    agent.submit("x")
    events = run_all(agent)
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.ok for r in results] == [False, False, False]
    assert "작업 폴더 밖" in results[0].output and "알 수 없는 도구" in results[1].output


def test_invalid_json_arguments(tmp_path):
    from vibe_agent.llm import ToolCall

    agent = make(tmp_path, {"calls": [ToolCall("c1", "read_file", "{not json")]}, {"text": "ok"})
    agent.submit("x")
    result = next(e for e in run_all(agent) if isinstance(e, ToolResult))
    assert not result.ok and "JSON" in result.output


def test_invalid_args_rejected_before_approval(tmp_path):
    agent = make(tmp_path, {"calls": [call("c1", "run_command", command="sudo ls")]}, {"text": "ok"})
    agent.submit("x")
    events = run_all(agent)
    assert not any(isinstance(e, ApprovalRequest) for e in events)
    assert "sudo" in next(e for e in events if isinstance(e, ToolResult)).output


def test_max_steps_stops_loop(tmp_path):
    turns = [{"calls": [call(f"c{i}", "list_dir")]} for i in range(10)]
    agent = make(tmp_path, *turns, max_steps=3)
    agent.submit("loop")
    events = run_all(agent)
    assert events[-1].reason == "max_steps"
    assert len(agent.llm.seen) == 3
    # 새 사용자 메시지로 이어가면 단계 수가 초기화된다
    agent.submit("계속해줘")
    agent.llm.turns = [{"text": "끝"}]
    assert run_all(agent)[-1].reason == "complete"


def test_budget_stops_and_reports(tmp_path):
    turns = [{"calls": [call(f"c{i}", "list_dir")], "usage": Usage(10, 5, 0.6)} for i in range(5)]
    agent = make(tmp_path, *turns, tracker=CostTracker(limit_usd=1.0))
    agent.submit("x")
    events = run_all(agent)
    assert events[-1].reason == "budget"
    assert len(agent.llm.seen) == 2
    assert abs(agent.tracker.cost_usd - 1.2) < 1e-9
    # 도구 호출에는 모두 응답이 달려 있어 메시지 기록이 유효하다
    assert agent.messages[-1]["role"] == "tool"


def test_usage_without_cost_is_counted(tmp_path):
    agent = make(tmp_path, {"text": "a", "usage": Usage(10, 5, None)})
    agent.submit("x")
    run_all(agent)
    assert agent.tracker.unpriced_calls == 1 and agent.tracker.total_tokens == 15


def test_llm_error_is_reported_and_history_stays_valid(tmp_path):
    class Failing:
        def stream(self, messages, tools, model):
            raise LLMError("API 키가 올바르지 않습니다")
            yield

    agent = Agent(llm=Failing(), model="m", root=tmp_path)
    agent.submit("x")
    events = run_all(agent)
    assert isinstance(events[-1], ErrorEvent)
    assert agent.messages[-1]["role"] == "user"
    # 같은 대화를 그대로 다시 시도할 수 있다
    agent.llm = ScriptedLLM({"text": "ok"})
    assert run_all(agent)[-1].reason == "complete"


def test_run_without_new_input_does_not_call_llm(tmp_path):
    agent = make(tmp_path, {"text": "a"})
    agent.submit("x")
    run_all(agent)
    assert run_all(agent)[-1].reason == "complete"
    assert len(agent.llm.seen) == 1


def test_system_prompt_mentions_root_and_os(tmp_path):
    agent = make(tmp_path)
    system = agent.messages[0]["content"]
    assert str(tmp_path.resolve()) in system and "Shell for run_command" in system
