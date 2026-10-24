import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import psutil
import pytest

from tests.fakes import ScriptedLLM, call
from vibe_agent.errors import ToolError
from vibe_agent.loop import Agent, ApprovalRequest, ToolResult
from vibe_agent.processes import ProcessManager, find_urls
from vibe_agent.tools import ToolContext, build_tools

TOOLS = build_tools()
PY = f'"{sys.executable}"'


@pytest.fixture
def mgr(tmp_path):
    manager = ProcessManager(registry=tmp_path / "reg.json")
    yield manager
    manager.stop_all()


@pytest.fixture
def ctx(tmp_path, mgr):
    return ToolContext((tmp_path / "proj").resolve() if (tmp_path / "proj").mkdir() is None else None, mgr)


def run(ctx, tool, /, **args):
    return TOOLS[tool].run(ctx, args)


def test_find_urls_normalizes_local_hosts():
    text = "Serving on http://0.0.0.0:8000/ and http://127.0.0.1:5000. Docs: https://example.com/a)."
    assert find_urls(text) == ["http://localhost:8000/", "http://localhost:5000", "https://example.com/a"]


def test_start_read_stop_lifecycle(ctx, mgr):
    out = run(ctx, "start_process", command=f"{PY} -c \"import time; print('서버 시작 http://localhost:8123', flush=True); time.sleep(60)\"", startup_wait=10)
    assert "실행 중" in out and "서버 시작" in out and "http://localhost:8123" in out
    proc = mgr.get("p1")
    pid = proc.popen.pid
    assert run(ctx, "read_process_output", id="p1").count("새 로그 없음") == 1  # 이미 읽은 로그는 다시 안 나온다
    assert "p1" in run(ctx, "list_processes") and "실행 중" in run(ctx, "list_processes")

    out = run(ctx, "stop_process", id="p1")
    assert "종료됨" in out
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_stop_kills_the_whole_process_tree(ctx, mgr):
    # 셸이 띄운 자식(파이썬)과 그 자식이 모두 종료돼야 한다
    child = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('ready',flush=True); time.sleep(60)"
    run(ctx, "start_process", command=f'{PY} -c "{child}"', startup_wait=10)
    proc = mgr.get("p1")
    family = psutil.Process(proc.popen.pid).children(recursive=True)
    assert len(family) >= 1
    run(ctx, "stop_process", id="p1")
    time.sleep(0.5)
    assert all(not p.is_running() or p.status() == psutil.STATUS_ZOMBIE for p in family)


def test_process_that_exits_immediately_reports_exit_code_and_logs(ctx):
    out = run(ctx, "start_process", command=f"{PY} -c \"import sys; print('boom'); sys.exit(3)\"", startup_wait=10)
    assert "종료됨 (종료 코드 3)" in out and "boom" in out


def test_read_waits_for_new_output(ctx, mgr):
    run(ctx, "start_process", command=f"{PY} -c \"import time; time.sleep(1.5); print('늦은 로그', flush=True); time.sleep(30)\"", startup_wait=0)
    started = time.monotonic()
    out = run(ctx, "read_process_output", id="p1", wait_seconds=10)
    assert "늦은 로그" in out and time.monotonic() - started < 9


def test_process_limit(ctx):
    for _ in range(3):
        run(ctx, "start_process", command=f"{PY} -c \"import time; time.sleep(60)\"", startup_wait=0)
    with pytest.raises(ToolError, match="3개까지"):
        run(ctx, "start_process", command=f"{PY} -c \"import time; time.sleep(60)\"", startup_wait=0)


def test_unknown_id_and_blocked_command(ctx):
    with pytest.raises(ToolError, match="없습니다"):
        run(ctx, "read_process_output", id="p9")
    with pytest.raises(ToolError):
        TOOLS["start_process"].preview(ctx, {"command": "sudo something"})


