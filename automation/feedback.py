"""Typed operator feedback linked to a task and, when applicable, execution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automation.classification import Classification
from automation.events import DuplicateEventError, EventLog
from automation.task_store import get_task

FEEDBACK_TYPES = (
    "classification_correction",
    "execution_defect",
    "input_supplement",
    "result_correction",
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_log(root: Path | str) -> EventLog:
    return EventLog(Path(root) / "state" / "events.jsonl")


def record_feedback(
    task_id: str,
    feedback_type: str,
    *,
    scope: str,
    task_revision: int,
    actor: str,
    reason: str,
    root: Path | str,
    execution_key: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, persist, and return one idempotent scoped feedback event."""
    from automation.patterns import validate_scope

    scope = validate_scope(scope)
    if not isinstance(task_id, str) or _SAFE_ID.fullmatch(task_id) is None:
        raise ValueError("task_id must be a safe identifier")
    task = get_task(task_id, root=root)
    if task.scope != scope:
        raise ValueError("task scope does not match feedback scope")
    if isinstance(task_revision, bool) or not isinstance(task_revision, int) or task_revision < 1:
        raise ValueError("task_revision must be a positive integer")
    if task.revision != task_revision:
        raise ValueError("feedback task_revision must be current")
    if feedback_type not in FEEDBACK_TYPES:
        raise ValueError(f"feedback_type must be one of {FEEDBACK_TYPES}")
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("actor and reason are required")
    if execution_key is not None and _SAFE_ID.fullmatch(execution_key) is None:
        raise ValueError("execution_key must be a safe identifier")
    if feedback_type in {"execution_defect", "result_correction"} and execution_key is None:
        raise ValueError(f"{feedback_type} requires execution_key")
    if details is not None and (not isinstance(details, dict) or not details):
        raise ValueError("details must be a non-empty object")
    details = dict(details or {})
    if feedback_type == "classification_correction":
        classification = details.get("classification")
        if not isinstance(classification, dict):
            raise ValueError("classification_correction requires details.classification")
        Classification.from_dict(classification)
    payload = {
        "type": feedback_type,
        "task_id": task_id,
        "scope": scope,
        "task_revision": task_revision,
        "revision_digest": task.revision_digest or task.computed_revision_digest(),
        "execution_key": execution_key,
        "actor": actor,
        "reason": reason,
        "details": details,
        "timestamp": _timestamp(),
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    event = {"event_id": f"feedback-{digest}", "type": "feedback", "task_id": task_id, "feedback": payload}
    try:
        _event_log(root).append(event)
    except DuplicateEventError:
        pass
    return event


def list_feedback(task_id: str | None = None, *, scope: str | None = None, root: Path | str) -> list[dict[str, Any]]:
    """Read typed feedback for review/replay without mutating patterns."""
    result = []
    for event in _event_log(root).read():
        if event.get("type") != "feedback":
            continue
        if task_id is not None and event.get("task_id") != task_id:
            continue
        feedback = event.get("feedback")
        if isinstance(feedback, dict) and feedback.get("type") in FEEDBACK_TYPES:
            if scope is not None and feedback.get("scope") != scope:
                continue
            result.append(dict(event))
    return result


