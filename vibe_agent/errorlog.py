"""앱 내부 오류를 파일에 남긴다. 윈도우 수강생이 터미널에서 오류를 복사하기 어려우므로 화면에도 보여주고 파일에도 기록한다."""
from __future__ import annotations

import time
import traceback
from pathlib import Path

LOG_PATH = Path.home() / ".vibe-agent" / "errors.log"
MAX_BYTES = 200_000


def log_exception(context: str, exc: BaseException) -> str:
    """오류를 기록하고 화면에 보여줄 traceback 문자열을 돌려준다. 기록에 실패해도 예외를 내지 않는다."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
            LOG_PATH.write_text(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-MAX_BYTES // 2:], encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n{text}\n")
    except OSError:
        pass
    return text