def test_without_manager_tools_explain(tmp_path):
    with pytest.raises(ToolError, match="백그라운드"):
        run(ToolContext(tmp_path), "list_processes")


def test_real_http_server_is_reachable_then_stopped(ctx, mgr):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (ctx.root / "index.html").write_text("<h1>안녕</h1>", encoding="utf-8")
    out = run(ctx, "start_process", command=f"{PY} -m http.server {port} --bind 127.0.0.1", startup_wait=15, name="web")
    assert f"http://localhost:{port}" in out
    assert "안녕" in urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5).read().decode()
    run(ctx, "stop_process", id="p1")
    time.sleep(0.5)
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)


def test_python_logs_are_not_buffered(ctx):
    # print만 해도(flush 없이) 파이프로 바로 보여야 한다
    out = run(ctx, "start_process", command=f"{PY} -c \"import time; print('즉시'); time.sleep(30)\"", startup_wait=5)
    assert "즉시" in out


def test_stale_cleanup_kills_only_orphans_with_matching_identity(tmp_path):
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    reused_pid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    guarded = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    me = psutil.Process()
    ct = lambda p: psutil.Process(p.pid).create_time()
    registry = tmp_path / "reg.json"
    registry.write_text(json.dumps([
        {"pid": orphan.pid, "create_time": ct(orphan), "owner": 2**22 + 7, "owner_create_time": 1.0},      # 주인 앱이 사라짐 -> 종료
        {"pid": reused_pid.pid, "create_time": ct(reused_pid) - 1000, "owner": 2**22 + 8, "owner_create_time": 1.0},  # PID 재사용 -> 건드리지 않음
        {"pid": guarded.pid, "create_time": ct(guarded), "owner": me.pid, "owner_create_time": me.create_time()},    # 주인 앱이 살아 있음 -> 건드리지 않음
    ]))
    try:
        assert ProcessManager(registry=registry).cleanup_stale() == 1
        orphan.wait(timeout=5)
        assert reused_pid.poll() is None and guarded.poll() is None
        left = json.loads(registry.read_text())
        assert [e["pid"] for e in left] == [guarded.pid]
    finally:
        for p in (orphan, reused_pid, guarded):
            p.kill()


def test_registry_keeps_other_owners_entries(tmp_path, ctx, mgr):
    other = {"pid": 999999, "create_time": 1.0, "owner": 2**22 + 9, "owner_create_time": 1.0}
    mgr._registry.write_text(json.dumps([other]))
    run(ctx, "start_process", command=f"{PY} -c \"import time; time.sleep(30)\"", startup_wait=0)
    entries = json.loads(mgr._registry.read_text())
    assert other in entries and any(e["owner"] == os.getpid() for e in entries)
    mgr.stop_all()
    assert json.loads(mgr._registry.read_text()) == [other]


def test_agent_flow_requires_approval_and_close_stops_processes(tmp_path):
    cmd = f"{PY} -c \"import time; print('up http://localhost:9000', flush=True); time.sleep(60)\""
    agent = Agent(
        llm=ScriptedLLM(
            {"calls": [call("c1", "start_process", command=cmd, name="web", startup_wait=10)]},
            {"calls": [call("c2", "read_process_output", id="p1")]},
            {"text": "실행했어요"},
        ),
        model="m", root=tmp_path,
    )
    agent.processes._registry = tmp_path / "reg.json"
    agent.submit("서버 띄워줘")
    events = list(agent.run())
    request = events[-1]
    assert isinstance(request, ApprovalRequest) and "백그라운드" in request.preview
    assert not agent.processes.all()  # 승인 전에는 실행하지 않는다

    agent.decide("c1", True)
    events = list(agent.run())
    results = [e for e in events if isinstance(e, ToolResult)]
    assert "http://localhost:9000" in results[0].output and "새 로그 없음" in results[1].output
    pid = agent.processes.get("p1").popen.pid
    agent.close()
    time.sleep(0.5)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
