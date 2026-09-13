"""Deliver task decision requests as safe, bounded chat notifications.

This is a presentation adapter over Task 38's message cards. It renders text
and hands it to an injected transport; it never changes task state, never
resolves a recipe, and never reads a credential. The Gateway owns the bot
token and the chat binding, so nothing here needs one -- a module that cannot
read a secret cannot leak one.

The notification text contains only the event, masked task name, safe reason,
next action, local task reference, and Core decision actions. Artifact contents,
metadata, filesystem paths, and raw source never cross this boundary.

The actions offered are Core's ``/task <action> <task_id>`` contract. Hermes's
own ``/approve`` is tool-call permission, not business approval, and must not
be mixed in here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlsplit

from automation.message_cards import TaskMessageCard, render_task_message
from automation.hermes_sanitize import mask_for_hermes

# Telegram's hard limit is 4096 characters; stay under it with room for the
# chunk to end on a line boundary.
MAX_MESSAGE_LENGTH = 3500
_MARKDOWN_V2_SPECIALS = "_*[]()~`>#+-=|{}.!"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


_SAFE_TASK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_VERSION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SAFE_EVENT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")


def _bounded_reference(value: Any, *, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value.strip()) is None:
        raise NotificationError(f"{field} must be a bounded safe identifier")
    return value.strip()

class NotificationError(RuntimeError):
    """Raised when a notification cannot be built or delivered safely."""


def escape_markdown(text: str) -> str:
    """Escape MarkdownV2 control characters so text renders as text."""
    if not isinstance(text, str):
        raise NotificationError("text must be a string")
    return "".join(
        f"\\{character}" if character in _MARKDOWN_V2_SPECIALS else character
        for character in text
    )




def split_notification(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if not isinstance(text, str):
        raise NotificationError("text must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise NotificationError("limit must be a positive integer")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        boundary = window.rfind("\n")
        cut = boundary + 1 if boundary > 0 else limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks

def render_notification(card: TaskMessageCard, *, event: str, local_reference: str) -> str:
    """Render only the bounded C7 notification fields and Core actions."""
    if not isinstance(card, TaskMessageCard):
        raise NotificationError("card must be a TaskMessageCard")
    event = _bounded_reference(event, field="event", pattern=_SAFE_VERSION)
    if event not in _IMMEDIATE_EVENTS and event not in _BATCHED_EVENTS:
        raise NotificationError("event is not supported")
    local_reference = _safe_local_reference(card.task_id, local_reference)
    lines = [
        escape_markdown(f"event: {event}"),
        escape_markdown(f"local_reference: {local_reference}"),
    ]
    lines.extend(escape_markdown(line) for line in render_task_message(card).splitlines())
    return "\n".join(lines)


def build_gateway_request(base_url: str, chat_id: str, text: str) -> dict[str, Any]:
    """Build the local Gateway delivery request.

    The Gateway is a local process. A non-loopback destination would mean
    sending task context to some other host, so it is refused outright.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise NotificationError("gateway base URL must be a localhost HTTP URL")
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        raise NotificationError("gateway base URL must be a localhost HTTP URL") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise NotificationError("gateway base URL must be a localhost HTTP URL")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise NotificationError("chat_id must be a non-empty string")
    if not isinstance(text, str) or not text:
        raise NotificationError("text must be a non-empty string")

    return {
        "url": base_url.rstrip("/") + "/notify",
        "method": "POST",
        "json": {"chat_id": chat_id, "text": text},
    }


