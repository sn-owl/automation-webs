"""S4 deterministic rule classifier.

A rule matches a ``WorkItem`` only through the closed condition vocabulary in
``_CONDITIONS``; anything else in a rule's ``when`` block is a config error. The
classifier favours precision over coverage (docs/ARCHITECTURE.md §10): no match, or
matches that disagree on the taxonomy, yield ``None`` so the router hands the
item to a human instead of guessing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from automation.classification import Classification
from automation.models import WorkItem

DEFAULT_RULES_PATH = Path(__file__).parents[1] / "config" / "rules.json"

_CONDITIONS = (
    "title_contains_any",
    "body_contains_any",
    "title_or_body_contains_any",
    "attachment_type_in",
    "source_type_in",
)


def load_rules(path: Path | str = DEFAULT_RULES_PATH, *, rules: list[dict] | None = None) -> list[dict]:
    if rules is None:
        rules = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    # An empty ruleset is valid: every classification then abstains (safe
    # default). The bundled config being non-empty is asserted by the tests.

    for rule in rules:
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("each rule needs a non-empty id")

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
        Classification.from_rule(
            then,
            evidence=[_evidence_line(rule_id)],
            rule_ids=[rule_id],
        )

    return rules


def classify_with_rules(
    work_item: WorkItem, *, rules: list[dict] | None = None, root: Path | str | None = None
) -> Classification | None:
    """Classify one item with the repository baseline plus its scope's active patterns.

    R02: promotion never writes ``config/rules.json``. An approved pattern
    becomes an ``active`` revision in the scoped pattern store, so the read
    side has to merge it here -- otherwise promotion would be inert. Explicit
    ``rules`` (replay, tests) bypass both.
    """
    # ponytail: re-reads + revalidates config per call; wrap load_rules in
    # functools.lru_cache if a pipeline ever calls this per item in a hot loop.
    effective = load_rules(rules=rules)
    if rules is None and root is not None:
        from automation.pattern_store import active_definitions

        effective = effective + load_rules(rules=active_definitions(work_item.scope, root=root))
    matched = [rule for rule in effective if _matches(rule["when"], work_item)]
    if not matched:
        return None

    verdicts = {
        (r["then"]["responsibility"], r["then"]["task_type"], r["then"]["size"]) for r in matched
    }
    if len(verdicts) > 1:
        return None

    chosen = max(matched, key=lambda r: r["then"]["confidence"])
    evidence = [_evidence_line(r["id"]) for r in matched]
    return Classification.from_rule(
        chosen["then"],
        evidence=evidence,
        rule_ids=[rule["id"] for rule in matched],
    )


def _matches(when: dict[str, Any], item: WorkItem) -> bool:
    for key, needles in when.items():
        if key == "title_contains_any":
            ok = any(n in item.title for n in needles)
        elif key == "body_contains_any":
            ok = any(n in item.body for n in needles)
        elif key == "title_or_body_contains_any":
            ok = any(n in item.title or n in item.body for n in needles)
        elif key == "attachment_type_in":
            ok = any(a.type in needles for a in item.attachments)
        elif key == "source_type_in":
            ok = item.source.type in needles
        else:  # pragma: no cover - load_rules already rejected this
            raise ValueError(f"unknown condition {key}")
        if not ok:
            return False
    return True


def _evidence_line(rule_id: str) -> str:
    return f"rule:{rule_id} 조건과 일치"
