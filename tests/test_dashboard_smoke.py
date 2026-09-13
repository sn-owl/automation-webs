import os
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from automation.events import EventLog
from automation.models import WorkItem
from automation.task_store import put_task


ROOT = Path(__file__).parents[1]
FIXTURE_WORK_ITEM = {
    "task_id": "yeonje-13452",
    "source": {
        "type": "board",
        "id": "yeonje",
        "external_id": "13452",
        "url": "https://example.invalid/bbs/board.php?bo_table=yeonje",
    },
    "received_at": "2026-08-27T10:20:00+09:00",
    "title": "무더위쉼터 현황 현행화",
    "body": "PRIVATE_BODY_MARKER 위치: SANITIZED_TARGET",
    "author": "DEPARTMENT_001",
    "attachments": [],
    "mask_table_ref": "local://masks/yeonje-13452.json",
}


def _seed(root: Path) -> None:
    put_task(WorkItem.from_dict(FIXTURE_WORK_ITEM), root=root)
    EventLog(root / "state" / "events.jsonl").append(
        {
            "event_id": "yeonje-13452:assessed",
            "type": "assessment",
            "task_id": "yeonje-13452",
            "state": "review_required",
            "assessment": {
                "automation_level": "ready",
                "risk": "local_artifact_only",
                "confidence": 0.9,
                "evidence": ["eval:heat-shelter-ready 조건과 일치"],
                "recipe": "restarea-hwpx-to-xls",
            },
        }
    )


