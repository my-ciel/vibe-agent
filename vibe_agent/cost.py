from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None  # USD. OpenRouter가 알려주지 않으면 None


@dataclass
class CostTracker:
    """세션 누적 사용량. limit_usd / max_tokens를 넘으면 check()가 사유를 돌려준다."""

    limit_usd: float | None = None
    max_tokens: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0  # 비용이 보고되지 않은 호출 수 (한도 적용이 부정확할 수 있음)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, usage: Usage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        if usage.cost is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += usage.cost

    def check(self) -> str | None:
        if self.limit_usd is not None and self.cost_usd >= self.limit_usd:
            return f"세션 비용 한도(${self.limit_usd:.2f})에 도달했습니다. (사용: ${self.cost_usd:.4f})"
        if self.max_tokens is not None and self.total_tokens >= self.max_tokens:
            return f"세션 토큰 한도({self.max_tokens:,})에 도달했습니다. (사용: {self.total_tokens:,})"
        return None
