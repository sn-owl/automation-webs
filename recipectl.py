"""Define, test, review, activate, and match user-authored recipes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from automation.recipe_onboarding import (
    RecipeOnboardingError,
    activate_draft,
    cli_payload,
    create_draft,
    get_draft,
    list_drafts,
    review_draft,
    select_active_recipe,
    select_recipe,
    test_draft,
    update_draft,
)
from automation.models import WorkItem


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path("runtime"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="save an inactive user-authored recipe draft")
    create.add_argument("--recipe-id", required=True)
    create.add_argument("--task-name", required=True)
    create.add_argument("--case-reference", required=True)
    create.add_argument("--description", required=True)
    create.add_argument("--executor", required=True)
    create.add_argument("--input-type", required=True)
    create.add_argument("--output-type", required=True)
    create.add_argument("--template", default=None)
    create.add_argument("--steps", required=True, help="JSON list of ordered supported steps")
    create.add_argument("--applicability", required=True, help="JSON object of evidence conditions")
    create.add_argument("--required-condition", action="append", required=True)
    create.add_argument("--exclusion-condition", action="append", required=True)
    create.add_argument("--success-check", action="append", required=True)
    create.add_argument("--approval-scope", default="local_artifact_only")
    _root(create)

    update = commands.add_parser("update", help="replace an inactive draft and reset its test status")
    update.add_argument("recipe_id")
    update.add_argument("--task-name", required=True)
    update.add_argument("--case-reference", required=True)
    update.add_argument("--description", required=True)
    update.add_argument("--executor", required=True)
    update.add_argument("--input-type", required=True)
    update.add_argument("--output-type", required=True)
    update.add_argument("--template", default=None)
    update.add_argument("--steps", required=True, help="JSON list of ordered supported steps")
    update.add_argument("--applicability", required=True, help="JSON object of evidence conditions")
    update.add_argument("--required-condition", action="append", required=True)
    update.add_argument("--exclusion-condition", action="append", required=True)
    update.add_argument("--success-check", action="append", required=True)
    update.add_argument("--approval-scope", default="local_artifact_only")
    _root(update)
    for definition_parser in (create, update):
        definition_parser.add_argument("--scope", required=True)
        for field in ("execution", "verification", "recognition", "error-policy", "ai-policy"):
            definition_parser.add_argument(f"--{field}", default=None, help="JSON declaration")
    show = commands.add_parser("show", help="show a draft or active definition")
    show.add_argument("recipe_id")
    show.add_argument("--scope", required=True)
    _root(show)

    list_parser = commands.add_parser("list", help="list scoped recipe drafts")
    list_parser.add_argument("--scope", default=None)
    _root(list_parser)

    test = commands.add_parser("test", help="test a draft against a real WorkItem in isolation")
    test.add_argument("recipe_id")
    test.add_argument("--scope", required=True)
    task_input = test.add_mutually_exclusive_group(required=True)
    task_input.add_argument("--task-file", type=Path)
    task_input.add_argument("--task-id", help="existing Core task identifier")
    _root(test)
    test.add_argument(
        "--retry",
        action="store_true",
        help="explicitly rerun a previously failed test; passed tests remain idempotent",
    )

    review = commands.add_parser("review", help="record human review of a passed draft")
    review.add_argument("recipe_id")
    review.add_argument("--scope", required=True)
    review.add_argument("--actor", required=True)
    review.add_argument("--reason", required=True)
    _root(review)

    activate = commands.add_parser("activate", help="register a reviewed draft")
    activate.add_argument("recipe_id")
    activate.add_argument("--scope", required=True)
    activate.add_argument("--actor", required=True)
    activate.add_argument("--reason", required=True)
    _root(activate)

    match = commands.add_parser("match", help="select an active recipe using conditions and preflight evidence")
    match.add_argument("--task-file", required=True, type=Path)
    match.add_argument("--recipe", default=None)
    _root(match)
    return parser


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "create":
            _print(create_draft(cli_payload(args), root=args.root))
        elif args.command == "update":
            changes = cli_payload(args)
            changes["recipe_id"] = args.recipe_id
            _print(
                update_draft(
                    args.recipe_id,
                    changes,
                    scope=args.scope,
                    root=args.root,
                )
            )
        elif args.command == "show":
            _print(get_draft(args.recipe_id, scope=args.scope, root=args.root))
        elif args.command == "list":
            _print(list_drafts(scope=args.scope, root=args.root))
        elif args.command == "test":
            result = test_draft(
                args.recipe_id,
                args.task_file,
                task_id=args.task_id,
                retry=args.retry,
                scope=args.scope,
                root=args.root,
            )
            _print(result)
            return 0 if result.get("status") == "passed" else 1
        elif args.command == "review":
            _print(
                review_draft(
                    args.recipe_id,
                    scope=args.scope,
                    actor=args.actor,
                    reason=args.reason,
                    root=args.root,
                )
            )
        elif args.command == "activate":
            _print(
                activate_draft(
                    args.recipe_id,
                    scope=args.scope,
                    actor=args.actor,
                    reason=args.reason,
                    root=args.root,
                )
            )
        elif args.command == "match":
            item = WorkItem.from_dict(json.loads(args.task_file.read_text(encoding="utf-8")))
            _print(
                select_recipe(args.recipe, item, root=args.root)
                if args.recipe
                else select_active_recipe(item, root=args.root)
            )
        return 0
    except (RecipeOnboardingError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"recipectl failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
