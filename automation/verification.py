"""Business-result verification for allow-listed recipe artifacts."""

from __future__ import annotations

import hashlib
import json
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from automation.dispatch import DispatchError
from automation.models import WorkItem
from automation.executors.restarea import _source_attachment
from automation.recipes import get_recipe

_REPO_ROOT = Path(__file__).parents[1].resolve()
_CONVERTER_PATH = _REPO_ROOT / "restarea-converter" / "converter.py"


def _load_converter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("restarea_verifier_converter", _CONVERTER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("restarea converter could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_converter = _load_converter()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _check(name: str, *, passed: bool, expected: Any = None, actual: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": "passed" if passed else "failed"}
    if expected is not None:
        result["expected"] = expected
    if actual is not None:
        result["actual"] = actual
    return result


def verify_restarea_output(item: WorkItem, artifact_path: Path | str) -> dict[str, Any]:
    """Compare a generated XLS with the source table without storing cell text."""
    if not isinstance(item, WorkItem):
        raise TypeError("item must be a WorkItem")
    artifact = Path(artifact_path).resolve()
    try:
        source = _source_attachment(item)
        source_rows = _converter.parse_hwpx(source)
        input_sha256 = _sha256(source)
        if not artifact.is_file():
            return {
                "verification_version": "restarea-xls-v1",
                "status": "failed",
                "checks": [_check("artifact_exists", passed=False, expected=True, actual=False)],
                "input_sha256": input_sha256,
                "artifact_sha256": None,
                "handoff": {"required": True, "status": "pending"},
            }

        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        excel = None
        workbook = None
        try:
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            workbook = excel.Workbooks.Open(str(artifact), ReadOnly=True, UpdateLinks=0, AddToMru=False)
            sheet = workbook.Worksheets(1)
            used = sheet.UsedRange
            used_rows = int(used.Rows.Count)
            used_columns = int(used.Columns.Count)
            header = tuple(sheet.Cells(2, column).Value for column in range(1, 9))
            actual_rows = []
            for row in range(3, 3 + len(source_rows)):
                actual_rows.append(
                    tuple(_normalize_cell(sheet.Cells(row, column).Value) for column in range(1, 9))
                )
        finally:
            if workbook is not None:
                workbook.Close(False)
            if excel is not None:
                excel.Quit()
            pythoncom.CoUninitialize()

        expected_header = ("번호", "쉼터명", "주소", "연락처", "해당동", "운영시간", "운영요일", "해당동")
        row_match = len(actual_rows) == len(source_rows) and all(
            actual == expected for actual, expected in zip(actual_rows, source_rows)
        )
        checks = [
            _check("artifact_exists", passed=True, expected=True, actual=True),
            _check("header_mapping", passed=header == expected_header, expected="8-column restarea header", actual="8-column header" if len(header) == 8 else f"{len(header)} columns"),
            _check("row_count", passed=len(actual_rows) == len(source_rows), expected=len(source_rows), actual=len(actual_rows)),
            _check("cell_values", passed=row_match, expected="all source rows", actual="all rows matched" if row_match else "one or more rows differ"),
        ]
        passed = all(check["status"] == "passed" for check in checks)
        return {
            "verification_version": "restarea-xls-v1",
            "status": "passed" if passed else "failed",
            "checks": checks,
            "input_sha256": input_sha256,
            "artifact_sha256": _sha256(artifact),
            "used_rows": used_rows,
            "used_columns": used_columns,
            "handoff": {
                "required": True,
                "status": "pending",
                "next_action": "관리자 페이지에 사람이 XLS를 업로드하고 반영 결과를 확인하세요.",
            },
        }
    except Exception as exc:
        return {
            "verification_version": "restarea-xls-v1",
            "status": "unverifiable",
            "checks": [],
            "error": f"{type(exc).__name__}",
            "input_sha256": None,
            "artifact_sha256": _sha256(artifact) if artifact.is_file() else None,
            "handoff": {"required": True, "status": "pending"},
        }


def verify_code_analysis_result(plan: Any) -> dict[str, Any]:
    """Verify a static-analysis screen result without requiring a report file."""
    passed = (
        isinstance(plan, dict)
        and plan.get("plan_version") == "preparation-v2"
        and plan.get("state") == "ready"
        and plan.get("external_ai", {}).get("used") is False
        and {"read_only", "no_execution"}.issubset(set(plan.get("risk_flags", ())))
    )
    return {
        "verification_version": "code-analysis-plan-v1",
        "status": "passed" if passed else "failed",
        "checks": [
            _check(
                "structured_read_only_plan",
                passed=passed,
                expected=True,
                actual=passed,
            )
        ],
        "handoff": {"required": False, "status": "pending"},
    }


def _verify_code_analysis_report(artifact_path: Path | str) -> dict[str, Any]:
    artifact = Path(artifact_path)
    passed = False
    actual = "missing"
    if artifact.is_file() and artifact.suffix in {".txt", ".md"}:
        try:
            content = artifact.read_text(encoding="utf-8")
            if artifact.suffix == ".txt":
                value = json.loads(content)
                passed = (
                    isinstance(value, dict)
                    and value.get("plan_version") == "preparation-v2"
                    and value.get("state") == "ready"
                    and isinstance(value.get("read_evidence", {}).get("evidence_digest"), str)
                    and len(value["read_evidence"]["evidence_digest"]) == 64
                )
            else:
                passed = (
                    content.startswith("# ")
                    and "- 상태: `ready`" in content
                    and "evidence digest: `" in content
                    and "## 관련 위치" in content
                )
            if passed:
                actual = "valid_report"
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            actual = "unreadable_report"
    return {
        "verification_version": "code-analysis-report-v1",
        "status": "passed" if passed else "failed",
        "checks": [
            _check(
                "structured_report",
                passed=passed,
                expected=True,
                actual=actual,
            )
        ],
        "artifact_sha256": _sha256(artifact) if artifact.is_file() else None,
        "handoff": {"required": False, "status": "pending"},
    }


def verify_recipe_output(
    recipe_id: str,
    item: WorkItem,
    artifact_path: Path | str,
    *,
    recipe_definition=None,
) -> dict[str, Any]:
    """Dispatch verification through the same static capability allow-list."""
    recipe = recipe_definition or get_recipe(recipe_id)
    if recipe.executor == "restarea-converter":
        return verify_restarea_output(item, artifact_path)
    if recipe.executor == "excel-table":
        from automation.executors.excel import verify_excel_output

        return verify_excel_output(item, artifact_path, recipe_definition=recipe)
    if recipe.executor == "code-analysis":
        return _verify_code_analysis_report(artifact_path)
    raise DispatchError(f"recipe has no result verifier: {recipe_id}")
