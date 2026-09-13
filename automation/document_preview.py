"""Safe, bounded text previews for local dashboard attachments.

This module never downloads files and never sends extracted content to Hermes.
It only reads a local attachment that the dashboard already has on disk.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import re
import stat
import zlib
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import parser as hwpx_parser

MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_CHARS = 6000
MAX_PDF_PAGES = 5
MAX_PDF_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_PDF_PREVIEW_DIMENSION = 1600


class _CorruptPreview(ValueError):
    """The source container is malformed or fails complete validation."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class _PreviewFailure(RuntimeError):
    """A supported parser/renderer could not produce a bounded preview."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|password|passwd|pwd|"
    r"api[-_ ]?(?:key|token)|access[-_]?token|refresh[-_]?token|"
    r"session[-_]?(?:id|token|cookie)?|cookie|cookies|credential|credentials|"
    r"secret|private[-_ ]+key)\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL = re.compile(r"https?://[^\s<>\"']+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_preview_text(value: Any) -> str:
    """Return bounded display text with obvious secrets and URLs redacted."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _SECRET_ASSIGNMENT.sub("[민감정보 생략]", text)
    text = _BEARER.sub("Bearer [민감정보 생략]", text)
    text = _URL.sub("[URL 생략]", text)
    text = _CONTROL.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS].rstrip() + "\n… (미리보기는 6000자까지 표시)"
    return text


def _decode_image(source: Path, extension: str) -> dict[str, Any]:
    """Verify and fully decode an image before exposing it to a consumer."""
    try:
        pil_image = importlib.import_module("PIL.Image")
    except (ImportError, ModuleNotFoundError) as exc:
        raise _PreviewFailure(
            "image_decoder_unavailable",
            "검토된 로컬 이미지 디코더(Pillow)를 사용할 수 없습니다.",
        ) from exc

    try:
        with pil_image.open(source) as image:
            image.verify()
            image_format = str(image.format or "").upper()
            width, height = image.size
        # verify() invalidates the decoder, so reopen and consume every pixel.
        with pil_image.open(source) as image:
            image.load()
            if image.size != (width, height):
                raise _CorruptPreview("invalid_image_dimensions", "이미지 크기를 확인하지 못했습니다.")
    except _CorruptPreview:
        raise
    except (SyntaxError, ValueError, OSError) as exc:
        raise _CorruptPreview(
            "invalid_image_data",
            "이미지 전체 데이터를 검증·디코드하지 못했습니다.",
        ) from exc
    if width <= 0 or height <= 0 or not image_format:
        raise _CorruptPreview("invalid_image_dimensions", "이미지 크기가 비어 있거나 형식을 확인할 수 없습니다.")
    expected = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF"}[extension]
    if image_format != expected:
        raise _CorruptPreview("image_format_mismatch", "확장자와 실제 이미지 형식이 일치하지 않습니다.")
    return {
        "format": image_format,
        "width": width,
        "height": height,
        "display": "local",
        "ref": str(source),
    }


def _preview_result(extension: str, *, status: str, message: str, reason: str | None = None, parser: str | None = None) -> dict[str, Any]:
    return {
        "file_type": extension,
        "status": status,
        "parser": parser,
        "text": "",
        "message": message,
        "failure_reason": reason,
    }


def _is_locked(source: Path) -> bool:
    try:
        mode = source.stat().st_mode
    except PermissionError:
        return True
    except OSError:
        return False
    # Acceptance runs may execute as root, where chmod(0) remains readable.
    return not bool(mode & stat.S_IRUSR | mode & stat.S_IRGRP | mode & stat.S_IROTH)


