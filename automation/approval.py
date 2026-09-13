"""Approval policy — which assessed work still needs a human decision.

docs/ARCHITECTURE.md §15 says the final decision belongs to "사람 또는 명시적인 Core
정책". Only the first half existed: the pipeline ended every task at
``review_required`` regardless of what the task would actually do, so a request
that touches nothing cost the same review as one that uploads to a live admin
page.

The policy is keyed on ``risk`` alone, which is the contract §21 already fixed
in the profile's ``approval`` block::

    approval:
      local_artifact: required
      remote_write: required
      code_diff: required
      commit_push: required

``read_only`` is absent there, and that absence is the whole rule: work that
writes nothing anywhere needs no approval to be considered done by the system.
Keying on ``automation_level`` as well was considered and dropped — no evaluation
rule produces a (level, risk) pair the risk alone does not already settle, and a
wider key is a wider surface on a safety boundary.

Fail closed: an unknown risk requires approval.
"""

from __future__ import annotations

import json
from pathlib import Path

from automation.assessment import Assessment

DEFAULT_APPROVAL_PATH = Path(__file__).parents[1] / "config" / "approval.json"

DECISIONS = ("auto", "required")


def load_approval_policy(
    path: Path | str = DEFAULT_APPROVAL_PATH, *, policy: dict | None = None
) -> dict[str, str]:
    if policy is None:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("approval policy must be an object")

    unknown = set(policy) - set(Assessment.RISKS)
    if unknown:
        raise ValueError(f"approval policy has unknown risks: {sorted(unknown)}")
    # Every risk must be stated. A missing key would otherwise be answered by
    # the fail-closed default, turning a config typo into a silent extra gate
    # that no one can find.
    missing = set(Assessment.RISKS) - set(policy)
    if missing:
        raise ValueError(f"approval policy is missing risks: {sorted(missing)}")

    for risk, decision in policy.items():
        if decision not in DECISIONS:
            raise ValueError(f"risk {risk}: decision must be one of {DECISIONS}")

    return policy


def requires_approval(risk: str, *, policy: dict | None = None) -> bool:
    """Return whether ``risk`` needs a recorded human decision.

    Anything the policy does not answer with an explicit ``auto`` is gated.
    """
    try:
        table = load_approval_policy(policy=policy)
    except (OSError, ValueError):
        # A missing or malformed policy must not open the gate.
        return True
    return table.get(risk) != "auto"


def validate_pattern_approval(
    decision: "PatternDecision | None",
    *,
    scope: str,
    pattern_id: str,
    pattern_revision: int,
    definition_digest: str,
) -> None:
    """Require an approve decision bound to the exact current pattern."""
    if decision is None or decision.action != "approve":
        raise ValueError("current approve pattern decision is required")
    if (
        decision.scope != scope
        or decision.pattern_id != pattern_id
        or decision.pattern_revision != pattern_revision
        or decision.definition_digest != definition_digest
    ):
        raise ValueError("pattern approval binding does not match current revision")
