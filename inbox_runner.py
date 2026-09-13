"""Consume completed Chrome Extension bundles and run the Core pipeline."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from automation.hermes_client import HermesClient
from automation.inbox import accept_completed_download, accept_source_observation, ingest_source_observation
from run_pipeline import run_pipeline


_SOURCE_OBSERVATION_SUFFIX = "--source-observation"


def _is_source_observation(download_dir: Path) -> bool:
    return download_dir.name.endswith(_SOURCE_OBSERVATION_SUFFIX)


def _observation_result(*, status: str, event: dict[str, object] | None = None) -> dict[str, object]:
    if status == "blocked":
        return {
            "status": "blocked",
            "state": "source_observation_blocked",
            "error": "source observation requires configured scope and connector",
        }
    if status == "failed":
        return {
            "status": "failed",
            "state": "source_observation_rejected",
            "stage": "source",
            "reason": "source observation rejected by Core",
            "next_action": "inspect the source bundle and retry",
        }
    assert event is not None
    result: dict[str, object] = {
        "status": "source_observed",
        "kind": event["state"],
        "event_id": event["event_id"],
    }
    for key in ("source", "observed_at"):
        if event.get(key) is not None:
            result[key] = event[key]
    details = event.get("details")
    if isinstance(details, dict) and isinstance(details.get("error"), dict):
        error = details["error"]
        result["diagnosis"] = {
            key: error[key]
            for key in ("code", "stage", "retryable", "last_success_at", "next_action")
            if key in error
        }
    if event.get("task_id") is not None:
        result["task_id"] = event["task_id"]
    return result


def _bundle_failure(*, stage: str) -> dict[str, object]:
    return {
        "status": "failed",
        "stage": stage,
        "reason": "bundle processing failed",
        "next_action": "inspect the bundle and retry",
    }


def _bundle_dirs(download_root: Path) -> Iterable[Path]:
    try:
        entries = sorted(download_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return ()
    return (entry for entry in entries if entry.is_dir() and not entry.name.startswith("."))


def process_inbox(
    download_root: Path,
    inbox_root: Path,
    output_root: Path,
    *,
    hermes_client: Any = None,
    pipeline: Callable[..., dict[str, Any]] = run_pipeline,
    scope: str | None = None,
    connector_id: str | None = None,
) -> list[dict[str, Any]]:
    """Accept complete Downloads bundles, recording source observations directly."""
    results: list[dict[str, Any]] = []
    for download_dir in _bundle_dirs(Path(download_root)):
        if _is_source_observation(download_dir):
            if not scope or not connector_id:
                results.append(_observation_result(status="blocked"))
                continue
            try:
                accepted = accept_source_observation(download_dir, Path(inbox_root))
            except Exception:
                accepted = None
            if accepted is None:
                results.append(_observation_result(status="failed"))
                continue
            try:
                event = ingest_source_observation(
                    accepted,
                    root=Path(output_root),
                    scope=scope,
                    connector_id=connector_id,
                )
            except Exception:
                event = None
            results.append(
                _observation_result(status="success", event=event)
                if event is not None
                else _observation_result(status="failed")
            )
            continue
        try:
            accepted = accept_completed_download(download_dir, Path(inbox_root))
        except Exception:
            results.append(_bundle_failure(stage="acceptance"))
            continue
        if accepted is None:
            continue
        try:
            result = pipeline(
                accepted,
                output_root=Path(output_root),
                hermes_client=hermes_client,
                scope=scope,
                connector_id=connector_id,
            )
        except Exception:
            results.append(_bundle_failure(stage="pipeline"))
        else:
            results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, required=True, help="Chrome Downloads/upmuzadong-inbox directory")
    parser.add_argument("--inbox", type=Path, default=None, help="Core Inbox directory (defaults to output-root/inbox)")
    parser.add_argument("--output-root", type=Path, required=True, help="Core state/artifact root")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interval", type=float, default=0, help="Repeat every N seconds; zero processes once")
    parser.add_argument(
        "--scope",
        default=None,
        help="owner scope of this collection; requires --connector-id. "
        "Supplying both collects into contract_version 2 WorkItems with scoped task ids.",
    )
    parser.add_argument("--connector-id", default=None, dest="connector_id")
    args = parser.parse_args(argv)
    if args.interval < 0:
        parser.error("--interval must be non-negative")
    if (args.scope is None) != (args.connector_id is None):
        parser.error("--scope and --connector-id must be given together")

    repository_root = args.repository_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    inbox_root = (args.inbox or output_root / "inbox").expanduser().resolve()
    client = HermesClient(repository_root=repository_root)
    while True:
        results = process_inbox(
            args.downloads,
            inbox_root,
            output_root,
            hermes_client=client,
            scope=args.scope,
            connector_id=args.connector_id,
        )
        for result in results:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        if args.interval == 0:
            return 0 if all(result.get("status") != "failed" for result in results) else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
