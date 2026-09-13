from __future__ import annotations

from dataclasses import replace

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import taskctl
from automation.dispatch import capability_binding
from automation.executors.excel import _write_xlsx, _xlsx_rows
from automation.recipes import RecipeDefinition, get_recipe, recipe_digest
from automation.events import EventLog
from automation.models import WorkItem
from automation.project_profile import ProjectProfile, register_project
from automation.execution import ExecutionRequest
from automation import execution_service
from automation.execution_store import put_execution
from automation.task_store import put_task


TIMESTAMP = "2026-09-02T10:15:00+00:00"


def make_task(task_id: str, title: str = "업무", body: str = "본문") -> WorkItem:
    return WorkItem.from_dict(
        {
            "task_id": task_id,
            "source": {
                "type": "gnuboard",
                "id": "board",
                "external_id": task_id,
                "url": "https://example.test/task",
            },
            "received_at": TIMESTAMP,
            "title": title,
            "body": body,
            "author": "alice",
            "attachments": [],
            "mask_table_ref": "mask/table-1",
        }
    )

def make_profile(root: Path, project_id: str, *, managed_directories: list[str] | None = None) -> ProjectProfile:
    return ProjectProfile.from_dict(
        {
            "schema_version": 1,
            "scope": "owner-a",
            "project_id": project_id,
            "name": project_id,
            "root": str(root),
            "managed_patterns": ["**/*.py"],
            "managed_directories": managed_directories or ["."],
            "stack": ["python"],
            "run_reference": None,
            "test_reference": None,
            "allow_full_codebase": False,
            "allow_external_ai_code": False,
            "database_paths": [],
        }
    )


class TaskctlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        put_task(make_task("task-2"), root=self.root)
        put_task(make_task("task-1"), root=self.root)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = taskctl.main(["--root", str(self.root), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def approve_args(self, *extra: str) -> tuple[str, ...]:
        return (
            "approve",
            "task-1",
            "--actor",
            "operator",
            "--reason",
            "검토 완료",
            "--task-version",
            "1",
            "--timestamp",
            TIMESTAMP,
            *extra,
        )

    def feedback_args(self, *, scope: str = "legacy", task_revision: str = "1") -> tuple[str, ...]:
        return (
            "feedback",
            "task-1",
            "--type",
            "input_supplement",
            "--scope",
            scope,
            "--task-revision",
            task_revision,
            "--actor",
            "reviewer",
            "--reason",
            "자료 보완",
        )

    def test_feedback_without_execution_key_records_input_supplement(self):
        code, output, error = self.run_cli(*self.feedback_args())
        self.assertEqual(code, 0, error)
        payload = json.loads(output)["feedback"]
        self.assertEqual(payload["type"], "input_supplement")
        self.assertEqual((payload["scope"], payload["task_revision"]), ("legacy", 1))
        self.assertEqual(EventLog(self.root / "state/events.jsonl").read()[-1]["type"], "feedback")

    def test_feedback_bound_to_another_scope_is_refused(self):
        code, _, error = self.run_cli(*self.feedback_args(scope="owner-a"))

        self.assertNotEqual(code, 0)
        self.assertIn("scope", error)

    def test_feedback_bound_to_a_stale_task_revision_is_refused(self):
        code, _, error = self.run_cli(*self.feedback_args(task_revision="2"))

        self.assertNotEqual(code, 0)
        self.assertIn("revision", error)

    # -- R07: actual completion is its own state ------------------------

    def confirm_args(self, *extra: str) -> tuple[str, ...]:
        return (
            "confirm",
            "task-1",
            "--task-version",
            "1",
            "--actor",
            "operator",
            "--reason",
            "직접 처리 완료",
            *extra,
        )

    def completion_events(self) -> list[dict]:
        return [
            event
            for event in EventLog(self.root / "state/events.jsonl").read()
            if event.get("type") == "actual_completion"
        ]

    def test_work_finished_by_hand_can_be_recorded_as_actually_completed(self):
        # Most real work never runs through an Executor. Requiring a succeeded
        # execution made that work impossible to complete.
        code, output, error = self.run_cli(*self.confirm_args("--basis", "manual"))

        self.assertEqual(code, 0, error)
        completion = json.loads(output)["actual_completion"]
        self.assertEqual(completion["basis"], "manual")
        self.assertEqual(completion["task_version"], 1)
        self.assertNotIn("execution_key", completion)
        self.assertEqual(len(self.completion_events()), 1)

    def test_work_finished_in_the_source_system_can_be_recorded(self):
        code, output, error = self.run_cli(*self.confirm_args("--basis", "external"))

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["actual_completion"]["basis"], "external")

    def test_a_completion_without_an_execution_must_name_its_basis(self):
        code, _, error = self.run_cli(*self.confirm_args())

        self.assertNotEqual(code, 0)
        self.assertIn("--basis", error)

    def test_a_manual_completion_does_not_take_an_execution_key(self):
        code, _, error = self.run_cli(
            *self.confirm_args("--basis", "manual", "--execution-key", "abc123")
        )

        self.assertNotEqual(code, 0)
        self.assertIn("execution-key", error)

    def test_recording_the_same_manual_completion_twice_is_idempotent(self):
        self.run_cli(*self.confirm_args("--basis", "manual"))
        code, output, error = self.run_cli(*self.confirm_args("--basis", "manual"))

        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["idempotent"])
        self.assertEqual(len(self.completion_events()), 1)

    def test_a_task_already_completed_is_not_completed_again_on_another_basis(self):
        self.run_cli(*self.confirm_args("--basis", "manual"))

        code, _, error = self.run_cli(*self.confirm_args("--basis", "external"))

        self.assertNotEqual(code, 0)
        self.assertIn("already", error)
        self.assertEqual(len(self.completion_events()), 1)

    def test_a_stale_task_version_cannot_be_completed(self):
        code, _, error = self.run_cli(
            "confirm",
            "task-1",
            "--task-version",
            "2",
            "--actor",
            "operator",
            "--reason",
            "직접 처리 완료",
            "--basis",
            "manual",
        )

        self.assertNotEqual(code, 0)
        self.assertIn("stale task version", error)

    def test_observing_the_source_as_completed_is_not_a_completion(self):
        # U12: source 완료 감지는 관찰값이며 Core 의 실제 업무 완료를 자동으로
        # 바꾸지 않는다. Only an explicit operator action completes a task.
        put_task(
            WorkItem.from_dict(
                dict(
                    make_task("task-3").to_dict(),
                    contract_version=2,
                    scope="owner-a",
                    connector_id="c1",
                    capture_id="task-3",
                    revision=1,
                    completeness="complete",
                    provenance={"policy_version": "1"},
                    source_completion_observed=True,
                )
            ),
            root=self.root,
        )

        self.assertEqual(self.completion_events(), [])

    def test_list_outputs_tasks_in_deterministic_order(self):
        code, output, error = self.run_cli("list")

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        payload = json.loads(output)
        self.assertEqual([item["task_id"] for item in payload], ["task-1", "task-2"])
        self.assertEqual(output, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def test_prepare_can_load_a_registered_profile(self):
        project = self.root / "project"
        project.mkdir()
        (project / "app.py").write_text("def target():\n    return 1\n", encoding="utf-8")
        register_project(make_profile(project, "demo"), root=self.root)

        code, output, error = self.run_cli(
            "prepare",
            "--scope",
            "owner-a",
            "--project",
            "demo",
            "--query",
            "target",
        )

        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["target"]["primary"]["path"], "app.py")

    def test_prepare_persists_plan_and_explicit_report_status_for_task_detail(self):
        import dashboard

        project = self.root / "project-with-report"
        project.mkdir()
        (project / "target.py").write_text("def target():\n    return 1\n", encoding="utf-8")
        register_project(make_profile(project, "report-profile"), root=self.root)
        code, output, error = self.run_cli(
            "prepare",
            "--scope",
            "owner-a",
            "--project",
            "report-profile",
            "--task-id",
            "task-1",
            "--query",
            "target",
            "--export",
            "md",
        )
        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        self.assertEqual(plan["report"]["status"], "created")
        self.assertEqual(
            plan["report"]["path"],
            "state/preparation-artifacts/owner-a/report-profile/report-profile_analysis_v1.md",
        )
        detail = dashboard.load_read_model(root=self.root)["tasks"][0]
        self.assertEqual(detail["task_id"], "task-1")
        self.assertEqual(detail["preparation"]["state"], "ready")
        self.assertEqual(detail["preparation"]["report"], plan["report"])

    def test_project_cli_registers_lists_and_prepares_scoped_profile(self):
        project = self.root / "registered-project"
        project.mkdir()
        (project / "src").mkdir()
        (project / "src" / "app.py").write_text(
            "def target():\n    return 'registered'\n",
            encoding="utf-8",
        )
        profile_file = self.root / "profile.json"
        profile_file.write_text(
            json.dumps(make_profile(project, "registered", managed_directories=["src"]).to_dict()),
            encoding="utf-8",
        )

        code, output, error = self.run_cli(
            "project", "register", "--profile", str(profile_file)
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["project_id"], "registered")
        code, output, error = self.run_cli("project", "list", "--scope", "owner-a")
        self.assertEqual(code, 0, error)
        self.assertEqual([item["project_id"] for item in json.loads(output)], ["registered"])
        code, output, error = self.run_cli(
            "project", "show", "registered", "--scope", "owner-a"
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["managed_directories"], ["src"])

        code, output, error = self.run_cli(
            "prepare",
            "--scope",
            "owner-a",
            "--project",
            "registered",
            "--query",
            "target",
        )
        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        self.assertEqual(plan["project_id"], "registered")
        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["read_evidence"]["read_files"], ["src/app.py"])

    def test_runtime_only_code_recipe_executes_through_scoped_registry(self):
        project = self.root / "runtime-code-project"
        project.mkdir()
        (project / "service.py").write_text(
            "def target():\n    return 'runtime'\n",
            encoding="utf-8",
        )
        register_project(make_profile(project, "runtime-code"), root=self.root)
        task_payload = make_task("runtime-code-task", title="target analysis").to_dict()
        task_payload.update(
            {
                "contract_version": 2,
                "scope": "owner-a",
                "connector_id": "local-fixture",
                "capture_id": "runtime-code-capture",
                "revision": 1,
                "completeness": "complete",
                "provenance": {},
                "source_completion_observed": False,
            }
        )
        put_task(WorkItem.from_dict(task_payload), root=self.root)
        recipe = RecipeDefinition.from_dict(
            {
                "recipe_id": "code-analysis-alpha",
                "description": "runtime-only static analysis",
                "executor": "code-analysis",
                "input_type": "code",
                "output_type": "json",
                "scope": "owner-a",
                "steps": [{"operation": "analyze_code", "parameters": {}}],
                "execution": {"project_id": "runtime-code"},
                "applicability": {"title_or_body_contains_any": ["target"]},
            }
        )
        registry = self.root / "state" / "recipes" / "owner-a" / recipe.recipe_id
        registry.mkdir(parents=True)
        (registry / "1.json").write_text(json.dumps(recipe.to_dict()), encoding="utf-8")
        (registry / "active.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "owner-a",
                    "recipe_id": recipe.recipe_id,
                    "version": 1,
                    "definition_sha256": recipe_digest(recipe),
                    "capability": capability_binding(recipe),
                    "activation": {"actor": "test", "reason": "runtime-only"},
                }
            ),
            encoding="utf-8",
        )
        approved, _, error = self.run_cli(
            "approve",
            "runtime-code-task",
            "--actor",
            "operator",
            "--reason",
            "runtime recipe",
            "--recipe",
            recipe.recipe_id,
        )
        self.assertEqual(approved, 0, error)
        executed, output, error = self.run_cli(
            "execute",
            "runtime-code-task",
            "--recipe",
            recipe.recipe_id,
        )
        self.assertEqual(executed, 0, error)
        result = json.loads(output)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["recipe_id"], recipe.recipe_id)
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(result["plan"]["project_id"], "runtime-code")
        self.assertEqual(result["artifacts"], [])

    def test_prepare_records_scope_and_missing_location_reason(self):
        project = self.root / "scoped"
        (project / "src").mkdir(parents=True)
        (project / "outside").mkdir()
        (project / "src" / "main.py").write_text(
            "def target():\n    pass\n",
            encoding="utf-8",
        )
        (project / "outside" / "hidden.py").write_text(
            "missing = True\n",
            encoding="utf-8",
        )
        register_project(
            make_profile(project, "scoped", managed_directories=["src"]),
            root=self.root,
        )
        code, output, error = self.run_cli(
            "prepare",
            "--scope",
            "owner-a",
            "--project",
            "scoped",
            "--query",
            "missing",
        )
        self.assertEqual(code, 0, error)
        plan = json.loads(output)
        self.assertEqual(plan["state"], "information_needed")
        self.assertIn("location_unknown", plan["missing_information"])
        self.assertEqual(plan["read_evidence"]["read_files"], [])
        self.assertEqual(plan["read_evidence"]["safe_search_files"], ["src/main.py"])

    def test_execute_uses_declared_recipe_for_content_and_verification(self):
        source = self.root / "input.xlsx"
        _write_xlsx(
            source,
            [
                ["Name", "Score", "Group"],
                ["alpha", "30", "keep"],
                ["beta", "10", "keep"],
                ["gamma", "20", "keep"],
            ],
        )
        before = source.read_bytes()
        task = WorkItem.from_dict(
            {
                **make_task("runtime-recipe-task").to_dict(),
                "attachments": [
                    {
                        "name": source.name,
                        "type": "xlsx",
                        "raw_ref": str(source),
                        "extracted_ref": str(source),
                        "sha256": hashlib.sha256(before).hexdigest(),
                    }
                ],
            }
        )
        put_task(task, root=self.root)
        recipe = replace(
            get_recipe("excel-table-v1"),
            steps=(
                {"operation": "select_attachment", "parameters": {"type": "xlsx", "count": 1}},
                {"operation": "parse_table", "parameters": {"required_headers": ["Name", "Score"]}},
                {"operation": "map_columns", "parameters": {"output_columns": ["Score", "Name"]}},
                {"operation": "filter_rows", "parameters": {"column": "Score", "lt": 30}},
                {"operation": "sort_rows", "parameters": {"column": "Name", "descending": False, "numeric": False}},
                {"operation": "write_workbook", "parameters": {"output_type": "xlsx"}},
                {"operation": "verify_output", "parameters": {"checks": ["artifact_exists", {"name": "columns", "value": ["Score", "Name"]}, {"name": "min_rows", "value": 2}]}},
            ),
            verification={"checks": ["artifact_exists", {"name": "columns", "value": ["Score", "Name"]}, {"name": "min_rows", "value": 2}]},
        )
        satisfied = {"state": "satisfied", "evidence": [], "reasons": []}
        with mock.patch.object(execution_service, "get_recipe", return_value=recipe), mock.patch.object(
            execution_service, "select_recipe", return_value=satisfied
        ), mock.patch.object(
            execution_service,
            "load_active_metadata",
            return_value={"version": 1, "capability": {"version": 1}},
        ):
            approved, _, error = self.run_cli(
                "approve",
                "runtime-recipe-task",
                "--actor",
                "operator",
                "--reason",
                "runtime declaration approved",
                "--recipe",
                "excel-table-v1",
            )
            self.assertEqual(approved, 0, error)
            executed, output, error = self.run_cli(
                "execute",
                "runtime-recipe-task",
                "--recipe",
                "excel-table-v1",
            )

        self.assertEqual(executed, 0, error)
        result = json.loads(output)
        artifact = self.root / result["artifacts"][0]["path"]
        self.assertEqual(result["state"], "succeeded")
        expected_rows = [["Score", "Name"], ["10", "beta"], ["20", "gamma"]]
        self.assertEqual(_xlsx_rows(artifact), expected_rows)
        self.assertEqual(result["preview"], expected_rows)
        self.assertEqual(result["changes"]["output_columns"], ["Score", "Name"])
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(
            {check["name"]: check["passed"] for check in result["verification"]["checks"]}["columns"],
            True,
        )
        self.assertEqual(source.read_bytes(), before)

    def test_show_outputs_task_detail(self):
        code, output, error = self.run_cli("show", "task-1")

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(json.loads(output), make_task("task-1").to_dict())

    def test_missing_task_fails_for_show_approve_and_execute(self):
        commands = (
            ("show", "missing"),
            (
                "approve",
                "missing",
                "--actor",
                "operator",
                "--reason",
                "검토 완료",
                "--task-version",
                "1",
                "--timestamp",
                TIMESTAMP,
            ),
            (
                "execute",
                "missing",
                "--recipe",
                "restarea-hwpx-to-xls",
                "--input-hash",
                "a" * 64,
            ),
        )
        for command in commands:
            with self.subTest(command=command):
                code, output, error = self.run_cli(*command)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertIn("task not found", error)

    def test_approve_validates_decision_and_records_event(self):
        code, output, error = self.run_cli(*self.approve_args())

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        decision = json.loads(output)
        self.assertEqual(decision["action"], "approve")
        events = EventLog(self.root / "state" / "events.jsonl").read()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "decision")
        self.assertEqual(events[0]["decision"], decision)
    def test_confirm_records_actual_completion_separately_and_is_idempotent(self):
        request = ExecutionRequest(
            task_id="task-1",
            recipe_id="restarea-hwpx-to-xls",
            input_hash="a" * 64,
            state="succeeded",
            approval="event:approval",
        )
        put_execution(request, root=self.root)
        EventLog(self.root / "state" / "events.jsonl").append(
            {
                "event_id": f"verification-{request.execution_key}",
                "type": "verification",
                "task_id": "task-1",
                "state": "verification_recorded",
                "stage": "execution",
                "execution": request.to_dict(),
                "verification": {
                    "status": "passed",
                    "handoff": {"required": True, "status": "pending"},
                },
            }
        )

        args = (
            "confirm",
            "task-1",
            "--execution-key",
            request.execution_key,
            "--task-version",
            "1",
            "--actor",
            "operator",
            "--reason",
            "실제 업무를 확인했습니다",
        )
        code, output, error = self.run_cli(*args)
        self.assertEqual(code, 0, error)
        payload = json.loads(output)
        self.assertFalse(payload["idempotent"])
        self.assertEqual(payload["actual_completion"]["task_version"], 1)

        events = EventLog(self.root / "state" / "events.jsonl").read()
        self.assertEqual([event["type"] for event in events if event["task_id"] == "task-1"][-2:], ["verification", "actual_completion"])
        completion = [event for event in events if event.get("type") == "actual_completion"][-1]
        self.assertEqual(completion["actual_completion"]["actor"], "operator")
        self.assertTrue(completion["actual_completion"]["reason_recorded"])

        code, output, error = self.run_cli(*args)
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["idempotent"])
        self.assertEqual(
            len([event for event in EventLog(self.root / "state" / "events.jsonl").read() if event.get("type") == "actual_completion"]),
            1,
        )

    def test_confirm_rejects_stale_version_and_cross_task_execution(self):
        request = ExecutionRequest(
            task_id="task-2",
            recipe_id="restarea-hwpx-to-xls",
            input_hash="b" * 64,
            state="succeeded",
            approval="event:approval",
        )
        put_execution(request, root=self.root)
        EventLog(self.root / "state" / "events.jsonl").append(
            {
                "event_id": f"verification-{request.execution_key}",
                "type": "verification",
                "task_id": "task-2",
                "state": "verification_recorded",
                "verification": {"status": "passed", "handoff": {"required": True, "status": "pending"}},
                "execution": request.to_dict(),
            }
        )
        code, _, error = self.run_cli(
            "confirm", "task-2", "--execution-key", request.execution_key,
            "--task-version", "2", "--actor", "operator", "--reason", "확인",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("stale task version", error)
        code, _, error = self.run_cli(
            "confirm", "task-1", "--execution-key", request.execution_key,
            "--task-version", "1", "--actor", "operator", "--reason", "확인",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("does not belong to task", error)

    def test_decision_actor_or_reason_is_required(self):
        for option in ("--actor", "--reason"):
            args = list(self.approve_args())
            index = args.index(option)
            del args[index : index + 2]
            with self.subTest(option=option):
                code, output, error = self.run_cli(*args)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertIn(option.lstrip("-"), error)

    def test_modify_requires_non_empty_json_object_details(self):
        for details in (None, "{}", "[]", "not-json"):
            args = list(
                self.approve_args("--details", details)
                if details is not None
                else self.approve_args()
            )
            args[0] = "modify"
            if details is None:
                args.extend([])
            with self.subTest(details=details):
                code, output, error = self.run_cli(*args)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertIn("details", error)

    def test_reject_accepts_a_structured_signal(self):
        args = list(self.approve_args("--details", '{"reject_signal":"out_of_scope"}'))
        args[0] = "reject"

        code, output, error = self.run_cli(*args)

        self.assertEqual(code, 0, error)
        self.assertEqual(error, "")
        decision = json.loads(output)
        self.assertEqual(decision["details"], {"reject_signal": "out_of_scope"})
        self.assertEqual(
            EventLog(self.root / "state" / "events.jsonl").read()[0]["decision"],
            decision,
        )

    def test_stale_approval_version_fails(self):
        args = list(self.approve_args())
        args[args.index("1")] = "2"

        code, output, error = self.run_cli(*args)

        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("stale approval", error)
        self.assertFalse((self.root / "state" / "events.jsonl").exists())

    def test_approve_does_not_start_execute(self):
        code, _, _ = self.run_cli(*self.approve_args())

        self.assertEqual(code, 0)
        self.assertFalse((self.root / "state" / "executions").exists())

    def test_execute_without_approval_is_blocked(self):
        code, output, error = self.run_cli(
            "execute",
            "task-1",
            "--recipe",
            "restarea-hwpx-to-xls",
            "--input-hash",
            "a" * 64,
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("approval", error)

    def test_later_reject_revokes_an_earlier_approval(self):
        approve, _, _ = self.run_cli(*self.approve_args())
        self.assertEqual(approve, 0)
        reject_args = list(self.approve_args())
        reject_args[0] = "reject"
        reject_code, _, _ = self.run_cli(*reject_args)
        self.assertEqual(reject_code, 0)

        code, output, error = self.run_cli(
            "execute",
            "task-1",
            "--recipe",
            "restarea-hwpx-to-xls",
            "--input-hash",
            "a" * 64,
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("no recorded approval", error)

    def test_unknown_recipe_is_rejected(self):
        approve, _, approve_error = self.run_cli(*self.approve_args())
        self.assertEqual(approve, 0, approve_error)
        code, output, error = self.run_cli(
            "execute",
            "task-1",
            "--recipe",
            "unknown-recipe",
            "--input-hash",
            "a" * 64,
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("unknown recipe", error)

    def test_hermes_approve_command_is_not_a_taskctl_command(self):
        code, output, error = self.run_cli("/approve", "task-1")

        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("invalid choice", error)

    def test_event_does_not_include_task_body_or_secret_value(self):
        secret = "Authorization: Bearer raw-token-value"
        put_task(make_task("secret-task", body=secret), root=self.root)
        args = list(self.approve_args())
        args[1] = "secret-task"
        args[args.index("검토 완료")] = "업무 승인"

        code, _, error = self.run_cli(*args)

        self.assertEqual(code, 0, error)
        event_bytes = (self.root / "state" / "events.jsonl").read_bytes()
        self.assertNotIn(secret.encode(), event_bytes)
        self.assertNotIn(b"body", event_bytes)


if __name__ == "__main__":
    unittest.main()
