import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCENARIOS = ("ready", "manual", "developable")


class DemoRunnerTest(unittest.TestCase):
    def runner(self):
        import run_demo

        return run_demo

    def test_all_three_scenarios_are_produced(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo.run_demo(output_root=Path(directory))

        self.assertEqual(tuple(scenario["name"] for scenario in summary["scenarios"]), SCENARIOS)
        by_name = {scenario["name"]: scenario for scenario in summary["scenarios"]}
        for scenario in by_name.values():
            self.assertEqual(scenario["status"], "review_required")
            self.assertEqual(scenario["route"], "classification_only")
            self.assertIsNone(scenario["assessment"])
            self.assertIsNotNone(scenario["classification"])

    def test_each_scenario_gets_its_own_output_directory(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_demo.run_demo(output_root=root)
            directories = sorted(path.name for path in root.iterdir() if path.is_dir())

        for scenario in SCENARIOS:
            self.assertIn(scenario, directories)

    def test_second_run_into_a_new_directory_is_byte_identical(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_summary = run_demo.run_demo(output_root=Path(first))
            second_summary = run_demo.run_demo(output_root=Path(second))

            first_manifest = (Path(first) / "manifest.json").read_bytes()
            second_manifest = (Path(second) / "manifest.json").read_bytes()

        self.assertEqual(first_summary, second_summary)
        self.assertEqual(first_manifest, second_manifest)

    def test_manifest_records_a_sha256_for_every_recorded_file(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_demo.run_demo(output_root=root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(sorted(manifest), ["files", "scenarios"])
            self.assertTrue(manifest["files"])
            for relative, digest in manifest["files"].items():
                with self.subTest(path=relative):
                    self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")
                    self.assertTrue((root / relative).is_file())
                    self.assertNotIn("..", relative)

    def test_manifest_excludes_runtime_lock_files(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_demo.run_demo(output_root=root)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

        for relative in manifest["files"]:
            with self.subTest(path=relative):
                self.assertFalse(relative.endswith(".lock"))

    def test_demo_never_opens_a_network_connection(self):
        import socket

        run_demo = self.runner()

        def forbidden(*_args, **_kwargs):
            raise AssertionError("demo attempted a network connection")

        original_socket = socket.socket
        original_create = socket.create_connection
        socket.socket = forbidden
        socket.create_connection = forbidden
        try:
            with tempfile.TemporaryDirectory() as directory:
                run_demo.run_demo(output_root=Path(directory))
        finally:
            socket.socket = original_socket
            socket.create_connection = original_create

    def test_recorded_classifications_are_validated_by_the_core_boundary(self):
        from automation.hermes_validator import validate_hermes_classification

        run_demo = self.runner()
        for name, raw in run_demo.RECORDED_PROPOSALS.items():
            with self.subTest(scenario=name):
                validated = validate_hermes_classification(raw)
                self.assertTrue(validated.responsibility)

    def test_no_raw_private_fixture_content_reaches_the_summary(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = run_demo.run_demo(output_root=root)
            manifest_text = (root / "manifest.json").read_text(encoding="utf-8")

        serialized = json.dumps(summary, ensure_ascii=False) + manifest_text
        for forbidden in ("<html", "<table", "PHPSESSID", "Bearer ", "cug.thewebs.kr"):
            with self.subTest(marker=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_manifest_is_identical_for_relative_and_absolute_output_roots(self):
        # The pipeline reports absolute paths even for a relative output root,
        # so a manifest must not depend on how the caller spelled it.
        import os

        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            absolute_root = Path(directory) / "absolute"
            run_demo.run_demo(output_root=absolute_root)
            absolute_manifest = (absolute_root / "manifest.json").read_bytes()

            previous = os.getcwd()
            os.chdir(directory)
            try:
                run_demo.run_demo(output_root=Path("relative"))
                relative_manifest = (Path(directory) / "relative" / "manifest.json").read_bytes()
            finally:
                os.chdir(previous)

        self.assertEqual(absolute_manifest, relative_manifest)

    def test_summary_paths_are_relative_to_the_output_root(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo.run_demo(output_root=Path(directory))

        serialized = json.dumps(summary)
        self.assertNotIn(directory, serialized)
        self.assertNotIn("/tmp", serialized)

    def test_cli_writes_the_demo_and_returns_zero(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                code = run_demo.main(["--output-root", directory])
            self.assertEqual(code, 0)
            self.assertTrue((Path(directory) / "manifest.json").is_file())

    def test_cli_refuses_to_overwrite_an_existing_run(self):
        run_demo = self.runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            root.mkdir()
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code = run_demo.main(["--output-root", str(root)])
            self.assertNotEqual(code, 0)
            self.assertEqual((root / "manifest.json").read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
