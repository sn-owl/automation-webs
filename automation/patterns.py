"""Scoped Core pattern lifecycle contract (docs/ARCHITECTURE.md 13, 22).

An observed pattern never becomes a Skill directly:

    observation -> candidate -> shadow (raw fixture replay) -> active
                                                              -> suspended
                                                              -> retired

Every pattern is bound to one ``scope``: patterns are per-owner data, never
shared vocabulary, so scope is part of the identity and of the storage path.

``transition`` owns which edges are LEGAL and nothing else. Who authorized a
promotion is a separate question, answered by ``decisions.PatternDecision``
and enforced at the write boundary (``pattern_store.revise_pattern``) and in
``candidates.promote`` -- not by a free-text approval string here (R02).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any

from automation._contract import _evidence, _one_of, _required, _unit_interval


_SAFE_SCOPE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
_SAFE_PATTERN_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,127}\Z")

_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "candidate": ("shadow", "retired"),
    "shadow": ("active", "retired"),
    "active": ("suspended", "retired"),
    "suspended": ("active", "retired"),
    "retired": (),
}


def validate_scope(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_SCOPE.fullmatch(value) is None:
        raise ValueError("scope must match lowercase [a-z0-9._-], 1-64 characters")
    return value


def validate_pattern_id(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_PATTERN_ID.fullmatch(value) is None:
        raise ValueError("pattern_id must be a safe identifier")
    return value


def definition_digest(definition: Any) -> str:
    """Return the canonical sha256 digest bound by PatternDecision."""
    try:
        encoded = json.dumps(
            definition,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("definition must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Pattern:
    KINDS = ("rule", "evaluation_rule", "recipe", "skill", "manual_template")
    STATES = ("candidate", "shadow", "active", "suspended", "retired")

    scope: str
    pattern_id: str
    kind: str
    state: str
    confidence: float
    evidence: tuple[str, ...]
    definition: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", validate_scope(self.scope))
        object.__setattr__(self, "pattern_id", validate_pattern_id(self.pattern_id))
        if self.definition is not None:
            if not isinstance(self.definition, (dict, MappingProxyType)) or not self.definition:
                raise ValueError("definition must be a non-empty object")
            object.__setattr__(self, "definition", _freeze(self.definition))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pattern":
        if not isinstance(data, dict):
            raise ValueError("pattern must be an object")
        allowed = {"scope", "pattern_id", "kind", "state", "confidence", "evidence", "definition"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown pattern field: {sorted(unknown)[0]}")
        return cls(
            scope=_required(data, "scope"),
            pattern_id=_required(data, "pattern_id"),
            kind=_one_of(data, "kind", cls.KINDS),
            state=_one_of(data, "state", cls.STATES),
            confidence=_unit_interval(data, "confidence"),
            evidence=_evidence(data),
            definition=data.get("definition"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": self.scope,
            "pattern_id": self.pattern_id,
            "kind": self.kind,
            "state": self.state,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }
        if self.definition is not None:
            payload["definition"] = _thaw(self.definition)
        return payload

    def transition(self, to_state: str) -> "Pattern":
        if to_state not in self.STATES:
            raise ValueError(f"state must be one of {self.STATES}")
        allowed = _TRANSITIONS.get(self.state, ())
        if to_state not in allowed:
            raise ValueError(f"illegal transition from {self.state!r} to {to_state!r}")
        return replace(self, state=to_state)


def _freeze(value: Any) -> Any:
    if isinstance(value, (dict, MappingProxyType)):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("definition object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, (dict, MappingProxyType)):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
