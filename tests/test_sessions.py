import json

import pytest

from tests.fakes import ScriptedLLM, call
from vibe_agent import sessions
from vibe_agent.attachments import Upload, prepare_message
from vibe_agent.loop import Agent


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_ROOT", tmp_path / "store")
    return tmp_path / "store"


def make_agent(root, *turns, **kw):
    return Agent(llm=ScriptedLLM(*turns), model="m", root=root, **kw)


def chat(agent, text):
    agent.submit(text)
    list(agent.run())


def test_folder_dir_is_stable_and_distinct(tmp_path):
    a, b = tmp_path / "프로젝트 A", tmp_path / "b"
    a.mkdir(); b.mkdir()
    assert sessions.folder_dir(a) == sessions.folder_dir(a / ".." / "프로젝트 A")
    assert sessions.folder_dir(a) != sessions.folder_dir(b)
    assert "/" not in sessions.folder_dir(a).name and "프로젝트_A" in sessions.folder_dir(a).name


def test_empty_conversation_is_not_saved(tmp_path):
    agent = make_agent(tmp_path)
    assert sessions.save(agent, "s1", 0.0) is False
    assert sessions.list_sessions(tmp_path) == []


def test_save_list_load_restore_roundtrip(tmp_path):
    agent = make_agent(tmp_path, {"text": "안녕하세요", "usage": __import__("vibe_agent.cost", fromlist=["Usage"]).Usage(100, 20, 0.0123)})
    chat(agent, "  인사해줘\n\n제목이 길어질 때는 줄여서 보여줘 아주아주아주아주아주 아주아주아주아주 정말 길게 계속 이어지는 문장  ")
    assert sessions.save(agent, "s1", 1.0)

    infos = sessions.list_sessions(tmp_path)
    assert [(i.id, i.messages) for i in infos] == [("s1", 2)]
    assert infos[0].title.startswith("인사해줘 제목이 길어질 때는") and infos[0].title.endswith("…") and len(infos[0].title) <= 40
    assert abs(infos[0].cost - 0.0123) < 1e-9

    fresh = make_agent(tmp_path)
    sessions.restore(fresh, sessions.load(tmp_path, "s1"))
    assert [m["role"] for m in fresh.messages] == ["system", "user", "assistant"]
    assert fresh.messages[-1]["content"] == "안녕하세요" and abs(fresh.tracker.cost_usd - 0.0123) < 1e-9
    assert fresh.tracker.prompt_tokens == 100


def test_secrets_are_not_stored(tmp_path):
    agent = make_agent(tmp_path, {"text": "ok"})
    agent.llm.api_key = "sk-or-v1-secret"          # 클라이언트 객체는 저장 대상이 아니다
    chat(agent, "hi")
    sessions.save(agent, "s1", 0.0)
    raw = "".join(p.read_text(encoding="utf-8") for p in sessions.folder_dir(tmp_path).iterdir())
    assert "sk-or-v1-secret" not in raw and '"api_key"' not in raw


def test_pending_approval_survives_restore(tmp_path):
    agent = make_agent(tmp_path, {"calls": [call("c1", "write_file", path="a.txt", content="x")]})
    chat(agent, "파일 만들어줘")
    assert agent.has_pending_calls() and not agent.decisions
    sessions.save(agent, "s1", 0.0)

    fresh = make_agent(tmp_path, {"text": "완료"})
    sessions.restore(fresh, sessions.load(tmp_path, "s1"))
    assert fresh.has_pending_calls()
    from vibe_agent.loop import ApprovalRequest
    assert isinstance(list(fresh.run())[-1], ApprovalRequest)     # 승인 카드가 다시 나온다
    fresh.decide("c1", True)
    list(fresh.run())
    assert (tmp_path / "a.txt").read_text() == "x"


