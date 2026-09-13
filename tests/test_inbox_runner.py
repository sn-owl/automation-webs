from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.events import EventLog
from automation.identity import task_id
from automation.models import WorkItem
from automation.task_store import get_task, list_task_revisions, put_task
from tests.test_source_observations import SourceObservationBoundaryTest
from tests.test_inbox import InboxContractTest
from inbox_runner import process_inbox


class InboxRunnerTest(unittest.TestCase):
    def test_accepts_ready_bundle_and_runs_pipeline_from_bundle_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = InboxContractTest(
                "test_accepts_complete_bundle_atomically_as_work_item_input"
            )._bundle(root / "downloads")
            seen: dict[str, object] = {}
            client = object()

            def fake_pipeline(input_path, *, output_root, hermes_client, scope, connector_id):
                seen["input_path"] = Path(input_path)
                seen["output_root"] = Path(output_root)
                seen["hermes_client"] = hermes_client
                seen["scope"] = scope
                seen["connector_id"] = connector_id
                self.assertTrue(Path(input_path).parent.joinpath("attachments", "request.hwpx").is_file())
                return {"status": "review_required", "task_id": "alpha-13452"}

            results = process_inbox(
                root / "downloads",
                root / "inbox",
                root / "state",
                hermes_client=client,
                pipeline=fake_pipeline,
            )

            self.assertEqual(results, [{"status": "review_required", "task_id": "alpha-13452"}])
            self.assertEqual(seen["input_path"].name, "work_item.json")
            self.assertEqual(seen["output_root"], root / "state")
            self.assertIs(seen["hermes_client"], client)
            self.assertIsNone(seen["scope"])
            self.assertIsNone(seen["connector_id"])
            self.assertFalse(download.exists())
            self.assertTrue((root / "inbox" / "alpha-13452-ee983842" / "work_item.json").is_file())

    def test_a_configured_connection_reaches_every_collected_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            InboxContractTest(
                "test_accepts_complete_bundle_atomically_as_work_item_input"
            )._bundle(root / "downloads")
            seen: dict[str, object] = {}

            def fake_pipeline(input_path, *, output_root, hermes_client, scope, connector_id):
                seen["scope"] = scope
                seen["connector_id"] = connector_id
                return {"status": "review_required"}

            process_inbox(
                root / "downloads",
                root / "inbox",
                root / "state",
                pipeline=fake_pipeline,
                scope="owner-a",
                connector_id="chrome-extension",
            )

            self.assertEqual(seen["scope"], "owner-a")
            self.assertEqual(seen["connector_id"], "chrome-extension")

    def test_accepts_title_suffixed_bundle_and_keeps_canonical_capture_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = InboxContractTest(
                "test_accepts_complete_bundle_atomically_as_work_item_input"
            )._bundle(root / "downloads")
            suffixed = download.with_name(f"{download.name}--무더위쉼터-현황-갱신")
            download.rename(suffixed)

            results = process_inbox(
                root / "downloads",
                root / "inbox",
                root / "state",
                pipeline=lambda *args, **kwargs: {"status": "review_required"},
            )

            self.assertEqual(results, [{"status": "review_required"}])
            self.assertTrue((root / "inbox" / download.name / "work_item.json").is_file())
            self.assertFalse(suffixed.exists())

    def test_real_pipeline_anchors_bundle_attachment_for_approved_execution(self):
        import taskctl
        from automation.task_store import get_task
        from tests.test_execute_command import install_active_recipe

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = InboxContractTest(
                "test_accepts_complete_bundle_atomically_as_work_item_input"
            )._bundle(root / "downloads")

            results = process_inbox(
                root / "downloads",
                root / "inbox",
                root / "state",
            )

            self.assertEqual(results[0]["status"], "blocked")
            task_id = results[0]["task_id"]
            item = get_task(task_id, root=root / "state")
            source = Path(item.attachments[0].raw_ref)
            self.assertTrue(source.is_file())
            self.assertEqual(source, (root / "inbox" / download.name / "attachments" / "request.hwpx").resolve())
            install_active_recipe(root / "state")

            selection = {"state": "satisfied", "evidence": [], "reasons": []}
            with patch("automation.execution_service.select_recipe", return_value=selection):
                self.assertEqual(
                    taskctl.main(
                        [
                            "approve",
                            task_id,
                            "--actor",
                            "test",
                            "--reason",
                            "integration",
                            "--recipe",
                            "restarea-hwpx-to-xls",
                            "--root",
                            str(root / "state"),
                        ],
                    ),
                    0,
                )

            def fake_convert(source_path, output_path, template_path):
                output_path.write_bytes(b"fake xls")
            with patch("automation.executors.restarea._converter.convert", fake_convert), patch(
                "automation.execution_service.verify_recipe_output",
                return_value={
                    "verification_version": "restarea-xls-v1",
                    "status": "passed",
                    "checks": [],
                    "handoff": {"required": True, "status": "pending"},
                },
            ), patch(
                "automation.execution_service.validate_recipe_input",
                return_value={"state": "satisfied", "recipe_id": "restarea-hwpx-to-xls"},
            ), patch(
                "automation.execution_service.select_recipe",
                return_value=selection,
            ):
                self.assertEqual(
                    taskctl.main(
                        [
                            "execute",
                            task_id,
                            "--recipe",
                            "restarea-hwpx-to-xls",
                            "--root",
                            str(root / "state"),
                        ]
                    ),
                    0,
                )

            execution_files = list((root / "state" / "state" / "executions").glob("*.json"))
            self.assertEqual(len(execution_files), 1)
            self.assertEqual(
                json.loads(execution_files[0].read_text(encoding="utf-8"))["state"],
                "succeeded",
            )

    def test_ignores_incomplete_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            download = InboxContractTest(
                "test_does_not_consume_partial_bundle_without_marker"
            )._bundle(root / "downloads")
            (download / "_READY").unlink()

            results = process_inbox(
                root / "downloads",
                root / "inbox",
                root / "state",
                pipeline=lambda *args, **kwargs: self.fail("incomplete bundle was executed"),
            )

            self.assertEqual(results, [])
            self.assertTrue(download.exists())

    def test_source_error_and_recovery_are_recorded_in_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            builder = SourceObservationBoundaryTest()
            downloads = root / "downloads"
            downloads.mkdir()
            error = builder.payload("source_error")
            recovery = builder.payload("source_recovered")
            recovery["observation_id"] = "obs-recovery"
            builder.bundle(downloads, error)
            builder.bundle(downloads, recovery)
            results = process_inbox(downloads, root / "inbox", root / "state", scope="alice", connector_id="board")
            self.assertEqual([result["status"] for result in results], ["source_observed", "source_observed"])
            self.assertEqual(results[0]["diagnosis"]["code"], error["code"])
            self.assertEqual(results[0]["diagnosis"]["stage"], error["stage"])
            self.assertEqual(results[0]["diagnosis"]["retryable"], error["retryable"])
            self.assertEqual(results[0]["diagnosis"]["next_action"], error["next_action"])
            self.assertEqual(results[0]["source"], error["source"])

            events = EventLog(root / "state" / "state" / "events.jsonl").read()
            self.assertEqual([event["state"] for event in events], ["source_error", "source_recovered"])

    def test_completion_observation_binds_task_without_mutating_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "downloads").mkdir()
            identifier = task_id("alpha", "42", scope="alice", connector_id="board")
            task = WorkItem.from_dict({"task_id": identifier, "source": {"type": "board", "id": "alpha", "external_id": "42", "url": "https://fixture.invalid/task"}, "received_at": "2026-09-12T00:00:00Z", "title": "업무", "body": "본문", "author": "작성자", "attachments": [], "mask_table_ref": "local://masks/x.json", "contract_version": 2, "scope": "alice", "connector_id": "board", "capture_id": "capture", "revision": 1, "completeness": "complete", "provenance": {}, "source_completion_observed": False})
            output_root = root / "state"
            put_task(task, root=output_root)
            task_path = output_root / "state" / "tasks" / f"{identifier}.json"
            before = task_path.read_bytes()
            payload = SourceObservationBoundaryTest().payload("source_completion_observed")
            SourceObservationBoundaryTest().bundle(root / "downloads", payload)
            results = process_inbox(root / "downloads", root / "inbox", output_root, scope="alice", connector_id="board")
            self.assertEqual(results[0]["status"], "source_observed")
            self.assertEqual(results[0]["task_id"], identifier)
            self.assertEqual(task_path.read_bytes(), before)
            self.assertEqual(len(list_task_revisions(identifier, root=output_root)), 1)
            self.assertFalse(any(event.get("actual_completion") for event in EventLog(output_root / "state" / "events.jsonl").read()))

    def test_invalid_observation_does_not_block_normal_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            downloads = root / "downloads"
            downloads.mkdir()
            invalid = SourceObservationBoundaryTest().payload("source_error")
            invalid["code"] = "not safe"
            invalid["observation_id"] = "aaa-observation"
            SourceObservationBoundaryTest().bundle(downloads, invalid)
            InboxContractTest("test_accepts_complete_bundle_atomically_as_work_item_input")._bundle(downloads)
            results = process_inbox(downloads, root / "inbox", root / "state", scope="alice", connector_id="board", pipeline=lambda *args, **kwargs: {"status": "normal"})
            self.assertEqual([result["status"] for result in results], ["failed", "normal"])
            self.assertEqual(results[0]["stage"], "source")
            self.assertIn("reason", results[0])

    def test_pipeline_failure_isolated_from_later_source_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            downloads = root / "downloads"
            InboxContractTest("test_accepts_complete_bundle_atomically_as_work_item_input")._bundle(downloads)
            observation = SourceObservationBoundaryTest().payload("source_error")
            observation["observation_id"] = "zzz-observation"
            SourceObservationBoundaryTest().bundle(downloads, observation)
            calls = 0

            def pipeline(*args, **kwargs):
                nonlocal calls
                calls += 1
                raise RuntimeError("private detail")

            results = process_inbox(
                downloads,
                root / "inbox",
                root / "state",
                scope="alice",
                connector_id="board",
                pipeline=pipeline,
            )
            self.assertEqual([result["status"] for result in results], ["failed", "source_observed"])
            self.assertEqual(results[0]["stage"], "pipeline")
            self.assertIn("reason", results[0])
            self.assertIn("next_action", results[0])
            self.assertEqual(calls, 1)

    def test_valid_observation_coexists_without_running_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            downloads = root / "downloads"
            downloads.mkdir()
            observation = SourceObservationBoundaryTest().payload("source_error")
            observation["observation_id"] = "aaa-observation"
            SourceObservationBoundaryTest().bundle(downloads, observation)
            InboxContractTest("test_accepts_complete_bundle_atomically_as_work_item_input")._bundle(downloads)
            calls: list[Path] = []

            def fake_pipeline(input_path, **kwargs):
                calls.append(Path(input_path))
                return {"status": "normal"}

            results = process_inbox(downloads, root / "inbox", root / "state", scope="alice", connector_id="board", pipeline=fake_pipeline)
            self.assertEqual([result["status"] for result in results], ["source_observed", "normal"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].name, "work_item.json")

    def test_missing_observation_configuration_leaves_download_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            downloads = root / "downloads"
            downloads.mkdir()
            observation = SourceObservationBoundaryTest().bundle(downloads, SourceObservationBoundaryTest().payload("source_error"))
            results = process_inbox(downloads, root / "inbox", root / "state")
            self.assertEqual(results, [{"status": "blocked", "state": "source_observation_blocked", "error": "source observation requires configured scope and connector"}])
            self.assertTrue(observation.exists())


if __name__ == "__main__":
    unittest.main()
