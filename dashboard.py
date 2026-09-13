"""Read-only maintenance task dashboard (docs/ARCHITECTURE.md §17).

The dashboard is a business screen over Core state, not a Hermes UI. It reads
through ``build_dashboard_read_model`` and routes decisions, execution, and
completion through the shared Core service. Scoped preparation is the one
remaining CLI adapter because it only builds a read-only plan.

Approval and execution stay separate. A ready assessment is a proposal that
still needs a human decision; only an approved task exposes a deliberate
execution button.

Streamlit is an optional presentation dependency. Importing this module never
requires it, so the data and decision layer stays testable on any checkout.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import taskctl
import recipectl
from automation.execution_service import (
    confirm_task,
    execute_task,
    record_decision,
    recover_execution,
)
from automation.feedback import FEEDBACK_TYPES
from automation.dashboard_read import (
    _safe_relative_path,
    _safe_summary,
    build_dashboard_read_model,
)
from automation.decisions import Decision
from automation.task_store import TaskStoreError, get_task, list_tasks
from automation.notification_store import NotificationStore
DECISION_ACTIONS: tuple[str, ...] = Decision.ACTIONS

_PERSONAL_ACTOR = "owner"
_DECISION_LABELS = {
    "approve": "진행 허용",
    "reject": "처리 안 함",
    "modify": "정보 보완 필요",
    "defer": "나중에 결정",
}
_DECISION_HELP = {
    "approve": "내용을 확인했고 다음 처리 단계로 진행해도 됩니다. 자동 실행이나 게시를 뜻하지는 않습니다.",
    "reject": "이 업무는 현재 처리하지 않습니다. 잘못된 요청, 담당 외 업무, 진행 불가 업무에 사용합니다.",
    "modify": "업무는 필요하지만 정보가 부족합니다. 무엇을 보완해야 하는지 사유에 적습니다.",
    "defer": "지금 결정하지 않습니다. 담당자 확인이나 추가 자료를 기다린 뒤 다시 판단합니다.",
}

_STATUS_LABELS = {
    "new": "새 업무",
    "review_required": "검토 필요",
    "completed": "준비 완료 · 실제 업무 완료 대기",
    "actual_completed": "실제 업무 완료",
    "assessed": "분석 완료",
    "storage_failed": "저장 오류",
    "normalize_failed": "입력 정리 오류",
    "classification_failed": "업무 분류 오류",
    "evaluation_failed": "판정 오류",
    "verification_failed": "결과 검증 실패",
    "verification_unverifiable": "결과 검증 확인 불가",
}
_RESPONSIBILITY_LABELS = {
    "content": "콘텐츠",
    "operations": "운영",
    "development": "개발",
    "design": "디자인",
    "external": "외부 확인",
}
_TASK_TYPE_LABELS = {
    "content_edit": "콘텐츠 수정",
    "data_refresh": "자료 현행화",
    "image_popup": "팝업·이미지 게시",
    "info_request": "자료·정보 확인",
    "audit_report": "점검·보고서",
    "structure_change": "구조 변경",
    "feature_dev": "기능 개발",
    "feature_bug": "오류 수정",
}
_STAGE_LABELS = {
    "input": "수집",
    "storage": "저장",
    "normalize": "파일 처리",
    "classification": "분류",
    "evaluation": "자동화 평가",
    "hermes": "Hermes 검토",
    "render": "사람 검토",
    "execution": "실행",
}
_AUTOMATION_LABELS = {
    "manual": "사람 검토",
    "assisted": "보조 처리",
    "ready": "자동화 후보",
    "developable": "개발 검토",
}
_RISK_LABELS = {
    "read_only": "읽기 전용",
    "remote_write": "외부 변경",
}
# States that need no human decision: the approval policy auto-completed the
# task, or an execution already carried it through.
_SETTLED_STATES = frozenset({"completed", "succeeded", "actual_completed"})
_DECISION_STATUS_LABELS = {
    "approve": "진행 허용됨",
    "reject": "처리 안 함",
    "modify": "보완 요청됨",
    "defer": "보류 중",
}
_EMPTY_MODEL: dict[str, Any] = {"tasks": [], "status_counts": {}}

_IMAGE_ATTACHMENT_TYPES = frozenset({"jpg", "jpeg", "png", "gif"})


# The same shape the task store accepts. An identifier that starts with "-" is
# read by taskctl's parser as an option instead of a value: "-h" makes argparse
# print help and exit 0, which a caller would take for a recorded decision that
# never happened. Reject the shape here rather than rely on the parser.
_SAFE_TASK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _safe_option_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if value.startswith("-"):
        raise ValueError(f"{field} must not begin with a dash")
    return value


class _EventLogReader:
    """Read-only view of the event log.

    The projection only needs ``read``; exposing just that keeps a display
    surface from reaching an append API by accident.
    """

    def __init__(self, path: Path):
        self._path = path

    def read(self) -> list[dict[str, Any]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return []
        events = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


class _TaskStoreReader:
    def __init__(self, root: Path):
        self._root = root

    def list_tasks(self) -> list[Any]:
        try:
            return list_tasks(root=self._root)
        except (OSError, ValueError):
            return []


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_bundle_file(bundle: Path, relative: Any) -> bool:
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")):
        return False
    try:
        candidate = (bundle / relative).resolve()
        candidate.relative_to(bundle.resolve())
        return candidate.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _bundle_rows(downloads_root: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted(downloads_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return []

    rows = []
    for bundle in entries:
        if not bundle.is_dir() or bundle.name.startswith("."):
            continue
        manifest = _json_object(bundle / "manifest.json") or {}
        attachments = manifest.get("attachments")
        declared = attachments if isinstance(attachments, list) else []
        present = sum(
            _safe_bundle_file(bundle, item.get("path"))
            for item in declared
            if isinstance(item, dict)
        )
        marker = (bundle / "_READY").is_file()
        rows.append(
            {
                "bundle": bundle.name,
                "capture_id": manifest.get("capture_id", ""),
                "marker": marker,
                "manifest": (bundle / "manifest.json").is_file(),
                "page": _safe_bundle_file(bundle, manifest.get("page", {}).get("path"))
                if isinstance(manifest.get("page"), dict)
                else False,
                "attachments": f"{present}/{len(declared)}",
                "state": "complete" if marker and present == len(declared) else "partial",
            }
        )
    return rows


def _inbox_rows(inbox_root: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted(inbox_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return []

    rows = []
    for bundle in entries:
        if not bundle.is_dir() or bundle.name.startswith("."):
            continue
        work_item_path = bundle / "work_item.json"
        work_item = _json_object(work_item_path) or {}
        try:
            attachment_count = sum(
                child.is_file() for child in (bundle / "attachments").iterdir()
            )
        except OSError:
            attachment_count = 0
        title = _safe_summary(work_item.get("title"))
        source = work_item.get("source")
        source_id = source.get("id") if isinstance(source, dict) else None
        rows.append(
            {
                "capture_id": bundle.name,
                "task_id": work_item.get("task_id", ""),
                "업무명": title or "제목 확인 필요",
                "출처": str(source_id) if isinstance(source_id, str) else "확인 필요",
                "received_at": work_item.get("received_at", ""),
                "work_item": work_item_path.is_file(),
                "attachments": attachment_count,
                "state": "accepted" if work_item_path.is_file() else "incomplete",
            }
        )
    return rows

def _event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for event in events:
        task_id = event.get("task_id", "")
        stage = event.get("stage")
        state = event.get("state")
        if not all(isinstance(value, str) and value for value in (stage, state)):
            continue
        if (
            task_id
            and _SAFE_TASK_ID.fullmatch(task_id) is None
            or _SAFE_TASK_ID.fullmatch(stage) is None
            or _SAFE_TASK_ID.fullmatch(state) is None
        ):
            continue
        rows.append(
            {
                "event_id": event.get("event_id", ""),
                "task_id": task_id,
                "stage": stage,
                "state": state,
            }
        )
    return rows


def _execution_rows(root: Path, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    directory = root / "state" / "executions"
    try:
        paths = sorted(directory.glob("*.json"), key=lambda path: path.name.casefold())
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    verification_by_key: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
    quarantine_by_key: dict[str, list[tuple[tuple[int, int] | None, str]]] = {}
    for event in events or []:
        execution = event.get("execution")
        if not isinstance(execution, dict):
            continue
        key = execution.get("execution_key")
        if not isinstance(key, str) or _SAFE_TASK_ID.fullmatch(key) is None:
            continue
        result_version = execution.get("result_version")
        attempt = execution.get("attempt")
        rank = (
            (result_version, attempt)
            if (
                isinstance(result_version, int)
                and not isinstance(result_version, bool)
                and result_version > 0
                and isinstance(attempt, int)
                and not isinstance(attempt, bool)
                and attempt > 0
            )
            else None
        )
        if event.get("type") == "verification" and isinstance(event.get("verification"), dict):
            if rank is not None:
                prior = verification_by_key.get(key)
                if prior is None or rank >= prior[0]:
                    verification_by_key[key] = (rank, event["verification"])
        quarantine = _safe_relative_path(event.get("quarantine"))
        if quarantine is not None:
            quarantine_by_key.setdefault(key, []).append((rank, quarantine))
    for path in paths:
        record = _json_object(path)
        if record is None:
            continue
        key = record.get("execution_key", path.stem)
        task_id = record.get("task_id")
        recipe_id = record.get("recipe_id")
        state = record.get("state")
        if (
            not isinstance(key, str)
            or key != path.stem
            or _SAFE_TASK_ID.fullmatch(key) is None
            or not isinstance(task_id, str)
            or _SAFE_TASK_ID.fullmatch(task_id) is None
            or not isinstance(recipe_id, str)
            or _SAFE_TASK_ID.fullmatch(recipe_id) is None
            or not isinstance(state, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", state) is None
        ):
            continue
        result_version = record.get("result_version", 1)
        attempt = record.get("attempt", 1)
        if (
            not isinstance(result_version, int)
            or isinstance(result_version, bool)
            or result_version < 1
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
        ):
            continue
        rank = (result_version, attempt)
        verification_entry = verification_by_key.get(path.stem)
        verification = (
            verification_entry[1]
            if verification_entry is not None and verification_entry[0] == rank
            else {}
        )
        quarantines = quarantine_by_key.get(path.stem, [])
        history = list(dict.fromkeys(item[1] for item in quarantines))
        current_quarantine = next(
            (
                value
                for item_rank, value in reversed(quarantines)
                if item_rank is None or item_rank == rank
            ),
            None,
        ) if state == "failed" else None
        row: dict[str, Any] = {
            "execution_key": path.stem,
            "task_id": task_id,
            "recipe_id": recipe_id,
            "state": state,
            "result_version": result_version,
            "attempt": attempt,
            "quarantine": current_quarantine,
            "verification": verification.get("status", "unverifiable")
            if isinstance(verification, dict)
            else "unverifiable",
            "handoff": (
                verification.get("handoff", {}).get("status", "pending")
                if isinstance(verification, dict)
                and isinstance(verification.get("handoff"), dict)
                else "pending"
            ),
        }
        rebuild_request_id = record.get("rebuild_request_id")
        if isinstance(rebuild_request_id, str) and _SAFE_TASK_ID.fullmatch(rebuild_request_id):
            row["rebuild_request_id"] = rebuild_request_id
        if history:
            row["quarantine_history"] = history
        rows.append(row)
    return rows


def _default_runtime_root(root: Path | str = ".") -> Path:
    """Resolve the production runtime directory used by the web dashboard."""
    explicit = os.environ.get("UPMU_DASHBOARD_RUNTIME")
    if explicit and explicit.strip():
        return Path(explicit).expanduser().resolve()

    initial_root = Path(root).expanduser().resolve()
    if initial_root.name.casefold() == "runtime":
        return initial_root
    repository_root = Path(__file__).resolve().parent
    runtime_candidate = initial_root / "runtime"
    if initial_root == repository_root or runtime_candidate.is_dir():
        return runtime_candidate
    return initial_root


def _default_downloads_root() -> Path:
    return Path.home() / "Downloads" / "upmuzadong-inbox"


def _default_inbox_root(runtime_root: Path | str) -> Path:
    return Path(runtime_root) / "inbox"


def _notification_rows(root: Path | str) -> list[dict[str, Any]]:
    from automation.notification_store import NotificationStore

    fields = (
        "delivery_key",
        "task_id",
        "event",
        "channel",
        "status",
        "attempt",
        "retryable",
        "local_reference",
        "recorded_at",
    )
    rows: list[dict[str, Any]] = []
    for record in NotificationStore(root).read_deliveries():
        row = {
            field: record[field]
            for field in fields
            if field in record
            and isinstance(record[field], (str, int, bool))
            and not isinstance(record[field], float)
        }
        if row:
            rows.append(row)
    return rows


def load_pipeline_model(
    *,
    root: Path | str = ".",
    downloads_root: Path | str | None = None,
    inbox_root: Path | str | None = None,
) -> dict[str, Any]:
    """Project all local pipeline stages without exposing source content."""
    runtime_root = Path(root)
    if downloads_root is None:
        downloads_root = _default_downloads_root()
    if inbox_root is None:
        inbox_root = _default_inbox_root(runtime_root)
    event_reader = _EventLogReader(runtime_root / "state" / "events.jsonl")
    events = event_reader.read()
    read_model = load_read_model(root=runtime_root)
    return {
        "bundles": _bundle_rows(Path(downloads_root)),
        "inbox": _inbox_rows(Path(inbox_root)),
        "tasks": read_model["tasks"],
        "status_counts": read_model["status_counts"],
        "events": _event_rows(events),
        "executions": _execution_rows(runtime_root, events),
        "notifications": _notification_rows(runtime_root),
    }

def load_read_model(*, root: Path | str = ".") -> dict[str, Any]:
    root = Path(root)
    if not root.exists():
        return dict(_EMPTY_MODEL)
    try:
        return build_dashboard_read_model(
            _TaskStoreReader(root),
            _EventLogReader(root / "state" / "events.jsonl"),
            NotificationStore(root),
        )
    except (OSError, TypeError, ValueError):
        return dict(_EMPTY_MODEL)




def submit_decision(
    task_id: str,
    action: str,
    *,
    actor: str,
    reason: str,
    version: int,
    root: Path | str,
    details: dict[str, Any] | None = None,
    recipe: str | None = None,
) -> int:
    """Record a decision through the shared Core service."""
    if action not in DECISION_ACTIONS:
        raise ValueError(f"action must be one of {DECISION_ACTIONS}")
    if not isinstance(task_id, str) or _SAFE_TASK_ID.fullmatch(task_id) is None:
        raise ValueError("task_id must be a safe identifier")
    _safe_option_value(actor, "actor")
    _safe_option_value(reason, "reason")
    if recipe is not None:
        _safe_option_value(recipe, "recipe")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer")
    try:
        record_decision(
            task_id,
            action,
            actor=actor,
            reason=reason,
            root=root,
            task_version=version,
            details=details,
            recipe_id=recipe,
        )
    except Exception as exc:
        print(f"taskctl failed: {exc}", file=sys.stderr)
        return 1
    return 0



def submit_registered_preparation(
    project_id: str,
    query: str,
    *,
    scope: str,
    root: Path | str,
    task_id: str | None = None,
    report: str | Path | None = None,
    report_format: str | None = None,
) -> int:
    """Build a scoped read-only plan through the remaining CLI adapter."""
    project_text = _safe_option_value(project_id, "project_id")
    scope_text = _safe_option_value(scope, "scope")
    query_text = _safe_option_value(query, "query")
    argv = [
        "prepare",
        "--scope",
        scope_text,
        "--project",
        project_text,
        "--query",
        query_text,
        "--root",
        str(root),
    ]
    if task_id is not None:
        if _SAFE_TASK_ID.fullmatch(task_id) is None:
            raise ValueError("task_id must be a safe identifier")
        argv.extend(["--task-id", task_id])
    if report is not None:
        raise ValueError("explicit report paths are not supported")
    if report_format is not None:
        if report_format not in {"txt", "md"}:
            raise ValueError("report_format must be txt or md")
        argv.extend(["--export", report_format])
    return taskctl.main(argv)


def submit_execution(task_id: str, recipe_id: str, *, root: Path | str) -> int:
    """Run one approved recipe through the shared Core execution service."""
    if not isinstance(task_id, str) or _SAFE_TASK_ID.fullmatch(task_id) is None:
        raise ValueError("task_id must be a safe identifier")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        raise ValueError("recipe_id must be non-empty")
    try:
        execute_task(task_id, recipe_id, root=root)
    except Exception as exc:
        print(f"taskctl failed: {exc}", file=sys.stderr)
        return 1
    return 0


def submit_rebuild(task_id: str, recipe_id: str, request_id: str, *, root: Path | str) -> int:
    if not isinstance(task_id, str) or _SAFE_TASK_ID.fullmatch(task_id) is None:
        raise ValueError("task_id must be a safe identifier")
    if not isinstance(request_id, str) or _SAFE_TASK_ID.fullmatch(request_id) is None:
        raise ValueError("request_id must be a safe identifier")
    try:
        execute_task(
            task_id,
            recipe_id,
            root=root,
            rebuild_request_id=request_id,
            actor=_PERSONAL_ACTOR,
            reason="dashboard explicit rebuild",
        )
    except Exception as exc:
        print(f"taskctl failed: {exc}", file=sys.stderr)
        return 1
    return 0


def submit_recipe_create(payload: dict[str, Any], *, root: Path | str) -> int:
    """Create through recipectl and refresh the Core-owned projection."""
    if not isinstance(payload, dict):
        raise ValueError("recipe payload must be an object")
    argv = _recipe_argv("create", payload, root=root)
    code = recipectl.main(argv)
    if code == 0:
        _recipe_rows(payload.get("scope"), root)
    return code


def submit_recipe_update(
    recipe_id: str, payload: dict[str, Any], *, scope: str, root: Path | str
) -> int:
    if not isinstance(payload, dict):
        raise ValueError("recipe payload must be an object")
    argv = _recipe_argv("update", payload, root=root, recipe_id=recipe_id)
    code = recipectl.main(argv)
    if code == 0:
        _recipe_rows(scope, root)
    return code


def submit_recipe_test(
    recipe_id: str,
    *,
    scope: str,
    root: Path | str,
    task_id: str | None = None,
    task_file: Path | str | None = None,
    retry: bool = False,
) -> int:
    if (task_id is None) == (task_file is None):
        raise ValueError("exactly one Core task id or task file is required")
    if task_id is not None:
        _safe_option_value(task_id, "task_id")
    argv = ["test", recipe_id, "--scope", _safe_option_value(scope, "scope")]
    argv += ["--task-id", task_id] if task_id is not None else ["--task-file", str(task_file)]
    if retry:
        argv.append("--retry")
    argv += ["--root", str(root)]
    code = recipectl.main(argv)
    if code == 0:
        _recipe_rows(scope, root)
    return code


def submit_recipe_review(
    recipe_id: str, *, scope: str, actor: str, reason: str, root: Path | str
) -> int:
    argv = [
        "review",
        _safe_option_value(recipe_id, "recipe_id"),
        "--scope",
        _safe_option_value(scope, "scope"),
        "--actor",
        _safe_option_value(actor, "actor"),
        "--reason",
        _safe_option_value(reason, "reason"),
        "--root",
        str(root),
    ]
    code = recipectl.main(argv)
    if code == 0:
        _recipe_rows(scope, root)
    return code


def submit_recipe_activate(
    recipe_id: str, *, scope: str, actor: str, reason: str, root: Path | str
) -> int:
    argv = [
        "activate",
        _safe_option_value(recipe_id, "recipe_id"),
        "--scope",
        _safe_option_value(scope, "scope"),
        "--actor",
        _safe_option_value(actor, "actor"),
        "--reason",
        _safe_option_value(reason, "reason"),
        "--root",
        str(root),
    ]
    code = recipectl.main(argv)
    if code == 0:
        _recipe_rows(scope, root)
    return code


def _recipe_rows(scope: str | None, root: Path | str) -> list[dict[str, Any]]:
    from automation.recipe_onboarding import list_drafts

    return list_drafts(scope=scope, root=root)


def _recipe_argv(
    command: str,
    payload: dict[str, Any],
    *,
    root: Path | str,
    recipe_id: str | None = None,
) -> list[str]:
    required = (
        "recipe_id",
        "task_name",
        "case_reference",
        "description",
        "executor",
        "input_type",
        "output_type",
        "steps",
        "applicability",
        "required_conditions",
        "exclusion_conditions",
        "success_checks",
        "scope",
    )
    values = dict(payload)
    if recipe_id is not None:
        values["recipe_id"] = recipe_id
    for field in required:
        if field not in values:
            raise ValueError(f"recipe {field} is required")
    argv = [command]
    if command == "update":
        argv.append(_safe_option_value(str(values["recipe_id"]), "recipe_id"))
    option_fields = (
        ("task_name", "task-name"),
        ("case_reference", "case-reference"),
        ("description", "description"),
        ("executor", "executor"),
        ("input_type", "input-type"),
        ("output_type", "output-type"),
        ("template", "template"),
        ("approval_scope", "approval-scope"),
    )
    if command == "create":
        argv += ["--recipe-id", _safe_option_value(str(values["recipe_id"]), "recipe_id")]
    for key, option in option_fields:
        if values.get(key) is not None:
            argv += [f"--{option}", _safe_option_value(str(values[key]), key)]
    for key, option in (
        ("steps", "steps"),
        ("applicability", "applicability"),
        ("execution", "execution"),
        ("verification", "verification"),
        ("recognition", "recognition"),
        ("error_policy", "error-policy"),
        ("ai_policy", "ai-policy"),
    ):
        if values.get(key) is not None:
            argv += [f"--{option}", json.dumps(values[key], ensure_ascii=False, sort_keys=True)]
    for key, option in (
        ("required_conditions", "required-condition"),
        ("exclusion_conditions", "exclusion-condition"),
        ("success_checks", "success-check"),
    ):
        for value in values[key]:
            argv += [f"--{option}", _safe_option_value(str(value), key)]
    argv += ["--scope", _safe_option_value(str(values["scope"]), "scope"), "--root", str(root)]
    return argv
def submit_confirmation(
    task_id: str,
    execution_key: str | None = None,
    *,
    basis: str | None = None,
    actor: str,
    reason: str,
    version: int,
    root: Path | str,
) -> int:
    """Record actual completion through the shared Core service."""
    _safe_option_value(task_id, "task_id")
    _safe_option_value(actor, "actor")
    _safe_option_value(reason, "reason")
    if execution_key is not None:
        _safe_option_value(execution_key, "execution_key")
    if basis not in {None, "executor", "manual", "external"}:
        raise ValueError("basis must be executor, manual, or external")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer")
    try:
        confirm_task(
            task_id,
            execution_key=execution_key,
            basis=basis,
            task_version=version,
            actor=actor,
            reason=reason,
            root=root,
        )
    except Exception as exc:
        print(f"taskctl failed: {exc}", file=sys.stderr)
        return 1
    return 0




def submit_feedback(
    task_id: str,
    feedback_type: str,
    *,
    actor: str,
    reason: str,
    root: Path | str,
    execution_key: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Persist typed feedback through Core."""
    for field, value in (("task_id", task_id), ("feedback_type", feedback_type), ("actor", actor), ("reason", reason)):
        _safe_option_value(value, field)
    argv = [
        "feedback",
        task_id,
        "--type",
        feedback_type,
        "--actor",
        actor,
        "--reason",
        reason,
        "--root",
        str(root),
    ]
    if execution_key:
        argv += ["--execution-key", execution_key]
    if details:
        argv += ["--details", json.dumps(details, ensure_ascii=False, sort_keys=True)]
    return taskctl.main(argv)