def preview_attachment(path: Path | str, file_type: str | None = None) -> dict[str, Any]:
    """Extract a bounded local preview with an explicit processing outcome."""
    source = Path(path)
    extension = (file_type or source.suffix.lstrip(".")).casefold()
    result = _preview_result(
        extension,
        status="unsupported",
        reason="unsupported_format",
        message="이 파일 형식은 현재 미리보기를 지원하지 않습니다.",
    )
    if not source.is_file():
        return result | {
            "status": "missing",
            "failure_reason": "source_missing",
            "message": "첨부 원본을 로컬에서 찾을 수 없습니다.",
        }
    if _is_locked(source):
        return result | {
            "status": "locked",
            "failure_reason": "source_access_denied",
            "message": "첨부 원본에 접근할 수 없어 미리보기를 만들지 못했습니다.",
        }
    try:
        if source.stat().st_size > MAX_PREVIEW_BYTES:
            return result | {
                "status": "too_large",
                "failure_reason": "preview_size_limit",
                "message": "안전 한도(8MB)를 넘어 미리보기를 생략했습니다.",
            }
        metadata: dict[str, Any] = {}
        visual_success = False
        if extension == "hwpx":
            _validate_hwpx_container(source)
            text, parser_name, message = _preview_hwpx(source)
            metadata["representation"] = (
                "structured_fallback" if parser_name == "hwpx-preview-v1" else "structured"
            )
        elif extension == "pdf":
            text, parser_name, message, metadata = _preview_pdf(source)
            visual_success = bool(metadata.get("pages"))
        elif extension == "xlsx":
            _validate_xlsx_container(source)
            text, parser_name, message, metadata = _preview_xlsx(source)
        elif extension == "xls":
            return result | {
                "failure_reason": "legacy_xls_unsupported",
                "message": "구형 XLS는 외부 프로그램 없이 안전하게 표를 읽을 수 없습니다.",
            }
        elif extension in {"jpg", "jpeg", "png", "gif"}:
            image = _decode_image(source, extension)
            return result | {
                "status": "parsed",
                "parser": "pillow-verify-v1",
                "message": "전체 데이터가 검증된 이미지를 로컬에서 표시할 수 있습니다.",
                "failure_reason": None,
                "kind": "image",
                "representation": "local_image",
                "local_path": str(source),
                "image": image,
                "metadata": {"representation": "local_image", "image": image},
            }
        elif extension == "hwp":
            return result | {
                "failure_reason": "legacy_hwp_unsupported",
                "message": "구형 HWP는 별도 변환기 없이 안전하게 미리보기할 수 없습니다.",
            }
        else:
            return result
    except PermissionError:
        return result | {
            "status": "locked",
            "failure_reason": "source_access_denied",
            "message": "첨부 원본에 접근할 수 없어 미리보기를 만들지 못했습니다.",
        }
    except _CorruptPreview as exc:
        return result | {
            "status": "corrupt",
            "failure_reason": exc.reason,
            "message": exc.message,
        }
    except _PreviewFailure as exc:
        return result | {
            "status": "preview_failed",
            "failure_reason": exc.reason,
            "message": exc.message,
        }
    except (OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile, zlib.error, ElementTree.ParseError) as exc:
        return result | {
            "status": "preview_failed",
            "failure_reason": "preview_parser_error",
            "message": f"미리보기 추출에 실패했습니다 ({type(exc).__name__}).",
        }

    text = sanitize_preview_text(text)
    if metadata.get("pages"):
        pages = []
        for page in metadata["pages"]:
            pages.append(
                {
                    key: (
                        sanitize_preview_text(value)
                        if key == "text"
                        else value
                    )
                    for key, value in page.items()
                }
            )
        metadata["pages"] = pages
    if metadata.get("rows"):
        metadata["rows"] = [
            [sanitize_preview_text(cell) for cell in row]
            for row in metadata["rows"]
        ]
    metadata["representation_status"] = "success" if (text or visual_success) else "empty"
    if not text and not visual_success:
        return result | {
            "status": "empty",
            "parser": parser_name,
            "failure_reason": "empty_preview",
            "message": message or "표시할 미리보기 내용이 비어 있습니다.",
            "metadata": metadata,
        }
    return result | {
        "status": "parsed",
        "parser": parser_name,
        "text": text,
        "message": "",
        "failure_reason": None,
        "metadata": metadata,
    }


