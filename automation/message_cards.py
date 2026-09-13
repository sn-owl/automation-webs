"""Platform-neutral business decision message cards.

A :class:`TaskMessageCard` is presentation data only.  It contains the small,
safe subset of task context needed for a human decision; rendering never
consults Core state, invokes ``taskctl``, or performs a decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import ClassVar, Final

from automation.events import _value_is_secret


_ACTIONS: Final[tuple[str, ...]] = ("approve", "reject", "modify", "defer")
_SAFE_TASK_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_FORBIDDEN_CONTENT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:raw\s+body|passwords?|passwds?|pwds?|credentials?|cookies?|"
    r"tokens?|commands?|secrets?)\b"
    r"|(?:https?://|file://|/|\\)",
    re.IGNORECASE,
)
_SECRET_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:password|passwd|pwd|credential|credentials|secret|secrets|"
    r"api[-_ ]?(?:key|token)|access[-_]?token|refresh[-_]?token|"
    r"session[-_]?(?:id|token|cookie)?)\b"
    r"(?:\s*(?:[:=]|is|are|of)\s*|\s+)\S+",
    re.IGNORECASE,
)
_AUTH_BEARER: Final[re.Pattern[str]] = re.compile(
    r"\bauthorization\s+(?:header\s+)?bearer\s+\S+|\bbearer\s+\S+",
    re.IGNORECASE,
)
_AUTH_HEADER: Final[re.Pattern[str]] = re.compile(
    r"\bauthorization(?:\s+header)?\s*(?:(?::|=)\s*|\s+(?:bearer\s+)?\S+)",
    re.IGNORECASE,
)
_SHELL_COMMAND: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w-])(?:sudo\s+)?(?:bash|sh|zsh|fish|"
    r"rm|mv|cp|chmod|chown|curl|wget|git|make|npm|pip|python(?:3)?|"
    r"ruby|perl|php|taskctl|echo|ls|cat|grep|docker|podman|kubectl|node|java|go|cargo|pytest|"
    r"unzip|tar|ssh|scp|ps|kill|mkdir|touch)\b(?:\s|$)"
    r"|(?:&&|\|\||;|`|\$\(|[<>])",
    re.IGNORECASE,
)


def _safe_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = " ".join(value.split())
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    if (
        _value_is_secret(value)
        or _FORBIDDEN_CONTENT.search(value)
        or _SECRET_CONTEXT.search(value)
        or _AUTH_BEARER.search(value)
        or _AUTH_HEADER.search(value)
        or _SHELL_COMMAND.search(value)
    ):
        raise ValueError(f"{field} contains unsafe content")
    return value




@dataclass(frozen=True)
class TaskMessageCard:
    """Immutable, safe presentation data for one task decision.

    ``available_actions`` is deliberately closed: a card can present all four
    Core decision actions, but cannot introduce a command or an alternate
    approval syntax.  Raw source, artifact contents, credentials, and command
    text are not fields on this type.
    """

    task_id: str
    task_version: int
    proposal: str
    assessment: str
    evidence: tuple[str, ...]
    risk: str
    impact: str
    available_actions: tuple[str, ...] = _ACTIONS

    ACTIONS: ClassVar[tuple[str, ...]] = _ACTIONS

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id is required")
        task_id = self.task_id.strip()
        if _SAFE_TASK_ID.fullmatch(task_id) is None:
            raise ValueError("task_id must be a safe identifier")
        object.__setattr__(self, "task_id", task_id)

        if isinstance(self.task_version, bool) or not isinstance(self.task_version, int):
            raise ValueError("task_version must be a positive integer")
        if self.task_version < 1:
            raise ValueError("task_version must be a positive integer")

        object.__setattr__(self, "proposal", _safe_text(self.proposal, "proposal"))
        object.__setattr__(self, "assessment", _safe_text(self.assessment, "assessment"))
        object.__setattr__(self, "risk", _safe_text(self.risk, "risk"))
        object.__setattr__(self, "impact", _safe_text(self.impact, "impact"))

        if not isinstance(self.evidence, (tuple, list)) or not self.evidence:
            raise ValueError("evidence must be a non-empty sequence")
        evidence = tuple(_safe_text(entry, "evidence entries") for entry in self.evidence)
        object.__setattr__(self, "evidence", evidence)

        actions = tuple(self.available_actions) if isinstance(self.available_actions, (tuple, list)) else None
        if actions != _ACTIONS:
            raise ValueError("available_actions must contain the four Core task actions")
        object.__setattr__(self, "available_actions", actions)


def render_task_message(card: TaskMessageCard) -> str:
    """Render deterministic human-facing text without changing task state."""

    if not isinstance(card, TaskMessageCard):
        raise TypeError("card must be a TaskMessageCard")

    # Every free-form value has already been bounded and validated by the
    # immutable card; render the safe values rather than presence markers.
    lines = [
        f"task_name: {card.proposal}",
        f"reason: {card.assessment}",
        f"next_action: {card.impact}",
        "actions:",
    ]
    lines.extend(f"/task {action} {card.task_id}" for action in card.available_actions)
    return "\n".join(lines)
