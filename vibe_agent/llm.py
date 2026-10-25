"""OpenRouter(OpenAI 호환 API) 스트리밍 클라이언트.

에이전트 루프는 stream()이 내놓는 StreamEvent만 알면 되므로 테스트에서는 가짜 클라이언트로 대체한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Protocol

import openai

from .cost import Usage
from .history import safe_id

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMError(Exception):
    """사용자에게 그대로 보여줄 수 있는 API 오류."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
                    id=safe_id(slot["id"]) if slot["id"] else f"call_{uuid.uuid4().hex[:12]}",
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
            message = str(error["message"])
            # OpenRouter는 "Provider returned error"처럼 뭉뚱그린 메시지 뒤에 제공자의 원문 오류를 metadata에 담는다.
            meta = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
            extra = []
            if meta.get("provider_name"):
                extra.append(f"제공자: {meta['provider_name']}")
            if meta.get("raw"):
                extra.append(f"원문: {str(meta['raw'])[:200]}")
            return (message + (f" [{' / '.join(extra)}]" if extra else ""))[:500]
    return str(getattr(exc, "message", None) or exc)[:300]


def translate_error(exc: Exception) -> LLMError:
    detail = _error_detail(exc)
    if isinstance(exc, openai.AuthenticationError):
        return LLMError("API 키가 올바르지 않거나 만료되었습니다. 키를 다시 확인해 주세요.")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        low = detail.lower()
        if status == 402:
            return LLMError("크레딧이 부족하거나 키의 사용 한도에 도달했습니다. 강사에게 문의해 주세요.", status=status)
        if status == 403:
            return LLMError(f"이 키로는 요청할 수 없습니다 (권한/지역 제한 또는 모델 접근 불가): {detail}", status=status)
        if status == 429:
            return LLMError("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", status=status)
        if status == 400 and "valid model" in low:
            return LLMError(f"모델 ID가 올바르지 않습니다. 사이드바에서 모델을 다시 선택하거나 철자를 확인하세요. ({detail})", status=status)
        # 도구 호출 미지원은 OpenRouter의 구체적인 문구로만 판단한다 ("tool"이라는 단어만으로는 다른 오류와 구분되지 않는다)
        if status == 404 or "support tool" in low or "tool use" in low:
            return LLMError(f"이 모델은 코딩 에이전트(도구 호출)를 지원하지 않거나 지금 이용할 수 없습니다. 다른 모델을 선택해 보세요. ({detail[:120]})", status=status)
        if status >= 500:
            return LLMError(f"OpenRouter/모델 제공자 서버 오류입니다. 잠시 후 다시 시도하세요. ({status})", status=status)
        hint = " 모델을 바꿔서 다시 보내거나, 계속되면 '새 대화'로 다시 시작해 보세요." if status == 400 else ""
        return LLMError(f"요청이 거절되었습니다 ({status}): {detail}{hint}", status=status)
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
