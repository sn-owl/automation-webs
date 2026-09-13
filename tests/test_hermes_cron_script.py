import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "hermes" / "scripts" / "run-pipeline.py"
FIXTURE = ROOT / "fixtures" / "sanitized" / "yeonje-ready.html"


def run(args, **kwargs):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


class HermesCronScriptTest(unittest.TestCase):
    def test_script_exists_under_the_hermes_scripts_directory(self):
        # Hermes reads script-only cron entries from ~/.hermes/scripts/; the
        # repository mirrors that layout so the file can be copied verbatim.
        self.assertTrue(SCRIPT.is_file())
        self.assertEqual(SCRIPT.parent.name, "scripts")
        self.assertEqual(SCRIPT.parent.parent.name, "hermes")

    def test_success_writes_nothing_to_stdout(self):
        # Hermes notifies on output. A quiet success must stay quiet so a
        # scheduled run does not page anyone for working correctly.
        with tempfile.TemporaryDirectory() as directory:
            result = run([str(FIXTURE), "--output-root", directory])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_failure_exits_nonzero_and_reports_on_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run([str(ROOT / "does-not-exist.html"), "--output-root", directory])
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())

    def test_failure_output_carries_no_traceback_or_private_path(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run([str(ROOT / "does-not-exist.html"), "--output-root", directory])
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("File \"", result.stderr)

    def test_blocked_task_is_reported_rather_than_silently_passing(self):
        # A task that stops at a safety boundary is an operational signal.
        with tempfile.TemporaryDirectory() as directory:
            result = run(
                [str(ROOT / "fixtures" / "sanitized" / "dongnae-developable.html"),
                 "--output-root", directory]
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked", (result.stdout + result.stderr).lower())

    def test_quiet_flag_suppresses_state_reporting_but_not_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run([str(FIXTURE), "--output-root", directory, "--quiet"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_script_uses_no_conversational_state_and_no_agent(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("hermes_client", "HermesClient", "input(", "session", "conversation"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)

    def test_script_never_decides_or_executes(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("taskctl", "get_recipe", "Decision", "subprocess"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, source)

    def test_missing_arguments_fail_fast(self):
        result = run([])
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
