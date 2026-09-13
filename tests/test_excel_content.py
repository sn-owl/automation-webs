import tempfile
from dataclasses import replace
import unittest
import zipfile
from pathlib import Path

from automation.executors.excel import (
    _write_xlsx,
    _xlsx_rows,
    execute_excel,
    verify_excel_output,
)
from automation.models import WorkItem
from automation.dispatch import validate_recipe_plan
from automation.recipes import get_recipe


def _item(src: Path) -> WorkItem:
    return WorkItem.from_dict(
        {
            "task_id": "t-1",
            "source": {"type": "manual", "id": "x", "external_id": "x", "url": "https://fixture.local/x"},
            "received_at": "2026-01-01T00:00:00Z",
            "title": "t",
            "body": "b",
            "mask_table_ref": "none",
            "attachments": [
                {
                    "name": "in.xlsx",
                    "type": "xlsx",
                    "raw_ref": str(src),
                    "extracted_ref": str(src),
                    "sha256": "0" * 64,
                }
            ],
        }
    )


class ExcelContentTest(unittest.TestCase):
    def test_sparse_inline_cells_preserve_meaning_through_execution(self):
        # A1/B1/C1 headers; B2 absent and C2=99. Inline strings are standard OOXML.
        sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>name</t></is></c><c r="B1" t="inlineStr"><is><t>middle</t></is></c><c r="C1" t="inlineStr"><is><t>last</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>alpha</t></is></c><c r="C2"><v>99</v></c></row></sheetData></worksheet>'

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            template = directory / "template.xlsx"
            source = directory / "source.xlsx"
            _write_xlsx(template, [["unused"]])
            with zipfile.ZipFile(template) as original, zipfile.ZipFile(source, "x") as target:
                for entry in original.infolist():
                    target.writestr(entry, sheet if entry.filename == "xl/worksheets/sheet1.xml" else original.read(entry.filename))
            original_bytes = source.read_bytes()
            recipe = replace(get_recipe("excel-table-v1"), steps=({"operation": "map_columns", "parameters": {"output_columns": ["middle", "name", "last"]}},))
            result = execute_excel(_item(source), directory / "out", recipe_definition=recipe)
            artifact = result["artifacts"][0]["path"]
            self.assertEqual(_xlsx_rows(Path(artifact)), [["middle", "name", "last"], ["", "alpha", "99"]])
            self.assertEqual(verify_excel_output(_item(source), artifact, recipe_definition=recipe)["status"], "passed")
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_workbook_writer_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.xlsx"
            _write_xlsx(path, [["original"]])
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                _write_xlsx(path, [["replacement"]])
            self.assertEqual(path.read_bytes(), before)

    def test_written_xlsx_round_trips_through_own_reader(self):
        # A08: shared strings, not inline strings, so the project's own reader
        # (and Excel/openpyxl) can read the text back instead of empty cells.
        rows = [["이름", "값"], ["가", "10"], ["나", "20"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.xlsx"
            _write_xlsx(path, rows)
            self.assertEqual(_xlsx_rows(path), rows)

    def test_verification_rejects_a_text_file_renamed_xlsx(self):
        # A08: existence is not enough -- verification must read real content.
        recipe = get_recipe("excel-table-v1")
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "in.xlsx"
            _write_xlsx(source, [["a", "b"], ["1", "2"]])
            item = _item(source)
            result = execute_excel(item, directory / "out", recipe_id="excel-table-v1", recipe_definition=recipe)
            artifact = result["artifacts"][0]["path"]
            self.assertEqual(verify_excel_output(item, artifact, recipe_definition=recipe)["status"], "passed")

            fake = Path(artifact).with_name("fake.xlsx")
            fake.write_text("this is not a spreadsheet")
            self.assertEqual(verify_excel_output(item, fake, recipe_definition=recipe)["status"], "failed")

    def test_plan_validation_rejects_invalid_headers_and_duplicate_output_names(self):
        recipe = get_recipe("excel-table-v1")
        steps = list(recipe.steps)
        steps[1] = {"operation": "parse_table", "parameters": {"required_headers": "name"}}
        with self.assertRaisesRegex(ValueError, "required_headers"):
            validate_recipe_plan(replace(recipe, steps=tuple(steps)))

        steps = list(recipe.steps)
        steps.insert(
            2,
            {
                "operation": "map_columns",
                "parameters": {
                    "output_columns": ["name", "middle"],
                    "rename": {"middle": "name"},
                },
            },
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_recipe_plan(replace(recipe, steps=tuple(steps)))

    def test_plan_validation_rejects_wrong_verification_value_types(self):
        recipe = get_recipe("excel-table-v1")
        invalid_checks = (
            {"name": "columns", "value": "name"},
            {"name": "min_rows", "value": True},
            {"name": "required_values", "value": ["name", "name"]},
            {"name": "artifact_exists", "value": True},
        )
        for check in invalid_checks:
            with self.subTest(check=check):
                with self.assertRaises(ValueError):
                    validate_recipe_plan(replace(recipe, verification={"checks": [check]}))

    def test_required_values_declaration_changes_verification_result(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "in.xlsx"
            _write_xlsx(source, [["name", "value"], ["alpha", ""]])
            item = _item(source)
            recipe = get_recipe("excel-table-v1")
            result = execute_excel(item, directory / "out", recipe_definition=recipe)
            artifact = result["artifacts"][0]["path"]
            self.assertEqual(
                verify_excel_output(item, artifact, recipe_definition=recipe)["status"],
                "passed",
            )

            required_value_recipe = replace(
                recipe,
                verification={
                    "checks": [{"name": "required_values", "value": ["value"]}]
                },
            )
            self.assertEqual(
                verify_excel_output(
                    item,
                    artifact,
                    recipe_definition=required_value_recipe,
                )["status"],
                "failed",
            )

if __name__ == "__main__":
    unittest.main()
