#!/usr/bin/env python3
"""Script-only cron entrypoint for the Core pipeline (no agent, no reasoning).

Hermes can run a plain script on a schedule without starting an agent turn.
This file is that script: a thin process entrypoint that calls the Core
pipeline with explicit arguments. It carries no chat state, starts no agent,
and asks Hermes for no judgement -- a scheduled run must be reproducible from
its arguments alone.

Output policy matches how Hermes cron reports: a scheduled job that succeeds
prints nothing, so a quiet run raises no notification. Anything that needs a
human -- a failure, or a task that stopped at a safety boundary -- exits
nonzero and says why on stderr, without a traceback or an internal path.

Copy this file to ~/.hermes/scripts/ (or the equivalent on the host) to
register it. The repository keeps the same layout so the copy is verbatim.

Exit codes:
  0  every task reached a normal state
  1  a task stopped at a boundary (blocked) or failed
  2  the invocation itself was wrong
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from run_pipeline import run_pipeline  # noqa: E402

_NEEDS_ATTENTION = frozenset({"blocked", "failed"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Core pipeline for one input on a schedule.",
        add_help=True,
    )
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="report only through the exit code",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0

    if not args.input_path.is_file():
        print("run-pipeline: input path not found", file=sys.stderr)
        return 2

    try:
        result = run_pipeline(args.input_path, output_root=args.output_root)
    except Exception as exc:
        # Never surface a traceback or an internal path into a notification.
        print(f"run-pipeline: pipeline failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    status = str(result.get("status", "unknown"))
    if status in _NEEDS_ATTENTION:
        if not args.quiet:
            task_id = result.get("task_id") or "unknown"
            print(f"run-pipeline: task {task_id} is {status}", file=sys.stderr)
        return 1

    # Success is silent: Hermes notifies on output, and a healthy scheduled run
    # should not page anyone.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
