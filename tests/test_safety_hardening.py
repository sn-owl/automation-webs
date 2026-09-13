import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from automation.executors.code_analysis import execute_code_analysis
from automation.project_profile import (
    ProjectProfile,
    discover_files,
    load_project,
    project_content_digest,
    register_project,
)
from automation.preparation_plan import build_preparation_plan, export_preparation_report
from automation.document_preview import _preview_xlsx
from automation.verification import verify_recipe_output


def _profile(root: Path, **overrides) -> ProjectProfile:
    values = {
        "schema_version": 1,
        "scope": "owner-a",
        "project_id": "p",
        "name": "Synthetic project",
        "root": str(root),
        "managed_patterns": ["*.py"],
        "managed_directories": ["."],
        "stack": ["python"],
        "run_reference": None,
        "test_reference": None,
        "allow_full_codebase": False,
        "allow_external_ai_code": False,
    }
    values.update(overrides)
    return ProjectProfile.from_dict(values)
class ProjectProfileSafetyTest(unittest.TestCase):
    def test_read_scope_preserves_directories_patterns_and_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src").mkdir()
            profile = _profile(
                root,
                managed_patterns=["src/**/*.py"],
                managed_directories=["src"],
                database_paths=["state.sqlite"],
            )
            self.assertEqual(profile.read_scope["directories"], ["src"])
            self.assertEqual(profile.read_scope["file_patterns"], ["src/**/*.py"])
            self.assertEqual(profile.read_scope["database_paths"], ["state.sqlite"])
            self.assertEqual(profile.to_dict()["managed_directories"], ["src"])

    def test_read_scope_rejects_outside_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                _profile(Path(directory).resolve(), managed_directories=["../outside"])
    def test_string_false_is_not_coerced_to_grant_access(self):
        # A07: bool("false") is True; a switch must parse strictly.
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "explicitly selected"):
                _profile(
                    Path(directory).resolve(),
                    allow_full_codebase="false",
                    allow_external_ai_code="false",
                )

    def test_non_boolean_switch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                _profile(Path(directory).resolve(), allow_full_codebase="yes")

    def test_symlink_escaping_root_is_not_discovered(self):
        # A07: a symlink under root that resolves outside must be excluded.
        with tempfile.TemporaryDirectory() as inside_dir, tempfile.TemporaryDirectory() as outside_dir:
            inside = Path(inside_dir)
            (inside / "a.py").write_text("x = 1")
            secret = Path(outside_dir) / "secret.py"
            secret.write_text("SECRET")
            try:
                os.symlink(secret, inside / "link.py")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this platform")
            names = [path.name for path in discover_files(_profile(inside))]
        self.assertIn("a.py", names)
        self.assertNotIn("link.py", names)

    def test_git_credentials_and_environment_files_are_hard_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "safe.py").write_text("def safe(): pass")
            (root / ".env").write_text("TOKEN=not-for-analysis")
            (root / "credentials.json").write_text("{}")
            (root / ".git").mkdir()
            (root / ".git" / "tracked.py").write_text("SECRET = 1")
            names = [
                path.relative_to(root).as_posix()
                for path in discover_files(
                    _profile(root, managed_patterns=["**/*"], allow_full_codebase=True)
                )
            ]
        self.assertEqual(names, ["safe.py"])

    def test_scoped_registry_and_content_digest_follow_real_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            source = project / "service.py"
            source.write_text("def old_name(): pass")
            state = root / "runtime"
            first = _profile(project)
            second = _profile(project, scope="owner-b")
            register_project(first, root=state)
            register_project(second, root=state)
            self.assertEqual(load_project("owner-a", "p", root=state).scope, "owner-a")
            self.assertEqual(load_project("owner-b", "p", root=state).scope, "owner-b")
            before = project_content_digest(first)
            source.write_text("def new_name(): pass")
            self.assertNotEqual(project_content_digest(first), before)