def test_read_state_is_restored_so_conflict_guard_keeps_working(tmp_path):
    (tmp_path / "f.txt").write_text("원본")
    agent = make_agent(tmp_path, {"calls": [call("c1", "read_file", path="f.txt")]}, {"text": "읽음"})
    chat(agent, "읽어줘")
    sessions.save(agent, "s1", 0.0)
    fresh = make_agent(tmp_path)
    sessions.restore(fresh, sessions.load(tmp_path, "s1"))
    assert "f.txt" in fresh.read_state


def test_images_survive_and_title_uses_user_text(tmp_path):
    import io
    from PIL import Image

    buf = io.BytesIO(); Image.new("RGB", (30, 30), (1, 2, 3)).save(buf, "PNG")
    prepared = prepare_message("이 그림 봐줘", [Upload("a.png", buf.getvalue())], tmp_path, vision=True)
    agent = make_agent(tmp_path, {"text": "봤어요"})
    chat(agent, prepared.content)
    sessions.save(agent, "s1", 0.0)
    assert sessions.list_sessions(tmp_path)[0].title == "이 그림 봐줘"
    fresh = make_agent(tmp_path)
    sessions.restore(fresh, sessions.load(tmp_path, "s1"))
    assert fresh.messages[1]["content"][2]["type"] == "image_url"


def test_sessions_are_separate_per_folder_and_sorted_by_recency(tmp_path):
    p1, p2 = tmp_path / "p1", tmp_path / "p2"
    p1.mkdir(); p2.mkdir()
    for root, sid, text in [(p1, "a", "첫째"), (p1, "b", "둘째"), (p2, "c", "다른 폴더")]:
        agent = make_agent(root, {"text": "ok"})
        chat(agent, text)
        sessions.save(agent, sid, 0.0)
    assert [i.title for i in sessions.list_sessions(p1)][0] == "둘째" and len(sessions.list_sessions(p1)) == 2
    assert [i.title for i in sessions.list_sessions(p2)] == ["다른 폴더"]


def test_delete_and_delete_all(tmp_path):
    for sid in ("a", "b", "c"):
        agent = make_agent(tmp_path, {"text": "ok"})
        chat(agent, sid)
        sessions.save(agent, sid, 0.0)
    sessions.delete(tmp_path, "a")
    assert {i.id for i in sessions.list_sessions(tmp_path)} == {"b", "c"} and sessions.load(tmp_path, "a") is None
    assert sessions.delete_all(tmp_path) == 2 and sessions.list_sessions(tmp_path) == []


def test_corrupt_and_foreign_files_are_ignored(tmp_path):
    agent = make_agent(tmp_path, {"text": "ok"})
    chat(agent, "정상")
    sessions.save(agent, "good", 0.0)
    folder = sessions.folder_dir(tmp_path)
    (folder / "bad.json").write_text("{깨짐", encoding="utf-8")
    (folder / "bad.meta.json").write_text("{깨짐", encoding="utf-8")
    (folder / "orphan.meta.json").write_text(json.dumps({"id": "orphan", "title": "t", "updated": 1, "messages": 1, "cost": 0}), encoding="utf-8")
    assert [i.id for i in sessions.list_sessions(tmp_path)] == ["good"]
    assert sessions.load(tmp_path, "bad") is None and sessions.load(tmp_path, "nope") is None
    (folder / "future.json").write_text(json.dumps({"version": 99, "messages": []}), encoding="utf-8")
    assert sessions.load(tmp_path, "future") is None


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "a b", "x" * 80])
def test_invalid_ids_are_rejected(tmp_path, bad):
    agent = make_agent(tmp_path, {"text": "ok"})
    chat(agent, "hi")
    assert sessions.save(agent, bad, 0.0) is False and sessions.load(tmp_path, bad) is None
    sessions.delete(tmp_path, bad)  # 예외 없이 무시


def test_save_does_not_leave_temp_files(tmp_path):
    agent = make_agent(tmp_path, {"text": "ok"})
    chat(agent, "hi")
    sessions.save(agent, "s1", 0.0)
    sessions.save(agent, "s1", 0.0)
    assert sorted(p.name for p in sessions.folder_dir(tmp_path).iterdir()) == ["s1.json", "s1.meta.json"]
