"""대화 저장/복원. 폴더별로 여러 대화를 홈 폴더 아래(~/.vibe-agent/sessions)에 보관한다.

- 대화 하나 = JSON 파일 하나(내용) + 작은 메타 파일 하나(목록 표시용). 파일이 따로라서 대화끼리 덮어쓰지 않는다.
- API 키는 저장하지 않는다. 대화에는 코드와 파일 내용이 들어 있으므로 공용 PC에서는 삭제하거나 저장을 끈다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import platform_utils as pu
from .attachments import parse_user_content

SESSIONS_ROOT = Path.home() / ".vibe-agent" / "sessions"
VERSION = 1
TITLE_LENGTH = 40
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class SessionInfo:
    id: str
    title: str
    updated: float
    messages: int
    cost: float


def folder_dir(root: Path) -> Path:
    """폴더 경로로 정한 저장 위치. 같은 폴더는 늘 같은 곳을 가리킨다 (윈도우는 대소문자 무시)."""
    resolved = str(Path(root).resolve())
    key = resolved.lower() if pu.IS_WINDOWS else resolved
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^\w.-]+", "_", Path(resolved).name, flags=re.UNICODE).strip("._")[:30] or "root"
    return SESSIONS_ROOT / f"{slug}-{digest}"


def new_session_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:6]}"


def valid_id(session_id: str) -> bool:
    return bool(_ID.match(session_id or ""))


def make_title(messages: list[dict]) -> str:
    for message in messages:
        if message["role"] == "user":
            text, attachments = parse_user_content(message["content"])
            text = " ".join(text.split()) or (f"📎 {attachments[0].path}" if attachments else "")
            if text:
                return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1] + "…"
    return "새 대화"


def has_content(messages: list[dict]) -> bool:
    return any(m["role"] == "user" for m in messages)


def snapshot(agent: Any, session_id: str, created: float) -> dict:
    tracker = agent.tracker
    return {
        "version": VERSION,
        "id": session_id,
        "root": str(agent.root),
        "created": created,
        "updated": time.time(),
        "title": make_title(agent.messages),
        "messages": agent.messages[1:],  # 시스템 프롬프트는 매번 새로 만든다
        "decisions": agent.decisions,
        "previews": agent.previews,
        "read_state": agent.read_state,
        "tracker": {
            "prompt_tokens": tracker.prompt_tokens,
            "completion_tokens": tracker.completion_tokens,
            "cost_usd": tracker.cost_usd,
            "unpriced_calls": tracker.unpriced_calls,
        },
    }


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # 쓰는 도중 꺼져도 기존 파일이 깨지지 않는다


def save(agent: Any, session_id: str, created: float) -> bool:
    """대화를 저장한다. 사용자 메시지가 없으면 저장하지 않는다. 저장 실패는 조용히 무시한다(앱은 계속 동작)."""
    if not valid_id(session_id) or not has_content(agent.messages):
        return False
    try:
        folder = folder_dir(agent.root)
        folder.mkdir(parents=True, exist_ok=True)
        data = snapshot(agent, session_id, created)
        _write_atomic(folder / f"{session_id}.json", data)
        meta = {k: data[k] for k in ("id", "title", "updated", "created")}
        meta.update(messages=len(data["messages"]), cost=data["tracker"]["cost_usd"])
        _write_atomic(folder / f"{session_id}.meta.json", meta)
        return True
    except (OSError, TypeError, ValueError):
        return False


def load(root: Path, session_id: str) -> dict | None:
    if not valid_id(session_id):
        return None
    try:
        data = json.loads((folder_dir(root) / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("messages"), list):
        return None
    return data


def restore(agent: Any, data: dict) -> None:
    """저장된 내용을 에이전트에 되돌린다 (시스템 프롬프트는 유지)."""
    agent.messages = [agent.messages[0], *data["messages"]]
    agent.decisions = {k: bool(v) for k, v in (data.get("decisions") or {}).items()}
    agent.previews = dict(data.get("previews") or {})
    agent.read_state = {k: list(v) for k, v in (data.get("read_state") or {}).items()}
    saved = data.get("tracker") or {}
    tracker = agent.tracker
    tracker.prompt_tokens = int(saved.get("prompt_tokens", 0))
    tracker.completion_tokens = int(saved.get("completion_tokens", 0))
    tracker.cost_usd = float(saved.get("cost_usd", 0.0))
    tracker.unpriced_calls = int(saved.get("unpriced_calls", 0))
    agent.steps = 0


def list_sessions(root: Path) -> list[SessionInfo]:
    """이 폴더의 저장된 대화 (최근에 쓴 순). 깨진 파일은 건너뛴다."""
    infos = []
    for meta_path in folder_dir(root).glob("*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not valid_id(meta["id"]) or not (meta_path.parent / f"{meta['id']}.json").exists():
                continue
            infos.append(SessionInfo(meta["id"], str(meta["title"]), float(meta["updated"]), int(meta["messages"]), float(meta["cost"])))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(infos, key=lambda i: i.updated, reverse=True)


def delete(root: Path, session_id: str) -> None:
    if not valid_id(session_id):
        return
    for suffix in (".json", ".meta.json"):
        try:
            (folder_dir(root) / f"{session_id}{suffix}").unlink()
        except OSError:
            pass


def delete_all(root: Path) -> int:
    count = 0
    for info in list_sessions(root):
        delete(root, info.id)
        count += 1
    return count
