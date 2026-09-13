import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from automation.classification import Classification
from automation.models import WorkItem


def work_item(**overrides):
    base = {
        "task_id": "alpha-13452",
        "source": {
            "type": "board",
            "id": "alpha",
            "external_id": "13452",
            "url": "https://fixture.local/bbs/board.php?wr_id=13452",
        },
        "received_at": "2026-08-28T09:44:00+09:00",
        "title": "무더위쉼터 현행화",
        "body": "최신 파일로 현행화해 주세요.",
        "author": "요청부서",
        "attachments": [],
        "mask_table_ref": "local://masks/alpha-13452.json",
    }
    base.update(overrides)
    return WorkItem.from_dict(base)


def classification(**overrides):
    base = {
        "responsibility": "operations",
        "task_type": "data_refresh",
        "size": "recurring",
        "confidence": 0.9,
    }
    base.update(overrides)
    return Classification.from_rule(
        base,
        evidence=["rule:heat-shelter-refresh"],
        rule_ids=["heat-shelter-refresh"],
    )


class WorkPackageRendererTest(unittest.TestCase):
    def renderer(self):
        try:
            from automation.work_package import render_work_package
        except ModuleNotFoundError as exc:
            self.fail(f"automation.work_package should exist: {exc}")
        return render_work_package

    def test_renders_reviewable_package_structure_and_metadata(self):
        render_work_package = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / "generated.xls"
            artifact.write_bytes(b"generated artifact")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            package = render_work_package(
                work_item(),
                classification(),
                root / "packages",
                artifacts=[{"path": str(artifact), "sha256": digest}],
                final_status="succeeded",
            )

            self.assertEqual(package, root / "packages" / "alpha-13452")
            self.assertEqual(
                {
                    "ACTION.md",
                    "WORKLOG.md",
                    "source.json",
                    "classification.json",
                    "artifacts",
                },
                {entry.name for entry in package.iterdir()},
            )
            self.assertEqual(
                work_item().to_dict(),
                json.loads((package / "source.json").read_text(encoding="utf-8")),
            )
            self.assertEqual(
                classification().to_dict(),
                json.loads((package / "classification.json").read_text(encoding="utf-8")),
            )
            copied = package / "artifacts" / "generated.xls"
            self.assertEqual(b"generated artifact", copied.read_bytes())

            action = (package / "ACTION.md").read_text(encoding="utf-8")
            worklog = (package / "WORKLOG.md").read_text(encoding="utf-8")
            self.assertIn("무더위쉼터 현행화", action)
            self.assertIn("최신 파일로 현행화해 주세요.", action)
            self.assertIn("operations", action)
            self.assertIn("data_refresh", action)
            source_digest = hashlib.sha256((package / "source.json").read_bytes()).hexdigest()
            classification_digest = hashlib.sha256(
                (package / "classification.json").read_bytes()
            ).hexdigest()
            self.assertIn("source.json", worklog)
            self.assertIn(source_digest, worklog)
            self.assertIn("classification.json", worklog)
            self.assertIn(classification_digest, worklog)
            self.assertIn("generated.xls", worklog)
            self.assertIn(digest, worklog)
            self.assertIn("succeeded", worklog)
    def test_title_cannot_escape_task_directory(self):
        render_work_package = self.renderer()
        item = work_item(title="../../outside-package")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = render_work_package(item, classification(), root / "packages")

            self.assertEqual(package, root / "packages" / item.task_id)
            self.assertTrue(package.is_dir())
            self.assertFalse((root / "outside-package").exists())
            self.assertIn(item.title, (package / "ACTION.md").read_text(encoding="utf-8"))

    def test_task_id_path_traversal_is_rejected(self):
        render_work_package = self.renderer()
        item = work_item(task_id="../outside")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "safe identifier"):
                render_work_package(item, classification(), directory)


    def test_artifact_hash_mismatch_is_rejected(self):
        render_work_package = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / "generated.xls"
            artifact.write_bytes(b"actual")
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                render_work_package(
                    work_item(),
                    classification(),
                    root / "packages",
                    artifacts=[{"path": str(artifact), "sha256": "0" * 64}],
                )

    def test_accepts_single_artifact_record_mapping(self):
        render_work_package = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / "single.xls"
            artifact.write_bytes(b"single artifact")
            metadata = {
                "path": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }

            package = render_work_package(
                work_item(), classification(), root / "packages", artifacts=metadata
            )

            self.assertEqual(
                b"single artifact",
                (package / "artifacts" / "single.xls").read_bytes(),
            )

    def test_rerender_removes_obsolete_artifact_files(self):
        render_work_package = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / "first.xls"
            second = root / "second.xls"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            first_metadata = {
                "path": str(first),
                "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
            }
            second_metadata = {
                "path": str(second),
                "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
            }

            package = render_work_package(
                work_item(),
                classification(),
                root / "packages",
                artifacts=[first_metadata, second_metadata],
            )
            render_work_package(
                work_item(),
                classification(),
                root / "packages",
                artifacts=[second_metadata],
            )

            self.assertFalse((package / "artifacts" / "first.xls").exists())
            self.assertTrue((package / "artifacts" / "second.xls").is_file())
    def test_repeated_rendering_is_byte_identical(self):
        render_work_package = self.renderer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / "generated.xls"
            artifact.write_bytes(b"generated artifact")
            metadata = {
                "path": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }

            package = render_work_package(
                work_item(), classification(), root / "packages", artifacts=[metadata]
            )
            before = {
                path.relative_to(package).as_posix(): path.read_bytes()
                for path in package.rglob("*")
                if path.is_file()
            }
            render_work_package(
                work_item(), classification(), root / "packages", artifacts=[metadata]
            )
            after = {
                path.relative_to(package).as_posix(): path.read_bytes()
                for path in package.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
