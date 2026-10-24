"""채팅 첨부 파일 처리.

첨부 파일은 작업 폴더의 attachments/ 에 저장해서 에이전트가 프로젝트에서 쓸 수 있게 하고,
모델에게는 종류에 따라 다르게 전달한다.
  - 이미지: 크기를 줄여 모델에게 직접 보여준다 (비전 모델만)
  - 텍스트/코드: 내용을 메시지에 포함한다
  - 그 외(pdf 등): 저장만 하고 경로를 알려준다
메시지에는 <vibe-attachment ...> 표식을 남겨서 화면에서 첨부를 다시 알아볼 수 있게 한다.
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from html import escape, unescape
from pathlib import Path
from typing import Any

from .sandbox import resolve_in_root

ATTACH_DIR = "attachments"
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
TEXT_EXTS = {
    "txt", "md", "py", "js", "jsx", "ts", "tsx", "html", "htm", "css", "json", "csv", "tsv",
    "yaml", "yml", "xml", "sql", "sh", "bat", "java", "c", "cpp", "h", "cs", "go", "rs", "php",
    "rb", "kt", "swift", "ini", "toml", "log", "svg",
}
OTHER_EXTS = {"pdf", "docx", "xlsx", "pptx"}  # 저장만 한다
ALLOWED_EXTS = sorted(IMAGE_EXTS | TEXT_EXTS | OTHER_EXTS)

MAX_FILE_BYTES = 10_000_000
MAX_IMAGES = 4
MAX_TEXT_CHARS = 60_000
MAX_IMAGE_SIDE = 1280

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_TAG = re.compile(r'<vibe-attachment path="(?P<path>[^"]*)" kind="(?P<kind>[a-z]+)"(?: note="(?P<note>[^"]*)")?\s*(?:/>|>(?P<body>.*?)</vibe-attachment>)', re.S)


@dataclass
class Upload:
    name: str
    data: bytes


@dataclass
class Prepared:
    content: str | list[dict[str, Any]]
    saved: list[str] = field(default_factory=list)  # 작업 폴더 기준 저장 경로
    notes: list[str] = field(default_factory=list)  # 사용자에게 보여줄 안내/경고


@dataclass
class AttachmentView:
    path: str
    kind: str  # image | text | file
    note: str = ""
    image: bytes | None = None


def safe_filename(name: str) -> str:
    """경로 구분자, 제어문자, 윈도우 예약어를 없앤 안전한 파일명."""
    base = re.split(r"[\\/]", name or "")[-1].strip().strip(".")
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = stem[:80] or "file"
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    return f"{stem}.{ext[:10]}" if ext else stem


def _unique_path(folder: Path, name: str) -> Path:
    candidate = folder / name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while candidate.exists():
        candidate = folder / (f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})")
        n += 1
    return candidate


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding).replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
    return None


def shrink_image(data: bytes) -> tuple[str, bytes]:
    """모델에 보낼 이미지를 줄인다 (토큰/비용 절약). 반환: (mime, bytes)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img.load()
        fmt = (img.format or "PNG").upper()
        if max(img.size) <= MAX_IMAGE_SIDE and fmt in ("PNG", "JPEG", "WEBP") and len(data) < 1_500_000:
            return f"image/{fmt.lower()}", data
        img = img.copy()
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(out, "PNG", optimize=True)
        return "image/png", out.getvalue()
    img.convert("RGB").save(out, "JPEG", quality=85)
    return "image/jpeg", out.getvalue()


def _tag(path: str, kind: str, note: str = "", body: str | None = None) -> str:
    note_attr = f' note="{escape(note, quote=True)}"' if note else ""
    head = f'<vibe-attachment path="{escape(path, quote=True)}" kind="{kind}"{note_attr}'
    return f"{head}>\n{body}\n</vibe-attachment>" if body is not None else f"{head}/>"


