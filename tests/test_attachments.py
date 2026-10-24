import base64
import io

import pytest
from PIL import Image

from vibe_agent import attachments as att
from vibe_agent.attachments import Upload, parse_user_content, prepare_message, safe_filename
from vibe_agent.transcript import build_turns


def png(size=(40, 30), color=(200, 30, 30), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\logo.png", "logo.png"),
        ("a/b/c.txt", "c.txt"),
        ("CON.txt", "_CON.txt"),
        ("na<me>:?.py", "na_me___.py"),
        ("...", "file"),
        ("", "file"),
        ("한글 이름.csv", "한글 이름.csv"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_no_uploads_returns_plain_text(tmp_path):
    prepared = prepare_message("  안녕  ", [], tmp_path)
    assert prepared.content == "안녕" and not prepared.saved and not (tmp_path / "attachments").exists()


def test_text_file_is_inlined_and_saved(tmp_path):
    prepared = prepare_message("평균 구해줘", [Upload("data.csv", "a,b\n1,2\n".encode())], tmp_path)
    assert isinstance(prepared.content, str)
    assert "평균 구해줘" in prepared.content and "a,b\n1,2" in prepared.content
    assert prepared.saved == ["attachments/data.csv"]
    assert (tmp_path / "attachments" / "data.csv").read_bytes() == b"a,b\n1,2\n"


def test_cp949_text_is_decoded(tmp_path):
    prepared = prepare_message("읽어줘", [Upload("메모.txt", "안녕하세요".encode("cp949"))], tmp_path)
    assert "안녕하세요" in prepared.content


def test_long_text_is_truncated(tmp_path):
    big = "x" * (att.MAX_TEXT_CHARS + 500)
    prepared = prepare_message("", [Upload("big.txt", big.encode())], tmp_path)
    assert "500자 생략" in prepared.content and len(prepared.content) < att.MAX_TEXT_CHARS + 400


def test_image_becomes_multimodal_content(tmp_path):
    prepared = prepare_message("이렇게 만들어줘", [Upload("design.png", png())], tmp_path, vision=True)
    kinds = [p["type"] for p in prepared.content]
    assert kinds == ["text", "text", "image_url"]
    assert prepared.content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (tmp_path / "attachments" / "design.png").exists()


def test_large_image_is_downscaled_but_original_saved(tmp_path):
    original = png((3000, 2000))
    prepared = prepare_message("", [Upload("big.png", original)], tmp_path, vision=True)
    data = base64.b64decode(prepared.content[2]["image_url"]["url"].split(",", 1)[1])
    assert max(Image.open(io.BytesIO(data)).size) <= att.MAX_IMAGE_SIDE
    assert (tmp_path / "attachments" / "big.png").read_bytes() == original


def test_image_not_sent_when_model_cannot_see(tmp_path):
    prepared = prepare_message("봐줘", [Upload("a.png", png())], tmp_path, vision=False)
    assert isinstance(prepared.content, str) and "볼 수 없어" in prepared.content
    assert prepared.notes and (tmp_path / "attachments" / "a.png").exists()


def test_image_limit(tmp_path):
    uploads = [Upload(f"{i}.png", png()) for i in range(att.MAX_IMAGES + 2)]
    prepared = prepare_message("", uploads, tmp_path, vision=True)
    assert sum(p["type"] == "image_url" for p in prepared.content) == att.MAX_IMAGES
    assert len(prepared.notes) == 2 and len(prepared.saved) == att.MAX_IMAGES + 2


def test_fake_image_is_rejected_but_saved(tmp_path):
    prepared = prepare_message("", [Upload("evil.png", b"not an image")], tmp_path, vision=True)
    assert isinstance(prepared.content, str) and prepared.notes


def test_unsupported_and_oversized_are_skipped(tmp_path):
    prepared = prepare_message(
        "x", [Upload("run.exe", b"MZ"), Upload("huge.txt", b"a" * (att.MAX_FILE_BYTES + 1))], tmp_path
    )
    assert prepared.content == "x" and len(prepared.notes) == 2 and not (tmp_path / "attachments").exists()


def test_same_name_gets_unique_path_and_traversal_name_is_contained(tmp_path):
    prepared = prepare_message("", [Upload("a.txt", b"1"), Upload("a.txt", b"2"), Upload("../../evil.txt", b"3")], tmp_path)
    assert prepared.saved == ["attachments/a.txt", "attachments/a (2).txt", "attachments/evil.txt"]
    assert not (tmp_path.parent / "evil.txt").exists()


def test_pdf_is_saved_only(tmp_path):
    prepared = prepare_message("", [Upload("doc.pdf", b"%PDF-1.4")], tmp_path)
    assert "직접 읽을 수 없는" in prepared.content and (tmp_path / "attachments" / "doc.pdf").exists()


def test_round_trip_through_transcript(tmp_path):
    original = png((20, 20))
    prepared = prepare_message(
        "<b>태그</b>도 그대로", [Upload("s.png", original), Upload("n.txt", "메모".encode()), Upload("d.pdf", b"x")], tmp_path, vision=True
    )
    text, views = parse_user_content(prepared.content)
    assert text == "<b>태그</b>도 그대로"
    assert [(v.path, v.kind) for v in views] == [("attachments/n.txt", "text"), ("attachments/d.pdf", "file"), ("attachments/s.png", "image")]
    image = next(v for v in views if v.kind == "image")
    assert Image.open(io.BytesIO(image.image)).size == (20, 20)

    turns = build_turns([{"role": "user", "content": prepared.content}])
    assert turns[0].blocks[0].text == text and [b.attachment.path for b in turns[0].blocks[1:]] == [v.path for v in views]


def test_plain_user_message_unchanged_in_transcript():
    turns = build_turns([{"role": "user", "content": "그냥 질문"}])
    assert [b.text for b in turns[0].blocks] == ["그냥 질문"]
