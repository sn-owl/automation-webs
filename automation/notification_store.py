"""Durable notification profile and delivery records."""

from __future__ import annotations
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import re
from typing import Any

from automation.events import _value_is_secret
from automation.locking import file_lock


_CHANNELS = frozenset({"local", "dashboard", "telegram"})
_EVENT_MODES = frozenset({"immediate", "batched"})
_EVENTS = frozenset({
    "new_task",
    "classification_review",
    "failure",
    "preparation_started",
    "preparation_completed",
})
_DELIVERY_STATUSES = frozenset({"queued", "delivered", "failed"})
_RETRY_KEYS = frozenset({"max_attempts"})
_MAX_ATTEMPTS = 10
_CONFIRMATION_POLICIES = frozenset({"user_required"})
_SAFE_PROFILE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_TASK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_RECORD_REFERENCE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_DELIVERY_FIELDS = frozenset({
    "delivery_key", "task_id", "event", "channel", "status", "attempt",
    "retryable", "event_payload", "local_reference", "message",
})


def _validate_profile_values(
    profile_id: Any,
    channels: Any,
    event_policy: Any,
    retry_policy: Any,
    confirmation_policy: Any,
    sensitive: Any,
    telegram_enabled: Any,
) -> tuple[str, tuple[str, ...], dict[str, str], dict[str, Any], str, bool, bool]:
    profile_id = profile_id.strip() if isinstance(profile_id, str) else profile_id
    if not isinstance(profile_id, str) or _SAFE_PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError("notification profile_id is required")
    if not isinstance(channels, tuple) or not channels or any(
        not isinstance(channel, str) or channel not in _CHANNELS for channel in channels
    ):
        raise ValueError("notification channels are invalid")
    if len(set(channels)) != len(channels):
        raise ValueError("notification channels must be unique")
    if not isinstance(telegram_enabled, bool):
        raise ValueError("telegram_enabled must be boolean")
    if "telegram" in channels and telegram_enabled is not True:
        raise ValueError("telegram requires explicit activation")
    if event_policy is None:
        event_policy = {}
    if not isinstance(event_policy, dict) or any(
        not isinstance(event, str) or event not in _EVENTS
        or not isinstance(mode, str) or mode not in _EVENT_MODES
        for event, mode in event_policy.items()
    ):
        raise ValueError("notification event policy is invalid")
    if retry_policy is None:
        retry_policy = {}
    if not isinstance(retry_policy, dict) or any(
        not isinstance(key, str) or key not in _RETRY_KEYS for key in retry_policy
    ):
        raise ValueError("notification retry policy is invalid")
    max_attempts = retry_policy.get("max_attempts", 3)
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= _MAX_ATTEMPTS
    ):
        raise ValueError("notification max_attempts must be an integer from 1 to 10")
    if not isinstance(confirmation_policy, str) or confirmation_policy not in _CONFIRMATION_POLICIES:
        raise ValueError("notification confirmation policy is invalid")
    if not isinstance(sensitive, bool):
        raise ValueError("sensitive must be boolean")
    return (
        profile_id,
        channels,
        dict(event_policy),
        {**retry_policy, "max_attempts": max_attempts},
        confirmation_policy,
        sensitive,
        telegram_enabled,
    )


@dataclass(frozen=True)
class NotificationProfile:
    profile_id: str
    channels: tuple[str, ...] = ("local",)
    event_policy: dict[str, str] | None = None
    retry_policy: dict[str, Any] | None = None
    confirmation_policy: str = "user_required"
    sensitive: bool = False
    telegram_enabled: bool = False

    def __post_init__(self) -> None:
        values = _validate_profile_values(
            self.profile_id,
            self.channels,
            self.event_policy,
            self.retry_policy,
            self.confirmation_policy,
            self.sensitive,
            self.telegram_enabled,
        )
        for name, value in zip(
            ("profile_id", "channels", "event_policy", "retry_policy", "confirmation_policy", "sensitive", "telegram_enabled"),
            values,
        ):
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "channels": list(self.channels),
            "event_policy": dict(self.event_policy or {}),
            "retry_policy": dict(self.retry_policy or {}),
            "confirmation_policy": self.confirmation_policy,
            "sensitive": self.sensitive,
            "telegram_enabled": self.telegram_enabled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NotificationProfile":
        if not isinstance(value, dict):
            raise ValueError("notification profile must be an object")
        allowed = {"profile_id", "channels", "event_policy", "retry_policy", "confirmation_policy", "sensitive", "telegram_enabled"}
        if set(value) - allowed:
            raise ValueError("notification profile contains unknown fields")
        channels = value.get("channels", ["local"])
        if not isinstance(channels, list):
            raise ValueError("notification channels must be a list")
        return cls(
            profile_id=value.get("profile_id"),
            channels=tuple(channels),
            event_policy=value.get("event_policy"),
            retry_policy=value.get("retry_policy"),
            confirmation_policy=value.get("confirmation_policy", "user_required"),
            sensitive=value.get("sensitive", False),
            telegram_enabled=value.get("telegram_enabled", False),
        )


