import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCANNER = ROOT / "scripts" / "check_public_safety.py"


def scan(root):
    return subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(root)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class ScannerContractTest(unittest.TestCase):
    def module(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import check_public_safety

        return check_public_safety

    def test_clean_tree_reports_no_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe.md").write_text("공개해도 되는 문서\n", encoding="utf-8")
            result = scan(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 findings", result.stdout)

    def test_private_ip_url_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.md").write_text("see http://192.168.0.14/admin\n", encoding="utf-8")
            result = scan(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bad.md", result.stdout)

    def test_phone_number_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.txt").write_text("담당자 연락처 010-1234-5678\n", encoding="utf-8")
            result = scan(root)
        self.assertNotEqual(result.returncode, 0)

    def test_token_like_value_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.env").write_text("PHPSESSID=abc123def456ghi\n", encoding="utf-8")
            result = scan(root)
        self.assertNotEqual(result.returncode, 0)

    def test_findings_never_print_the_matched_secret(self):
        secret = "SuperSecretSessionValue123"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.txt").write_text(f"token = {secret}\n", encoding="utf-8")
            result = scan(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)

    def test_findings_report_file_and_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.md").write_text("ok\nok\nhttp://10.1.2.3/x\n", encoding="utf-8")
            result = scan(root)
        self.assertIn("bad.md:3", result.stdout)

    def test_private_archives_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "업무자료.zip").write_bytes(b"PK\x03\x04binary")
            result = scan(root)
        self.assertNotEqual(result.returncode, 0)

    def test_ignored_paths_are_not_scanned(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ignored in ("file", "raw", "masks", ".git", "__pycache__", "worktrees"):
                (root / ignored).mkdir()
                (root / ignored / "leak.md").write_text("http://10.0.0.1/x\n", encoding="utf-8")
            (root / "clean.md").write_text("fine\n", encoding="utf-8")
            findings = module.scan_tree(root)
        self.assertEqual(findings, [])

    def test_scanner_is_deterministic(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.md").write_text("http://10.0.0.1/x\n", encoding="utf-8")
            (root / "b.md").write_text("010-1234-5678\n", encoding="utf-8")
            first = module.scan_tree(root)
            second = module.scan_tree(root)
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_binary_files_do_not_crash_the_scanner(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "icon.png").write_bytes(bytes(range(256)))
            self.assertEqual(module.scan_tree(root), [])


class PlaceholderAndExclusionTest(unittest.TestCase):
    def module(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import check_public_safety

        return check_public_safety

    def test_documented_placeholder_hosts_are_not_findings(self):
        module = self.module()
        for host in module.PLACEHOLDER_HOSTS:
            with self.subTest(host=host):
                self.assertEqual(module.scan_text(f'see https://{host}/x?wr_id=1\n'), [])

    def test_an_undocumented_local_host_still_trips_the_gate(self):
        module = self.module()
        self.assertTrue(module.scan_text("see https://payroll-db.corp.local/x\n"))

    def test_a_real_internal_host_beside_a_placeholder_is_still_reported(self):
        module = self.module()
        findings = module.scan_text("https://fixture.local/a and https://10.0.0.7/b\n")
        self.assertTrue(findings)

    def test_bare_post_id_without_a_host_is_not_a_finding(self):
        module = self.module()
        self.assertEqual(module.scan_text("sourceId('wr_id=13452')\n"), [])

    def test_test_paths_are_the_only_excluded_class(self):
        # Excluding test files is a real limitation: a test that asserts a
        # token is rejected must contain one. The allowlist must therefore
        # never widen beyond test paths.
        module = self.module()
        for path in ("tests/test_x.py", "a/tests/b.py", "extension/archiver.test.mjs", "test_y.py"):
            with self.subTest(path=path):
                self.assertTrue(module.TEST_PATH.search(path))
        for path in ("automation/models.py", "config/rules.json", "fixtures/a.html", "README.md"):
            with self.subTest(path=path):
                self.assertIsNone(module.TEST_PATH.search(path))


class RepositoryGateTest(unittest.TestCase):
    # This repository is published, so the gate must pass with zero findings.
    # The test pins that state: any newly added business document, credential,
    # or unsanitized fixture fails the suite before it can reach a push.

    def test_the_repository_has_no_findings(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import check_public_safety

        findings = check_public_safety.scan_tree(ROOT)
        self.assertEqual(findings, [])

    def test_the_gate_exits_clean(self):
        result = scan(ROOT)
        self.assertEqual(result.returncode, 0, result.stdout)


class GitignoreTest(unittest.TestCase):
    def test_existing_user_ignore_rules_are_preserved(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for existing in ("file/", "raw/", "masks/", ".session/", ".env", "__pycache__/", ".worktrees/"):
            with self.subTest(rule=existing):
                self.assertIn(existing, text)


if __name__ == "__main__":
    unittest.main()
