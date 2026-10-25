import json

import pytest

from tests.fakes import call
from tests.test_app import make_app, texts
from vibe_agent import errorlog, sessions
from vibe_agent.transcript import ToolEntry, tool_title


@pytest.mark.parametrize("name,output,status", [
    ("run_command", "[종료 코드 0]\nok", "ok"),
    ("run_command", "[종료 코드 1]\nTraceback...", "warn"),
    ("run_command", "[시간 초과로 중단됨]\n", "warn"),
    ("start_process", "[p1 · web] 시작함 · 종료됨 (종료 코드 1)\n--- 새 로그 ---\nboom", "warn"),
    ("start_process", "[p1 · web] 시작함 · 실행 중 (pid 1)\n--- 새 로그 ---\nup", "ok"),
    ("read_file", "오류: 존재하지 않는 파일입니다: x", "error"),
])
def test_status_marks_failed_commands(name, output, status):
    entry = ToolEntry("1", name, {"command": "x"}, output)
    assert entry.status == status
    assert ("⚠️" in tool_title(entry)) == (status == "warn")


def test_failed_command_card_is_shown_open_with_a_warning_mark(tmp_path):
    cmd = "python -c \"import sys; print('boom'); sys.exit(2)\""
    at, _, _ = make_app(tmp_path, {"calls": [call("c1", "run_command", command=cmd)]}, {"text": "실패했네요"})
    at.run()
    at.chat_input[0].set_value("실행해줘").run()
    at.button(key="ok-c1").click().run()
    failed = next(e for e in at.expander if "⚠️" in e.label)
    assert failed.proto.expanded                       # 접혀 있지 않다
    assert any("종료 코드 2" in c.value for c in at.code)


def test_internal_error_shows_details_and_is_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(errorlog, "LOG_PATH", tmp_path / "errors.log")
    from vibe_agent.loop import Agent

    def broken(self):
        raise RuntimeError("일부러 낸 오류")
        yield

    monkeypatch.setattr(Agent, "run", broken)
    at, _, _ = make_app(tmp_path, {"text": "x"})
    at.run()
    at.chat_input[0].set_value("hi").run()
    assert not at.exception                                              # 화면이 깨지지 않는다
    assert any("앱 내부 오류가 발생했어요: RuntimeError" in e.value for e in at.error)
    assert any("Traceback" in c.value and "일부러 낸 오류" in c.value for c in at.code)
    assert "일부러 낸 오류" in (tmp_path / "errors.log").read_text(encoding="utf-8")


def test_save_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "save", lambda *a, **k: False)
    at, _, _ = make_app(tmp_path, {"text": "ok"})
    at.run()
    at.chat_input[0].set_value("hi").run()
    assert any("저장하지 못했어요" in w.value for w in at.sidebar.warning)


def test_corrupt_saved_conversation_starts_fresh_with_a_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(errorlog, "LOG_PATH", tmp_path / "errors.log")
    at, project, _ = make_app(tmp_path, {"text": "원래 답"})
    at.run()
    at.chat_input[0].set_value("원래 질문").run()
    folder = sessions.folder_dir(project)
    sid = next(folder.glob("*.json")).stem.replace(".meta", "")
    path = folder / f"{sid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"].append({"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]})  # 화면에 그릴 수 없는 구조
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    reloaded, _, _ = make_app(tmp_path)
    reloaded.run()
    assert not reloaded.exception
    assert any("새 대화로 시작" in i.value for i in reloaded.sidebar.info)
    assert "원래 질문" not in texts(reloaded)


def test_unreadable_saved_file_is_reported_not_silently_skipped(tmp_path):
    at, project, _ = make_app(tmp_path, {"text": "ok"})
    at.run()
    at.chat_input[0].set_value("질문").run()
    folder = sessions.folder_dir(project)
    sid = next(folder.glob("*.meta.json")).name.replace(".meta.json", "")
    (folder / f"{sid}.json").write_text("{깨진 파일", encoding="utf-8")
    reloaded, _, _ = make_app(tmp_path)
    reloaded.run()
    assert not reloaded.exception and "질문" not in texts(reloaded)
