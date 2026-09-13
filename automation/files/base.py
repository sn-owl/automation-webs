from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class FileResult:
    path: str
    type: str
    status: str
    sha256: str
    extracted_ref: str | None = None
    parser: str | None = None
    error: str | None = None


class FileHandler(Protocol):
    def __call__(self, path: Path) -> FileResult:
        ...
