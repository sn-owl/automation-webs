"""Deterministic, read-only data projection for dashboard consumers.

The task store remains the source of task identity and the event log remains the
source of state and derived metadata.  This module deliberately reads only the
small, display-safe fields needed by a dashboard; it never calls a store write
API and never serializes a WorkItem (which would include its raw body).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

_UNSAFE_SUMMARY = re.compile(
    r"(?i)(?:authorization|proxy-authorization)\s*[:=]\s*\S+"
    r"|(?<![a-z])bearer\s+\S+"
    r"|(?:password|passwd|pwd|api(?:[-_ ]+)?(?:key|token)|access[-_]?token|"
    r"refresh[-_]?token|session[-_]?(?:id|token|cookie)?|cookie|cookies|"
    r"credential|credentials|secret|token|tokens|private[-_ ]+key)\b"
)

_SAFE_STATE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_CANONICAL_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_CLASSIFICATION_ENUMS = {
    "ownership": {"내 업무", "다른 담당자", "정보 부족"},
}
_ASSESSMENT_VALUES = {
    "automation_level": {"manual", "assisted", "developable", "ready"},
    "risk": {"read_only", "local_artifact_only", "remote_write", "code_diff", "commit_push"},
}
_CLASSIFICATION_LABEL_FIELDS = frozenset({"responsibility", "task_type", "size"})
_CLASSIFICATION_REVIEW_CODES = frozenset(
    {"ai_disabled", "ai_call_failed", "ai_response_invalid"}
)


_SAFE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DECISION_ACTIONS = {"approve", "reject", "modify", "defer"}
_INPUT_CHECK_STATES = {"satisfied", "missing", "ambiguous", "invalid"}
_VERIFICATION_STATES = {"passed", "failed", "unverifiable"}
_FEEDBACK_TYPES = {"classification_correction", "execution_defect", "input_supplement", "result_correction"}
_PREPARATION_STATES = {"single_candidate", "multiple_candidates", "no_candidate"}
_REPORT_STATES = {"not_requested", "created"}
_REPORT_FORMATS = {"txt", "md"}
_TECHNICAL_STATES = frozenset(
    {
        "discovered",
        "stored",
        "files_processed",
        "classified",
        "assessed",
        "duplicate",
        "input_checked",
        "verification_recorded",
        "source_completion_observed",
        "preparation_recorded",
        "execution_started",
        "execution_completed",
    }
)
_DOMAIN_BY_TASK_TYPE = {
    "content_edit": "homepage_content",
    "image_popup": "homepage_content",
    "structure_change": "development_support",
    "feature_dev": "development_support",
    "feature_bug": "development_support",
    "data_refresh": "document_processing",
    "audit_report": "document_processing",
    "info_request": "information_request",
}
_DOMAIN_LABELS = {
    "homepage_content": "홈페이지 콘텐츠",
    "document_processing": "문서·자료 처리",
    "development_support": "개발·장애",
    "information_request": "문의·확인",
    "operations": "운영",
}



_CLASSIFICATION_FIELDS = (
    "responsibility",
    "task_type",
    "size",
    "confidence",
    "evidence",
    # Extended user-intent context (C3). pattern_match is internal matching
    # detail and is intentionally not surfaced in the safe display projection.
    "ownership",
    "primary_role",
    "responsibility_scope",
    "collaboration",
    "risk_flags",
    "next_action",
    "unmatched_aspects",
)
_CLASSIFICATION_TEXT_FIELDS = ("primary_role", "responsibility_scope", "next_action")
_CLASSIFICATION_LIST_FIELDS = ("collaboration", "risk_flags", "unmatched_aspects")
_ASSESSMENT_FIELDS = (
    "automation_level",
    "risk",
    "confidence",
    "recipe",
    "evidence",
)
_ARTIFACT_FIELDS = (
    "name",
    "type",
    "path",
    "sha256",
    "status",
    "size",
    "parser",
)


def build_dashboard_read_model(task_store, event_log, notification_store=None) -> dict[str, Any]:
    """Build a safe dashboard projection without mutating either input.

    ``task_store`` is expected to expose the existing ``list_tasks`` API and
    ``event_log`` the existing ``read`` API.  Small duck-typed adapters are
    accepted as well, which keeps the projection independent of persistence.
    Stored WorkItems are inspected field-by-field rather than converted with
    ``to_dict`` so their raw body and source payload cannot enter the result.
    """
    tasks = _read_tasks(task_store)
    events = _read_events(event_log)
    events_by_task = _latest_metadata(events)
    deliveries_by_task = _notification_metadata(notification_store)

    projected: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: _task_id(item)):
        task_id = _task_id(task)
        if not task_id:
            continue
        metadata = events_by_task.get(task_id, {})
        task_revision = _positive_int(_field(task, "revision")) or 1
        detail: dict[str, Any] = {
            "task_id": task_id,
            "state": metadata.get("state", "new"),
            "stage": metadata.get("stage", "input"),
            "version": metadata.get("version", task_revision),
            "source_completion_observed": bool(
                metadata.get("source_completion_observed")
                or _field(task, "source_completion_observed")
            ),
            "masking": "recorded" if _text(_field(task, "mask_table_ref")) else "missing",
        }
        for scalar in (
            "result_version",
            "attempt",
            "execution_state",
            "quarantine",
            "quarantine_history",
        ):
            if scalar in metadata:
                detail[scalar] = metadata[scalar]
        scope = _text(_field(task, "scope"))
        if scope is not None and _SAFE_REFERENCE.fullmatch(scope):
            detail["scope"] = scope
        summary = _safe_summary(_field(task, "title"))
        if summary is not None:
            detail["summary"] = summary
            detail["display_name"] = summary
        received_at = _text(_field(task, "received_at"))
        if received_at is not None and _is_iso_timestamp(received_at):
            detail["received_at"] = received_at
        source = _safe_source(_field(task, "source"))
        if source:
            detail["source"] = source
        for section in (
            "classification",
            "classification_review",
            "assessment_review",
            "assessment",
            "preparation",
            "decision",
            "artifacts",
            "input_check",
            "verification",
            "actual_completion",
            "feedback",
        ):
            value = metadata.get(section)
            if value:
                detail[section] = value
        if metadata.get("preparation_state"):
            detail["preparation_state"] = metadata["preparation_state"]
        if deliveries_by_task.get(task_id):
            detail["notification_delivery"] = deliveries_by_task[task_id]
        classification = detail.get("classification") or {}
        if isinstance(classification, Mapping):
            domain = _business_domain(classification, summary)
            detail["domain"] = domain
            detail["domain_label"] = business_domain_label(domain)
        projected.append(detail)

    status_counts: dict[str, int] = {}
    for detail in projected:
        state = detail["state"]
        status_counts[state] = status_counts.get(state, 0) + 1

    return {
        "tasks": projected,
        "status_counts": {state: status_counts[state] for state in sorted(status_counts)},
    }


def _notification_metadata(store) -> dict[str, list[dict[str, Any]]]:
    if store is None:
        return {}
    reader = getattr(store, "read_deliveries", None)
    if not callable(reader):
        return {}
    try:
        records = reader()
    except Exception:
        return {}
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        task_id = _text(record.get("task_id"))
        safe = _safe_notification_delivery(record)
        if task_id is not None and safe:
            by_task.setdefault(task_id, []).append(safe)
    for task_id in by_task:
        by_task[task_id].sort(
            key=lambda item: (
                str(item.get("recorded_at", "")),
                str(item.get("event", "")),
                str(item.get("channel", "")),
            )
        )
    return by_task


def _read_tasks(task_store) -> list[Any]:
    if task_store is None:
        return []
    reader = getattr(task_store, "list_tasks", None)
    if callable(reader):
        value = reader()
    elif callable(task_store):
        value = task_store()
    else:
        value = task_store
    if value is None:
        return []
    return list(value)


def _read_events(event_log) -> list[Mapping[str, Any]]:
    if event_log is None:
        return []
    reader = getattr(event_log, "read", None)
    if callable(reader):
        value = reader()
    elif callable(event_log):
        value = event_log()
    else:
        value = event_log
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    return [event for event in value if isinstance(event, Mapping)]


def _latest_metadata(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fold events while selecting the newest result version and attempt.

    Quarantine paths remain in ``quarantine_history`` for audit display, but
    ``quarantine`` is only the path for the current failed attempt. This keeps
    an old failed artifact from looking like the current successful result.
    """
    by_task: dict[str, dict[str, Any]] = {}
    for event in events:
        task_id = _text(event.get("task_id"))
        if task_id is None:
            decision = event.get("decision")
            if isinstance(decision, Mapping):
                task_id = _text(decision.get("task_id"))
        if task_id is None:
            continue

        metadata = by_task.setdefault(task_id, {})
        state_before_event = metadata.get("state")
        state = _first_text(event, "state", "status", "to_state", "final_status")
        stage = _text(event.get("stage"))
        if state is not None and _SAFE_STATE.fullmatch(state):
            if state not in _TECHNICAL_STATES:
                current = metadata.get("state")
                if state == "actual_completed":
                    if current is not None:
                        metadata["preparation_state"] = current
                    metadata["state"] = state
                    if stage is not None:
                        metadata["stage"] = stage
                elif not (
                    state.endswith("_failed")
                    and current in {"review_required", "completed", "blocked", "requested", "approved", "executing", "succeeded"}
                ):
                    metadata["state"] = state
                    if stage is not None:
                        metadata["stage"] = stage

        # Task revisions are the task-level version. Execution result versions
        # are intentionally folded independently from the execution record.
        version = _positive_int(event.get("task_version"))
        if version is not None:
            metadata["version"] = version

        execution = event.get("execution")
        execution_rank: tuple[int, int] | None = None
        execution_state: str | None = None
        if isinstance(execution, Mapping):
            result_version = _positive_int(execution.get("result_version"))
            attempt = _positive_int(execution.get("attempt"))
            if result_version is not None and attempt is not None:
                execution_rank = (result_version, attempt)
            execution_state = _text(execution.get("state"))
            current_rank = metadata.get("_execution_rank")
            if execution_rank is not None and (
                not isinstance(current_rank, tuple) or execution_rank >= current_rank
            ):
                if isinstance(current_rank, tuple) and execution_rank > current_rank:
                    metadata.pop("verification", None)
                    metadata.pop("artifacts", None)
                    metadata.pop("quarantine", None)
                metadata["_execution_rank"] = execution_rank
                if execution_state in {"requested", "approved", "executing", "succeeded", "failed"}:
                    metadata["execution_state"] = execution_state
                metadata["result_version"] = result_version
                metadata["attempt"] = attempt
                if execution_state in {"requested", "approved", "executing", "succeeded"}:
                    metadata.pop("quarantine", None)
        current_rank = metadata.get("_execution_rank")
        if (
            execution_rank is not None
            and isinstance(current_rank, tuple)
            and execution_rank < current_rank
            and event.get("type") == "execution"
        ):
            if state_before_event is None:
                metadata.pop("state", None)
            else:
                metadata["state"] = state_before_event
        quarantine = _safe_relative_path(event.get("quarantine"))
        if quarantine is not None:
            history = metadata.setdefault("quarantine_history", [])
            if quarantine not in history:
                history.append(quarantine)
            current_rank = metadata.get("_execution_rank")
            if (
                (execution_rank is None and current_rank is None)
                or execution_rank == current_rank
            ) and execution_state == "failed":
                metadata["quarantine"] = quarantine

        classification = _safe_section(event.get("classification"), _CLASSIFICATION_FIELDS)
        if classification:
            metadata["classification"] = classification
        assessment = _safe_section(event.get("assessment"), _ASSESSMENT_FIELDS)
        if assessment:
            metadata["assessment"] = assessment
        preparation = _safe_preparation(event.get("preparation"))
        if preparation:
            metadata["preparation"] = preparation

        input_check = _safe_input_check(event.get("input_check"))
        if input_check:
            metadata["input_check"] = input_check
        decision_value = event.get("decision")
        if isinstance(decision_value, Mapping):
            decision = _safe_decision(decision_value)
            if decision:
                metadata["decision"] = decision

        artifacts_value = event.get("artifacts")
        if artifacts_value is not None and (
            not isinstance(execution, Mapping) or execution_state == "succeeded"
        ):
            artifacts = _safe_artifacts(artifacts_value)
            if artifacts:
                metadata["artifacts"] = artifacts

        verification = _safe_verification(event.get("verification"))
        if verification and (
            execution_rank is None
            or execution_rank == metadata.get("_execution_rank")
        ):
            metadata["verification"] = verification
            if verification["status"] == "failed":
                metadata["state"] = "verification_failed"
            elif verification["status"] == "unverifiable":
                metadata["state"] = "verification_unverifiable"
        actual = _safe_actual_completion(event.get("actual_completion"))
        if actual:
            metadata["actual_completion"] = actual
        if event.get("source_completion_observed") is True:
            metadata["source_completion_observed"] = True
        for review_key in ("classification_review", "assessment_review"):
            review = _safe_classification_review(event.get(review_key))
            if review:
                metadata[review_key] = review
        feedback = _safe_feedback(event.get("feedback"))
        if feedback:
            metadata.setdefault("feedback", []).append(feedback)

    for metadata in by_task.values():
        metadata.pop("_execution_rank", None)
    return by_task
