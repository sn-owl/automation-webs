"""S6 raw fixture replay (docs/ARCHITECTURE.md §13 "S6 — 패턴 학습").

    관찰 -> 후보 패턴 -> 과거 raw fixture replay -> 사람 검토 -> 활성 Rule/Recipe/Skill

Before a candidate rule reaches a human, it is replayed against a corpus of
past ``WorkItem``s and compared to the currently-active baseline rules
(``config/rules.json`` by default, same as ``rules.classify_with_rules``'s
default). Disagreement between candidate and baseline is the signal Task
25's promotion gate blocks on -- this module only reports it. It never
promotes anything, never touches pattern state, and calls no LLM
(deterministic stage, docs/ARCHITECTURE.md §4 핵심 불변조건).

``replay_candidate`` is the core function: given a candidate ruleset and an
explicit corpus of ``WorkItem``s, it returns a plain, JSON-serialisable
report dict -- Task 25 consumes that dict directly. ``load_fixture_corpus``
is a thin, read-only loader that turns the shipped
``fixtures/sanitized/*.html`` into such a corpus via
``adapters.registry.normalize_html``, so the shipped fixtures get real
replay coverage too.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from automation.adapters.registry import normalize_html
from automation.classification import Classification
from automation.models import WorkItem
from automation.rules import classify_with_rules, load_rules

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "sanitized"

# fixtures/sanitized/manifest.json carries file, source_type and scenario,
# but not the board_id / source URL the gnuboard and egov adapters
# require -- same fixture set and values tests/test_adapter_registry.py
# already uses for these three sanitized files.
_FIXTURE_SOURCES: dict[str, dict[str, str]] = {
    "alpha-ready.html": {
        "board_id": "alpha",
        "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
    },
    "beta-manual.html": {
        "board_id": "beta",
        "url": "https://fixture.local/bbs/board.php?bo_table=beta&wr_id=2048",
    },
    "egov-developable.html": {
        "url": "https://fixture.local/egov/board/view.sko?nttId=9001",
    },
}


def load_fixture_corpus(fixture_dir: Path | str = FIXTURE_DIR) -> list[WorkItem]:
    """Build the replay corpus from ``fixtures/sanitized/`` via ``normalize_html``.

    Read-only: the sanitized HTML and its manifest are never written to.
    Iteration follows ``manifest.json``'s fixture list, which is fixed on
    disk, so the corpus order -- and therefore every downstream report -- is
    stable.
    """
    fixture_dir = Path(fixture_dir)
    manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    corpus = []
    for entry in manifest["fixtures"]:
        filename = entry["file"]
        html = (fixture_dir / filename).read_text(encoding="utf-8")
        source = _FIXTURE_SOURCES[filename]
        corpus.append(normalize_html(entry["source_type"], html, source))
    return corpus


def replay_candidate(
    pattern_id: str,
    candidate_rules: list[dict],
    corpus: Sequence[WorkItem],
    *,
    baseline_rules: list[dict] | None = None,
) -> dict[str, Any]:
    """Replay ``candidate_rules`` over ``corpus`` and report vs. the baseline.

    - match: candidate and baseline agree on
      ``(responsibility, task_type, size)``, or the baseline abstains while
      the candidate confidently classifies (a coverage gain).
    - abstain: the candidate returns ``None`` for the item.
    - disagree: both classify the item, but on different verdicts --
      recorded in ``disagreements`` for a human to adjudicate.

    ``baseline_rules`` defaults to ``config/rules.json``, the same default
    ``classify_with_rules`` itself uses.

    Duplicate corpus entries for the same source item (identical
    ``WorkItem.task_id``) must not each count as independent evidence, so
    they are deduped -- first occurrence wins -- before any counting. Every
    field but ``total`` is computed over the deduped set; ``total`` keeps
    the raw corpus size so the would-be inflation stays visible.
    """
    load_rules(rules=candidate_rules)  # closed vocabulary, fail fast

    distinct_items: dict[str, WorkItem] = {}
    for item in corpus:
        distinct_items.setdefault(item.task_id, item)

    match = abstain = disagree = 0
    disagreements: list[dict[str, Any]] = []

    for item in distinct_items.values():
        candidate = classify_with_rules(item, rules=candidate_rules)
        if candidate is None:
            abstain += 1
            continue

        baseline = classify_with_rules(item, rules=baseline_rules)
        if baseline is None or _verdict(candidate) == _verdict(baseline):
            match += 1
            continue

        disagree += 1
        disagreements.append(
            {
                "task_id": item.task_id,
                "candidate": candidate.to_dict(),
                "baseline": baseline.to_dict(),
            }
        )

    return {
        "pattern_id": pattern_id,
        "total": len(corpus),
        "distinct": len(distinct_items),
        "match": match,
        "abstain": abstain,
        "disagree": disagree,
        "disagreements": disagreements,
    }


def _verdict(classification: Classification) -> tuple[str, str, str]:
    return (classification.responsibility, classification.task_type, classification.size)
