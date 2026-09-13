"""Append-only, auditable JSONL task event log."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class EventLogError(ValueError):
    """Base error for invalid or unsafe event log records."""


class DuplicateEventError(EventLogError):
    """Raised when an event ID already exists in the log."""


class CorruptEventError(EventLogError):
    """Raised when a JSONL record cannot be read as an event."""


class SecretFieldError(EventLogError):
    """Raised when an event contains a field that must not be recorded."""


_SECRET_FIELD_PARTS = (
    "password",
    "passwd",
    "sessioncookie",
    "apikey",
    "apitoken",
    "accesstoken",
    "refreshtoken",
    "bearertoken",
    "authorization",
    "clientsecret",
    "secretkey",
    "privatekey",
    "internalserver",
    "internalhost",
    "internalip",
    "privateserver",
    "privateip",
    "serverdetails",
    "databaseurl",
    "connectionstring",
)
_SECRET_FIELD_NAMES = {"pwd", "cookie", "cookies", "token", "secret"}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*(?:bearer|basic)\s+\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|api[-_ ]?(?:key|token)|"
        r"access[-_]?token|refresh[-_]?token|session[-_]?(?:id|token|cookie)?|"
        r"(?:client|private)?secret)\s*[:=]\s*\S+"
    ),
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqps?|"
        r"mssql)://[^\s/@]+:[^\s/@]+@"
    ),
)


def _value_is_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


_SECRET_FIELD_NAMES = {"pwd", "cookie", "cookies", "token", "secret"}


def _field_is_secret(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return normalized in _SECRET_FIELD_NAMES or any(
        part in normalized for part in _SECRET_FIELD_PARTS
    )


def _reject_secret_fields(value: Any, *, location: str = "event") -> None:
    if isinstance(value, str) and _value_is_secret(value):
        raise SecretFieldError(f"secret value is not allowed: {location}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EventLogError(f"{location} object keys must be strings")
            if _field_is_secret(key):
                raise SecretFieldError(f"secret field is not allowed: {location}.{key}")
            _reject_secret_fields(child, location=f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_fields(child, location=f"{location}[{index}]")





def _validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise EventLogError("event must be an object")
    payload = dict(event)
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise EventLogError("event_id is required")
    _reject_secret_fields(payload)
    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EventLogError(f"event is not JSON serializable: {exc}") from exc
    return payload


def _decode_line(line: bytes, line_number: int) -> dict[str, Any]:
    try:
        text = line.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptEventError(f"invalid JSONL event at line {line_number}") from exc
    if not isinstance(payload, dict):
        raise CorruptEventError(f"event at line {line_number} must be an object")
    try:
        return _validate_event(payload)
    except EventLogError as exc:
        raise CorruptEventError(f"invalid event at line {line_number}: {exc}") from exc


class EventLog:
    """Read and append events without rewriting existing log bytes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_bytes()
        except OSError:
            raise
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                raise CorruptEventError(f"blank event at line {line_number}")
            event = _decode_line(line, line_number)
            event_id = event["event_id"]
            if event_id in seen:
                raise DuplicateEventError(f"duplicate event_id: {event_id}")
            seen.add(event_id)
            events.append(event)
        return events

    def append(self, event: Mapping[str, Any]) -> None:
        payload = _validate_event(event)
        existing = self.read()
        event_id = payload["event_id"]
        if any(item["event_id"] == event_id for item in existing):
            raise DuplicateEventError(f"duplicate event_id: {event_id}")
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            if self.path.stat().st_size and not self.path.read_bytes()[-1:] in (b"\n", b"\r"):
                stream.write(b"\n")
            stream.write(encoded)

    append_event = append
    read_events = read


def append_event(path: Path | str, event: Mapping[str, Any]) -> None:
    """Append one validated event to ``path``."""
    EventLog(path).append(event)


def read_events(path: Path | str) -> list[dict[str, Any]]:
    """Read all events from ``path`` in their stored order."""
    return EventLog(path).read()