class DashboardImportTest(unittest.TestCase):
    def test_module_imports_without_streamlit(self):
        # Streamlit is an optional presentation dependency; the dashboard's
        # data and decision layer must import and be testable without it.
        result = subprocess.run(
            [sys.executable, "-c", "import dashboard; print(dashboard.__name__)"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dashboard", result.stdout)


class DashboardReadTest(unittest.TestCase):
    def test_read_model_is_projected_without_raw_source_content(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _seed(root)
            model = dashboard.load_read_model(root=root)

        self.assertEqual(list(model), ["tasks", "status_counts"])
        self.assertEqual(len(model["tasks"]), 1)
        detail = model["tasks"][0]
        self.assertEqual(detail["task_id"], "yeonje-13452")
        self.assertEqual(detail["state"], "review_required")
        self.assertEqual(detail["assessment"]["automation_level"], "ready")
        self.assertEqual(detail["assessment"]["risk"], "local_artifact_only")

        serialized = json.dumps(model, ensure_ascii=False)
        self.assertNotIn("PRIVATE_BODY_MARKER", serialized)
        self.assertNotIn("bbs/board.php", serialized)

    def test_read_model_projects_business_labels_and_evidence(self):
        import dashboard

        task = dict(FIXTURE_WORK_ITEM, title="팝업 게시 요청")
        events = [
            {
                "event_id": "popup-classified",
                "task_id": "yeonje-13452",
                "stage": "classification",
                "state": "classified",
                "classification": {
                    "responsibility": "content",
                    "task_type": "image_popup",
                    "size": "simple",
                    "confidence": 0.9,
                    "evidence": ["팝업 게시 요청이 제목에 명시됨"],
                },
                "assessment": {
                    "automation_level": "manual",
                    "risk": "remote_write",
                    "confidence": 0.8,
                    "evidence": ["외부 홈페이지 변경이 필요함"],
                },
            }
        ]

        model = dashboard.build_dashboard_read_model(
            [WorkItem.from_dict(task)], events
        )
        detail = model["tasks"][0]
        self.assertEqual(detail["display_name"], "팝업 게시 요청")
        self.assertEqual(detail["domain"], "homepage_content")
        self.assertEqual(detail["domain_label"], "홈페이지 콘텐츠")
        self.assertEqual(
            detail["classification"]["evidence"],
            ["팝업 게시 요청이 제목에 명시됨"],
        )
        self.assertEqual(detail["masking"], "recorded")

    def test_missing_root_yields_empty_model_rather_than_raising(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            model = dashboard.load_read_model(root=Path(directory).resolve() / "absent")
        self.assertEqual(model, {"tasks": [], "status_counts": {}})


    def test_pipeline_model_shows_all_local_stages_without_source_content(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            downloads = root / "downloads"
            bundle = downloads / "yeonje-13452--title"
            (bundle / "attachments").mkdir(parents=True)
            (bundle / "page.html").write_text("PRIVATE_PAGE_BODY", encoding="utf-8")
            (bundle / "attachments" / "request.hwpx").write_bytes(b"hwpx")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "capture_id": "yeonje-13452",
                        "page": {"path": "page.html"},
                        "attachments": [{"path": "attachments/request.hwpx"}],
                    }
                ),
                encoding="utf-8",
            )
            (bundle / "_READY").write_text("yeonje-13452\n", encoding="utf-8")
            inbox = root / "inbox" / "yeonje-13452"
            (inbox / "attachments").mkdir(parents=True)
            (inbox / "work_item.json").write_text("{}", encoding="utf-8")
            _seed(root)
            EventLog(root / "state" / "events.jsonl").append(
                {
                    "event_id": "yeonje-13452:hermes",
                    "task_id": "yeonje-13452",
                    "stage": "hermes",
                    "state": "blocked",
                }
            )
            execution = root / "state" / "executions" / "execution-key.json"
            execution.parent.mkdir(parents=True)
            execution.write_text(
                json.dumps(
                    {
                        "task_id": "yeonje-13452",
                        "recipe_id": "restarea-hwpx-to-xls",
                        "state": "failed",
                    }
                ),
                encoding="utf-8",
            )

            model = dashboard.load_pipeline_model(
                root=root,
                downloads_root=downloads,
                inbox_root=root / "inbox",
            )

        self.assertEqual(model["bundles"][0]["state"], "complete")
        self.assertEqual(model["inbox"][0]["state"], "accepted")
        self.assertEqual(model["events"][-1]["stage"], "hermes")
        self.assertEqual(model["executions"][0]["state"], "failed")
        encoded = json.dumps(model, ensure_ascii=False)
        self.assertNotIn("PRIVATE_PAGE_BODY", encoded)

    def test_next_task_id_advances_in_filtered_order(self):
        import dashboard

        tasks = [{"task_id": "first"}, {"task_id": "second"}, {"task_id": "last"}]
        self.assertEqual(dashboard._next_task_id(tasks, "first"), "second")
        self.assertEqual(dashboard._next_task_id(tasks, "second"), "last")
        self.assertIsNone(dashboard._next_task_id(tasks, "last"))
        self.assertIsNone(dashboard._next_task_id(tasks, "missing"))
    def test_decision_status_is_separate_from_hermes_processing_state(self):
        import dashboard

        pending = {"state": "review_required"}
        decided = {"state": "review_required", "decision": {"action": "defer"}}
        self.assertEqual(dashboard._review_status(pending), "검토 필요")
        self.assertTrue(dashboard._is_decision_pending(pending))
        self.assertEqual(dashboard._review_status(decided), "보류 중")
        self.assertFalse(dashboard._is_decision_pending(decided))

    def test_auto_completed_work_leaves_the_review_queue(self):
        # The queue used to be "every task with no decision event", so work the
        # approval policy settled sat in it forever with nothing to click.
        import dashboard

        settled = {"state": "completed"}
        executed = {"state": "succeeded"}
        self.assertFalse(dashboard._is_decision_pending(settled))
        self.assertFalse(dashboard._is_decision_pending(executed))
        self.assertEqual(dashboard._review_status(settled), "준비 완료 · 실제 업무 완료 대기")
        self.assertTrue(dashboard._is_decision_pending({"state": "review_required"}))






class DashboardPathDefaultsTest(unittest.TestCase):
    def test_defaults_follow_runner_storage_layout(self):
        import dashboard

        repository_root = Path(dashboard.__file__).resolve().parent
        with patch.dict(os.environ, {"UPMU_DASHBOARD_RUNTIME": ""}, clear=False):
            self.assertEqual(
                dashboard._default_runtime_root(repository_root),
                repository_root / "runtime",
            )
        runtime = repository_root / "runtime"
        self.assertEqual(dashboard._default_inbox_root(runtime), runtime / "inbox")
        self.assertEqual(
            dashboard._default_downloads_root(),
            Path.home() / "Downloads" / "upmuzadong-inbox",
        )

    def test_explicit_runtime_override_wins(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory).resolve() / "custom-runtime"
            with patch.dict(os.environ, {"UPMU_DASHBOARD_RUNTIME": str(explicit)}):
                self.assertEqual(dashboard._default_runtime_root("."), explicit)


class DashboardDecisionRoutingTest(unittest.TestCase):
    def test_a_refused_identifier_never_reports_success(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _seed(root)
            with self.assertRaises(ValueError):
                dashboard.submit_decision(
                    "-h", "approve", actor="operator", reason="r", version=1, root=root
                )
            recorded = (root / "state" / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn('"decision"', recorded)



    def test_submit_execution_routes_through_shared_core_service(self):
        import dashboard

        with patch("dashboard.execute_task", return_value={"state": "succeeded"}) as execute:
            code = dashboard.submit_execution(
                "yeonje-13452",
                "restarea-hwpx-to-xls",
                root=Path("runtime"),
            )

        self.assertEqual(code, 0)
        execute.assert_called_once_with(
            "yeonje-13452",
            "restarea-hwpx-to-xls",
            root=Path("runtime"),
        )


    def test_submit_manual_and_external_completion_use_shared_core_service(self):
        import dashboard

        for basis in ("manual", "external"):
            with self.subTest(basis=basis), patch(
                "dashboard.confirm_task", return_value={"idempotent": False}
            ) as confirm:
                code = dashboard.submit_confirmation(
                    "task-1",
                    basis=basis,
                    actor="owner",
                    reason="actual work checked",
                    version=2,
                    root=Path("runtime"),
                )

            self.assertEqual(code, 0)
            confirm.assert_called_once_with(
                "task-1",
                execution_key=None,
                basis=basis,
                task_version=2,
                actor="owner",
                reason="actual work checked",
                root=Path("runtime"),
            )

    def test_submit_recovery_routes_through_shared_core_service(self):
        import dashboard

        with patch("dashboard.recover_execution", return_value={"recovered": True}) as recover:
            code = dashboard.submit_recovery(
                "execution-key",
                actor="owner",
                reason="retry after inspection",
                root=Path("runtime"),
            )

        self.assertEqual(code, 0)
        recover.assert_called_once_with(
            "execution-key",
            actor="owner",
            reason="retry after inspection",
            root=Path("runtime"),
        )


    def test_submit_registered_preparation_uses_scoped_project_id(self):
        import dashboard

        with patch("dashboard.taskctl.main", return_value=0) as main:
            code = dashboard.submit_registered_preparation(
                "demo",
                "find the relevant files",
                scope="owner-a",
                root=Path("runtime"),
            )

        self.assertEqual(code, 0)
        main.assert_called_once_with(
            [
                "prepare",
                "--scope",
                "owner-a",
                "--project",
                "demo",
                "--query",
                "find the relevant files",
                "--root",
                "runtime",
            ]
        )

    def test_submit_registered_preparation_for_task_can_request_report(self):
        import dashboard

        with patch("dashboard.taskctl.main", return_value=0) as main:
            code = dashboard.submit_registered_preparation(
                "demo",
                "find the relevant files",
                scope="owner-a",
                root=Path("runtime"),
                task_id="task-1",
                report_format="md",
            )

        self.assertEqual(code, 0)
        main.assert_called_once_with(
            [
                "prepare",
                "--scope",
                "owner-a",
                "--project",
                "demo",
                "--query",
                "find the relevant files",
                "--root",
                "runtime",
                "--task-id",
                "task-1",
                "--export",
                "md",
            ]
        )

    def test_submit_decision_records_one_core_event(self):

        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _seed(root)
            with redirect_stdout(io.StringIO()):
                code = dashboard.submit_decision(
                    "yeonje-13452",
                    "approve",
                    actor="operator",
                    reason="승인",
                    version=1,
                    root=root,
                )
            self.assertEqual(code, 0)
            events = [
                json.loads(line)
                for line in (root / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        decisions = [event for event in events if event.get("type") == "decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"]["action"], "approve")
        self.assertEqual(decisions[0]["decision"]["task_id"], "yeonje-13452")
        self.assertEqual(decisions[0]["decision"]["actor"], "operator")

    def test_stale_task_version_is_rejected_by_the_core_contract(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _seed(root)
            with redirect_stdout(io.StringIO()):
                code = dashboard.submit_decision(
                    "yeonje-13452",
                    "approve",
                    actor="operator",
                    reason="승인",
                    version=99,
                    root=root,
                )
            self.assertNotEqual(code, 0)
            events = (root / "state" / "events.jsonl").read_text(encoding="utf-8")

        self.assertNotIn('"type": "decision"', events)
        self.assertNotIn('"approve"', events)



class DashboardExecutionProjectionTest(unittest.TestCase):
    def test_rows_include_version_attempt_and_quarantine(self):
        import dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            execution_dir = root / "state" / "executions"
            execution_dir.mkdir(parents=True)
            (execution_dir / "k.json").write_text(
                json.dumps({"task_id": "task-a", "recipe_id": "r", "state": "failed",
                            "result_version": 2, "attempt": 1}), encoding="utf-8"
            )
            rows = dashboard._execution_rows(root, [{
                "type": "execution", "execution": {"execution_key": "k"},
                "quarantine": "quarantine/task-a/result-2/attempt-1",
            }])
        self.assertEqual(rows[0]["result_version"], 2)
        self.assertEqual(rows[0]["attempt"], 1)
        self.assertEqual(rows[0]["quarantine"], "quarantine/task-a/result-2/attempt-1")


if __name__ == "__main__":
    unittest.main()
