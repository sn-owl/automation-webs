"""Static capability registry, declaration validation and execution boundary."""
from pathlib import Path
import hashlib
import json
from automation.recipes import get_recipe
from automation.executors.excel import execute_excel, source_digest as excel_digest, validate_excel_input
from automation.executors.restarea import execute_restarea, source_digest as restarea_digest, validate_restarea_input, validate_restarea_plan
from automation.executors.code_analysis import execute_code_analysis, source_digest as code_digest, validate_code_input

EXECUTOR_DISPATCH = {'excel-table': execute_excel, 'restarea-converter': execute_restarea, 'code-analysis': execute_code_analysis}
INPUT_DIGEST_DISPATCH = {'excel-table': excel_digest, 'restarea-converter': restarea_digest, 'code-analysis': code_digest}
INPUT_VALIDATION_DISPATCH = {'excel-table': validate_excel_input, 'restarea-converter': validate_restarea_input, 'code-analysis': validate_code_input}

_REPO_ROOT = Path(__file__).resolve().parents[1]


class DispatchError(RuntimeError):
    """Execution boundary failure, carrying a message-free ``diagnosis``."""

    def __init__(self, message, diagnosis=None):
        super().__init__(message)
        self.diagnosis = diagnosis


def safe_diagnosis(stage, exc):
    """Return a structured, message-free account of why a dispatch failed.

    Exception text can carry local paths, credentials or document content, so
    it is never recorded -- the same policy `run_pipeline._safe_error` applies
    to events. But a bare type name ("DispatchError") tells an operator
    nothing they can act on, which is how a failed recipe test became a dead
    end. The type chain plus the innermost frame inside this repository says
    WHERE to look without saying what the data was. Frames outside the
    repository (COM proxies, third-party libraries) are skipped because their
    line numbers are not addressable by the reader; the innermost frame of any
    kind is the fallback so the field is never empty.
    """
    chain = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__

    innermost = None
    in_repo = None
    traceback = exc.__traceback__
    while traceback is not None:
        path = Path(traceback.tb_frame.f_code.co_filename)
        location = f"{path.name}:{traceback.tb_lineno}"
        try:
            # A synthetic frame name (COM proxies report "<COMObject ...>")
            # still resolves against the working directory, so require a real
            # file before believing the frame is ours.
            resolved = path.resolve()
            if resolved.is_file():
                location = f"{resolved.relative_to(_REPO_ROOT).as_posix()}:{traceback.tb_lineno}"
                in_repo = location
        except (ValueError, OSError):
            pass
        innermost = location
        traceback = traceback.tb_next

    return {"stage": stage, "cause_chain": chain, "origin": in_repo or innermost}


class AIPolicyError(DispatchError):
    """Raised when a recipe's declared AI policy cannot be safely executed."""

    def __init__(self, code, message, **details):
        self.code = code
        self.details = details
        super().__init__(message)


def validate_ai_execution_policy(recipe):
    """Fail closed on declared AI work before an executor can create output."""
    ai = recipe.ai_policy or {}
    allowed = {"enabled", "max_calls", "expected_calls", "model_projection"}
    unsupported = set(ai) - allowed
    if unsupported:
        raise AIPolicyError(
            "invalid_ai_policy",
            "unsupported AI policy fields",
            fields=sorted(unsupported),
        )

    enabled = ai.get("enabled", False)
    expected_calls = ai.get("expected_calls", 0)
    max_calls = ai.get("max_calls", 0)
    if type(enabled) is not bool:
        raise AIPolicyError("invalid_ai_policy", "AI enabled must be boolean")
    if any(type(value) is not int or value < 0 for value in (expected_calls, max_calls)):
        raise AIPolicyError(
            "invalid_ai_policy",
            "AI call limits must be nonnegative integers",
        )
    if expected_calls > max_calls:
        raise AIPolicyError(
            "ai_call_limit_exceeded",
            "declared AI call limit is below expected calls",
            enabled=enabled,
            expected_calls=expected_calls,
            max_calls=max_calls,
        )
    if enabled or expected_calls:
        raise AIPolicyError(
            "ai_capability_unavailable",
            "reviewed AI execution capability is unavailable",
            enabled=enabled,
            expected_calls=expected_calls,
            max_calls=max_calls,
        )


