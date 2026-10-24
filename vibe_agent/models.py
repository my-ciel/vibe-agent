"""모델 목록(강사가 models.json으로 관리)과 OpenRouter 공개 카탈로그(가격, 도구 지원 여부)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .llm import OPENROUTER_BASE_URL

MODELS_FILE = Path(__file__).resolve().parent.parent / "models.json"
FALLBACK_MODELS = [{"id": "openai/gpt-4o-mini", "label": "GPT-4o mini"}]


def load_model_choices(path: Path = MODELS_FILE) -> list[dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        models = [m for m in data["models"] if isinstance(m.get("id"), str) and m["id"].strip()]
        return models or FALLBACK_MODELS
    except (OSError, ValueError, KeyError, TypeError):
        return FALLBACK_MODELS


@dataclass
class ModelInfo:
    id: str
    prompt_per_m: float | None  # 입력 100만 토큰당 USD
    completion_per_m: float | None
    supports_tools: bool | None  # 알 수 없으면 None


def _per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def parse_catalog(payload: dict) -> dict[str, ModelInfo]:
    catalog: dict[str, ModelInfo] = {}
    for item in payload.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        pricing = item.get("pricing") or {}
        params = item.get("supported_parameters")
        catalog[model_id] = ModelInfo(
            id=model_id,
            prompt_per_m=_per_million(pricing.get("prompt")),
            completion_per_m=_per_million(pricing.get("completion")),
            supports_tools=("tools" in params) if isinstance(params, list) else None,
        )
    return catalog


_cache: tuple[float, dict[str, ModelInfo]] | None = None
CACHE_OK_SECONDS = 3600
CACHE_RETRY_SECONDS = 60


def fetch_catalog(base_url: str = OPENROUTER_BASE_URL, http: Any = None) -> dict[str, ModelInfo]:
    """공개 카탈로그를 가져온다. 실패하면 빈 dict (앱은 가격 표시 없이 계속 동작)."""
    global _cache
    now = time.monotonic()
    if _cache is not None:
        fetched_at, cached = _cache
        if now - fetched_at < (CACHE_OK_SECONDS if cached else CACHE_RETRY_SECONDS):
            return cached
    try:
        response = (http or httpx).get(f"{base_url}/models", timeout=5)
        catalog = parse_catalog(response.json()) if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        catalog = {}
    _cache = (now, catalog)
    return catalog