def prepare_message(text: str, uploads: list[Upload], root: Path, vision: bool | None = None) -> Prepared:
    """사용자 입력과 첨부를 모델에 보낼 메시지로 만든다. vision=False면 이미지를 모델에 보내지 않는다."""
    text = text.strip() or ("첨부한 파일을 확인해 주세요." if uploads else "")
    if not uploads:
        return Prepared(content=text)

    root = root.resolve()
    folder = resolve_in_root(root, ATTACH_DIR)
    prepared = Prepared(content=text)
    blocks: list[str] = []
    images: list[tuple[str, str]] = []  # (표식, data URL)

    for upload in uploads:
        name = safe_filename(upload.name)
        ext = name.rpartition(".")[2].lower() if "." in name else ""
        if len(upload.data) > MAX_FILE_BYTES:
            prepared.notes.append(f"'{upload.name}'은(는) {MAX_FILE_BYTES // 1_000_000}MB를 넘어서 첨부하지 않았어요.")
            continue
        if ext not in IMAGE_EXTS | TEXT_EXTS | OTHER_EXTS:
            prepared.notes.append(f"'{upload.name}'은(는) 지원하지 않는 형식이라 첨부하지 않았어요.")
            continue

        folder.mkdir(parents=True, exist_ok=True)
        target = _unique_path(folder, name)
        target.write_bytes(upload.data)
        rel = target.relative_to(root).as_posix()
        prepared.saved.append(rel)

        if ext in IMAGE_EXTS:
            if vision is False:
                blocks.append(_tag(rel, "image", "선택한 모델이 이미지를 볼 수 없어 내용은 전달되지 않았어요"))
                prepared.notes.append(f"'{upload.name}': 선택한 모델은 이미지를 볼 수 없어서 폴더에 저장만 했어요. 이미지 지원 모델로 바꿔 보세요.")
                continue
            if len(images) >= MAX_IMAGES:
                blocks.append(_tag(rel, "image", f"이미지는 한 번에 {MAX_IMAGES}장까지만 전달돼요"))
                prepared.notes.append(f"'{upload.name}': 이미지는 한 번에 {MAX_IMAGES}장까지만 모델에 전달돼요. 폴더에는 저장했어요.")
                continue
            try:
                mime, data = shrink_image(upload.data)
            except Exception:
                blocks.append(_tag(rel, "file", "이미지로 열 수 없는 파일이에요"))
                prepared.notes.append(f"'{upload.name}'은(는) 올바른 이미지 파일이 아니에요. 폴더에는 저장했어요.")
                continue
            images.append((_tag(rel, "image"), f"data:{mime};base64,{base64.b64encode(data).decode()}"))
        elif ext in TEXT_EXTS:
            content = _decode_text(upload.data)
            if content is None:
                blocks.append(_tag(rel, "file", "텍스트로 읽을 수 없는 파일이에요"))
                prepared.notes.append(f"'{upload.name}'은(는) 텍스트로 읽을 수 없어요. 폴더에는 저장했어요.")
                continue
            if len(content) > MAX_TEXT_CHARS:
                content = content[:MAX_TEXT_CHARS] + f"\n... (이하 {len(content) - MAX_TEXT_CHARS}자 생략. 전체는 read_file로 읽을 수 있어요)"
            blocks.append(_tag(rel, "text", body=content))
        else:
            blocks.append(_tag(rel, "file", "에이전트가 직접 읽을 수 없는 형식이에요. 필요하면 내용을 텍스트로 붙여넣어 주세요"))

    full_text = "\n\n".join([text, *blocks]) if blocks else text
    if images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": full_text}]
        for marker, url in images:
            parts.append({"type": "text", "text": marker})
            parts.append({"type": "image_url", "image_url": {"url": url}})
        prepared.content = parts
    else:
        prepared.content = full_text
    return prepared


def parse_user_content(content: str | list[dict[str, Any]]) -> tuple[str, list[AttachmentView]]:
    """저장된 사용자 메시지에서 (사용자가 쓴 글, 첨부 목록)을 복원한다."""
    texts: list[str] = []
    views: list[AttachmentView] = []
    pending_image: AttachmentView | None = None
    if isinstance(content, str):
        parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
    else:
        parts = content
    for part in parts:
        if part.get("type") == "image_url" and pending_image is not None:
            url = part["image_url"]["url"]
            try:
                pending_image.image = base64.b64decode(url.split(",", 1)[1])
            except (IndexError, ValueError):
                pass
            pending_image = None
        elif part.get("type") == "text":
            raw = part["text"]
            found = list(_TAG.finditer(raw))
            if found:
                texts.append(raw[: found[0].start()])
                for match in found:
                    view = AttachmentView(
                        unescape(match["path"]), match["kind"], unescape(match["note"] or "")
                    )
                    views.append(view)
                    if view.kind == "image" and not view.note:
                        pending_image = view
            else:
                texts.append(raw)
    return "".join(texts).strip(), views
