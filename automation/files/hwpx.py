from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import parser as hwpx_parser

from .base import FileResult
from .registry import register_handler


PARSER_VERSION = hwpx_parser.PARSER_VERSION


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handle_hwpx(path: Path) -> FileResult:
    source = Path(path)
    source_hash = _sha256(source)
    try:
        parsed = hwpx_parser.parse(source)
    except (
        OSError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
        hwpx_parser.UnsafeXMLError,
    ) as exc:
        # UnsafeXMLError is the parser refusing an entity-bomb / oversized member;
        # it must surface as a failed attachment, never crash the pipeline.
        return FileResult(
            path=str(source),
            type="hwpx",
            status="failed",
            sha256=source_hash,
            parser=PARSER_VERSION,
            error=f"HWPX parser failed: {exc}",
        )

    if parsed is None:
        return FileResult(
            path=str(source),
            type="hwpx",
            status="requires_review",
            sha256=source_hash,
            parser=PARSER_VERSION,
            error="non-standard HWPX structure",
        )

    return FileResult(
        path=str(source),
        type="hwpx",
        status="parsed",
        sha256=source_hash,
        parser=PARSER_VERSION,
    )


register_handler("hwpx", handle_hwpx)
