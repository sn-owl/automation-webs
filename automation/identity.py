from __future__ import annotations

import hashlib
import re


_SAFE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]*")


def _normalize_identifier(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")

    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if "/" in normalized or "\\" in normalized or "\x00" in normalized:
        raise ValueError(f"{name} must be a safe identifier")
    if normalized in {".", ".."} or re.match(r"^[a-z]:", normalized):
        raise ValueError(f"{name} must be a safe identifier")
    if _SAFE_IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a safe identifier")

    return normalized


def task_id(
    source_id: str,
    external_id: str,
    *,
    scope: str | None = None,
    connector_id: str | None = None,
) -> str:
    """Return a stable task id, optionally scoped to an input connection.

    The two-argument form remains the legacy Case1 representation. New
    connectors pass scope and connector explicitly so identical external ids
    cannot collide across owners or connections.
    """
    source = _normalize_identifier(source_id, "source_id")
    external = _normalize_identifier(external_id, "external_id")
    if scope is None and connector_id is None:
        return f"{source}-{external}"
    if scope is None or connector_id is None:
        raise ValueError("scope and connector_id must be provided together")
    owner = _normalize_identifier(scope, "scope")
    connector = _normalize_identifier(connector_id, "connector_id")
    # Delimiter concatenation collides when a component itself contains "-".
    payload = "\0".join((owner, connector, source, external)).encode("utf-8")
    return "task-" + hashlib.sha256(payload).hexdigest()


def revision_digest(
    *,
    scope: str,
    connector_id: str,
    source_id: str,
    external_id: str,
    meaningful_content: object,
) -> str:
    """Hash logical identity and meaningful content for a WorkItem revision."""
    identity = (
        _normalize_identifier(scope, "scope"),
        _normalize_identifier(connector_id, "connector_id"),
        _normalize_identifier(source_id, "source_id"),
        _normalize_identifier(external_id, "external_id"),
    )
    import json

    payload = json.dumps(
        {"identity": identity, "content": meaningful_content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def execution_key(task_id: str, recipe_id: str, input_hash: str) -> str:
    task = _normalize_identifier(task_id, "task_id")
    recipe = _normalize_identifier(recipe_id, "recipe_id")
    input_digest = _normalize_identifier(input_hash, "input_hash")
    payload = "\0".join((task, recipe, input_digest)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def rebuild_execution_key(
    task_id: str, recipe_id: str, input_hash: str, rebuild_request_id: str
) -> str:
    """Derive a distinct stable key for an explicit user-requested rebuild."""
    task = _normalize_identifier(task_id, "task_id")
    recipe = _normalize_identifier(recipe_id, "recipe_id")
    digest = _normalize_identifier(input_hash, "input_hash")
    request = _normalize_identifier(rebuild_request_id, "rebuild_request_id")
    payload = "\0".join(("execution-rebuild-v1", task, recipe, digest, request)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
