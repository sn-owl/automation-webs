"""Allow-listed, deterministic spreadsheet preparation capability."""

from __future__ import annotations

import csv
import hashlib
import json
import posixpath
import re
from pathlib import Path
from typing import Any
import zipfile
from xml.etree import ElementTree


from automation import identity
from automation.models import WorkItem

_ALLOWED = {"xls", "xlsx", "csv"}
MAX_WORKBOOK_BYTES = 32 * 1024 * 1024
MAX_WORKBOOK_ROWS = 100_000
MAX_WORKBOOK_COLUMNS = 1_024
MAX_WORKBOOK_CELLS = 1_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attachment(item: WorkItem, input_type: str | None = None) -> Path:
    values = [a for a in item.attachments if input_type is None or a.type == input_type]
    if len(values) != 1:
        raise ValueError("exactly one declared spreadsheet attachment is required")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", values[0].raw_ref) or values[0].raw_ref.startswith(("\\\\", "//")):
        raise ValueError("spreadsheet must be a local file")
    path = Path(values[0].raw_ref)
    if not path.is_file() or values[0].type not in _ALLOWED:
        raise ValueError("spreadsheet attachment is unavailable or unsupported")
    return path

def source_digest(item: WorkItem, recipe_definition=None) -> str:
    input_type = getattr(recipe_definition, "input_type", None) if recipe_definition is not None else None
    return _sha256(_attachment(item, input_type))


def _xlsx_rows(path: Path, sheet: str | int = 0) -> list[list[str]]:
    """Read bounded worksheet values, preserving sparse cell coordinates."""
    if path.stat().st_size > MAX_WORKBOOK_BYTES:
        raise ValueError("workbook exceeds size limit")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 10_000 or sum(info.file_size for info in infos) > MAX_WORKBOOK_BYTES:
            raise ValueError("workbook decompressed size exceeds limit")
        if len({info.filename for info in infos}) != len(infos):
            raise ValueError("duplicate workbook members")

        def xml(name: str):
            raw = archive.read(name)
            if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw, re.IGNORECASE):
                raise ValueError("DTD and entity declarations are not supported")
            return ElementTree.fromstring(raw)

        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [
                "".join(node.text or "" for node in entry.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                for entry in xml("xl/sharedStrings.xml")
                if entry.tag.rsplit("}", 1)[-1] == "si"
            ]
        workbook = xml("xl/workbook.xml")
        sheets = [node for node in workbook.iter() if node.tag.rsplit("}", 1)[-1] == "sheet"]
        if isinstance(sheet, str):
            chosen = next((node for node in sheets if node.get("name") == sheet), None)
        elif type(sheet) is int and 0 <= sheet < len(sheets):
            chosen = sheets[sheet]
        else:
            chosen = None
        if chosen is None:
            raise ValueError("declared worksheet not found")
        relationship = chosen.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        links = xml("xl/_rels/workbook.xml.rels")
        link = next((node for node in links if node.get("Id") == relationship), None)
        if link is None or link.get("TargetMode") == "External":
            raise ValueError("worksheet relationship is missing or external")
        target = link.get("Target", "")
        member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if not member.startswith("xl/worksheets/") or "\\" in member:
            raise ValueError("worksheet path escapes workbook")
        rows: list[list[str]] = []
        width = 0
        allocated = 0
        for row in xml(member).iter():
            if row.tag.rsplit("}", 1)[-1] != "row":
                continue
            row_number = int(row.get("r", len(rows) + 1))
            if row_number <= len(rows) or row_number > MAX_WORKBOOK_ROWS:
                raise ValueError("worksheet row coordinates invalid or too large")
            while len(rows) < row_number:
                rows.append([])
            values = rows[-1]
            for cell in row:
                if cell.tag.rsplit("}", 1)[-1] != "c":
                    continue
                coordinate = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell.get("r", ""))
                if coordinate is None or int(coordinate[2]) != row_number:
                    raise ValueError("invalid worksheet cell coordinate")
                column = 0
                for letter in coordinate[1]:
                    column = column * 26 + ord(letter) - ord("A") + 1
                if column <= len(values) or column > MAX_WORKBOOK_COLUMNS:
                    raise ValueError("worksheet column coordinates invalid or too large")
                allocated += column - len(values)
                if allocated > MAX_WORKBOOK_CELLS:
                    raise ValueError("worksheet cell count exceeds limit")
                values.extend([""] * (column - len(values)))
                kind = cell.get("t", "n")
                value = next((child.text or "" for child in cell if child.tag.rsplit("}", 1)[-1] == "v"), "")
                if kind == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                elif kind == "s":
                    if not value.isdigit() or int(value) >= len(shared):
                        raise ValueError("invalid shared-string reference")
                    value = shared[int(value)]
                elif kind == "e":
                    raise ValueError("worksheet contains an error cell")
                elif kind not in {"n", "str", "b", "d"}:
                    raise ValueError("unsupported worksheet cell type")
                if any(child.tag.rsplit("}", 1)[-1] == "f" for child in cell):
                    raise ValueError("formula preservation requires an explicit supported capability")
                values[column - 1] = value
            width = max(width, len(values))
        if len(rows) * width > MAX_WORKBOOK_CELLS:
            raise ValueError("worksheet dimensions exceed cell limit")
        for values in rows:
            values.extend([""] * (width - len(values)))
        return rows


