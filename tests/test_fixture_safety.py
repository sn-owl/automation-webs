import hashlib
import json
import tempfile
from pathlib import Path

import unittest

from scripts.sanitize_fixture import scan_fixture_text


class FixtureSafetyTest(unittest.TestCase):
    def test_flags_identity_contact_internal_url_and_session_tokens(self):
        unsafe_fixture = """
        <html>
          <body>
            <p>담당자: 홍길동</p>
            <p>연락처: 051-123-4567</p>
            <a href="http://192.168.0.10/admin/board.php?wr_id=7">관리자</a>
            <script>document.cookie = "PHPSESSID=abc123secret";</script>
          </body>
        </html>
        """

        findings = scan_fixture_text(unsafe_fixture)

        self.assertEqual(
            findings,
            [
                "person_name",
                "phone_number",
                "internal_url",
                "source_identifier",
                "session_token",
            ],
        )

    def test_flags_company_maintenance_url(self):
        findings = scan_fixture_text('<a href="https://cug.thewebs.kr/bbs/board.php">link</a>')

        self.assertEqual(findings, ["internal_url"])

    def test_flags_real_source_identifiers_in_query_strings(self):
        findings = scan_fixture_text("./board.php?bo_table=alpha&amp;wr_id=13457")

        self.assertEqual(findings, ["source_identifier"])

    def test_sanitizer_replaces_sensitive_values_deterministically(self):
        from scripts.sanitize_fixture import sanitize_html

        unsafe_fixture = """
        <html>
          <body>
            <p>담당자: 홍길동</p>
            <p>연락처: 051-123-4567</p>
            <a href="http://192.168.0.10/admin/board.php?wr_id=7">관리자</a>
          </body>
        </html>
        """

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "source.html"
            output = directory / "sanitized.html"
            source.write_text(unsafe_fixture, encoding="utf-8")

            report = sanitize_html(
                source,
                output,
                {
                    "홍길동": "PERSON_001",
                    "051-123-4567": "PHONE_001",
                    "http://192.168.0.10/admin/board.php?wr_id=7": "URL_001",
                },
            )

            sanitized = output.read_text(encoding="utf-8")

        self.assertNotIn("홍길동", sanitized)
        self.assertNotIn("051-123-4567", sanitized)
        self.assertNotIn("192.168.0.10", sanitized)
        self.assertIn("PERSON_001", sanitized)
        self.assertIn("PHONE_001", sanitized)
        self.assertIn("URL_001", sanitized)
        self.assertEqual(report["replacements"], 3)

    def test_sanitizer_removes_output_when_sensitive_text_remains(self):
        from scripts.sanitize_fixture import sanitize_html

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "source.html"
            output = directory / "sanitized.html"
            source.write_text("<p>연락처: 051-123-4567</p>", encoding="utf-8")
            output.write_text("old unsafe output", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "phone_number"):
                sanitize_html(source, output, {})

            self.assertFalse(output.exists())

    def test_sanitized_fixture_manifest_matches_safe_files(self):
        fixture_dir = Path(__file__).parents[1] / "fixtures" / "sanitized"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(
            [
                (item["file"], item["source_type"], item["scenario"])
                for item in manifest["fixtures"]
            ],
            [
                ("alpha-ready.html", "gnuboard", "ready"),
                ("beta-manual.html", "gnuboard", "manual"),
                ("egov-developable.html", "egov", "developable"),
            ],
        )

        for item in manifest["fixtures"]:
            self.assertEqual(set(item), {"file", "source_type", "scenario", "sha256"})
            text = (fixture_dir / item["file"]).read_text(encoding="utf-8")
            self.assertEqual(scan_fixture_text(text), [])
            self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), item["sha256"])


if __name__ == "__main__":
    unittest.main()