def submit_recovery(
    execution_key: str,
    *,
    actor: str,
    reason: str,
    root: Path | str,
) -> int:
    for field, value in (("execution_key", execution_key), ("actor", actor), ("reason", reason)):
        _safe_option_value(value, field)
    try:
        recover_execution(execution_key, actor=actor, reason=reason, root=root)
    except Exception as exc:
        print(f"taskctl failed: {exc}", file=sys.stderr)
        return 1
    return 0

def _execution_for_task(executions: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    def positive_int(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    candidates = [execution for execution in executions if execution.get("task_id") == task_id]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda execution: (
            positive_int(execution.get("result_version")),
            positive_int(execution.get("attempt")),
        ),
    )




def _show_table(st, rows: list[dict[str, Any]], empty_message: str) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        _show_empty_state(st, "표시할 데이터가 없습니다", empty_message)
def _classification_label(value: Any, mapping: dict[str, str], fallback: str = "확인 필요") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return mapping.get(text, text or fallback)


def _classification_reason(detail: dict[str, Any]) -> str:
    classification = detail.get("classification") or {}
    evidence = classification.get("evidence") or []
    if evidence:
        return str(evidence[0])
    review = detail.get("classification_review") or {}
    return str(review.get("reason") or "확인 필요")


def _classification_confidence(detail: dict[str, Any]) -> str:
    value = (detail.get("classification") or {}).get("confidence")
    return f"{value:.0%}" if isinstance(value, (int, float)) else "확인 필요"


def _label(mapping: dict[str, str], value: Any, fallback: str = "확인 필요") -> str:
    return mapping.get(str(value), fallback) if value is not None else fallback


def _review_status(detail: dict[str, Any]) -> str:
    decision = detail.get("decision")
    if isinstance(decision, dict):
        action = decision.get("action")
        if action in _DECISION_STATUS_LABELS:
            return _DECISION_STATUS_LABELS[action]
    return _label(_STATUS_LABELS, detail.get("state"))


def _is_decision_pending(detail: dict[str, Any]) -> bool:
    # A task the approval policy already settled owes nobody a click. Without
    # this the review queue holds every task the pipeline ever saw, which is
    # why the board was easier to read than the dashboard.
    if detail.get("state") in _SETTLED_STATES:
        return False
    return not isinstance(detail.get("decision"), dict)


def _task_table_rows(tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for detail in tasks:
        classification = detail.get("classification") or {}
        assessment = detail.get("assessment") or {}
        attachments = detail.get("attachments") or []
        evidence_count = len(
            (classification.get("evidence") or [])
            + (assessment.get("evidence") or [])
        )
        rows.append(
            {
                "업무명": str(detail.get("display_name") or detail.get("summary") or "제목 비공개"),
                "도메인": str(detail.get("domain_label") or "기타 업무"),
                "직무/역할": _classification_label(
                    classification.get("primary_role") or classification.get("responsibility"),
                    _RESPONSIBILITY_LABELS,
                ),
                "책임 판정": _classification_label(classification.get("ownership"), {}),
                "업무 유형": _classification_label(
                    classification.get("task_type"), _TASK_TYPE_LABELS
                ),
                "확신도": _classification_confidence(detail),
                "핵심 근거": _classification_reason(detail),
                "단계": _label(_STAGE_LABELS, detail.get("stage"), "확인 필요"),
                "판단": _review_status(detail),
                "자동화": _label(_AUTOMATION_LABELS, assessment.get("automation_level")),
                "첨부": f"{len(attachments)}개",
                "근거": f"{evidence_count}개",
            }
        )
    return rows


def _inject_theme_css(st) -> None:
    """Bridge the few dashboard-specific details not covered by Streamlit theme."""
    st.markdown(
        """
        <style>
        html, body, [data-testid="stAppViewContainer"] {
            font-feature-settings: "calt", "kern", "liga", "ss03";
        }
        [data-testid="stAppViewContainer"],
        [data-testid="stHeader"] {
            background: #07080a;
        }
        [data-testid="stHeader"] {
            border-bottom: 1px solid #242728;
        }
        [data-testid="stSidebar"] {
            background: #0d0d0d;
            border-right: 1px solid #242728;
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] label {
            color: #cdcdcd;
        }
        [data-testid="stMetric"] {
            background: #0d0d0d;
            border: 1px solid #242728;
            border-radius: 10px;
            padding: 16px;
        }
        [data-testid="stMetricLabel"] {
            color: #9c9c9d;
        }
        [data-testid="stMetricValue"] {
            color: #f4f4f6;
        }
        [data-testid="stTextInput"] input,
        [data-testid="stSelectbox"] [data-baseweb="select"] > div,
        [data-testid="stTextArea"] textarea {
            background: #101111;
            border-color: #242728;
            color: #f4f4f6;
            border-radius: 8px;
        }
        [data-testid="stButton"] button,
        [data-testid="stFormSubmitButton"] button {
            border-radius: 8px;
            font-weight: 500;
            letter-spacing: 0.2px;
        }
        [data-testid="stButton"] button[kind="primary"],
        [data-testid="stFormSubmitButton"] button[kind="primary"] {
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _show_empty_state(st, title: str, message: str, icon: str = "inbox") -> None:
    with st.container(border=True):
        st.subheader(title, icon=f":material/{icon}:")
        st.caption(message)


def _show_status(st, label: str, state: str) -> None:
    color = {
        "ok": "green",
        "error": "red",
        "attention": "orange",
        "muted": "gray",
    }.get(state, "gray")
    icon = {
        "ok": ":material/check:",
        "error": ":material/error:",
        "attention": ":material/schedule:",
        "muted": ":material/remove_circle_outline:",
    }.get(state, ":material/info:")
    st.badge(label, icon=icon, color=color)


def _detail_metadata(detail: dict[str, Any]) -> dict[str, str]:
    metadata = {
        "업무 ID": str(detail.get("task_id", "")),
        "상태": _review_status(detail),
        "현재 단계": _label(_STAGE_LABELS, detail.get("stage")),
        "업무 도메인": str(detail.get("domain_label") or "기타 업무"),
        "마스킹": "처리 기록 있음" if detail.get("masking") == "recorded" else "확인 필요",
        "버전": str(detail.get("version", 1)),
        "Source 완료 관찰": "관찰됨" if detail.get("source_completion_observed") else "관찰 안 됨",
        "실제 업무 완료": (
            "확인됨" if detail.get("actual_completion") else "사용자 확인 대기"
        ),
    }
    deliveries = detail.get("notification_delivery") or []
    if deliveries:
        latest = deliveries[-1]
        metadata["알림 전달"] = (
            f"{latest.get('event', 'unknown')} / "
            f"{latest.get('channel', 'unknown')} / {latest.get('status', 'unknown')}"
        )
    else:
        metadata["알림 전달"] = "기록 없음"
    if detail.get("received_at"):
        metadata["접수 시각"] = str(detail["received_at"])
    source = detail.get("source")
    if isinstance(source, dict):
        source_text = " / ".join(
            str(source[key]) for key in ("type", "id", "external_id")
            if source.get(key)
        )
        if source_text:
            metadata["출처"] = source_text
    return metadata


def _detail_card_rows(detail: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return the stable Korean detail projection used by dashboard tests."""
    classification = detail.get("classification") or {}
    assessment = detail.get("assessment") or {}
    size_labels = {
        "simple": "단순",
        "recurring": "반복",
        "small_dev": "소규모 개발",
        "medium_feature": "중간 규모 기능",
        "large_feature": "대규모 기능",
        "unclear": "확인 필요",
    }
    return [
        ("업무 상태", _review_status(detail), "blue"),
        ("업무 영역", _classification_label(classification.get("responsibility"), _RESPONSIBILITY_LABELS), "purple"),
        ("무슨 일인가", _classification_label(classification.get("task_type"), _TASK_TYPE_LABELS), "teal"),
        ("업무 범위", _label(size_labels, classification.get("size")), "orange"),
        ("처리 방법", _label(_AUTOMATION_LABELS, assessment.get("automation_level")), "green"),
        ("외부 영향", _label(_RISK_LABELS, assessment.get("risk")), "red"),
        *(
            [("분류 확신도", f"{classification['confidence']:.0%}", "gray")]
            if isinstance(classification.get("confidence"), (int, float))
            else []
        ),
        *(
            [("판정 확신도", f"{assessment['confidence']:.0%}", "gray")]
            if isinstance(assessment.get("confidence"), (int, float))
            else []
        ),
    ]


def _detail_tag_groups(detail: dict[str, Any]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = _detail_card_rows(detail)
    return [
        ("현재 상태", rows[:1]),
        ("업무 분류", rows[1:4]),
        ("처리 판단", rows[4:6]),
        ("확신도", rows[6:]),
    ]


def _header_led_rows(
    model: dict[str, Any],
    hermes_events: list[dict[str, Any]],
    failed_events: list[dict[str, Any]],
) -> list[tuple[str, bool, str]]:
    tasks = model["tasks"]
    executions = model["executions"]
    terminal_on = bool(tasks) and not any(
        str(task.get("state", "")).endswith("failed") for task in tasks
    )
    execution_on = bool(executions) and not any(
        str(execution.get("state", "")).endswith("failed")
        for execution in executions
    )
    hermes_on = bool(hermes_events) and hermes_events[-1].get("state") == "assessed"
    return [
        ("Downloads", bool(model["bundles"]), "normal"),
        ("Core Inbox", bool(model["inbox"]), "normal"),
        ("Tasks", bool(tasks), "normal"),
        ("Hermes", hermes_on, "normal"),
        ("Executions", execution_on, "normal"),
        ("Terminal", terminal_on, "normal"),
        ("오류 감지", bool(failed_events), "error"),
    ]


def _detail_cards(detail: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (label, value)
        for label, value, _tone in _detail_card_rows(detail)[:6]
    ]


def _show_detail_cards(st, detail: dict[str, Any]) -> None:
    cards = _detail_cards(detail)
    for start in range(0, len(cards), 3):
        row = cards[start:start + 3]
        columns = st.columns(len(row), gap="small")
        for column, (label, value) in zip(columns, row):
            with column:
                with st.container(border=True):
                    st.caption(label)
                    st.markdown(f"**{html.escape(value)}**", unsafe_allow_html=True)
    classification = detail.get("classification") or {}
    assessment = detail.get("assessment") or {}
    confidence_rows = [
        ("분류 확신도", classification.get("confidence")),
        ("판정 확신도", assessment.get("confidence")),
    ]
    confidence_rows = [
        (label, value) for label, value in confidence_rows
        if isinstance(value, (int, float))
    ]
    if confidence_rows:
        with st.container(border=True):
            st.caption("확신도")
            columns = st.columns(len(confidence_rows), gap="small")
            for column, (label, value) in zip(columns, confidence_rows):
                with column:
                    st.progress(
                        max(0.0, min(1.0, float(value))),
                        text=f"{label} · {value:.0%}",
                    )


def _effective_failed_events(model: dict[str, Any]) -> list[dict[str, Any]]:
    states = {
        str(detail.get("task_id")): str(detail.get("state"))
        for detail in model["tasks"]
    }
    return [
        event
        for event in model["events"]
        if str(event.get("state", "")).endswith("failed")
        and not (
            event.get("state") == "storage_failed"
            and states.get(str(event.get("task_id"))) == "review_required"
        )
    ]


def _stage_rows(model: dict[str, Any]) -> list[tuple[str, int, str, str]]:
    tasks = model["tasks"]
    inbox_task_ids = {
        str(row.get("task_id"))
        for row in model["inbox"]
        if row.get("task_id")
    }
    classified = sum(bool(detail.get("classification")) for detail in tasks)
    pending = sum(_is_decision_pending(detail) for detail in tasks)
    approved_ready = sum(
        isinstance(detail.get("decision"), dict)
        and detail["decision"].get("action") == "approve"
        and (detail.get("assessment") or {}).get("automation_level")
        in {"ready", "developable"}
        for detail in tasks
    )
    succeeded = sum(
        str(item.get("state")) == "succeeded" for item in model["executions"]
    )
    return [
        (
            "수집 대기",
            len(model["bundles"]),
            "Bundle 있음" if model["bundles"] else "비어 있음",
            "ok" if model["bundles"] else "muted",
        ),
        (
            "수집 완료",
            len(inbox_task_ids),
            "업무 식별됨" if inbox_task_ids else "비어 있음",
            "ok" if inbox_task_ids else "muted",
        ),
        (
            "분류 완료",
            classified,
            "업무 분류됨" if classified else "대기 중",
            "ok" if classified else "muted",
        ),
        (
            "사람 판단",
            pending,
            "결정 대기" if pending else "결정 완료",
            "attention" if pending else "ok",
        ),
        (
            "실행 대기",
            approved_ready,
            "승인된 자동화" if approved_ready else "해당 없음",
            "attention" if approved_ready else "muted",
        ),
        (
            "실행 완료",
            succeeded,
            "산출물 확인" if succeeded else "기록 없음",
            "ok" if succeeded else "muted",
        ),
    ]


def _local_attachment_path(root: Path, raw_ref: Any) -> Path | None:
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        return None
    if re.match(r"\A[A-Za-z][A-Za-z0-9+.\-]*://", raw_ref) or raw_ref.startswith(("\\\\", "//")):
        return None
    try:
        candidate = Path(raw_ref).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        return candidate if candidate.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def build_task_content_preview(root: Path | str, task_id: str) -> dict[str, Any]:
    """Load local post content and bounded attachment previews for one task."""
    try:
        task = get_task(task_id, root=root)
    except (OSError, ValueError, TaskStoreError):
        return {"body": "", "attachments": []}

    from automation.document_preview import preview_attachment, sanitize_preview_text

    attachments: list[dict[str, Any]] = []
    for attachment in task.attachments:
        local_path = _local_attachment_path(Path(root), attachment.raw_ref)
        preview = (
            preview_attachment(local_path, attachment.type)
            if local_path is not None
            else {
                "status": "missing",
                "parser": None,
                "text": "",
                "message": "첨부 원본이 로컬 파일이 아니거나 찾을 수 없습니다.",
            }
        )
        attachments.append(
            {
                "name": attachment.name,
                "type": attachment.type,
                "local_path": local_path,
                "preview": preview,
            }
        )
    return {
        "body": sanitize_preview_text(task.body),
        "attachments": attachments,
    }


def _render_actual_completion(st, detail: dict[str, Any], executions: list[dict[str, Any]], runtime_root: Path) -> None:
    """Render completion independently of approval, Recipe, or notification state."""
    task_id = str(detail.get("task_id", ""))
    completion = detail.get("actual_completion") or {}
    with st.container(border=True):
        st.subheader("실제 업무 완료", icon=":material/task_alt:")
        if completion:
            st.success(
                f"완료 기록됨 · basis={completion.get('basis', 'executor')} · "
                f"actor={completion.get('actor', 'unknown')} · "
                f"time={completion.get('timestamp', 'unknown')}"
            )
            return
        execution = _execution_for_task(executions, task_id)
        passed_executor = (
            execution
            if execution
            and execution.get("state") == "succeeded"
            and (detail.get("verification") or {}).get("status") == "passed"
            else None
        )
        choices = ["manual", "external"]
        if passed_executor:
            choices.insert(0, "executor")
        basis = st.selectbox(
            "완료 근거",
            choices,
            format_func=lambda value: {
                "executor": "Core Executor 실행",
                "manual": "수동 처리",
                "external": "외부 시스템 처리",
            }[value],
            key=f"completion-basis-{task_id}",
        )
        reason = st.text_input(
            "완료 확인 사유",
            value="실제 처리 결과를 확인했습니다.",
            key=f"completion-reason-{task_id}",
        )
        if st.button("실제 업무 완료 기록", key=f"completion-{task_id}", type="primary"):
            if not reason.strip():
                st.error("완료 확인 사유가 필요합니다.", icon=":material/error:")
                return
            execution_key = (
                str(passed_executor.get("execution_key"))
                if basis == "executor" and passed_executor
                else None
            )
            code = submit_confirmation(
                task_id,
                execution_key,
                basis=basis,
                actor=_PERSONAL_ACTOR,
                reason=reason,
                version=int(detail.get("version", 1)),
                root=runtime_root,
            )
            if code == 0:
                st.success("실제 완료가 Core에 기록되었습니다.")
                st.rerun()
            else:
                st.error("실제 완료 기록이 Core에서 거부되었습니다.", icon=":material/error:")


def _latest_hermes_event(events: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("task_id") == task_id and event.get("stage") == "hermes":
            return event
    return None


def _hermes_status_text(event: dict[str, Any] | None) -> str:
    if event is None:
        return "Hermes 미사용 — 규칙 엔진 결과를 사용했습니다."
    state = event.get("state")
    return {
        "assessed": "Hermes 제안 검증 완료",
        "blocked": "Hermes 제안 차단됨",
        "failed": "Hermes를 사용할 수 없음",
    }.get(str(state), f"Hermes 상태: {state}")












def _task_matches(
    detail: dict[str, Any],
    *,
    query: str,
    status: str,
    responsibility: str,
    risk: str,
    domain: str = "전체",
) -> bool:
    classification = detail.get("classification") or {}
    assessment = detail.get("assessment") or {}
    haystack = " ".join(
        str(detail.get(key, "")) for key in ("task_id", "summary", "display_name")
    ).casefold()
    status_matches = (
        status == "전체"
        or (status == "결정 대기" and _is_decision_pending(detail))
        or _review_status(detail) == status
    )
    return (
        (not query or query.casefold() in haystack)
        and status_matches
        and (
            responsibility == "전체"
            or _classification_label(
                classification.get("responsibility"), _RESPONSIBILITY_LABELS
            )
            == responsibility
        )
        and (
            risk == "전체"
            or _label(_RISK_LABELS, assessment.get("risk")) == risk
        )
        and (domain == "전체" or detail.get("domain_label") == domain)
    )
def _next_task_id(tasks: list[dict[str, Any]], current_task_id: str) -> str | None:
    for index, detail in enumerate(tasks):
        if detail.get("task_id") != current_task_id:
            continue
        if index + 1 < len(tasks):
            return str(tasks[index + 1]["task_id"])
        return None
    return None






def _render_overview(st, model: dict[str, Any]) -> None:
    tasks = model["tasks"]
    pending_tasks = [detail for detail in tasks if _is_decision_pending(detail)]
    failed_events = _effective_failed_events(model)

    st.header("파이프라인 현황", icon=":material/space_dashboard:")
    st.caption("수집부터 사람의 결정과 Core 실행 기록까지 한 화면에서 확인합니다.")

    kpi_columns = st.columns(4, gap="small")
    kpis = [
        ("결정 대기", len(pending_tasks), "사람의 확인 필요"),
        ("전체 업무", len(tasks), "Core에 기록된 업무"),
        ("오류 감지", len(failed_events), "즉시 확인 필요"),
        ("실행 기록", len(model["executions"]), "Core 기록 기준"),
    ]
    for column, (label, value, help_text) in zip(kpi_columns, kpis):
        with column:
            st.metric(label, value, help=help_text, border=True)

    st.subheader("처리 단계")
    stages = _stage_rows(model)
    for start in range(0, len(stages), 3):
        row = stages[start:start + 3]
        columns = st.columns(len(row), gap="small")
        for column, (label, count, state_label, tone) in zip(columns, row):
            with column:
                with st.container(border=True):
                    st.markdown(f"**{label}**")
                    st.metric("건수", count)
                    _show_status(st, state_label, tone)

    domain_counts: dict[str, int] = {}
    for detail in tasks:
        domain = str(detail.get("domain_label") or "기타 업무")
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
    with st.container(border=True):
        st.subheader("업무 도메인별 현황", icon=":material/category:")
        _show_table(
            st,
            [
                {"도메인": domain, "업무 수": count}
                for domain, count in sorted(domain_counts.items())
            ],
            "분류된 업무가 없습니다.",
        )

    attention_columns = st.columns(2, gap="large")
    with attention_columns[0]:
        with st.container(border=True):
            st.subheader("결정 대기 업무", icon=":material/pending_actions:")
            if pending_tasks:
                _show_table(st, _task_table_rows(pending_tasks[:5]), "")
                if len(pending_tasks) > 5:
                    st.caption(f"외 {len(pending_tasks) - 5}건 · 업무 검토에서 전체 보기")
            else:
                st.caption("현재 결정 대기 업무가 없습니다.")
    with attention_columns[1]:
        with st.container(border=True):
            st.subheader("최근 오류", icon=":material/error:")
            if failed_events:
                _show_table(st, failed_events[-5:], "")
            else:
                st.caption("최근 오류가 없습니다.")

    with st.container(border=True):
        st.subheader("최근 파이프라인 이벤트", icon=":material/timeline:")
        if model["events"]:
            _show_table(st, model["events"][-20:], "")
        else:
            st.caption("아직 기록된 파이프라인 이벤트가 없습니다.")
        st.caption("이 표에는 원문을 넣지 않습니다. 원문과 첨부는 선택한 업무에서 안전하게 미리봅니다.")

    with st.container(border=True):
        st.subheader("알림 전달 이력", icon=":material/notifications:")
        notifications = model.get("notifications", [])
        if notifications:
            _show_table(st, notifications[-20:], "")
        else:
            st.caption("기록된 알림 전달이 없습니다.")


def _render_review(st, model: dict[str, Any], runtime_root: Path) -> None:
    st.header("업무 검토", icon=":material/rule:")
    notice = st.session_state.pop("_decision_notice", None)
    if notice:
        st.success(notice, icon=":material/check_circle:")

    toolbar = st.columns([2, 1], gap="small")
    query = toolbar[0].text_input(
        "업무 검색",
        placeholder="제목 또는 업무 ID",
        label_visibility="visible",
    )
    status_filter = toolbar[1].segmented_control(
        "상태",
        ["결정 대기", "전체"],
        default="결정 대기",
        key="review-status-filter",
    )
    filter_columns = st.columns([1, 1, 1, 2], gap="small")
    with filter_columns[0].popover("추가 필터", icon=":material/filter_list:"):
        domain_filter = st.selectbox(
            "업무 도메인",
            ["전체"] + sorted({
                str(detail.get("domain_label") or "기타 업무")
                for detail in model["tasks"]
            }),
        )
        responsibility_filter = st.selectbox(
            "담당 영역",
            ["전체"] + sorted({
                _classification_label(
                    (detail.get("classification") or {}).get("responsibility"),
                    _RESPONSIBILITY_LABELS,
                )
                for detail in model["tasks"]
            }),
        )
        risk_filter = st.selectbox(
            "외부 영향",
            ["전체"] + sorted({
                _label(_RISK_LABELS, (detail.get("assessment") or {}).get("risk"))
                for detail in model["tasks"]
            }),
        )
    filter_columns[1].caption(
        f"전체 {len(model['tasks'])}건 중 조건에 맞는 업무를 표시합니다."
    )

    filtered_tasks = [
        detail for detail in model["tasks"]
        if _task_matches(
            detail,
            query=query,
            status=status_filter or "결정 대기",
            responsibility=responsibility_filter if "responsibility_filter" in locals() else "전체",
            risk=risk_filter if "risk_filter" in locals() else "전체",
            domain=domain_filter if "domain_filter" in locals() else "전체",
        )
    ]
    if not filtered_tasks:
        _show_empty_state(st, "검토할 업무가 없습니다", "현재 필터 조건에 맞는 업무가 없습니다.", "task_alt")
        return

    options = {
        f"{detail.get('summary') or '제목 비공개'} · {detail.get('task_id', '')}": detail
        for detail in filtered_tasks
    }
    selected_task_id = st.session_state.get("selected_task_id")
    if not any(item.get("task_id") == selected_task_id for item in filtered_tasks):
        selected_task_id = filtered_tasks[0]["task_id"]
        st.session_state["selected_task_id"] = selected_task_id
    option_labels = list(options)
    selected_index = next(
        (
            index for index, item in enumerate(filtered_tasks)
            if item.get("task_id") == selected_task_id
        ),
        0,
    )

    queue_column, detail_column = st.columns([0.36, 0.64], gap="large")
    with queue_column:
        with st.container(border=True):
            st.subheader("검토 queue", icon=":material/list_alt:")
            selected_label = st.radio(
                "검토할 업무",
                option_labels,
                index=selected_index,
                key=f"review-queue-{','.join(str(item.get('task_id')) for item in filtered_tasks)}",
                label_visibility="collapsed",
            )
            detail = options[selected_label]
            st.caption(f"{len(filtered_tasks)}건 표시 · 개인 프로젝트 모드")
            _show_status(st, _review_status(detail), "attention" if _is_decision_pending(detail) else "ok")

    task_id = detail["task_id"]
    summary = detail.get("summary") or "제목을 표시할 수 없는 업무"
    with detail_column:
        with st.container(border=True):
            st.title(summary)
            st.caption(f"{task_id} · 상태 {_review_status(detail)}")
            st.table(_detail_metadata(detail))

            st.subheader("업무 판단", icon=":material/analytics:")
            _show_detail_cards(st, detail)
            classification_review = detail.get("classification_review") or {}
            if classification_review:
                st.error(
                    "DO NOT · 사용자 검토 대기",
                    icon=":material/pause_circle:",
                )
                st.caption(
                    f"분류 이유 · {classification_review.get('reason', '확인 필요')}"
                )

            content_preview = build_task_content_preview(runtime_root, task_id)
            image_attachments = [
                (index, attachment)
                for index, attachment in enumerate(content_preview["attachments"], start=1)
                if attachment["type"] in _IMAGE_ATTACHMENT_TYPES
            ]
            other_attachments = [
                (index, attachment)
                for index, attachment in enumerate(content_preview["attachments"], start=1)
                if attachment["type"] not in _IMAGE_ATTACHMENT_TYPES
            ]
            with st.expander("게시글 본문 미리보기", expanded=True, icon=":material/article:"):
                body = content_preview["body"] or "표시할 안전한 본문이 없습니다."
                st.text_area(
                    "본문",
                    value=body,
                    height=220,
                    disabled=True,
                    key=f"body-preview-{task_id}",
                )
                if image_attachments:
                    st.caption("본문에 포함된 이미지")
                    columns = st.columns(min(2, len(image_attachments)), gap="small")
                    for column, (_index, attachment) in zip(columns, image_attachments):
                        with column:
                            local_path = attachment.get("local_path")
                            preview = attachment["preview"]
                            image_meta = preview.get("image") or {}
                            st.caption(f"원본 파일명: {attachment['name']} ({attachment['type'].upper()})")
                            if (
                                local_path is not None
                                and preview.get("status") == "parsed"
                                and preview.get("kind") == "image"
                                and image_meta.get("display") == "local"
                                and image_meta.get("ref") == str(local_path)
                            ):
                                st.image(str(local_path), width=240)
                            else:
                                st.caption(preview["message"])
                                if preview.get("failure_reason"):
                                    st.caption(
                                        f"처리 상태: {preview['status']} ({preview['failure_reason']})"
                                    )
            for index, attachment in other_attachments:
                preview = attachment["preview"]
                with st.expander(f"첨부파일 {index} 미리보기", expanded=False):
                    st.caption(f"원본 파일명: {attachment['name']} ({attachment['type'].upper()})")
                    if preview.get("status") == "parsed":
                        metadata = preview.get("metadata") or {}
                        if metadata.get("representation") == "pdf_pages":
                            for page in metadata.get("pages") or []:
                                image = page.get("image") or {}
                                encoded = image.get("data")
                                if image.get("representation") == "inline" and isinstance(encoded, str):
                                    try:
                                        st.image(
                                            base64.b64decode(encoded, validate=True),
                                            caption=f"PDF {page.get('page', '?')}페이지",
                                            width=520,
                                        )
                                    except (ValueError, TypeError):
                                        st.caption("PDF 페이지 이미지 데이터를 표시하지 못했습니다.")
                        elif metadata.get("representation") == "table" and metadata.get("rows"):
                            table = getattr(st, "dataframe", None)
                            if callable(table):
                                table(metadata["rows"], hide_index=True, use_container_width=True)
                        if preview.get("text"):
                            st.text_area(
                                "추출된 내용",
                                value=preview["text"],
                                height=240,
                                disabled=True,
                                key=f"attachment-preview-{task_id}-{index}",
                            )
                    else:
                        st.caption(preview["message"])
                        if preview.get("failure_reason"):
                            st.caption(
                                f"처리 상태: {preview['status']} ({preview['failure_reason']})"
                            )
                    if preview.get("parser"):
                        st.caption(f"사용한 파서: {preview['parser']}")

            with st.expander("프로젝트 정적 준비 계획", expanded=False, icon=":material/account_tree:"):
                st.caption("등록된 프로젝트 프로필을 기준으로 읽기 전용 관련 범위를 계산합니다. 코드·DB를 수정하거나 실행하지 않습니다.")
                existing_preparation = detail.get("preparation") or {}
                if existing_preparation:
                    existing_report = existing_preparation.get("report") or {}
                    st.caption(
                        f"Core 계획 상태: {existing_preparation.get('state', 'unknown')} · "
                        f"보고서 요청={existing_report.get('requested', False)} · "
                        f"생성 결과={existing_report.get('status', 'not_requested')}"
                    )
                with st.form(f"prepare-form-{task_id}", border=False):
                    profile_id = st.text_input(
                        "등록된 프로젝트 ID",
                        placeholder="my-project",
                        key=f"prepare-project-id-{task_id}",
                    )
                    prepare_query = st.text_input(
                        "분석 요청",
                        value=summary,
                        key=f"prepare-query-{task_id}",
                    )
                    report_requested = st.checkbox(
                        "업무별 정적 분석 보고서 생성",
                        value=False,
                        key=f"prepare-report-requested-{task_id}",
                    )
                    report_format = st.selectbox(
                        "보고서 형식",
                        ("md", "txt"),
                        key=f"prepare-report-format-{task_id}",
                        disabled=not report_requested,
                    )
                    prepare_submitted = st.form_submit_button("읽기 전용 계획 생성")
                if prepare_submitted:
                    task_scope = detail.get("scope")
                    if (
                        not isinstance(task_scope, str)
                        or not task_scope
                        or not profile_id.strip()
                        or not prepare_query.strip()
                    ):
                        st.error("업무 범위, 프로젝트 ID와 분석 요청이 필요합니다.", icon=":material/error:")
                    else:
                        code = submit_registered_preparation(
                            profile_id,
                            prepare_query,
                            scope=task_scope,
                            root=runtime_root,
                            task_id=task_id,
                            report_format=report_format if report_requested else None,
                        )
                        if code == 0:
                            updated_detail = next(
                                (
                                    item for item in load_read_model(root=runtime_root)["tasks"]
                                    if item.get("task_id") == task_id
                                ),
                                {},
                            )
                            report = (updated_detail.get("preparation") or {}).get("report") or {}
                            st.success(
                                f"정적 계획 완료 · 보고서 요청={report.get('requested', False)} · "
                                f"생성 결과={report.get('status', 'not_requested')}"
                            )
                        else:
                            st.error("정적 준비 계획 생성에 실패했습니다.", icon=":material/error:")

            with st.expander("Hermes에 전달된 정보", expanded=False, icon=":material/security:"):
                st.caption(_hermes_status_text(_latest_hermes_event(model["events"], task_id)))
                st.markdown(
                    "- 전달: 요청 ID, **마스킹된 요약**, 비식별 직무·책임 정책 문맥\n"
                    "- 미전달: 원문·첨부 내용과 메타데이터, URL, 쿠키·토큰, 로컬 경로\n"
                    "- Hermes 원문 응답은 저장하거나 화면에 노출하지 않습니다."
                )

            with st.expander("판정 및 결과 검증", expanded=True, icon=":material/fact_check:"):
                classification_evidence = (detail.get("classification") or {}).get("evidence", [])
                assessment_evidence = (detail.get("assessment") or {}).get("evidence", [])
                artifacts = detail.get("artifacts", [])
                verification = detail.get("verification") or {}
                for item in classification_evidence:
                    st.caption(f"분류 근거 · {item}")
                for item in assessment_evidence:
                    st.caption(f"자동화 평가 근거 · {item}")
                for item in artifacts:
                    st.caption(f"산출물 · {item}")
                if verification:
                    st.caption(f"결과 검증 · {verification.get('status', 'unverifiable')}")
                    checks = verification.get("checks") or []
                    if checks:
                        _show_table(st, checks, "")
                    handoff = verification.get("handoff") or {}
                    if handoff.get("required"):
                        st.info(
                            f"수동 인계 상태: {handoff.get('status', 'pending')} · "
                            "관리자 페이지 업로드는 사람의 확인이 필요합니다.",
                            icon=":material/publish:",
                        )
                if not classification_evidence and not assessment_evidence and not artifacts and not verification:
                    st.caption("기록된 근거가 없습니다.")

            feedback_records = detail.get("feedback") or []
            with st.expander("처리 피드백", expanded=False, icon=":material/feedback:"):
                if feedback_records:
                    _show_table(st, feedback_records, "")
                with st.form(f"feedback-form-{task_id}", border=False):
                    feedback_type = st.selectbox(
                        "피드백 유형",
                        list(FEEDBACK_TYPES),
                        format_func=lambda value: {
                            "classification_correction": "적용 판단 교정",
                            "execution_defect": "실행 결함",
                            "input_supplement": "입력 보완",
                            "result_correction": "결과 보정",
                        }[value],
                    )
                    feedback_reason = st.text_input(
                        "피드백 사유",
                        placeholder="다음 조건 검토에 사용할 근거",
                    )
                    feedback_details: dict[str, Any] = {}
                    if feedback_type == "classification_correction":
                        current = detail.get("classification") or {}
                        feedback_details["classification"] = {
                            "responsibility": st.text_input(
                                "교정 담당",
                                value=str(current.get("responsibility") or ""),
                            ),
                            "task_type": st.text_input(
                                "교정 업무 유형",
                                value=str(current.get("task_type") or ""),
                            ),
                            "size": st.text_input(
                                "교정 규모",
                                value=str(current.get("size") or ""),
                            ),
                            "confidence": st.number_input(
                                "교정 확신도",
                                min_value=0.0,
                                max_value=1.0,
                                value=float(current.get("confidence", 0.5)),
                                step=0.05,
                            ),
                            "evidence": ["dashboard operator correction"],
                        }
                    feedback_submitted = st.form_submit_button("피드백 기록", type="primary")
                if feedback_submitted:
                    execution = _execution_for_task(model["executions"], task_id)
                    execution_key = execution.get("execution_key") if execution else None
                    if feedback_type in {"execution_defect", "result_correction"} and not execution_key:
                        st.error("이 유형은 연결할 실행 기록이 필요합니다.", icon=":material/error:")
                    elif not feedback_reason.strip():
                        st.error("피드백 사유가 필요합니다.", icon=":material/error:")
                    else:
                        code = submit_feedback(
                            task_id,
                            feedback_type,
                            actor=_PERSONAL_ACTOR,
                            reason=feedback_reason,
                            root=runtime_root,
                            execution_key=str(execution_key) if execution_key else None,
                            details=feedback_details or None,
                        )
                        if code == 0:
                            st.success("피드백이 기록되었습니다. 후보 검토에서 다시 재생할 수 있습니다.")
                        else:
                            st.error("피드백 기록에 실패했습니다.", icon=":material/error:")
                        st.rerun()

            assessment = detail.get("assessment") or {}
            decision = detail.get("decision") or {}
            recipe = assessment.get("recipe")
            if decision.get("action") == "approve" and recipe:
                with st.container(border=True):
                    st.subheader("승인된 자동화", icon=":material/play_circle:")
                    st.caption(
                        f"Recipe `{recipe}`가 승인되었습니다. 실행은 이 버튼을 누를 때 한 번 시작됩니다."
                    )
                    execution = _execution_for_task(model["executions"], task_id)
                    if execution is not None:
                        st.info(
                            f"실행 상태: {execution.get('state', 'unknown')} · "
                            f"결과 버전: {execution.get('result_version', detail.get('result_version', 1))} · "
                            f"시도: {execution.get('attempt', detail.get('attempt', 1))}",
                            icon=":material/info:",
                        )
                        if execution.get("state") == "succeeded":
                            rebuild_counter_key = f"_rebuild-counter-{task_id}"
                            counter = int(st.session_state.get(rebuild_counter_key, 1))
                            session_nonce = st.session_state.setdefault(
                                f"_rebuild-session-{task_id}", uuid.uuid4().hex[:12]
                            )
                            rebuild_request_id = st.session_state.setdefault(
                                f"_rebuild-request-{task_id}",
                                f"dashboard-rebuild-{task_id}-{session_nonce}-{counter}",
                            )
                            if st.button("새 작성본 만들기", key=f"rebuild-{task_id}"):
                                code = submit_rebuild(task_id, str(recipe), rebuild_request_id, root=runtime_root)
                                if code == 0:
                                    st.session_state[rebuild_counter_key] = counter + 1
                                    st.session_state[f"_rebuild-request-{task_id}"] = (
                                        f"dashboard-rebuild-{task_id}-{session_nonce}-{counter + 1}"
                                    )
                                    st.success("새 결과 버전 생성을 Core에 요청했습니다.")
                                else:
                                    st.error("새 작성본 생성에 실패했습니다.", icon=":material/error:")
                                st.rerun()
                        handoff = (detail.get("verification") or {}).get("handoff") or {}
                        if execution.get("state") == "executing":
                            recovery_reason = st.text_input(
                                "중단 확인 사유",
                                value="실행 중단을 확인해 실패로 복구합니다.",
                                key=f"recovery-reason-{task_id}",
                            )
                            if st.button(
                                "중단 실행을 실패로 복구",
                                key=f"recover-{task_id}",
                            ):
                                code = submit_recovery(
                                    str(execution.get("execution_key", "")),
                                    actor=_PERSONAL_ACTOR,
                                    reason=recovery_reason,
                                    root=runtime_root,
                                )
                                if code == 0:
                                    st.success("중단 실행을 실패 상태로 기록했습니다.")
                                else:
                                    st.error("중단 복구 기록에 실패했습니다.", icon=":material/error:")
                                st.rerun()
                    elif st.button(
                        "승인된 Recipe 실행",
                        key=f"execute-{task_id}",
                        type="primary",
                    ):
                        code = submit_execution(task_id, str(recipe), root=runtime_root)
                        if code == 0:
                            st.success("실행과 결과 검증이 완료되었습니다.", icon=":material/check_circle:")
                        else:
                            st.error("실행 또는 결과 검증에 실패했습니다.", icon=":material/error:")
                        st.rerun()
            _render_actual_completion(st, detail, model["executions"], runtime_root)

            st.subheader("결정 기록", icon=":material/edit_note:")
            st.caption("결정 사유는 감사용 이벤트로 저장되며 실행 명령이 아닙니다.")
            with st.form(f"decision-form-{task_id}", border=False):
                reason = st.text_input(
                    "결정 사유",
                    key=f"reason-{task_id}",
                    placeholder="예: 첨부 자료 부족으로 보류",
                )
                action_columns = st.columns(len(DECISION_ACTIONS), gap="small")
                submitted_action = None
                for column, action in zip(action_columns, DECISION_ACTIONS):
                    with column:
                        if st.form_submit_button(
                            _DECISION_LABELS[action],
                            help=_DECISION_HELP[action],
                            type="primary" if action == "approve" else "secondary",
                            width="stretch",
                        ):
                            submitted_action = action
            if submitted_action is None:
                return
            if not reason.strip():
                st.error("결정 사유가 필요합니다.", icon=":material/error:")
                return
            details = {"note": reason} if submitted_action == "modify" else None
            code = submit_decision(
                task_id,
                submitted_action,
                actor=_PERSONAL_ACTOR,
                reason=reason,
                version=int(detail.get("version", 1)),
                root=runtime_root,
                details=details,
                recipe=str(recipe) if submitted_action == "approve" and recipe else None,
            )
            label = _DECISION_LABELS[submitted_action]
            if code == 0:
                next_task_id = _next_task_id(filtered_tasks, task_id)
                st.session_state["selected_task_id"] = next_task_id
                st.session_state["_decision_notice"] = (
                    f"{label} 기록됨. "
                    + (
                        "다음 업무로 이동합니다."
                        if next_task_id is not None
                        else "필터 결과의 마지막 업무입니다."
                    )
                )
                st.rerun()
            else:
                st.error(f"{label} 거절됨 — Core 계약이 거부했습니다.", icon=":material/error:")


def _render_execution(st, model: dict[str, Any]) -> None:
    st.header("실행 기록", icon=":material/terminal:")
    st.caption("사람이 승인한 Recipe만 실행 대기 또는 실행 결과로 표시됩니다.")
    executions = model["executions"]
    pending_execution = []
    for detail in model["tasks"]:
        decision = detail.get("decision") or {}
        assessment = detail.get("assessment") or {}
        if (
            decision.get("action") == "approve"
            and assessment.get("recipe")
            and _execution_for_task(executions, str(detail.get("task_id"))) is None
        ):
            pending_execution.append(
                {
                    "업무명": detail.get("display_name") or detail.get("summary", ""),
                    "도메인": detail.get("domain_label", "기타 업무"),
                    "Recipe": assessment["recipe"],
                    "상태": "실행 대기",
                }
            )
    if pending_execution:
        with st.container(border=True):
            st.subheader("승인 후 실행 대기", icon=":material/hourglass_top:")
            _show_table(st, pending_execution, "")
            st.caption("업무 검토 화면에서 승인된 업무를 선택하면 실행 버튼이 나타납니다.")
    if executions:
        state_counts: dict[str, int] = {}
        for execution in executions:
            state = str(execution.get("state", "unknown"))
            state_counts[state] = state_counts.get(state, 0) + 1
        columns = st.columns(min(4, len(state_counts)), gap="small")
        for column, (state, count) in zip(columns, sorted(state_counts.items())):
            with column:
                st.metric(state, count, border=True)
        with st.container(border=True):
            st.subheader("실행 상태 이력")
            _show_table(st, executions, "")
        execution_events = [
            {
                "event_id": event.get("event_id", ""),
                "task_id": event.get("task_id", ""),
                "상태": event.get("state", ""),
            }
            for event in model["events"]
            if event.get("type") == "execution" or event.get("stage") == "execution"
        ]
        if execution_events:
            with st.container(border=True):
                st.subheader("실행 단계 이력", icon=":material/timeline:")
                _show_table(st, execution_events, "")
    elif not pending_execution:
        _show_empty_state(st, "실행 기록이 없습니다", "승인된 실행 Recipe가 아직 없습니다.", "history")


def _render_intake(st, model: dict[str, Any]) -> None:
    st.header("수집 현황", icon=":material/inbox:")
    st.caption("파일 개수가 아니라 수집된 업무와 현재 파이프라인 단계를 보여줍니다.")
    columns = st.columns(2, gap="large")
    with columns[0]:
        with st.container(border=True):
            st.subheader("수집 대기 Bundle", icon=":material/download:")
            bundles = model["bundles"]
            st.caption(f"{len(bundles)}개 Bundle · 수집 전 기술 큐")
            if bundles:
                _show_table(
                    st,
                    [
                        {
                            "Bundle": row.get("bundle", ""),
                            "capture_id": row.get("capture_id", ""),
                            "첨부": row.get("attachments", ""),
                            "상태": row.get("state", ""),
                        }
                        for row in bundles
                    ],
                    "",
                )
            else:
                st.caption("Downloads inbox가 비어 있습니다.")
    with columns[1]:
        with st.container(border=True):
            st.subheader("수집된 업무", icon=":material/archive:")
            inbox = model["inbox"]
            unique_tasks = {row.get("task_id") for row in inbox if row.get("task_id")}
            st.caption(f"{len(unique_tasks)}개 업무 · {len(inbox)}개 수집 사본")
            intake_rows = [
                {
                    "업무명": row.get("업무명", ""),
                    "업무 ID": row.get("task_id", ""),
                    "출처": row.get("출처", ""),
                    "접수 시각": row.get("received_at", ""),
                    "첨부": row.get("attachments", 0),
                    "상태": row.get("state", ""),
                }
                for row in inbox
            ]
            if intake_rows:
                _show_table(st, intake_rows, "")
            else:
                st.caption("Core Inbox가 비어 있습니다.")


def _render_recipe_authoring(st, model: dict[str, Any], runtime_root: Path) -> None:
    """Scoped Recipe lifecycle screen; all writes cross the recipectl boundary."""
    st.header("Recipe 제작", icon=":material/build:")
    st.caption("Core가 보유한 업무와 Recipe 상태를 조회하고, draft → 시험 → 검토 → 활성 순서로 진행합니다.")
    notice = st.session_state.pop("_recipe_notice", None)
    if notice:
        st.success(notice, icon=":material/check_circle:")
    task_scopes = {
        str(detail.get("scope"))
        for detail in model.get("tasks", [])
        if detail.get("scope")
    }
    draft_rows = _recipe_rows(None, runtime_root)
    scopes = sorted(task_scopes | {str(row.get("scope")) for row in draft_rows if row.get("scope")})
    if not scopes:
        scopes = ["owner"]
    scope = st.selectbox("Recipe scope", scopes, key="recipe-scope")
    scoped_rows = _recipe_rows(scope, runtime_root)
    rows_by_id = {str(row["recipe_id"]): row for row in scoped_rows}
    selection_key = f"recipe-selection-{scope}"
    pending_recipe_id = st.session_state.pop("_recipe_select_id", None)
    if pending_recipe_id in rows_by_id:
        st.session_state[selection_key] = pending_recipe_id

    def format_recipe_selection(value: str) -> str:
        return value or "새 draft"

    selected = st.selectbox(
        "기존 draft",
        ["", *rows_by_id],
        key=selection_key,
        format_func=format_recipe_selection,
    )
    existing = None
    if selected:
        recipe_id = selected
        try:
            from automation.recipe_onboarding import get_draft

            existing = get_draft(recipe_id, scope=scope, root=runtime_root)
        except Exception as exc:
            st.error(f"draft 조회 실패: {exc}")
    if existing:
        st.caption(
            f"현재 lifecycle: v{existing.get('version', 1)} · "
            f"{existing.get('status', 'draft')}"
        )
    defaults = existing or {}
    with st.form(f"recipe-definition-{scope}-{selected}", border=False):
        recipe_id = st.text_input("Recipe ID", value=str(defaults.get("recipe_id", "")))
        task_name = st.text_input("업무 이름", value=str(defaults.get("task_name", "")))
        case_reference = st.text_input("사례 참조", value=str(defaults.get("case_reference", "")))
        description = st.text_area("선언 설명", value=str(defaults.get("description", "")))
        executor = st.selectbox(
            "Executor capability",
            ("excel-table", "code-analysis", "restarea-converter"),
            index=max(0, ("excel-table", "code-analysis", "restarea-converter").index(
                str(defaults.get("executor", "excel-table"))
            )) if str(defaults.get("executor", "excel-table")) in {
                "excel-table", "code-analysis", "restarea-converter"
            } else 0,
        )
        input_type = st.text_input("입력 형식", value=str(defaults.get("input_type", "xlsx")))
        output_type = st.text_input("출력 형식", value=str(defaults.get("output_type", "xlsx")))
        steps_text = st.text_area(
            "단계·operation·parameters (JSON)",
            value=json.dumps(defaults.get("steps", []), ensure_ascii=False, indent=2),
        )
        applicability_text = st.text_area(
            "적용 조건 (JSON)",
            value=json.dumps(defaults.get("applicability", {}), ensure_ascii=False, indent=2),
        )
        verification_text = st.text_area(
            "검증 정책 (JSON)",
            value=json.dumps(defaults.get("verification", {}), ensure_ascii=False, indent=2),
        )
        error_policy_text = st.text_area(
            "오류 정책 (JSON)",
            value=json.dumps(defaults.get("error_policy", {}), ensure_ascii=False, indent=2),
        )
        ai_policy_text = st.text_area(
            "AI 정책 (JSON)",
            value=json.dumps(defaults.get("ai_policy", {"enabled": False, "expected_calls": 0, "max_calls": 0}), ensure_ascii=False, indent=2),
        )
        required_text = st.text_input(
            "필수 조건 (쉼표 구분)",
            value=", ".join(defaults.get("required_conditions", [])),
        )
        exclusion_text = st.text_input(
            "제외 조건 (쉼표 구분)",
            value=", ".join(defaults.get("exclusion_conditions", [])),
        )
        success_text = st.text_input(
            "성공 확인 (쉼표 구분)",
            value=", ".join(defaults.get("success_checks", [])),
        )
        declaration_submit = st.form_submit_button(
            "현재 draft 수정" if existing else "새 draft 작성",
            type="primary",
        )
    if declaration_submit:
        try:
            payload = {
                "recipe_id": recipe_id,
                "task_name": task_name,
                "case_reference": case_reference,
                "description": description,
                "scope": scope,
                "executor": executor,
                "input_type": input_type,
                "output_type": output_type,
                "steps": json.loads(steps_text),
                "applicability": json.loads(applicability_text),
                "verification": json.loads(verification_text),
                "error_policy": json.loads(error_policy_text),
                "ai_policy": json.loads(ai_policy_text),
                "required_conditions": [item.strip() for item in required_text.split(",") if item.strip()],
                "exclusion_conditions": [item.strip() for item in exclusion_text.split(",") if item.strip()],
                "success_checks": [item.strip() for item in success_text.split(",") if item.strip()],
                "approval_scope": "local_artifact_only",
            }
            code = (
                submit_recipe_update(recipe_id, payload, scope=scope, root=runtime_root)
                if existing
                else submit_recipe_create(payload, root=runtime_root)
            )
            if code == 0:
                st.session_state["_recipe_notice"] = "Recipe 선언이 Core에 저장되었습니다."
                st.session_state["_recipe_select_id"] = recipe_id
                st.rerun()
            else:
                st.error("Recipe 선언 저장이 거부되었습니다.", icon=":material/error:")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            st.error(f"선언 입력 오류: {exc}", icon=":material/error:")
    if existing:
        recipe_id = str(existing["recipe_id"])
        status = str(existing.get("status", "draft"))
        st.subheader("시험·검토·활성", icon=":material/verified:")
        task_options = [
            str(detail["task_id"])
            for detail in model.get("tasks", [])
            if detail.get("scope") == scope
        ]
        if task_options:
            test_task_id = st.selectbox("시험할 Core task", task_options, key=f"recipe-test-task-{scope}-{recipe_id}")
            if st.button("Core task로 시험", key=f"recipe-test-{scope}-{recipe_id}"):
                code = submit_recipe_test(recipe_id, scope=scope, root=runtime_root, task_id=test_task_id)
                if code == 0:
                    st.session_state["_recipe_notice"] = (
                        "시험 passed. 동일 요청 재전달은 새 session을 만들지 않습니다."
                    )
                    st.rerun()
                else:
                    st.error("시험 failed 또는 unsupported capability입니다.", icon=":material/error:")
            if status == "test_failed" and st.button(
                "실패 시험 명시적 재시험",
                key=f"recipe-retry-{scope}-{recipe_id}",
            ):
                code = submit_recipe_test(
                    recipe_id,
                    scope=scope,
                    root=runtime_root,
                    task_id=test_task_id,
                    retry=True,
                )
                if code == 0:
                    st.session_state["_recipe_notice"] = "재시험 passed."
                    st.rerun()
                else:
                    st.error("재시험이 실패했습니다.", icon=":material/error:")
        else:
            st.info("같은 scope의 Core task가 없어 시험할 수 없습니다.")
        if status in {"tested", "reviewed", "active"}:
            review_reason = st.text_input("검토 사유", key=f"recipe-review-reason-{scope}-{recipe_id}")
            if st.button("검토 기록", key=f"recipe-review-{scope}-{recipe_id}"):
                if not review_reason.strip():
                    st.error("검토 사유가 필요합니다.", icon=":material/error:")
                else:
                    code = submit_recipe_review(
                        recipe_id,
                        scope=scope,
                        actor=_PERSONAL_ACTOR,
                        reason=review_reason,
                        root=runtime_root,
                    )
                    if code == 0:
                        st.session_state["_recipe_notice"] = "검토가 Core에 기록되었습니다."
                        st.rerun()
                    else:
                        st.error("passed 시험이 없어 검토할 수 없습니다.", icon=":material/error:")
        if status in {"reviewed", "active"}:
            activation_reason = st.text_input("활성화 사유", key=f"recipe-activate-reason-{scope}-{recipe_id}")
            if st.button("Recipe 활성화", key=f"recipe-activate-{scope}-{recipe_id}", type="primary"):
                if not activation_reason.strip():
                    st.error("활성화 사유가 필요합니다.", icon=":material/error:")
                else:
                    code = submit_recipe_activate(
                        recipe_id,
                        scope=scope,
                        actor=_PERSONAL_ACTOR,
                        reason=activation_reason,
                        root=runtime_root,
                    )
                    if code == 0:
                        st.session_state["_recipe_notice"] = "Recipe active pointer가 갱신되었습니다."
                        st.rerun()
                    else:
                        st.error("검토되지 않았거나 stale capability입니다.", icon=":material/error:")


def render(root: Path | str = ".") -> None:  # pragma: no cover - presentation
    """Render the read-only end-to-end pipeline surface."""
    import streamlit as st

    st.set_page_config(
        page_title="유지보수 파이프라인",
        page_icon=":material/space_dashboard:",
        layout="wide",
        initial_sidebar_state="auto",
    )
    _inject_theme_css(st)

    initial_root = _default_runtime_root(root)
    default_downloads = os.environ.get(
        "UPMU_DASHBOARD_DOWNLOADS",
        str(_default_downloads_root()),
    )
    default_inbox = os.environ.get(
        "UPMU_DASHBOARD_INBOX",
        str(_default_inbox_root(initial_root)),
    )
    with st.sidebar:
        st.markdown("**UPMU / Core**")
        st.caption("유지보수 업무 검토 콘솔")
        with st.expander("Runtime 설정", expanded=True, icon=":material/settings:"):
            with st.form("runtime-settings", border=False):
                runtime_text = st.text_input("Core runtime root", value=str(initial_root))
                downloads_text = st.text_input(
                    "Extension Downloads inbox",
                    value=default_downloads,
                )
                inbox_text = st.text_input("Core Inbox", value=default_inbox)
                st.form_submit_button("경로 적용", type="secondary", width="stretch")
        if st.button("새로고침", type="primary", width="stretch", icon=":material/refresh:"):
            st.rerun()

    runtime_root = Path(runtime_text).expanduser()
    model = load_pipeline_model(
        root=runtime_root,
        downloads_root=Path(downloads_text).expanduser(),
        inbox_root=Path(inbox_text).expanduser(),
    )

    st.title("유지보수 파이프라인", icon=":material/space_dashboard:")
    st.caption(
        "Extension Bundle → Core Inbox → Hermes/Rule → Review → Approval → Execution. "
        "승인된 Recipe 실행과 결과 검증은 Core를 통해 수행하며 관리자 페이지 업로드는 사람의 확인이 필요합니다."
    )
    view = st.segmented_control(
        "주요 화면",
        ["파이프라인", "업무 검토", "실행 기록", "수집 현황", "Recipe 제작"],
        default="파이프라인",
        key="dashboard-view",
        label_visibility="collapsed",
    )
    view = view or st.session_state.get("dashboard-view") or "파이프라인"
    if view == "업무 검토":
        _render_review(st, model, runtime_root)
    elif view == "실행 기록":
        _render_execution(st, model)
    elif view == "수집 현황":
        _render_intake(st, model)
    elif view == "Recipe 제작":
        _render_recipe_authoring(st, model, runtime_root)
    else:
        _render_overview(st, model)


if __name__ == "__main__":  # pragma: no cover - presentation
    render()
