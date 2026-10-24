"""OS별로 달라지는 부분(셸 실행, 인코딩, 프로세스 종료)을 이 모듈에 모은다."""
from __future__ import annotations

import locale
import os
import platform
import signal
import subprocess
import sys
from dataclasses import dataclass

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


def run_shell(
    command: str, cwd: str, timeout: float = 60, max_output: int = 20_000
) -> CommandResult:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
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