def deliver(
    base_url: str,
    chat_id: str,
    text: str,
    *,
    transport: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Send each chunk through ``transport`` and report the outcome.

    Delivery is best-effort reporting, never a state transition. When the
    Gateway is unavailable the result says so deterministically and names the
    Extension fallback; it does not retry, and it does not raise into a caller
    that may be mid-pipeline.
    """
    if not callable(transport):
        raise NotificationError("transport must be callable")

    chunks = split_notification(text)
    for chunk in chunks:
        request = build_gateway_request(base_url, chat_id, chunk)
        try:
            transport(request)
        except Exception:
            return {"delivered": False, "fallback": "extension", "chunks": len(chunks)}
    return {"delivered": True, "fallback": None, "chunks": len(chunks)}

_IMMEDIATE_EVENTS = frozenset({"new_task", "classification_review", "failure"})
_BATCHED_EVENTS = frozenset({"preparation_started", "preparation_completed"})


def _event_mode(profile: Any, event: str) -> str:
    policy = getattr(profile, "event_policy", None) or {}
    mode = policy.get(event)
    if mode is None:
        mode = "batched" if event in _BATCHED_EVENTS else "immediate"
    if mode not in {"immediate", "batched"}:
        raise NotificationError("notification event policy is invalid")
    return mode



def _delivery_records(store, *, locked: bool = False) -> list[dict[str, Any]]:
    if store is None:
        return []
    try:
        reader = getattr(store, "_read_deliveries_unlocked", None)
        records = reader(since_days=None) if locked and callable(reader) else store.read_deliveries()
    except Exception:
        return []
    return [record for record in records if isinstance(record, Mapping)]
def _retryable_error(error: BaseException) -> bool:
    return not isinstance(error, NotificationError)


def _max_attempts(profile: Any) -> int:
    value = (getattr(profile, "retry_policy", None) or {}).get("max_attempts", 3)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise NotificationError("notification max_attempts must be an integer from 1 to 10")
    return value

def _safe_local_reference(task_id: str, local_reference: str | None) -> str:
    if local_reference is None:
        return f"state/tasks/{task_id}.json"
    if (
        not isinstance(local_reference, str)
        or not 1 <= len(local_reference) <= 256
        or local_reference.startswith("/")
        or "\\" in local_reference
        or ".." in local_reference.split("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in local_reference)
    ):
        raise NotificationError("local_reference must be a bounded relative local path")
    return local_reference

def _delivery_guard(store):
    lock = getattr(store, "delivery_lock", None) if store is not None else None
    return lock() if callable(lock) else nullcontext()


def _latest_delivery(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        key = record.get("delivery_key")
        attempt = record.get("attempt", 1)
        if not isinstance(key, str) or not isinstance(attempt, int) or isinstance(attempt, bool):
            continue
        prior = latest.get(key)
        prior_attempt = prior.get("attempt", 1) if prior else 0
        if not isinstance(prior_attempt, int) or isinstance(prior_attempt, bool):
            prior_attempt = 0
        if prior is None or attempt >= prior_attempt:
            latest[key] = record
    return latest


def _next_attempt(records: Sequence[Mapping[str, Any]], delivery_key: str) -> int:
    attempts = [
        record.get("attempt", 1)
        for record in records
        if record.get("delivery_key") == delivery_key
        and isinstance(record.get("attempt", 1), int)
        and not isinstance(record.get("attempt", 1), bool)
    ]
    return max(attempts, default=0) + 1


def _event_payload(event: str, event_id: str | None, state_version: str) -> dict[str, str]:
    payload = {"event": event, "state_version": state_version}
    if event_id:
        payload["event_id"] = event_id
    return payload


def _sensitive_notification(event: str, local_reference: str) -> str:
    return "\n".join(
        [
            escape_markdown(f"event: {event}"),
            escape_markdown(f"local_reference: {local_reference}"),
        ]
    )


def _append_delivery(
    store,
    *,
    delivery_key: str,
    task_id: str,
    event: str,
    channel: str,
    status: str,
    attempt: int,
    retryable: bool,
    event_id: str | None,
    state_version: str,
    local_reference: str,
    text: str | None = None,
    locked: bool = False,
) -> None:
    if store is None:
        return
    record: dict[str, Any] = {
        "delivery_key": delivery_key,
        "task_id": task_id,
        "event": event,
        "channel": channel,
        "status": status,
        "attempt": attempt,
        "retryable": retryable,
        "event_payload": _event_payload(event, event_id, state_version),
        "local_reference": local_reference,
    }
    if text is not None:
        record["message"] = text
    writer = getattr(store, "_append_delivery_unlocked", None) if locked else None
    (writer(record) if callable(writer) else store.append_delivery(record))


def deliver_with_profile(
    profile,
    *,
    task_id: str,
    event: str,
    card: TaskMessageCard,
    store=None,
    transport: Callable[[dict[str, Any]], None] | None = None,
    state_version: str = "1",
    event_id: str | None = None,
    local_reference: str | None = None,
    sensitive: bool | None = None,
) -> dict[str, Any]:
    """Deliver one Core event under immediate/batched and retry policy.

    Delivery records are append-only.  A stable delivery key deduplicates a
    successful attempt, while retryable failures receive a new attempt record.
    No channel outcome changes task state.
    """
    task_id = _bounded_reference(task_id, field="task_id", pattern=_SAFE_TASK_ID)
    if not isinstance(event, str) or not event.strip():
        raise NotificationError("event must be a non-empty string")
    event = _bounded_reference(event, field="event", pattern=_SAFE_VERSION)
    if event not in _IMMEDIATE_EVENTS and event not in _BATCHED_EVENTS:
        raise NotificationError("event is not supported")
    if not isinstance(state_version, str) or not state_version.strip():
        raise NotificationError("state_version must be a non-empty string")
    state_version = _bounded_reference(state_version, field="state_version", pattern=_SAFE_VERSION)
    if event_id is not None:
        event_id = _bounded_reference(event_id, field="event_id", pattern=_SAFE_EVENT_ID)
    local_reference = _safe_local_reference(task_id, local_reference)
    mode = _event_mode(profile, event)
    max_attempts = _max_attempts(profile)
    if sensitive is not None and not isinstance(sensitive, bool):
        raise NotificationError("sensitive override must be boolean")
    is_sensitive = getattr(profile, "sensitive", False) if sensitive is None else sensitive
    if not isinstance(is_sensitive, bool):
        raise NotificationError("profile sensitive must be boolean")
    text = (
        _sensitive_notification(event, local_reference)
        if is_sensitive
        else render_notification(card, event=event, local_reference=local_reference)
    )
    outcome: dict[str, Any] = {
        "task_id": task_id,
        "event": event,
        "event_id": event_id,
        "state_version": state_version,
        "local_reference": local_reference,
        "policy": mode,
        "sensitive": is_sensitive,
        "channels": {},
        "attempts": {},
        "status": "delivered",
    }
    channels = tuple(getattr(profile, "channels", ("local",)))
    for channel in channels:
        delivery_key = f"{task_id}:{event}:{channel}:{state_version}"
        with _delivery_guard(store):
            records = _delivery_records(store, locked=True)
            latest = _latest_delivery(records)
            prior = latest.get(delivery_key)
            if prior is not None and prior.get("status") == "delivered":
                status = "delivered"
                attempt = prior.get("attempt", 1)
            elif prior is not None and prior.get("status") == "queued":
                status = "queued"
                attempt = prior.get("attempt", 1)
            elif prior is not None and prior.get("status") == "failed" and prior.get("retryable") is False:
                status = "failed"
                attempt = prior.get("attempt", 1)
            else:
                attempt = _next_attempt(records, delivery_key)
                if attempt > max_attempts:
                    status = "failed"
                    attempt -= 1
                else:
                    retryable = False
                    status = "failed"
                    if mode == "batched":
                        status = "queued" if store is not None else "failed"
                        retryable = store is not None
                    elif channel in {"local", "dashboard", "telegram"}:
                        if transport is not None:
                            try:
                                for chunk in split_notification(text):
                                    transport({"channel": channel, "task_id": task_id, "text": chunk})
                                status = "delivered"
                            except Exception as exc:
                                retryable = _retryable_error(exc) and attempt < max_attempts
                        elif store is not None:
                            status = "queued"
                            retryable = True
                    if store is not None:
                        _append_delivery(
                            store,
                            delivery_key=delivery_key,
                            task_id=task_id,
                            event=event,
                            channel=channel,
                            status=status,
                            attempt=attempt,
                            retryable=retryable,
                            event_id=event_id,
                            state_version=state_version,
                            local_reference=local_reference,
                            text=text if status == "queued" else None,
                            locked=True,
                        )
        outcome["channels"][channel] = status
        outcome["attempts"][channel] = attempt
        if status == "failed":
            outcome["status"] = "failed"
        elif status == "queued" and outcome["status"] != "failed":
            outcome["status"] = "queued"
    if not channels:
        outcome["status"] = "failed"
    return outcome


def flush_batched(
    store,
    *,
    transport: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Flush queued notification records without touching task state."""
    result = {"flushed": 0, "failed": 0, "skipped": 0}
    keys = tuple(_latest_delivery(_delivery_records(store)).keys())
    for key in keys:
        with _delivery_guard(store):
            records = _delivery_records(store, locked=True)
            record = _latest_delivery(records).get(key)
            if record is None or record.get("status") != "queued":
                continue
            channel = record.get("channel")
            text = record.get("message")
            if not isinstance(channel, str) or not isinstance(text, str):
                result["failed"] += 1
                continue
            if transport is None:
                result["skipped"] += 1
                continue
            status = "failed"
            retryable = False
            try:
                for chunk in split_notification(text):
                    transport({"channel": channel, "task_id": record["task_id"], "text": chunk})
                status = "delivered"
            except Exception as exc:
                retryable = _retryable_error(exc)
            attempt = _next_attempt(records, key)
            event_payload = record.get("event_payload")
            if not isinstance(event_payload, Mapping):
                event_payload = {}
            original_event = event_payload.get("event", record.get("event"))
            original_state_version = event_payload.get("state_version", "1")
            original_event_id = event_payload.get("event_id")
            _append_delivery(
                store,
                delivery_key=key,
                task_id=str(record["task_id"]),
                event=str(original_event),
                channel=channel,
                status=status,
                attempt=attempt,
                retryable=retryable,
                event_id=original_event_id if isinstance(original_event_id, str) else None,
                state_version=str(original_state_version),
                local_reference=str(record.get("local_reference", "")),
                text=text if status == "queued" else None,
                locked=True,
            )
            if status == "delivered":
                result["flushed"] += 1
            else:
                result["failed"] += 1
    return result


def classify_pipeline_event(event: Mapping[str, Any]) -> str | None:
    """Map Core pipeline states to the NotificationDelivery event contract."""
    state = event.get("state")
    stage = event.get("stage")
    if not isinstance(state, str):
        return None
    if state in {"stored", "received"} and stage in {"storage", "input", None}:
        return "new_task"
    if state in {"review_required", "blocked"}:
        return "classification_review"
    if state == "failed" or state.endswith("_failed") or state in {
        "verification_failed",
        "verification_unverifiable",
        "render_failed",
    }:
        return "failure"
    if state in {"executing", "execution_started"}:
        return "preparation_started"
    if state in {"succeeded", "execution_completed", "verification_recorded"}:
        return "preparation_completed"
    return None

def _masked_card_value(value: Any, fallback: str) -> str:
    masked = mask_for_hermes(value).strip()[:200]
    return masked or fallback


def notify_pipeline_event(
    root,
    event: Mapping[str, Any],
    *,
    transport: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Route one persisted Core event to local Notification Profiles.

    It only reads task metadata to build a safe presentation card. Delivery
    failures remain in the notification store and never change task state.
    """
    event_name = classify_pipeline_event(event)
    task_id = event.get("task_id")
    if event_name is None or not isinstance(task_id, str):
        return []
    from automation.dashboard_read import _latest_metadata
    from automation.notification_store import NotificationProfile, NotificationStore
    from automation.task_store import get_task

    try:
        task = get_task(task_id, root=root)
    except Exception:
        return []
    store = NotificationStore(root)
    configured = store.load_profiles()
    profiles = []
    for value in configured.values():
        try:
            profiles.append(NotificationProfile.from_dict(value))
        except (KeyError, TypeError, ValueError):
            continue
    if not profiles:
        profiles = [NotificationProfile(profile_id="default")]
    metadata = _latest_metadata([event]).get(task_id, {})
    review = metadata.get("classification_review") or metadata.get("assessment_review") or {}
    reason = _masked_card_value(
        review.get("reason") if isinstance(review, Mapping) else None,
        f"Core recorded {event_name}.",
    )
    next_action = _masked_card_value(
        review.get("next_action") if isinstance(review, Mapping) else None,
        "user_review",
    )
    title = _masked_card_value(getattr(task, "title", None), "Core pipeline event")
    version_value = event.get("task_version") or getattr(task, "revision", 1) or 1
    if isinstance(version_value, bool) or not isinstance(version_value, int) or version_value < 1:
        return []
    try:
        card = TaskMessageCard(
            task_id=task_id,
            task_version=version_value,
            proposal=title,
            assessment=reason,
            evidence=(reason,),
            risk="read_only",
            impact=next_action,
        )
    except ValueError:
        card = TaskMessageCard(
            task_id=task_id,
            task_version=version_value,
            proposal="Core pipeline event",
            assessment=f"Core recorded {event_name}.",
            evidence=(f"Core recorded {event_name}.",),
            risk="read_only",
            impact="user_review",
    )
    state_version = str(event.get("task_version") or getattr(task, "revision", 1) or 1)
    event_id = event.get("event_id")
    return [
        deliver_with_profile(
            profile,
            task_id=task_id,
            event=event_name,
            card=card,
            store=store,
            transport=transport,
            state_version=state_version,
            event_id=event_id if isinstance(event_id, str) else None,
            local_reference=f"state/tasks/{task_id}.json",
        )
        for profile in profiles
    ]


def _delivered_keys(store) -> set[str]:
    """Keys already recorded as delivered, so the transport is not re-fired."""
    if store is None:
        return set()
    try:
        records = store.read_deliveries()
    except Exception:
        return set()
    return {
        record.get("delivery_key")
        for record in records
        if record.get("status") == "delivered"
    }