def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _safe_summary(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", text)
    if _UNSAFE_SUMMARY.search(normalized):
        return None
    return text


def _safe_evidence(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    safe: list[str] = []
    for item in value:
        evidence = _safe_summary(item)
        if evidence is None:
            continue
        normalized = re.sub(r"\s+", " ", evidence)
        if len(normalized) > 240 or re.search(
            r"(?i)\b(raw|unrestricted|source\s+body|source\s+content|original\s+content)\b",
            normalized,
        ):
            continue
        safe.append(normalized)
    return safe


def _business_domain(classification: Mapping[str, Any], summary: str | None) -> str:
    task_type = _text(classification.get("task_type"))
    if task_type in _DOMAIN_BY_TASK_TYPE:
        return _DOMAIN_BY_TASK_TYPE[task_type]
    if summary and "무더위쉼터" in summary:
        return "document_processing"
    return "operations"


def business_domain_label(value: Any) -> str:
    return _DOMAIN_LABELS.get(str(value), "기타 업무")


def _task_id(task: Any) -> str:
    value = _text(_field(task, "task_id"))
    return value or ""


def _safe_source(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    safe: dict[str, str] = {}
    for field in ("type", "id", "external_id"):
        text = _text(_field(value, field))
        if text is not None and _SAFE_REFERENCE.fullmatch(text):
            safe[field] = text
    return safe


def _safe_attachments(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    allowed_types = {
        "hwpx", "hwp", "pdf", "docx", "xls", "xlsx",
        "zip", "jpg", "jpeg", "png", "gif",
    }
    safe: list[dict[str, str]] = []
    for attachment in value:
        name = _safe_summary(_field(attachment, "name"))
        file_type = _text(_field(attachment, "type"))
        if name is None or file_type is None:
            continue
        file_type = file_type.lower()
        if file_type not in allowed_types:
            continue
        safe.append({"name": name, "type": file_type})
    return safe

def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _first_text(event: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = _text(event.get(name))
        if value is not None:
            return value
    return None


def _safe_relative_path(value: Any) -> str | None:
    text = _text(value)
    if (
        text is None
        or text.startswith("/")
        or "\\" in text
        or ":" in text
        or ".." in text.split("/")
    ):
        return None
    if len(text) > 240:
        return None
    return text


def _safe_classification_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    decision = _text(value.get("decision"))
    required = value.get("review_required")
    reason_code = _text(value.get("reason_code"))
    reason = _safe_summary(value.get("reason"))
    next_action = _text(value.get("next_action"))
    if decision != "DO NOT" or required is not True:
        return {}
    if reason_code not in _CLASSIFICATION_REVIEW_CODES or reason is None:
        return {}
    if next_action != "user_review":
        return {}
    return {
        "decision": decision,
        "review_required": True,
        "reason_code": reason_code,
        "reason": reason,
        "next_action": next_action,
    }


def _safe_classification_label(value: Any) -> str | None:
    text = _safe_summary(value)
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", text)
    if len(normalized) > 120:
        return None
    return normalized
def _safe_relative_path(value: Any) -> str | None:
    text = _text(value)
    if text is None or text.startswith("/") or "\\" in text or ".." in text.split("/"):
        return None
    if len(text) > 240:
        return None
    return text


def _safe_digest(value: Any) -> str | None:
    text = _text(value)
    return text if text is not None and re.fullmatch(r"[0-9a-f]{64}", text) else None


def _safe_preparation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in ("plan_version", "scope", "project_id", "state", "candidate_state"):
        text = _text(value.get(field))
        if text is not None and len(text) <= 128:
            result[field] = text
    task_id = _text(value.get("task_id"))
    if task_id is not None and _SAFE_REFERENCE.fullmatch(task_id):
        result["task_id"] = task_id
    query = _safe_summary(value.get("query"))
    if query is not None:
        result["query"] = query
    confidence = value.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1:
        result["confidence"] = confidence
    missing = value.get("missing_information")
    if isinstance(missing, (list, tuple)):
        result["missing_information"] = [
            item for item in (_safe_classification_label(entry) for entry in missing) if item
        ]

    def safe_candidate(candidate: Any) -> dict[str, Any] | None:
        if not isinstance(candidate, Mapping):
            return None
        path = _safe_relative_path(candidate.get("path"))
        digest = _safe_digest(candidate.get("sha256"))
        if path is None or digest is None:
            return None
        safe: dict[str, Any] = {"path": path, "sha256": digest}
        for field in ("size", "score", "path_score", "content_score"):
            number = candidate.get(field)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                safe[field] = number
        symbols = candidate.get("symbols")
        if isinstance(symbols, (list, tuple)):
            safe["symbols"] = [
                item for item in (_safe_classification_label(entry) for entry in symbols) if item
            ]
        evidence = candidate.get("match_evidence")
        if isinstance(evidence, (list, tuple)):
            safe["match_evidence"] = [
                {"kind": kind, "term": term}
                for entry in evidence[:20]
                if isinstance(entry, Mapping)
                and (kind := _safe_classification_label(entry.get("kind"))) is not None
                and (term := _safe_classification_label(entry.get("term"))) is not None
            ]
        return safe

    target = value.get("target")
    if isinstance(target, Mapping):
        safe_target: dict[str, Any] = {}
        primary = safe_candidate(target.get("primary"))
        if primary is not None:
            safe_target["primary"] = primary
        alternatives = target.get("alternatives")
        if isinstance(alternatives, (list, tuple)):
            safe_target["alternatives"] = [
                item for entry in alternatives[:10] if (item := safe_candidate(entry)) is not None
            ]
        if safe_target:
            result["target"] = safe_target

    evidence = value.get("read_evidence")
    if isinstance(evidence, Mapping):
        safe_evidence: dict[str, Any] = {}
        for field in (
            "indexed_paths",
            "search_attempted_files",
            "safe_search_files",
            "read_files",
            "detail_read_files",
            "symbol_analysis_files",
        ):
            items = evidence.get(field)
            if isinstance(items, (list, tuple)):
                safe_evidence[field] = [
                    path for item in items if (path := _safe_relative_path(item)) is not None
                ]
        skipped = evidence.get("skipped_files")
        if isinstance(skipped, (list, tuple)):
            safe_evidence["skipped_files"] = [
                {"path": path, "reason": reason}
                for entry in skipped
                if isinstance(entry, Mapping)
                and (path := _safe_relative_path(entry.get("path"))) is not None
                and (reason := _safe_classification_label(entry.get("reason"))) is not None
            ]
        if evidence.get("hard_exclusions_applied") is True:
            safe_evidence["hard_exclusions_applied"] = True
        evidence_digest = _safe_digest(evidence.get("evidence_digest"))
        if evidence_digest is not None:
            safe_evidence["evidence_digest"] = evidence_digest
        if safe_evidence:
            result["read_evidence"] = safe_evidence

    external_ai = value.get("external_ai")
    if isinstance(external_ai, Mapping):
        permitted = external_ai.get("permitted")
        used = external_ai.get("used")
        if isinstance(permitted, bool) and isinstance(used, bool):
            result["external_ai"] = {"permitted": permitted, "used": used}

    for field in ("ordered_steps", "risk_flags"):
        items = value.get(field)
        if isinstance(items, (list, tuple)):
            result[field] = [
                item for item in (_safe_classification_label(entry) for entry in items) if item
            ]
    next_action = _safe_summary(value.get("next_action"))
    if next_action is not None:
        result["next_action"] = next_action

    report = value.get("report")
    if isinstance(report, Mapping):
        requested = report.get("requested")
        status = _text(report.get("status"))
        if isinstance(requested, bool) and status in _REPORT_STATES:
            safe_report: dict[str, Any] = {"requested": requested, "status": status}
            report_format = _text(report.get("format"))
            if report_format in _REPORT_FORMATS:
                safe_report["format"] = report_format
            report_path = _safe_relative_path(report.get("path"))
            if report_path is not None:
                safe_report["path"] = report_path
            result["report"] = safe_report
    return result




def _safe_section(value: Any, fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed_values = (
        _CLASSIFICATION_ENUMS if "responsibility" in fields else _ASSESSMENT_VALUES
    )
    safe: dict[str, Any] = {}
    for field in fields:
        item = value.get(field)
        if field in _CLASSIFICATION_LABEL_FIELDS:
            text = _safe_classification_label(item)
            if text is not None:
                safe[field] = text
        elif field == "confidence":
            if isinstance(item, (int, float)) and not isinstance(item, bool) and 0 <= item <= 1:
                safe[field] = item
        elif field == "evidence":
            evidence = _safe_evidence(item)
            if evidence:
                safe[field] = evidence
        elif field == "recipe":
            text = _text(item)
            if text is not None and _SAFE_REFERENCE.fullmatch(text):
                safe[field] = text
        elif field in _CLASSIFICATION_TEXT_FIELDS:
            text = _safe_summary(item)
            if text is not None:
                safe[field] = text
        elif field in _CLASSIFICATION_LIST_FIELDS:
            items = _safe_evidence(item)
            if items:
                safe[field] = items
        elif field in allowed_values:
            text = _text(item)
            if text in allowed_values[field]:
                safe[field] = text
    return safe
def _safe_actual_completion(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    task_id = _text(value.get("task_id"))
    if task_id is not None and _SAFE_REFERENCE.fullmatch(task_id):
        safe["task_id"] = task_id
    version = _positive_int(value.get("task_version"))
    if version is not None:
        safe["task_version"] = version
    execution_key = _text(value.get("execution_key"))
    if execution_key is not None and _SAFE_REFERENCE.fullmatch(execution_key):
        safe["execution_key"] = execution_key
    actor = _text(value.get("actor"))
    if actor is not None and _SAFE_REFERENCE.fullmatch(actor):
        safe["actor"] = actor
    timestamp = _text(value.get("timestamp"))
    if timestamp is not None and _is_iso_timestamp(timestamp):
        safe["timestamp"] = timestamp
    if value.get("reason_recorded") is True:
        safe["has_reason"] = True
    # What the completion rests on: an Executor run, work done by hand, or work
    # finished in the source system. A reader has to be able to tell them apart.
    basis = _text(value.get("basis"))
    if basis in ("executor", "manual", "external"):
        safe["basis"] = basis
    return safe


def _safe_notification_delivery(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for field in ("event", "channel", "status"):
        text = _text(value.get(field))
        if text is not None and _SAFE_REFERENCE.fullmatch(text):
            safe[field] = text
    attempt = _positive_int(value.get("attempt"))
    if attempt is not None:
        safe["attempt"] = attempt
    if isinstance(value.get("retryable"), bool):
        safe["retryable"] = value["retryable"]
    recorded_at = _text(value.get("recorded_at"))
    if recorded_at is not None and _is_iso_timestamp(recorded_at):
        safe["recorded_at"] = recorded_at
    local_reference = _safe_relative_path(value.get("local_reference"))
    if local_reference is not None:
        safe["local_reference"] = local_reference
    return safe


def _safe_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}

    actor = _text(value.get("actor"))
    if actor is not None and _SAFE_REFERENCE.fullmatch(actor):
        safe["actor"] = actor
    task_id = _text(value.get("task_id"))
    if task_id is not None and _SAFE_REFERENCE.fullmatch(task_id):
        safe["task_id"] = task_id
    version = _positive_int(value.get("task_version"))
    if version is not None:
        safe["task_version"] = version
    action = _text(value.get("action"))
    if action in _DECISION_ACTIONS:
        safe["action"] = action
    timestamp = _text(value.get("timestamp"))
    if timestamp is not None and _is_iso_timestamp(timestamp):
        safe["timestamp"] = timestamp
    if _text(value.get("reason")) is not None:
        safe["has_reason"] = True
    return safe
def _is_iso_timestamp(value: str) -> bool:
    if _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _safe_artifacts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []

    result: list[dict[str, Any]] = []
    for artifact in value:
        if not isinstance(artifact, Mapping):
            continue
        safe: dict[str, Any] = {}
        for field in _ARTIFACT_FIELDS:
            item = artifact.get(field)
            if field == "path":
                text = _safe_relative_path(item)
                if text is not None:
                    safe[field] = text
            elif field == "size":
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    safe[field] = item
            else:
                text = _safe_summary(item)
                if text is not None:
                    safe[field] = text
        if safe:
            result.append(safe)
    return sorted(result, key=lambda item: tuple(str(item.get(field, "")) for field in _ARTIFACT_FIELDS))

def _safe_input_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    state = _text(value.get("state"))
    result: dict[str, Any] = {}
    if state in _INPUT_CHECK_STATES:
        result["state"] = state
    reasons = _safe_evidence(value.get("reasons"))
    evidence = _safe_evidence(value.get("evidence"))
    if reasons:
        result["reasons"] = reasons
    if evidence:
        result["evidence"] = evidence
    recipe_id = _text(value.get("recipe_id"))
    if recipe_id is not None and _SAFE_REFERENCE.fullmatch(recipe_id):
        result["recipe_id"] = recipe_id
    return result


def _safe_verification(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    status = _text(value.get("status"))
    if status not in _VERIFICATION_STATES:
        return {}
    result: dict[str, Any] = {
        "status": status,
        "verification_version": _text(value.get("verification_version")) or "unknown",
    }
    for field in ("input_sha256", "artifact_sha256"):
        digest = _text(value.get(field))
        if digest is not None and re.fullmatch(r"[0-9a-f]{64}", digest):
            result[field] = digest
    checks_value = value.get("checks")
    checks: list[dict[str, Any]] = []
    if isinstance(checks_value, (list, tuple)):
        for check in checks_value:
            if not isinstance(check, Mapping):
                continue
            name = _text(check.get("name"))
            check_status = _text(check.get("status"))
            if check_status not in {"passed", "failed"}:
                passed = check.get("passed")
                check_status = (
                    "passed" if type(passed) is bool and passed else
                    "failed" if type(passed) is bool else None
                )
            if name is None or check_status not in {"passed", "failed"}:
                continue
            safe_check: dict[str, Any] = {"name": name, "status": check_status}
            for field in ("expected", "actual"):
                item = check.get(field)
                if isinstance(item, (str, int, bool)) and not isinstance(item, float):
                    safe_check[field] = item
            checks.append(safe_check)
    if checks:
        result["checks"] = checks
    handoff = value.get("handoff")
    if isinstance(handoff, Mapping):
        safe_handoff: dict[str, Any] = {}
        required = handoff.get("required")
        if isinstance(required, bool):
            safe_handoff["required"] = required
        handoff_status = _text(handoff.get("status"))
        if handoff_status in {"pending", "confirmed"}:
            safe_handoff["status"] = handoff_status
        next_action = _safe_summary(handoff.get("next_action"))
        if next_action is not None:
            safe_handoff["next_action"] = next_action
        if safe_handoff:
            result["handoff"] = safe_handoff
    return result


def _safe_feedback(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    feedback_type = _text(value.get("type"))
    if feedback_type not in _FEEDBACK_TYPES:
        return {}
    result: dict[str, Any] = {"type": feedback_type}
    for field in ("actor", "execution_key"):
        text = _text(value.get(field))
        if text is not None and _SAFE_REFERENCE.fullmatch(text):
            result[field] = text
    if _text(value.get("reason")) is not None:
        result["reason_recorded"] = True
    details = value.get("details")
    if feedback_type == "classification_correction" and isinstance(details, Mapping):
        correction = _safe_section(details.get("classification"), _CLASSIFICATION_FIELDS)
        if correction:
            result["classification"] = correction
    return result
