import hashlib
import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from automation.files.hwpx import handle_hwpx  # noqa: F401
from automation.files.registry import process_attachment


_HEADER_ROWS = (
    ("사이트명", "연제구 홈페이지"),
    ("메뉴명", "무더위쉼터"),
    ("위치", "복지/안내"),
    ("내용", "신청 내용"),
    ("담당자명", "PERSON_001"),
    ("행정번호", "051-000-0000"),
)


def _hwpx_bytes(*, standard: bool) -> bytes:
    if standard:
        rows = "".join(
            f"<tr><tc><p><run><t>{label}</t></run></p></tc>"
            f"<tc><p><run><t>{value}</t></run></p></tc></tr>"
            for label, value in _HEADER_ROWS
        )
    else:
        rows = "<tr><tc><p><run><t>비표준 문서</t></run></p></tc></tr>"
    section = f"<root><tbl>{rows}</tbl></root>".encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Contents/section0.xml", section)
    return output.getvalue()


class HwpxHandlerTest(unittest.TestCase):
    def test_standard_hwpx_is_parsed_with_source_metadata(self):
        original = _hwpx_bytes(standard=True)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "request.hwpx"
            path.write_bytes(original)

            result = process_attachment(path)

            self.assertEqual(result.path, str(path))
            self.assertEqual(result.type, "hwpx")
            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.sha256, hashlib.sha256(original).hexdigest())
            self.assertEqual(result.parser, "hwpx-standard-v1")
            self.assertEqual(result.extracted_ref, None)
            self.assertEqual(path.read_bytes(), original)

    def test_hwpx_with_entity_declaration_is_refused_not_expanded(self):
        # 'billion laughs' style: a DTD defining nested entities. The parser must
        # refuse the DTD outright (UnsafeXMLError) so nothing is expanded; the
        # attachment surfaces as failed for human review, never crashing.
        section = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE root [<!ENTITY a "AAAAAAAAAA">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            b"<root><tbl><tr><tc><p><run><t>&b;</t></run></p></tc></tr></tbl></root>"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("Contents/section0.xml", section)
        original = output.getvalue()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bomb.hwpx"
            path.write_bytes(original)

            result = process_attachment(path)

            self.assertNotEqual(result.status, "parsed")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.sha256, hashlib.sha256(original).hexdigest())
            self.assertEqual(path.read_bytes(), original)

    def test_nonstandard_hwpx_requires_review_without_false_parse(self):
        original = _hwpx_bytes(standard=False)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "other.hwpx"
            path.write_bytes(original)

            result = process_attachment(path)

            self.assertEqual(result.path, str(path))
            self.assertEqual(result.type, "hwpx")
            self.assertEqual(result.status, "requires_review")
            self.assertEqual(result.sha256, hashlib.sha256(original).hexdigest())
            self.assertEqual(result.parser, "hwpx-standard-v1")
            self.assertIsNotNone(result.error)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
