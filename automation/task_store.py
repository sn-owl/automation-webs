from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace
from threading import RLock
from pathlib import Path

from .models import WorkItem


_SAFE_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_STORE_LOCK = RLock()


class TaskStoreError(RuntimeError):
    """Base error for task state storage failures."""


class TaskNotFoundError(TaskStoreError):
    """Raised when a requested task does not exist."""


class TaskConflictError(TaskStoreError):
    """Raised when an existing task has different content."""


class CorruptTaskError(TaskStoreError):
    """Raised when a persisted task cannot be decoded as a WorkItem."""


def _validate_task_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("task_id must be a safe identifier")
    if value in {".", ".."} or re.match(r"^[A-Za-z]:", value):
        raise ValueError("task_id must be a safe identifier")
    if _SAFE_TASK_ID.fullmatch(value) is None:
        raise ValueError("task_id must be a safe identifier")
    return value


def _task_path(task_id: str, root: Path | str) -> Path:
    return Path(root) / "state" / "tasks" / f"{_validate_task_id(task_id)}.json"


def _read_task(path: Path, expected_task_id: str) -> WorkItem:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("task JSON must be an object")
        task = WorkItem.from_dict(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptTaskError(f"invalid task record: {path}") from exc

    if task.task_id != expected_task_id:
        raise CorruptTaskError(f"task id does not match path: {path}")
    return task


def _write_atomically(path: Path, task: WorkItem) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    content = json.dumps(task.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
            temp_name = temp.name
        os.replace(temp_name, path)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)


def _revision_path(task_id: str, revision_digest: str, root: Path | str) -> Path:
    if not isinstance(revision_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", revision_digest):
        raise ValueError("revision_digest must be a sha256 hex digest")
    return Path(root) / "state" / "tasks" / _validate_task_id(task_id) / "revisions" / f"{revision_digest}.json"


def put_task_revision(task: WorkItem, *, root: Path | str = ".") -> str:
    """Persist immutable semantic content; recapture metadata is not a revision."""
    if not isinstance(task, WorkItem):
        raise TypeError("task must be a WorkItem")
    digest = task.computed_revision_digest()
    path = _revision_path(task.task_id, digest, root)
    if path.exists():
        existing = _read_task(path, task.task_id)
        if existing.meaningful_payload() != task.meaningful_payload():
            raise TaskConflictError(f"task revision content conflict: {task.task_id}")
        return digest
    _write_atomically(path, task)
    return digest


def get_task_revision(task_id: str, revision_digest: str, *, root: Path | str = ".") -> WorkItem:
    path = _revision_path(task_id, revision_digest, root)
    if not path.exists():
        raise TaskNotFoundError(f"task revision not found: {task_id}:{revision_digest}")
    return _read_task(path, task_id)


def list_task_revisions(task_id: str, *, root: Path | str = ".") -> list[WorkItem]:
    directory = Path(root) / "state" / "tasks" / _validate_task_id(task_id) / "revisions"
    if not directory.exists():
        return []
    return [_read_task(path, task_id) for path in sorted(directory.glob("*.json"))]


def put_task(task: WorkItem, *, root: Path | str = ".") -> None:
    """Store a canonical task and retain every prior version before replacement."""
    if not isinstance(task, WorkItem):
        raise TypeError("task must be a WorkItem")
    path = _task_path(task.task_id, root)
    with _STORE_LOCK:
        existing = _read_task(path, task.task_id) if path.exists() else None
        if existing is not None:
            old_identity = (existing.scope, existing.connector_id, existing.source.type, existing.source.id, existing.source.external_id)
            new_identity = (task.scope, task.connector_id, task.source.type, task.source.id, task.source.external_id)
            if old_identity != new_identity:
                raise TaskConflictError("task identity belongs to a different owner or source")
            if existing.to_dict() == task.to_dict():
                return
            if task.contract_version < 2 or existing.contract_version < 2:
                raise TaskConflictError(f"task already exists with different content: {task.task_id}")
            put_task_revision(existing, root=root)
            if existing.meaningful_payload() == task.meaningful_payload():
                updated_completeness = (
                    "complete"
                    if existing.completeness == "complete"
                    else task.completeness
                )
                task = replace(
                    task,
                    revision=existing.revision,
                    revision_digest=existing.revision_digest or existing.computed_revision_digest(),
                    content_digest=existing.content_digest or existing.computed_content_digest(),
                    source_completion_observed=(
                        existing.source_completion_observed or task.source_completion_observed
                    ),
                    completeness=updated_completeness,
                )
                if task.to_dict() != existing.to_dict():
                    _write_atomically(path, task)
                return
            digest = task.computed_content_digest()
            if task.content_digest is not None and task.content_digest != digest:
                raise TaskConflictError("provided content digest does not match task content")
            if task.revision_digest is not None and task.revision_digest != task.computed_revision_digest():
                raise TaskConflictError("provided revision digest does not match task content")
            task = replace(
                task,
                revision=max(task.revision, existing.revision + 1),
                content_digest=digest,
                revision_digest=task.computed_revision_digest(),
            )
            put_task_revision(task, root=root)
        elif task.contract_version >= 2:
            digest = task.computed_content_digest()
            if task.content_digest is not None and task.content_digest != digest:
                raise TaskConflictError("provided content digest does not match task content")
            if task.revision_digest is not None and task.revision_digest != task.computed_revision_digest():
                raise TaskConflictError("provided revision digest does not match task content")
            task = replace(
                task,
                revision=task.revision,
                content_digest=digest,
                revision_digest=task.computed_revision_digest(),
            )
            put_task_revision(task, root=root)
        _write_atomically(path, task)


def get_task(task_id: str, *, root: Path | str = ".") -> WorkItem:
    path = _task_path(task_id, root)
    if not path.exists():
        raise TaskNotFoundError(f"task not found: {task_id}")
    return _read_task(path, task_id)


def list_tasks(*, root: Path | str = ".") -> list[WorkItem]:
    task_dir = Path(root) / "state" / "tasks"
    if not task_dir.exists():
        return []

    tasks: list[WorkItem] = []
    for path in sorted(task_dir.glob("*.json")):
        task_id = path.stem
        tasks.append(_read_task(path, task_id))
    return tasks
