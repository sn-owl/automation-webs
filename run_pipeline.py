from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

from automation.adapters.registry import normalize_html
from automation.approval import requires_approval
from automation.hermes_sanitize import mask_for_hermes
from automation.evaluator import evaluate_with_rules
from automation.hermes_validator import (
    HermesValidationError,
    validate_hermes_assessment,
    validate_hermes_classification,
)
from automation.events import EventLog
from automation import identity
from automation.dispatch import DispatchError, validate_recipe_input
from automation.recipes import get_recipe, load_recipes
from automation.recipe_onboarding import select_active_recipe
from automation.store import RawStoreConflict, store_raw
from automation.router import route
from automation.rules import classify_with_rules
from automation.task_store import TaskNotFoundError, get_task, put_task
from automation.recognition import build_recognition_event
from automation.work_package import render_work_package


_FIXTURE_SOURCES = {
    "alpha-ready.html": ("gnuboard", {"board_id": "alpha", "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452"}),
    "beta-manual.html": ("gnuboard", {"board_id": "beta", "url": "https://fixture.local/bbs/board.php?bo_table=beta&wr_id=2048"}),
    "egov-developable.html": ("egov", {"url": "https://fixture.local/egov/board/view.sko?nttId=9001"}),
}


def _safe_error(stage: str, exc: BaseException) -> str:
    """Return an error suitable for state/event output, without exception text."""
    return f"{stage} failed ({type(exc).__name__})"

def _event_path(root: Path) -> Path:
    return root / "state" / "events.jsonl"


