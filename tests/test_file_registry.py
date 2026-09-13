import hashlib
import tempfile
import unittest
from pathlib import Path

from automation.files.base import FileResult
from automation.files.registry import (
    FileMutationError,
    process_attachment,
    register_handler,
    unregister_handler,
)


class FileRegistryTest(unittest.TestCase):
    def test_unsupported_extension_returns_explicit_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.BIN"
            original = b"opaque bytes"
            path.write_bytes(original)

            result = process_attachment(path)

            self.assertEqual(result, FileResult(
                path=str(path),
                type="bin",
                status="unsupported",
                sha256=hashlib.sha256(original).hexdigest(),
            ))
            self.assertEqual(path.read_bytes(), original)

    def test_registered_handler_returns_file_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.TXT"
            path.write_text("내용", encoding="utf-8")

            def read_only_handler(file_path: Path) -> FileResult:
                return FileResult(
                    path=str(file_path),
                    type="txt",
                    status="parsed",
                    sha256=hashlib.sha256(file_path.read_bytes()).hexdigest(),
                    parser="test-handler-v1",
                )

            register_handler(".txt", read_only_handler)
            try:
                result = process_attachment(path)
            finally:
                unregister_handler("txt")

            self.assertEqual(result.type, "txt")
            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.parser, "test-handler-v1")
    def test_one_argument_handler_accepts_optional_mime_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.TXT"
            path.write_text("내용", encoding="utf-8")

            def read_only_handler(file_path: Path) -> FileResult:
                return FileResult(
                    path=str(file_path),
                    type="txt",
                    status="parsed",
                    sha256=hashlib.sha256(file_path.read_bytes()).hexdigest(),
                )

            register_handler("txt", read_only_handler)
            try:
                result = process_attachment(path, mime_type="text/plain")
            finally:
                unregister_handler("txt")

            self.assertEqual(result.type, "txt")
            self.assertEqual(result.status, "parsed")


    def test_registry_rejects_handler_that_modifies_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.TXT"
            path.write_text("원본", encoding="utf-8")
            original = path.read_bytes()

            def mutating_handler(file_path: Path) -> FileResult:
                file_path.write_text("변조", encoding="utf-8")
                return FileResult(
                    path=str(file_path),
                    type="txt",
                    status="parsed",
                    sha256="not-original",
                )

            register_handler("txt", mutating_handler)
            try:
                with self.assertRaises(FileMutationError):
                    process_attachment(path)
            finally:
                unregister_handler("txt")

            self.assertNotEqual(path.read_bytes(), original)

    def test_extension_matching_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.BIN"
            path.write_bytes(b"opaque bytes")

            result = process_attachment(path)

            self.assertEqual(result.type, "bin")
            self.assertEqual(result.status, "unsupported")


if __name__ == "__main__":
    unittest.main()
