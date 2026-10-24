"""강사용: 수강생별 OpenRouter API 키를 한도/만료일과 함께 일괄 발급한다.

  export OPENROUTER_MANAGEMENT_KEY=...     # 관리용 키 (https://openrouter.ai/settings/management-keys)
  python tools/issue_keys.py --names names.txt --limit 3 --days 3 --prefix 11월반 --out keys.csv

names.txt: 수강생 이름/ID를 한 줄에 하나씩. 발급된 키는 이때만 볼 수 있으므로 CSV를 안전하게 보관하세요.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://openrouter.ai/api/v1/keys"


def expiry(days: float, now: datetime | None = None) -> str:
    when = (now or datetime.now(timezone.utc)) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")  # 초 단위까지 필요 (분 단위는 거절됨)


def read_names(path: Path) -> list[str]:
    names = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return [n for n in names if n and not n.startswith("#")]


def create_key(management_key: str, name: str, limit: float, expires_at: str, http: Any = None) -> str:
    response = (http or httpx).post(
        API_URL,
        headers={"Authorization": f"Bearer {management_key}"},
        json={"name": name, "limit": limit, "expires_at": expires_at},
        timeout=30,
    )
    if response.status_code not in (200, 201):
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise RuntimeError(f"HTTP {response.status_code} {message}".strip())
    key = response.json().get("key")
    if not key:
        raise RuntimeError("응답에 key가 없습니다.")
    return key


def main(argv: list[str] | None = None, http: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--names", type=Path, required=True)
    parser.add_argument("--limit", type=float, required=True, help="키당 사용 한도(USD)")
    parser.add_argument("--days", type=float, required=True, help="발급 시점부터 유효한 일수")
    parser.add_argument("--prefix", default="vibe", help="키 이름 앞에 붙일 말")
    parser.add_argument("--out", type=Path, default=Path("keys.csv"))
    parser.add_argument("--dry-run", action="store_true", help="발급하지 않고 계획만 출력")
    args = parser.parse_args(argv)

    if args.limit <= 0 or args.days <= 0:
        print("--limit와 --days는 0보다 커야 합니다.", file=sys.stderr)
        return 2
    names = read_names(args.names)
    if not names:
        print("names 파일에 이름이 없습니다.", file=sys.stderr)
        return 2
    expires_at = expiry(args.days)
    print(f"{len(names)}명 · 키당 ${args.limit:g} · 만료 {expires_at}")
    if args.dry_run:
        for name in names:
            print(f"  (예정) {args.prefix}-{name}")
        return 0

    management_key = os.environ.get("OPENROUTER_MANAGEMENT_KEY", "").strip()
    if not management_key:
        print("환경변수 OPENROUTER_MANAGEMENT_KEY가 필요합니다.", file=sys.stderr)
        return 2
    if args.out.exists():
        print(f"{args.out}가 이미 있습니다. 덮어쓰지 않으려면 다른 --out을 지정하세요.", file=sys.stderr)
        return 2

    rows, failures = [], 0
    for name in names:
        key_name = f"{args.prefix}-{name}"
        try:
            rows.append([name, key_name, create_key(management_key, key_name, args.limit, expires_at, http), args.limit, expires_at])
            print(f"  ✅ {key_name}")
        except (RuntimeError, httpx.HTTPError) as exc:
            failures += 1
            print(f"  ❌ {key_name}: {exc}", file=sys.stderr)

    if rows:
        with args.out.open("w", newline="", encoding="utf-8-sig") as handle:  # 엑셀에서 한글이 깨지지 않게 BOM 포함
            writer = csv.writer(handle)
            writer.writerow(["이름", "키 이름", "API 키", "한도(USD)", "만료(UTC)"])
            writer.writerows(rows)
        try:
            args.out.chmod(0o600)
        except OSError:
            pass
        print(f"\n{len(rows)}개 발급 완료 → {args.out}  (키는 이 파일에서만 확인할 수 있어요)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