_EVENT_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def _event_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _EVENT_THREAD_LOCK:
        with lock_path.open("a+b") as lock:
            if _fcntl is not None:
                _fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                _msvcrt.locking(lock.fileno(), _msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if _fcntl is not None:
                    _fcntl.flock(lock.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:  # pragma: no cover - Windows
                    lock.seek(0)
                    _msvcrt.locking(lock.fileno(), _msvcrt.LK_UNLCK, 1)


def _record_event(
    root: Path,
    event_id: str,
    state: str,
    *,
    task_id: str | None = None,
    stage: str | None = None,
    **details: Any,
) -> None:
    if task_id is not None:
        try:
            current = get_task(task_id, root=root)
        except TaskNotFoundError:
            current = None
        if current is not None:
            details = {**details, "task_version": current.revision}
        if current is not None and current.contract_version >= 2:
            digest = current.computed_content_digest()
            event_id = f"{event_id}:{digest}"
            details = {**details, "content_digest": digest}
    payload: dict[str, Any] = {"event_id": event_id, "state": state, **details}
    if task_id is not None:
        payload["task_id"] = task_id
    if stage is not None:
        payload["stage"] = stage
    path = _event_path(root)
    with _event_lock(path):
        log = EventLog(path)
        existing = log.read()
        if any(prior["event_id"] == event_id for prior in existing):
            return
        log.append(payload)
    try:
        from automation.notifications import notify_pipeline_event

        notify_pipeline_event(root, payload)
    except Exception:
        # Notification is an adapter; it cannot turn a persisted Core event
        # into a pipeline failure.
        pass

def _raw_metadata(root: Path, task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    candidates = [
        root / "raw" / task_id / name
        for name in ("page.html", "input.json")
        if (root / "raw" / task_id / name).exists()
    ]
    if not candidates:
        return None
    path = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _result(
    *,
    root: Path,
    status: str,
    task_id: str | None = None,
    classification: Any = None,
    assessment: Any = None,
    route_name: str | None = None,
    package: Path | None = None,
    input_check: dict[str, Any] | None = None,
    classification_review: dict[str, Any] | None = None,
    assessment_review: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    try:
        events = EventLog(_event_path(root)).read()
    except Exception as exc:
        return {
            "status": "failed",
            "state": "corrupt_event_log",
            "task_id": task_id,
            "classification": None,
            "assessment": None,
            "input_check": None,
            "route": None,
            "work_package": None,
            "raw": _raw_metadata(root, task_id),
            "events": [],
            "error": _safe_error("event_log", exc),
        }
    result: dict[str, Any] = {
        "status": status,
        "state": status,
        "task_id": task_id,
        "classification": classification.to_dict() if classification is not None else None,
        "assessment": assessment.to_dict() if assessment is not None else None,
        "input_check": input_check,
        "route": route_name,
        "work_package": str(package) if package is not None else None,
        "raw": _raw_metadata(root, task_id),
        "events": events,
    }
    if classification_review is not None:
        result["classification_review"] = classification_review
    if assessment_review is not None:
        result["assessment_review"] = assessment_review
    if error is not None:
        result["error"] = error
    return result
def _with_state(result: dict[str, Any], state: str) -> dict[str, Any]:
    if result.get("state") == "corrupt_event_log":
        return result
    return result | {"state": state}


def _connection(scope: str | None, connector_id: str | None) -> dict[str, str]:
    """Return the collection identity the adapters stamp onto a WorkItem.

    Collection is per-connector by definition (제품 A), so scope and connector
    are inputs of collection, not something inferred from the page. Supplying
    them is what makes an adapter emit a contract_version 2 WorkItem; leaving
    them out keeps the legacy flat identity, because `identity.task_id`
    derives a different (hashed) task id once a connection is named.
    """
    if scope is None and connector_id is None:
        return {}
    if scope is None or connector_id is None:
        raise ValueError("scope and connector_id must be provided together")
    return {"scope": scope, "connector_id": connector_id}


def _source_for_html(
    input_path: Path,
    html: str,
    *,
    scope: str | None = None,
    connector_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    connection = _connection(scope, connector_id)
    known = _FIXTURE_SOURCES.get(input_path.name)
    if known is not None:
        board_type, source = known
        return board_type, {**source, **connection}

    if 'id="bo_v"' in html or "id='bo_v'" in html:
        board_id = input_path.stem.split("-", 1)[0].lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", board_id):
            board_id = "fixture"
        match = re.search(r"(?:bo_table=)([^&\"']+).*?(?:wr_id=)([^&\"']+)", html)
        external_id = match.group(2) if match else "1"
        return "gnuboard", {
            "board_id": board_id,
            "url": f"https://fixture.local/bbs/board.php?bo_table={board_id}&wr_id={external_id}",
            **connection,
        }
    if 'id="view"' in html or "id='view'" in html:
        return "egov", {
            "url": "https://fixture.local/egov/board/view.sko?nttId=1",
            **connection,
        }
    raise ValueError("unsupported HTML fixture")


def _hermes_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (OSError, TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "HermesClientError",
        "HermesTransportError",
    }


def _classification_review(reason_code: str) -> dict[str, Any]:
    reasons = {
        "ai_disabled": "AI 비활성으로 분류할 수 없습니다. 사용자 검토가 필요합니다.",
        "ai_call_failed": "AI 호출에 실패했습니다. 사용자 검토가 필요합니다.",
        "ai_response_invalid": "AI 응답 검증에 실패했습니다. 사용자 검토가 필요합니다.",
    }
    if reason_code not in reasons:
        raise ValueError(f"unknown classification review reason: {reason_code}")
    return {
        "decision": "DO NOT",
        "review_required": True,
        "reason_code": reason_code,
        "reason": reasons[reason_code],
        "next_action": "user_review",
    }


def _assessment_review(reason_code: str) -> dict[str, Any]:
    reasons = {
        "ai_disabled": "AI 비활성으로 자동화 가능성을 평가할 수 없습니다. 사용자 검토가 필요합니다.",
        "ai_call_failed": "AI 호출에 실패하여 자동화 가능성을 평가할 수 없습니다. 사용자 검토가 필요합니다.",
        "ai_response_invalid": "AI 응답 검증에 실패하여 자동화 가능성을 평가할 수 없습니다. 사용자 검토가 필요합니다.",
    }
    if reason_code not in reasons:
        raise ValueError(f"unknown assessment review reason: {reason_code}")
    return {
        "decision": "DO NOT",
        "review_required": True,
        "reason_code": reason_code,
        "reason": reasons[reason_code],
        "next_action": "user_review",
    }

def _anchor_local_attachment_refs(item: Any, base_dir: Path) -> Any:
    """Resolve bundle-relative attachment files before persisting a WorkItem."""
    bundle_root = Path(base_dir).resolve()
    anchored = []
    for attachment in item.attachments:
        raw_ref = str(attachment.raw_ref)
        if (
            re.match(r"\A[A-Za-z][A-Za-z0-9+.\-]*://", raw_ref)
            or raw_ref.startswith(("\\\\", "//"))
            or Path(raw_ref).is_absolute()
        ):
            anchored.append(attachment)
            continue

        candidate = (bundle_root / raw_ref).resolve()
        try:
            candidate.relative_to(bundle_root)
        except ValueError as exc:
            raise ValueError("attachment reference escapes input bundle") from exc
        anchored.append(replace(attachment, raw_ref=str(candidate)))

    return replace(item, attachments=tuple(anchored))


def _read_input(
    input_path: Path,
    raw: bytes | None = None,
    *,
    scope: str | None = None,
    connector_id: str | None = None,
) -> tuple[Any, str]:
    if (scope is None) != (connector_id is None):
        raise ValueError("scope and connector_id must be provided together")
    if raw is None:
        raw = input_path.read_bytes()
    text = raw.decode("utf-8")
    if input_path.suffix.casefold() == ".json" or text.lstrip().startswith("{"):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("WorkItem JSON must be an object")
        from automation.models import WorkItem

        item = WorkItem.from_dict(payload)
        if scope is not None:
            scoped_id = identity.task_id(
                item.source.id, item.source.external_id,
                scope=scope, connector_id=connector_id,
            )
            if item.contract_version >= 2:
                if item.scope != scope or item.connector_id != connector_id:
                    raise ValueError("WorkItem does not belong to the configured connection")
            else:
                if item.scope != "legacy" or item.connector_id != "legacy":
                    raise ValueError("legacy WorkItem contains a conflicting connection")
                item = replace(
                    item, task_id=scoped_id, contract_version=2,
                    scope=scope, connector_id=connector_id,
                    capture_id=item.capture_id or hashlib.sha256(raw).hexdigest(),
                    provenance={**(item.provenance or {}), "adapter": item.source.type},
                    mask_table_ref=f"local://masks/{scoped_id}.json",
                )
        return _anchor_local_attachment_refs(item, input_path.parent), "json"
    board_type, source = _source_for_html(
        input_path, text, scope=scope, connector_id=connector_id
    )
    return normalize_html(board_type, text, source), "html"

def _store_capture_raw(
    task_id: str, raw_name: str, input_bytes: bytes, *, root: Path
) -> tuple[Any, bool]:
    """Keep recaptures immutable without turning them into storage failures."""
    try:
        return store_raw(task_id, raw_name, input_bytes, root=root), False
    except RawStoreConflict:
        digest = hashlib.sha256(input_bytes).hexdigest()[:12]
        raw_path = Path(raw_name)
        versioned_name = f"{raw_path.stem}-{digest}{raw_path.suffix}"
        return store_raw(task_id, versioned_name, input_bytes, root=root), True


def _hermes_input(item: Any) -> dict[str, Any]:
    """Build the attachment-free projection with explicit role policy only."""
    from automation.recognition import build_model_projection

    policy_context = (item.provenance or {}).get("classification_context", {})
    return build_model_projection(item, policy_context)
def _prior_hermes_result(
    root: Path,
    task_id: str,
    content_digest: str,
    operation: str,
) -> tuple[str, Any, str | None] | None:
    """Recover one content-bound advisory result without crossing operations."""
    for event in reversed(EventLog(_event_path(root)).read()):
        if (
            event.get("task_id") != task_id
            or event.get("stage") != "hermes"
            or event.get("operation") != operation
            or event.get("content_digest") != content_digest
        ):
            continue
        if event.get("state") == "blocked":
            return "blocked", None, event.get("error")
        try:
            if operation == "classification" and event.get("state") == "classified":
                from automation.classification import Classification

                return "classified", Classification.from_dict(event["classification"]), None
            if operation == "assessment" and event.get("state") == "assessed":
                from automation.assessment import Assessment

                return "assessed", Assessment.from_dict(event["assessment"]), None
        except (KeyError, TypeError, ValueError):
            return None
    return None
def _prior_classification_review(
    root: Path, task_id: str, content_digest: str
) -> dict[str, Any] | None:
    for event in reversed(EventLog(_event_path(root)).read()):
        if (
            event.get("task_id") == task_id
            and event.get("stage") == "hermes"
            and event.get("operation") == "classification"
            and event.get("content_digest") == content_digest
            and isinstance(event.get("classification_review"), dict)
        ):
            return dict(event["classification_review"])
    return None

def _prior_assessment_review(
    root: Path, task_id: str, content_digest: str
) -> dict[str, Any] | None:
    for event in reversed(EventLog(_event_path(root)).read()):
        if (
            event.get("task_id") == task_id
            and event.get("stage") == "hermes"
            and event.get("operation") == "assessment"
            and event.get("content_digest") == content_digest
            and isinstance(event.get("assessment_review"), dict)
        ):
            return dict(event["assessment_review"])
    return None


def _select_active_assessment_recipe(item: Any, proposed_recipe: str, *, root: Path) -> tuple[Any, dict[str, Any]]:
    """Bind an assessment proposal to one active, applicable runtime recipe."""
    selection = select_active_recipe(item, root=root)
    if selection.get("state") != "selected":
        raise ValueError(
            "active recipe selection is not unique: "
            + str(selection.get("state", "unknown"))
        )
    selected = selection.get("selected") or {}
    if selected.get("recipe_id") != proposed_recipe:
        raise ValueError("assessment recipe is not the selected active recipe")
    recipe = get_recipe(proposed_recipe, root=root, scope=getattr(item, "scope", None))
    return recipe, selected


def _validate_assessment_recipe(
    item: Any,
    proposed_recipe: str | None,
    *,
    root: Path,
) -> tuple[dict[str, Any] | None, Any | None]:
    if proposed_recipe is None:
        return None, None
    try:
        recipe, selection = _select_active_assessment_recipe(item, proposed_recipe, root=root)
        input_check = validate_recipe_input(
            proposed_recipe,
            item,
            recipe_definition=recipe,
            root=root,
        )
    except Exception as exc:
        return {"state": "invalid", "reasons": [_safe_error("recipe", exc)], "evidence": []}, None
    return input_check, selection


def _record_recipe_block(
    root: Path,
    task_id: str,
    *,
    classification: Any,
    assessment: Any,
    input_check: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    _record_event(
        root,
        f"{task_id}:recipe_blocked",
        "blocked",
        task_id=task_id,
        stage="routing",
        input_check=input_check,
        recipe_reason=reason,
    )
    return _result(
        root=root,
        status="blocked",
        task_id=task_id,
        classification=classification,
        assessment=assessment,
        input_check=input_check,
        route_name="needs_recipe",
        error=reason,
    )


def run_pipeline(
    input_path: Path,
    *,
    output_root: Path,
    hermes_client=None,
    scope: str | None = None,
    connector_id: str | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(str(input_path.resolve()).encode("utf-8")).hexdigest()[:16]
    try:
        _record_event(root, f"input:{fingerprint}:discovered", "discovered", stage="input")
        input_bytes = input_path.read_bytes()
        item, input_kind = _read_input(
            input_path, input_bytes, scope=scope, connector_id=connector_id
        )
    except Exception as exc:
        try:
            _record_event(root, f"input:{fingerprint}:failed", "normalize_failed", stage="normalize")
        except Exception:
            pass
        return _with_state(_result(root=root, status="failed", error=_safe_error("normalize", exc)), "normalize_failed")

    task_id = item.task_id
    content_digest = item.content_digest or item.computed_content_digest()
    try:
        identity._normalize_identifier(task_id, "task_id")
        raw_name = "page.html" if input_kind == "html" else "input.json"
        raw_object, recaptured = _store_capture_raw(
            task_id, raw_name, input_bytes, root=root
        )
        raw_metadata = {
            "path": f"raw/{task_id}/{raw_object.name}",
            "sha256": raw_object.sha256,
            "size": raw_object.size,
        }
        try:
            existing = get_task(task_id, root=root)
        except TaskNotFoundError:
            existing = None
        if recaptured and existing is not None:
            if item.contract_version < 2:
                # v1 has no revision concept: a changed recapture stays a duplicate.
                _record_event(
                    root,
                    f"{task_id}:duplicate:{raw_object.sha256[:12]}",
                    "duplicate",
                    task_id=task_id,
                    stage="storage",
                    raw=raw_metadata,
                )
                return _result(root=root, status="duplicate", task_id=task_id)
            meaningful_change = existing.meaningful_payload() != item.meaningful_payload()
            put_task(item, root=root)
            item = get_task(task_id, root=root)
            if meaningful_change:
                # Preserve the new revision, then run the same recognition and
                # classification path. Event identities include semantic content.
                _record_event(
                    root,
                    f"{task_id}:revised:{raw_object.sha256[:12]}",
                    "revised",
                    task_id=task_id,
                    stage="storage",
                    raw=raw_metadata,
                )
        put_task(item, root=root)
        item = get_task(task_id, root=root)
        if (
            item.source_completion_observed
            and (existing is None or not existing.source_completion_observed)
        ):
            _record_event(
                root,
                f"{task_id}:source-completion:{raw_object.sha256[:12]}",
                "source_completion_observed",
                task_id=task_id,
                stage="source",
                source_completion_observed=True,
            )
        content_digest = item.computed_content_digest()
        _record_event(root, f"{task_id}:stored", "stored", task_id=task_id, stage="storage", raw=raw_metadata)
        _record_event(root, f"{task_id}:files_processed", "files_processed", task_id=task_id, stage="normalize")
    except Exception as exc:
        try:
            _record_event(root, f"{task_id}:storage_failed", "storage_failed", task_id=task_id, stage="storage")
        except Exception:
            pass
        return _with_state(_result(root=root, status="failed", task_id=task_id, error=_safe_error("storage", exc)), "storage_failed")

    # Partial captures are audit data, never actionable classification input.
    if item.completeness != "complete" or any(a.status != "complete" for a in item.attachments):
        try:
            _record_event(root, f"{task_id}:intake_blocked", "blocked", task_id=task_id, stage="intake")
        except Exception:
            pass
        return _with_state(
            _result(root=root, status="blocked", task_id=task_id, route_name="intake_blocked",
                    error="source capture is not complete"),
            "intake_blocked",
        )

    try:
        _record_event(root, f"{task_id}:recognized", "recognized", task_id=task_id, stage="recognition", recognition=build_recognition_event(item))
        classification = classify_with_rules(item, root=root)
    except Exception as exc:
        try:
            _record_event(root, f"{task_id}:classification_failed", "classification_failed", task_id=task_id, stage="classification")
        except Exception:
            pass
        return _with_state(_result(root=root, status="failed", task_id=task_id, error=_safe_error("classification", exc)), "classification_failed")
    if classification is not None:
        try:
            _record_event(
                root,
                f"{task_id}:classified",
                "classified",
                task_id=task_id,
                stage="classification",
                classification=classification.to_dict(),
            )
        except Exception as exc:
            try:
                _record_event(root, f"{task_id}:classification_failed", "classification_failed", task_id=task_id, stage="classification")
            except Exception:
                pass
            return _with_state(_result(root=root, status="failed", task_id=task_id, error=_safe_error("classification", exc)), "classification_failed")

    assessment = None
    input_check = None
    route_name = None
    package = None
    classification_source = "rules"
    assessment_source = "rules"
    assessment_review = None
    assessment_requested = bool(
        (item.provenance or {}).get("assessment_requested")
    )

    if classification is None:
        prior = _prior_hermes_result(
            root, task_id, content_digest, "classification"
        )
        if prior is not None and prior[0] == "blocked":
            review = _prior_classification_review(root, task_id, content_digest)
            return _result(
                root=root,
                status="blocked",
                task_id=task_id,
                route_name="needs_hermes",
                classification_review=review,
                error=(review or {}).get("reason") or prior[2],
            )
        if prior is not None:
            classification = prior[1]
            classification_source = "hermes"
        elif hermes_client is None:
            review = _classification_review("ai_disabled")
            _record_event(
                root,
                f"{task_id}:blocked:{content_digest[:12]}",
                "blocked",
                task_id=task_id,
                stage="hermes",
                operation="classification",
                content_digest=content_digest,
                classification_review=review,
                error=review["reason"],
            )
            return _result(
                root=root,
                status="blocked",
                task_id=task_id,
                route_name="needs_hermes",
                classification_review=review,
                error=review["reason"],
            )
        else:
            try:
                raw_response = hermes_client.classify(_hermes_input(item))
                classification = validate_hermes_classification(raw_response)
                classification_source = "hermes"
                _record_event(
                    root,
                    f"{task_id}:hermes_classified:{content_digest[:12]}",
                    "classified",
                    task_id=task_id,
                    stage="hermes",
                    operation="classification",
                    content_digest=content_digest,
                    classification=classification.to_dict(),
                )
            except Exception as exc:
                safe_error = _safe_error("hermes", exc)
                reason_code = (
                    "ai_response_invalid"
                    if isinstance(exc, HermesValidationError)
                    else "ai_call_failed"
                )
                review = _classification_review(reason_code)
                state = "failed" if _hermes_retryable(exc) else "blocked"
                _record_event(
                    root,
                    f"{task_id}:hermes_classification_{state}:{content_digest[:12]}",
                    state,
                    task_id=task_id,
                    stage="hermes",
                    operation="classification",
                    content_digest=content_digest,
                    classification_review=review,
                    error=safe_error,
                )
                return _result(
                    root=root,
                    status="blocked",
                    task_id=task_id,
                    route_name="needs_hermes",
                    classification_review=review,
                    error=review["reason"],
                )

    if assessment_requested:
        try:
            assessment = evaluate_with_rules(item, classification)
        except Exception as exc:
            _record_event(
                root,
                f"{task_id}:evaluation_failed",
                "evaluation_failed",
                task_id=task_id,
                stage="evaluation",
            )
            return _with_state(
                _result(
                    root=root,
                    status="failed",
                    task_id=task_id,
                    classification=classification,
                    error=_safe_error("evaluation", exc),
                ),
                "evaluation_failed",
            )
        if assessment is not None:
            _record_event(
                root,
                f"{task_id}:assessed",
                "assessed",
                task_id=task_id,
                stage="evaluation",
                assessment=assessment.to_dict(),
            )
        else:
            prior = _prior_hermes_result(
                root, task_id, content_digest, "assessment"
            )
            if prior is not None and prior[0] == "blocked":
                assessment_review = _prior_assessment_review(
                    root, task_id, content_digest
                )
                return _result(
                    root=root,
                    status="blocked",
                    task_id=task_id,
                    classification=classification,
                    route_name="needs_hermes",
                    assessment_review=assessment_review,
                    error=(assessment_review or {}).get("reason") or prior[2],
                )
            if prior is not None:
                assessment = prior[1]
                assessment_source = "hermes"
            elif hermes_client is None:
                assessment_review = _assessment_review("ai_disabled")
                _record_event(
                    root,
                    f"{task_id}:assessment_blocked:{content_digest[:12]}",
                    "blocked",
                    task_id=task_id,
                    stage="hermes",
                    operation="assessment",
                    content_digest=content_digest,
                    assessment_review=assessment_review,
                    error=assessment_review["reason"],
                )
                return _result(
                    root=root,
                    status="blocked",
                    task_id=task_id,
                    classification=classification,
                    route_name="needs_assessment",
                    assessment_review=assessment_review,
                    error=assessment_review["reason"],
                )
            else:
                try:
                    allowed = tuple(
                        sorted(
                            load_recipes(
                                root=root,
                                scope=getattr(item, "scope", None),
                            )
                        )
                    )
                    raw_response = hermes_client.assess(
                        _hermes_input(item), allowed
                    )
                    assessment = validate_hermes_assessment(
                        raw_response,
                        allowed,
                        root=str(root),
                        scope=getattr(item, "scope", None),
                    )
                    assessment_source = "hermes"
                    _record_event(
                        root,
                        f"{task_id}:hermes_assessed:{content_digest[:12]}",
                        "assessed",
                        task_id=task_id,
                        stage="hermes",
                        operation="assessment",
                        content_digest=content_digest,
                        assessment=assessment.to_dict(),
                    )
                except Exception as exc:
                    safe_error = _safe_error("hermes", exc)
                    reason_code = (
                        "ai_response_invalid"
                        if isinstance(exc, HermesValidationError)
                        else "ai_call_failed"
                    )
                    assessment_review = _assessment_review(reason_code)
                    state = "failed" if _hermes_retryable(exc) else "blocked"
                    _record_event(
                        root,
                        f"{task_id}:hermes_assessment_{state}:{content_digest[:12]}",
                        state,
                        task_id=task_id,
                        stage="hermes",
                        operation="assessment",
                        content_digest=content_digest,
                        assessment_review=assessment_review,
                        error=safe_error,
                    )
                    return _result(
                        root=root,
                        status="blocked",
                        task_id=task_id,
                        classification=classification,
                        route_name="needs_hermes",
                        assessment_review=assessment_review,
                        error=assessment_review["reason"],
                    )

    if assessment is not None and assessment.recipe is not None:
        input_check, selected_recipe = _validate_assessment_recipe(
            item,
            assessment.recipe,
            root=root,
        )
        input_check = input_check or {
            "state": "invalid",
            "reasons": ["recipe input could not be evaluated"],
            "evidence": [],
        }
        _record_event(
            root,
            f"{task_id}:input_checked",
            "input_checked",
            task_id=task_id,
            stage="evaluation",
            input_check=input_check,
            selected_recipe=selected_recipe,
        )
        if selected_recipe is None:
            reason = "; ".join(str(value) for value in input_check.get("reasons", []))
            return _record_recipe_block(
                root,
                task_id,
                classification=classification,
                assessment=assessment,
                input_check=input_check,
                reason=reason or "active recipe selection is unavailable",
            )
        if input_check.get("state") != "satisfied":
            reasons = input_check.get("reasons") or ["필수 입력을 확인할 수 없습니다."]
            _record_event(
                root,
                f"{task_id}:input_missing",
                "blocked",
                task_id=task_id,
                stage="routing",
                input_check=input_check,
            )
            return _result(
                root=root,
                status="blocked",
                task_id=task_id,
                classification=classification,
                assessment=assessment,
                input_check=input_check,
                route_name="needs_input",
                error="; ".join(str(reason) for reason in reasons),
            )

    try:
        route_name = route(assessment, source=assessment_source)
    except Exception as exc:
        try:
            _record_event(root, f"{task_id}:routing_failed", "failed", task_id=task_id, stage="routing")
        except Exception:
            pass
        return _with_state(_result(root=root, status="failed", task_id=task_id, classification=classification, assessment=assessment, input_check=input_check, error=_safe_error("routing", exc)), "routing_failed")

    # Whether a human decision is still owed is policy, not a constant: work
    # that writes nothing anywhere is finished once the package is rendered.
    # The policy answers for deterministic verdicts only. A Hermes-sourced
    # assessment always gets a human, or the model would be closing tasks on
    # its own judgment — the authority docs/ARCHITECTURE.md §15 reserves for a person
    # or an explicit Core policy, and a model proposal is neither.
    auto_complete = (
        assessment is not None
        and assessment_source == "rules"
        and not requires_approval(assessment.risk)
    )
    final_state = "completed" if auto_complete else "review_required"

    try:
        package = render_work_package(
            item,
            classification,
            root,
            final_status=final_state,
        )
        _record_event(root, f"{task_id}:{final_state}", final_state, task_id=task_id, stage="render")
    except Exception as exc:
        try:
            _record_event(root, f"{task_id}:render_failed", "render_failed", task_id=task_id, stage="render")
        except Exception:
            pass
        return _with_state(_result(root=root, status="failed", task_id=task_id, classification=classification, assessment=assessment, input_check=input_check, route_name=route_name, error=_safe_error("render", exc)), "render_failed")

    # A route is advisory workflow information; no recipe is executed here.
    return _result(root=root, status=final_state, task_id=task_id, classification=classification, assessment=assessment, input_check=input_check, route_name=route_name, package=package)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline maintenance task pipeline.")
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--scope",
        default=None,
        help="owner scope of this collection; requires --connector-id. "
        "Supplying both emits a contract_version 2 WorkItem with a scoped task id.",
    )
    parser.add_argument("--connector-id", default=None, dest="connector_id")
    args = parser.parse_args(argv)
    if (args.scope is None) != (args.connector_id is None):
        parser.error("--scope and --connector-id must be given together")
    try:
        result = run_pipeline(
            args.input_path,
            output_root=args.output_root,
            scope=args.scope,
            connector_id=args.connector_id,
        )
    except Exception as exc:  # pragma: no cover - argparse/process boundary
        print(f"pipeline failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
