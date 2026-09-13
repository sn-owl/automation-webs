import json
import tempfile
import unittest
from pathlib import Path

from automation.executors.excel import _write_xlsx, _xlsx_rows
from automation.recipe_onboarding import (
    RecipeOnboardingError,
    activate_draft,
    create_draft,
    get_draft,
    review_draft,
    select_recipe,
    test_draft,
    update_draft,
)
from automation.recipes import get_recipe, load_recipes


class RecipeOnboardingTest(unittest.TestCase):
    scope = "owner-a"
    recipe_id = "custom-excel-v1"

    def payload(self, *, columns=None):
        columns = columns or ["name", "qty"]
        return {
            "recipe_id": self.recipe_id,
            "task_name": "사용자 정의 표 정리",
            "case_reference": "synthetic spreadsheet",
            "description": "합성 XLSX의 열 순서를 선언에 따라 변경한다",
            "scope": self.scope,
            "executor": "excel-table",
            "input_type": "xlsx",
            "output_type": "xlsx",
            "steps": [
                {
                    "operation": "select_attachment",
                    "parameters": {"type": "xlsx", "count": 1},
                },
                {
                    "operation": "parse_table",
                    "parameters": {"required_headers": ["name", "qty"]},
                },
                {
                    "operation": "map_columns",
                    "parameters": {"output_columns": columns},
                },
                {
                    "operation": "write_workbook",
                    "parameters": {"output_type": "xlsx"},
                },
                {
                    "operation": "verify_output",
                    "parameters": {
                        "checks": [
                            {"name": "columns", "value": columns},
                            {"name": "row_count", "value": 2},
                        ]
                    },
                },
            ],
            "required_conditions": ["single_spreadsheet", "required_headers"],
            "exclusion_conditions": ["missing_input", "invalid_table"],
            "success_checks": ["artifact_exists", "content_matches_source"],
            "approval_scope": "local_artifact_only",
            "applicability": {
                "title_or_body_contains_any": ["표 정리"],
                "attachment_type_in": ["xlsx"],
                "source_type_in": ["synthetic"],
            },
            "verification": {"checks": ["sha256"]},
            "error_policy": {
                "on_missing_input": "blocked",
                "on_parse_error": "blocked",
                "on_validation_error": "blocked",
            },
            "ai_policy": {
                "enabled": False,
                "expected_calls": 0,
                "max_calls": 0,
                "model_projection": "attachment_free",
            },
        }

    def task_file(self, root, *, scope=None, source_type="synthetic"):
        source = root / "source.xlsx"
        if not source.exists():
            _write_xlsx(source, [["name", "qty"], ["alpha", "2"], ["beta", "10"]])
        path = root / f"task-{scope or self.scope}.json"
        path.write_text(
            json.dumps(
                {
                    "task_id": f"task-{scope or self.scope}",
                    "source": {
                        "type": source_type,
                        "id": "fixture",
                        "external_id": "1",
                        "url": "https://fixture.invalid/1",
                    },
                    "received_at": "2026-09-10T00:00:00Z",
                    "title": "표 정리 요청",
                    "body": "name 및 qty 열을 정리",
                    "attachments": [
                        {
                            "name": source.name,
                            "type": "xlsx",
                            "raw_ref": str(source),
                            "extracted_ref": str(source),
                            "sha256": "0" * 64,
                        }
                    ],
                    "mask_table_ref": "none",
                    "contract_version": 2,
                    "scope": scope or self.scope,
                    "connector_id": "local-fixture",
                    "capture_id": "capture-1",
                    "revision": 1,
                    "completeness": "complete",
                    "provenance": {},
                    "source_completion_observed": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def activate(self, root, payload=None):
        create_draft(payload or self.payload(), root=root)
        result = test_draft(
            self.recipe_id,
            self.task_file(root),
            scope=self.scope,
            root=root,
        )
        self.assertEqual(result["status"], "passed")
        review_draft(
            self.recipe_id,
            scope=self.scope,
            actor="operator",
            reason="actual content matches declaration",
            root=root,
        )
        return activate_draft(
            self.recipe_id,
            scope=self.scope,
            actor="operator",
            reason="passed isolated verification",
            root=root,
        )

    def test_drafts_and_active_definitions_are_scope_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_draft(self.payload(), root=root)
            create_draft({**self.payload(), "scope": "owner-b"}, root=root)
            self.assertEqual(
                get_draft(self.recipe_id, scope="owner-a", root=root)["scope"],
                "owner-a",
            )
            self.assertEqual(
                get_draft(self.recipe_id, scope="owner-b", root=root)["scope"],
                "owner-b",
            )
            self.assertEqual(load_recipes(root=root, scope=self.scope), {})

    def test_actual_test_review_activation_is_immutable_and_reloadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pointer = self.activate(root)
            self.assertEqual(pointer["version"], 1)
            version_path = (
                root
                / "state"
                / "recipes"
                / self.scope
                / self.recipe_id
                / "1.json"
            )
            original = version_path.read_bytes()
            loaded = get_recipe(self.recipe_id, root=root, scope=self.scope)
            self.assertEqual(loaded.version, 1)
            self.assertEqual(loaded.scope, self.scope)
            self.assertEqual(version_path.read_bytes(), original)

    def test_identical_lifecycle_requests_are_idempotent_with_core_task_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_draft = create_draft(self.payload(), root=root)
            repeated_draft = create_draft(self.payload(), root=root)
            unchanged = update_draft(
                self.recipe_id,
                self.payload(),
                scope=self.scope,
                root=root,
            )
            self.assertEqual(repeated_draft["created_at"], first_draft["created_at"])
            self.assertEqual(unchanged["version"], 1)

            task_path = self.task_file(root)
            from automation.models import WorkItem
            from automation.task_store import put_task

            item = WorkItem.from_dict(json.loads(task_path.read_text(encoding="utf-8")))
            put_task(item, root=root)
            first_test = test_draft(
                self.recipe_id,
                task_id=item.task_id,
                scope=self.scope,
                root=root,
            )
            repeated_test = test_draft(
                self.recipe_id,
                task_id=item.task_id,
                scope=self.scope,
                root=root,
            )
            self.assertEqual(repeated_test["session_id"], first_test["session_id"])

            first_review = review_draft(
                self.recipe_id,
                scope=self.scope,
                actor="operator",
                reason="reviewed once",
                root=root,
            )
            repeated_review = review_draft(
                self.recipe_id,
                scope=self.scope,
                actor="operator",
                reason="reviewed once",
                root=root,
            )
            self.assertEqual(
                repeated_review["review"]["reviewed_at"],
                first_review["review"]["reviewed_at"],
            )
            first_pointer = activate_draft(
                self.recipe_id,
                scope=self.scope,
                actor="operator",
                reason="activate once",
                root=root,
            )
            repeated_pointer = activate_draft(
                self.recipe_id,
                scope=self.scope,
                actor="operator",
                reason="activate once",
                root=root,
            )
            self.assertEqual(repeated_pointer, first_pointer)
            registry = root / "state" / "recipes" / self.scope / self.recipe_id
            self.assertEqual([path.name for path in registry.glob("[0-9]*.json")], ["1.json"])


    def test_new_declaration_version_changes_actual_output_without_rewriting_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.activate(root)
            registry = root / "state" / "recipes" / self.scope / self.recipe_id
            v1_bytes = (registry / "1.json").read_bytes()
            first_artifact = Path(
                get_draft(self.recipe_id, scope=self.scope, root=root)["last_test"]
                ["result"]["artifacts"][0]["path"]
            )
            self.assertEqual(_xlsx_rows(first_artifact)[0], ["name", "qty"])

            update_draft(
                self.recipe_id,
                self.payload(columns=["qty", "name"]),
                scope=self.scope,
                root=root,
            )
            result = test_draft(
                self.recipe_id,
                self.task_file(root),
                scope=self.scope,
                root=root,
            )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(_xlsx_rows(Path(result["result"]["artifacts"][0]["path"]))[0], ["qty", "name"])
            review_draft(
                self.recipe_id,
                scope=self.scope,
                actor="operator",
                reason="v2 output verified",
                root=root,
            )
            pointer = activate_draft(
                self.recipe_id,
                scope=self.scope,
                actor="operator",
                reason="activate verified v2",
                root=root,
            )
            self.assertEqual(pointer["version"], 2)
            self.assertEqual(get_recipe(self.recipe_id, root=root, scope=self.scope).version, 2)
            self.assertEqual((registry / "1.json").read_bytes(), v1_bytes)

    def test_source_condition_and_tampered_definition_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.activate(root)
            wrong_source = self.task_file(root, source_type="email")
            from automation.models import WorkItem

            item = WorkItem.from_dict(json.loads(wrong_source.read_text(encoding="utf-8")))
            selection = select_recipe(self.recipe_id, item, root=root)
            self.assertEqual(selection["state"], "ineligible")
            self.assertIn("source_type_condition_missing", selection["reasons"])

            version_path = (
                root / "state" / "recipes" / self.scope / self.recipe_id / "1.json"
            )
            tampered = json.loads(version_path.read_text(encoding="utf-8"))
            tampered["description"] = "tampered"
            version_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                get_recipe(self.recipe_id, root=root, scope=self.scope)

    def test_a_failed_draft_test_records_where_the_failure_happened(self):
        # Without this the evidence said only {"error": "DispatchError",
        # "message": "test failed"}, so a draft could never be taken past
        # `tested` and the whole draft -> active lifecycle dead-ended.
        from unittest import mock

        import automation.dispatch as dispatch

        def explode(item, artifact_dir, **kwargs):
            raise RuntimeError("SENSITIVE_DOCUMENT_TEXT")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_draft(self.payload(), root=root)
            with mock.patch.dict(
                dispatch.EXECUTOR_DISPATCH, {"excel-table": explode}, clear=False
            ):
                result = test_draft(
                    self.recipe_id, self.task_file(root), scope=self.scope, root=root
                )
                repeated = test_draft(
                    self.recipe_id, self.task_file(root), scope=self.scope, root=root
                )
                retried = test_draft(
                    self.recipe_id,
                    self.task_file(root),
                    scope=self.scope,
                    root=root,
                    retry=True,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["diagnosis"]["stage"], "executor")
            self.assertEqual(result["diagnosis"]["cause_chain"], ["RuntimeError"])
            self.assertTrue(result["diagnosis"]["origin"])
            self.assertNotIn("SENSITIVE_DOCUMENT_TEXT", json.dumps(result))
            self.assertEqual(repeated["session_id"], result["session_id"])
            self.assertNotEqual(retried["session_id"], result["session_id"])

            evidence = json.loads(
                (root / "_sessions" / result["session_id"] / "test-evidence.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["diagnosis"], result["diagnosis"])

    def test_a_refused_plan_is_diagnosed_before_any_executor_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self.payload()
            payload["steps"][2] = {
                "operation": "run_shell",
                "parameters": {"command": "never"},
            }
            create_draft(payload, root=root)

            result = test_draft(
                self.recipe_id, self.task_file(root), scope=self.scope, root=root
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["diagnosis"]["stage"], "onboarding")

    def test_unsupported_operation_cannot_be_tested_or_reviewed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self.payload()
            payload["steps"][2] = {
                "operation": "run_shell",
                "parameters": {"command": "never"},
            }
            create_draft(payload, root=root)
            result = test_draft(
                self.recipe_id,
                self.task_file(root),
                scope=self.scope,
                root=root,
            )
            self.assertEqual(result["status"], "failed")
            with self.assertRaisesRegex(RecipeOnboardingError, "passed test"):
                review_draft(
                    self.recipe_id,
                    scope=self.scope,
                    actor="operator",
                    reason="must fail",
                    root=root,
                )


if __name__ == "__main__":
    unittest.main()
