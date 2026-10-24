import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import issue_keys  # noqa: E402


class FakeHttp:
    def __init__(self, fail_for=()):
        self.calls, self.fail_for = [], fail_for

    def post(self, url, headers, json, timeout):
        self.calls.append((url, headers, json))
        if json["name"] in self.fail_for:
            return SimpleNamespace(status_code=400, json=lambda: {"error": {"message": "bad"}})
        return SimpleNamespace(status_code=201, json=lambda: {"data": {}, "key": f"sk-or-v1-{json['name']}"})


def names_file(tmp_path, text="# 목록\n김철수\n\n이영희\n"):
    path = tmp_path / "names.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_expiry_format_has_seconds():
    when = issue_keys.expiry(3, datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc))
    assert when == "2026-11-04T09:00:00Z" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", when)


def test_read_names_skips_blank_and_comments(tmp_path):
    assert issue_keys.read_names(names_file(tmp_path)) == ["김철수", "이영희"]


def test_issues_keys_and_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "mgmt")
    out, http = tmp_path / "keys.csv", FakeHttp()
    code = issue_keys.main(["--names", str(names_file(tmp_path)), "--limit", "3", "--days", "2", "--prefix", "반", "--out", str(out)], http=http)
    assert code == 0
    url, headers, body = http.calls[0]
    assert url.endswith("/keys") and headers["Authorization"] == "Bearer mgmt"
    assert body["name"] == "반-김철수" and body["limit"] == 3.0 and body["expires_at"].endswith("Z")
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert rows[1][:3] == ["김철수", "반-김철수", "sk-or-v1-반-김철수"] and len(rows) == 3


def test_partial_failure_returns_nonzero_but_keeps_successes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "mgmt")
    out = tmp_path / "keys.csv"
    code = issue_keys.main(["--names", str(names_file(tmp_path)), "--limit", "1", "--days", "1", "--prefix", "p", "--out", str(out)], http=FakeHttp(fail_for={"p-이영희"}))
    assert code == 1 and "p-이영희" in capsys.readouterr().err
    assert len(list(csv.reader(out.open(encoding="utf-8-sig")))) == 2


def test_dry_run_needs_no_key_and_issues_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)
    http = FakeHttp()
    assert issue_keys.main(["--names", str(names_file(tmp_path)), "--limit", "1", "--days", "1", "--dry-run"], http=http) == 0
    assert not http.calls


def test_refuses_without_key_or_to_overwrite(tmp_path, monkeypatch):
    names = names_file(tmp_path)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)
    assert issue_keys.main(["--names", str(names), "--limit", "1", "--days", "1"], http=FakeHttp()) == 2
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "m")
    existing = tmp_path / "keys.csv"
    existing.write_text("keep")
    http = FakeHttp()
    assert issue_keys.main(["--names", str(names), "--limit", "1", "--days", "1", "--out", str(existing)], http=http) == 2
    assert existing.read_text() == "keep" and not http.calls


@pytest.mark.parametrize("args", [["--limit", "0", "--days", "1"], ["--limit", "1", "--days", "-1"]])
def test_rejects_non_positive_values(tmp_path, args):
    assert issue_keys.main(["--names", str(names_file(tmp_path)), *args], http=FakeHttp()) == 2
