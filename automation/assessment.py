"""S5 assessment contract.

The automation-possibility verdict (docs/ARCHITECTURE.md §11–12). Task 19 fixes the
output shape and the values it accepts; Task 20's deterministic evaluator is
what produces it.

- automation_level ← manual / assisted / developable / ready
- risk             ← approval-gate ladder from the profile's ``approval`` block,
                      read_only being the floor
- recipe           ← free-form reference string. Required *present* when the
                      level is ``ready`` (a Ready verdict with no recipe is a
                      contradiction); its existence in the Recipe Registry
                      (Task 26) is not checked here. Omitted, never null, for
                      other levels — matching the no-nullable-field policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from automation._contract import _evidence, _one_of, _unit_interval


@dataclass(frozen=True)
class Assessment:
    LEVELS = ("manual", "assisted", "developable", "ready")
    RISKS = ("read_only", "local_artifact_only", "remote_write", "code_diff", "commit_push")

    automation_level: str
    risk: str
    confidence: float
    evidence: tuple[str, ...]
    recipe: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assessment":
        if not isinstance(data, dict):
            raise ValueError("assessment must be an object")
        allowed = {"automation_level", "risk", "confidence", "evidence", "recipe"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"assessment contains unknown field: {sorted(unknown)[0]}")
        automation_level = _one_of(data, "automation_level", cls.LEVELS)
        recipe = data.get("recipe")
        if recipe is not None and (not isinstance(recipe, str) or not recipe.strip()):
            raise ValueError("recipe must be a non-empty string when present")
        if automation_level == "ready" and not (isinstance(recipe, str) and recipe.strip()):
            raise ValueError("a ready assessment requires a recipe reference")
        return cls(
            automation_level=automation_level,
            risk=_one_of(data, "risk", cls.RISKS),
            confidence=_unit_interval(data, "confidence"),
            evidence=_evidence(data),
            recipe=recipe,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "automation_level": self.automation_level,
            "risk": self.risk,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }
        if self.recipe is not None:
            payload["recipe"] = self.recipe
        return payload
