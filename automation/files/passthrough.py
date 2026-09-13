from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from .base import FileResult
from .registry import register_handler


_SUPPORTED_EXTENSIONS = ("pdf", "docx", "xlsx", "hwp")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handle_passthrough(path: Path, mime_type: str | None = None) -> FileResult:
    source = Path(path)
    extension = source.suffix.lower().lstrip(".")
    inferred_mime = mimetypes.guess_type(source.name, strict=False)[0]
    error = None
    if mime_type is not None and inferred_mime is not None:
        supplied_mime = mime_type.strip().lower()
        if supplied_mime != inferred_mime.lower():
            error = (
                f"MIME type mismatch for .{extension}: "
                f"expected {inferred_mime}, received {mime_type}"
            )

    return FileResult(
        path=str(source),
        type=extension,
        status="requires_review",
        sha256=_sha256(source),
        error=error,
    )


for _extension in _SUPPORTED_EXTENSIONS:
    register_handler(_extension, handle_passthrough)
