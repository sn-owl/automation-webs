import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "sanitized" / "alpha-ready.html"


class NormalizeCliTest(unittest.TestCase):
    def test_fixture_to_json_cli_writes_work_item(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "work-item.json"

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "normalize.py"),
                    "gnuboard",
                    str(FIXTURE),
                    str(output),
                    "--board-id",
                    "alpha",
                    "--source-url",
                    "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(data["task_id"], "alpha-13452")
        self.assertEqual(data["title"], "무더위쉼터 현황 현행화")
        self.assertEqual(data["attachments"][0]["raw_ref"], "https://example.invalid/url-004")

    def test_same_input_writes_byte_identical_json(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            command = [
                sys.executable,
                str(ROOT / "normalize.py"),
                "gnuboard",
                str(FIXTURE),
                str(first),
                "--board-id",
                "alpha",
                "--source-url",
                "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
            ]

            first_result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            command[4] = str(second)
            second_result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
