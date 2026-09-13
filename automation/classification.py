"""Extensible task-classification contract owned by Core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from automation._contract import _evidence, _unit_interval


@dataclass(frozen=True)
class Classification:
    """One validated classification with explicit ownership and uncertainty."""

    OWNERSHIPS = ("내 업무", "다른 담당자", "정보 부족")
    FIELDS = frozenset(
        {
            "responsibility",
            "task_type",
            "size",
            "ownership",
            "primary_role",
            "responsibility_scope",
            "collaboration",
            "risk_flags",
            "confidence",
            "evidence",
            "next_action",
            "pattern_match",
            "unmatched_aspects",
        }
    )

    responsibility: str
    task_type: str
    size: str
    ownership: str
    primary_role: str
    responsibility_scope: str
    collaboration: tuple[str, ...]
    risk_flags: tuple[str, ...]
    confidence: float
    evidence: tuple[str, ...]
    next_action: str
    pattern_match: dict[str, Any]
    unmatched_aspects: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Classification":
        if not isinstance(data, dict) or set(data) != cls.FIELDS:
            raise ValueError("classification must contain exactly the required fields")

        def text(key: str) -> str:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be non-empty text")
            return value.strip()

        def texts(key: str) -> tuple[str, ...]:
            value = data.get(key)
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"{key} must be a list of text")
            return tuple(item.strip() for item in value)

        ownership = data.get("ownership")
        if ownership not in cls.OWNERSHIPS:
            raise ValueError("ownership must be 내 업무, 다른 담당자, or 정보 부족")
        pattern_match = data.get("pattern_match")
        if not isinstance(pattern_match, dict):
            raise ValueError("pattern_match must be an object")
        return cls(
            responsibility=text("responsibility"),
            task_type=text("task_type"),
            size=text("size"),
            ownership=ownership,
            primary_role=text("primary_role"),
            responsibility_scope=text("responsibility_scope"),
            collaboration=texts("collaboration"),
            risk_flags=texts("risk_flags"),
            confidence=_unit_interval(data, "confidence"),
            evidence=_evidence(data),
            next_action=text("next_action"),
            pattern_match=dict(pattern_match),
            unmatched_aspects=texts("unmatched_aspects"),
        )

    @classmethod
    def from_rule(
        cls,
        verdict: dict[str, Any],
        *,
        evidence: list[str],
        rule_ids: list[str],
    ) -> "Classification":
        """Make deterministic legacy rule matches honest about missing ownership."""
        return cls.from_dict(
            {
                "responsibility": verdict.get("responsibility"),
                "task_type": verdict.get("task_type"),
                "size": verdict.get("size"),
                "ownership": "정보 부족",
                "primary_role": verdict.get("responsibility"),
                "responsibility_scope": "규칙이 식별한 업무 영역",
                "collaboration": [],
                "risk_flags": [],
                "confidence": verdict.get("confidence"),
                "evidence": evidence,
                "next_action": "담당 소유권과 요청 범위를 확인하세요.",
                "pattern_match": {"rule_ids": list(rule_ids)},
                "unmatched_aspects": ["담당 소유권"],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "responsibility": self.responsibility,
            "task_type": self.task_type,
            "size": self.size,
            "ownership": self.ownership,
            "primary_role": self.primary_role,
            "responsibility_scope": self.responsibility_scope,
            "collaboration": list(self.collaboration),
            "risk_flags": list(self.risk_flags),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "next_action": self.next_action,
            "pattern_match": dict(self.pattern_match),
            "unmatched_aspects": list(self.unmatched_aspects),
        }
