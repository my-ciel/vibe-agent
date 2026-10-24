import os
import sys
from pathlib import Path

import pytest

from vibe_agent import platform_utils as pu
from vibe_agent.tools import ToolContext, build_tools


def fake_python_dir(base: Path, name="py", windows=False) -> str:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    exe = d / ("python.exe" if windows else "python")
    exe.write_text("")
    exe.chmod(0o755)
    return str(d)


def env_for(root, path_entries, **kw):
    environ = {"PATH": os.pathsep.join(path_entries), "VIRTUAL_ENV": "/app/.venv", "KEEP": "1"}
    return pu.command_env(root, environ=environ, **kw)


def test_app_venv_is_removed_from_path_and_env(tmp_path):
    app_bin = fake_python_dir(tmp_path, "appenv/bin")
    system = fake_python_dir(tmp_path, "system")
    env = env_for(tmp_path / "proj", [app_bin, system], prefix=str(tmp_path / "appenv"), base_prefix="/usr", windows=False)
    assert env["PATH"].split(os.pathsep) == [system]
    assert "VIRTUAL_ENV" not in env and env["KEEP"] == "1" and env["PYTHONUTF8"] == "1"


def test_not_in_venv_leaves_path_alone(tmp_path):
    system = fake_python_dir(tmp_path, "system")
    env = env_for(tmp_path / "proj", [system], prefix="/usr", base_prefix="/usr", windows=False)
    assert env["PATH"] == system and env["VIRTUAL_ENV"] == "/app/.venv"


def test_project_venv_is_activated(tmp_path):
    proj = tmp_path / "proj"
    venv_bin = fake_python_dir(proj, ".venv/bin")
    system = fake_python_dir(tmp_path, "system")
    env = env_for(proj, [system], prefix="/usr", base_prefix="/usr", windows=False)
    assert env["PATH"].split(os.pathsep)[0] == venv_bin
    assert env["VIRTUAL_ENV"] == str(proj / ".venv")


def test_windows_layout_uses_scripts_dir(tmp_path):
    proj = tmp_path / "proj"
    scripts = fake_python_dir(proj, ".venv/Scripts", windows=True)
    env = env_for(proj, [], prefix="C:/x", base_prefix="C:/x", windows=True)
    assert env["PATH"].split(os.pathsep)[0] == scripts


def test_base_python_provided_when_system_has_none(tmp_path):
    base = fake_python_dir(tmp_path, "uvpython")
    env = env_for(tmp_path / "proj", [str(tmp_path / "empty")], prefix="/usr", base_prefix="/usr",
                  base_python=str(Path(base) / "python"), windows=False)
    assert env["PATH"].split(os.pathsep)[0] == base


def test_windowsapps_stub_does_not_count_as_python(tmp_path):
    stub = fake_python_dir(tmp_path, "Microsoft/WindowsApps")
    base = fake_python_dir(tmp_path, "uvpython")
    env = env_for(tmp_path / "proj", [stub], prefix="/usr", base_prefix="/usr",
                  base_python=str(Path(base) / "python"), windows=False)
    assert env["PATH"].split(os.pathsep)[0] == base


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="앱이 가상환경에서 실행될 때만 의미가 있다")
def test_real_commands_do_not_see_app_packages_and_use_project_venv(tmp_path):
    run = build_tools()["run_command"].run
    ctx = ToolContext(tmp_path.resolve())

    out = run(ctx, {"command": 'python -c "import streamlit"', "timeout": 60})
    assert "ModuleNotFoundError" in out and "streamlit" in out  # 앱의 패키지가 새지 않는다

    out = run(ctx, {"command": "python -m venv .venv", "timeout": 120})
    assert "[종료 코드 0]" in out
    out = run(ctx, {"command": 'python -c "import sys; print(sys.prefix)"', "timeout": 60})
    assert str((tmp_path / ".venv").resolve()) in out  # .venv가 자동으로 활성화된다
    out = run(ctx, {"command": "python -m pip --version", "timeout": 60})
    assert "[종료 코드 0]" in out and ".venv" in out  # pip 사용 가능
