import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
BATCH = ROOT / "scripts" / "run_scheduled.bat"
INSTALL = ROOT / "scripts" / "install_task.ps1"

# The same classes the fixture policy forbids, applied to operational scripts.
FORBIDDEN = (
    ("private_host", re.compile(r"cug\.thewebs\.kr|[A-Za-z0-9-]+\.local\b")),
    (
        "private_ip",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
            r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        ),
    ),
    ("token", re.compile(r"(?i)\b(?:api[-_ ]?key|token|password|secret|bearer)\b\s*[=:]\s*\S")),
    ("absolute_user_path", re.compile(r"[Cc]:\\Users\\[A-Za-z0-9]")),
)


class BatchWrapperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = BATCH.read_text(encoding="utf-8")

    def test_script_exists(self):
        self.assertTrue(BATCH.is_file())

    def test_resolves_repository_root_from_its_own_location(self):
        # %~dp0 is the script's directory; the wrapper must not depend on the
        # scheduler's working directory, which Task Scheduler does not set.
        self.assertIn("%~dp0", self.text)

    def test_invokes_the_pipeline_and_nothing_else(self):
        self.assertIn("run_pipeline.py", self.text)
        for forbidden in ("taskctl.py", "--recipe", "approve", "execute"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, self.text)

    def test_routes_download_directory_through_inbox_runner(self):
        self.assertIn("inbox_runner.py", self.text)
        self.assertIn("--downloads", self.text)
        self.assertIn("--inbox", self.text)

    def test_writes_an_explicit_log_path(self):
        self.assertRegex(self.text, r"(?i)LOG")
        self.assertIn(">>", self.text)

    def test_propagates_the_pipeline_exit_code(self):
        self.assertRegex(self.text, r"(?i)ERRORLEVEL")
        self.assertRegex(self.text, r"(?i)exit /b")

    def test_fails_when_prerequisites_are_missing(self):
        # A scheduled run that silently does nothing is worse than one that
        # fails, so a missing interpreter or input must exit nonzero.
        self.assertRegex(self.text, r"(?i)where\s+python|python\s+--version|if\s+not\s+exist")

    def test_contains_no_credential_or_private_value(self):
        for name, pattern in FORBIDDEN:
            with self.subTest(pattern=name):
                self.assertIsNone(pattern.search(self.text), f"batch script contains {name}")


class InstallScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL.read_text(encoding="utf-8")

    def test_script_exists(self):
        self.assertTrue(INSTALL.is_file())

    def test_registration_is_dry_run_by_default(self):
        # Registering a scheduled task changes the host. That must be opt-in.
        self.assertRegex(self.text, r"(?i)\[switch\]\s*\$Register")
        self.assertNotRegex(self.text, r"(?i)\[switch\]\s*\$DryRun")

    def test_dry_run_prints_the_registration_without_applying_it(self):
        self.assertRegex(self.text, r"(?i)Register-ScheduledTask")
        self.assertRegex(self.text, r"(?i)if\s*\(\s*-not\s+\$Register\s*\)")

    def test_takes_repository_and_log_paths_as_parameters(self):
        for parameter in ("$RepositoryRoot", "$LogPath", "$InputPath", "$OutputRoot"):
            with self.subTest(parameter=parameter):
                self.assertIn(parameter, self.text)

    def test_contains_no_credential_or_private_value(self):
        for name, pattern in FORBIDDEN:
            with self.subTest(pattern=name):
                self.assertIsNone(pattern.search(self.text), f"install script contains {name}")

    def test_never_stores_a_password_for_the_scheduled_task(self):
        for forbidden in ("-Password", "ConvertTo-SecureString", "-AsPlainText"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main()
