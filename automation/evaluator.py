"""S5 deterministic evaluator.

``evaluate_with_rules(work_item, classification)`` turns a classification into an
``Assessment`` using the closed rule table in ``config/evaluation.json``. Like
the S4 classifier it favours precision: a classification below the confidence
floor, no matching rule, or rules that disagree on ``(automation_level, risk)``
all abstain (``None``) so the router escalates to a human.

``evaluation_order`` is read from config, not hardcoded — it sets the sequence
of the per-dimension evidence lines. docs/ARCHITECTURE.md §11 says a personalised
Profile may define this order; the Profile schema is Task 51 (deferred), so the
config file is the single source until then.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from automation.assessment import Assessment
from automation.classification import Classification
from automation.models import WorkItem

DEFAULT_EVALUATION_PATH = Path(__file__).parents[1] / "config" / "evaluation.json"

_STAGES = ("responsibility", "task_type", "automation_level", "risk")
_CONDITIONS = ("responsibility_in", "task_type_in", "size_in", "attachment_type_in")


def load_evaluation(
    path: Path | str = DEFAULT_EVALUATION_PATH, *, config: dict | None = None
) -> dict:
    if config is None:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("evaluation config must be an object")

    evaluation_order(config)  # validates the order stages

    floor = config.get("min_classification_confidence")
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0.0 <= floor <= 1.0:
        raise ValueError("min_classification_confidence must be a number between 0 and 1")

    rules = config.get("rules")
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    for rule in rules:
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("each evaluation rule needs a non-empty id")
        when = rule.get("when")
        if not isinstance(when, dict) or not when:
            raise ValueError(f"rule {rule_id}: when must be a non-empty object")
        unknown = set(when) - set(_CONDITIONS)
        if unknown:
            raise ValueError(f"rule {rule_id}: unknown condition {sorted(unknown)}")
        for key, value in when.items():
            if not isinstance(value, list) or not value or not all(
                isinstance(v, str) and v.strip() for v in value
            ):
                raise ValueError(f"rule {rule_id}: {key} must be a non-empty string list")
        then = rule.get("then")
        if not isinstance(then, dict):
            raise ValueError(f"rule {rule_id}: then must be an object")
        Assessment.from_dict({**then, "evidence": [f"eval:{rule_id}"]})

    return config


def evaluation_order(config: dict) -> list[str]:
    order = config.get("evaluation_order")
    if not isinstance(order, list) or not order:
        raise ValueError("evaluation_order must be a non-empty list")
    unknown = [stage for stage in order if stage not in _STAGES]
    if unknown:
        raise ValueError(f"evaluation_order has unknown stages: {unknown}")
    return order


def evaluate_with_rules(
    work_item: WorkItem, classification: Classification, *, config: dict | None = None
) -> Assessment | None:
    cfg = load_evaluation(config=config)
    if classification.confidence < cfg["min_classification_confidence"]:
        return None

    matched = [
        rule for rule in cfg["rules"] if _matches(rule["when"], work_item, classification)
    ]
    if not matched:
        return None

    verdicts = {(r["then"]["automation_level"], r["then"]["risk"]) for r in matched}
    if len(verdicts) > 1:
        return None

    chosen = max(matched, key=lambda r: r["then"]["confidence"])
    evidence = [f"eval:{chosen['id']} 조건과 일치"]
    evidence += _ordered_evidence(cfg["evaluation_order"], classification, chosen["then"])
    return Assessment.from_dict({**chosen["then"], "evidence": evidence})


def _matches(when: dict[str, Any], item: WorkItem, cls: Classification) -> bool:
    for key, values in when.items():
        if key == "responsibility_in":
            ok = cls.responsibility in values
        elif key == "task_type_in":
            ok = cls.task_type in values
        elif key == "size_in":
            ok = cls.size in values
        elif key == "attachment_type_in":
            ok = any(a.type in values for a in item.attachments)
        else:  # pragma: no cover - load_evaluation already rejected this
            raise ValueError(f"unknown condition {key}")
        if not ok:
            return False
    return True


def _ordered_evidence(order: list[str], cls: Classification, then: dict[str, Any]) -> list[str]:
    values = {
        "responsibility": cls.responsibility,
        "task_type": cls.task_type,
        "automation_level": then["automation_level"],
        "risk": then["risk"],
    }
    return [f"{stage}={values[stage]}" for stage in order]
