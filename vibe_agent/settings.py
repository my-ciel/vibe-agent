"""마지막으로 쓴 폴더/모델만 기억한다. API 키는 어디에도 저장하지 않는다."""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path.home() / ".vibe-agent" / "settings.json"


def load_settings(path: Path | None = None) -> dict[str, str]:
    path = path or SETTINGS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in ("last_root", "last_model") and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_settings(values: dict[str, str], path: Path | None = None) -> None:
    path = path or SETTINGS_PATH
    allowed = {k: v for k, v in values.items() if k in ("last_root", "last_model")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**load_settings(path), **allowed}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 기억하지 못해도 앱은 정상 동작한다
