from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import hashlib
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from automation.adapters.registry import normalize_html
from automation.events import read_events
from automation.models import WorkItem

from automation.assessment import Assessment

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "fixtures" / "sanitized"


class HermesDouble:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[tuple[dict, tuple[str, ...]]] = []

    def classify(self, work_item: dict):
        self.calls.append((work_item, ()))
        return self.response

    def assess(self, work_item: dict, allowed_recipe_ids):
        self.calls.append((work_item, tuple(allowed_recipe_ids)))
        return self.response


class StrictHermesDouble(HermesDouble):
    def classify(self, work_item: dict):
        if set(work_item) != {"request_id", "summary", "policy_context"}:
            raise AssertionError("unexpected Hermes input fields")
        return super().classify(work_item)



class PipelineTest(unittest.TestCase):
    def pipeline(self):
        from run_pipeline import run_pipeline

        return run_pipeline


    def test_changed_v2_recapture_hands_new_revision_to_task_store(self):
        # pipeline-recapture-duplicate: a re-captured v2 task with changed
        # content must not be discarded as a duplicate; the new revision must
        # become the stored current task.
        from automation.task_store import get_task

        run_pipeline = self.pipeline()

        def payload(body):
            return {
                "task_id": "rev-1",
                "source": {"type": "gnuboard", "id": "y", "external_id": "1", "url": "https://x.invalid/1"},
                "received_at": "2026-08-31T00:00:00+00:00", "title": "t", "body": body,
                "author": "PERSON_001", "attachments": [], "mask_table_ref": "local://m.json",
                "contract_version": 2, "scope": "public", "connector_id": "c1",
                "capture_id": "gnuboard-1-abc", "revision": 1, "completeness": "complete",
                "provenance": {}, "source_completion_observed": False,
            }

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "out"
            source = directory / "in.json"
            source.write_text(json.dumps(payload("팝업 등록 요청")), encoding="utf-8")
            first = run_pipeline(source, output_root=output)
            source.write_text(json.dumps(payload("apache 접속로그 조사")), encoding="utf-8")
            result = run_pipeline(source, output_root=output)
            stored = get_task("rev-1", root=output)

        self.assertEqual(result["status"], "review_required")
        self.assertEqual(stored.body, "apache 접속로그 조사")
        self.assertEqual(stored.revision, 2)
        self.assertEqual(first["classification"]["responsibility"], "design")
        self.assertEqual(result["classification"]["responsibility"], "development")
        classified = [event for event in result["events"] if event.get("state") == "classified"]
        self.assertEqual({event["task_version"] for event in classified}, {1, 2})

    def test_source_completion_observation_does_not_create_revision(self):
        from automation.task_store import get_task, list_task_revisions

        run_pipeline = self.pipeline()

        def payload(capture_id, observed):
            return {
                "task_id": "completion-1",
                "source": {"type": "gnuboard", "id": "y", "external_id": "1", "url": "https://x.invalid/1"},
                "received_at": "2026-08-31T00:00:00+00:00",
                "title": "t",
                "body": "apache 접속로그 조사",
                "author": "PERSON_001",
                "attachments": [],
                "mask_table_ref": "local://m.json",
                "contract_version": 2,
                "scope": "public",
                "connector_id": "c1",
                "capture_id": capture_id,
                "revision": 1,
                "completeness": "complete",
                "provenance": {},
                "source_completion_observed": observed,
            }

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "out"
            source = directory / "in.json"
            source.write_text(json.dumps(payload("capture-1", False)), encoding="utf-8")
            run_pipeline(source, output_root=output)
            source.write_text(json.dumps(payload("completion-capture", True)), encoding="utf-8")
            result = run_pipeline(source, output_root=output)
            stored = get_task("completion-1", root=output)
            revision_count = len(list_task_revisions("completion-1", root=output))
            events = read_events(output / "state/events.jsonl")

        self.assertEqual(result["status"], "review_required")
        self.assertTrue(stored.source_completion_observed)
        self.assertEqual(stored.revision, 1)
        self.assertEqual(revision_count, 1)
        self.assertNotIn("revised", [event["state"] for event in events])
        self.assertTrue(
            any(
                event["state"] == "source_completion_observed"
                and event["task_id"] == "completion-1"
                for event in events
            )
        )

    def test_failed_completeness_is_blocked_at_intake(self):
        # A02: a source that reported completeness=failed is recorded for audit
        # but must not be classified, assessed, or rendered as actionable.
        run_pipeline = self.pipeline()
        item = {
            "task_id": "gate-1",
            "source": {"type": "gnuboard", "id": "y", "external_id": "1", "url": "https://x.invalid/1"},
            "received_at": "2026-08-31T00:00:00+00:00", "title": "t", "body": "b",
            "author": "PERSON_001", "attachments": [], "mask_table_ref": "local://m.json",
            "contract_version": 2, "scope": "public", "connector_id": "c1",
            "capture_id": "gnuboard-1-abc", "revision": 1, "completeness": "failed",
            "provenance": {}, "source_completion_observed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "in.json"
            source.write_text(json.dumps(item), encoding="utf-8")
            result = run_pipeline(source, output_root=directory / "out")
        self.assertEqual(result["status"], "blocked")
        states = [event.get("state") for event in result["events"]]
        self.assertNotIn("classified", states)
        self.assertNotIn("assessed", states)


    def test_shipped_evaluation_recipe_is_registered(self):
        # Regression guard: a ready verdict must name a recipe the registry
        # actually holds, otherwise every ready task blocks at routing.
        import json as json_module

        from automation.recipes import load_recipes

        config = json_module.loads(
            (Path(__file__).parents[1] / "config" / "evaluation.json").read_text(encoding="utf-8")
        )
        registered = set(load_recipes())
        for rule in config["rules"]:
            recipe = rule["then"].get("recipe")
            if recipe is not None:
                with self.subTest(rule=rule["id"]):
                    self.assertIn(recipe, registered)

    def test_ready_html_is_classified_without_implicit_assessment(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            result = run_pipeline(
                FIXTURES / "alpha-ready.html", output_root=Path(directory)
            )
            self.assertEqual(result["status"], "review_required")
            self.assertEqual(result["task_id"], "alpha-13452")
            self.assertEqual(result["route"], "classification_only")
            self.assertIsNone(result["assessment"])
            self.assertIsNotNone(result["classification"])
            events = read_events(Path(directory) / "state/events.jsonl")
            self.assertEqual(events[-1]["state"], "review_required")
    # -- F1: collection supplies the scoped contract -------------------

    def collect(self, directory, **connection):
        run_pipeline = self.pipeline()
        result = run_pipeline(
            FIXTURES / "alpha-ready.html", output_root=Path(directory), **connection
        )
        self.assertEqual(result["status"], "review_required")
        from automation.task_store import get_task

        return result, get_task(result["task_id"], root=Path(directory))

    def test_collection_without_a_connection_stays_on_the_legacy_identity(self):
        # Naming a connection changes the task id (identity.task_id), so an
        # unconfigured runtime must keep collecting exactly as before.
        with tempfile.TemporaryDirectory() as directory:
            result, task = self.collect(directory)

            self.assertEqual(result["task_id"], "alpha-13452")
            self.assertEqual(task.contract_version, 1)
            self.assertNotIn("scope", task.to_dict())

    def test_a_named_connection_collects_a_contract_version_2_work_item(self):
        with tempfile.TemporaryDirectory() as directory:
            result, task = self.collect(
                directory, scope="owner-a", connector_id="chrome-extension"
            )

            self.assertEqual(task.contract_version, 2)
            self.assertEqual(task.scope, "owner-a")
            self.assertEqual(task.connector_id, "chrome-extension")
            self.assertEqual(task.revision, 1)
            self.assertEqual(task.completeness, "complete")
            self.assertEqual(task.provenance["adapter"], "gnuboard")
            self.assertIs(task.source_completion_observed, False)
            stored = task.to_dict()
            for field in (
                "scope",
                "connector_id",
                "capture_id",
                "revision",
                "completeness",
                "provenance",
                "source_completion_observed",
            ):
                self.assertIn(field, stored)

    def test_the_same_external_item_in_two_scopes_does_not_collide(self):
        with tempfile.TemporaryDirectory() as directory:
            mine, _ = self.collect(
                directory, scope="owner-a", connector_id="chrome-extension"
            )
            theirs, _ = self.collect(
                directory, scope="owner-b", connector_id="chrome-extension"
            )

            self.assertNotEqual(mine["task_id"], theirs["task_id"])

    def test_minimal_connector_work_item_reaches_core_store_and_dashboard(self):
        from automation.dashboard_read import build_dashboard_read_model
        from automation.events import EventLog
        from automation.task_store import get_task

        payload = {
            "task_id": "minimal-connector-1",
            "source": {
                "type": "future-connector",
                "id": "board-a",
                "external_id": "request-1",
            },
            "received_at": "2026-09-10T00:00:00Z",
            "title": "새 개발 요청",
            "body": "apache 접속로그 조사",
            "mask_table_ref": "local://masks/minimal-connector-1.json",
            "contract_version": 2,
            "scope": "owner-a",
            "connector_id": "connector-a",
            "capture_id": "capture-minimal-1",
            "revision": 1,
            "completeness": "complete",
            "provenance": {"adapter": "future-connector"},
            "source_completion_observed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "work_item.json"
            source.write_text(json.dumps(payload), encoding="utf-8")

            result = self.pipeline()(source, output_root=root / "runtime")
            stored = get_task("minimal-connector-1", root=root / "runtime")
            model = build_dashboard_read_model(
                [stored],
                EventLog(root / "runtime" / "state" / "events.jsonl"),
            )

        self.assertEqual(result["status"], "review_required")
        self.assertIsNone(stored.author)
        self.assertIsNone(stored.source.url)
        self.assertEqual(stored.attachments, ())
        detail = model["tasks"][0]
        self.assertEqual(detail["scope"], "owner-a")
        self.assertEqual(detail["version"], 1)
        self.assertEqual(detail["state"], "review_required")

    def test_a_half_named_connection_is_refused(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            for connection in ({"scope": "owner-a"}, {"connector_id": "chrome-extension"}):
                with self.subTest(connection=connection):
                    result = run_pipeline(
                        FIXTURES / "alpha-ready.html",
                        output_root=Path(directory),
                        **connection,
                    )
                    self.assertEqual(result["status"], "failed")

    def test_a_collected_v2_item_is_reachable_through_its_scope(self):
        # What F1 is for: scoped storage only narrows real work if collection
        # actually stamps a scope.
        from automation.task_store import list_tasks

        with tempfile.TemporaryDirectory() as directory:
            self.collect(directory, scope="owner-a", connector_id="chrome-extension")

            root = Path(directory)
            mine = [t for t in list_tasks(root=root) if t.scope == "owner-a"]
            theirs = [t for t in list_tasks(root=root) if t.scope == "owner-b"]
            self.assertEqual(len(mine), 1)
            self.assertEqual(theirs, [])

    def test_json_collection_applies_connection_and_rejects_other_owner(self):
        from run_pipeline import _read_input

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, legacy = self.collect(root / "legacy")
            incoming = root / "work_item.json"
            incoming.write_text(json.dumps(legacy.to_dict()), encoding="utf-8")
            owner_a, _ = _read_input(incoming, scope="owner-a", connector_id="extension")
            owner_b, _ = _read_input(incoming, scope="owner-b", connector_id="extension")
            self.assertNotEqual(owner_a.task_id, owner_b.task_id)
            self.assertEqual(owner_a.scope, "owner-a")
            self.assertEqual(owner_a.contract_version, 2)
            incoming.write_text(json.dumps(owner_b.to_dict()), encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_input(incoming, scope="owner-a", connector_id="extension")
            accepted, _ = _read_input(incoming, scope="owner-b", connector_id="extension")
            self.assertEqual(accepted.task_id, owner_b.task_id)

    def test_exact_input_bytes_are_stored_immutably_with_hash_metadata(self):
        run_pipeline = self.pipeline()
        input_bytes = (FIXTURES / "alpha-ready.html").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_pipeline(
                FIXTURES / "alpha-ready.html", output_root=root
            )
            raw_path = root / "raw/alpha-13452/page.html"
            self.assertEqual(raw_path.read_bytes(), input_bytes)
            self.assertEqual(
                result["raw"]["sha256"], hashlib.sha256(input_bytes).hexdigest()
            )
            self.assertEqual(result["raw"]["size"], len(input_bytes))
            event = next(
                event for event in read_events(root / "state/events.jsonl")
                if event["state"] == "stored"
            )
            self.assertEqual(event["raw"]["sha256"], result["raw"]["sha256"])
    def test_unsafe_task_id_is_rejected_before_raw_storage(self):
        run_pipeline = self.pipeline()
        item = normalize_html(
            "gnuboard",
            (FIXTURES / "alpha-ready.html").read_text(encoding="utf-8"),
            {
                "board_id": "alpha",
                "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
            },
        )
        payload = item.to_dict()
        payload["task_id"] = "../outside"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "item.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            result = run_pipeline(input_path, output_root=root / "out")
            self.assertEqual(result["state"], "storage_failed")
            self.assertFalse((root / "out" / "outside").exists())

    def test_bundle_relative_attachment_is_anchored_before_task_storage(self):
        from automation.task_store import get_task
        from run_pipeline import run_pipeline

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = normalize_html(
                "gnuboard",
                (FIXTURES / "alpha-ready.html").read_text(encoding="utf-8"),
                {
                    "board_id": "alpha",
                    "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
                },
            )
            payload = item.to_dict()
            payload["attachments"][0]["raw_ref"] = "attachments/request.hwpx"
            payload["attachments"][0]["extracted_ref"] = "attachments/request.hwpx"
            input_path = root / "bundle" / "work_item.json"
            input_path.parent.joinpath("attachments").mkdir(parents=True)
            input_path.parent.joinpath("attachments/request.hwpx").write_bytes(b"hwpx")
            input_path.write_text(json.dumps(payload), encoding="utf-8")

            result = run_pipeline(input_path, output_root=root / "out")

            self.assertEqual(result["status"], "review_required")
            self.assertEqual(result["route"], "classification_only")
            self.assertIsNone(result["input_check"])
            stored = get_task("alpha-13452", root=root / "out")
            self.assertEqual(
                stored.attachments[0].raw_ref,
                str((input_path.parent / "attachments/request.hwpx").resolve()),
            )
            self.assertTrue(Path(stored.attachments[0].raw_ref).is_file())

    def test_bundle_relative_attachment_cannot_escape_input_directory(self):
        from run_pipeline import run_pipeline

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = normalize_html(
                "gnuboard",
                (FIXTURES / "alpha-ready.html").read_text(encoding="utf-8"),
                {
                    "board_id": "alpha",
                    "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
                },
            )
            payload = item.to_dict()
            payload["attachments"][0]["raw_ref"] = "../outside.hwpx"
            input_path = root / "bundle" / "work_item.json"
            input_path.parent.mkdir(parents=True)
            input_path.write_text(json.dumps(payload), encoding="utf-8")

            result = run_pipeline(input_path, output_root=root / "out")

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["state"], "normalize_failed")
            self.assertFalse((root / "out" / "state" / "tasks").exists())


    def test_concurrent_same_input_runs_are_event_idempotent(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        run_pipeline,
                        FIXTURES / "egov-developable.html",
                        output_root=root,
                    )
                    for _ in range(2)
                ]
                results = [future.result() for future in futures]
            self.assertEqual(results[0], results[1])
            events = read_events(root / "state/events.jsonl")
            event_ids = [event["event_id"] for event in events]
            self.assertEqual(len(event_ids), len(set(event_ids)))

    def test_transient_hermes_classification_failure_is_retryable(self):
        run_pipeline = self.pipeline()
        response = json.dumps(
            {
                "classification": {
                    "responsibility": "development",
                    "task_type": "feature request",
                    "size": "small",
                    "ownership": "정보 부족",
                    "primary_role": "development",
                    "responsibility_scope": "요청 범위 확인",
                    "collaboration": [],
                    "risk_flags": [],
                    "confidence": 0.85,
                    "evidence": ["fixture evidence"],
                    "next_action": "담당과 범위를 확인하세요.",
                    "pattern_match": {},
                    "unmatched_aspects": ["담당 소유권"],
                }
            }
        )

        class RetryClient(HermesDouble):
            def __init__(self):
                super().__init__(response)
                self.failed = False

            def classify(self, work_item):
                if not self.failed:
                    self.failed = True
                    raise OSError("temporary transport failure")
                return super().classify(work_item)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RetryClient()
            first = run_pipeline(
                FIXTURES / "egov-developable.html",
                output_root=root,
                hermes_client=client,
            )
            second = run_pipeline(
                FIXTURES / "egov-developable.html",
                output_root=root,
                hermes_client=client,
            )
            self.assertEqual(first["status"], "blocked")
            self.assertEqual(second["status"], "review_required")
            self.assertEqual(len(client.calls), 1)

    def test_corrupt_event_log_returns_safe_corrupt_state(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root / "state/events.jsonl"
            event_path.parent.mkdir(parents=True)
            event_path.write_text("{not-json\n", encoding="utf-8")
            result = run_pipeline(
                FIXTURES / "egov-developable.html", output_root=root
            )
            self.assertEqual(result["state"], "corrupt_event_log")
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("not-json", json.dumps(result))



    def test_hermes_rejection_is_idempotent_and_not_retried(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rejecting = HermesDouble('{"assessment":{"command":"unsafe"}}')
            first = run_pipeline(
                FIXTURES / "egov-developable.html",
                output_root=root,
                hermes_client=rejecting,
            )
            succeeding = HermesDouble("{}")
            second = run_pipeline(
                FIXTURES / "egov-developable.html",
                output_root=root,
                hermes_client=succeeding,
            )
            self.assertEqual(first, second)
            self.assertEqual(len(rejecting.calls), 1)
            self.assertEqual(len(succeeding.calls), 0)



    def test_deterministic_events_include_safe_classification_metadata(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            result = run_pipeline(
                FIXTURES / "alpha-ready.html", output_root=Path(directory)
            )
            events = read_events(Path(directory) / "state/events.jsonl")
            classified = next(event for event in events if event["state"] == "classified")
            self.assertEqual(classified["classification"], result["classification"])
            self.assertFalse(any(event["state"] == "assessed" for event in events))

    def test_classification_event_failure_returns_safe_result(self):
        import run_pipeline as pipeline_module

        original = pipeline_module._record_event

        def fail_classification(root, event_id, state, **kwargs):
            if state == "classified":
                raise OSError("private classification failure")
            return original(root, event_id, state, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pipeline_module, "_record_event", side_effect=fail_classification):
                result = pipeline_module.run_pipeline(
                    FIXTURES / "alpha-ready.html", output_root=Path(directory)
                )
            self.assertEqual(result["state"], "classification_failed")
            self.assertIsNone(result["classification"])
            self.assertNotIn("private classification failure", json.dumps(result))

    def test_canonical_work_item_json_is_supported_and_repeat_is_idempotent(self):
        run_pipeline = self.pipeline()
        item = normalize_html(
            "gnuboard",
            (FIXTURES / "alpha-ready.html").read_text(encoding="utf-8"),
            {
                "board_id": "alpha",
                "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "item.json"
            input_path.write_text(json.dumps(item.to_dict()), encoding="utf-8")
            first = run_pipeline(input_path, output_root=root / "out")
            event_bytes = (root / "out/state/events.jsonl").read_bytes()
            package_bytes = {
                p.relative_to(root / "out"): p.read_bytes()
                for p in (root / "out/alpha-13452").rglob("*")
                if p.is_file()
            }
            second = run_pipeline(input_path, output_root=root / "out")
            self.assertEqual(first, second)
            self.assertEqual(event_bytes, (root / "out/state/events.jsonl").read_bytes())
            self.assertEqual(
                package_bytes,
                {
                    p.relative_to(root / "out"): p.read_bytes()
                    for p in (root / "out/alpha-13452").rglob("*")
                    if p.is_file()
                },
            )


    def test_recapture_with_same_task_id_is_stored_as_duplicate(self):
        run_pipeline = self.pipeline()
        item = normalize_html(
            "gnuboard",
            (FIXTURES / "alpha-ready.html").read_text(encoding="utf-8"),
            {
                "board_id": "alpha",
                "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
            },
        )
        changed = item.to_dict()
        changed["title"] = "같은 업무의 최신 제목"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.json"
            second_path = root / "second.json"
            first_path.write_text(json.dumps(item.to_dict()), encoding="utf-8")
            second_path.write_text(json.dumps(changed), encoding="utf-8")
            run_pipeline(first_path, output_root=root / "out")
            result = run_pipeline(second_path, output_root=root / "out")
            events = read_events(root / "out/state/events.jsonl")

        self.assertEqual(result["status"], "duplicate")
        self.assertTrue(any(event["state"] == "duplicate" for event in events))
        self.assertFalse(any(event["state"] == "storage_failed" for event in events))
    def test_unresolved_html_is_blocked_without_hermes(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            result = run_pipeline(
                FIXTURES / "egov-developable.html", output_root=Path(directory)
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["route"], "needs_hermes")
            self.assertIsNone(result["work_package"])
            self.assertEqual(read_events(Path(directory) / "state/events.jsonl")[-1]["state"], "blocked")




    def test_developable_html_is_blocked_until_advisory_assessment(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            result = run_pipeline(
                FIXTURES / "egov-developable.html", output_root=Path(directory)
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["task_id"], "egov-9001")
            self.assertEqual(result["route"], "needs_hermes")


    def test_hermes_rejection_is_recorded_without_raw_response(self):
        run_pipeline = self.pipeline()
        hermes = HermesDouble('{"assessment":{"command":"rm -rf /"}}')
        with tempfile.TemporaryDirectory() as directory:
            result = run_pipeline(
                FIXTURES / "egov-developable.html",
                output_root=Path(directory),
                hermes_client=hermes,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["route"], "needs_hermes")
            self.assertNotIn("rm -rf", json.dumps(result))
            self.assertEqual(read_events(Path(directory) / "state/events.jsonl")[-1]["state"], "blocked")
    def test_structurally_valid_secret_evidence_is_not_returned_when_event_persistence_rejects_it(self):
        run_pipeline = self.pipeline()
        response = {
            "classification": {
                "responsibility": "design",
                "task_type": "image_popup",
                "size": "simple",
                "confidence": 0.85,
                "evidence": ["password=super-secret"],
            },
            "assessment": {
                "automation_level": "manual",
                "risk": "remote_write",
                "confidence": 0.85,
                "evidence": ["password=super-secret"],
            },
        }
        hermes = HermesDouble(json.dumps(response))
        with tempfile.TemporaryDirectory() as directory:
            result = run_pipeline(
                FIXTURES / "egov-developable.html",
                output_root=Path(directory),
                hermes_client=hermes,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIsNone(result["classification"])
            self.assertIsNone(result["assessment"])
            self.assertNotIn("super-secret", json.dumps(result))



    def test_normalization_and_storage_failures_are_safe(self):
        run_pipeline = self.pipeline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_html = root / "bad.html"
            bad_html.write_text("<html><body>not a post</body></html>", encoding="utf-8")
            result = run_pipeline(bad_html, output_root=root / "out")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["state"], "normalize_failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("run_pipeline.put_task", side_effect=OSError("secret")):
                result = run_pipeline(FIXTURES / "alpha-ready.html", output_root=root)
            self.assertEqual(result["state"], "storage_failed")
            self.assertNotIn("secret", json.dumps(result))


    def test_cli_main_emits_json_and_success_exit_code(self):
        from run_pipeline import main

        with tempfile.TemporaryDirectory() as directory:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        str(FIXTURES / "alpha-ready.html"),
                        "--output-root",
                        directory,
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["task_id"], "alpha-13452")
            self.assertEqual(payload["status"], "review_required")

    def test_ordinary_unmatched_work_requests_classification_only(self):
        run_pipeline = self.pipeline()

        class Client:
            classify_calls = 0
            assess_calls = 0

            def classify(self, projection):
                self.classify_calls += 1
                self.projection = projection
                return json.dumps(
                    {
                        "classification": {
                            "responsibility": "새 업무 영역",
                            "task_type": "새 요청 유형",
                            "size": "범위 미정",
                            "ownership": "정보 부족",
                            "primary_role": "업무 담당",
                            "responsibility_scope": "요청 범위 확인",
                            "collaboration": [],
                            "risk_flags": [],
                            "confidence": 0.45,
                            "evidence": ["요약에 새 요청이 있음"],
                            "next_action": "담당과 범위를 확인하세요.",
                            "pattern_match": {},
                            "unmatched_aspects": ["담당 소유권"],
                        }
                    }
                )

            def assess(self, *_args):
                self.assess_calls += 1
                raise AssertionError("ordinary classification must not assess automation")

        item = {
            "task_id": "new-domain-1",
            "source": {
                "type": "gnuboard",
                "id": "board-a",
                "external_id": "1",
                "url": "https://fixture.invalid/1",
            },
            "received_at": "2026-09-09T00:00:00Z",
            "title": "새 업무 요청",
            "body": "기존 규칙에 없는 업무입니다.",
            "attachments": [],
            "mask_table_ref": "local://mask.json",
            "contract_version": 2,
            "scope": "user-a",
            "connector_id": "connector-a",
            "capture_id": "capture-a",
            "revision": 1,
            "completeness": "complete",
            "provenance": {},
            "source_completion_observed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "item.json"
            source.write_text(json.dumps(item), encoding="utf-8")
            client = Client()
            result = run_pipeline(
                source, output_root=root / "runtime", hermes_client=client
            )

        self.assertEqual(client.classify_calls, 1)
        self.assertEqual(client.assess_calls, 0)
        self.assertEqual(result["route"], "classification_only")
        self.assertIsNone(result["assessment"])
        self.assertEqual(result["classification"]["responsibility"], "새 업무 영역")
        self.assertNotIn("source", client.projection)
        self.assertNotIn("attachments", client.projection)

    def test_explicit_onboarding_can_request_separate_assessment(self):
        run_pipeline = self.pipeline()

        class Client:
            def __init__(self):
                self.operations = []

            def classify(self, _projection):
                self.operations.append("classification")
                return json.dumps(
                    {
                        "classification": {
                            "responsibility": "새 업무 영역",
                            "task_type": "새 요청 유형",
                            "size": "범위 미정",
                            "ownership": "내 업무",
                            "primary_role": "업무 담당",
                            "responsibility_scope": "자료 검토",
                            "collaboration": [],
                            "risk_flags": [],
                            "confidence": 0.8,
                            "evidence": ["요청자가 담당 기준을 제공함"],
                            "next_action": "자동화 가능성을 검토하세요.",
                            "pattern_match": {},
                            "unmatched_aspects": [],
                        }
                    }
                )

            def assess(self, _projection, allowed):
                self.operations.append(("assessment", tuple(allowed)))
                return json.dumps(
                    {
                        "assessment": {
                            "automation_level": "manual",
                            "risk": "read_only",
                            "confidence": 0.8,
                            "evidence": ["등록된 처리 능력이 없음"],
                        }
                    }
                )

        item = {
            "task_id": "onboarding-1",
            "source": {
                "type": "gnuboard",
                "id": "board-a",
                "external_id": "2",
                "url": "https://fixture.invalid/2",
            },
            "received_at": "2026-09-09T00:00:00Z",
            "title": "새 업무 자동화 검토",
            "body": "기존 규칙에 없는 업무입니다.",
            "attachments": [],
            "mask_table_ref": "local://mask.json",
            "contract_version": 2,
            "scope": "user-a",
            "connector_id": "connector-a",
            "capture_id": "capture-b",
            "revision": 1,
            "completeness": "complete",
            "provenance": {"assessment_requested": True},
            "source_completion_observed": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "item.json"
            source.write_text(json.dumps(item), encoding="utf-8")
            client = Client()
            result = run_pipeline(
                source, output_root=root / "runtime", hermes_client=client
            )

        self.assertEqual(client.operations, ["classification", ("assessment", ())])
        self.assertEqual(result["assessment"]["automation_level"], "manual")
        self.assertEqual(result["route"], "manual_handoff")

    def test_ai_classification_failures_are_distinct_user_review_gates(self):
        run_pipeline = self.pipeline()

        class TransportFailure:
            def classify(self, _projection):
                raise OSError("private transport detail")

        class InvalidResponse:
            def classify(self, _projection):
                return '{"classification":{"unexpected":"invalid"}}'

        cases = (
            ("ai_disabled", None),
            ("ai_call_failed", TransportFailure()),
            ("ai_response_invalid", InvalidResponse()),
        )
        for reason_code, client in cases:
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as directory:
                result = run_pipeline(
                    FIXTURES / "egov-developable.html",
                    output_root=Path(directory),
                    hermes_client=client,
                )
                review = result["classification_review"]
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["route"], "needs_hermes")
                self.assertIsNone(result["classification"])
                self.assertEqual(review["decision"], "DO NOT")
                self.assertTrue(review["review_required"])
                self.assertEqual(review["reason_code"], reason_code)
                self.assertEqual(review["next_action"], "user_review")
                self.assertIn("사용자 검토", review["reason"])
                event = read_events(Path(directory) / "state/events.jsonl")[-1]
                self.assertEqual(event["classification_review"], review)
                self.assertNotIn("private transport detail", json.dumps(result))

    def test_explicit_assessment_failures_have_distinct_review_reasons(self):
        run_pipeline = self.pipeline()
        item = normalize_html(
            "gnuboard",
            (FIXTURES / "alpha-ready.html").read_text(encoding="utf-8"),
            {
                "board_id": "alpha",
                "url": "https://fixture.local/bbs/board.php?bo_table=alpha&wr_id=13452",
            },
        )
        payload = item.to_dict()
        payload.update(
            {
                "contract_version": 2,
                "scope": "user-a",
                "connector_id": "connector-a",
                "capture_id": "assessment-failure",
                "revision": 1,
                "completeness": "complete",
                "provenance": {"assessment_requested": True},
                "source_completion_observed": False,
            }
        )

        class TransportFailure:
            def assess(self, _projection, _allowed):
                raise OSError("private transport detail")

        class InvalidResponse:
            def assess(self, _projection, _allowed):
                return '{"assessment":{"unexpected":"invalid"}}'

        cases = (
            ("ai_disabled", None),
            ("ai_call_failed", TransportFailure()),
            ("ai_response_invalid", InvalidResponse()),
        )
        for reason_code, client in cases:
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "item.json"
                source.write_text(json.dumps(payload), encoding="utf-8")
                with patch("run_pipeline.evaluate_with_rules", return_value=None):
                    result = run_pipeline(
                        source,
                        output_root=Path(directory) / "runtime",
                        hermes_client=client,
                    )
                review = result["assessment_review"]
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["route"], "needs_assessment" if client is None else "needs_hermes")
                self.assertEqual(review["reason_code"], reason_code)
                self.assertEqual(review["next_action"], "user_review")
                self.assertNotIn("private transport detail", json.dumps(result))

if __name__ == "__main__":
    unittest.main()
