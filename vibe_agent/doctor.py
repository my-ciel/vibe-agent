"""환경 자가진단. 윈도우/맥에서 앱이 쓰는 기능이 실제로 동작하는지 확인한다."""
from __future__ import annotations

import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import platform_utils as pu
from .errors import ToolError
from .llm import OPENROUTER_BASE_URL
from .sandbox import resolve_in_root
from .tools import ToolContext, build_tools


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str = ""


def _safe(name: str, fn) -> Check:
    try:
        return fn()
    except Exception as exc:  # 진단 도구는 어떤 경우에도 끝까지 돌아야 한다
        return Check(name, "fail", f"{type(exc).__name__}: {exc}")


def _python() -> Check:
    version = sys.version.split()[0]
    ok = sys.version_info >= (3, 10)
    return Check("파이썬 버전", "ok" if ok else "fail", f"{version} (3.10 이상 필요)")


def _system() -> Check:
    return Check("운영체제", "ok", f"{pu.os_description()} / 셸: {'cmd.exe' if pu.IS_WINDOWS else 'sh'}")


def _packages() -> Check:
    import openai
    import streamlit

    return Check("패키지", "ok", f"streamlit {streamlit.__version__}, openai {openai.__version__}")


def _tkinter() -> Check:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return Check("폴더 선택창(tkinter)", "warn", "사용할 수 없어요. 폴더 경로를 직접 입력하면 됩니다.")
    return Check("폴더 선택창(tkinter)", "ok", "사용 가능")


def _korean_files() -> Check:
    tools = build_tools()
    with tempfile.TemporaryDirectory(prefix="한글폴더_") as tmp:
        ctx = ToolContext(Path(tmp).resolve())
        tools["write_file"].run(ctx, {"path": "하위/안녕.txt", "content": "가나다\n라마바\n"})
        shown = tools["read_file"].run(ctx, {"path": "하위/안녕.txt"})
        if "가나다" not in shown:
            return Check("한글 경로/파일", "fail", "한글 파일을 다시 읽지 못했어요")
        (ctx.root / "옛날파일.txt").write_bytes("한글 cp949".encode("cp949"))
        if "한글 cp949" not in tools["read_file"].run(ctx, {"path": "옛날파일.txt"}):
            return Check("한글 경로/파일", "fail", "cp949 파일을 읽지 못했어요")
    return Check("한글 경로/파일", "ok", "한글 폴더/파일 쓰기·읽기, cp949 파일 읽기 정상")


def _command_korean() -> Check:
    result = pu.run_shell("echo 안녕하세요", cwd=tempfile.gettempdir(), timeout=20)
    if "안녕하세요" in result.output:
        return Check("명령 실행(한글 출력)", "ok", "정상")
    return Check("명령 실행(한글 출력)", "fail", f"출력이 깨졌어요: {result.output.strip()[:80]!r}")


def _command_timeout() -> Check:
    start = time.monotonic()
    result = pu.run_shell(f'"{sys.executable}" -c "import time; time.sleep(30)"', cwd=tempfile.gettempdir(), timeout=2)
    elapsed = time.monotonic() - start
    if result.timed_out and elapsed < 15:
        return Check("명령 시간 초과 처리", "ok", f"{elapsed:.1f}초 만에 중단됨")
    return Check("명령 시간 초과 처리", "fail", f"중단되지 않았어요 (timed_out={result.timed_out}, {elapsed:.1f}초)")


def _sandbox() -> Check:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            resolve_in_root(Path(tmp), "..")
        except ToolError:
            return Check("작업 폴더 밖 접근 차단", "ok", "정상")
    return Check("작업 폴더 밖 접근 차단", "fail", "차단되지 않았어요")


def _port() -> Check:
    from launch import find_port

    return Check("사용 가능한 포트", "ok", f"{find_port()}번")


def _network() -> Check:
    try:
        response = httpx.get(f"{OPENROUTER_BASE_URL}/models", timeout=10)
    except httpx.HTTPError as exc:
        return Check("OpenRouter 연결", "fail", f"접속할 수 없어요 ({type(exc).__name__}). 학교/회사 네트워크가 막고 있을 수 있어요.")
    if response.status_code == 200:
        return Check("OpenRouter 연결", "ok", "정상")
    return Check("OpenRouter 연결", "fail", f"HTTP {response.status_code}")


def run_checks(network: bool = True) -> list[Check]:
    checks = [
        _safe("파이썬 버전", _python),
        _safe("운영체제", _system),
        _safe("패키지", _packages),
        _safe("폴더 선택창(tkinter)", _tkinter),
        _safe("한글 경로/파일", _korean_files),
        _safe("명령 실행(한글 출력)", _command_korean),
        _safe("명령 시간 초과 처리", _command_timeout),
        _safe("작업 폴더 밖 접근 차단", _sandbox),
        _safe("사용 가능한 포트", _port),
    ]
    if network:
        checks.append(_safe("OpenRouter 연결", _network))
    return checks


def format_report(checks: list[Check]) -> str:
    icons = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    lines = [f"{icons[c.status]} {c.name}: {c.detail}" for c in checks]
    failed = sum(c.status == "fail" for c in checks)
    lines.append("")
    lines.append("모든 항목이 정상이에요." if not failed else f"문제가 {failed}개 있어요. 위 내용을 강사에게 보여주세요.")
    return "\n".join(lines)