class NotificationStore:
    def __init__(self, root: Path | str):
        self.root = Path(root) / "state" / "notifications"
        self.profile_path = self.root / "profiles.json"
        self.delivery_path = self.root / "deliveries.jsonl"
        self.profile_lock_path = self.root / "profiles.lock"
        self.delivery_lock_path = self.root / "deliveries.lock"

    @contextmanager
    def profile_lock(self):
        with file_lock(self.profile_lock_path):
            yield

    @contextmanager
    def delivery_lock(self):
        with file_lock(self.delivery_lock_path):
            yield

    def save_profile(self, profile: NotificationProfile) -> None:
        if not isinstance(profile, NotificationProfile):
            raise TypeError("profile must be a NotificationProfile")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.profile_lock():
            payload = self.load_profiles()
            payload[profile.profile_id] = profile.to_dict()
            fd, temporary_name = tempfile.mkstemp(prefix="profiles-", suffix=".tmp", dir=self.root)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary, self.profile_path)
            finally:
                temporary.unlink(missing_ok=True)

    def load_profiles(self) -> dict[str, dict[str, Any]]:
        if not self.profile_path.is_file():
            return {}
        try:
            value = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _read_deliveries_unlocked(self, *, since_days: int | None = 90) -> list[dict[str, Any]]:
        if not self.delivery_path.is_file():
            return []
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=since_days)
            if since_days is not None
            else None
        )
        result = []
        for line in self.delivery_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    continue
                recorded = datetime.fromisoformat(str(item.get("recorded_at")).replace("Z", "+00:00"))
                if recorded.tzinfo is None or recorded.utcoffset() is None:
                    continue
            except (ValueError, TypeError, json.JSONDecodeError, OSError):
                continue
            if cutoff is None or recorded >= cutoff:
                result.append(item)
        return result

    def _append_delivery_unlocked(self, delivery: dict[str, Any]) -> bool:
        if not isinstance(delivery, dict):
            raise ValueError("delivery record must be an object")
        required = {"delivery_key", "task_id", "event", "channel", "status", "attempt", "retryable", "event_payload", "local_reference"}
        if not required <= set(delivery) or not set(delivery) <= _DELIVERY_FIELDS:
            raise ValueError("delivery record fields are invalid")
        if "message" in delivery and delivery["status"] != "queued":
            raise ValueError("delivery message is only allowed for queued records")
        if not isinstance(delivery.get("delivery_key"), str) or _SAFE_RECORD_REFERENCE.fullmatch(delivery["delivery_key"]) is None:
            raise ValueError("delivery_key is not a bounded safe identifier")
        if not isinstance(delivery.get("task_id"), str) or _SAFE_TASK_ID.fullmatch(delivery["task_id"]) is None:
            raise ValueError("delivery task_id is invalid")
        if not isinstance(delivery.get("event"), str) or delivery["event"] not in _EVENTS:
            raise ValueError("delivery event is invalid")
        channel = delivery["channel"]
        if channel not in _CHANNELS:
            raise ValueError("delivery channel is invalid")
        status = delivery["status"]
        if status not in _DELIVERY_STATUSES:
            raise ValueError("delivery status is invalid")
        if not isinstance(delivery["retryable"], bool):
            raise ValueError("delivery retryable must be boolean")
        event_payload = delivery.get("event_payload")
        if not isinstance(event_payload, Mapping):
            raise ValueError("delivery event_payload is invalid")
        payload_event = event_payload.get("event")
        payload_version = event_payload.get("state_version")
        if (
            not isinstance(payload_event, str)
            or _SAFE_RECORD_REFERENCE.fullmatch(payload_event) is None
            or payload_event != delivery["event"]
            or not isinstance(payload_version, str)
            or _SAFE_RECORD_REFERENCE.fullmatch(payload_version) is None
        ):
            raise ValueError("delivery event_payload identity is invalid")
        expected_key = f"{delivery['task_id']}:{delivery['event']}:{channel}:{payload_version}"
        if delivery["delivery_key"] != expected_key:
            raise ValueError("delivery_key does not match event identity")
        payload_event_id = event_payload.get("event_id")
        if payload_event_id is not None and (
            not isinstance(payload_event_id, str)
            or _SAFE_RECORD_REFERENCE.fullmatch(payload_event_id) is None
        ):
            raise ValueError("delivery event_payload event_id is invalid")
        local_reference = delivery.get("local_reference")
        if (
            not isinstance(local_reference, str)
            or not 1 <= len(local_reference) <= 256
            or local_reference.startswith("/")
            or "\\" in local_reference
            or ".." in local_reference.split("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in local_reference)
        ):
            raise ValueError("delivery local_reference is invalid")
        message = delivery.get("message")
        if message is not None and (
            not isinstance(message, str)
            or len(message) > 10000
            or any(ord(char) < 32 and char not in "\n\t" for char in message)
            or _value_is_secret(message)
        ):
            raise ValueError("delivery message is unsafe")
        attempt = delivery.get("attempt", 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("delivery attempt must be a positive integer")
        existing = self._read_deliveries_unlocked(since_days=None)
        matching = [item for item in existing if item.get("delivery_key") == delivery["delivery_key"]]
        if any(item.get("status") == "delivered" for item in matching):
            return False
        if any(item.get("attempt", 1) >= attempt for item in matching):
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        with self.delivery_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {**delivery, "attempt": attempt, "recorded_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False,
                ) + "\n"
            )
        return True

    def append_delivery(self, delivery: dict[str, Any]) -> bool:
        with self.delivery_lock():
            return self._append_delivery_unlocked(delivery)

    def read_deliveries(self, *, since_days: int = 90) -> list[dict[str, Any]]:
        if isinstance(since_days, bool) or not isinstance(since_days, int) or since_days < 0:
            raise ValueError("since_days must be a non-negative integer")
        with self.delivery_lock():
            return self._read_deliveries_unlocked(since_days=since_days)
