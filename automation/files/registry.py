from __future__ import annotations

import hashlib
import inspect
import re
from pathlib import Path

from .base import FileHandler, FileResult


_EXTENSION = re.compile(r"[a-z0-9][a-z0-9._-]*")
_HANDLERS: dict[str, FileHandler] = {}


class FileMutationError(RuntimeError):
    """Raised when a handler changes the source attachment."""


def _normalize_extension(extension: str) -> str:
    if not isinstance(extension, str):
        raise ValueError("extension must be a string")
    normalized = extension.strip().lower()
    if normalized.startswith("."):
        normalized = normalized[1:]
    if _EXTENSION.fullmatch(normalized) is None:
        raise ValueError("extension must be a safe identifier")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _invoke_handler(
    handler: FileHandler,
    source: Path,
    mime_type: str | None,
) -> FileResult:
    if mime_type is None:
        return handler(source)

    try:
        inspect.signature(handler).bind(source, mime_type)
    except (TypeError, ValueError):
        return handler(source)
    return handler(source, mime_type)


def register_handler(extension: str, handler: FileHandler) -> None:
    normalized = _normalize_extension(extension)
    if not callable(handler):
        raise TypeError("handler must be callable")
    _HANDLERS[normalized] = handler


def unregister_handler(extension: str) -> None:
    _HANDLERS.pop(_normalize_extension(extension), None)


def process_attachment(
    path: Path | str,
    mime_type: str | None = None,
) -> FileResult:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)

    extension = _normalize_extension(source.suffix)
    original_hash = _sha256(source)
    handler = _HANDLERS.get(extension)
    if handler is None:
        return FileResult(
            path=str(source),
            type=extension,
            status="unsupported",
            sha256=original_hash,
        )

    result = _invoke_handler(handler, source, mime_type)
    if not isinstance(result, FileResult):
        raise TypeError("handler must return FileResult")
    if _sha256(source) != original_hash:
        raise FileMutationError(f"handler modified original attachment: {source}")
    return result