def _preview_hwpx(source: Path) -> tuple[str, str, str]:
    parsed = hwpx_parser.parse(source)
    if parsed is not None:
        lines: list[str] = []
        fields = parsed.get("fields", {})
        if isinstance(fields, dict):
            for name, value in fields.items():
                if isinstance(value, str) and value.strip():
                    lines.append(f"{name}: {value.strip()}")

        blocks = parsed.get("blocks", [])
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                body = block.get("body")
                if isinstance(body, str) and body.strip():
                    lines.append(body.strip())
                table = block.get("table")
                if isinstance(table, list):
                    for row in table:
                        if isinstance(row, list):
                            cells = [str(cell).strip() for cell in row if str(cell).strip()]
                            if cells:
                                lines.append(" | ".join(cells))
        if lines:
            return "\n".join(lines), hwpx_parser.PARSER_VERSION, ""

    # Non-standard HWPX files commonly carry a plain-text thumbnail in this
    # member even when their tables do not match the standard request form.
    with zipfile.ZipFile(source) as archive:
        try:
            info = archive.getinfo("Preview/PrvText.txt")
        except KeyError:
            return "", hwpx_parser.PARSER_VERSION, "HWPX 미리보기 텍스트가 없습니다."
        if info.file_size > MAX_PREVIEW_BYTES:
            return "", "hwpx-preview-v1", "HWPX 미리보기 텍스트가 안전 한도를 넘었습니다."
        text = archive.read(info.filename).decode("utf-8-sig")
    if text.strip():
        return text, "hwpx-preview-v1", ""
    return "", "hwpx-preview-v1", "HWPX 미리보기 텍스트가 비어 있습니다."

def _validate_hwpx_container(source: Path) -> None:
    try:
        with zipfile.ZipFile(source) as archive:
            if not archive.namelist():
                raise _CorruptPreview("hwpx_empty_package", "HWPX ZIP 패키지가 비어 있습니다.")
            if archive.testzip() is not None:
                raise _CorruptPreview("hwpx_bad_zip_member", "HWPX 압축 구성원이 손상되었습니다.")
    except zipfile.BadZipFile as exc:
        raise _CorruptPreview("hwpx_not_zip", "HWPX 파일이 유효한 ZIP 패키지가 아닙니다.") from exc

def _validate_xlsx_container(source: Path) -> None:
    """Reject non-ZIP and incomplete OOXML packages before invoking the reader."""
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            if len(names) != len(infos):
                raise _CorruptPreview("xlsx_duplicate_member", "XLSX 패키지에 중복 구성원이 있습니다.")
            if archive.testzip() is not None:
                raise _CorruptPreview("xlsx_bad_zip_member", "XLSX 압축 구성원이 손상되었습니다.")
            required = {
                "[Content_Types].xml",
                "_rels/.rels",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
            }
            worksheets = {name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")}
            if required - names or not worksheets:
                raise _CorruptPreview("xlsx_missing_structure", "XLSX 필수 구조가 없습니다.")
            for name in required | worksheets:
                raw = archive.read(name)
                if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw, re.IGNORECASE):
                    raise _CorruptPreview("xlsx_unsafe_xml", "XLSX XML의 DTD/entity를 허용하지 않습니다.")
                try:
                    ElementTree.fromstring(raw)
                except ElementTree.ParseError as exc:
                    raise _CorruptPreview("xlsx_invalid_xml", "XLSX 필수 XML 구조가 손상되었습니다.") from exc
    except zipfile.BadZipFile as exc:
        raise _CorruptPreview("xlsx_not_zip", "XLSX 파일이 유효한 ZIP 패키지가 아닙니다.") from exc

def _preview_xlsx(source: Path) -> tuple[str, str, str, dict[str, Any]]:
    """Use the same bounded coordinate-aware reader as spreadsheet preparation."""
    from automation.executors.excel import _xlsx_rows

    rows = _xlsx_rows(source)
    bounded_rows = [list(row[:20]) for row in rows[:20]]
    column_count = max((len(row) for row in rows), default=0)
    metadata = {
        "representation": "table",
        "row_count": len(rows),
        "column_count": column_count,
        "rows": bounded_rows,
    }
    return "\n".join(" | ".join(row) for row in bounded_rows), "xlsx-preview-v2", "", metadata


