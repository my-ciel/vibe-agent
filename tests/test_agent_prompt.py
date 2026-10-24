from vibe_agent.instructions import load_instructions
from vibe_agent.prompts import build_system_prompt


def test_prompt_has_quality_guidance_and_no_approval_chatter(tmp_path):
    prompt = build_system_prompt(tmp_path)
    assert "localStorage" in prompt and "no CDN" in prompt and "Never ask for permission" in prompt


def test_instructor_instructions_are_appended(tmp_path):
    assert "수업 주제" in build_system_prompt(tmp_path, "수업 주제는 포트폴리오")


def test_load_instructions_strips_comments_and_handles_missing(tmp_path):
    f = tmp_path / "i.md"
    f.write_text("<!-- 안내\n여러 줄 -->\n학습 모드로 설명하세요.\n<!-- 끝 -->", encoding="utf-8")
    assert load_instructions(f) == "학습 모드로 설명하세요."
    assert load_instructions(tmp_path / "none.md") == ""


def test_shipped_instructions_file_is_empty_by_default():
    assert load_instructions() == ""
