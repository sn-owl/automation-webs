"""Shared validation helpers for S4-S6 output contracts.

``Classification`` (S4), ``Assessment`` (S5) and ``Pattern`` (S6) each parse an
untrusted ``dict`` into a frozen dataclass with explicit control validation,
extensible domain labels, a [0, 1]-bounded confidence, and a non-empty
free-text evidence list. This module is the single copy of that parsing logic;
the per-contract modules own only their field shapes and enum values.
"""

from __future__ import annotations

from typing import Any


def _required(data: dict[str, Any], key: str) -> Any:
    try:
        return data[key]
    except KeyError as exc:
        raise ValueError(f"{key} is required") from exc


def _one_of(data: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = _required(data, key)
    if value not in allowed:
        raise ValueError(f"{key} must be one of {allowed}")
    return value


def _unit_interval(data: dict[str, Any], key: str) -> float:
    value = _required(data, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number between 0 and 1")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{key} must be a number between 0 and 1")
    return float(value)


def _evidence(data: dict[str, Any]) -> tuple[str, ...]:
    value = _required(data, "evidence")
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("evidence must be a non-empty list")
    entries = tuple(value)
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("evidence entries must be non-empty strings")
    return entries
