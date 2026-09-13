"""Scoped immutable Core pattern revision store (docs/ARCHITECTURE.md 7, 13).

Every state change is written as a NEW numbered revision under
``state/patterns/<scope>/<pattern_id>/<NNN>.json``; no revision is ever
overwritten. The write hard-links into place, so an occupied slot fails
atomically instead of being clobbered even under a racing writer. That
history is the audit trail promotion review builds on.

This module is the single write boundary for ``active``: ``revise_pattern``
refuses that target without the ``approval_event_id`` of the decision that
authorized it. ``create_pattern`` is the one entry point that does not go
through ``Pattern.transition``, so it enforces directly that a pattern can
only enter storage as ``candidate``.

``active_definitions`` is the read side: ``rules.classify_with_rules`` merges
it over the repository baseline, which is why promotion never needs to write
``config/rules.json``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .approval import validate_pattern_approval
from .decisions import PatternDecision
from .events import EventLog
from .locking import file_lock
from .patterns import Pattern, definition_digest, validate_pattern_id, validate_scope

_SAFE_EVENT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_REVISION_FILE = re.compile(r"(\d+)\.json\Z")

class PatternStoreError(RuntimeError):
    """Base error for pattern revision storage failures."""


class PatternNotFoundError(PatternStoreError):
    """Raised when a requested scoped pattern has no revisions."""


class PatternConflictError(PatternStoreError):
    """Raised when an immutable revision slot is occupied."""


class CorruptPatternError(PatternStoreError):
    """Raised when persisted pattern data is invalid or scope-bound incorrectly."""


def _pattern_dir(scope: str, pattern_id: str, root: Path | str) -> Path:
    return Path(root) / "state" / "patterns" / validate_scope(scope) / validate_pattern_id(pattern_id)


def _revision_path(scope: str, pattern_id: str, revision: int, root: Path | str) -> Path:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision must be a positive integer")
    return _pattern_dir(scope, pattern_id, root) / f"{revision:03d}.json"


def _existing_revisions(pattern_dir: Path) -> list[int]:
    if not pattern_dir.exists():
        return []
    revisions: list[int] = []
    for path in pattern_dir.glob("*.json"):
        match = _REVISION_FILE.fullmatch(path.name)
        if match:
            revisions.append(int(match.group(1)))
    return sorted(revisions)


def _read_revision(path: Path, expected_scope: str, expected_pattern_id: str) -> tuple[Pattern, int, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("revision JSON must be an object")
        if set(payload) - {"revision", "pattern", "approval_event_id"}:
            raise ValueError("unknown pattern revision field")
        revision = payload["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")
        pattern = Pattern.from_dict(payload["pattern"])
        approval_event_id = payload.get("approval_event_id")
        if approval_event_id is not None and (
            not isinstance(approval_event_id, str) or _SAFE_EVENT_ID.fullmatch(approval_event_id) is None
        ):
            raise ValueError("approval_event_id must be a safe identifier")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise CorruptPatternError(f"invalid pattern revision: {path}") from exc
    if pattern.scope != expected_scope or pattern.pattern_id != expected_pattern_id or revision != int(path.stem):
        raise CorruptPatternError(f"pattern binding does not match path: {path}")
    return pattern, revision, approval_event_id


def _write_atomically(path: Path, envelope: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
            temp_name = temp.name
        try:
            os.link(temp_name, path)
        except FileExistsError as exc:
            raise PatternConflictError(f"revision already exists: {path}") from exc
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_revision(pattern: Pattern, *, revision: int, approval_event_id: str | None, root: Path | str) -> None:
    envelope: dict[str, Any] = {"revision": revision, "pattern": pattern.to_dict()}
    if approval_event_id is not None:
        if _SAFE_EVENT_ID.fullmatch(approval_event_id) is None:
            raise ValueError("approval_event_id must be a safe identifier")
        envelope["approval_event_id"] = approval_event_id
    _write_atomically(_revision_path(pattern.scope, pattern.pattern_id, revision, root), envelope)


def create_pattern(pattern: Pattern, *, root: Path | str = ".") -> Pattern:
    if not isinstance(pattern, Pattern):
        raise TypeError("pattern must be a Pattern")
    if pattern.state != "candidate":
        raise ValueError("new patterns must start in candidate state")
    pattern_dir = _pattern_dir(pattern.scope, pattern.pattern_id, root)
    revisions = _existing_revisions(pattern_dir)
    if revisions:
        latest, _, _ = _read_revision(_revision_path(pattern.scope, pattern.pattern_id, revisions[-1], root), pattern.scope, pattern.pattern_id)
        if latest.to_dict() == pattern.to_dict():
            return pattern
        raise PatternConflictError(f"pattern already exists: {pattern.scope}/{pattern.pattern_id}")
    _write_revision(pattern, revision=1, approval_event_id=None, root=root)
    return pattern


def get_pattern(scope: str, pattern_id: str, *, root: Path | str = ".") -> tuple[Pattern, int, str | None]:
    pattern_dir = _pattern_dir(scope, pattern_id, root)
    revisions = _existing_revisions(pattern_dir)
    if not revisions:
        raise PatternNotFoundError(f"pattern not found: {scope}/{pattern_id}")
    return _read_revision(_revision_path(scope, pattern_id, revisions[-1], root), validate_scope(scope), validate_pattern_id(pattern_id))


def revise_pattern(scope: str, pattern_id: str, to_state: str, *, root: Path | str = ".", approval_event_id: str | None = None) -> Pattern:
    # The store is the single write boundary for `active`: every caller must
    # hand over the decision event that authorized it (R02). `Pattern.transition`
    # owns which edges are legal; it does not own who approved them.
    if to_state == "active" and approval_event_id is None:
        raise ValueError("promotion to 'active' requires an approval_event_id")
    with file_lock(_pattern_dir(scope, pattern_id, root) / ".decision.lock"):
        current, revision, _ = get_pattern(scope, pattern_id, root=root)
        updated = current.transition(to_state)
        if to_state == "active":
            latest = None
            for event in EventLog(Path(root) / "state" / "events.jsonl").read():
                if (
                    event.get("type") == "pattern_decision"
                    and event.get("scope") == scope
                    and event.get("pattern_id") == pattern_id
                ):
                    latest = event
            if latest is None or latest["event_id"] != approval_event_id:
                raise ValueError("approval_event_id must reference the current pattern decision")
            validate_pattern_approval(
                PatternDecision.from_dict(latest["decision"]),
                scope=scope,
                pattern_id=pattern_id,
                pattern_revision=revision,
                definition_digest=definition_digest(current.to_dict().get("definition")),
            )
        _write_revision(updated, revision=revision + 1, approval_event_id=approval_event_id, root=root)
        return updated


def retire_pattern(scope: str, pattern_id: str, *, root: Path | str = ".") -> Pattern:
    return revise_pattern(scope, pattern_id, "retired", root=root)


def list_patterns(scope: str, *, root: Path | str = ".") -> list[Pattern]:
    scope = validate_scope(scope)
    patterns_dir = Path(root) / "state" / "patterns" / scope
    if not patterns_dir.exists():
        return []
    patterns: list[Pattern] = []
    for pattern_dir in sorted(patterns_dir.iterdir()):
        if not pattern_dir.is_dir():
            continue
        pattern_id = validate_pattern_id(pattern_dir.name)
        revisions = _existing_revisions(pattern_dir)
        if revisions:
            patterns.append(_read_revision(_revision_path(scope, pattern_id, revisions[-1], root), scope, pattern_id)[0])
    return patterns

def active_definitions(scope: str, *, root: Path | str = ".") -> list[dict[str, Any]]:
    return [pattern.to_dict()["definition"] for pattern in list_patterns(scope, root=root) if pattern.kind == "rule" and pattern.state == "active" and pattern.definition is not None]
