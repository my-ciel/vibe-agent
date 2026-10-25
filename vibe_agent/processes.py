"""백그라운드 프로세스 관리: 서버나 GUI 앱처럼 계속 실행되는 프로그램을 띄우고, 로그를 읽고, 종료한다.

run_command는 끝나야 결과를 돌려주지만, 개발 서버는 끝나지 않으므로 별도로 관리한다.
앱이 종료되거나 새 대화를 시작하면 띄운 프로세스를 함께 정리한다.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import platform_utils as pu
from .errors import ToolError

MAX_PROCESSES = 3
MAX_BUFFER_CHARS = 200_000
MAX_READ_CHARS = 8_000
REGISTRY_PATH = Path.home() / ".vibe-agent" / "processes.json"

_URL = re.compile(r"https?://[^\s'\"<>)\]]+")
_LOCAL_HOSTS = re.compile(r"^(https?://)(0\.0\.0\.0|127\.0\.0\.1|\[::\]|\[::1\]|::)(?=[:/]|$)")


def kill_tree(pid: int) -> None:
    """프로세스와 그 자식들을 모두 종료한다 (셸이 띄운 실제 프로그램까지)."""
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def find_urls(text: str) -> list[str]:
    """로그에서 접속 주소를 찾는다. 0.0.0.0/127.0.0.1은 localhost로 바꿔 보여준다."""
    urls: list[str] = []
    for raw in _URL.findall(text):
        url = _LOCAL_HOSTS.sub(r"\1localhost", raw.rstrip(".,;:"))
        if url not in urls:
            urls.append(url)
    return urls


@dataclass
class ManagedProcess:
    id: str
    name: str
    command: str
    popen: subprocess.Popen
    cwd: str = ""
    started: float = field(default_factory=time.time)
    _text: str = ""
    _base: int = 0  # 버퍼 앞부분이 잘려 나간 글자 수
    _read_pos: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.popen.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.popen.poll()

    def _append(self, chunk: str) -> None:
        with self._lock:
            self._text += chunk
            if len(self._text) > MAX_BUFFER_CHARS:
                cut = len(self._text) - MAX_BUFFER_CHARS
                self._text, self._base = self._text[cut:], self._base + cut

    def _pump(self) -> None:
        assert self.popen.stdout is not None
        for line in iter(self.popen.stdout.readline, b""):
            self._append(pu.decode_output(line))
        self.popen.stdout.close()

    def full_text(self) -> str:
        with self._lock:
            return self._text

    def read_new(self) -> str:
        with self._lock:
            end = self._base + len(self._text)
            skipped = max(self._base - self._read_pos, 0)
            new = self._text[max(self._read_pos - self._base, 0):]
            self._read_pos = end
        prefix = f"(로그가 너무 길어 앞부분 {skipped}자는 생략됨)\n" if skipped else ""
        return prefix + new

    def urls(self) -> list[str]:
        return find_urls(self.full_text())


class ProcessManager:
    def __init__(self, registry: Path | None = None):
        self._procs: dict[str, ManagedProcess] = {}
        self._counter = 0
        self._registry = registry or REGISTRY_PATH
        atexit.register(self.stop_all)

    # -- 조회

    def all(self) -> list[ManagedProcess]:
        return list(self._procs.values())

    def running(self) -> list[ManagedProcess]:
        return [p for p in self._procs.values() if p.running]

    def get(self, proc_id: str) -> ManagedProcess:
        proc = self._procs.get(str(proc_id).strip())
        if proc is None:
            known = ", ".join(self._procs) or "없음"
            raise ToolError(f"'{proc_id}' 프로세스가 없습니다. (현재 목록: {known})")
        return proc

    # -- 실행/종료

    def find_running(self, command: str, cwd: str) -> ManagedProcess | None:
        """같은 명령(같은 폴더)으로 이미 실행 중인 프로세스."""
        norm = " ".join(command.split())
        for proc in self.running():
            if " ".join(proc.command.split()) == norm and proc.cwd == cwd:
                return proc
        return None

    def start(self, command: str, cwd: str, name: str = "") -> ManagedProcess:
        if len(self.running()) >= MAX_PROCESSES:
            raise ToolError(f"동시에 실행할 수 있는 프로세스는 {MAX_PROCESSES}개까지예요. 먼저 stop_process로 정리하세요.")
        env = pu.command_env(cwd)
        env["PYTHONUNBUFFERED"] = "1"  # 파이썬 프로그램의 로그가 바로 보이게
        if pu.IS_WINDOWS:
            full, extra = f"chcp 65001>nul & {command}", {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            full, extra = command, {"start_new_session": True}
        popen = subprocess.Popen(
            full, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **extra,
        )
        self._counter += 1
        proc = ManagedProcess(f"p{self._counter}", name.strip() or command.split()[0], command, popen, cwd=cwd)
        threading.Thread(target=proc._pump, daemon=True).start()
        self._procs[proc.id] = proc
        self._save_registry()
        return proc

    def stop(self, proc_id: str) -> ManagedProcess:
        proc = self.get(proc_id)
        if proc.running:
            kill_tree(proc.popen.pid)
            try:
                proc.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._save_registry()
        return proc

    def stop_all(self) -> None:
        for proc in list(self._procs.values()):
            if proc.running:
                kill_tree(proc.popen.pid)
        self._save_registry()

    def wait_for_output(self, proc: ManagedProcess, seconds: float) -> None:
        """새 로그가 나오거나 프로세스가 끝나거나 시간이 다 될 때까지 기다린다."""
        deadline = time.monotonic() + seconds
        start_len = len(proc.full_text())
        while time.monotonic() < deadline and proc.running and len(proc.full_text()) == start_len:
            time.sleep(0.1)

    # -- 이전 실행이 남긴 프로세스 정리 (앱이 비정상 종료됐을 때)

    def _read_registry(self) -> list[dict]:
        try:
            data = json.loads(self._registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def _save_registry(self) -> None:
        """내가 띄운 프로세스를 기록한다. 다른 앱 인스턴스가 기록한 항목은 그대로 둔다."""
        try:
            me = os.getpid()
            others = [e for e in self._read_registry() if e.get("owner") != me]
            mine = []
            for proc in self.running():
                try:
                    mine.append({
                        "pid": proc.popen.pid,
                        "create_time": psutil.Process(proc.popen.pid).create_time(),
                        "owner": me,
                        "owner_create_time": psutil.Process(me).create_time(),
                    })
                except psutil.NoSuchProcess:
                    pass
            self._registry.parent.mkdir(parents=True, exist_ok=True)
            self._registry.write_text(json.dumps(others + mine), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _owner_alive(entry: dict) -> bool:
        try:
            owner = psutil.Process(int(entry["owner"]))
            return abs(owner.create_time() - float(entry["owner_create_time"])) < 1.0
        except (psutil.NoSuchProcess, KeyError, TypeError, ValueError):
            return False

    def cleanup_stale(self) -> int:
        """앱이 비정상 종료되어 남은 프로세스를 종료한다.

        소유 앱이 이미 없고, PID와 시작 시각이 모두 일치할 때만 종료한다 (PID 재사용, 동시에 떠 있는 다른 앱 보호).
        """
        keep, killed = [], 0
        for entry in self._read_registry():
            if self._owner_alive(entry):
                keep.append(entry)
                continue
            try:
                proc = psutil.Process(int(entry["pid"]))
                if abs(proc.create_time() - float(entry["create_time"])) < 1.0:
                    kill_tree(proc.pid)
                    killed += 1
            except (psutil.NoSuchProcess, KeyError, TypeError, ValueError):
                continue
        try:
            self._registry.parent.mkdir(parents=True, exist_ok=True)
            self._registry.write_text(json.dumps(keep), encoding="utf-8")
        except OSError:
            pass
        return killed
