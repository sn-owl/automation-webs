import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import taskctl
from automation.events import EventLog
from automation.models import WorkItem
from automation.notification_store import NotificationProfile, NotificationStore
from automation.task_store import put_task
from run_pipeline import _record_event


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = taskctl.main(argv)
    return code, out.getvalue(), err.getvalue()


def _seed(root: Path, *, state: str) -> None:
    put_task(
        WorkItem.from_dict(
            {
                "task_id": "alpha-1",
                "source": {"type": "gnuboard", "id": "alpha", "external_id": "1", "url": "https://x.invalid/1"},
                "received_at": "2026-01-01T00:00:00Z", "title": "현행화 요청", "body": "본문",
                "mask_table_ref": "local://m.json", "attachments": [],
            }
        ),
        root=root,
    )
    log = EventLog(root / "state" / "events.jsonl")
    log.append({"event_id": "alpha-1:classified", "task_id": "alpha-1", "stage": "classification",
                "state": "classified",
                "classification": {"responsibility": "operations", "task_type": "data_refresh", "size": "recurring",
                                   "confidence": 0.9, "evidence": ["제목 일치"], "next_action": "사용자 검토"}})
    log.append({"event_id": "alpha-1:assessed", "task_id": "alpha-1", "stage": "evaluation", "state": "assessed",
                "assessment": {"automation_level": "ready", "risk": "local_artifact_only", "confidence": 0.9,
                               "evidence": ["근거"], "recipe": "restarea-hwpx-to-xls"}})
    log.append({"event_id": f"alpha-1:{state}", "task_id": "alpha-1", "stage": "render", "state": state})


class NotifyCommandTest(unittest.TestCase):
    def test_review_required_task_is_queued_once_and_deduped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root, state="review_required")
            code, _, _ = run(["notify", "alpha-1", "--root", str(root)])
            self.assertEqual(code, 0)
            deliveries = NotificationStore(root).read_deliveries()
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0]["status"], "queued")
            # Re-running must not double-deliver.
            run(["notify", "alpha-1", "--root", str(root)])
            self.assertEqual(len(NotificationStore(root).read_deliveries()), 1)
    def test_pipeline_events_create_immediate_notification_records_without_state_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root, state="review_required")
            store = NotificationStore(root)
            store.save_profile(NotificationProfile(profile_id="default"))
            _record_event(root, "pipeline-stored", "stored", task_id="alpha-1", stage="storage")
            _record_event(root, "pipeline-review", "review_required", task_id="alpha-1", stage="render")
            deliveries = store.read_deliveries()
            code, output, error = run(["notify", "alpha-1", "--root", str(root)])
            state_events = EventLog(root / "state" / "events.jsonl").read()

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "queued")
        self.assertEqual({record["event"] for record in deliveries}, {"new_task", "classification_review"})
        self.assertTrue(all(record["event_payload"]["event_id"] for record in deliveries))
        self.assertTrue(all(record["local_reference"] == "state/tasks/alpha-1.json" for record in deliveries))
        self.assertEqual(state_events[-1]["state"], "review_required")

    def test_task_not_awaiting_a_decision_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _seed(root, state="completed")
            code, _, err = run(["notify", "alpha-1", "--root", str(root)])
            self.assertNotEqual(code, 0)
            self.assertIn("not awaiting a decision", err)
            self.assertEqual(NotificationStore(root).read_deliveries(), [])


if __name__ == "__main__":
    unittest.main()
