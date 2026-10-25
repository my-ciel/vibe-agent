from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.fakes import ScriptedLLM, call
from vibe_agent import models, sessions, settings

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    import streamlit as st

    from vibe_agent import processes

    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings" / "s.json")
    monkeypatch.setattr(sessions, "SESSIONS_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(processes, "REGISTRY_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(models, "fetch_catalog", lambda *a, **k: {})
    st.cache_resource.clear()  # 앱 전체가 공유하는 프로세스 관리자를 테스트마다 새로 만든다
    yield
    st.cache_resource.clear()


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
    next(t for t in at.sidebar.toggle if "묻지 않고" in t.label).set_value(True).run()
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
    assert "안녕" not in texts(at)  # 새 대화는 비어 있고, 이전 대화는 목록에 남는다
    assert any("hi" in b.label for b in at.sidebar.button)


def test_model_browser_selects_from_catalog(tmp_path, monkeypatch):
    cat = {
        "x/cheap": models.ModelInfo("x/cheap", 0.1, 0.2, True, True, 5),
        "x/pricey": models.ModelInfo("x/pricey", 5, 25, True, False, 4),
    }
    monkeypatch.setattr(models, "fetch_catalog", lambda *a, **k: cat)
    at, project, llm = make_app(tmp_path, {"text": "ok"})
    at.run()
    at.sidebar.selectbox[0].set_value("__browse__").run()
    assert not at.exception
    browse = at.sidebar.selectbox[1]
    assert browse.options == ["x/cheap  ·  $0.10 / $0.20  ·  🖼️", "x/pricey  ·  $5.00 / $25.00"]
    browse.set_value("x/pricey").run()
    assert any("비싼 모델" in w.value for w in at.sidebar.warning)
    assert at.main.caption[0].value.endswith("x/pricey")


def test_project_rules_file_is_announced_in_header(tmp_path):
    at, project, _ = make_app(tmp_path, {"text": "ok"})
    (project / "GEMINI.md").write_text("# 규칙", encoding="utf-8")
    at.run()
    assert "프로젝트 지침: GEMINI.md" in at.main.caption[0].value


def test_running_process_panel_survives_new_chat_and_can_be_stopped(tmp_path):
    import sys
    import time as _t

    at, project, _ = make_app(tmp_path, {"text": "ok"})
    at.run()
    agent = at.session_state["agent"]
    proc = agent.processes.start(f'"{sys.executable}" -c "import time; print(\'http://localhost:7777\', flush=True); time.sleep(60)"', str(project), "웹")
    _t.sleep(1.0)
    at.run()
    assert any("웹" in m.value for m in at.sidebar.markdown)
    next(b for b in at.sidebar.button if "새 대화" in b.label).click().run()
    assert proc.running and any("웹" in m.value for m in at.sidebar.markdown)  # 대화를 바꿔도 서버는 유지된다
    next(b for b in at.sidebar.button if b.label == "⏹️ 중지").click().run()
    assert not proc.running


# ---------------------------------------------------------------- 대화 저장/전환

def session_buttons(at):
    return [b for b in at.sidebar.button if b.key and b.key.startswith("sess-")]


def test_conversation_and_cost_survive_a_browser_reload(tmp_path):
    at, _, _ = make_app(tmp_path, {"text": "첫 답변"})
    at.run()
    at.chat_input[0].set_value("처음 질문").run()
    assert "첫 답변" in texts(at)

    reloaded, _, _ = make_app(tmp_path)  # 새로고침 = 새 브라우저 세션
    reloaded.run()
    assert not reloaded.exception
    assert "처음 질문" in texts(reloaded) and "첫 답변" in texts(reloaded)
    assert any("$0.0010" in m.value for m in reloaded.sidebar.metric)  # 비용도 이어진다
    assert len(session_buttons(reloaded)) == 1


def test_pending_approval_is_restored_after_reload(tmp_path):
    at, project, _ = make_app(tmp_path, {"calls": [call("c1", "write_file", path="a.txt", content="x")]})
    at.run()
    at.chat_input[0].set_value("파일 만들어줘").run()
    assert any("승인이 필요해요" in w.value for w in at.warning)

    reloaded, _, _ = make_app(tmp_path, {"text": "완료했어요"})
    reloaded.run()
    assert any("승인이 필요해요" in w.value for w in reloaded.warning)
    assert reloaded.chat_input[0].disabled and not (project / "a.txt").exists()
    reloaded.button(key="ok-c1").click().run()
    assert (project / "a.txt").read_text() == "x" and "완료했어요" in texts(reloaded)


def test_multiple_conversations_switch_and_delete(tmp_path):
    at, project, _ = make_app(tmp_path, {"text": "답1"}, {"text": "답2"})
    at.run()
    at.chat_input[0].set_value("질문1").run()
    next(b for b in at.sidebar.button if "새 대화" in b.label).click().run()
    assert "질문1" not in texts(at)
    at.chat_input[0].set_value("질문2").run()

    buttons = session_buttons(at)
    assert [b.label for b in buttons] == ["● 질문2", "질문1"]            # 최근 순, 현재 대화 표시
    assert buttons[0].disabled and not buttons[1].disabled

    buttons[1].click().run()                                            # 첫 대화로 전환
    assert "질문1" in texts(at) and "답1" in texts(at) and "질문2" not in texts(at)

    delete_current = next(b for b in at.sidebar.button if b.key == f"del-{at.session_state['session_id']}")
    delete_current.click().run()                                        # 현재 대화를 지우면 빈 새 대화
    assert "질문1" not in texts(at)
    assert [b.label for b in session_buttons(at)] == ["질문2"]


def test_each_folder_has_its_own_conversations(tmp_path):
    at, project, _ = make_app(tmp_path, {"text": "A폴더 답"})
    other = tmp_path / "other"
    other.mkdir()
    at.run()
    at.chat_input[0].set_value("A폴더 질문").run()

    at.session_state["root_text"] = str(other)
    at.run()
    assert "A폴더 질문" not in texts(at) and session_buttons(at) == []   # 다른 폴더는 비어 있다
    at.session_state["root_text"] = str(project)
    at.run()
    assert "A폴더 질문" in texts(at)                                     # 돌아오면 이어진다


def test_changing_only_the_key_keeps_the_conversation(tmp_path):
    at, _, _ = make_app(tmp_path, {"text": "계속 이어지는 답"})
    at.run()
    at.chat_input[0].set_value("질문").run()
    at.text_input(key="api_key").set_value("sk-or-new-key").run()
    assert "계속 이어지는 답" in texts(at)


def test_turning_saving_off_leaves_no_files(tmp_path):
    at, _, _ = make_app(tmp_path, {"text": "ok"})
    at.run()
    next(t for t in at.sidebar.toggle if "대화 기록 저장" in t.label).set_value(False).run()
    at.chat_input[0].set_value("저장하지 마").run()
    assert "ok" in texts(at)
    assert not (tmp_path / "sessions").exists() or not any((tmp_path / "sessions").rglob("*.json"))


def test_delete_all_removes_every_conversation_in_the_folder(tmp_path):
    at, _, _ = make_app(tmp_path, {"text": "a"}, {"text": "b"})
    at.run()
    at.chat_input[0].set_value("첫째").run()
    next(b for b in at.sidebar.button if "새 대화" in b.label).click().run()
    at.chat_input[0].set_value("둘째").run()
    assert len(session_buttons(at)) == 2
    next(b for b in at.sidebar.button if b.key == "del-all").click().run()
    assert session_buttons(at) == [] and not any((tmp_path / "sessions").rglob("*.json"))
