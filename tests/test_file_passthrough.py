import hashlib
import mimetypes
import tempfile
import unittest
from pathlib import Path

from automation.files.passthrough import handle_passthrough  # noqa: F401
from automation.files.registry import process_attachment


class FilePassthroughTest(unittest.TestCase):
    def test_supported_unparsed_files_preserve_source_metadata(self):
        original = b"opaque attachment bytes"
        expected_hash = hashlib.sha256(original).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            for extension in ("pdf", "docx", "xlsx", "hwp"):
                path = Path(directory) / f"attachment.{extension}"
                path.write_bytes(original)

                result = process_attachment(path)

                with self.subTest(extension=extension):
                    self.assertEqual(result.path, str(path))
                    self.assertEqual(result.type, extension)
                    self.assertEqual(result.status, "requires_review")
                    self.assertEqual(result.sha256, expected_hash)
                    self.assertIsNone(result.extracted_ref)
                    self.assertEqual(path.read_bytes(), original)

    def test_mime_extension_conflict_is_explicitly_reported(self):
        original = b"opaque pdf bytes"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attachment.pdf"
            path.write_bytes(original)

            result = handle_passthrough(path, mime_type="text/plain")
            self.assertEqual(path.read_bytes(), original)

        self.assertEqual(result.status, "requires_review")
        self.assertIsNotNone(result.error)
        self.assertIn("MIME type mismatch", result.error)
        self.assertIn(mimetypes.guess_type(path.name, strict=False)[0], result.error)
        self.assertIn("text/plain", result.error)
        self.assertEqual(result.sha256, hashlib.sha256(original).hexdigest())
    def test_registry_dispatch_reports_mime_extension_conflict(self):
        original = b"opaque pdf bytes"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attachment.pdf"
            path.write_bytes(original)

            result = process_attachment(path, mime_type="text/plain")

            self.assertEqual(path.read_bytes(), original)

        self.assertEqual(result.status, "requires_review")
        self.assertIsNotNone(result.error)
        self.assertIn("MIME type mismatch", result.error)
        self.assertIn("text/plain", result.error)



if __name__ == "__main__":
    unittest.main()