def capability_binding(recipe):
    if recipe.executor not in EXECUTOR_DISPATCH:
        raise ValueError('unsupported capability')
    directory = Path(__file__).parent
    names = {'excel-table': ['executors/excel.py'], 'code-analysis': ['executors/code_analysis.py', 'preparation_plan.py', 'project_profile.py'], 'restarea-converter': ['executors/restarea.py', '../restarea-converter/converter.py']}[recipe.executor]
    digest = hashlib.sha256()
    for name in [*names, 'dispatch.py', 'verification.py']:
        digest.update((directory / name).read_bytes())
    return {'executor': recipe.executor, 'version': 2, 'implementation_digest': digest.hexdigest()}


def _text_list(value, field, *, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{field} must be a {'list' if allow_empty else 'non-empty list'} of text")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} must be a list of non-empty text")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicates")
    return value


def validate_recipe_plan(recipe):
    if recipe.executor not in EXECUTOR_DISPATCH:
        raise ValueError("capability_gap: unsupported executor")
    if not recipe.scope or not isinstance(recipe.scope, str):
        raise ValueError("recipe scope is required")
    for policy, allowed in (
        (recipe.error_policy, {"on_missing_input", "on_parse_error", "on_validation_error"}),
        (recipe.ai_policy, {"enabled", "max_calls", "expected_calls", "model_projection"}),
    ):
        if policy and set(policy) - allowed:
            raise ValueError("unsupported policy fields")
    if recipe.error_policy and any(value not in {"blocked", "stop"} for value in recipe.error_policy.values()):
        raise ValueError("capability_gap: partial output and retry policies are unsupported")
    ai = recipe.ai_policy or {}
    if type(ai.get("enabled", False)) is not bool:
        raise ValueError("AI enabled must be boolean")
    for name in ("max_calls", "expected_calls"):
        if type(ai.get(name, 0)) is not int or ai.get(name, 0) < 0:
            raise ValueError("AI call limits must be nonnegative integers")
    if ai.get("enabled") or ai.get("expected_calls", 0):
        raise ValueError("capability_gap: installed preparation capabilities are deterministic; AI execution requires reviewed capability development")
    if recipe.executor == "restarea-converter":
        validate_restarea_plan(recipe)
        return
    if not recipe.steps:
        raise ValueError("steps are required")
    if recipe.executor == "code-analysis":
        if [step["operation"] for step in recipe.steps] != ["analyze_code"]:
            raise ValueError("code analysis supports one bounded analyze_code operation")
        if recipe.steps[0]["parameters"]:
            raise ValueError("code analysis parameters belong to the registered ProjectProfile")
        execution = recipe.execution or {}
        if set(execution) != {"project_id"}:
            raise ValueError("code analysis execution must name only one registered project_id")
        if not isinstance(execution["project_id"], str) or not execution["project_id"]:
            raise ValueError("registered project_id is required")
        if recipe.input_type != "code" or recipe.output_type != "json":
            raise ValueError("code analysis requires code input and structured JSON output")
        return
    if recipe.input_type not in {"csv", "xls", "xlsx"} or recipe.output_type not in {"csv", "json", "xls", "xlsx"}:
        raise ValueError("unsupported spreadsheet type")
    allowed = {
        "select_attachment": {"type", "count"},
        "parse_table": {"sheet", "required_headers"},
        "map_columns": {"output_columns", "columns", "rename"},
        "filter_rows": {"column", "equals", "gt", "lt"},
        "sort_rows": {"column", "descending", "numeric"},
        "write_workbook": {"output_type"},
        "verify_output": {"checks"},
    }
    phase = "input"
    for step in recipe.steps:
        operation, params = step["operation"], step["parameters"]
        if operation not in allowed or set(params) - allowed[operation]:
            raise ValueError(f"capability_gap: unsupported operation or parameters: {operation}")
        if operation == "select_attachment":
            if phase != "input" or params.get("type") != recipe.input_type or params.get("count", 1) != 1:
                raise ValueError("select_attachment requires matching type and count=1")
            phase = "selected"
        elif operation == "parse_table":
            if phase != "selected":
                raise ValueError("parse_table requires selected attachment")
            if "sheet" in params and not (
                type(params["sheet"]) is int and params["sheet"] >= 0
                or isinstance(params["sheet"], str) and params["sheet"]
            ):
                raise ValueError("sheet must be index or name")
            if "required_headers" in params:
                _text_list(params["required_headers"], "required_headers")
            phase = "table"
        elif operation in {"map_columns", "filter_rows", "sort_rows"}:
            if phase != "table":
                raise ValueError("table transformation requires parsed table")
            if operation == "map_columns":
                columns = params.get("output_columns", params.get("columns"))
                _text_list(columns, "mapping columns")
                rename = params.get("rename", {})
                if not isinstance(rename, dict) or any(
                    not isinstance(key, str) or not key
                    or not isinstance(value, str) or not value
                    for key, value in rename.items()
                ):
                    raise ValueError("rename must map non-empty text column names")
                if set(rename) - set(columns):
                    raise ValueError("rename refers to unselected column")
                output_columns = [rename.get(column, column) for column in columns]
                if len(set(output_columns)) != len(output_columns):
                    raise ValueError("map_columns output names must be unique")
            elif not isinstance(params.get("column"), str) or not params["column"]:
                raise ValueError("filter/sort column is required")
            if operation == "filter_rows":
                comparisons = [key for key in ("equals", "gt", "lt") if key in params]
                if len(comparisons) != 1:
                    raise ValueError("filter requires exactly one comparison")
                value = params[comparisons[0]]
                if value is None or isinstance(value, (dict, list)):
                    raise ValueError("filter comparison must be a scalar value")
                if comparisons[0] in {"gt", "lt"}:
                    try:
                        float(value)
                    except (TypeError, ValueError):
                        raise ValueError("numeric filter comparison must be numeric") from None
            if operation == "sort_rows" and any(type(params.get(key, False)) is not bool for key in ("descending", "numeric")):
                raise ValueError("sort options must be boolean")
        elif operation == "write_workbook":
            if phase != "table" or params.get("output_type", recipe.output_type) != recipe.output_type:
                raise ValueError("output declaration must match recipe output type")
            phase = "written"
        elif operation == "verify_output":
            if phase != "written":
                raise ValueError("verification requires written output")
            if "checks" in params and not isinstance(params["checks"], list):
                raise ValueError("verification checks must be a list")
            phase = "verified"
    if phase != "verified":
        raise ValueError("plan must produce and verify an output")
    from automation.executors.excel import verification_rules
    verification_rules(recipe)