def _preview_pdf(source: Path) -> tuple[str, str, str, dict[str, Any]]:
    data = source.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise _CorruptPreview("invalid_pdf_signature", "PDF 파일 헤더가 올바르지 않습니다.")
    if len(data) < 32 or b"%%EOF" not in data[-1024:]:
        raise _CorruptPreview("invalid_pdf_truncated", "PDF 파일이 끝까지 기록되지 않았습니다.")
    if b"startxref" not in data[-2048:]:
        raise _CorruptPreview("invalid_pdf_structure", "PDF 교차 참조 구조를 확인할 수 없습니다.")

    try:
        pdfium = importlib.import_module("pypdfium2")
    except (ImportError, ModuleNotFoundError) as exc:
        raise _PreviewFailure(
            "pdf_renderer_unavailable",
            "검토된 로컬 PDF renderer(pypdfium2)를 사용할 수 없습니다.",
        ) from exc

    try:
        document = pdfium.PdfDocument(str(source))
    except Exception as exc:
        raise _CorruptPreview("invalid_pdf_structure", "PDF 문서 구조를 완전히 열 수 없습니다.") from exc
    try:
        page_count = len(document)
        if page_count <= 0:
            raise _CorruptPreview("invalid_pdf_page_count", "PDF에 표시할 페이지가 없습니다.")
        limit = min(page_count, MAX_PDF_PAGES)
        pages = _render_pdf_pages(document, limit)
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()
    if not pages:
        raise _PreviewFailure("pdf_render_failed", "PDF 페이지 이미지를 표시하지 못했습니다.")

    pieces = _extract_pdf_text(data)
    for index, page in enumerate(pages):
        if index < len(pieces):
            page["text"] = pieces[index]
    return (
        "\n".join(piece for piece in pieces if piece),
        "pypdfium2-render-v1",
        "",
        {
            "representation": "pdf_pages",
            "page_count": page_count,
            "rendered_page_count": len(pages),
            "pages": pages,
        },
    )


def _render_pdf_pages(document: Any, limit: int) -> list[dict[str, Any]]:
    try:
        pil_image = importlib.import_module("PIL.Image")
    except (ImportError, ModuleNotFoundError) as exc:
        raise _PreviewFailure(
            "pdf_image_encoder_unavailable",
            "PDF 페이지를 PNG로 인코딩할 로컬 이미지 라이브러리가 없습니다.",
        ) from exc
    rendered: list[dict[str, Any]] = []
    for index in range(limit):
        try:
            page = document[index]
            bitmap = page.render(scale=1.5)
            image = bitmap.to_pil()
            width, height = image.size
            if width <= 0 or height <= 0:
                raise RuntimeError("empty page image")
            if max(width, height) > MAX_PDF_PREVIEW_DIMENSION:
                image.thumbnail((MAX_PDF_PREVIEW_DIMENSION, MAX_PDF_PREVIEW_DIMENSION))
                width, height = image.size
            output = __import__("io").BytesIO()
            image.save(output, format="PNG", optimize=True)
            encoded = output.getvalue()
            if not encoded or len(encoded) > MAX_PDF_PREVIEW_BYTES:
                image.thumbnail((800, 800))
                width, height = image.size
                output = __import__("io").BytesIO()
                image.save(output, format="PNG", optimize=True)
            if not encoded or len(encoded) > MAX_PDF_PREVIEW_BYTES:
                raise RuntimeError("page image exceeds preview limit")
            rendered.append(
                {
                    "page": index + 1,
                    "text": "",
                    "image": {
                        "representation": "inline",
                        "mime_type": "image/png",
                        "data": base64.b64encode(encoded).decode("ascii"),
                        "width": width,
                        "height": height,
                    },
                }
            )
        except _PreviewFailure:
            raise
        except Exception as exc:
            raise _PreviewFailure("pdf_render_failed", "PDF 페이지 이미지를 렌더링하지 못했습니다.") from exc
    return rendered


