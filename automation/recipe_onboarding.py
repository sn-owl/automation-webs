"""User-authored Recipe draft, test, review, activation, and matching flow.

Runtime activation is explicit, owner-scoped, and immutable by version. Shipped
examples are never a fallback when a runtime root is supplied.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
import uuid

from automation.models import WorkItem
from automation.recipes import (
    RecipeDefinition,
    get_recipe,
    load_recipes,
    recipe_digest,
)


_RECIPE_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{2,63}\Z")
_SCOPE_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
_ALLOWED_APPLICABILITY = frozenset(
    {"title_or_body_contains_any", "attachment_type_in", "source_type_in"}
)


class RecipeOnboardingError(ValueError):
    """Raised when a draft cannot advance through its lifecycle."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_id(value: Any) -> str:
    if not isinstance(value, str) or _RECIPE_ID.fullmatch(value) is None:
        raise RecipeOnboardingError(
            "recipe_id must match lowercase [a-z0-9._-], 3-64 characters"
        )
    return value


def _safe_scope(value: Any) -> str:
    if not isinstance(value, str) or _SCOPE_ID.fullmatch(value) is None:
        raise RecipeOnboardingError(
            "scope must match lowercase [a-z0-9._-], 1-64 characters"
        )
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecipeOnboardingError(f"{field} is required")
    return value.strip()


def _list_of_text(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise RecipeOnboardingError(f"{field} must be a non-empty list of text")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise RecipeOnboardingError(f"{field} must not contain duplicates")
    return normalized


def _steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RecipeOnboardingError("steps must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for step in value:
        if not isinstance(step, dict) or set(step) != {"operation", "parameters"}:
            raise RecipeOnboardingError(
                "each step must contain operation and parameters"
            )
        operation = _text(step["operation"], "step.operation")
        if not isinstance(step["parameters"], dict):
            raise RecipeOnboardingError("step.parameters must be an object")
        normalized.append(
            {"operation": operation, "parameters": dict(step["parameters"])}
        )
    return normalized


def _applicability(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict) or not value:
        raise RecipeOnboardingError("applicability is required")
    unknown = set(value) - _ALLOWED_APPLICABILITY
    if unknown:
        raise RecipeOnboardingError(
            f"unsupported applicability field(s): {sorted(unknown)}"
        )
    return {
        key: _list_of_text(values, f"applicability.{key}")
        for key, values in value.items()
    }


def _load_item(task_file: Path | str) -> WorkItem:
    try:
        payload = json.loads(Path(task_file).read_text(encoding="utf-8"))
        return WorkItem.from_dict(payload)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RecipeOnboardingError("task file is not a valid WorkItem") from exc


def _normalize_payload(
    payload: Mapping[str, Any], *, version: int = 1
) -> dict[str, Any]:
    executor = _text(payload.get("executor"), "executor")
    template_value = payload.get("template")
    template = (
        _text(template_value, "template")
        if template_value is not None
        else None
    )
    result = {
        "recipe_id": _safe_id(payload.get("recipe_id")),
        "version": version,
        "task_name": _text(payload.get("task_name"), "task_name"),
        "case_reference": _text(payload.get("case_reference"), "case_reference"),
        "description": _text(payload.get("description"), "description"),
        "scope": _safe_scope(payload.get("scope")),
        "executor": executor,
        "input_type": _text(payload.get("input_type"), "input_type"),
        "output_type": _text(payload.get("output_type"), "output_type"),
        "template": template,
        "steps": _steps(payload.get("steps")),
        "required_conditions": _list_of_text(
            payload.get("required_conditions"), "required_conditions"
        ),
        "exclusion_conditions": _list_of_text(
            payload.get("exclusion_conditions"), "exclusion_conditions"
        ),
        "success_checks": _list_of_text(
            payload.get("success_checks"), "success_checks"
        ),
        "approval_scope": _text(payload.get("approval_scope"), "approval_scope"),
        "applicability": _applicability(payload.get("applicability")),
        "status": "draft",
        "created_at": payload.get("created_at") or _now(),
        "updated_at": _now(),
    }
    for key in (
        "recognition",
        "execution",
        "verification",
        "error_policy",
        "ai_policy",
    ):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, dict):
                raise RecipeOnboardingError(f"{key} must be an object")
            result[key] = dict(value)
    _core_recipe(result)
    return result


def _draft_path(root: Path | str, scope: str, recipe_id: str) -> Path:
    return (
        Path(root)
        / "state"
        / "recipe-drafts"
        / _safe_scope(scope)
        / f"{_safe_id(recipe_id)}.json"
    )


def _registry_dir(root: Path | str, scope: str, recipe_id: str) -> Path:
    return (
        Path(root)
        / "state"
        / "recipes"
        / _safe_scope(scope)
        / _safe_id(recipe_id)
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def _core_recipe(draft: Mapping[str, Any]) -> RecipeDefinition:
    fields = {
        key: draft[key]
        for key in (
            "recipe_id",
            "description",
            "executor",
            "input_type",
            "output_type",
            "template",
            "version",
            "scope",
            "steps",
            "required_conditions",
            "exclusion_conditions",
            "success_checks",
            "approval_scope",
            "applicability",
            "recognition",
            "execution",
            "verification",
            "error_policy",
            "ai_policy",
        )
        if key in draft and draft[key] is not None
    }
    return RecipeDefinition.from_dict(fields)


def _validate_supported_plan(draft: Mapping[str, Any]) -> None:
    from automation.dispatch import validate_recipe_plan

    try:
        validate_recipe_plan(_core_recipe(draft))
    except ValueError as exc:
        raise RecipeOnboardingError(str(exc)) from exc


def list_drafts(
    *, scope: str | None = None, root: Path | str = "."
) -> list[dict[str, Any]]:
    """Return the existing draft/active projection, optionally for one scope.

    The files remain the single registry of truth.  This read boundary is used
    by the dashboard so a rerun never promotes session state to lifecycle
    authority.
    """
    scopes = [_safe_scope(scope)] if scope is not None else None
    base = Path(root) / "state" / "recipe-drafts"
    if not base.is_dir():
        return []
    if scopes is None:
        scope_paths = sorted(path for path in base.iterdir() if path.is_dir())
    else:
        scope_paths = [base / scopes[0]]
    rows: list[dict[str, Any]] = []
    for scope_path in scope_paths:
        if not scope_path.is_dir():
            continue
        for path in sorted(scope_path.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            if value.get("scope") != scope_path.name or not isinstance(value.get("recipe_id"), str):
                continue
            rows.append(
                {
                    "recipe_id": value["recipe_id"],
                    "scope": value["scope"],
                    "version": value.get("version", 1),
                    "status": value.get("status", "draft"),
                    "updated_at": value.get("updated_at"),
                    "last_test": value.get("last_test"),
                    "review": value.get("review"),
                    "active_pointer": value.get("active_pointer"),
                }
            )
    return rows


def _declaration_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "recipe_id",
            "task_name",
            "case_reference",
            "description",
            "scope",
            "executor",
            "input_type",
            "output_type",
            "template",
            "steps",
            "required_conditions",
            "exclusion_conditions",
            "success_checks",
            "approval_scope",
            "applicability",
            "recognition",
            "execution",
            "verification",
            "error_policy",
            "ai_policy",
        )
    }


def _same_declaration(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _declaration_payload(left) == _declaration_payload(right)


def create_draft(
    payload: Mapping[str, Any], *, root: Path | str = "."
) -> dict[str, Any]:
    draft = _normalize_payload(payload)
    path = _draft_path(root, draft["scope"], draft["recipe_id"])
    if path.exists():
        existing = get_draft(draft["recipe_id"], scope=draft["scope"], root=root)
        if _same_declaration(existing, draft):
            return existing
        raise RecipeOnboardingError(f"draft already exists: {draft['recipe_id']}")
    _write_json(path, draft)
    return draft


def get_draft(
    recipe_id: str, *, scope: str, root: Path | str = "."
) -> dict[str, Any]:
    path = _draft_path(root, scope, recipe_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecipeOnboardingError(f"draft not found: {recipe_id}") from exc
    if (
        not isinstance(value, dict)
        or value.get("recipe_id") != recipe_id
        or value.get("scope") != scope
    ):
        raise RecipeOnboardingError(f"invalid scoped draft: {recipe_id}")
    return value


def update_draft(
    recipe_id: str,
    changes: Mapping[str, Any],
    *,
    scope: str,
    root: Path | str = ".",
) -> dict[str, Any]:
    current = get_draft(recipe_id, scope=scope, root=root)
    merged = dict(current)
    merged.update(changes)
    merged["recipe_id"] = recipe_id
    merged["scope"] = scope
    candidate = _normalize_payload(merged, version=int(current.get("version", 1)))
    if _same_declaration(current, candidate):
        return current
    version = int(current.get("version", 1)) + 1
    draft = _normalize_payload(merged, version=version)
    # A changed declaration invalidates all evidence from the prior version.
    draft.pop("last_test", None)
    draft.pop("review", None)
    draft.pop("active_pointer", None)
    _write_json(_draft_path(root, scope, recipe_id), draft)
    return draft



def _test_verification(
    recipe: RecipeDefinition,
    item: WorkItem,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    artifacts = result.get("artifacts") or []
    if recipe.executor == "code-analysis":
        plan = result.get("plan")
        passed = (
            isinstance(plan, dict)
            and plan.get("plan_version") == "preparation-v2"
            and plan.get("state") == "ready"
            and "read_only" in plan.get("risk_flags", [])
        )
        return {
            "verification_version": "code-analysis-plan-v1",
            "status": "passed" if passed else "failed",
            "checks": [
                {
                    "name": "structured_read_only_plan",
                    "passed": passed,
                    "expected": True,
                    "actual": passed,
                }
            ],
        }
    if len(artifacts) != 1 or not isinstance(artifacts[0], dict):
        return {
            "verification_version": "artifact-count-v1",
            "status": "failed",
            "checks": [],
        }
    from automation.verification import verify_recipe_output

    return verify_recipe_output(
        recipe.recipe_id,
        item,
        artifacts[0].get("path"),
        recipe_definition=recipe,
    )

def test_draft(
    recipe_id: str,
    task_file: Path | str | None = None,
    *,
    task_id: str | None = None,
    retry: bool = False,
    scope: str,
    root: Path | str = ".",
) -> dict[str, Any]:
    draft = get_draft(recipe_id, scope=scope, root=root)
    if (task_file is None) == (task_id is None):
        raise RecipeOnboardingError("exactly one of task_file or task_id is required")
    if task_id is not None:
        try:
            from automation.task_store import get_task

            item = get_task(task_id, root=root)
        except Exception as exc:
            raise RecipeOnboardingError("Core task was not found") from exc
    else:
        item = _load_item(task_file)

    # Repeated delivery of the same test request is a read, not another
    # execution.  The capability and definition digests make stale evidence
    # ineligible for this shortcut.
    recipe = _core_recipe(draft)
    expected_capability = None
    try:
        _validate_supported_plan(draft)
        from automation.dispatch import capability_binding

        expected_capability = capability_binding(recipe)
    except Exception:
        pass
    previous = draft.get("last_test") or {}
    if (
        expected_capability is not None
        and previous.get("scope") == scope
        and previous.get("task_id") == item.task_id
        and previous.get("task_revision") == item.revision
        and previous.get("definition_sha256") == recipe_digest(recipe)
        and previous.get("capability") == expected_capability
        and (
            previous.get("status") == "passed"
            or (previous.get("status") == "failed" and not retry)
        )
    ):
        return previous

    session_id = (
        f"recipe-test-{datetime.now(timezone.utc).strftime('%m%dT%H%M%S')}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    session_root = Path(root) / "_sessions" / session_id
    artifact_dir = session_root / "artifacts" / scope / recipe_id
    from automation.dispatch import (
        capability_binding,
        dispatch_recipe,
        validate_recipe_input,
    )

    try:
        if item.scope != scope:
            raise RecipeOnboardingError("test WorkItem scope does not match draft scope")
        _validate_supported_plan(draft)
        recipe = _core_recipe(draft)
        input_check = validate_recipe_input(
            recipe_id,
            item,
            recipe_definition=recipe,
            root=root,
        )
        if input_check.get("state") != "satisfied":
            raise RecipeOnboardingError(
                "test blocked: "
                + "; ".join(str(reason) for reason in input_check.get("reasons", []))
            )
        result = dispatch_recipe(
            recipe_id,
            item,
            artifact_dir,
            recipe_definition=recipe,
            root=root,
        )
        verification = _test_verification(recipe, item, result)
        status = "passed" if verification.get("status") == "passed" else "failed"
        record = {
            "schema_version": 1,
            "session_id": session_id,
            "scope": scope,
            "task_id": item.task_id,
            "task_revision": item.revision,
            "status": status,
            "input_check": input_check,
            "result": result,
            "definition": recipe.to_dict(),
            "definition_sha256": recipe_digest(recipe),
            "capability": capability_binding(recipe),
            "verification": verification,
            "artifact_dir": str(artifact_dir),
            "tested_at": _now(),
        }
    except Exception as exc:
        diagnosis = getattr(exc, "diagnosis", None)
        if diagnosis is None:
            from automation.dispatch import safe_diagnosis

            diagnosis = safe_diagnosis("onboarding", exc)
        record = {
            "schema_version": 1,
            "session_id": session_id,
            "scope": scope,
            "task_id": item.task_id,
            "task_revision": item.revision,
            "status": "failed",
            "error": type(exc).__name__,
            "message": (
                str(exc) if isinstance(exc, RecipeOnboardingError) else "test failed"
            ),
            "diagnosis": diagnosis,
            "definition": recipe.to_dict(),
            "definition_sha256": recipe_digest(recipe),
            "capability": expected_capability,
            "artifact_dir": str(artifact_dir),
            "tested_at": _now(),
        }
    _write_json(session_root / "test-evidence.json", record)
    updated = dict(draft)
    updated["status"] = "tested" if record["status"] == "passed" else "test_failed"
    updated["last_test"] = record
    updated["updated_at"] = _now()
    _write_json(_draft_path(root, scope, recipe_id), updated)
    return record


def review_draft(
    recipe_id: str,
    *,
    scope: str,
    actor: str,
    reason: str,
    root: Path | str = ".",
) -> dict[str, Any]:
    draft = get_draft(recipe_id, scope=scope, root=root)
    if (
        draft.get("status") == "reviewed"
        and isinstance(draft.get("review"), dict)
        and draft["review"].get("actor") == actor
        and draft["review"].get("reason") == reason
    ):
        return draft
    if (
        draft.get("status") != "tested"
        or (draft.get("last_test") or {}).get("status") != "passed"
    ):
        raise RecipeOnboardingError("review blocked: a passed test is required")
    updated = dict(draft)
    updated["status"] = "reviewed"
    updated["review"] = {
        "actor": _text(actor, "actor"),
        "reason": _text(reason, "reason"),
        "reviewed_at": _now(),
    }
    updated["updated_at"] = _now()
    _write_json(_draft_path(root, scope, recipe_id), updated)
    return updated


def activate_draft(
    recipe_id: str,
    *,
    scope: str,
    actor: str,
    reason: str,
    root: Path | str = ".",
) -> dict[str, Any]:
    draft = get_draft(recipe_id, scope=scope, root=root)
    existing_pointer = load_active_metadata(recipe_id, scope=scope, root=root)
    if existing_pointer is not None:
        try:
            current_recipe = _core_recipe(draft)
            current_digest = recipe_digest(current_recipe)
        except (RecipeOnboardingError, ValueError):
            current_digest = None
        if (
            draft.get("status") == "active"
            and existing_pointer.get("version") == draft.get("version")
            and existing_pointer.get("definition_sha256") == current_digest
        ):
            return existing_pointer
    if draft.get("status") != "reviewed":
        raise RecipeOnboardingError(
            "activation blocked: reviewed draft with passed test required"
        )
    recipe = _core_recipe(draft)
    activation_actor = _text(actor, "actor")
    activation_reason = _text(reason, "reason")
    from automation.dispatch import capability_binding

    tested = draft.get("last_test") or {}
    capability = capability_binding(recipe)
    definition_sha256 = recipe_digest(recipe)
    if (
        tested.get("definition_sha256") != definition_sha256
        or tested.get("capability") != capability
    ):
        raise RecipeOnboardingError(
            "activation blocked: definition or capability changed since test"
        )
    registry = _registry_dir(root, scope, recipe_id)
    registry.mkdir(parents=True, exist_ok=True)
    version_path = registry / f"{recipe.version}.json"
    try:
        with version_path.open("x", encoding="utf-8") as stream:
            json.dump(recipe.to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        raise RecipeOnboardingError(
            "activation blocked: immutable recipe version already exists"
        ) from None
    pointer = {
        "schema_version": 1,
        "scope": scope,
        "recipe_id": recipe_id,
        "version": recipe.version,
        "definition_sha256": definition_sha256,
        "capability": capability,
        "activation": {
            "actor": activation_actor,
            "reason": activation_reason,
            "activated_at": _now(),
        },
    }
    _write_json(registry / "active.json", pointer)
    updated = dict(draft)
    updated["status"] = "active"
    updated["active_pointer"] = pointer
    updated["updated_at"] = _now()
    _write_json(_draft_path(root, scope, recipe_id), updated)
    return pointer


def load_active_metadata(
    recipe_id: str, *, scope: str, root: Path | str = "."
) -> dict[str, Any] | None:
    pointer_path = _registry_dir(root, scope, recipe_id) / "active.json"
    if not pointer_path.is_file():
        return None
    get_recipe(recipe_id, root=root, scope=scope)
    value = json.loads(pointer_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecipeOnboardingError("active recipe pointer is invalid")
    return value


def _matches(
    item: WorkItem, applicability: Mapping[str, Any]
) -> tuple[bool, list[str], list[str]]:
    evidence: list[str] = []
    reasons: list[str] = []
    haystack = f"{item.title}\n{item.body}"
    title_values = applicability.get("title_or_body_contains_any")
    if title_values and not isinstance(title_values, list):
        reasons.append("title_or_body_condition_invalid")
    elif title_values and not any(value in haystack for value in title_values):
        reasons.append("title_or_body_condition_missing")
    elif title_values:
        evidence.append("title_or_body_condition=true")
    source_values = applicability.get("source_type_in")
    if source_values and not isinstance(source_values, list):
        reasons.append("source_type_condition_invalid")
    elif source_values and item.source.type not in source_values:
        reasons.append("source_type_condition_missing")
    elif source_values:
        evidence.append("source_type_condition=true")
    types = {attachment.type for attachment in item.attachments}
    attachment_values = applicability.get("attachment_type_in")
    if attachment_values and not isinstance(attachment_values, list):
        reasons.append("attachment_type_condition_invalid")
    elif attachment_values and not types.intersection(attachment_values):
        reasons.append("attachment_type_condition_missing")
    elif attachment_values:
        evidence.append(f"attachment_types={len(types.intersection(attachment_values))}")
    return not reasons, evidence, reasons


def select_recipe(
    recipe_id: str, item: WorkItem, *, root: Path | str = "."
) -> dict[str, Any]:
    from automation.dispatch import validate_recipe_input

    recipe = get_recipe(recipe_id, root=root, scope=item.scope)
    matched, evidence, reasons = _matches(item, recipe.applicability or {})
    if matched:
        check = validate_recipe_input(
            recipe_id,
            item,
            recipe_definition=recipe,
            root=root,
        )
        evidence.extend(str(value) for value in check.get("evidence", []))
        reasons.extend(str(value) for value in check.get("reasons", []))
        state = (
            "satisfied"
            if check.get("state") == "satisfied"
            else str(check.get("state", "invalid"))
        )
    else:
        state = "ineligible"
    return {
        "recipe_id": recipe_id,
        "version": recipe.version,
        "state": state,
        "evidence": evidence,
        "reasons": reasons,
    }


def select_active_recipe(
    item: WorkItem, *, root: Path | str = "."
) -> dict[str, Any]:
    evaluations = [
        select_recipe(recipe_id, item, root=root)
        for recipe_id in sorted(load_recipes(root=root, scope=item.scope))
    ]
    satisfied = [entry for entry in evaluations if entry["state"] == "satisfied"]
    if len(satisfied) == 1:
        return {
            "state": "selected",
            "selected": satisfied[0],
            "candidates": evaluations,
        }
    if len(satisfied) > 1:
        return {"state": "ambiguous", "selected": None, "candidates": evaluations}
    blocked = any(
        entry["state"] not in {"satisfied", "ineligible"} for entry in evaluations
    )
    return {
        "state": "blocked" if blocked else "none",
        "selected": None,
        "candidates": evaluations,
    }


def cli_payload(args: Any) -> dict[str, Any]:
    try:
        steps = json.loads(args.steps)
        applicability = json.loads(args.applicability)
        policies = {
            key: json.loads(getattr(args, key)) if getattr(args, key) else None
            for key in (
                "execution",
                "verification",
                "recognition",
                "error_policy",
                "ai_policy",
            )
        }
    except json.JSONDecodeError as exc:
        raise RecipeOnboardingError("Recipe JSON declarations are invalid") from exc
    return {
        "recipe_id": args.recipe_id,
        "task_name": args.task_name,
        "case_reference": args.case_reference,
        "description": args.description,
        "executor": args.executor,
        "input_type": args.input_type,
        "output_type": args.output_type,
        "template": args.template,
        "steps": steps,
        "required_conditions": args.required_condition,
        "exclusion_conditions": args.exclusion_condition,
        "success_checks": args.success_check,
        "approval_scope": args.approval_scope,
        "applicability": applicability,
        "scope": args.scope,
        **policies,
    }
