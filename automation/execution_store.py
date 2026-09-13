"""Durable storage for execution requests.

The state machine reserves execution keys in memory, which is enough within a
single process but loses the reservation the moment the CLI exits. Persisting
each request is what makes an execution key meaningful across runs: a second
invocation sees the first one's key and refuses to treat an already-performed
execution as fresh.

Records are written atomically, the same way ``task_store`` writes tasks, so a
crash mid-write cannot leave a half-parsed request behind.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from automation.execution import ExecutionRequest
from automation.locking import file_lock

_SAFE_EXECUTION_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")




def _validate_rebuild_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_EXECUTION_KEY.fullmatch(value) is None:
        raise ValueError("rebuild_request_id must be a safe identifier")
    return value


class ExecutionStoreError(RuntimeError):
    """Base error for execution storage failures."""


class ExecutionNotStoredError(ExecutionStoreError):
    """Raised when a requested execution key has no stored record."""


class CorruptExecutionError(ExecutionStoreError):
    """Raised when a stored record cannot be decoded as an ExecutionRequest."""

class ExecutionAlreadyReservedError(ExecutionStoreError):
    """Raised when an execution key is reserved by another request."""


def _validate_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("execution_key must be a safe identifier")
    if value in {".", ".."} or _SAFE_EXECUTION_KEY.fullmatch(value) is None:
        raise ValueError("execution_key must be a safe identifier")
    return value


def _execution_dir(root: Path | str) -> Path:
    return Path(root) / "state" / "executions"


def _execution_path(execution_key: str, root: Path | str) -> Path:
    return _execution_dir(root) / f"{_validate_key(execution_key)}.json"


def _version_path(task_id: str, root: Path | str) -> Path:
    safe = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id)
    if safe is None:
        raise ValueError("task_id must be a safe identifier")
    return Path(root) / "state" / "result_versions" / f"{task_id}.json"
def reserve_result_version(
    task_id: str,
    *,
    rebuild_request_id: str | None = None,
    execution_key: str,
    root: Path | str = ".",
) -> tuple[int, bool]:
    """Atomically reserve a never-reused result version.

    Returns ``(version, already_reserved)``. A repeated rebuild identity is an
    idempotent read, while malformed persisted state fails closed as corruption.
    """
    if not isinstance(execution_key, str) or _SAFE_EXECUTION_KEY.fullmatch(execution_key) is None:
        raise ValueError("execution_key must be a safe identifier")
    if rebuild_request_id is not None:
        _validate_rebuild_id(rebuild_request_id)
    path = _version_path(task_id, root)
    with file_lock(path.with_suffix(".lock")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"requests": {}, "next_version": 1}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptExecutionError(f"corrupt result version record: {task_id}") from exc
        if not isinstance(data, dict):
            raise CorruptExecutionError(f"corrupt result version record: {task_id}")

        raw_next = data.get("next_version", 1)
        if isinstance(raw_next, bool) or not isinstance(raw_next, int) or raw_next < 1:
            raise CorruptExecutionError(f"corrupt result version record: {task_id}")
        requests = data.get("requests", {})
        if not isinstance(requests, dict):
            raise CorruptExecutionError(f"corrupt result version record: {task_id}")

        versions: set[int] = set()
        matched: int | None = None
        for registry_key, entry in requests.items():
            if registry_key == "initial":
                expected_rebuild_id = None
            elif isinstance(registry_key, str) and registry_key.startswith("rebuild:"):
                expected_rebuild_id = registry_key.removeprefix("rebuild:")
                try:
                    _validate_rebuild_id(expected_rebuild_id)
                except ValueError as exc:
                    raise CorruptExecutionError(
                        f"corrupt result version record: {task_id}"
                    ) from exc
            else:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            if not isinstance(entry, dict) or set(entry) != {"version", "execution_key"}:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            version = entry["version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            if expected_rebuild_id is None and version != 1:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            if expected_rebuild_id is not None and version < 2:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            try:
                stored_key = _validate_key(entry["execution_key"])
            except (TypeError, ValueError) as exc:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}") from exc
            if version in versions:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            versions.add(version)
            if expected_rebuild_id is not None and expected_rebuild_id == rebuild_request_id:
                if execution_key != stored_key:
                    raise ExecutionAlreadyReservedError("rebuild request identity mismatch")
                matched = version
            elif expected_rebuild_id is None and rebuild_request_id is None:
                if execution_key != stored_key:
                    raise ExecutionAlreadyReservedError("initial execution identity mismatch")
                matched = 1

        if matched is not None:
            if raw_next <= matched:
                raise CorruptExecutionError(f"corrupt result version record: {task_id}")
            return matched, True

        if raw_next <= max(versions, default=0):
            raise CorruptExecutionError(f"corrupt result version record: {task_id}")
        if rebuild_request_id is None:
            version = 1
            requests["initial"] = {"version": version, "execution_key": execution_key}
            raw_next = max(raw_next, 2)
        else:
            version = max(raw_next, 2)
            requests[f"rebuild:{rebuild_request_id}"] = {
                "version": version,
                "execution_key": execution_key,
            }
            raw_next = version + 1
        data["next_version"] = raw_next
        _atomic_json(path, data)
        return version, False


def _atomic_json(path: Path, payload: object) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as temp:
            temp.write(content); temp.flush(); os.fsync(temp.fileno()); temp_name = temp.name
        os.replace(temp_name, path)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)


def put_execution(request: ExecutionRequest, *, root: Path | str = ".") -> None:
    """Write ``request`` atomically, replacing any earlier state for its key."""
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    assert request.execution_key is not None

    path = _execution_path(request.execution_key, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(request.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    temp_name: str | None = None
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

def next_attempt(execution_key: str, *, root: Path | str = ".") -> int:
    """Return the attempt number a retry of this key may claim.

    1 when the key has never been reserved. Otherwise one past the stored
    attempt, but ONLY when that attempt failed -- a succeeded or still
    in-flight execution is not something a caller may re-run, which is the
    idempotency the execution key exists to provide.
    """
    try:
        stored = get_execution(execution_key, root=root)
    except ExecutionNotStoredError:
        return 1
    if stored.state != "failed":
        raise ExecutionAlreadyReservedError(
            f"execution key already reserved: {execution_key}"
        )
    return stored.attempt + 1


def reserve_execution(request: ExecutionRequest, *, root: Path | str = ".") -> None:
    """Atomically reserve one attempt while preserving the prior failed record.

    Cooperating retry callers share a per-key lock. Replacement is atomic, so
    readers never see a missing/partial record and a failed write keeps the
    previous attempt available for recovery.
    """
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    assert request.execution_key is not None
    path = _execution_path(request.execution_key, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock")):
        try:
            stored = get_execution(request.execution_key, root=root)
        except ExecutionNotStoredError:
            if request.attempt != 1:
                raise ExecutionAlreadyReservedError("a new execution must start at attempt 1")
        else:
            if stored.state != "failed" or request.attempt != stored.attempt + 1:
                raise ExecutionAlreadyReservedError(
                    f"execution key already reserved: {request.execution_key}"
                )
        put_execution(request, root=root)


def get_execution(execution_key: str, *, root: Path | str = ".") -> ExecutionRequest:
    path = _execution_path(execution_key, root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ExecutionNotStoredError(f"unknown execution key: {execution_key}") from exc
    except (OSError, UnicodeError) as exc:
        raise CorruptExecutionError(f"unreadable execution record: {execution_key}") from exc

    try:
        return ExecutionRequest.from_dict(json.loads(text))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CorruptExecutionError(f"corrupt execution record: {execution_key}") from exc


def list_execution_keys(*, root: Path | str = ".") -> tuple[str, ...]:
    """Return every reserved execution key, sorted."""
    directory = _execution_dir(root)
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.stem for path in directory.glob("*.json")))
