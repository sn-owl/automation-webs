"""Authoritative validation boundary for Hermes advisory proposals."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from automation.assessment import Assessment
from automation.classification import Classification
from automation.recipes import load_recipes

_FORBIDDEN_KEYS = frozenset({"command", "tool", "url", "path", "raw", "cookie", "token"})
_CLASSIFICATION_ENVELOPE = frozenset({"classification"})
_ASSESSMENT_ENVELOPE = frozenset({"assessment"})
_ASSESSMENT_REQUIRED_KEYS = frozenset({"automation_level", "risk", "confidence", "evidence"})
_ASSESSMENT_KEYS = frozenset((*_ASSESSMENT_REQUIRED_KEYS, "recipe"))


class HermesValidationError(ValueError):
    """Raised when Hermes output violates the Core proposal contract."""


def _decode(raw_output: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_output, str):
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise HermesValidationError("Hermes output is not valid JSON") from exc
    elif isinstance(raw_output, dict):
        payload = raw_output
    else:
        raise HermesValidationError("Hermes output must be a JSON object")
    if not isinstance(payload, dict):
        raise HermesValidationError("Hermes output must be a JSON object")
    _assert_no_forbidden_keys(payload)
    return payload


def _assert_no_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_KEYS:
                raise HermesValidationError("Hermes output contains a forbidden field")
            _assert_no_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_forbidden_keys(child)


def _require_envelope(
    payload: dict[str, Any], expected: frozenset[str], section: str
) -> dict[str, Any]:
    if set(payload) != expected or not isinstance(payload.get(section), dict):
        raise HermesValidationError(f"Hermes {section} envelope is invalid")
    return payload[section]


def _validate_allowed_recipe_ids(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise HermesValidationError(
            "allowed_recipe_ids must be a sequence of unique non-empty strings"
        )
    ids = tuple(value)
    if (
        any(not isinstance(recipe_id, str) or not recipe_id.strip() for recipe_id in ids)
        or len(set(ids)) != len(ids)
    ):
        raise HermesValidationError(
            "allowed_recipe_ids must be a sequence of unique non-empty strings"
        )
    return ids


def validate_hermes_classification(
    raw_output: str | dict[str, Any],
) -> Classification:
    """Validate one classification-only response for ordinary intake."""
    data = _require_envelope(
        _decode(raw_output), _CLASSIFICATION_ENVELOPE, "classification"
    )
    try:
        return Classification.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise HermesValidationError("invalid Hermes classification") from exc


def validate_hermes_assessment(
    raw_output: str | dict[str, Any],
    allowed_recipe_ids: Sequence[str],
    *,
    recipes: dict[str, dict[str, Any]] | None = None,
    root: str | None = None,
    scope: str | None = None,
) -> Assessment:
    """Validate one explicitly requested automation assessment."""
    allowed_ids = _validate_allowed_recipe_ids(allowed_recipe_ids)
    data = _require_envelope(_decode(raw_output), _ASSESSMENT_ENVELOPE, "assessment")
    keys = set(data)
    if not _ASSESSMENT_REQUIRED_KEYS <= keys or not keys <= _ASSESSMENT_KEYS:
        raise HermesValidationError("Hermes assessment has invalid fields")
    try:
        assessment = Assessment.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise HermesValidationError("invalid Hermes assessment") from exc
    if assessment.recipe is not None:
        if assessment.recipe not in allowed_ids:
            raise HermesValidationError("Hermes recipe is not allowed")
        if recipes is not None and not isinstance(recipes, dict):
            raise HermesValidationError("invalid Hermes recipe registry")
        try:
            registry = load_recipes(recipes=recipes, root=root, scope=scope)
        except (OSError, TypeError, ValueError) as exc:
            raise HermesValidationError("invalid Hermes recipe registry") from exc
        if assessment.recipe not in registry:
            raise HermesValidationError("Hermes recipe is not registered")
    return assessment
