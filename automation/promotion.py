"""S6 pattern promotion gate (docs/ARCHITECTURE.md §13, §5 "패턴은 즉시 Skill이 되지 않는다").

    관찰 -> 후보 패턴 -> 과거 raw fixture replay -> 사람 검토 -> 활성 Rule/Recipe/Skill

This module sits at the "사람 검토" step: it looks at a candidate ``Pattern``
and its ``replay.replay_candidate`` report and returns a plain
recommendation dict for a human reviewer. It NEVER writes ``active`` state
itself -- it does not import ``automation.pattern_store`` and never calls
``Pattern.transition``. The actual promotion write is the Decision
Service's job.

# TODO(Task 30): the Decision Service reads this recommendation, gets a
# human approval reference, and performs the real
# ``pattern.transition("active", approval=...)`` + pattern_store write.

Three conditions gate a recommendation, evaluated independently so a human
reviewer sees every reason a pattern was refused, not just the first one
that failed:

- **replay 부족 (insufficient replay)**: fewer than ``min_distinct`` distinct
  corpus items were replayed. Reads ``report["distinct"]``, never
  ``report["total"]`` -- duplicated source items must not inflate the
  evidence a promotion rests on (see ``replay.py``).
- **disagreement 존재**: the candidate disagreed with the active baseline on
  more than ``max_disagreements`` items.
- **승인 없음 (no approval)**: expressed structurally -- only a pattern in the
  ``shadow`` state (already through observation and replay, awaiting human
  review) is eligible for a promotion recommendation at all. This mirrors
  ``patterns.py``'s state machine, which already refuses ``candidate ->
  active`` outright.

Thresholds are policy, not code: they live in ``config/promotion.json``,
loaded the same way ``rules.load_rules``/``evaluator.load_evaluation`` load
their configs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from automation.patterns import Pattern

DEFAULT_PROMOTION_PATH = Path(__file__).parents[1] / "config" / "promotion.json"

_POLICY_KEYS = ("min_distinct", "max_disagreements")

# Only a pattern that has already been through observation and raw fixture
# replay under a human's eye is a promotion candidate at all.
_ELIGIBLE_STATE = "shadow"


def load_promotion_policy(
    path: Path | str = DEFAULT_PROMOTION_PATH, *, policy: dict | None = None
) -> dict:
    if policy is None:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("promotion policy must be an object")

    unknown = set(policy) - set(_POLICY_KEYS)
    if unknown:
        raise ValueError(f"unknown promotion policy key(s): {sorted(unknown)}")

    for key in _POLICY_KEYS:
        if key not in policy:
            raise ValueError(f"{key} is required")
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")

    return policy


def evaluate_promotion(
    pattern: Pattern, report: dict[str, Any], *, policy: dict | None = None
) -> dict[str, Any]:
    """Evaluate whether ``pattern`` should be recommended for promotion.

    ``report`` is a ``replay.replay_candidate`` result for this same
    pattern. Returns a plain, JSON-serialisable recommendation dict --
    ``{"pattern_id", "recommend", "reasons"}`` -- that always carries the
    reasons behind its verdict, so a human can audit the call without
    re-running the replay. This function never mutates ``pattern`` or
    writes anywhere; it only reports.
    """
    if pattern.pattern_id != report["pattern_id"]:
        raise ValueError(
            f"pattern_id mismatch: pattern is {pattern.pattern_id!r}, "
            f"report is for {report['pattern_id']!r}"
        )

    cfg = load_promotion_policy(policy=policy)

    reasons: list[str] = []
    eligible = pattern.state == _ELIGIBLE_STATE
    if eligible:
        reasons.append(f"eligible: pattern state is {_ELIGIBLE_STATE!r}")
    else:
        reasons.append(
            f"not eligible: pattern state is {pattern.state!r}, only "
            f"{_ELIGIBLE_STATE!r} patterns are eligible for a promotion recommendation"
        )

    distinct = report["distinct"]
    min_distinct = cfg["min_distinct"]
    sufficient_replay = distinct >= min_distinct
    if sufficient_replay:
        reasons.append(
            f"sufficient replay: distinct={distinct} >= min_distinct={min_distinct}"
        )
    else:
        reasons.append(
            f"insufficient replay: distinct={distinct} < min_distinct={min_distinct}"
        )

    disagree = report["disagree"]
    max_disagreements = cfg["max_disagreements"]
    no_blocking_disagreement = disagree <= max_disagreements
    if no_blocking_disagreement:
        reasons.append(
            f"no blocking disagreement: disagree={disagree} <= max_disagreements={max_disagreements}"
        )
    else:
        reasons.append(
            f"disagreement present: disagree={disagree} > max_disagreements={max_disagreements}"
        )

    recommend = eligible and sufficient_replay and no_blocking_disagreement

    return {
        "pattern_id": pattern.pattern_id,
        "recommend": recommend,
        "reasons": tuple(reasons),
    }
