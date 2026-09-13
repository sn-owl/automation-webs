"""S7-A recipe contract and allow-listed registry (docs/ARCHITECTURE.md §14, §22).

A Recipe is an exact input->output transformation ("정확한 입력→출력 변환",
e.g. HWPX->XLS); an Executor is the code that performs it (``converter.py``).
The Global Constraints require Rule/Recipe/Skill to use only allow-listed
registered values -- this module is that allow-list for Recipes.

A ``RecipeDefinition`` never carries a command, a subprocess argv, or an
importable module path. It names its executor by an identifier that must be
a member of ``EXECUTORS`` -- the same "module-level dict of allow-listed
keys" shape ``automation.adapters.registry`` uses for board adapters.
``EXECUTORS``' values are human-readable notes only, never anything this
module (or a caller) could be tempted to run: there is no ``subprocess``, no
``shell=True``, no ``eval``/``exec``, and no dynamic import of a
caller-supplied module name anywhere here. Task 27 builds the first real
executor behind the ``restarea-converter`` key; wiring the two together is
Task 27's job, not this module's.

``get_recipe(recipe_id)`` is the sole way to resolve a Recipe -- an unknown
id raises ``UnknownRecipeError``, exactly like
``adapters.registry.normalize_html`` raises for an unknown board type.

# TODO(Task 41): automation.assessment's `recipe` field is a free-form
# reference string whose existence in this registry Task 19 explicitly
# deferred to Task 26. The actual `get_recipe(assessment.recipe)` coupling
# belongs in Task 41's pipeline wiring, where both objects are already in
# hand -- not here.
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from automation._contract import _one_of, _required

DEFAULT_RECIPES_DIR = Path(__file__).parents[1] / "config" / "recipes"
_SAFE_SCOPE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")

# Allow-listed executor identifiers. Values are human-readable notes only --
# never a command, a module path, or anything else this module could import
# or execute. Task 27 adds the executor implementation behind a key here;
# until then a key merely says "this identifier is permitted".
EXECUTORS: dict[str, str] = {
    "restarea-converter": "무더위쉼터 HWPX→XLS 변환",
    "excel-table": "허용된 선언형 Excel 표 정리",
    "code-analysis": "등록 프로젝트의 읽기 전용 구조 분석",
}

_FIELDS = frozenset(
    {
        "recipe_id",
        "description",
        "executor",
        "input_type",
        "output_type",
        "template",
        "version",
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
        "scope",
    }
)

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


class UnknownRecipeError(ValueError):
    """Raised when a recipe id is not present in the registry."""


def _validate_repo_relative_path(value: Any, *, field: str) -> str:
    """Reject anything that could resolve outside the repository root.

    Pure string validation, no filesystem access: a `..` segment, an
    absolute path, a drive-letter jump, or a UNC path (`\\\\host\\share`)
    all raise before any ``Path`` is ever touched on disk.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")

    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_LETTER.match(normalized):
        raise ValueError(f"{field} must be a path relative to the repository root: {value!r}")

    segments = normalized.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"{field} must not escape the repository root: {value!r}")
    return value

