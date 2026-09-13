"""Reusable Core execution lifecycle for CLI, Dashboard, and other callers."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automation import dispatch as dispatch_core
from automation.decisions import Decision
from automation.dispatch import DispatchError, canonical_input_hash, dispatch_recipe, validate_recipe_input
from automation.events import DuplicateEventError, EventLog
from automation.execution import ExecutionRequest, ExecutionStateMachine
from automation.execution_store import (
    ExecutionNotStoredError,
    get_execution,
    list_execution_keys,
    next_attempt,
    put_execution,
    reserve_execution,
    reserve_result_version,
)
from automation.recipe_onboarding import load_active_metadata, select_recipe
from automation.recipes import get_recipe
from automation.task_store import get_task
from automation.locking import file_lock
from automation.verification import verify_code_analysis_result, verify_recipe_output


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _events(root: Path) -> EventLog:
    return EventLog(root / "state" / "events.jsonl")

def _notify_event(root: Path, payload: dict[str, Any]) -> None:
    """Forward persisted execution events to local notification profiles.

    Notifications are an adapter: a delivery failure must never change the
    Core execution outcome.
    """
    try:
        from automation.notifications import notify_pipeline_event

        notify_pipeline_event(root, payload)
    except Exception:
        pass



def _decision_lock(root: Path, task_id: str) -> Path:
    safe = task_id if _SAFE_ID.fullmatch(task_id) else hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return root / "state" / "decisions" / f"{safe}.decision.lock"


def _record_execution_claim(root: Path, task: Any, *, execution_key: str, approval_event_id: str) -> None:
    try:
        _events(root).append(
            {
                "event_id": f"execution-claim-{execution_key}-{approval_event_id}",
                "type": "execution_claim",
                "task_id": task.task_id,
                "execution_claim": {
                    "execution_key": execution_key,
                    "approval_event_id": approval_event_id,
                    "task_version": task.revision,
                },
            }
        )
    except DuplicateEventError:
        pass


def _approved(root: Path, task: Any) -> tuple[str, Decision] | None:
    latest: tuple[str, Decision] | None = None
    for event in _events(root).read():
        if event.get("type") != "decision" or event.get("task_id") != task.task_id:
            continue
        try:
            decision = Decision.from_dict(event["decision"], current_task_version=task.revision)
        except (KeyError, TypeError, ValueError):
            continue
        if decision.task_id == task.task_id:
            latest = (event["event_id"], decision)
    if latest is not None and latest[1].action == "approve":
        return latest
    return None


def record_decision(
    task_id: str,
    action: str,
    *,
    actor: str,
    reason: str,
    root: Path | str = ".",
    task_version: int | None = None,
    timestamp: str | None = None,
    details: dict[str, Any] | None = None,
    recipe_id: str | None = None,
) -> Decision:
    """Record an approval/decision and its optional execution binding."""
    root = Path(root)
    task = get_task(task_id, root=root)
    payload: dict[str, Any] = {
        "actor": actor,
        "task_id": task_id,
        "task_version": task.revision if task_version is None else task_version,
        "action": action,
        "reason": reason,
        "timestamp": timestamp or _timestamp(),
    }
    if details is not None:
        payload["details"] = details
    if action == "approve" and recipe_id:
        metadata = load_active_metadata(recipe_id, scope=task.scope, root=root)
        if metadata is None:
            raise ValueError("approval blocked: active scoped recipe required")
        try:
            selection = select_recipe(recipe_id, task, root=root)
        except DispatchError as exc:
            raise ValueError(f"approval blocked: {exc}") from None
        if selection.get("state") != "satisfied":
            reasons = selection.get("reasons") or [
                "recipe applicability or input conditions are not satisfied"
            ]
            raise ValueError(f"approval blocked: {'; '.join(str(reason) for reason in reasons)}")
        try:
            recipe_definition = get_recipe(recipe_id, root=root, scope=task.scope)
            input_hash = canonical_input_hash(
                recipe_id, task, recipe_definition=recipe_definition, root=root
            )
            capability = dispatch_core.capability_binding(recipe_definition)
        except (DispatchError, TypeError, ValueError):
            raise ValueError("approval blocked: recipe input is not ready") from None
        payload["details"] = {
            "execution": {
                "recipe_id": recipe_id,
                "input_hash": input_hash,
                "task_version": task.revision,
                "content_digest": task.content_digest or task.computed_content_digest(),
                "definition_version": metadata["version"],
                "capability_version": metadata["capability"]["version"],
                "capability": capability,
                "policy_version": (task.provenance or {}).get("policy_version", "1"),
                "selection": {
                    "state": selection.get("state"),
                    "evidence": selection.get("evidence", []),
                    "reasons": selection.get("reasons", []),
                },
            }
        }
    decision = Decision.from_dict(payload, current_task_version=task.revision)
    if decision.task_id != task.task_id:
        raise ValueError("decision task_id does not match requested task")
    decision_dict = decision.to_dict()
    digest = hashlib.sha256(
        json.dumps(decision_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with file_lock(_decision_lock(root, task.task_id)):
        try:
            _events(root).append(
                {
                    "event_id": f"decision-{digest}",
                    "type": "decision",
                    "task_id": task.task_id,
                    "decision": decision_dict,
                }
            )
        except DuplicateEventError:
            pass
    return decision


def _record_execution(root: Path, request: ExecutionRequest, **extra: Any) -> None:
    payload = {
        "event_id": f"execution-{request.execution_key}-attempt-{request.attempt}-{request.state}",
        "type": "execution",
        "task_id": request.task_id,
        "state": request.state,
        "execution": request.to_dict(),
        **extra,
    }
    try:
        _events(root).append(payload)
    except DuplicateEventError:
        pass
    _notify_event(root, payload)

def _record_verification(root: Path, request: ExecutionRequest, verification: dict[str, Any]) -> None:
    state = {"failed": "verification_failed", "unverifiable": "verification_unverifiable"}.get(
        verification.get("status"), "verification_recorded"
    )
    payload = {
        "event_id": f"verification-{request.execution_key}-attempt-{request.attempt}",
        "type": "verification",
        "task_id": request.task_id,
        "stage": "execution",
        "state": state,
        "execution": request.to_dict(),
        "verification": verification,
    }
    try:
        _events(root).append(payload)
    except DuplicateEventError:

        pass
    _notify_event(root, payload)
def _quarantine(root: Path, request: ExecutionRequest, artifact_dir: Path) -> str | None:
    if not artifact_dir.exists():
        return None
    if not _SAFE_ID.fullmatch(request.task_id) or artifact_dir.is_symlink():
        raise ValueError("unsafe artifact quarantine identity")
    root = root.resolve()
    try:
        artifact_dir.resolve().relative_to(
            root / "artifacts" / request.task_id / f"result-{request.result_version}"
        )
    except ValueError as exc:
        raise ValueError("artifact directory is outside the result root") from exc
    target = root / "artifacts" / "quarantine" / request.task_id / f"result-{request.result_version}" / f"attempt-{request.attempt}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise ValueError("artifact quarantine destination already exists")
    os.replace(artifact_dir, target)
    return f"quarantine/{request.task_id}/result-{request.result_version}/attempt-{request.attempt}"


def _normalize_artifacts(root: Path, artifact_dir: Path, artifacts: Any) -> tuple[list[dict[str, Any]], Path | None]:
    if not isinstance(artifacts, list):
        return [], None
    expected = artifact_dir.resolve()
    root = root.resolve()
    normalized: list[dict[str, Any]] = []
    first: Path | None = None
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("path")
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = expected / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(expected)
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            raise ValueError("artifact path is outside the execution result directory") from None
        if not candidate.is_file():
            raise ValueError("execution artifact is missing")
        record = dict(item)
        record["path"] = relative
        normalized.append(record)
        if first is None:
            first = candidate
    return normalized, first


def execute_task(
    task_id: str,
    recipe_id: str,
    *,
    root: Path | str = ".",
    input_hash: str | None = None,
    rebuild_request_id: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Execute one approved task; all lifecycle authority lives here."""
    root = Path(root)
    task = get_task(task_id, root=root)
    approved = _approved(root, task)
    if approved is None:
        raise ValueError("execute blocked: task has no recorded approval")
    event_id, decision = approved
    recipe_definition = get_recipe(recipe_id, root=root, scope=task.scope)
    metadata = load_active_metadata(recipe_id, scope=task.scope, root=root)
    if metadata is None:
        raise ValueError("execute blocked: active scoped recipe required")
    try:
        selection = select_recipe(recipe_id, task, root=root)
    except DispatchError as exc:
        raise ValueError(f"execute blocked: {exc}") from None
    if selection.get("state") != "satisfied":
        reasons = selection.get("reasons") or ["recipe applicability or input conditions are not satisfied"]
        raise ValueError(f"execute blocked: {'; '.join(str(reason) for reason in reasons)}")
    try:
        resolved_hash = canonical_input_hash(recipe_id, task, recipe_definition=recipe_definition, root=root)
    except (DispatchError, TypeError, ValueError):
        raise ValueError("execute blocked: recipe input is not ready") from None
    if input_hash is not None and input_hash != resolved_hash:
        raise ValueError("execute blocked: --input-hash does not match the attachment contents")
    binding = decision.details.get("execution") if decision.details is not None else None
    if not isinstance(binding, Mapping):
        raise ValueError("execute blocked: approval is not bound to a recipe and input")
    capability = dispatch_core.capability_binding(recipe_definition)
    policy_version = (task.provenance or {}).get("policy_version", "1")
    content_digest = task.content_digest or task.computed_content_digest()
    if (
        binding.get("recipe_id") != recipe_id
        or binding.get("input_hash") != resolved_hash
        or binding.get("task_version") != task.revision
        or binding.get("content_digest") != content_digest
        or binding.get("definition_version") != metadata["version"]
        or binding.get("capability_version") != metadata["capability"]["version"]
        or binding.get("capability") != capability
        or binding.get("policy_version") != policy_version
    ):
        raise ValueError("execute blocked: approval does not match the current recipe, capability, policy, definition, task version, and input")
    try:
        input_check = validate_recipe_input(recipe_id, task, recipe_definition=recipe_definition, root=root)
    except DispatchError as exc:
        raise ValueError(f"execute blocked: {exc}") from None
    if input_check.get("state") != "satisfied":
        reasons = input_check.get("reasons") or ["required input is not ready"]
        raise ValueError(f"execute blocked: {'; '.join(str(reason) for reason in reasons)}")
    latest_approval = _approved(root, task)
    if latest_approval is None or latest_approval[0] != event_id:
        raise ValueError("execute blocked: approval was revoked or superseded")
    if rebuild_request_id is not None:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("rebuild actor is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rebuild reason is required")
        initial_probe = ExecutionRequest(task.task_id, recipe_id, resolved_hash)
        try:
            initial = get_execution(initial_probe.execution_key, root=root)
        except ExecutionNotStoredError:
            raise ValueError("rebuild blocked: initial execution must succeed first") from None
        if initial.state != "succeeded" or initial.result_version != 1:
            raise ValueError("rebuild blocked: initial execution must succeed first")
        reserve_result_version(task.task_id, execution_key=initial_probe.execution_key, root=root)
    probe = ExecutionRequest(task.task_id, recipe_id, resolved_hash, result_version=2 if rebuild_request_id else 1, rebuild_request_id=rebuild_request_id)
    result_version, _ = reserve_result_version(task.task_id, rebuild_request_id=rebuild_request_id, execution_key=probe.execution_key, root=root)
    try:
        existing = get_execution(probe.execution_key, root=root)
    except ExecutionNotStoredError:
        existing = None
    if existing is not None and existing.result_version != result_version:
        raise ValueError("execution result version does not match its reservation")
    if existing is not None and existing.state != "failed":
        if rebuild_request_id is not None:
            return {"task_id": task.task_id, "execution_key": existing.execution_key, "result_version": existing.result_version, "attempt": existing.attempt, "rebuild_request_id": existing.rebuild_request_id, "state": "already_reserved"}
        raise ValueError(f"execution key already reserved: {existing.execution_key}")
    reserved = list_execution_keys(root=root)
    attempt = next_attempt(probe.execution_key, root=root)
    machine = ExecutionStateMachine(existing_execution_keys=reserved)
    request = machine.request(task.task_id, recipe_id, resolved_hash, attempt=attempt, result_version=result_version, rebuild_request_id=rebuild_request_id)
    reserve_execution(request, root=root)
    _record_execution(root, request, **({"rebuild_request": {"request_id": rebuild_request_id, "actor": actor, "reason_recorded": True}} if rebuild_request_id else {}))
    request = machine.transition(request.execution_key, "approved", approval=f"event:{event_id}")
    put_execution(request, root=root)
    request = machine.transition(request.execution_key, "executing")
    put_execution(request, root=root)
    _record_execution(root, request)
    try:
        dispatch_core.validate_ai_execution_policy(recipe_definition)
    except dispatch_core.AIPolicyError as exc:
        request = machine.transition(request.execution_key, "failed")
        put_execution(request, root=root)
        _record_execution(root, request, failure={"stage": "ai_policy", "code": exc.code, "details": dict(exc.details)})
        raise ValueError(f"execute blocked: {exc.code}: {exc}") from None
    with file_lock(_decision_lock(root, task.task_id)):
        latest_approval = _approved(root, task)
        if latest_approval is None or latest_approval[0] != event_id:
            failed = machine.transition(request.execution_key, "failed")
            put_execution(failed, root=root)
            _record_execution(root, failed, failure={"stage": "approval"})
            raise ValueError("execute blocked: approval was revoked or superseded")
        _record_execution_claim(
            root,
            task,
            execution_key=request.execution_key,
            approval_event_id=event_id,
        )
    artifact_dir = root / "artifacts" / task.task_id / f"result-{result_version}" / f"attempt-{attempt}"
    try:
        result = dispatch_recipe(recipe_id, task, artifact_dir, seen_execution_keys=tuple(key for key in reserved if key != request.execution_key), recipe_definition=recipe_definition, root=root, execution_key=request.execution_key)
        artifacts, artifact_path = _normalize_artifacts(root, artifact_dir, result.get("artifacts") or [])
    except DispatchError as exc:
        quarantine = _quarantine(root, request, artifact_dir)
        failed = machine.transition(request.execution_key, "failed")
        put_execution(failed, root=root)
        _record_execution(root, failed, quarantine=quarantine)
        raise ValueError(f"execute failed: {exc}") from None
    except ValueError as exc:
        quarantine = _quarantine(root, request, artifact_dir)
        failed = machine.transition(request.execution_key, "failed")
        put_execution(failed, root=root)
        _record_execution(root, failed, quarantine=quarantine)
        raise ValueError(f"execute failed: {exc}") from None
    try:
        if recipe_definition.executor == "code-analysis" and artifact_path is None:
            verification = verify_code_analysis_result(result.get("plan"))
        elif artifact_path is not None:
            verification = verify_recipe_output(
                recipe_id, task, artifact_path, recipe_definition=recipe_definition
            )
        else:
            verification = {
                "verification_version": "artifact-v1",
                "status": "unverifiable",
                "checks": [],
                "handoff": {"required": True, "status": "pending"},
            }
    except Exception as exc:
        verification = {
            "verification_version": "artifact-v1",
            "status": "unverifiable",
            "checks": [],
            "error": type(exc).__name__,
            "handoff": {"required": True, "status": "pending"},
        }
    if verification.get("status") != "passed":
        quarantine = _quarantine(root, request, artifact_dir)
        failed = machine.transition(request.execution_key, "failed")
        put_execution(failed, root=root)
        _record_execution(root, failed, quarantine=quarantine)
        _record_verification(root, failed, verification)
        raise ValueError(f"execute verification {verification.get('status', 'unverifiable')}")
    succeeded = machine.transition(request.execution_key, "succeeded")
    put_execution(succeeded, root=root)
    _record_execution(root, succeeded, artifacts=artifacts)
    _record_verification(root, succeeded, verification)
    return {"task_id": succeeded.task_id, "recipe_id": succeeded.recipe_id, "input_hash": succeeded.input_hash, "execution_key": succeeded.execution_key, "state": succeeded.state, "approval": succeeded.approval, "result_version": succeeded.result_version, "attempt": succeeded.attempt, "rebuild_request_id": succeeded.rebuild_request_id, "artifacts": artifacts, "preview": result.get("preview"), "changes": result.get("changes"), "plan": result.get("plan"), "report_requested": result.get("report_requested"), "verification": verification}