def _state_kwargs(recipe, root):
    return {"state_root": root} if recipe.executor == "code-analysis" else {}


def canonical_input_hash(recipe_id, item, *, recipe_definition=None, root=None):
    recipe = recipe_definition or get_recipe(
        recipe_id,
        root=root,
        scope=item.scope if root is not None else None,
    )
    return INPUT_DIGEST_DISPATCH[recipe.executor](
        item,
        recipe_definition=recipe,
        **_state_kwargs(recipe, root),
    )


def validate_recipe_input(recipe_id, item, *, recipe_definition=None, root=None):
    recipe = recipe_definition or get_recipe(
        recipe_id,
        root=root,
        scope=item.scope if root is not None else None,
    )
    return INPUT_VALIDATION_DISPATCH[recipe.executor](
        item,
        recipe_definition=recipe,
        **_state_kwargs(recipe, root),
    )


def dispatch_recipe(
    recipe_id,
    item,
    artifact_dir,
    *,
    seen_execution_keys=(),
    recipe_definition=None,
    root=None,
    execution_key=None,
):
    # Each phase is wrapped separately so the recorded `stage` says whether the
    # recipe could not be found, its plan was refused, or the executor itself
    # failed -- three very different next actions for the operator.
    stage = "recipe_lookup"
    try:
        recipe = recipe_definition or get_recipe(
            recipe_id,
            root=root,
            scope=item.scope if root is not None else None,
        )
        stage = "plan_validation"
        validate_recipe_plan(recipe)
        stage = "executor"
        executor_kwargs = {
            "seen_execution_keys": seen_execution_keys,
            "recipe_id": recipe_id,
            "recipe_definition": recipe,
            "execution_key": execution_key,
            **_state_kwargs(recipe, root),
        }
        return EXECUTOR_DISPATCH[recipe.executor](
            item,
            artifact_dir,
            **executor_kwargs,
        )
    except Exception as exc:
        # `from None` stays: a chained traceback would put exception text back
        # into whatever logs this. The diagnosis carries the same information
        # in a form that is safe to store.
        raise DispatchError(
            f"recipe execution failed: {recipe_id} ({type(exc).__name__})",
            safe_diagnosis(stage, exc),
        ) from None
