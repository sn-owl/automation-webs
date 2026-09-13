"""Validate one injected Hermes proposal without executing model output.

The adapter boundary is deliberately injected by the caller.  Running this
module as a command without an adapter can only produce a safe rejection.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# The Skill is bundled below the repository root.  Resolve the Core package
# from this file, rather than trusting a model-controlled working directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.hermes_validator import (
    HermesValidationError,
    validate_hermes_assessment,
    validate_hermes_classification,
)


RawResponse = str | dict[str, Any]
Adapter = Callable[[dict[str, Any], Sequence[str]], RawResponse | None]


class ProposalRejected(ValueError):
    """A rejection with a fixed, non-sensitive reason category."""

    _REASONS = frozenset(
        {
            "abstained",
            "adapter_unavailable",
            "invalid_allow_list",
            "invalid_work_item",
            "proposal_rejected",
        }
    )

    def __init__(self, reason: str) -> None:
        if reason not in self._REASONS:
            reason = "proposal_rejected"
        self.reason = reason
        super().__init__(reason)


def _read_work_item(path: str | Path) -> dict[str, Any]:
    payload: Any = None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
        pass
    if not isinstance(payload, dict):
        raise ProposalRejected("invalid_work_item") from None
    return payload


def _parse_allow_list(value: str) -> list[str]:
    parsed: Any = None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError, RecursionError):
        pass
    if (
        not isinstance(parsed, list)
        or any(not isinstance(recipe_id, str) or not recipe_id.strip() for recipe_id in parsed)
        or len(set(parsed)) != len(parsed)
    ):
        raise ProposalRejected("invalid_allow_list") from None
    return parsed

def _snapshot_allow_list(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    try:
        ids = tuple(value)
        if (
            any(not isinstance(recipe_id, str) or not recipe_id.strip() for recipe_id in ids)
            or len(set(ids)) != len(ids)
        ):
            return None
    except Exception:
        return None
    return ids


def _adapter_response(
    adapter: Adapter | Any,
    operation: str,
    work_item: dict[str, Any],
    allowed_recipe_ids: Sequence[str],
) -> RawResponse | None:
    if adapter is None:
        raise ProposalRejected("adapter_unavailable")
    method = getattr(adapter, operation, None)
    if callable(method):
        if operation == "classification":
            return method(work_item)
        return method(work_item, allowed_recipe_ids)
    raise ProposalRejected("adapter_unavailable")


def _proposal_dict(validated: Any, operation: str) -> dict[str, Any]:
    if operation == "classification":
        return {"classification": validated.to_dict()}
    return {"assessment": validated.to_dict()}


def validate_proposal(
    work_item_path: str | Path,
    allowed_recipe_ids: Sequence[str],
    *,
    adapter: Adapter | Any,
    operation: str = "assessment",
    recipes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return one operation-specific, Core-validated advisory proposal."""
    if operation not in {"classification", "assessment"}:
        raise ProposalRejected("proposal_rejected") from None
    allow_list_snapshot = _snapshot_allow_list(allowed_recipe_ids)
    if allow_list_snapshot is None:
        raise ProposalRejected("invalid_allow_list") from None
    allowed_recipe_ids = allow_list_snapshot
    work_item = _read_work_item(work_item_path)
    try:
        raw_response = _adapter_response(
            adapter, operation, work_item, allowed_recipe_ids
        )
    except ProposalRejected:
        raise
    except Exception:
        raise ProposalRejected("proposal_rejected") from None
    if raw_response is None:
        raise ProposalRejected("abstained") from None
    try:
        if operation == "classification":
            validated = validate_hermes_classification(raw_response)
        else:
            validated = validate_hermes_assessment(
                raw_response,
                allowed_recipe_ids,
                recipes=recipes,
            )
    except (HermesValidationError, TypeError, ValueError, RecursionError):
        raise ProposalRejected("proposal_rejected") from None
    return _proposal_dict(validated, operation)


def summarize_proposal(proposal: dict[str, Any]) -> str:
    """Create a safe deterministic summary from validated fields only."""
    if "assessment" in proposal:
        assessment = proposal["assessment"]
        return (
            "Hermes proposed "
            f"{assessment['automation_level']} handling at {assessment['risk']} risk; "
            "Core review is required before any state change."
        )
    return "Hermes proposed a classification; Core review is required."


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _safe_rejection(reason: str) -> dict[str, Any]:
    if reason not in ProposalRejected._REASONS:
        reason = "proposal_rejected"
    return {"rejected": True, "reason": reason}


class _SafeArgumentParser(argparse.ArgumentParser):
    """Reject malformed invocation without echoing attacker-controlled argv."""

    def error(self, _message: str) -> None:
        raise ProposalRejected("invalid_work_item")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Validate an injected Hermes maintenance-triage proposal."
    )
    parser.add_argument(
        "work_item_path",
        nargs="?",
        type=Path,
        help="path to one sanitized WorkItem JSON",
    )
    parser.add_argument(
        "allowed_recipes",
        nargs="?",
        help="JSON array of allowed recipe IDs, for example ['recipe-id']",
    )
    parser.add_argument(
        "--work-item",
        dest="work_item_option",
        type=Path,
        help="path to one sanitized WorkItem JSON (named form)",
    )
    parser.add_argument(
        "--allowed-recipes",
        dest="allowed_recipes_option",
        help="JSON array of allowed recipe IDs (named form)",
    )
    parser.add_argument(
        "--operation",
        choices=("classification", "assessment"),
        default="assessment",
        help="advisory operation to validate",
    )
    return parser


def _cli_inputs(args: argparse.Namespace) -> tuple[Path, str]:
    if args.work_item_option is not None and args.work_item_path is not None:
        raise ProposalRejected("invalid_work_item") from None
    if args.allowed_recipes_option is not None and args.allowed_recipes is not None:
        raise ProposalRejected("invalid_allow_list") from None
    work_item_path = (
        args.work_item_option if args.work_item_option is not None else args.work_item_path
    )
    allowed_recipes = (
        args.allowed_recipes_option
        if args.allowed_recipes_option is not None
        else args.allowed_recipes
    )
    if work_item_path is None:
        raise ProposalRejected("invalid_work_item") from None
    if allowed_recipes is None:
        raise ProposalRejected("invalid_allow_list") from None
    return work_item_path, allowed_recipes


def main(
    argv: list[str] | None = None,
    *,
    adapter: Adapter | Any = None,
    recipes: dict[str, dict[str, Any]] | None = None,
) -> int:
    """CLI entrypoint; stdout is proposal JSON or a fixed safe rejection."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        work_item_path, allowed_recipes = _cli_inputs(args)
        allowed_recipe_ids = _parse_allow_list(allowed_recipes)
        proposal = validate_proposal(
            work_item_path,
            allowed_recipe_ids,
            adapter=adapter,
            operation=args.operation,
            recipes=recipes,
        )
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        print(_json_text(_safe_rejection("invalid_work_item")))
        return 1
    except ProposalRejected as exc:
        print(_json_text(_safe_rejection(exc.reason)))
        return 1
    except Exception:
        # Do not expose parser, filesystem, adapter, validator, or model text.
        print(_json_text(_safe_rejection("proposal_rejected")))
        return 1

    print(_json_text(proposal))
    print(summarize_proposal(proposal), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