class PreparationPlanSafetyTest(unittest.TestCase):
    def test_bounded_search_records_search_and_detail_reads_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve() / "project"
            project.mkdir()
            (project / "target.py").write_text(
                "def selected_symbol():\n    return 1\n",
                encoding="utf-8",
            )
            (project / "unrelated.py").write_text("value = 1\n", encoding="utf-8")
            plan = build_preparation_plan(
                _profile(project, managed_patterns=["**/*.py"]),
                query="selected_symbol",
            )
            evidence = plan["read_evidence"]
            self.assertEqual(plan["state"], "ready")
            self.assertEqual(evidence["safe_search_files"], ["target.py", "unrelated.py"])
            self.assertEqual(evidence["detail_read_files"], ["target.py"])
            self.assertEqual(evidence["symbol_analysis_files"], ["target.py"])
            self.assertEqual(evidence["read_files"], ["target.py"])
            self.assertEqual(
                plan["target"]["primary"]["match_evidence"][-1]["kind"],
                "symbol",
            )

    def test_safe_search_scans_entire_bounded_file_before_detail_read(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve() / "project"
            project.mkdir()
            (project / "late.py").write_text(
                ("# padding\n" * 9000) + "class LateSymbol:\n    pass\n",
                encoding="utf-8",
            )
            plan = build_preparation_plan(
                _profile(project, managed_patterns=["**/*.py"]),
                query="latesymbol",
            )
            self.assertEqual(plan["state"], "ready")
            self.assertEqual(plan["target"]["primary"]["path"], "late.py")
            self.assertEqual(plan["read_evidence"]["detail_read_files"], ["late.py"])

    def test_report_is_only_created_by_explicit_versioned_export(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            project = directory / "project"
            project.mkdir()
            (project / "service.py").write_text("def handle_failure(): pass")
            output = directory / "reports"
            plan = build_preparation_plan(
                _profile(project),
                query="handle_failure",
            )
            self.assertEqual(plan["state"], "ready")
            self.assertFalse(output.exists())
            first = export_preparation_report(plan, output, report_format="md")
            second = export_preparation_report(plan, output, report_format="md")
            self.assertEqual(first.name, "p_analysis_v1.md")
            self.assertEqual(second.name, "p_analysis_v2.md")
            self.assertNotEqual(first, second)


    def test_cli_owned_export_stays_versioned_and_confined(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            source = project / "target.py"
            source.write_text("def target():\n    return 1\n", encoding="utf-8")
            output = project / "state" / "preparation-artifacts" / "owner-a" / "p"
            profile = _profile(project)
            before = source.read_bytes()
            plan = build_preparation_plan(profile, query="target")
            first = export_preparation_report(plan, output, report_format="txt")
            second = export_preparation_report(plan, output, report_format="txt")
            self.assertEqual(first.name, "p_analysis_v1.txt")
            self.assertEqual(second.name, "p_analysis_v2.txt")
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                ["p_analysis_v1.txt", "p_analysis_v2.txt"],
            )
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(
                plan["read_evidence"]["evidence_digest"],
                build_preparation_plan(profile, query="target")["read_evidence"]["evidence_digest"],
            )
    def test_multiple_candidates_are_information_needed_not_primary_target(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            (project / "first.py").write_text("def target():\n    return 1\n", encoding="utf-8")
            (project / "second.py").write_text("def target():\n    return 2\n", encoding="utf-8")
            plan = build_preparation_plan(
                _profile(project, managed_patterns=["*.py"]),
                query="target",
            )
            self.assertEqual(plan["state"], "information_needed")
            self.assertEqual(plan["candidate_state"], "multiple_candidates")
            self.assertIn("multiple_candidates", plan["missing_information"])
            self.assertIsNone(plan["target"]["primary"])
            self.assertEqual(plan["read_evidence"]["read_files"], ["first.py", "second.py"])

    def test_code_analysis_defaults_to_screen_plan_and_verifies_explicit_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            (project / "service.py").write_text("def target():\n    return 1\n", encoding="utf-8")
            profile = _profile(project)
            state = root / "runtime"
            register_project(profile, root=state)
            recipe = SimpleNamespace(
                scope="owner-a",
                execution={"project_id": "p"},
                executor="code-analysis",
                output_type="json",
            )
            item = SimpleNamespace(
                task_id="analysis-task",
                scope="owner-a",
                title="target",
                body="",
                provenance={},
            )
            screen = execute_code_analysis(
                item,
                root / "artifacts" / "screen",
                recipe_definition=recipe,
                state_root=state,
            )
            self.assertEqual(screen["status"], "succeeded")
            self.assertEqual(screen["artifacts"], [])
            self.assertFalse(screen["report_requested"])
            self.assertEqual(screen["plan"]["state"], "ready")
            item.provenance = {"preparation_report": {"requested": True, "format": "md"}}
            report = execute_code_analysis(
                item,
                root / "artifacts" / "report",
                recipe_definition=recipe,
                state_root=state,
            )
            report_path = Path(report["artifacts"][0]["path"])
            self.assertEqual(report_path.suffix, ".md")
            self.assertIn("## 관련 위치", report_path.read_text(encoding="utf-8"))
            verified = verify_recipe_output(
                "analysis-recipe",
                item,
                report_path,
                recipe_definition=recipe,
            )
            self.assertEqual(verified["status"], "passed")
            self.assertEqual(verified["checks"][0]["actual"], "valid_report")

class XlsxPreviewSafetyTest(unittest.TestCase):
    def test_decompression_bomb_is_bounded(self):
        # C2: a small archive that inflates past the preview budget must fail
        # instead of being fully decompressed.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "bomb.xlsx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("xl/workbook.xml", "<workbook/>")
                archive.writestr(
                    "xl/sharedStrings.xml",
                    "<sst><si><t>" + ("A" * 50_000_000) + "</t></si></sst>",
                )
            with self.assertRaises(ValueError):
                _preview_xlsx(path)


if __name__ == "__main__":
    unittest.main()
