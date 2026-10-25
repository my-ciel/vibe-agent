import sys

import pytest

from vibe_agent.errors import ToolError
from vibe_agent.tools import ToolContext, build_tools

TOOLS = build_tools()


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(tmp_path.resolve())


def run(ctx, name, **args):
    return TOOLS[name].run(ctx, args)


def preview(ctx, name, **args):
    return TOOLS[name].preview(ctx, args)


def test_list_dir_skips_ignored_and_marks_dirs(ctx):
    (ctx.root / "src").mkdir()
    (ctx.root / "src" / "a.py").write_text("x")
    (ctx.root / "node_modules").mkdir()
    (ctx.root / "README.md").write_text("hi")
    out = run(ctx, "list_dir")
    assert out.splitlines() == ["src/", "README.md"]
    assert "a.py" in run(ctx, "list_dir", depth=2)
    assert "node_modules" not in out


def test_list_dir_errors(ctx):
    with pytest.raises(ToolError):
        run(ctx, "list_dir", path="nope")
    (ctx.root / "f.txt").write_text("x")
    with pytest.raises(ToolError):
        run(ctx, "list_dir", path="f.txt")


def test_read_file_numbers_lines_and_pages(ctx):
    (ctx.root / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = run(ctx, "read_file", path="f.txt", offset=3, limit=2)
    assert "     3\tline3" in out and "     4\tline4" in out and "line5" not in out
    assert "offset=5" in out
    assert run(ctx, "read_file", path="f.txt").count("\n") == 9  # 잘림 안내 없음


def test_read_file_rejects_binary_and_missing(ctx):
    (ctx.root / "b.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ToolError):
        run(ctx, "read_file", path="b.bin")
    with pytest.raises(ToolError):
        run(ctx, "read_file", path="missing.txt")


def test_read_file_cp949(ctx):
    (ctx.root / "k.txt").write_bytes("안녕하세요".encode("cp949"))
    assert "안녕하세요" in run(ctx, "read_file", path="k.txt")


def test_grep(ctx):
    (ctx.root / "a.py").write_text("def foo():\n    pass\n")
    (ctx.root / "b.txt").write_text("foo bar\n")
    out = run(ctx, "grep", pattern="foo", glob="*.py")
    assert out == "a.py:1: def foo():"
    assert "b.txt" in run(ctx, "grep", pattern="FOO", ignore_case=True)
    assert run(ctx, "grep", pattern="zzz") == "(일치하는 결과 없음)"
    with pytest.raises(ToolError):
        run(ctx, "grep", pattern="(")


def test_write_file_creates_with_parents_and_previews(ctx):
    pv = preview(ctx, "write_file", path="app/main.py", content="print(1)\n")
    assert "새 파일" in pv and "print(1)" in pv
    assert not (ctx.root / "app").exists()  # 미리보기는 쓰지 않는다
    run(ctx, "write_file", path="app/main.py", content="print(1)\n")
    assert (ctx.root / "app" / "main.py").read_text() == "print(1)\n"


def test_write_file_overwrite_shows_diff(ctx):
    (ctx.root / "a.txt").write_text("one\ntwo\n")
    pv = preview(ctx, "write_file", path="a.txt", content="one\nTWO\n")
    assert "-two" in pv and "+TWO" in pv


def test_edit_file_unique_match(ctx):
    (ctx.root / "a.py").write_text("x = 1\ny = 2\n")
    run(ctx, "edit_file", path="a.py", old_string="y = 2", new_string="y = 3")
    assert (ctx.root / "a.py").read_text() == "x = 1\ny = 3\n"


def test_edit_file_ambiguous_and_missing(ctx):
    (ctx.root / "a.py").write_text("a\na\n")
    with pytest.raises(ToolError, match="2곳"):
        run(ctx, "edit_file", path="a.py", old_string="a", new_string="b")
    run(ctx, "edit_file", path="a.py", old_string="a", new_string="b", replace_all=True)
    assert (ctx.root / "a.py").read_text() == "b\nb\n"
    with pytest.raises(ToolError, match="찾지 못"):
        run(ctx, "edit_file", path="a.py", old_string="zzz", new_string="b")
    with pytest.raises(ToolError):
        run(ctx, "edit_file", path="nope.py", old_string="a", new_string="b")


def test_edit_preserves_crlf_and_bom(ctx):
    (ctx.root / "w.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    run(ctx, "edit_file", path="w.txt", old_string="one\ntwo", new_string="one\nTWO")
    assert (ctx.root / "w.txt").read_bytes() == b"\xef\xbb\xbfone\r\nTWO\r\n"


def test_edit_preserves_cp949(ctx):
    (ctx.root / "k.txt").write_bytes("안녕\n세상\n".encode("cp949"))
    run(ctx, "edit_file", path="k.txt", old_string="세상", new_string="월드")
    assert (ctx.root / "k.txt").read_bytes() == "안녕\n월드\n".encode("cp949")


def test_write_outside_root_and_git_blocked(ctx):
    with pytest.raises(ToolError):
        run(ctx, "write_file", path="../evil.txt", content="x")
    with pytest.raises(ToolError):
        run(ctx, "write_file", path=".git/config", content="x")


def test_run_command_output_and_exit_code(ctx):
    out = run(ctx, "run_command", command=f'"{sys.executable}" -c "print(\'안녕\')"')
    assert "[종료 코드 0]" in out and "안녕" in out
    out = run(ctx, "run_command", command=f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    assert "[종료 코드 3]" in out


def test_run_command_runs_in_root(ctx):
    (ctx.root / "marker.txt").write_text("x")
    cmd = f'"{sys.executable}" -c "import os; print(os.listdir(\'.\'))"'
    assert "marker.txt" in run(ctx, "run_command", command=cmd)


def test_run_command_timeout_kills_process(ctx):
    cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    out = run(ctx, "run_command", command=cmd, timeout=1)
    assert "시간 초과" in out


def test_run_command_blocked_before_approval(ctx):
    with pytest.raises(ToolError):
        preview(ctx, "run_command", command="sudo rm -rf /")


def test_stdin_is_closed(ctx):
    cmd = f'"{sys.executable}" -c "print(repr(input()))"'
    assert "EOFError" in run(ctx, "run_command", command=cmd, timeout=10)


def test_bad_argument_types(ctx):
    with pytest.raises(ToolError):
        run(ctx, "read_file", path=123)
    with pytest.raises(ToolError):
        run(ctx, "read_file")


def test_read_file_hints_attachments_folder(ctx):
    (ctx.root / "attachments").mkdir()
    (ctx.root / "attachments" / "sales.csv").write_text("a")
    with pytest.raises(ToolError, match="attachments/sales.csv"):
        run(ctx, "read_file", path="sales.csv")


# ---- 덮어쓰기 충돌 방지 (ToolContext에 read_state가 있을 때만 작동) ----

import time as _time


@pytest.fixture
def sctx(tmp_path):
    return ToolContext(tmp_path.resolve(), None, {})


def _touch_later(path, text):
    _time.sleep(0.02)
    path.write_text(text, encoding="utf-8")


def test_overwriting_unread_existing_file_is_blocked(sctx):
    (sctx.root / "a.txt").write_text("기존")
    with pytest.raises(ToolError, match="아직 읽지 않았어요"):
        TOOLS["write_file"].run(sctx, {"path": "a.txt", "content": "덮어씀"})
    run(sctx, "read_file", path="a.txt")
    TOOLS["write_file"].run(sctx, {"path": "a.txt", "content": "덮어씀"})
    assert (sctx.root / "a.txt").read_text() == "덮어씀"


def test_file_changed_after_read_is_blocked_for_write_and_edit(sctx):
    f = sctx.root / "a.txt"
    f.write_text("원본")
    run(sctx, "read_file", path="a.txt")
    _touch_later(f, "다른 대화가 고침")
    with pytest.raises(ToolError, match="바뀌었어요"):
        TOOLS["write_file"].run(sctx, {"path": "a.txt", "content": "x"})
    with pytest.raises(ToolError, match="바뀌었어요"):
        TOOLS["edit_file"].run(sctx, {"path": "a.txt", "old_string": "고침", "new_string": "x"})
    assert f.read_text() == "다른 대화가 고침"            # 아무것도 덮어쓰지 않았다
    run(sctx, "read_file", path="a.txt")                    # 다시 읽으면 통과
    TOOLS["edit_file"].run(sctx, {"path": "a.txt", "old_string": "고침", "new_string": "수정"})


def test_files_the_agent_wrote_are_known_and_new_files_are_free(sctx):
    TOOLS["write_file"].run(sctx, {"path": "new.txt", "content": "1"})        # 새 파일은 읽을 필요 없음
    TOOLS["write_file"].run(sctx, {"path": "new.txt", "content": "2"})        # 자기가 쓴 파일은 다시 써도 됨
    TOOLS["edit_file"].run(sctx, {"path": "new.txt", "old_string": "2", "new_string": "3"})
    assert (sctx.root / "new.txt").read_text() == "3"


def test_edit_of_unread_file_is_allowed_when_text_matches(sctx):
    (sctx.root / "a.txt").write_text("hello")
    TOOLS["edit_file"].run(sctx, {"path": "a.txt", "old_string": "hello", "new_string": "bye"})


def test_preview_also_reports_conflict_before_approval(sctx):
    (sctx.root / "a.txt").write_text("기존")
    with pytest.raises(ToolError, match="아직 읽지 않았어요"):
        TOOLS["write_file"].preview(sctx, {"path": "a.txt", "content": "x"})
