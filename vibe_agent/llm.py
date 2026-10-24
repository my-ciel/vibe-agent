"""OpenRouter(OpenAI 호환 API) 스트리밍 클라이언트.

에이전트 루프는 stream()이 내놓는 StreamEvent만 알면 되므로 테스트에서는 가짜 클라이언트로 대체한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Protocol

import openai

from .cost import Usage

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMError(Exception):
    """사용자에게 그대로 보여줄 수 있는 API 오류."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 문자열 (모델이 만든 그대로)


@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCallsReady:
    calls: list[ToolCall]


@dataclass
class UsageInfo:
    usage: Usage


@dataclass
class Finished:
    reason: str | None


StreamEvent = TextChunk | ToolCallsReady | UsageInfo | Finished


class LLMClient(Protocol):
    def stream(
        self, messages: list[dict], tools: list[dict], model: str
    ) -> Iterator[StreamEvent]: ...


def parse_stream(chunks: Iterable[Any]) -> Iterator[StreamEvent]:
    """openai SDK의 스트림 청크를 StreamEvent로 변환한다."""
    partial: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: Usage | None = None

    for chunk in chunks:
        error = getattr(chunk, "error", None)  # 스트림 도중 오류
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise LLMError(f"모델 응답 중 오류가 발생했습니다: {message}")
        if getattr(chunk, "usage", None):
            u = chunk.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                cost=getattr(u, "cost", None),
            )
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if getattr(delta, "content", None):
            yield TextChunk(delta.content)
        for tc in getattr(delta, "tool_calls", None) or []:
            slot = partial.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                slot["name"] += fn.name or ""
                slot["arguments"] += fn.arguments or ""
        if choice.finish_reason:
            finish_reason = choice.finish_reason

    if partial:
        yield ToolCallsReady(
            [
                ToolCall(
                    id=slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    name=slot["name"],
                    arguments=slot["arguments"],
                )
                for _, slot in sorted(partial.items())
            ]
        )
    if usage is not None:
        yield UsageInfo(usage)
    yield Finished(finish_reason)


def _error_detail(exc: Exception) -> str:
    """응답 본문의 error.message만 꺼낸다 (없으면 예외 문자열)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        # openai SDK는 응답의 error 항목만 body로 주지만, 전체 본문이 오는 경우도 대비한다.
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        if error.get("message"):
            return str(error["message"])[:300]
    return str(getattr(exc, "message", None) or exc)[:300]


def translate_error(exc: Exception) -> LLMError:
    detail = _error_detail(exc)
    if isinstance(exc, openai.AuthenticationError):
        return LLMError("API 키가 올바르지 않거나 만료되었습니다. 키를 다시 확인해 주세요.")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status == 402:
            return LLMError("크레딧이 부족하거나 키의 사용 한도에 도달했습니다. 강사에게 문의해 주세요.")
        if status == 403:
            return LLMError(f"이 키로는 요청할 수 없습니다 (권한/지역 제한 또는 모델 접근 불가): {detail}")
        if status == 429:
            return LLMError("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
        if status == 400 and "valid model" in detail.lower():
            return LLMError(f"모델 ID가 올바르지 않습니다. 사이드바에서 모델을 다시 선택하거나 철자를 확인하세요. ({detail})")
        if status == 404 or (status == 400 and "tool" in detail.lower()):
            return LLMError(f"이 모델은 코딩 에이전트(도구 호출)를 지원하지 않거나 지금 이용할 수 없습니다. 다른 모델을 선택해 보세요. ({detail[:120]})")
        if status >= 500:
            return LLMError(f"OpenRouter/모델 제공자 서버 오류입니다. 잠시 후 다시 시도하세요. ({status})")
        return LLMError(f"요청이 거절되었습니다 ({status}): {detail}")
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return LLMError("OpenRouter에 연결할 수 없습니다. 인터넷 연결을 확인해 주세요.")
    return LLMError(f"예상하지 못한 오류: {type(exc).__name__}: {detail}")


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str = OPENROUTER_BASE_URL, client: Any = None):
        self._client = client or openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=120,
            max_retries=2,
            default_headers={"X-Title": "vibe-agent"},
        )

    def stream(self, messages: list[dict], tools: list[dict], model: str) -> Iterator[StreamEvent]:
        try:
            chunks = self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                stream=True,
                stream_options={"include_usage": True},
                extra_body={"usage": {"include": True}},  # 응답에 실제 비용(USD)을 포함시킨다
            )
            yield from parse_stream(chunks)
        except LLMError:
            raise
        except openai.OpenAIError as exc:
            raise translate_error(exc) from exc


@dataclass
class KeyInfo:
    label: str = ""
    limit: float | None = None  # 키에 설정된 한도(USD). 없으면 None
    usage: float = 0.0
    limit_remaining: float | None = None
    is_free_tier: bool = False


def fetch_key_info(api_key: str, base_url: str = OPENROUTER_BASE_URL, http: Any = None) -> KeyInfo:
    """키가 유효한지, 한도가 얼마나 남았는지 확인한다."""
    import httpx

    try:
        response = (http or httpx).get(
            f"{base_url}/auth/key",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
    except httpx.HTTPError:
        raise LLMError("OpenRouter에 연결할 수 없습니다. 인터넷 연결을 확인해 주세요.") from None
    if response.status_code in (401, 403):
        raise LLMError("API 키가 올바르지 않거나 만료되었습니다. 키를 다시 확인해 주세요.")
    if response.status_code != 200:
        raise LLMError(f"키 정보를 확인하지 못했습니다 (HTTP {response.status_code}).")
    data = response.json().get("data") or {}
    return KeyInfo(
        label=str(data.get("label") or ""),
        limit=data.get("limit"),
        usage=float(data.get("usage") or 0),
        limit_remaining=data.get("limit_remaining"),
        is_free_tier=bool(data.get("is_free_tier")),
    )
