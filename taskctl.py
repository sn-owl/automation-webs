from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automation.decisions import Decision
from automation import candidates
from automation.events import DuplicateEventError, EventLog
from automation.pattern_store import list_patterns
from automation.task_store import get_task, list_tasks
from automation.feedback import FEEDBACK_TYPES, record_feedback
from automation.preparation_plan import build_preparation_plan, export_preparation_report
from automation.project_profile import (
    ProjectProfile,
    list_projects,
    load_project,
    register_project,
)




def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"details must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise argparse.ArgumentTypeError("details must be a non-empty JSON object")
    return parsed


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_root_argument(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_default else Path(".")
    parser.add_argument("--root", type=Path, default=default, help="state root directory")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and decide persisted tasks.")
    _add_root_argument(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="list stored tasks")
    _add_root_argument(list_parser, suppress_default=True)

    show_parser = commands.add_parser("show", help="show one stored task")
    show_parser.add_argument("task_id")
    _add_root_argument(show_parser, suppress_default=True)

    for action in Decision.ACTIONS:
        decision_parser = commands.add_parser(action, help=f"{action} one task")
        decision_parser.add_argument("task_id")
        decision_parser.add_argument("--actor", required=True)
        decision_parser.add_argument("--reason", required=True)
        decision_parser.add_argument(
            "--task-version",
            type=int,
            default=None,
            help="task revision; defaults to the current persisted revision",
        )
        decision_parser.add_argument("--timestamp", default=None)
        if action in ("modify", "reject"):
            decision_parser.add_argument(
                "--details",
                required=action == "modify",
                default=None,
                type=_json_object,
            )
        _add_root_argument(decision_parser, suppress_default=True)
        if action == "approve":
            decision_parser.add_argument(
                "--recipe",
                default=None,
                help="recipe to bind to this approval; required before recipe execution",
            )

    pattern_parser = commands.add_parser(
        "pattern", help="turn a recorded correction into an active rule"
    )
    _add_root_argument(pattern_parser, suppress_default=True)
    pattern_commands = pattern_parser.add_subparsers(dest="pattern_command", required=True)

    pattern_list = pattern_commands.add_parser("list", help="list stored patterns in a scope")
    pattern_list.add_argument("--scope", required=True)
    _add_root_argument(pattern_list, suppress_default=True)

    pattern_propose = pattern_commands.add_parser(
        "propose", help="create a candidate from three scoped corrections"
    )
    pattern_propose.add_argument("--scope", required=True)
    pattern_propose.add_argument(
        "--keyword",
        required=True,
        action="append",
        dest="keywords",
        help="title/body text the scoped pattern matches on; repeatable",
    )
    _add_root_argument(pattern_propose, suppress_default=True)

    pattern_review = pattern_commands.add_parser(
        "review", help="replay a scoped candidate over collected work and score it"
    )
    pattern_review.add_argument("pattern_id")
    pattern_review.add_argument("--scope", required=True)
    _add_root_argument(pattern_review, suppress_default=True)

    pattern_decide = pattern_commands.add_parser(
        "decide", help="record a Core decision for a scoped pattern"
    )
    pattern_decide.add_argument("pattern_id")
    pattern_decide.add_argument("action", choices=("approve", "reject", "modify", "defer"))
    pattern_decide.add_argument("--scope", required=True)
    pattern_decide.add_argument("--actor", required=True)
    pattern_decide.add_argument("--reason", required=True)
    _add_root_argument(pattern_decide, suppress_default=True)

    pattern_promote = pattern_commands.add_parser(
        "promote", help="activate a reviewed scoped pattern after Core approval"
    )
    pattern_promote.add_argument("pattern_id")
    pattern_promote.add_argument("--scope", required=True)
    _add_root_argument(pattern_promote, suppress_default=True)

    execute_parser = commands.add_parser("execute", help="request execution of one task")
    execute_parser.add_argument("task_id")
    execute_parser.add_argument("--recipe", required=True)
    execute_parser.add_argument(
        "--input-hash",
        default=None,
        help="optional sha256 of the attachment; verified against the real "
        "bytes when given. The execution key is always derived from the "
        "attachment itself, so this argument cannot be used to force a replay.",
    )
    rebuild_parser = commands.add_parser("rebuild", help="explicitly create a new result version")
    rebuild_parser.add_argument("task_id")
    rebuild_parser.add_argument("--recipe", required=True)
    rebuild_parser.add_argument("--request-id", required=True, dest="rebuild_request_id")
    rebuild_parser.add_argument("--actor", required=True)
    rebuild_parser.add_argument("--reason", required=True)
    _add_root_argument(rebuild_parser, suppress_default=True)

    _add_root_argument(execute_parser, suppress_default=True)

    confirm_parser = commands.add_parser(
        "confirm", help="record the user's actual task completion"
    )
    confirm_parser.add_argument("task_id")
    confirm_parser.add_argument(
        "--execution-key",
        default=None,
        help="the succeeded execution this completion rests on. Omit it for work "
        "finished outside Core and name --basis instead.",
    )
    confirm_parser.add_argument(
        "--basis",
        choices=("executor", "manual", "external"),
        default=None,
        help="what the completion rests on. Defaults to 'executor' when "
        "--execution-key is given; required without one.",
    )
    confirm_parser.add_argument(
        "--task-version",
        required=True,
        type=int,
        help="current task revision/version being explicitly completed",
    )
    confirm_parser.add_argument("--actor", required=True)
    confirm_parser.add_argument("--reason", required=True)
    _add_root_argument(confirm_parser, suppress_default=True)

    recover_parser = commands.add_parser(
        "recover", help="mark an interrupted executing request as failed"
    )
    recover_parser.add_argument("execution_key")
    recover_parser.add_argument("--actor", required=True)
    recover_parser.add_argument("--reason", required=True)
    _add_root_argument(recover_parser, suppress_default=True)

    feedback_parser = commands.add_parser("feedback", help="record typed operator feedback")
    feedback_parser.add_argument("task_id")
    feedback_parser.add_argument("--type", required=True, choices=FEEDBACK_TYPES, dest="feedback_type")
    feedback_parser.add_argument("--scope", required=True)
    feedback_parser.add_argument("--task-revision", type=int, required=True)
    feedback_parser.add_argument("--actor", required=True)
    feedback_parser.add_argument("--reason", required=True)
    feedback_parser.add_argument("--execution-key", default=None)
    project_parser = commands.add_parser(
        "project", help="register and inspect scoped read-only projects"
    )
    _add_root_argument(project_parser, suppress_default=True)
    project_commands = project_parser.add_subparsers(
        dest="project_command", required=True
    )
    project_register = project_commands.add_parser(
        "register", help="register a ProjectProfile JSON file"
    )
    project_register.add_argument("--profile", type=Path, required=True)
    _add_root_argument(project_register, suppress_default=True)
    project_list = project_commands.add_parser(
        "list", help="list projects in one owner scope"
    )
    project_list.add_argument("--scope", required=True)
    _add_root_argument(project_list, suppress_default=True)
    project_show = project_commands.add_parser(
        "show", help="show one registered project"
    )
    project_show.add_argument("project_id")
    project_show.add_argument("--scope", required=True)
    _add_root_argument(project_show, suppress_default=True)

    prepare_parser = commands.add_parser(
        "prepare", help="build a registered project's read-only preparation plan"
    )
    prepare_parser.add_argument("--scope", required=True)
    prepare_parser.add_argument("--project", dest="project_id", required=True)
    prepare_parser.add_argument("--query", required=True)
    prepare_parser.add_argument("--export", choices=("txt", "md"), default=None)
    prepare_parser.add_argument("--task-id", default=None)
    _add_root_argument(prepare_parser, suppress_default=True)


    notify_parser = commands.add_parser("notify", help="deliver a decision-request notification for a task awaiting review")

    notify_parser.add_argument("task_id")
    notify_parser.add_argument("--profile-id", default="default")
    _add_root_argument(notify_parser, suppress_default=True)
    feedback_parser.add_argument("--details", default=None, type=_json_object)
    _add_root_argument(feedback_parser, suppress_default=True)
    return parser


def _event_log(root: Path) -> EventLog:
    return EventLog(root / "state" / "events.jsonl")


# States where a human decision is still owed, so a notification is meaningful.
_NOTIFIABLE_STATES = {"review_required", "blocked"}


def _notify(args: argparse.Namespace) -> None:
    """Deliver a decision-request notification for a task awaiting review.

    Kept out of run_pipeline on purpose: the pipeline stays offline and
    deterministic, and this is the only place that performs notification I/O.
    It never changes task state -- delivery is recorded in the notification
    store and deduplicated there.
    """
    from automation.dashboard_read import _latest_metadata
    from automation.message_cards import TaskMessageCard
    from automation.notifications import deliver_with_profile
    from automation.notification_store import NotificationProfile, NotificationStore

    task = get_task(args.task_id, root=args.root)
    events = _event_log(args.root).read()
    metadata = _latest_metadata(events).get(task.task_id, {})
    state = metadata.get("state")
    if state not in _NOTIFIABLE_STATES:
        raise ValueError(f"notify blocked: task is not awaiting a decision (state={state})")

    classification = metadata.get("classification") or {}
    assessment = metadata.get("assessment") or {}
    evidence = tuple(
        classification.get("evidence") or assessment.get("evidence") or ["사용자 검토가 필요합니다."]
    )
    version = metadata.get("version") or getattr(task, "revision", 1) or 1
    card = TaskMessageCard(
        task_id=task.task_id,
        task_version=version,
        proposal=classification.get("next_action") or task.title or "검토가 필요한 작업입니다.",
        assessment=assessment.get("automation_level") or state,
        evidence=evidence,
        risk=assessment.get("risk") or "unknown",
        impact=assessment.get("recipe") or "산출물 없음",
    )
    profiles = NotificationStore(args.root).load_profiles()
    profile = (
        NotificationProfile.from_dict(profiles[args.profile_id])
        if args.profile_id in profiles
        else NotificationProfile(profile_id=args.profile_id)
    )
    source_event = next(
        (
            event for event in reversed(events)
            if event.get("task_id") == task.task_id
            and event.get("state") == state
        ),
        {},
    )
    outcome = deliver_with_profile(
        profile,
        task_id=task.task_id,
        event="classification_review" if state == "review_required" else "failure",
        card=card,
        store=NotificationStore(args.root),
        state_version=str(version),
        event_id=source_event.get("event_id") if isinstance(source_event.get("event_id"), str) else None,
        local_reference=f"state/tasks/{task.task_id}.json",
    )
    _print_json(outcome)


def _record_decision(args: argparse.Namespace) -> None:
    from automation.execution_service import record_decision

    decision = record_decision(
        args.task_id,
        args.command,
        actor=args.actor,
        reason=args.reason,
        root=args.root,
        task_version=args.task_version,
        timestamp=args.timestamp,
        details=getattr(args, "details", None),
        recipe_id=getattr(args, "recipe", None),
    )
    _print_json(decision.to_dict())


def _execute(args: argparse.Namespace) -> None:
    from automation.execution_service import execute_task

    outcome = execute_task(
        args.task_id,
        args.recipe,
        root=args.root,
        input_hash=getattr(args, "input_hash", None),
        rebuild_request_id=getattr(args, "rebuild_request_id", None),
        actor=getattr(args, "actor", None),
        reason=getattr(args, "reason", None),
    )
    _print_json(outcome)

def _confirm(args: argparse.Namespace) -> None:
    from automation.execution_service import confirm_task

    outcome = confirm_task(
        args.task_id,
        execution_key=args.execution_key,
        basis=args.basis,
        task_version=args.task_version,
        actor=args.actor,
        reason=args.reason,
        root=args.root,
    )
    _print_json(outcome)


def _recover(args: argparse.Namespace) -> None:
    from automation.execution_service import recover_execution

    _print_json(
        recover_execution(
            args.execution_key,
            actor=args.actor,
            reason=args.reason,
            root=args.root,
        )
    )

def _feedback_command(args: argparse.Namespace) -> None:
    task = get_task(args.task_id, root=args.root)
    if task.scope != args.scope or task.revision != args.task_revision:
        raise ValueError("feedback binding does not match current task scope/revision")
    event = record_feedback(
        args.task_id,
        args.feedback_type,
        scope=args.scope,
        task_revision=args.task_revision,
        actor=args.actor,
        reason=args.reason,
        root=args.root,
        execution_key=args.execution_key,
        details=args.details,
    )
    _print_json(event)



def _record_preparation_event(root: Path, task_id: str, plan: dict[str, Any]) -> None:
    serialized = json.dumps(
        {"task_id": task_id, "plan": plan},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    event = {
        "event_id": f"preparation-{hashlib.sha256(serialized).hexdigest()}",
        "type": "preparation",
        "stage": "planning",
        "state": "preparation_recorded",
        "task_id": task_id,
        "preparation": plan,
    }
    try:
        _event_log(root).append(event)
    except DuplicateEventError:
        pass


def _project(args: argparse.Namespace) -> None:
    if args.project_command == "register":
        payload = json.loads(args.profile.read_text(encoding="utf-8"))
        _print_json(register_project(ProjectProfile.from_dict(payload), root=args.root))
    elif args.project_command == "list":
        _print_json(
            [profile.to_dict() for profile in list_projects(args.scope, root=args.root)]
        )
    elif args.project_command == "show":
        _print_json(
            load_project(args.scope, args.project_id, root=args.root).to_dict()
        )
    else:
        raise ValueError(f"unsupported project command: {args.project_command}")


def _prepare(args: argparse.Namespace) -> None:
    profile = load_project(args.scope, args.project_id, root=args.root)
    if args.task_id is not None:
        get_task(args.task_id, root=args.root)
    plan = build_preparation_plan(profile, query=args.query)
    if args.export is not None:
        output = export_preparation_report(
            plan,
            args.root
            / "state"
            / "preparation-artifacts"
            / profile.scope
            / profile.project_id,
            report_format=args.export,
        )
        plan["report"] = {
            "requested": True,
            "status": "created",
            "format": args.export,
            "path": output.resolve().relative_to(args.root.resolve()).as_posix(),
        }
    else:
        plan["report"] = {"requested": False, "status": "not_requested"}
    if args.task_id is not None:
        plan["task_id"] = args.task_id
        _record_preparation_event(args.root, args.task_id, plan)
    _print_json(plan)


def _pattern(args: argparse.Namespace) -> None:
    if args.pattern_command == "list":
        _print_json([pattern.to_dict() for pattern in list_patterns(args.scope, root=args.root)])
    elif args.pattern_command == "propose":
        _print_json(candidates.propose(args.scope, args.keywords, root=args.root).to_dict())
    elif args.pattern_command == "review":
        _print_json(candidates.review(args.scope, args.pattern_id, root=args.root))
    elif args.pattern_command == "decide":
        _print_json(
            candidates.record_pattern_decision(
                args.scope,
                args.pattern_id,
                args.action,
                actor=args.actor,
                reason=args.reason,
                root=args.root,
            )
        )
    elif args.pattern_command == "promote":
        _print_json(candidates.promote(args.scope, args.pattern_id, root=args.root).to_dict())
    else:  # pragma: no cover - argparse already required a subcommand
        raise ValueError(f"unsupported pattern command: {args.pattern_command}")


def main(argv: list[str] | None = None) -> int:
    # Windows console defaults to cp949; task text routinely holds en-dashes and
    # other non-cp949 characters.  Match parser.py / tools/check_access.py.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        if args.command == "list":
            _print_json([task.to_dict() for task in list_tasks(root=args.root)])
        elif args.command == "show":
            _print_json(get_task(args.task_id, root=args.root).to_dict())
        elif args.command == "confirm":
            _confirm(args)
        elif args.command == "recover":
            _recover(args)
        elif args.command == "prepare":
            _prepare(args)
        elif args.command == "project":
            _project(args)
        elif args.command == "notify":
            _notify(args)
        elif args.command == "feedback":
            _feedback_command(args)
        elif args.command in Decision.ACTIONS:
            _record_decision(args)
        elif args.command == "execute":
            _execute(args)
        elif args.command == "rebuild":
            _execute(args)
        elif args.command == "pattern":
            _pattern(args)
        else:
            raise ValueError(f"unsupported command: {args.command}")
    except Exception as exc:
        print(f"taskctl failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
