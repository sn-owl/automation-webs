from __future__ import annotations

from dataclasses import replace
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import taskctl
from automation.models import WorkItem
from automation.task_store import put_task
from automation.dispatch import capability_binding
from automation import execution_service
from automation.recipes import RecipeDefinition, recipe_digest


class CapabilityBindingTest(unittest.TestCase):
    def run_cli(self, root: Path, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = taskctl.main(["--root", str(root), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def task(self) -> WorkItem:
        return WorkItem.from_dict(
            {
                "task_id": "capability-task",
                "source": {"type": "manual", "id": "source", "external_id": "1", "url": "https://example.invalid"},
                "received_at": "2026-09-10T00:00:00Z",
                "title": "capability binding",
                "body": "approval must bind the installed capability",
                "author": "SYNTHETIC",
                "attachments": [],
                "mask_table_ref": "local://mask",
                "contract_version": 2,
                "scope": "legacy",
                "connector_id": "local",
                "capture_id": "capture-1",
                "revision": 1,
                "completeness": "complete",
                "provenance": {"policy_version": "1"},
                "source_completion_observed": False,
            }
        )
    def test_approval_binds_runtime_capability_and_rejects_version_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.task()
            put_task(task, root=root)

            recipe_data = json.loads(
                (Path(__file__).parents[1] / "config" / "recipes" / "excel-table-v1.json").read_text(encoding="utf-8")
            )
            recipe = RecipeDefinition.from_dict(recipe_data)
            recipe_dir = root / "state" / "recipes" / recipe.scope / recipe.recipe_id
            recipe_dir.mkdir(parents=True)
            (recipe_dir / "1.json").write_text(json.dumps(recipe_data), encoding="utf-8")
            pointer = {
                "schema_version": 1,
                "scope": recipe.scope,
                "recipe_id": recipe.recipe_id,
                "version": 1,
                "definition_sha256": recipe_digest(recipe),
                "capability": capability_binding(recipe),
                "activation": {
                    "actor": "test",
                    "reason": "isolated capability contract",
                    "activated_at": "2026-09-10T00:00:00+00:00",
                },
            }
            pointer_path = recipe_dir / "active.json"
            pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

            selection = {"state": "satisfied", "evidence": [], "reasons": []}
            with mock.patch("automation.execution_service.select_recipe", return_value=selection), mock.patch.object(
                execution_service, "canonical_input_hash", return_value="a" * 64
            ):
                code, output, error = self.run_cli(
                    root,
                    "approve",
                    "capability-task",
                    "--actor",
                    "operator",
                    "--reason",
                    "bind",
                    "--recipe",
                    "excel-table-v1",
                )
            self.assertEqual(code, 0, error)
            approval = json.loads(output)
            binding = approval["details"]["execution"]
            self.assertEqual(binding["task_version"], 1)
            self.assertNotIn("revision", binding)
            self.assertEqual(binding["capability_version"], 2)
            self.assertEqual(binding["capability"]["version"], 2)
            self.assertEqual(binding["capability"]["executor"], "excel-table")
            self.assertTrue(binding["capability"]["implementation_digest"])
            policy_update = replace(
                task,
                capture_id="capture-2",
                provenance={"policy_version": "2"},
            )
            put_task(policy_update, root=root)
            with mock.patch.object(execution_service, "select_recipe", return_value=selection), mock.patch.object(
                execution_service, "canonical_input_hash", return_value="a" * 64
            ), mock.patch.object(
                execution_service, "dispatch_recipe", side_effect=AssertionError("executor reached")
            ):
                code, output, error = self.run_cli(
                    root,
                    "execute",
                    "capability-task",
                    "--recipe",
                    "excel-table-v1",
                )
            self.assertNotEqual(code, 0)
            self.assertEqual(output, "")
            self.assertIn("approval does not match", error)
            self.assertFalse((root / "state" / "executions").exists())

            pointer["capability"]["version"] = 99
            pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
            with mock.patch.object(execution_service, "select_recipe", return_value=selection), mock.patch.object(
                execution_service, "canonical_input_hash", return_value="a" * 64
            ), mock.patch.object(
                execution_service, "dispatch_recipe", side_effect=AssertionError("executor reached")
            ):
                code, output, error = self.run_cli(
                    root,
                    "execute",
                    "capability-task",
                    "--recipe",
                    "excel-table-v1",
                )

            self.assertNotEqual(code, 0)
            self.assertEqual(output, "")
            self.assertIn("approval does not match", error)
            self.assertIn("capability", error)
            self.assertFalse((root / "state" / "executions").exists())


if __name__ == "__main__":
    unittest.main()
