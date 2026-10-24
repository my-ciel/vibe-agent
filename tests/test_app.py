from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.fakes import ScriptedLLM, call
from vibe_agent import models, settings

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings" / "s.json")
    monkeypatch.setattr(models, "fetch_catalog", lambda *a, **k: {})


def make_app(tmp_path, *turns):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    at = AppTest.from_file(APP, default_timeout=15)
    llm = ScriptedLLM(*turns)
    at.session_state["llm_factory"] = lambda key: llm
    at.session_state["api_key"] = "sk-or-test"
    at.session_state["root_text"] = str(project)
    return at, project, llm


def texts(at):
    return " ".join(m.value for m in at.markdown)


def test_welcome_when_not_configured():
    at = AppTest.from_file(APP, default_timeout=15).run()
    assert not at.exception
    assert "시작하기" in texts(at)
    assert at.chat_input[0].disabled


def test_invalid_folder_is_reported(tmp_path):
    at, _, _ = make_app(tmp_path)
    at.session_state["root_text"] = str(tmp_path / "missing")
    at.run()
    assert not at.exception
    assert any("폴더를 찾을 수 없어요" in e.value for e in at.sidebar.error)


def test_chat_with_approval_flow(tmp_path):
    at, project, llm = make_app(
        tmp_path,
        {"calls": [call("c1", "write_file", path="hello.txt", content="hi\n")], "text": "파일을 만들게요."},
        {"text": "hello.txt를 만들었어요."},
    )
    at.run()
    assert not at.exception and not at.chat_input[0].disabled

    at.chat_input[0].set_value("파일 만들어줘").run()
    assert not at.exception
    assert not (project / "hello.txt").exists()          # 승인 전에는 쓰지 않는다
    assert at.chat_input[0].disabled                      # 승인 대기 중에는 입력 잠금
    assert any("승인이 필요해요" in w.value for w in at.warning)
    assert any("+hi" in c.value for c in at.code)         # 새 파일 미리보기

    at.button(key="ok-c1").click().run()
    assert not at.exception
    assert (project / "hello.txt").read_text() == "hi\n"
    assert "hello.txt를 만들었어요." in texts(at)
    assert not at.chat_input[0].disabled
    assert len(llm.seen) == 2


def test_reject_keeps_file_uncreated(tmp_path):
    at, project, _ = make_app(
        tmp_path,
        {"calls": [call("c1", "write_file", path="x.txt", content="x")]},
        {"text": "알겠어요, 만들지 않을게요."},
    )
    at.run()
    at.chat_input[0].set_value("x").run()
    at.button(key="no-c1").click().run()
    assert not at.exception
    assert not (project / "x.txt").exists()
    assert "만들지 않을게요" in texts(at)
    assert any("🚫" in e.label for e in at.expander)


def test_read_only_tools_run_without_prompt_and_show_cost(tmp_path):
    at, project, _ = make_app(
        tmp_path,
        {"calls": [call("c1", "list_dir")], "text": "확인할게요"},
        {"text": "비어 있어요."},
    )
    (project / "a.py").write_text("x")
    at.run()
    at.chat_input[0].set_value("뭐가 있어?").run()
    assert not at.exception and not at.warning
    assert any("폴더 보기" in e.label for e in at.expander)
    assert any("$0.0020" in m.value for m in at.sidebar.metric)


def test_auto_approve_edits_toggle(tmp_path):
    at, project, _ = make_app(
        tmp_path,
        {"calls": [call("c1", "write_file", path="a.txt", content="A")]},
        {"text": "끝"},
    )
    at.run()
    at.sidebar.toggle[0].set_value(True).run()
    at.chat_input[0].set_value("만들어").run()
    assert not at.exception
    assert (project / "a.txt").read_text() == "A"
    assert not any("승인이 필요해요" in w.value for w in at.warning)


def test_budget_limit_message(tmp_path):
    from vibe_agent.cost import Usage

    at, _, _ = make_app(
        tmp_path,
        {"calls": [call("c1", "list_dir")], "usage": Usage(1, 1, 2.0)},
    )
    at.run()
    at.chat_input[0].set_value("x").run()
    assert any("비용 한도" in w.value for w in at.warning)


def test_new_chat_resets(tmp_path):
    at, _, _ = make_app(tmp_path, {"text": "안녕"})
    at.run()
    at.chat_input[0].set_value("hi").run()
    assert "안녕" in texts(at)
    next(b for b in at.sidebar.button if "새 대화" in b.label).click().run()
    assert "안녕" not in texts(at)
