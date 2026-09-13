from __future__ import annotations

import hashlib
import shutil
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


from automation.models import AttachmentRef, SourceRef, WorkItem


REPO_ROOT = Path(__file__).parents[1]
FIXTURE = next((REPO_ROOT / "file" / "work").glob("*무더위쉼터*.hwpx"), None)


def _work_item(source: Path) -> WorkItem:
    return WorkItem(
        task_id="alpha-13452",
        source=SourceRef(
            type="gnuboard",
            id="alpha",
            external_id="13452",
            url="https://example.invalid/board/13452",
        ),
        received_at="2026-08-31T00:00:00+00:00",
        title="무더위쉼터 현행화 신청서",
        body="첨부파일을 변환합니다.",
        author="PERSON_001",
        attachments=(
            AttachmentRef(
                name=source.name,
                type="hwpx",
                raw_ref=str(source),
                extracted_ref="normalized/alpha-13452/restarea.json",
            ),
        ),
        mask_table_ref="local://masks/alpha-13452.json",
    )


def _fixture_bytes() -> bytes:
    # A minimal valid package for adapter tests; Excel is only exercised by the
    # genuine end-to-end test below.
    def cell(column: int, text: str) -> str:
        return (
            f'<tc><cellAddr colAddr="{column}"/><subList>'
            f"<p><run><t>{text}</t></run></p></subList></tc>"
        )

    headers = ("연번", "해당동", "시설명", "주소", "운영요일", "운영시간", "대표전화", "지도")
    header = "".join(cell(index, value) for index, value in enumerate(headers))
    row = "".join(
        cell(index, value)
        for index, value in enumerate(
            ("1", "거제1동", "테스트 쉼터", "주소", "월~금", "09:00~18:00", "051-000-0000", "")
        )
    )
    section = (
        '<sec xmlns="http://www.hancom.co.kr/hwpml/2011/section">'
        f'<tbl colCnt="8"><tr>{header}</tr><tr>{row}</tr></tbl></sec>'
    ).encode("utf-8")
    with TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.hwpx"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("Contents/section0.xml", section)
        return path.read_bytes()


class RestareaExecutorTest(unittest.TestCase):
    def test_converts_fixture_and_returns_xls_sha256_metadata(self):
        from automation.executors.restarea import execute_restarea

        with TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "fixture.hwpx"
            source.write_bytes(_fixture_bytes())
            artifact_dir = directory / "artifacts"
            item = _work_item(source)

            def fake_convert(source_path, output_path, template):
                shutil.copy2(source_path, output_path)
                return output_path

            with patch("automation.executors.restarea._converter.convert", side_effect=fake_convert):
                result = execute_restarea(item, artifact_dir)

            artifact = result["artifacts"][0]
            self.assertEqual(result["task_id"], item.task_id)
            self.assertEqual(result["recipe_id"], "restarea-hwpx-to-xls")
            self.assertEqual(result["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(artifact["sha256"], hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest())
            self.assertTrue(Path(artifact["path"]).is_file())
            self.assertEqual(Path(artifact["path"]).suffix, ".xls")
            self.assertFalse(result["duplicate"])

    def test_copies_source_before_conversion_and_reports_duplicate_key(self):
        from automation.executors.restarea import execute_restarea

        with TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "fixture.hwpx"
            original = _fixture_bytes()
            source.write_bytes(original)
            item = _work_item(source)
            artifact_dir = directory / "artifacts"
            observed_sources = []

            def fake_convert(source_path, output_path, template):
                observed_sources.append(Path(source_path))
                Path(source_path).write_bytes(b"converter changed its input")
                output_path.write_bytes(b"fake xls")
                return output_path

            with patch("automation.executors.restarea._converter.convert", side_effect=fake_convert):
                first = execute_restarea(item, artifact_dir)
                second = execute_restarea(
                    item,
                    artifact_dir,
                    seen_execution_keys={first["execution_key"]},
                )

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(observed_sources), 1)
            self.assertNotEqual(observed_sources[0], source)
            self.assertTrue(first["execution_key"] in {second["execution_key"]})
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])

    def test_rejects_unc_and_url_attachment_references(self):
        from automation.executors.restarea import execute_restarea

        # raw_ref is copied verbatim from scraped board HTML; a UNC or URL
        # reference must be refused before the executor opens it (no SMB/NTLM
        # fetch, no cross-scheme read).
        bad_refs = (
            r"\\attacker-host\share\payload.hwpx",
            "//attacker-host/share/payload.hwpx",
            "file://attacker-host/share/payload.hwpx",
            "https://www.example.invalid/board/download?fileSid=1",
            "smb://attacker-host/share/payload.hwpx",
        )
        for raw_ref in bad_refs:
            item = WorkItem(
                task_id="alpha-13452",
                source=SourceRef(
                    type="gnuboard",
                    id="alpha",
                    external_id="13452",
                    url="https://example.invalid/board/13452",
                ),
                received_at="2026-08-31T00:00:00+00:00",
                title="무더위쉼터 현행화 신청서",
                body="첨부파일을 변환합니다.",
                author="PERSON_001",
                attachments=(
                    AttachmentRef(
                        name="payload.hwpx",
                        type="hwpx",
                        raw_ref=raw_ref,
                        extracted_ref="normalized/alpha-13452/restarea.json",
                    ),
                ),
                mask_table_ref="local://masks/alpha-13452.json",
            )
            with self.assertRaises(ValueError, msg=raw_ref):
                execute_restarea(item, "artifacts-unused")

    def test_output_is_contained_by_artifact_directory(self):
        from automation.executors.restarea import execute_restarea

        with TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / ".." / "outside-name.hwpx"
            source.resolve().write_bytes(_fixture_bytes())
            item = _work_item(source.resolve())
            artifact_dir = directory / "artifacts"

            def fake_convert(source_path, output_path, template):
                output_path.write_bytes(b"fake xls")
                return output_path

            with patch("automation.executors.restarea._converter.convert", side_effect=fake_convert):
                result = execute_restarea(item, artifact_dir)

            output = Path(result["artifacts"][0]["path"]).resolve()
            self.assertEqual(output.parent, artifact_dir.resolve())