def confirm_task(task_id: str, *, execution_key: str | None = None, basis: str | None = None, task_version: int, actor: str, reason: str, root: Path | str = ".") -> dict[str, Any]:
    root = Path(root)
    task = get_task(task_id, root=root)
    if not isinstance(task_version, int) or isinstance(task_version, bool):
        raise ValueError("actual completion task_version must be an integer")
    if task_version != task.revision:
        raise ValueError(f"stale task version: task_version={task_version}, current_task_version={task.revision}")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actual completion actor is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("actual completion reason is required")
    basis = basis or ("executor" if execution_key is not None else None)
    if basis is None:
        raise ValueError("--basis is required when no --execution-key is given (manual or external)")
    if basis == "executor" and execution_key is None:
        raise ValueError("basis 'executor' requires --execution-key")
    if basis != "executor" and execution_key is not None:
        raise ValueError("a manual or external completion does not take an --execution-key")
    request = None
    latest = None
    if basis == "executor":
        request = get_execution(execution_key, root=root)
        if request.task_id != task_id:
            raise ValueError("execution key does not belong to task")
        if request.state != "succeeded":
            raise ValueError("only a succeeded execution can be confirmed")
        for event in _events(root).read():
            execution = event.get("execution")
            if event.get("type") == "verification" and isinstance(execution, Mapping) and execution.get("execution_key") == execution_key and execution.get("attempt", 1) == request.attempt and isinstance(event.get("verification"), Mapping):
                latest = dict(event["verification"])
        if latest is None or latest.get("status") != "passed":
            raise ValueError("a passed verification is required before confirmation")
    previous = None
    for event in _events(root).read():
        if event.get("type") == "actual_completion" and event.get("task_id") == task_id and isinstance(event.get("actual_completion"), Mapping):
            previous = dict(event["actual_completion"])
    if previous is not None:
        if previous.get("task_version") == task_version and previous.get("execution_key") == execution_key and previous.get("basis", "executor") == basis:
            return {"task_id": task_id, "execution_key": execution_key, "basis": basis, "verification": latest, "actual_completion": previous, "idempotent": True}
        raise ValueError("task is already actually completed")
    if latest is not None:
        handoff = dict(latest.get("handoff") or {})
        handoff.update({"required": True, "status": "confirmed", "operator": actor, "reason_recorded": True})
        latest["handoff"] = handoff
        try:
            _events(root).append({
                "event_id": f"verification-confirmed-{execution_key}-attempt-{request.attempt}",
                "type": "verification",
                "task_id": task_id,
                "state": "verification_recorded",
                "stage": "execution",
                "execution": request.to_dict(),
                "verification": latest,
                "confirmation": {"actor": actor, "reason_recorded": True, "timestamp": _timestamp()},
            })
        except DuplicateEventError:
            pass
    completion = {
        "task_id": task_id,
        "task_version": task_version,
        "basis": basis,
        "actor": actor,
        "reason_recorded": True,
        "timestamp": _timestamp(),
    }
    if execution_key is not None:
        completion["execution_key"] = execution_key
    payload = {"event_id": f"actual-completion-{task_id}:{task_version}:{execution_key or basis}", "type": "actual_completion", "task_id": task_id, "task_version": task_version, "state": "actual_completed", "stage": "completion", "actual_completion": completion}
    if request is not None:
        payload["execution_reference"] = {"task_id": request.task_id, "execution_key": request.execution_key}
    try:
        _events(root).append(payload)
    except DuplicateEventError:
        pass
    return {"task_id": task_id, "execution_key": execution_key, "basis": basis, "verification": latest, "actual_completion": completion, "idempotent": False}


def recover_execution(execution_key: str, *, actor: str, reason: str, root: Path | str = ".") -> dict[str, Any]:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("recovery actor is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("recovery reason is required")
    root = Path(root)
    request = get_execution(execution_key, root=root)
    if request.state != "executing":
        raise ValueError("only an executing request can be recovered")
    artifact_dir = root / "artifacts" / request.task_id / f"result-{request.result_version}" / f"attempt-{request.attempt}"
    quarantine = _quarantine(root, request, artifact_dir)
    failed = request.transition("failed")
    put_execution(failed, root=root)
    _record_execution(root, failed, quarantine=quarantine, recovery={"actor": actor, "reason_recorded": bool(reason.strip()), "next_action": "실패 원인을 확인한 뒤 새 승인으로 다시 판단하세요."})
    return {"execution": failed.to_dict(), "recovered": True}
