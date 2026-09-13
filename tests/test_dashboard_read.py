import json
import tempfile
import unittest
from pathlib import Path


class RecordingTaskStore:
    def __init__(self, tasks):
        self.tasks = tasks
        self.reads = 0
        self.writes = 0

    def list_tasks(self):
        self.reads += 1
        return list(self.tasks)

    def put_task(self, _task):
        self.writes += 1
        raise AssertionError("dashboard projection must not write tasks")


class RecordingEventLog:
    def __init__(self, events):
        self.events = events
        self.reads = 0
        self.writes = 0

    def read(self):
        self.reads += 1
        return list(self.events)

    def append(self, _event):
        self.writes += 1
        raise AssertionError("dashboard projection must not append events")


class DashboardReadModelTest(unittest.TestCase):
    def build(self, task_store, event_log):
        from automation.dashboard_read import build_dashboard_read_model

        return build_dashboard_read_model(task_store, event_log)

    def test_projects_sorted_tasks_status_counts_and_safe_detail(self):
        tasks = RecordingTaskStore(
            [
                {
                    "task_id": "task-b",
                    "title": "B summary",
                    "body": "private body token=do-not-leak",
                    "cookies": "session-cookie",
                    "credentials": "password",
                },
                {
                    "task_id": "task-a",
                    "title": "A summary",
                    "body": "another private body",
                },
            ]
        )
        events = RecordingEventLog(
            [
                {
                    "event_id": "state-b",
                    "type": "state",
                    "task_id": "task-b",
                    "state": "blocked",
                    "source_completion_observed": True,
                    "task_version": 2,
                    "classification": {
                        "responsibility": "operations",
                        "task_type": "data_refresh",
                        "size": "recurring",
                        "confidence": 0.8,
                        "evidence": ["rule:refresh"],
                    },
                    "assessment": {
                        "automation_level": "manual",
                        "risk": "read_only",
                        "confidence": 0.7,
                        "evidence": ["needs review"],
                    },
                    "decision": {
                        "actor": "alice",
                        "task_id": "task-b",
                        "task_version": 2,
                        "action": "defer",
                        "reason": "waiting for input",
                        "timestamp": "2026-09-02T00:00:00Z",
                        "details": {"body": "must not be shown"},
                    },
                    "artifacts": [
                        {
                            "name": "result.xls",
                            "type": "xls",
                            "path": "artifacts/result.xls",
                            "sha256": "abc123",
                            "status": "succeeded",
                            "raw_ref": "/private/source.hwpx",
                        }
                    ],
                    "source": {"body": "unrestricted source"},
                },
                {
                    "event_id": "state-a",
                    "type": "state",
                    "task_id": "task-a",
                    "status": "ready",
                    "version": 1,
                },
            ]
        )

        result = self.build(tasks, events)

        self.assertEqual([item["task_id"] for item in result["tasks"]], ["task-a", "task-b"])
        self.assertEqual(result["status_counts"], {"blocked": 1, "ready": 1})
        detail = result["tasks"][1]
        self.assertEqual(detail["state"], "blocked")
        self.assertTrue(detail["source_completion_observed"])
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["classification"]["responsibility"], "operations")
        self.assertEqual(detail["assessment"]["automation_level"], "manual")
        self.assertEqual(detail["decision"]["action"], "defer")
        self.assertEqual(detail["artifacts"], [{"name": "result.xls", "type": "xls", "path": "artifacts/result.xls", "sha256": "abc123", "status": "succeeded"}])

        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for forbidden in ("private body", "do-not-leak", "session-cookie", "password", "unrestricted source", "raw_ref", "must not be shown"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(tasks.writes, 0)
        self.assertEqual(events.writes, 0)

    def test_missing_and_empty_stores_return_empty_projection(self):
        self.assertEqual(self.build(RecordingTaskStore([]), RecordingEventLog([])), {"tasks": [], "status_counts": {}})
        self.assertEqual(self.build(RecordingTaskStore([]), RecordingEventLog([])), {"tasks": [], "status_counts": {}})

    def test_repeated_reads_are_deterministic_and_read_only(self):
        tasks = RecordingTaskStore([{"task_id": "same", "title": "summary", "body": "secret"}])
        events = RecordingEventLog([{"event_id": "same-state", "task_id": "same", "type": "state", "state": "new", "task_version": 1}])
        first = self.build(tasks, events)
        second = self.build(tasks, events)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(tasks.writes, 0)
        self.assertEqual(events.writes, 0)

    def test_sensitive_title_is_not_exposed_as_summary(self):
        tasks = RecordingTaskStore(
            [{"task_id": "sensitive", "title": "Authorization: Bearer top-secret-value"}]
        )

        result = self.build(tasks, RecordingEventLog([]))

        self.assertNotIn("summary", result["tasks"][0])
        self.assertNotIn("top-secret-value", json.dumps(result, sort_keys=True))


    def test_non_bearer_authorization_title_is_not_exposed(self):
        tasks = RecordingTaskStore(
            [{"task_id": "digest", "title": "Authorization: Digest dcd98b..."}]
        )

        result = self.build(tasks, RecordingEventLog([]))

        self.assertNotIn("summary", result["tasks"][0])
        self.assertNotIn("dcd98b", json.dumps(result, sort_keys=True))

    def test_unrestricted_evidence_is_not_projected(self):
        tasks = RecordingTaskStore([{"task_id": "evidence", "title": "Safe summary"}])
        events = RecordingEventLog(
            [
                {
                    "event_id": "evidence-state",
                    "task_id": "evidence",
                    "classification": {
                        "responsibility": "operations",
                        "task_type": "data_refresh",
                        "size": "recurring",
                        "confidence": 0.8,
                        "evidence": ["raw source body that must not be shown"],
                    },
                    "assessment": {
                        "automation_level": "manual",
                        "risk": "read_only",
                        "confidence": 0.7,
                        "evidence": ["unrestricted source content"],
                    },
                }
            ]
        )

        result = self.build(tasks, events)

        self.assertNotIn("evidence", result["tasks"][0]["classification"])
        self.assertNotIn("evidence", result["tasks"][0]["assessment"])
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn("raw source body", encoded)
        self.assertNotIn("unrestricted source", encoded)

    def test_standalone_token_title_is_not_exposed(self):
        tasks = RecordingTaskStore([{"task_id": "token", "title": "token=ABC123"}])

        result = self.build(tasks, RecordingEventLog([]))

        self.assertNotIn("summary", result["tasks"][0])
        self.assertNotIn("ABC123", json.dumps(result, sort_keys=True))

    def test_sensitive_decision_reason_is_not_exposed(self):
        tasks = RecordingTaskStore([{"task_id": "decision", "title": "Safe summary"}])
        events = RecordingEventLog(
            [
                {
                    "event_id": "decision-event",
                    "task_id": "decision",
                    "decision": {
                        "actor": "alice",
                        "task_id": "decision",
                        "task_version": 1,
                        "action": "defer",
                        "reason": "token=ABC123",
                        "timestamp": "2026-09-02T00:00:00Z",
                    },
                }
            ]
        )

        result = self.build(tasks, events)

        self.assertNotIn("reason", result["tasks"][0]["decision"])
        self.assertNotIn("ABC123", json.dumps(result, sort_keys=True))
    def test_real_event_log_decision_projection_excludes_free_text(self):
        from automation.events import EventLog
        from automation.dashboard_read import build_dashboard_read_model

        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory) / "events.jsonl")
            log.append(
                {
                    "event_id": "unsafe-decision",
                    "task_id": "decision",
                    "decision": {
                        "actor": "raw actor content",
                        "task_id": "decision",
                        "task_version": 1,
                        "action": "raw action content",
                        "reason": "raw source body with private details",
                        "timestamp": "raw timestamp content",
                    },
                }
            )
            result = build_dashboard_read_model(
                RecordingTaskStore([{"task_id": "decision", "title": "Safe summary"}]),
                log,
            )

        decision = result["tasks"][0]["decision"]
        self.assertEqual(
            decision,
            {"task_id": "decision", "task_version": 1, "has_reason": True},
        )
        encoded = json.dumps(result, sort_keys=True)
        for forbidden in ("raw actor content", "raw action content", "raw source body", "raw timestamp content"):
            self.assertNotIn(forbidden, encoded)

    def test_decision_timestamp_requires_timezone_aware_canonical_datetime(self):
        invalid = (
            "2026-09-02",
            "2026-09-02T00:00:00",
            "2026-09-02 00:00:00+00:00",
            "2026-W36-3T00:00:00+00:00",
        )
        for timestamp in invalid:
            with self.subTest(timestamp=timestamp):
                result = self.build(
                    RecordingTaskStore([{"task_id": "timestamp", "title": "Safe summary"}]),
                    RecordingEventLog(
                        [
                            {
                                "event_id": "decision-" + timestamp,
                                "task_id": "timestamp",
                                "decision": {
                                    "actor": "alice",
                                    "task_id": "timestamp",
                                    "task_version": 1,
                                    "action": "defer",
                                    "reason": "safe reason",
                                    "timestamp": timestamp,
                                },
                            }
                        ]
                    ),
                )
                self.assertNotIn("timestamp", result["tasks"][0]["decision"])

        for timestamp in ("2026-09-02T00:00:00Z", "2026-09-02T09:00:00+09:00"):
            with self.subTest(timestamp=timestamp):
                result = self.build(
                    RecordingTaskStore([{"task_id": "timestamp", "title": "Safe summary"}]),
                    RecordingEventLog(
                        [
                            {
                                "event_id": "valid-" + timestamp,
                                "task_id": "timestamp",
                                "decision": {
                                    "actor": "alice",
                                    "task_id": "timestamp",
                                    "task_version": 1,
                                    "action": "defer",
                                    "reason": "safe reason",
                                    "timestamp": timestamp,
                                },
                            }
                        ]
                    ),
                )
                self.assertEqual(result["tasks"][0]["decision"]["timestamp"], timestamp)

    def test_api_separator_whitespace_is_filtered_in_summaries_and_artifacts(self):
        tasks = RecordingTaskStore(
            [{"task_id": "api-secret", "title": "api\tkey=ABC123"}]
        )
        events = RecordingEventLog(
            [
                {
                    "event_id": "artifact-secret",
                    "task_id": "api-secret",
                    "artifacts": [
                        {
                            "name": "private  key=XYZ789",
                            "type": "xls",
                            "path": "artifacts/result.xls",
                            "sha256": "safe-digest",
                        }
                    ],
                }
            ]
        )

        result = self.build(tasks, events)

        self.assertNotIn("summary", result["tasks"][0])
        self.assertEqual(result["tasks"][0]["artifacts"], [{"type": "xls", "path": "artifacts/result.xls", "sha256": "safe-digest"}])
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn("ABC123", encoded)
        self.assertNotIn("XYZ789", encoded)

    def test_extensible_classification_labels_and_ai_review_survive_projection(self):
        tasks = RecordingTaskStore(
            [{"task_id": "extensible", "title": "새 업무"}]
        )
        events = RecordingEventLog(
            [
                {
                    "event_id": "classification",
                    "task_id": "extensible",
                    "state": "blocked",
                    "stage": "hermes",
                    "classification": {
                        "responsibility": "public-safety coordination",
                        "task_type": "seasonal facility reconciliation",
                        "size": "focused",
                        "confidence": 0.4,
                        "evidence": ["자료 범위가 확인됨"],
                    },
                    "classification_review": {
                        "decision": "DO NOT",
                        "review_required": True,
                        "reason_code": "ai_call_failed",
                        "reason": "AI 호출에 실패했습니다. 사용자 검토가 필요합니다.",
                        "next_action": "user_review",
                    },
                }
            ]
        )

        result = self.build(tasks, events)
        detail = result["tasks"][0]

        self.assertEqual(
            detail["classification"]["responsibility"],
            "public-safety coordination",
        )
        self.assertEqual(
            detail["classification"]["task_type"],
            "seasonal facility reconciliation",
        )
        self.assertEqual(detail["classification"]["size"], "focused")
        self.assertEqual(detail["classification_review"]["decision"], "DO NOT")
        self.assertTrue(detail["classification_review"]["review_required"])
        self.assertEqual(
            detail["classification_review"]["reason_code"], "ai_call_failed"
        )
        self.assertEqual(tasks.writes, 0)
        self.assertEqual(events.writes, 0)
    def test_completion_source_and_notification_are_separate_safe_projections(self):
        class DeliveryStore:
            def read_deliveries(self):
                return [
                    {
                        "task_id": "task-1",
                        "event": "classification_review",
                        "channel": "local",
                        "status": "failed",
                        "attempt": 1,
                        "retryable": False,
                        "local_reference": "state/tasks/task-1.json",
                        "recorded_at": "2026-09-10T12:00:00+00:00",
                        "message": "secret body must not project",
                    },
                    {
                        "task_id": "task-1",
                        "event": "classification_review",
                        "channel": "dashboard",
                        "status": "delivered",
                        "attempt": 1,
                        "retryable": False,
                        "local_reference": "state/tasks/task-1.json",
                        "recorded_at": "2026-09-10T12:01:00+00:00",
                    },
                ]

        events = [
            {
                "event_id": "source-observed",
                "task_id": "task-1",
                "state": "source_completion_observed",
                "source_completion_observed": True,
            },
            {
                "event_id": "review",
                "task_id": "task-1",
                "state": "review_required",
                "stage": "render",
                "task_version": 1,
            },
            {
                "event_id": "actual",
                "task_id": "task-1",
                "state": "actual_completed",
                "stage": "completion",
                "task_version": 1,
                "actual_completion": {
                    "task_id": "task-1",
                    "task_version": 1,
                    "execution_key": "execution-1",
                    "actor": "operator",
                    "reason": "secret reason must not project",
                    "reason_recorded": True,
                    "timestamp": "2026-09-10T12:02:00+00:00",
                },
            },
        ]
        model = self.build(
            RecordingTaskStore([{"task_id": "task-1", "title": "Safe task"}]),
            RecordingEventLog(events),
        )
        # Direct projections remain usable without a notification store.
        self.assertNotIn("notification_delivery", model["tasks"][0])
        from automation.dashboard_read import build_dashboard_read_model

        model = build_dashboard_read_model(
            RecordingTaskStore([{"task_id": "task-1", "title": "Safe task"}]),
            RecordingEventLog(events),
            DeliveryStore(),
        )
        detail = model["tasks"][0]
        self.assertEqual(detail["state"], "actual_completed")
        self.assertEqual(detail["preparation_state"], "review_required")
        self.assertTrue(detail["source_completion_observed"])
        self.assertEqual(detail["actual_completion"]["task_version"], 1)
        self.assertEqual(detail["notification_delivery"][-1]["status"], "delivered")
        encoded = json.dumps(model, ensure_ascii=False)
        self.assertNotIn("secret body", encoded)
        self.assertNotIn("secret reason", encoded)

    def test_task_version_is_projected_from_canonical_event_reference(self):
        tasks = RecordingTaskStore([{"task_id": "task-a", "title": "safe", "revision": 2}])
        events = RecordingEventLog(
            [
                {
                    "event_id": "task-a-classified-v2",
                    "task_id": "task-a",
                    "state": "classified",
                    "task_version": 2,
                }
            ]
        )

        detail = self.build(tasks, events)["tasks"][0]

        self.assertEqual(detail["version"], 2)

    def test_newest_execution_version_and_quarantine_projection(self):
        events = RecordingEventLog([
            {"type": "execution", "task_id": "task-a", "state": "failed",
             "execution": {"state": "failed", "result_version": 2, "attempt": 1},
             "quarantine": "quarantine/task-a/result-2/attempt-1"},
            {"type": "execution", "task_id": "task-a", "state": "executing",
             "execution": {"state": "executing", "result_version": 2, "attempt": 2}},
            {"type": "execution", "task_id": "task-a", "state": "succeeded",
             "execution": {"state": "succeeded", "result_version": 1, "attempt": 1},
             "artifacts": [{"path": "artifacts/task-a/result-1/attempt-1/result.xls"}]},
        ])
        model = self.build(RecordingTaskStore([{"task_id": "task-a", "title": "safe"}]), events)
        detail = model["tasks"][0]
        self.assertEqual(detail["result_version"], 2)
        self.assertEqual(
            detail["quarantine_history"],
            ["quarantine/task-a/result-2/attempt-1"],
        )
        self.assertEqual(detail["attempt"], 2)
        self.assertEqual(detail["execution_state"], "executing")
        self.assertNotIn("quarantine", detail)
if __name__ == "__main__":
    unittest.main()
