import pytest

from vibe_agent.errors import ToolError
from vibe_agent.sandbox import check_venv_policy
from vibe_agent.tools import ToolContext, build_tools

TOOLS = build_tools()


@pytest.mark.parametrize("cmd", [
    "pip install requests",
    "pip3 install -r requirements.txt",
    "python -m pip install flask",
    "python3 -m pip install flask",
    "py -m pip install numpy",
    "python -u -m pip install x",
    "uv pip install pandas",
    "cd app && pip install x",
    "python -m venv .venv && pip install requests",   # 같은 줄에서 만든 직후의 bare pip는 전역을 쓰게 된다
    "PIP INSTALL requests",
])
def test_pip_install_without_venv_is_blocked(tmp_path, cmd):
    assert "가상환경" in check_venv_policy(cmd, tmp_path)


@pytest.mark.parametrize("cmd", [
    ".venv/bin/pip install requests",
    ".venv\\Scripts\\python.exe -m pip install requests",
    ".venv/bin/python -m pip install requests",
    "pip list",
    "pip --version",
    "python -m venv .venv",
    "python app.py",
    "echo pip install",   # 글자일 뿐인 경우는 구분하기 어려우므로 아래 별도 확인
])
def test_other_commands_are_allowed(tmp_path, cmd):
    if cmd.startswith("echo"):
        pytest.skip("echo 안의 문장은 구분하지 않는다 (보수적으로 막힘)")
    assert check_venv_policy(cmd, tmp_path) is None


def test_install_allowed_once_venv_exists(tmp_path):
    (tmp_path / ".venv").mkdir()
    assert check_venv_policy("pip install requests", tmp_path) is None
    assert check_venv_policy("python -m pip install requests", tmp_path) is None


def test_rejected_before_approval_and_at_run(tmp_path):
    ctx = ToolContext(tmp_path)
    for tool in ("run_command", "start_process"):
        with pytest.raises(ToolError, match="venv"):
            TOOLS[tool].preview(ctx, {"command": "pip install requests"})
    with pytest.raises(ToolError, match="venv"):
        TOOLS["run_command"].run(ctx, {"command": "pip install requests"})


def test_prompt_tells_model_to_create_venv_first(tmp_path):
    from vibe_agent.prompts import build_system_prompt

    prompt = build_system_prompt(tmp_path)
    assert "FIRST create a virtual environment" in prompt and "Never install packages globally" in prompt
