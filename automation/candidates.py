"""Scoped Core pattern candidate and decision service."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automation.approval import validate_pattern_approval
from automation.classification import Classification
from automation.decisions import Decision, PatternDecision
from automation.events import DuplicateEventError, EventLog
from automation.locking import file_lock
from automation.models import WorkItem
from automation.pattern_store import (
    active_definitions,
    create_pattern,
    get_pattern,
    revise_pattern,
)
from automation.patterns import Pattern, definition_digest, validate_pattern_id, validate_scope
from automation.promotion import evaluate_promotion
from automation.rules import load_rules
from automation.replay import replay_candidate
from automation.task_store import get_task, list_tasks

CONDITION = "title_or_body_contains_any"


class CandidateError(RuntimeError):
    """Raised when a scoped candidate or promotion operation is refused."""


class InsufficientConfirmationsError(CandidateError):
    """Raised before a scope has three qualifying current confirmations."""


def _event_log(root: Path | str) -> EventLog:
    return EventLog(Path(root) / "state" / "events.jsonl")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_rule(rule_id: str, keywords: list[str], correction: Classification) -> dict[str, Any]:
    if not isinstance(keywords, list) or not keywords or not all(isinstance(k, str) and k.strip() for k in keywords):
        raise CandidateError("at least one non-empty keyword is required")
    rule = {
        "id": validate_pattern_id(rule_id),
        "when": {CONDITION: list(dict.fromkeys(k.strip() for k in keywords))},
        "then": {
            "responsibility": correction.responsibility,
            "task_type": correction.task_type,
            "size": correction.size,
            "confidence": correction.confidence,
        },
    }
    try:
        load_rules(rules=[rule])
    except ValueError as exc:
        raise CandidateError(f"correction does not form a loadable rule: {exc}") from exc
    return rule

def _task_current(task_id: str, *, scope: str, root: Path | str) -> WorkItem:
    try:
        task = get_task(task_id, root=root)
    except Exception as exc:
        raise CandidateError(f"task not found: {task_id}") from exc
    if task.scope != scope:
        raise CandidateError("task is outside the requested scope")
    return task


def _corrections(scope: str, keywords: list[str], *, root: Path | str) -> list[tuple[WorkItem, Classification, str]]:
    scope = validate_scope(scope)
    normalized = tuple(dict.fromkeys(k.strip() for k in keywords))
    if not normalized or any(not k for k in normalized):
        raise CandidateError("at least one non-empty keyword is required")
    found: dict[str, tuple[WorkItem, Classification, str]] = {}
    for event in _event_log(root).read():
        task_id = event.get("task_id")
        if not isinstance(task_id, str):
            continue
        try:
            task = _task_current(task_id, scope=scope, root=root)
        except CandidateError:
            continue
        haystack = f"{task.title}\n{task.body}"
        if not any(keyword in haystack for keyword in normalized):
            continue
        correction: Classification | None = None
        source = ""
        if event.get("type") == "decision":
            raw = event.get("decision")
            if not isinstance(raw, dict):
                continue
            try:
                decision = Decision.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if decision.task_id != task_id or decision.task_version != task.revision:
                continue
            correction = decision.correction()
            source = f"decision by {decision.actor}: {decision.reason}"
        elif event.get("type") == "feedback":
            feedback = event.get("feedback")
            if not isinstance(feedback, dict) or feedback.get("type") != "classification_correction":
                continue
            if feedback.get("scope") != scope or feedback.get("task_id") != task_id or feedback.get("task_revision") != task.revision:
                continue
            if feedback.get("revision_digest") != (task.revision_digest or task.computed_revision_digest()):
                continue
            details = feedback.get("details")
            if not isinstance(details, dict) or not isinstance(details.get("classification"), dict):
                continue
            try:
                correction = Classification.from_dict(details["classification"])
            except (TypeError, ValueError):
                continue
            source = f"feedback by {feedback.get('actor')}: {feedback.get('reason')}"
        if correction is not None:
            found[task.task_id] = (task, correction, source)
    return list(found.values())




def propose(scope: str, keywords: list[str], *, root: Path | str) -> Pattern:
    """Create one deterministic candidate after three distinct current confirmations."""
    scope = validate_scope(scope)
    confirmations = _corrections(scope, keywords, root=root)
    groups: dict[str, list[tuple[WorkItem, Classification, str]]] = {}
    for item in confirmations:
        groups.setdefault(json.dumps(item[1].to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")), []).append(item)
    eligible = max(groups.values(), key=len, default=[])
    if len(eligible) < 3:
        raise InsufficientConfirmationsError("insufficient confirmations: at least three distinct current task revisions are required")
    correction = eligible[0][1]
    keyword_values = list(dict.fromkeys(k.strip() for k in keywords))
    seed = {"scope": scope, "condition": CONDITION, "keywords": keyword_values, "classification": correction.to_dict()}
    pattern_id = "correction-" + hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    rule = build_rule(pattern_id, keyword_values, correction)
    evidence = [f"{item.task_id}@revision-{item.revision}: {source}" for item, _, source in sorted(eligible, key=lambda value: value[0].task_id)]
    evidence.append(f"matches {CONDITION}={keyword_values}")
    return create_pattern(Pattern(scope=scope, pattern_id=pattern_id, kind="rule", state="candidate", confidence=correction.confidence, evidence=tuple(evidence), definition=rule), root=root)


def corpus(scope: str, root: Path | str) -> list[WorkItem]:
    scope = validate_scope(scope)
    return [item for item in list_tasks(root=root) if item.scope == scope]


def get(scope: str, pattern_id: str, *, root: Path | str) -> Pattern:
    try:
        return get_pattern(scope, pattern_id, root=root)[0]
    except Exception as exc:
        raise CandidateError(f"pattern not found: {scope}/{pattern_id}") from exc


def review(scope: str, pattern_id: str, *, root: Path | str) -> dict[str, Any]:
    pattern = get(scope, pattern_id, root=root)
    if pattern.definition is None:
        raise CandidateError(f"pattern {pattern_id} has no definition to replay")
    if pattern.state == "candidate":
        pattern = revise_pattern(scope, pattern_id, "shadow", root=root)
    rule = json.loads(json.dumps(pattern.to_dict()["definition"]))
    # Replay against what actually classifies this scope today: the repository
    # baseline plus the scope's own active patterns (R02 -- promotion writes
    # the scoped store, never config/rules.json).
    baseline = load_rules() + active_definitions(scope, root=root)
    report = replay_candidate(pattern_id, [rule], corpus(scope, root), baseline_rules=baseline)
    recommendation = evaluate_promotion(pattern, report)
    return {"pattern": pattern.to_dict(), "revision": get_pattern(scope, pattern_id, root=root)[1], "report": report, "recommendation": recommendation}


def record_pattern_decision(scope: str, pattern_id: str, action: str, *, actor: str, reason: str, root: Path | str) -> dict[str, Any]:
    scope = validate_scope(scope)
    validate_pattern_id(pattern_id)
    with file_lock(Path(root) / "state" / "patterns" / scope / pattern_id / ".decision.lock"):
        pattern, revision, _ = get_pattern(scope, pattern_id, root=root)
        decision = PatternDecision(scope=scope, pattern_id=pattern_id, pattern_revision=revision, definition_digest=definition_digest(pattern.to_dict().get("definition")), action=action, actor=actor, reason=reason, timestamp=_timestamp())
        payload = decision.to_dict()
        identity = dict(payload)
        identity.pop("timestamp")
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        event = {"event_id": f"pattern-decision-{digest}", "type": "pattern_decision", "scope": scope, "pattern_id": pattern_id, "decision": payload}
        try:
            _event_log(root).append(event)
        except DuplicateEventError:
            pass
    if action != "approve" and pattern.state == "active":
        revise_pattern(scope, pattern_id, "suspended", root=root)
    return event


def latest_pattern_decision(scope: str, pattern_id: str, *, root: Path | str) -> PatternDecision | None:
    scope = validate_scope(scope)
    validate_pattern_id(pattern_id)
    found: PatternDecision | None = None
    for event in _event_log(root).read():
        if event.get("type") != "pattern_decision" or event.get("scope") != scope or event.get("pattern_id") != pattern_id:
            continue
        try:
            found = PatternDecision.from_dict(event["decision"])
        except (TypeError, ValueError, KeyError) as exc:
            raise CandidateError("corrupt pattern decision evidence") from exc
        if found.scope != scope or found.pattern_id != pattern_id:
            raise CandidateError("pattern decision binding mismatch")
    return found


def promote(scope: str, pattern_id: str, *, root: Path | str) -> Pattern:
    outcome = review(scope, pattern_id, root=root)
    if not outcome["recommendation"]["recommend"]:
        raise CandidateError("promotion refused: " + "; ".join(outcome["recommendation"]["reasons"]))
    pattern, revision, _ = get_pattern(scope, pattern_id, root=root)
    decision = latest_pattern_decision(scope, pattern_id, root=root)
    expected_digest = definition_digest(pattern.to_dict().get("definition"))
    try:
        validate_pattern_approval(decision, scope=scope, pattern_id=pattern_id, pattern_revision=revision, definition_digest=expected_digest)
    except ValueError as exc:
        raise CandidateError(f"promotion refused: {exc}") from exc
    event_id = next(event["event_id"] for event in reversed(_event_log(root).read()) if event.get("type") == "pattern_decision" and event.get("decision") == decision.to_dict())
    return revise_pattern(scope, pattern_id, "active", root=root, approval_event_id=event_id)
