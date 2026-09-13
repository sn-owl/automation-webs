import base64
import importlib.util
import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

# Pillow is an optional dependency: automation.document_preview degrades to
# "image_decoder_unavailable" without it, so a stdlib-only checkout still runs
# the whole suite. requirements-dev.txt installs it, and CI covers these paths.
HAS_IMAGE_DECODER = importlib.util.find_spec("PIL") is not None
REQUIRES_DECODER = unittest.skipUnless(
    HAS_IMAGE_DECODER, "Pillow is not installed (pip install -r requirements-dev.txt)"
)


class DocumentPreviewTest(unittest.TestCase):
    def test_hwpx_preview_extracts_fields_and_redacts_urls(self):
        section = (
            "<root><tbl>"
            "<tr><tc><p><run><t>사이트명</t></run></p></tc>"
            "<tc><p><run><t>내부 홈페이지</t></run></p></tc></tr>"
            "<tr><tc><p><run><t>메뉴명</t></run></p></tc>"
            "<tc><p><run><t>공지</t></run></p></tc></tr>"
            "<tr><tc><p><run><t>위치</t></run></p></tc>"
            "<tc><p><run><t>첫 화면</t></run></p></tc></tr>"
            "<tr><tc><p><run><t>내용</t></run></p></tc>"
            "<tc><p><run><t>https://internal.example/request</t></run></p></tc></tr>"
            "<tr><tc><p><run><t>담당자명</t></run></p></tc>"
            "<tc><p><run><t>담당 부서</t></run></p></tc></tr>"
            "</tbl></root>"
        ).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("Contents/section0.xml", section)

        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "request.hwpx"
            path.write_bytes(output.getvalue())
            result = preview_attachment(path)

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["parser"], "hwpx-standard-v1")
        self.assertIn("사이트명: 내부 홈페이지", result["text"])
        self.assertIn("내용: [URL 생략]", result["text"])
        self.assertNotIn("internal.example", result["text"])

    def test_nonstandard_hwpx_uses_embedded_preview_text(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("Contents/section0.xml", "<root/>")
            archive.writestr("Preview/PrvText.txt", "요청 내용\nhttps://internal.example")

        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "request.hwpx"
            path.write_bytes(output.getvalue())
            result = preview_attachment(path)

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["parser"], "hwpx-preview-v1")
        self.assertEqual(result["metadata"]["representation"], "structured_fallback")
        self.assertIn("요청 내용", result["text"])
        self.assertNotIn("internal.example", result["text"])

    def test_pdf_without_renderable_structure_is_corrupt_not_text_success(self):
        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "request.pdf"
            path.write_bytes(b"%PDF-1.4\nstream\nBT\n(Hello) Tj\nET\nendstream\n%%EOF\n")
            result = preview_attachment(path)

        self.assertEqual(result["status"], "corrupt")
        self.assertIn(result["failure_reason"], {"invalid_pdf_structure", "invalid_pdf_truncated"})
        self.assertNotEqual(result.get("metadata", {}).get("representation"), "page_text")

    @unittest.skipUnless(
        importlib.util.find_spec("pypdfium2") is not None,
        "pypdfium2 renderer unavailable",
    )
    def test_pdf_renderer_returns_real_png_page_preview(self):
        from PIL import Image
        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "rendered.pdf"
            Image.new("RGB", (120, 80), color=(240, 250, 255)).save(path, format="PDF")
            result = preview_attachment(path)

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["metadata"]["representation"], "pdf_pages")
        self.assertEqual(result["metadata"]["page_count"], 1)
        encoded = result["metadata"]["pages"][0]["image"]["data"]
        self.assertTrue(base64.b64decode(encoded, validate=True).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_old_hwp_is_explicitly_unavailable(self):
        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "request.hwp"
            path.write_bytes(b"not parsed")
            result = preview_attachment(path)

        self.assertEqual(result["status"], "unsupported")
        self.assertIn("구형 HWP", result["message"])

    @REQUIRES_DECODER
    def test_corrupt_image_is_not_reported_as_parsed(self):
        # A03: a file with a .png type but no valid image signature must not be
        # reported as a successfully parsed image.
        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "picture.png"
            path.write_bytes(b"image-bytes")
            result = preview_attachment(path, "png")

        self.assertEqual(result["failure_reason"], "invalid_image_data")
        self.assertEqual(result["status"], "corrupt")
        self.assertNotIn("local_path", result)
    def test_preview_failure_states_are_distinct_and_preserve_reference_contract(self):
        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            root = Path(directory)
            locked = root / "locked.pdf"
            locked.write_bytes(b"%PDF-1.4\n")
            with patch("automation.document_preview._is_locked", return_value=True):
                locked_result = preview_attachment(locked, "pdf")

            failed = root / "failed.xlsx"
            failed.write_bytes(b"not-a-zip-workbook")
            unsupported = root / "unsupported.docx"
            unsupported.write_bytes(b"opaque")
            oversized = root / "large.bin"
            oversized.write_bytes(b"0" * (8 * 1024 * 1024 + 1))

            self.assertEqual(locked_result["status"], "locked")
            self.assertEqual(
                preview_attachment(failed, "xlsx")["status"],
                "corrupt",
            )
            self.assertEqual(preview_attachment(unsupported, "docx")["status"], "unsupported")
            self.assertEqual(preview_attachment(oversized, "bin")["status"], "too_large")
            self.assertEqual(
                len(
                    {
                        locked_result["status"],
                        preview_attachment(failed, "xlsx")["status"],
                        preview_attachment(unsupported, "docx")["status"],
                        preview_attachment(oversized, "bin")["status"],
                    }
                ),
                4,
            )

    @REQUIRES_DECODER
    def test_valid_png_is_fully_verified_with_dimensions(self):
        from PIL import Image
        from automation.document_preview import preview_attachment

        with TemporaryDirectory() as directory:
            path = Path(directory) / "picture.png"
            Image.new("RGB", (2, 3), color=(10, 20, 30)).save(path, format="PNG")
            result = preview_attachment(path, "png")

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["image"]["format"], "PNG")
        self.assertEqual((result["image"]["width"], result["image"]["height"]), (2, 3))

