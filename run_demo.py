"""Offline submission demo: three scenarios, no network, deterministic output.

The demo runs the real Core pipeline against the sanitized fixtures and writes
one output directory per scenario plus a SHA-256 manifest. Running it twice
into two fresh directories produces byte-identical manifests, so a reviewer can
verify the result rather than trust a screenshot.

Two of the three fixtures do not match any rule, which is the designed
behaviour: the Rule Engine resolves the clear cases and everything ambiguous is
referred to Hermes. To keep the demo offline, those scenarios are driven by
*recorded* Hermes proposals rather than a live service. A recorded proposal is
still only a proposal -- it is passed through the same
``automation.hermes_validator`` boundary as a live response, so the demo shows
Core validation doing real work instead of bypassing it.

Nothing here approves or executes anything. A ``ready`` verdict ends at
``review_required`` with a rendered work package awaiting a human decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from run_pipeline import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "sanitized"

# Recorded Hermes responses. These are fixtures, not live output: they exist so
# the demo can show the manual and developable paths without a network call.
# Each one is validated by Core before the pipeline uses it.
RECORDED_PROPOSALS: dict[str, str] = {
    "manual": json.dumps(
        {
            "classification": {
                "responsibility": "content",
                "task_type": "content_edit",
                "size": "simple",
                "ownership": "정보 부족",
                "primary_role": "content",
                "responsibility_scope": "콘텐츠 변경 요청 검토",
                "collaboration": [],
                "risk_flags": ["원격 반영 별도 승인"],
                "confidence": 0.72,
                "evidence": ["본문이 기존 페이지 문구 수정을 요청함"],
                "next_action": "대상 페이지와 담당자를 확인하세요.",
                "pattern_match": {},
                "unmatched_aspects": ["대상 페이지"],
            }
        },
        ensure_ascii=False,
    ),
    "developable": json.dumps(
        {
            "classification": {
                "responsibility": "development",
                "task_type": "feature_dev",
                "size": "small_dev",
                "ownership": "정보 부족",
                "primary_role": "development",
                "responsibility_scope": "신규 기능 요청 분석",
                "collaboration": [],
                "risk_flags": ["구현 범위 미확정"],
                "confidence": 0.68,
                "evidence": ["신규 기능 추가 요청으로 읽히는 본문"],
                "next_action": "담당 소유권과 구현 범위를 확인하세요.",
                "pattern_match": {},
                "unmatched_aspects": ["담당 소유권", "구현 범위"],
            }
        },
        ensure_ascii=False,
    ),
}

SCENARIOS: tuple[tuple[str, str, str | None], ...] = (
    ("ready", "yeonje-ready.html", None),
    ("manual", "bsbukgu-manual.html", "manual"),
    ("developable", "dongnae-developable.html", "developable"),
)


class _RecordedProposals:
    """Offline stand-in for the Hermes transport.

    It returns one recorded response and never opens a socket. The pipeline
    still validates whatever it returns, so this cannot be used to smuggle an
    unregistered recipe or a forbidden field past Core.
    """

    def __init__(self, raw_response: str):
        self._raw_response = raw_response

    def classify(self, _work_item: dict[str, Any]) -> str:
        return self._raw_response

    def assess(self, _work_item: dict[str, Any], _allowed_recipe_ids) -> str:
        raise AssertionError("ordinary demo intake must not request assessment")


def _relative(value: Any, root: Path) -> Any:
    """Rewrite paths under ``root`` to root-relative strings.

    Both sides are resolved first: the pipeline reports absolute paths even
    when it was handed a relative output root, and a manifest that changed
    depending on how the caller spelled the directory would not be evidence
    of anything.
    """
    if isinstance(value, str):
        try:
            candidate = Path(value)
        except (TypeError, ValueError):
            return value
        for base in (root, root.resolve()):
            for target in (candidate, candidate.resolve() if candidate.is_absolute() else candidate):
                try:
                    return target.relative_to(base).as_posix()
                except ValueError:
                    continue
        return value
    if isinstance(value, dict):
        return {key: _relative(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_relative(item, root) for item in value]
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_demo(*, output_root: Path | str) -> dict[str, Any]:
    """Run all three scenarios into ``output_root`` and return the summary."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)

    scenarios: list[dict[str, Any]] = []
    for name, fixture, recorded in SCENARIOS:
        scenario_root = root / name
        scenario_root.mkdir(parents=True, exist_ok=True)
        client = _RecordedProposals(RECORDED_PROPOSALS[recorded]) if recorded else None
        result = run_pipeline(
            FIXTURES / fixture, output_root=scenario_root, hermes_client=client
        )
        # Paths are rewritten relative to the scenario directory so two runs in
        # different temporary directories compare equal byte for byte.
        relative_result = _relative(result, scenario_root)
        _write_json(scenario_root / "result.json", relative_result)

        scenarios.append(
            {
                "name": name,
                "fixture": fixture,
                "proposal_source": "hermes (recorded)" if recorded else "rule engine",
                "task_id": relative_result.get("task_id"),
                "status": relative_result.get("status"),
                "state": relative_result.get("state"),
                "route": relative_result.get("route"),
                "classification": relative_result.get("classification"),
                "assessment": relative_result.get("assessment"),
                "work_package": relative_result.get("work_package"),
            }
        )

    files = {
        path.relative_to(root).as_posix(): _digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name != "manifest.json"
        and path.suffix != ".lock"
        and path.as_posix().replace("\\", "/").split("/")[-3:] != ["state", "notifications", "deliveries.jsonl"]
    }
    manifest = {"scenarios": scenarios, "files": files}
    _write_json(root / "manifest.json", manifest)
    return {"scenarios": scenarios}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline submission demo across three sanitized scenarios."
    )
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)

    root: Path = args.output_root
    if (root / "manifest.json").exists():
        # A demo run is evidence; never silently overwrite a previous one.
        print(
            f"demo refused: {root / 'manifest.json'} already exists; use a fresh directory",
            file=sys.stderr,
        )
        return 1

    try:
        summary = run_demo(output_root=root)
    except Exception as exc:
        print(f"demo failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    for scenario in summary["scenarios"]:
        level = (scenario["assessment"] or {}).get("automation_level", "-")
        print(
            f"{scenario['name']:12s} {scenario['status']:16s} "
            f"level={level:12s} source={scenario['proposal_source']}"
        )
    print(f"manifest: {root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