@dataclass(frozen=True)
class RecipeDefinition:
    INPUT_OUTPUT_TYPES = (
        "hwpx", "hwp", "pdf", "docx", "xls", "xlsx", "zip",
        "jpg", "jpeg", "png", "gif", "csv", "json", "code",
    )

    recipe_id: str
    description: str
    executor: str
    input_type: str
    output_type: str
    template: str | None = None
    version: int = 1
    scope: str = "legacy"
    steps: tuple[dict[str, Any], ...] = ()
    required_conditions: tuple[str, ...] = ()
    exclusion_conditions: tuple[str, ...] = ()
    success_checks: tuple[str, ...] = ()
    approval_scope: str | None = None
    applicability: dict[str, Any] | None = None
    recognition: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    error_policy: dict[str, Any] | None = None
    ai_policy: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecipeDefinition":
        if not isinstance(data, dict):
            raise ValueError("recipe must be an object")

        unknown = set(data) - _FIELDS
        if unknown:
            raise ValueError(f"unknown recipe field(s): {sorted(unknown)}")

        recipe_id = _required(data, "recipe_id")
        if not isinstance(recipe_id, str) or not recipe_id.strip():
            raise ValueError("recipe_id must be a non-empty string")

        description = _required(data, "description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")

        executor = _one_of(data, "executor", tuple(EXECUTORS))
        input_type = _one_of(data, "input_type", cls.INPUT_OUTPUT_TYPES)
        output_type = _one_of(data, "output_type", cls.INPUT_OUTPUT_TYPES)
        version = data.get("version", 1)
        if type(version) is not int or version < 1:
            raise ValueError("version must be a positive integer")
        steps = data.get("steps", ())
        if not isinstance(steps, (list, tuple)) or any(not isinstance(step, dict) for step in steps):
            raise ValueError("steps must be a list of objects")
        for step in steps:
            if set(step) != {"operation", "parameters"}:
                raise ValueError("each step must contain operation and parameters")
            if not isinstance(step["operation"], str) or not step["operation"].strip() or not isinstance(step["parameters"], dict):
                raise ValueError("recipe step is invalid")

        def _text_tuple(key: str) -> tuple[str, ...]:
            value = data.get(key, ())
            if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"{key} must be a list of text")
            return tuple(item.strip() for item in value)

        template = data.get("template")
        if template is not None:
            template = _validate_repo_relative_path(template, field="template")

        mappings: dict[str, dict[str, Any] | None] = {}
        for key in ("applicability", "recognition", "execution", "verification", "error_policy", "ai_policy"):
            value = data.get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            mappings[key] = dict(value) if value is not None else None

        approval_scope = data.get("approval_scope")
        if approval_scope is not None and (not isinstance(approval_scope, str) or not approval_scope.strip()):
            raise ValueError("approval_scope must be non-empty when present")
        scope = _required(data, "scope")
        if not isinstance(scope, str) or _SAFE_SCOPE.fullmatch(scope) is None:
            raise ValueError("scope must be a safe lowercase identifier")
        return cls(
            recipe_id=recipe_id,
            description=description,
            executor=executor,
            input_type=input_type,
            output_type=output_type,
            template=template,
            version=version,
            scope=scope,
            steps=tuple(dict(step) for step in steps),
            required_conditions=_text_tuple("required_conditions"),
            exclusion_conditions=_text_tuple("exclusion_conditions"),
            success_checks=_text_tuple("success_checks"),
            approval_scope=approval_scope,
            **mappings,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "recipe_id": self.recipe_id,
            "description": self.description,
            "executor": self.executor,
            "input_type": self.input_type,
            "output_type": self.output_type,
            "scope": self.scope,
        }
        if self.template is not None:
            payload["template"] = self.template
        if self.version != 1:
            payload["version"] = self.version
        if self.steps:
            payload["steps"] = [dict(step) for step in self.steps]
        for key, value in (
            ("required_conditions", self.required_conditions),
            ("exclusion_conditions", self.exclusion_conditions),
            ("success_checks", self.success_checks),
        ):
            if value:
                payload[key] = list(value)
        if self.approval_scope is not None:
            payload["approval_scope"] = self.approval_scope
        for key in ("applicability", "recognition", "execution", "verification", "error_policy", "ai_policy"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = dict(value)
        return payload


def recipe_digest(recipe: RecipeDefinition) -> str:
    payload = json.dumps(
        recipe.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _runtime_recipe_data(root: Path | str, scope: str) -> dict[str, dict[str, Any]]:
    if not isinstance(scope, str) or _SAFE_SCOPE.fullmatch(scope) is None:
        raise ValueError("runtime recipe scope must be a safe lowercase identifier")
    raw: dict[str, dict[str, Any]] = {}
    registry_root = Path(root) / "state" / "recipes" / scope
    for pointer_path in sorted(registry_root.glob("*/active.json")):
        active = json.loads(pointer_path.read_text(encoding="utf-8"))
        required = {
            "schema_version",
            "scope",
            "recipe_id",
            "version",
            "definition_sha256",
            "capability",
            "activation",
        }
        if not isinstance(active, dict) or set(active) != required:
            raise ValueError("invalid active recipe pointer")
        recipe_id = pointer_path.parent.name
        if (
            active["schema_version"] != 1
            or active["scope"] != scope
            or active["recipe_id"] != recipe_id
            or type(active["version"]) is not int
            or active["version"] < 1
        ):
            raise ValueError("active recipe identity or version is invalid")
        version_path = pointer_path.parent / f"{active['version']}.json"
        if not version_path.is_file() or not version_path.resolve().is_relative_to(
            pointer_path.parent.resolve()
        ):
            raise ValueError("active recipe definition is unavailable")
        data = json.loads(version_path.read_text(encoding="utf-8"))
        recipe = RecipeDefinition.from_dict(data)
        if recipe.recipe_id != recipe_id or recipe.scope != scope:
            raise ValueError("active recipe definition identity is invalid")
        if recipe_digest(recipe) != active["definition_sha256"]:
            raise ValueError("active recipe definition digest mismatch")
        raw[recipe_id] = data
    return raw


def load_recipes(
    path: Path | str = DEFAULT_RECIPES_DIR,
    *,
    recipes: dict[str, dict] | None = None,
    root: Path | str | None = None,
    scope: str | None = None,
) -> dict[str, RecipeDefinition]:
    """Load the shipped registry or one scope's immutable runtime registry."""
    if root is not None:
        if scope is None:
            raise ValueError("scope is required for runtime recipe lookup")
        raw = _runtime_recipe_data(root, scope)
    elif recipes is None:
        raw: dict[str, Any] = {}
        recipes_dir = Path(path)
        if recipes_dir.is_dir():
            for file in sorted(recipes_dir.glob("*.json")):
                raw[file.stem] = json.loads(file.read_text(encoding="utf-8"))
    else:
        raw = recipes

    registry: dict[str, RecipeDefinition] = {}
    for recipe_id, data in raw.items():
        recipe = RecipeDefinition.from_dict(data)
        if recipe.recipe_id != recipe_id:
            raise ValueError(
                f"recipe id mismatch: key {recipe_id!r} defines recipe_id {recipe.recipe_id!r}"
            )
        if scope is None or recipe.scope == scope:
            registry[recipe_id] = recipe
    return registry


def get_recipe(
    recipe_id: str,
    *,
    recipes: dict[str, dict] | None = None,
    root: Path | str | None = None,
    scope: str | None = None,
) -> RecipeDefinition:
    registry = load_recipes(recipes=recipes, root=root, scope=scope)
    try:
        return registry[recipe_id]
    except KeyError as exc:
        raise UnknownRecipeError(f"unknown recipe id: {recipe_id}") from exc
