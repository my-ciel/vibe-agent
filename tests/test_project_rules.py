from tests.fakes import ScriptedLLM, call
from vibe_agent.loop import Agent
from vibe_agent.project_rules import find_rule_files, load_project_rules


def test_loads_existing_rule_files_only(tmp_path):
    assert load_project_rules(tmp_path) == "" and find_rule_files(tmp_path) == []
    (tmp_path / "GEMINI.md").write_text("# 규칙\n- 한글로 작성", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("   ", encoding="utf-8")
    text = load_project_rules(tmp_path)
    assert "### GEMINI.md" in text and "한글로 작성" in text and "AGENTS" not in text


def test_long_rules_are_truncated(tmp_path):
    (tmp_path / "GEMINI.md").write_text("가" * 9000, encoding="utf-8")
    assert "이하 생략" in load_project_rules(tmp_path)


def test_agent_rereads_rules_after_the_agent_edits_them(tmp_path):
    agent = Agent(llm=ScriptedLLM(
        {"calls": [call("c1", "write_file", path="GEMINI.md", content="## 진행 상황\n- [x] 1단계")]},
        {"text": "완료"},
    ), model="m", root=tmp_path, auto_approve={"write_file"})
    assert "진행 상황" not in agent.messages[0]["content"]
    agent.submit("규칙 파일을 만들어줘")
    list(agent.run())
    assert "진행 상황" not in agent.llm.seen[0][0]["content"]       # 첫 호출 때는 파일이 없었다
    assert "[x] 1단계" in agent.llm.seen[1][0]["content"]            # 직접 만든 규칙을 다음 호출부터 본다
    assert "Project rules" in agent.messages[0]["content"]


def test_system_prompt_describes_venv_behaviour(tmp_path):
    system = Agent(llm=ScriptedLLM(), model="m", root=tmp_path).messages[0]["content"]
    assert "python -m pip" in system and "fresh shell" in system
