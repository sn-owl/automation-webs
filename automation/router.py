"""S6 routing decision.

An explicit table from an ``Assessment`` to exactly one downstream route. Two
safety overrides sit in front of the table:

- confidence below ``confidence_floor`` → ``needs_hermes`` (ambiguous work goes
  for more analysis, never straight to execution)
- ``source="hermes"`` with an execution-bearing level (``ready`` /
  ``developable``) → ``needs_hermes``. Hermes owns no execution or
  code-application authority (Global Constraints); its verdict can only
  suggest, so a human confirms before anything runs or a draft is applied.
"""

from __future__ import annotations

from automation.assessment import Assessment

ROUTES = (
    "classification_only",
    "manual_handoff",
    "prepare_assistance",
    "development_draft",
    "execute_recipe",
    "needs_hermes",
)

_SOURCES = ("rules", "hermes")

_ROUTE_BY_LEVEL = {
    "manual": "manual_handoff",
    "assisted": "prepare_assistance",
    "developable": "development_draft",
    "ready": "execute_recipe",
}

# Levels whose route acts on the world (runs a recipe, applies a draft). A
# Hermes-sourced assessment must not reach these without a human in between.
_EXECUTION_LEVELS = ("ready", "developable")

def route(
    assessment: Assessment | None,
    *,
    source: str = "rules",
    confidence_floor: float = 0.7,
) -> str:
    if assessment is None:
        return "classification_only"
    if source not in _SOURCES:
        raise ValueError(f"source must be one of {_SOURCES}")
    if assessment.confidence < confidence_floor:
        return "needs_hermes"
    if source == "hermes" and assessment.automation_level in _EXECUTION_LEVELS:
        return "needs_hermes"
    return _ROUTE_BY_LEVEL[assessment.automation_level]
