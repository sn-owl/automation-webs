"""Scoped, read-only registered project profiles."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any


_PROFILE_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "project_id",
        "name",
        "root",
        "managed_patterns",
        "stack",
        "run_reference",
        "test_reference",
        "allow_full_codebase",
        "allow_external_ai_code",
        "database_paths",
        "managed_directories",
    }
)
_HARD_EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules"}
)
_HARD_EXCLUDED_NAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "secrets.json",
        "id_rsa",
        "id_ed25519",
        "npmrc",
        ".npmrc",
        "pypirc",
        ".pypirc",
    }
)
_HARD_EXCLUDED_SUFFIXES = frozenset(
    {
        ".key",
        ".pem",
        ".p12",
        ".pfx",
        ".kdbx",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".bin",
        ".class",
        ".o",
        ".obj",
        ".wasm",
        ".pyc",
        ".pyo",
        ".pickle",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".rar",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".bmp",
        ".tiff",
        ".tif",
        ".pdf",
        ".xls",
        ".xlsx",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".hwpx",
    }
)
MAX_DISCOVERED_FILES = 10_000
MAX_ANALYSIS_FILE_BYTES = 2 * 1024 * 1024


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _PROFILE_ID.fullmatch(value.strip().lower()) is None:
        raise ValueError(f"{field} must be a safe lowercase identifier")
    return value.strip().lower()

_HARD_EXCLUDED_PARTS = _HARD_EXCLUDED_DIRECTORIES


def _access_switch(value: dict[str, Any], key: str) -> bool:
    if key not in value or type(value[key]) is not bool:
        raise ValueError(f"{key} must be explicitly selected as a boolean")
    return value[key]


def _text_list(value: Any, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must contain non-empty text")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def _relative_value(value: str, field: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not normalized or "\x00" in normalized or ".." in path.parts:
        raise ValueError(f"{field} must stay relative to the project root")
    return path.as_posix()

def _scope_path(root: Path, value: str, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must contain non-empty relative paths")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"{field} must stay inside project root") from None
    return candidate



@dataclass(frozen=True)
class ProjectProfile:
    schema_version: int
    scope: str
    project_id: str
    name: str
    root: str
    managed_patterns: tuple[str, ...]
    stack: tuple[str, ...]
    run_reference: str | None
    test_reference: str | None
    allow_full_codebase: bool
    allow_external_ai_code: bool
    database_paths: tuple[str, ...] = ()
    managed_directories: tuple[str, ...] = (".",)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported ProjectProfile schema_version")
        root = Path(self.root).resolve()
        if not root.is_dir():
            raise ValueError("project root must be an existing directory")
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if self.project_id in {".", ".."} or "/" in self.project_id or "\\" in self.project_id:
            raise ValueError("project_id must be a registry-safe identifier")
        if not self.managed_patterns:
            raise ValueError("managed_patterns must not be empty")
        if not self.managed_directories:
            raise ValueError("managed_directories must not be empty")
        for directory in self.managed_directories:
            _scope_path(root, directory, "managed_directories")
        for pattern in self.managed_patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError("managed_patterns must contain text")
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("managed_patterns must stay inside project root")
        for database_path in self.database_paths:
            _scope_path(root, database_path, "database_paths")

    @property
    def read_scope(self) -> dict[str, Any]:
        return {
            "directories": list(self.managed_directories),
            "file_patterns": list(self.managed_patterns),
            "database_paths": list(self.database_paths),
            "allow_full_codebase": self.allow_full_codebase,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "project_id": self.project_id,
            "name": self.name,
            "root": self.root,
            "managed_patterns": list(self.managed_patterns),
            "managed_directories": list(self.managed_directories),
            "stack": list(self.stack),
            "run_reference": self.run_reference,
            "test_reference": self.test_reference,
            "allow_full_codebase": self.allow_full_codebase,
            "allow_external_ai_code": self.allow_external_ai_code,
            "database_paths": list(self.database_paths),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectProfile":
        if not isinstance(value, dict):
            raise ValueError("ProjectProfile must be an object")
        unknown = set(value) - _PROFILE_FIELDS
        if unknown:
            raise ValueError(f"unknown ProjectProfile field(s): {sorted(unknown)}")
        missing = _PROFILE_FIELDS - set(value) - {"database_paths"}
        if missing:
            raise ValueError(f"missing ProjectProfile field(s): {sorted(missing)}")
        if type(value.get("schema_version")) is not int:
            raise ValueError("schema_version must be an integer")
        for field in ("name", "root"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise ValueError(f"{field} is required")
        for field in ("run_reference", "test_reference"):
            if value.get(field) is not None and not isinstance(value[field], str):
                raise ValueError(f"{field} must be text or null")
        patterns = _text_list(value.get("managed_patterns"), "managed_patterns", allow_empty=False)
        for pattern in patterns:
            _relative_value(pattern, "managed_patterns")
        database_paths = tuple(
            _relative_value(path, "database_paths")
            for path in _text_list(value.get("database_paths", []), "database_paths")
        )
        managed_directories = tuple(
            _relative_value(path, "managed_directories")
            for path in _text_list(
                value["managed_directories"],
                "managed_directories",
                allow_empty=False,
            )
        )
        return cls(
            schema_version=value["schema_version"],
            scope=_safe_identifier(value.get("scope"), "scope"),
            project_id=_safe_identifier(value.get("project_id"), "project_id"),
            name=value["name"].strip(),
            root=str(Path(value["root"]).resolve()),
            managed_patterns=patterns,
            stack=_text_list(value.get("stack"), "stack"),
            run_reference=value["run_reference"].strip() if value.get("run_reference") else None,
            test_reference=value["test_reference"].strip() if value.get("test_reference") else None,
            allow_full_codebase=_access_switch(value, "allow_full_codebase"),
            allow_external_ai_code=_access_switch(value, "allow_external_ai_code"),
            database_paths=database_paths,
            managed_directories=managed_directories,
        )


def _contained(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root)
        if any(part in _HARD_EXCLUDED_PARTS for part in relative.parts):
            return False
        if path.name in _HARD_EXCLUDED_NAMES or path.name.startswith(".env"):
            return False
        return path.is_file()
    except (OSError, ValueError):
        return False


def _hard_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = tuple(part.casefold() for part in relative.parts)
    name = parts[-1] if parts else ""
    if any(part in _HARD_EXCLUDED_DIRECTORIES for part in parts):
        return True
    if name in _HARD_EXCLUDED_NAMES or name.startswith(".env."):
        return True
    if Path(name).suffix.casefold() in _HARD_EXCLUDED_SUFFIXES:
        return True
    if name.endswith(("credentials.json", "secrets.json", "tokens.json")):
        return True
    return False


def _matches_scope(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(
        fnmatch.fnmatch(relative, pattern)
        or fnmatch.fnmatch(relative, pattern.replace("**/", ""))
        or path.relative_to(root).match(pattern)
        for pattern in patterns
    )


def discover_files(profile: ProjectProfile) -> list[Path]:
    root = Path(profile.root).resolve()
    paths: set[Path] = set()
    for relative_directory in profile.managed_directories:
        base = _scope_path(root, relative_directory, "managed_directories")
        if not base.is_dir():
            continue
        for current, directories, filenames in os.walk(base, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if not _hard_excluded(current_path / name, root)
            ]
            for filename in filenames:
                path = current_path / filename
                if _hard_excluded(path, root) or not _contained(path, root):
                    continue
                if not profile.allow_full_codebase and not _matches_scope(
                    path, root, profile.managed_patterns
                ):
                    continue
                paths.add(path)
                if len(paths) > MAX_DISCOVERED_FILES:
                    raise ValueError("project exceeds the safe file-count limit")
    return sorted(paths)


def project_content_digest(profile: ProjectProfile) -> str:
    root = Path(profile.root).resolve()
    profile_data = profile.to_dict()
    profile_data["root"] = "$PROJECT_ROOT"
    digest = hashlib.sha256(
        json.dumps(profile_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    for path in discover_files(profile):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    for relative in profile.database_paths:
        path = (root / relative).resolve()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if not path.is_file():
            digest.update(b"missing")
            continue
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _profile_path(state_root: Path | str, scope: str, project_id: str) -> Path:
    safe_scope = _safe_identifier(scope, "scope")
    safe_project = _safe_identifier(project_id, "project_id")
    return Path(state_root) / "state" / "projects" / safe_scope / f"{safe_project}.json"


def register_project(profile: ProjectProfile, *, root: Path | str = ".") -> dict[str, Any]:
    path = _profile_path(root, profile.scope, profile.project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(profile.to_dict(), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
    return profile.to_dict()


def load_project(scope: str, project_id: str, *, root: Path | str = ".") -> ProjectProfile:
    path = _profile_path(root, scope, project_id)
    try:
        profile = ProjectProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise ValueError(f"project is not registered in scope: {project_id}") from None
    if profile.scope != _safe_identifier(scope, "scope") or profile.project_id != _safe_identifier(project_id, "project_id"):
        raise ValueError("stored ProjectProfile identity does not match its scoped path")
    return profile


def list_projects(scope: str, *, root: Path | str = ".") -> list[ProjectProfile]:
    directory = _profile_path(root, scope, "placeholder").parent
    if not directory.is_dir():
        return []
    return [load_project(scope, path.stem, root=root) for path in sorted(directory.glob("*.json"))]
