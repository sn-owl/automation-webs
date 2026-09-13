#!/usr/bin/env python3
"""Pipeline outcome metrics — the acceptance criterion for automation work.

Every number here is read from a runtime root that real work has passed
through, never from fixtures. That is the whole point: Milestone E shipped 601
lines with their own passing tests and moved none of these numbers, because
nothing called it. A unit test proves a module runs; only these numbers prove
the pipeline does more of the work than it did yesterday.

Run it before and after a change. The diff is the definition of done.

    python scripts/metrics.py --root runtime

Baseline recorded 2026-09-04, the same 10 real tasks replayed into a fresh root:

    before   rule_classified 0 · rule_assessed 0 · hermes_required 10 · needs_decision 10
    after    rule_classified 8 · rule_assessed 8 · hermes_required  0 · needs_decision  7

Read the before line as one sentence: ten real requests came in, the rule config
covered none of them, and every one cost a model call and a human decision.

``needs_decision`` is the queue the operator actually faces. It stops at 7
because seven of these are ``remote_write`` (팝업·콘텐츠 게재), which
``config/approval.json`` gates on purpose — that file is where to change it.
``human_decided`` and ``hermes_required`` are historical event counts, so they
only move for tasks ingested after a change; replay into a temporary root to
see what the current config would do to work already collected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.evaluator import evaluate_with_rules
from automation.events import read_events
from automation.rules import classify_with_rules
from automation.task_store import list_tasks

# Metric -> what has to change for it to move, so a plan can name a target.
GOALS = {
    "tasks": "수집된 실업무 (분모)",
    "rule_classified": "Hermes 없이 분류됨 — config/rules.json 이 커버하는 범위",
    "rule_assessed": "Hermes 없이 분류+평가 완료 — 여기까지 와야 자동 경로가 열림",
    "hermes_required": "모호해서 모델을 호출한 업무",
    "needs_decision": "지금 사람 결정을 기다리는 업무 — 승인 정책이 줄이는 대상",
    "human_decided": "사람 결정을 실제로 거친 업무 (과거 기록)",
    "executions_succeeded": "실제 산출물이 나온 업무",
    "verifications_passed": "산출물 내용 대조까지 통과한 업무",
    "handoffs_confirmed": "운영자가 검증하고 수동 인계를 확인한 업무",
}


def collect(root: Path | str) -> dict[str, int]:
    root = Path(root)
    tasks = list_tasks(root=root)
    events_path = root / "state" / "events.jsonl"
    events = read_events(events_path) if events_path.exists() else []

    decided = {e["task_id"] for e in events if e.get("type") == "decision" and "task_id" in e}
    hermes = {e["task_id"] for e in events if e.get("stage") == "hermes" and "task_id" in e}
    succeeded = {
        e["task_id"]
        for e in events
        if e.get("type") == "execution" and e.get("state") == "succeeded" and "task_id" in e
    }
    verified = {
        e["task_id"]
        for e in events
        if e.get("type") == "verification"
        and (e.get("verification") or {}).get("status") == "passed"
        and "task_id" in e
    }
    handoffs = {
        e["task_id"]
        for e in events
        if e.get("type") == "verification"
        and (e.get("verification") or {}).get("handoff", {}).get("status") == "confirmed"
        and "task_id" in e
    }

    classified = assessed = 0
    for task in tasks:
        classification = classify_with_rules(task, root=root)
        if classification is None:
            continue
        classified += 1
        if evaluate_with_rules(task, classification) is not None:
            assessed += 1

    # Last state wins. Decision events carry no "state", so recording one never
    # overwrites the pipeline state it was made against.
    states: dict[str, str] = {}
    for event in events:
        task_id, state = event.get("task_id"), event.get("state")
        if state == "verification_recorded":
            continue
        if isinstance(task_id, str) and isinstance(state, str):
            states[task_id] = state

    task_ids = {task.task_id for task in tasks}
    return {
        "tasks": len(tasks),
        "rule_classified": classified,
        "rule_assessed": assessed,
        "hermes_required": len(hermes & task_ids),
        "needs_decision": sum(
            1
            for task_id in task_ids
            if task_id not in decided and states.get(task_id) == "review_required"
        ),
        "human_decided": len(decided & task_ids),
        "executions_succeeded": len(succeeded & task_ids),
        "verifications_passed": len(verified & task_ids),
        "handoffs_confirmed": len(handoffs & task_ids),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("runtime"))
    parser.add_argument("--json", action="store_true", help="machine-readable output only")
    args = parser.parse_args(argv)

    metrics = collect(args.root)
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
        return 0

    total = metrics["tasks"] or 1
    print(f"runtime root: {args.root}")
    for name, value in metrics.items():
        share = "" if name == "tasks" else f"  ({value * 100 // total}%)"
        print(f"  {name:22} {value:4}{share:8}  {GOALS[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
