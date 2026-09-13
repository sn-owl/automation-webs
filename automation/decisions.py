"""Immutable human decision contract.

A decision is a Core-owned record of one human action on one task version.  It
only describes the decision; applying it is outside this contract.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import re
from types import MappingProxyType
from typing import Any, Mapping

from automation.classification import Classification

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


_MISSING = object()

# The keys Core reads back from decision details are deliberately closed.
# A reject can either carry a corrected classification or explicitly say the
# work is outside the operator's scope. A legacy reject without details stays
# valid but carries no learning signal.
CORRECTION_KEY = "classification"
REJECT_SIGNAL_KEY = "reject_signal"
REJECT_SIGNALS = ("classification_correction", "out_of_scope")


def _required(data: Mapping[str, Any], key: str) -> Any:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        raise ValueError(f"{key} is required")
    return value


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = _required(data, key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _task_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("task_version must be a positive integer")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is required")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601") from exc
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("details object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class Decision:
    """One immutable human decision for a specific task version."""

    ACTIONS = ("approve", "reject", "modify", "defer")

    actor: str
    task_id: str
    task_version: int
    action: str
    reason: str
    timestamp: str
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor", _text(self.actor, "actor"))
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "task_version", _task_version(self.task_version))
        if self.action not in self.ACTIONS:
            raise ValueError(f"action must be one of {self.ACTIONS}")
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))

        if self.action == "modify":
            if not isinstance(self.details, dict) or not self.details:
                raise ValueError("modify decisions require non-empty object details")
            _validate_correction(self.details)
            object.__setattr__(self, "details", _freeze(self.details))
        elif self.details is not None:
            if not isinstance(self.details, dict):
                raise ValueError("details must be an object")
            if self.action == "approve":
                _validate_execution_binding(self.details)
            if self.action == "reject":
                _validate_rejection(self.details)
            _validate_correction(self.details)
            object.__setattr__(self, "details", _freeze(self.details))

    def correction(self) -> Classification | None:
        """Return the corrected classification this decision carries, if any."""
        if self.details is None:
            return None
        value = self.details.get(CORRECTION_KEY)
        if value is None:
            return None
        return Classification.from_dict(_thaw(value))
    def rejection_signal(self) -> str | None:
        """Return a structured reject signal, if this is a reject decision."""
        if self.action != "reject" or self.details is None:
            return None
        value = self.details.get(REJECT_SIGNAL_KEY)
        return value if isinstance(value, str) else None

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        current_task_version: int | None = None,
    ) -> "Decision":
        if not isinstance(data, dict):
            raise ValueError("decision must be an object")
        allowed = {"actor", "task_id", "task_version", "action", "reason", "timestamp", "details"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown decision field: {sorted(unknown)[0]}")

        decision = cls(
            actor=_required_text(data, "actor"),
            task_id=_required_text(data, "task_id"),
            task_version=_required(data, "task_version"),
            action=_required(data, "action"),
            reason=_required_text(data, "reason"),
            timestamp=_required(data, "timestamp"),
            details=data.get("details"),
        )
        if current_task_version is not None:
            decision.validate_for_task_version(current_task_version)
        return decision

    def validate_for_task_version(self, current_task_version: int) -> "Decision":
        current = _task_version(current_task_version)
        if self.action == "approve" and self.task_version != current:
            raise ValueError(
                f"stale approval: task_version={self.task_version}, current_task_version={current}"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "actor": self.actor,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "action": self.action,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }
        if self.details is not None:
            payload["details"] = _thaw(self.details)
        return payload


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _validate_correction(details: Mapping[str, Any]) -> None:
    """Hold ``details["classification"]`` to the S4 contract, if it is present.

    Absent is fine — most decisions carry prose, not a correction. Present but
    malformed is rejected here so the operator sees it while they still
    remember the task, instead of the pattern pipeline silently skipping it.
    """
    value = details.get(CORRECTION_KEY, _MISSING)
    if value is _MISSING:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"details.{CORRECTION_KEY} must be an object")
    Classification.from_dict(_thaw(value))

def _validate_rejection(details: Mapping[str, Any]) -> None:
    """Validate the optional structured meaning of a reject decision.

    Old reject records without details remain readable and explicitly carry no
    learning signal. New structured records distinguish a corrected
    classification from work outside the operator's scope.
    """
    signal = details.get(REJECT_SIGNAL_KEY, _MISSING)
    if signal is _MISSING:
        if CORRECTION_KEY in details:
            raise ValueError(
                f"details.{CORRECTION_KEY} requires details.{REJECT_SIGNAL_KEY}"
            )
        return
    if signal not in REJECT_SIGNALS:
        raise ValueError(
            f"details.{REJECT_SIGNAL_KEY} must be one of {REJECT_SIGNALS}"
        )
    has_correction = CORRECTION_KEY in details
    if signal == "classification_correction" and not has_correction:
        raise ValueError(
            f"reject signal {signal!r} requires details.{CORRECTION_KEY}"
        )
    if signal == "out_of_scope" and has_correction:
        raise ValueError(
            f"reject signal {signal!r} cannot carry details.{CORRECTION_KEY}"
        )

def _validate_execution_binding(details: Mapping[str, Any]) -> None:
    value = details.get("execution", _MISSING)
    if value is _MISSING:
        return
    if not isinstance(value, Mapping):
        raise ValueError("details.execution must be an object")
    recipe_id = value.get("recipe_id")
    input_hash = value.get("input_hash")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        raise ValueError("details.execution.recipe_id is required")
    if not isinstance(input_hash, str) or _SHA256.fullmatch(input_hash) is None:
        raise ValueError("details.execution.input_hash must be a sha256")


@dataclass(frozen=True)
class PatternDecision:
    """Immutable human decision bound to one exact scoped pattern revision."""

    ACTIONS = ("approve", "reject", "modify", "defer")
    scope: str
    pattern_id: str
    pattern_revision: int
    definition_digest: str
    action: str
    actor: str
    reason: str
    timestamp: str

    def __post_init__(self) -> None:
        from automation.patterns import validate_pattern_id, validate_scope

        object.__setattr__(self, "scope", validate_scope(self.scope))
        object.__setattr__(self, "pattern_id", validate_pattern_id(self.pattern_id))
        if isinstance(self.pattern_revision, bool) or not isinstance(self.pattern_revision, int) or self.pattern_revision < 1:
            raise ValueError("pattern_revision must be a positive integer")
        if not isinstance(self.definition_digest, str) or _SHA256.fullmatch(self.definition_digest) is None:
            raise ValueError("definition_digest must be a sha256 digest")
        if self.action not in self.ACTIONS:
            raise ValueError(f"action must be one of {self.ACTIONS}")
        object.__setattr__(self, "actor", _text(self.actor, "actor"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PatternDecision":
        if not isinstance(data, Mapping):
            raise ValueError("pattern decision must be an object")
        allowed = {"scope", "pattern_id", "pattern_revision", "definition_digest", "action", "actor", "reason", "timestamp"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown pattern decision field: {sorted(unknown)[0]}")
        missing = allowed - set(data)
        if missing:
            raise ValueError(f"{sorted(missing)[0]} is required")
        return cls(**{key: data[key] for key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "pattern_id": self.pattern_id,
            "pattern_revision": self.pattern_revision,
            "definition_digest": self.definition_digest,
            "action": self.action,
            "actor": self.actor,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }
