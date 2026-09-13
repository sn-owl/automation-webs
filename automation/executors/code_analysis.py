"""Bounded static code analysis; never executes the target project."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from automation import identity
from automation.preparation_plan import build_preparation_plan, export_preparation_report
from automation.project_profile import ProjectProfile, load_project, project_content_digest


def _profile(item: Any, recipe_definition: Any, state_root: str | Path | None) -> ProjectProfile:
    execution = getattr(recipe_definition, "execution", None) or {}
    if not isinstance(execution, dict):
        raise ValueError("code analysis execution policy is invalid")
    project_id = execution.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("code analysis requires a registered project id")
    if state_root is None:
        raise ValueError("code analysis requires the scoped state root")
    recipe_scope = getattr(recipe_definition, "scope", None)
    item_scope = getattr(item, "scope", None)
    if not isinstance(recipe_scope, str) or recipe_scope in {"", "legacy"}:
        raise ValueError("code analysis recipe scope is required")
    if item_scope != recipe_scope:
        raise ValueError("code analysis item and recipe scopes do not match")
    return load_project(recipe_scope, project_id, root=state_root)


def _query(item: Any) -> str:
    provenance = getattr(item, "provenance", None) or {}
    declared = provenance.get("preparation_query") if isinstance(provenance, dict) else None
    if declared is not None:
        if not isinstance(declared, str) or not declared.strip():
            raise ValueError("preparation_query must be non-empty text")
        return declared.strip()
    query = "\n".join(
        value.strip()
        for value in (getattr(item, "title", ""), getattr(item, "body", ""))
        if isinstance(value, str) and value.strip()
    )
    if not query:
        raise ValueError("code analysis query is missing")
    return query


def source_digest(item: Any, recipe_definition=None, *, state_root=None) -> str:
    profile = _profile(item, recipe_definition, state_root)
    payload = {
        "project_content_digest": project_content_digest(profile),
        "query": _query(item),
        "task_id": getattr(item, "task_id", None),
        "revision": getattr(item, "revision", None),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_code_input(item: Any, recipe_definition=None, *, state_root=None) -> dict[str, Any]:
    try:
        profile = _profile(item, recipe_definition, state_root)
        _query(item)
        project_content_digest(profile)
        return {
            "state": "satisfied",
            "reasons": [],
            "evidence": [
                f"project={profile.project_id}",
                f"scope={profile.scope}",
                "read_only=true",
            ],
        }
    except (OSError, TypeError, ValueError) as exc:
        return {
            "state": "missing",
            "reasons": ["a readable registered project and analysis query are required"],
            "evidence": [type(exc).__name__],
        }


def _report_request(item: Any) -> str | None:
    provenance = getattr(item, "provenance", None) or {}
    request = provenance.get("preparation_report") if isinstance(provenance, dict) else None
    if request is None or request is False:
        return None
    if not isinstance(request, dict) or request.get("requested") is not True:
        raise ValueError("preparation_report must be an explicit request object")
    report_format = request.get("format")
    if report_format not in {"txt", "md"}:
        raise ValueError("preparation report format must be txt or md")
    return report_format


def execute_code_analysis(
    item: Any,
    artifact_dir: str | Path,
    *,
    seen_execution_keys=(),
    recipe_id="code-analysis",
    recipe_definition=None,
    execution_key=None,
    state_root=None,
) -> dict[str, Any]:
    profile = _profile(item, recipe_definition, state_root)
    input_hash = source_digest(item, recipe_definition, state_root=state_root)
    execution_key = execution_key or identity.execution_key(
        getattr(item, "task_id", "task"), recipe_id, input_hash
    )
    if execution_key in seen_execution_keys:
        return {
            "execution_key": execution_key,
            "status": "duplicate",
            "artifacts": [],
        }
    plan = build_preparation_plan(profile, query=_query(item))
    artifacts: list[dict[str, str]] = []
    report_format = _report_request(item)
    if report_format is not None:
        output = export_preparation_report(
            plan,
            Path(artifact_dir).resolve(),
            report_format=report_format,
        )
        artifacts.append(
            {
                "path": str(output),
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    return {
        "task_id": getattr(item, "task_id", None),
        "recipe_id": recipe_id,
        "execution_key": execution_key,
        "input_sha256": input_hash,
        "status": "succeeded",
        "plan": plan,
        "artifacts": artifacts,
        "report_requested": report_format is not None,
    }
