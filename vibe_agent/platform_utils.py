"""OS별로 달라지는 부분(셸 실행, 인코딩, 프로세스 종료)을 이 모듈에 모은다."""
from __future__ import annotations

import locale
import os
import platform
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def os_description() -> str:
    if IS_WINDOWS:
        return f"Windows {platform.release()}"
    if IS_MAC:
        return f"macOS {platform.mac_ver()[0]}"
    return f"{platform.system()} {platform.release()}"


def shell_description() -> str:
    if IS_WINDOWS:
        return (
            "Windows cmd.exe. Use cmd syntax (dir, type, del, copy, &&), not PowerShell "
            "or bash syntax. To use PowerShell, call: powershell -NoProfile -Command \"...\""
        )
    return "POSIX sh (/bin/sh)"


@dataclass
class CommandResult:
    output: str
    exit_code: int | None
    timed_out: bool = False


def truncate_middle(text: str, limit: int) -> str:
    """긴 출력은 앞뒤만 남긴다. 에러 메시지는 보통 끝에 있으므로 꼬리를 보존한다."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n... [{omitted}자 생략] ...\n\n{text[-tail:]}"


def decode_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def _kill_tree(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _is_inside(entry: str, folder: Path) -> bool:
    try:
        return Path(entry).resolve().is_relative_to(folder)
    except (OSError, ValueError):
        return False


def _has_real_python(path: str) -> bool:
    for name in ("python", "python3"):
        found = shutil.which(name, path=path)
        # 윈도우의 WindowsApps\python.exe는 파이썬이 아니라 스토어 안내용 바로가기다.
        if found and "windowsapps" not in found.lower():
            return True
    return False


def command_env(
    project_root: str | Path,
    environ: dict[str, str] | None = None,
    prefix: str | None = None,
    base_prefix: str | None = None,
    base_python: str | None = None,
    windows: bool | None = None,
) -> dict[str, str]:
    """에이전트가 실행하는 명령의 환경.

    - 앱 자신의 가상환경이 새어 나오지 않게 한다 (그대로면 `python`이 앱의 파이썬이 되어
      streamlit이 보이고 pip도 없다).
    - 프로젝트에 .venv가 있으면 활성화된 것처럼 PATH/VIRTUAL_ENV를 맞춘다. 명령마다 새 셸이라
      `activate`가 유지되지 않으므로 앱이 대신 항상 적용해 준다.
    - 시스템에 진짜 파이썬이 없으면(윈도우 수강생 등) 앱이 쓰는 기본 파이썬을 제공한다.
    """
    env = dict(os.environ if environ is None else environ)
    prefix = prefix or sys.prefix
    base_prefix = base_prefix or sys.base_prefix
    base_python = base_python or getattr(sys, "_base_executable", sys.executable)
    windows = IS_WINDOWS if windows is None else windows

    entries = [e for e in env.get("PATH", "").split(os.pathsep) if e]
    if prefix != base_prefix:  # 앱이 가상환경 안에서 실행 중일 때만 그 환경을 걷어낸다
        app_env = Path(prefix).resolve()
        entries = [e for e in entries if not _is_inside(e, app_env)]
        for name in ("VIRTUAL_ENV", "PYTHONHOME", "__PYVENV_LAUNCHER__"):
            env.pop(name, None)

    venv = Path(project_root) / ".venv"
    venv_bin = venv / ("Scripts" if windows else "bin")
    if (venv_bin / ("python.exe" if windows else "python")).exists():
        env["VIRTUAL_ENV"] = str(venv)
        entries.insert(0, str(venv_bin))
    elif not _has_real_python(os.pathsep.join(entries)):
        entries.insert(0, str(Path(base_python).parent))

    env["PATH"] = os.pathsep.join(entries)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def run_shell(
    command: str, cwd: str, timeout: float = 60, max_output: int = 20_000
) -> CommandResult:
    env = command_env(cwd)
    if IS_WINDOWS:
        # 한글 윈도우(cp949)에서도 출력이 깨지지 않도록 콘솔 코드페이지를 UTF-8로 맞춘다.
        full_command = f"chcp 65001>nul & {command}"
        extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        full_command = command
        extra = {"start_new_session": True}

    proc = subprocess.Popen(
        full_command,
        shell=True,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,  # 입력을 기다리는 명령이 멈추지 않도록
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **extra,
    )
    timed_out = False
    try:
        data, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            data, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            data = b""
    return CommandResult(
        output=truncate_middle(decode_output(data or b""), max_output),
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
    )