class RestareaConverterPathBudgetTest(unittest.TestCase):
    """Excel refuses Workbooks.Open past ~218 characters, and all it returns is
    an opaque com_error. The scratch file has to live beside the output for
    os.replace to stay atomic, so the only way to stay inside that budget is to
    keep its name short."""

    def converter(self):
        import importlib.util

        path = Path(__file__).parents[1] / "restarea-converter" / "converter.py"
        spec = importlib.util.spec_from_file_location("restarea_converter_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def template(self) -> Path:
        return Path(__file__).parents[1] / "restarea-converter" / "restarea-template.xls"

    def source(self, directory) -> Path:
        path = Path(directory) / "source.hwpx"
        path.write_bytes(_fixture_bytes())
        return path

    def test_the_scratch_name_leaves_room_for_a_deep_artifact_directory(self):
        # It used to be `.{64 char execution key}.{32 char uuid}.tmp.xls` --
        # 106 characters of filename, which blew the budget on its own.
        converter = self.converter()
        with TemporaryDirectory() as directory:
            output = Path(directory) / ("a" * 64 + ".xls")
            with patch.object(converter.shutil, "copy2", side_effect=RuntimeError("stop before COM")) as copy2:
                with self.assertRaises(RuntimeError):
                    converter.convert(self.source(directory), output, self.template())
                scratch = Path(copy2.call_args[0][1])

        self.assertLess(len(scratch.name), 32, scratch.name)
        self.assertTrue(scratch.name.endswith(".tmp.xls"))
        self.assertEqual(scratch.parent, output.parent)

    def test_a_path_over_the_budget_is_refused_before_any_com_call(self):
        converter = self.converter()
        with TemporaryDirectory() as directory:
            source = self.source(directory)
            deep = Path(directory)
            while len(str(deep.resolve())) < converter.EXCEL_MAX_PATH:
                deep = deep / ("d" * 40)
            deep.mkdir(parents=True, exist_ok=True)

            with self.assertRaises(converter.ExcelPathTooLongError) as caught:
                converter.convert(source, deep / "out.xls", self.template())

        self.assertIn(str(converter.EXCEL_MAX_PATH), str(caught.exception))


class RestareaExcelEndToEndTest(unittest.TestCase):
    @staticmethod
    def _excel_available() -> bool:
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            excel = win32com.client.DispatchEx("Excel.Application")
        except Exception:
            return False
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.Quit()
        pythoncom.CoUninitialize()
        return True

    @unittest.skipUnless(_excel_available.__func__(), "Microsoft Excel COM is unavailable")
    def test_real_fixture_converts_to_readable_xls(self):
        from automation.executors.restarea import execute_restarea
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()

        if FIXTURE is None:
            self.fail("expected the shipped restarea HWPX fixture")
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            result = execute_restarea(_work_item(FIXTURE), directory / "artifacts")
            output = Path(result["artifacts"][0]["path"])
            self.assertTrue(output.is_file())

            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            workbook = None
            try:
                workbook = excel.Workbooks.Open(str(output), ReadOnly=True)
                values = workbook.Worksheets(1).Range("A3:H3").Value
                workbook.Close(False)
                workbook = None
            finally:
                if workbook is not None:
                    workbook.Close(False)
                excel.Quit()
                pythoncom.CoUninitialize()

            self.assertEqual(values[0][0], 1.0)
            self.assertTrue(values[0][1])


if __name__ == "__main__":
    unittest.main()
