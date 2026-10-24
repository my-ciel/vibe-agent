"""터미널에서 에이전트를 시험해 보는 도구. (python -m vibe_agent.cli --root 폴더)"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from .cost import CostTracker
from .llm import OpenRouterClient
from .loop import (
    Agent,
    ApprovalRequest,
    Done,
    ErrorEvent,
    TextDelta,
    ToolResult,
    ToolStart,
    UsageUpdate,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="vibe-agent 터미널 데모")
    parser.add_argument("--root", default=".", help="작업 폴더")
    parser.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"))
    parser.add_argument("--limit-usd", type=float, default=1.0, help="세션 비용 한도(USD)")
    args = parser.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY") or getpass.getpass("OpenRouter API 키: ")
    agent = Agent(
        llm=OpenRouterClient(key),
        model=args.model,
        root=Path(args.root),
        tracker=CostTracker(limit_usd=args.limit_usd),
    )
    print(f"작업 폴더: {agent.root}\n모델: {agent.model}\n종료: 빈 줄 입력\n")

    while True:
        text = input("나> ").strip()
        if not text:
            return
        agent.submit(text)
        while True:
            paused = False
            for event in agent.run():
                if isinstance(event, TextDelta):
                    print(event.text, end="", flush=True)
                elif isinstance(event, ToolStart):
                    print(f"\n  [{event.call.name}] {event.args}")
                elif isinstance(event, ApprovalRequest):
                    print(f"\n  [{event.call.name}] 승인 필요:\n{event.preview}")
                    answer = input("  실행할까요? [y/N] ").strip().lower()
                    agent.decide(event.call.id, answer == "y")
                    paused = True
                elif isinstance(event, ToolResult):
                    shown = event.output if len(event.output) < 500 else event.output[:500] + "..."
                    print(f"  → {shown}")
                elif isinstance(event, UsageUpdate):
                    t = event.tracker
                    print(f"\n  (토큰 {t.total_tokens:,} / ${t.cost_usd:.4f})", end="")
                elif isinstance(event, ErrorEvent):
                    print(f"\n[오류] {event.message}")
                elif isinstance(event, Done) and event.message:
                    print(f"\n[{event.reason}] {event.message}")
            if not paused:
                break
        print()


if __name__ == "__main__":
    main()