def _write_xlsx(path: Path, rows: list[list[str]]) -> None:
    from xml.sax.saxutils import escape

    # Shared strings, not inline strings: the project's own readers
    # (_xlsx_rows, _preview_xlsx) and Excel/openpyxl all resolve t="s"/<v>.
    # inlineStr round-tripped to empty cells through those readers.
    shared_index: dict[str, int] = {}
    shared_order: list[str] = []

    def _intern(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared_order)
            shared_order.append(value)
        return shared_index[value]

    sheet_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = []
        for column_number, value in enumerate(row, 1):
            name = ""
            index = column_number
            while index:
                index, remainder = divmod(index - 1, 26)
                name = chr(65 + remainder) + name
            cells.append(f'<c r="{name}{row_number}" t="s"><v>{_intern(str(value))}</v></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{sum(len(row) for row in rows)}" uniqueCount="{len(shared_order)}">'
        + "".join(f"<si><t xml:space=\"preserve\">{escape(text)}</t></si>" for text in shared_order)
        + '</sst>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '</Types>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", shared_strings)


def _rows(path: Path, input_type: str, sheet: str | int = 0) -> list[list[str]]:
    if path.stat().st_size > MAX_WORKBOOK_BYTES:
        raise ValueError("spreadsheet exceeds size limit")
    if input_type == "csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = []
            for row in csv.reader(stream):
                if len(rows) >= MAX_WORKBOOK_ROWS or len(row) > MAX_WORKBOOK_COLUMNS:
                    raise ValueError("CSV dimensions exceed limit")
                rows.append(list(row))
            if sum(map(len, rows)) > MAX_WORKBOOK_CELLS:
                raise ValueError("CSV cell count exceeds limit")
            return rows
    if input_type == "xlsx":
        return _xlsx_rows(path, sheet)
    if input_type == "xls":
        import xlrd
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        try:
            table = workbook.sheet_by_name(sheet) if isinstance(sheet, str) else workbook.sheet_by_index(sheet)
            if table.nrows > MAX_WORKBOOK_ROWS or table.ncols > MAX_WORKBOOK_COLUMNS or table.nrows * table.ncols > MAX_WORKBOOK_CELLS:
                raise ValueError("XLS dimensions exceed limit")
            return [[str(int(v)) if isinstance(v, float) and v.is_integer() else str(v) for v in table.row_values(row)] for row in range(table.nrows)]
        finally:
            workbook.release_resources()
    raise ValueError("unsupported spreadsheet input type")


def _source_rows(item, recipe):
    source = _attachment(item, getattr(recipe, "input_type", None))
    parse = next((step["parameters"] for step in getattr(recipe, "steps", ()) if step["operation"] == "parse_table"), {})
    rows = _rows(source, getattr(recipe, "input_type", None) or source.suffix[1:].lower(), parse.get("sheet", 0))
    if not rows or not rows[0] or len(set(rows[0])) != len(rows[0]) or any(not str(column).strip() for column in rows[0]):
        raise ValueError("spreadsheet requires nonempty unique column names")
    required_headers = parse.get("required_headers", [])
    if not isinstance(required_headers, list) or any(type(header) is not str for header in required_headers):
        raise ValueError("required_headers must be a list of strings")
    if any(column not in rows[0] for column in required_headers):
        raise ValueError("required input headers missing")
    return rows

def _apply_steps(rows: list[list[str]], steps: tuple[dict[str, Any], ...]) -> list[list[str]]:
    result = [list(row) for row in rows]
    if not result or not result[0]:
        raise ValueError("spreadsheet requires a header row")
    header = result[0]
    for step in steps:
        operation = step["operation"]
        params = step["parameters"]
        if operation == "select_attachment" or operation == "parse_table":
            continue
        if operation == "map_columns":
            columns = params.get("output_columns") or params.get("columns")
            if not isinstance(columns, list) or not columns:
                raise ValueError("map_columns requires output_columns")
            if len(set(columns)) != len(columns):
                raise ValueError("map_columns source columns must be unique")
            if any(name not in header for name in columns):
                raise ValueError("map_columns source column is not present")
            indexes = [header.index(name) for name in columns]
            result = [[row[index] if index < len(row) else "" for index in indexes] for row in result]
            rename = params.get("rename", {})
            if any(name not in columns for name in rename):
                raise ValueError("rename refers to unselected column")
            output_header = [rename.get(name, name) for name in columns]
            if len(set(output_header)) != len(output_header):
                raise ValueError("mapping output columns must be unique")
            result[0] = output_header
            header = result[0]
        elif operation == "filter_rows":
            column = params.get("column")
            comparison = next((key for key in ("equals", "gt", "lt") if key in params), None)
            expected = params.get(comparison)
            if column not in header:
                raise ValueError("filter column is not present")
            index = header.index(column)
            def matches(row):
                if index >= len(row):
                    return False
                if comparison == "equals":
                    return row[index] == str(expected)
                if comparison == "gt":
                    return float(row[index]) > float(expected)
                if comparison == "lt":
                    return float(row[index]) < float(expected)
                raise ValueError("unsupported filter comparison")
            try:
                result = [result[0]] + [row for row in result[1:] if matches(row)]
            except (TypeError, ValueError) as exc:
                raise ValueError("filter comparison requires numeric values") from exc
        elif operation == "sort_rows":
            column = params.get("column")
            if column not in header:
                raise ValueError("sort column is not present")
            index = header.index(column)
            def order(row):
                value = row[index] if index < len(row) else ""
                return float(value) if params.get("numeric", False) else value
            try:
                result = [result[0]] + sorted(result[1:], key=order, reverse=params.get("descending", False))
            except (TypeError, ValueError) as exc:
                raise ValueError("sort comparison requires compatible values") from exc
        elif operation in {"write_workbook", "verify_output"}:
            continue
        else:
            raise ValueError(f"unsupported Excel operation: {operation}")
    return result


def validate_excel_input(item: WorkItem, recipe_definition=None) -> dict[str, Any]:
    input_type = getattr(recipe_definition, "input_type", None) if recipe_definition is not None else None
    attachments = [a for a in item.attachments if input_type is None or a.type == input_type]
    if len(attachments) != 1:
        return {"state": "missing" if not attachments else "ambiguous", "reasons": ["exactly one spreadsheet attachment is required"], "evidence": []}
    try:
        rows = _source_rows(item, recipe_definition)
    except Exception as exc:
        return {"state": "invalid", "reasons": ["spreadsheet could not be read safely"], "evidence": [type(exc).__name__]}
    return {"state": "satisfied", "reasons": [], "evidence": [f"rows={len(rows)}", "local_file=true"]}


def _read_artifact_rows(path: Path, output_type: str) -> list[list[str]]:
    if output_type == "csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [list(row) for row in csv.reader(stream)]
    if output_type == "json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
            raise ValueError("JSON artifact must contain a row array")
        return [list(map(str, row)) for row in value]
    if output_type in {"xlsx", "xls"}:
        return _rows(path, output_type)
    raise ValueError("unsupported spreadsheet output type")


def verification_rules(recipe):
    rules = list((getattr(recipe, "verification", None) or {}).get("checks", []))
    rules.extend(getattr(recipe, "success_checks", ()))
    for step in getattr(recipe, "steps", ()):
        if step["operation"] == "verify_output":
            checks = step["parameters"].get("checks", [])
            if not isinstance(checks, list):
                raise ValueError("verification checks must be a list")
            rules.extend(checks)
    supported = {
        "artifact_exists",
        "sha256",
        "content_matches_source",
        "cell_values",
        "row_count",
        "columns",
        "min_rows",
        "max_rows",
        "required_values",
    }
    valued = {"columns", "min_rows", "max_rows", "required_values"}
    valueless = {"artifact_exists", "sha256", "content_matches_source", "cell_values"}
    for rule in rules:
        name = rule if isinstance(rule, str) else rule.get("name") if isinstance(rule, dict) else None
        if name not in supported:
            raise ValueError(f"unsupported verification check: {name}")
        if isinstance(rule, dict) and set(rule) != {"name", "value"}:
            raise ValueError("verification rule requires name and value")
        if name in valued and not isinstance(rule, dict):
            raise ValueError(f"verification {name} requires an explicit value")
        if name in valueless and isinstance(rule, dict):
            raise ValueError(f"verification {name} does not accept a value")
        target = rule["value"] if isinstance(rule, dict) else None
        if name in {"row_count", "min_rows", "max_rows"} and target is not None:
            if type(target) is not int or target < 0:
                raise ValueError("row count verification requires a nonnegative integer")
        if name in {"columns", "required_values"}:
            if not isinstance(target, list) or not target:
                raise ValueError(f"verification {name} requires a non-empty list")
            if any(not isinstance(column, str) or not column for column in target):
                raise ValueError(f"verification {name} requires non-empty column names")
            if len(set(target)) != len(target):
                raise ValueError(f"verification {name} must not contain duplicates")
    return rules


def _declared_checks(recipe, rows, expected_rows):
    checks = []
    for rule in verification_rules(recipe):
        name = rule if isinstance(rule, str) else rule["name"]
        target = rule.get("value") if isinstance(rule, dict) else None
        if name in {"artifact_exists", "sha256", "content_matches_source", "cell_values"}:
            passed, actual = rows == expected_rows, "source comparison"
        elif name == "row_count":
            target = len(expected_rows) - 1 if target is None else target
            actual = len(rows) - 1
            passed = actual == target
        elif name in {"min_rows", "max_rows"}:
            if type(target) is not int or target < 0:
                raise ValueError("row limit must be a nonnegative integer")
            actual = len(rows) - 1
            passed = actual >= target if name == "min_rows" else actual <= target
        elif name == "columns":
            actual = rows[0] if rows else []
            passed = actual == target
        else:
            if not isinstance(target, list) or any(column not in rows[0] for column in target):
                raise ValueError("required_values must name output columns")
            actual = all(len(row) > rows[0].index(column) and row[rows[0].index(column)].strip() for row in rows[1:] for column in target)
            passed = actual
        checks.append(_verify_check(name, passed=passed, expected=target, actual=actual))
    return checks


def verify_excel_output(item: WorkItem, artifact_path: Path | str, *, recipe_definition=None) -> dict[str, Any]:
    """Verify real content: re-derive expected rows from source and compare.

    Existence is not enough -- a text file renamed .xlsx must not pass. This
    re-reads the artifact through the real parser and checks it equals what the
    recipe steps produce from the source attachment.
    """
    artifact = Path(artifact_path)
    output_type = getattr(recipe_definition, "output_type", "xlsx")
    checks = [_verify_check("artifact_exists", passed=artifact.is_file(), expected=True, actual=artifact.is_file())]
    if not artifact.is_file():
        return {"verification_version": "excel-content-v1", "status": "failed", "checks": checks,
                "artifact_sha256": None, "handoff": {"required": True, "status": "pending"}}
    try:
        actual_rows = _read_artifact_rows(artifact, output_type)
        expected_rows = _apply_steps(_source_rows(item, recipe_definition), getattr(recipe_definition, "steps", ()))
        checks.extend(_declared_checks(recipe_definition, actual_rows, expected_rows))
    except Exception as exc:
        checks.append(_verify_check("readable_spreadsheet", passed=False, expected=True, actual=type(exc).__name__))
        return {"verification_version": "excel-content-v1", "status": "failed", "checks": checks,
                "artifact_sha256": _sha256(artifact), "handoff": {"required": True, "status": "pending"}}
    matches = actual_rows == expected_rows and all(check["passed"] for check in checks)
    checks.append(_verify_check("readable_spreadsheet", passed=True, expected=True, actual=True))
    checks.append(_verify_check("content_matches_source", passed=matches, expected=len(expected_rows), actual=len(actual_rows)))
    return {
        "verification_version": "excel-content-v1",
        "status": "passed" if matches else "failed",
        "checks": checks,
        "artifact_sha256": _sha256(artifact),
        "handoff": {"required": True, "status": "pending"},
    }


def _verify_check(name: str, *, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}


def execute_excel(item: WorkItem, artifact_dir: Path | str, *, seen_execution_keys=(), recipe_id="excel-table", recipe_definition=None, execution_key=None) -> dict[str, Any]:
    recipe = recipe_definition
    input_type = getattr(recipe, "input_type", None)
    source = _attachment(item, input_type)
    input_hash = _sha256(source)
    execution_key = execution_key or identity.execution_key(item.task_id, recipe_id, input_hash)
    if execution_key in seen_execution_keys:
        return {"task_id": item.task_id, "recipe_id": recipe_id, "execution_key": execution_key, "status": "duplicate", "artifacts": []}
    original_rows = _source_rows(item, recipe)
    rows = _apply_steps(original_rows, getattr(recipe, "steps", ()))
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output_type = getattr(recipe, "output_type", "xlsx")
    output = root / f"{execution_key}.{output_type}"
    if output_type == "csv":
        with output.open("x", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(rows)
    elif output_type == "json":
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    elif output_type == "xlsx":
        _write_xlsx(output, rows)
    elif output_type == "xls":
        import xlwt
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Sheet1")
        for row_number, row in enumerate(rows):
            for column_number, value in enumerate(row):
                sheet.write(row_number, column_number, value)
        with output.open("xb") as stream:
            workbook.save(stream)
    else:
        raise ValueError("unsupported spreadsheet output type")
    return {"task_id": item.task_id, "recipe_id": recipe_id, "execution_key": execution_key, "input_sha256": input_hash, "status": "succeeded", "artifacts": [{"path": str(output), "sha256": _sha256(output)}], "preview": rows[:21], "changes": {"input_rows": len(original_rows) - 1, "output_rows": len(rows) - 1, "input_columns": original_rows[0], "output_columns": rows[0]}}