def _extract_pdf_text(data: bytes) -> list[str]:
    pieces: list[str] = []
    for match in re.finditer(rb"(?<![A-Za-z])stream\r?\n", data):
        end = data.find(b"endstream", match.end())
        if end < 0:
            continue
        stream = data[match.end():end].rstrip(b"\r\n")
        dictionary = data[max(0, match.start() - 1024):match.start()]
        if b"/FlateDecode" in dictionary:
            try:
                stream = zlib.decompress(stream)
            except zlib.error:
                continue
        pieces.extend(_pdf_text_block(block) for block in re.findall(rb"BT(.*?)ET", stream, re.DOTALL))
    return [piece for piece in pieces if piece.strip()]


def _pdf_text_block(block: bytes) -> str:
    position = 0
    pending: list[str] = []
    output: list[str] = []
    while position < len(block):
        byte = block[position]
        if byte in b" \t\r\n\x00":
            position += 1
            continue
        if byte == ord("("):
            token, position = _pdf_literal(block, position)
            pending.append(token)
            continue
        if byte == ord("<") and position + 1 < len(block) and block[position + 1] != ord("<"):
            token, position = _pdf_hex(block, position)
            pending.append(token)
            continue
        if byte == ord("["):
            tokens, position = _pdf_array(block, position)
            pending.extend(tokens)
            continue
        end = position
        while end < len(block) and block[end] not in b" \t\r\n[]()<>":
            end += 1
        operator = block[position:end]
        position = max(end, position + 1)
        if operator in (b"Tj", b"TJ"):
            output.append("".join(pending))
            pending.clear()
        elif operator in (b"'", b'"', b"T*", b"Td", b"TD"):
            if pending:
                output.append("".join(pending))
                pending.clear()
            output.append("\n")
        else:
            pending.clear()
    return " ".join(part for part in "".join(output).split(" ") if part).replace(" \n ", "\n")


def _pdf_literal(data: bytes, start: int) -> tuple[str, int]:
    position = start + 1
    depth = 1
    output = bytearray()
    while position < len(data) and depth:
        byte = data[position]
        position += 1
        if byte == ord("\\") and position < len(data):
            escaped = data[position]
            position += 1
            escapes = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
            if escaped in escapes:
                output.extend(escapes[escaped])
            elif 48 <= escaped <= 55:
                octal = bytes([escaped])
                while len(octal) < 3 and position < len(data) and 48 <= data[position] <= 55:
                    octal += bytes([data[position]])
                    position += 1
                output.append(int(octal, 8))
            elif escaped not in (ord("\n"), ord("\r")):
                output.append(escaped)
        elif byte == ord("("):
            depth += 1
            output.append(byte)
        elif byte == ord(")"):
            depth -= 1
            if depth:
                output.append(byte)
        else:
            output.append(byte)
    return _decode_pdf_bytes(bytes(output)), position


def _pdf_hex(data: bytes, start: int) -> tuple[str, int]:
    end = data.find(b">", start + 1)
    if end < 0:
        return "", len(data)
    raw = re.sub(rb"\s+", b"", data[start + 1:end])
    if len(raw) % 2:
        raw += b"0"
    try:
        return _decode_pdf_bytes(binascii.unhexlify(raw)), end + 1
    except binascii.Error:
        return "", end + 1


def _pdf_array(data: bytes, start: int) -> tuple[list[str], int]:
    position = start + 1
    values: list[str] = []
    while position < len(data) and data[position] != ord("]"):
        if data[position] == ord("("):
            value, position = _pdf_literal(data, position)
            values.append(value)
        elif data[position] == ord("<") and position + 1 < len(data) and data[position + 1] != ord("<"):
            value, position = _pdf_hex(data, position)
            values.append(value)
        else:
            position += 1
    return values, min(len(data), position + 1)


def _decode_pdf_bytes(value: bytes) -> str:
    if value.startswith(b"\xfe\xff"):
        return value[2:].decode("utf-16-be", errors="replace")
    return value.decode("utf-8", errors="replace")