class DashboardContentPreviewTest(unittest.TestCase):
    @REQUIRES_DECODER
    def test_dashboard_loads_local_body_and_attachment_preview(self):
        from automation.models import WorkItem
        from automation.task_store import put_task
        from dashboard import build_task_content_preview

        with TemporaryDirectory() as directory:
            root = Path(directory)
            attachment = root / "request.pdf"
            stream = b"BT\n(Attachment text) Tj\nET\n"
            attachment.write_bytes(
                b"%PDF-1.4\nstream\n" + stream + b"endstream\n%%EOF\n"
            )
            from PIL import Image
            image = root / "picture.png"
            Image.new("RGB", (2, 2), color=(40, 50, 60)).save(image, format="PNG")
            put_task(
                WorkItem.from_dict(
                    {
                        "task_id": "task-preview",
                        "source": {
                            "type": "board",
                            "id": "board",
                            "external_id": "1",
                            "url": "https://example.invalid/post",
                        },
                        "received_at": "2026-09-03T10:00:00+09:00",
                        "title": "게시글 제목",
                        "body": "게시글 본문 내용",
                        "author": "작성자",
                        "attachments": [
                            {
                                "name": "request.pdf",
                                "type": "pdf",
                                "raw_ref": str(attachment),
                                "extracted_ref": "normalized/request.pdf.json",
                            },
                            {
                                "name": "picture.png",
                                "type": "png",
                                "raw_ref": str(image),
                                "extracted_ref": "normalized/picture.png.json",
                            },
                        ],
                        "mask_table_ref": "local://masks/task-preview.json",
                    }
                ),
                root=root,
            )
            preview = build_task_content_preview(root, "task-preview")

        self.assertEqual(preview["attachments"][1]["local_path"], image)

        self.assertEqual(preview["body"], "게시글 본문 내용")
        self.assertEqual(preview["attachments"][0]["preview"]["status"], "corrupt")
        self.assertEqual(preview["attachments"][1]["preview"]["status"], "parsed")
        self.assertEqual(preview["attachments"][1]["preview"]["image"]["display"], "local")

class DashboardCardTest(unittest.TestCase):
    def test_classification_rows_are_korean_and_toned(self):
        from dashboard import _detail_card_rows, _detail_tag_groups

        rows = _detail_card_rows(
            {
                "state": "review_required",
                "classification": {
                    "responsibility": "content",
                    "task_type": "image_popup",
                    "size": "simple",
                    "confidence": 0.8,
                },
                "assessment": {
                    "automation_level": "manual",
                    "risk": "remote_write",
                    "confidence": 0.7,
                },
            }
        )

        self.assertEqual(
            [(label, value) for label, value, _tone in rows[:6]],
            [
                ("업무 상태", "검토 필요"),
                ("업무 영역", "콘텐츠"),
                ("무슨 일인가", "팝업·이미지 게시"),
                ("업무 범위", "단순"),
                ("처리 방법", "사람 검토"),
                ("외부 영향", "외부 변경"),
            ],
        )
        self.assertEqual([tone for _label, _value, tone in rows[:6]], [
            "blue", "purple", "teal", "orange", "green", "red",
        ])
        groups = _detail_tag_groups(
            {
                "state": "review_required",
                "classification": {"responsibility": "content", "task_type": "image_popup", "size": "simple"},
                "assessment": {"automation_level": "manual", "risk": "remote_write"},
            }
        )
        self.assertEqual([title for title, _rows in groups], ["현재 상태", "업무 분류", "처리 판단", "확신도"])
        self.assertEqual([len(rows) for _title, rows in groups], [1, 3, 2, 0])
    def test_header_leds_are_boolean_health_states(self):
        from dashboard import _header_led_rows

        rows = _header_led_rows(
            {
                "bundles": [{}],
                "inbox": [{}],
                "tasks": [{"state": "review_required"}, {"state": "storage_failed"}],
                "executions": [],
            },
            [{"stage": "hermes", "state": "assessed"}],
            [{"state": "storage_failed"}],
        )

        self.assertEqual(
            rows,
            [
                ("Downloads", True, "normal"),
                ("Core Inbox", True, "normal"),
                ("Tasks", True, "normal"),
                ("Hermes", True, "normal"),
                ("Executions", False, "normal"),
                ("Terminal", False, "normal"),
                ("오류 감지", True, "error"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
