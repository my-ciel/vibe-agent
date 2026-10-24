"""앱 실행기: 빈 포트를 찾아 서버를 띄우고, 준비되면 브라우저를 연다.

  python launch.py          앱 실행
  python launch.py --check  환경 자가진단
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8501


def find_port(start: int = DEFAULT_PORT, attempts: int = 30) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"{start}~{start + attempts - 1}번 포트가 모두 사용 중이에요.")


def wait_until_ready(port: int, timeout: float = 90.0, process: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/_stcore/health"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def run_check() -> int:
    from vibe_agent.doctor import format_report, run_checks

    checks = run_checks()
    print(format_report(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def cleanup_leftovers() -> None:
    """앱이 종료된 뒤 남은 백그라운드 프로그램(에이전트가 띄운 서버 등)을 정리한다."""
    try:
        from vibe_agent.processes import ProcessManager

        ProcessManager().cleanup_stale()
    except Exception:
        pass  # 정리 실패가 종료를 막으면 안 된다


def run_app() -> int:
    port = find_port()
    url = f"http://localhost:{port}"
    command = [
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.port", str(port),
        "--server.address", "localhost",  # 이 컴퓨터에서만 접속 가능
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    print("Vibe Agent를 시작합니다. 처음 실행할 때는 1~2분 걸릴 수 있어요...")
    process = subprocess.Popen(command, cwd=ROOT)
    try:
        if wait_until_ready(port, process=process):
            print(f"\n브라우저가 열립니다: {url}")
            print("열리지 않으면 위 주소를 브라우저에 붙여넣으세요.")
            print("끝내려면 이 창을 닫거나 Ctrl+C를 누르세요.\n")
            webbrowser.open(url)
        elif process.poll() is not None:
            print("앱이 시작되지 못했어요. 위의 오류 메시지를 강사에게 보여주세요.")
            return process.returncode or 1
        else:
            print(f"서버가 아직 준비되지 않았어요. 잠시 후 브라우저에서 {url} 을 열어 보세요.")
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0
    finally:
        cleanup_leftovers()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    return run_check() if "--check" in sys.argv[1:] else run_app()


if __name__ == "__main__":
    sys.exit(main())
