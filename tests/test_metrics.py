import tempfile
import unittest
from pathlib import Path

from automation.events import EventLog
from automation.models import WorkItem
from automation.task_store import put_task
from scripts.metrics import collect


def make_task(task_id: str, title: str, attachment_type: str | None = None) -> WorkItem:
    attachments = []
    if attachment_type is not None:
        attachments.append(
            {
                "name": f"첨부.{attachment_type}",
                "type": attachment_type,
                "raw_ref": f"raw/{task_id}/첨부.{attachment_type}",
                "extracted_ref": f"normalized/{task_id}.json",
            }
        )
    return WorkItem.from_dict(
        {
            "task_id": task_id,
            "source": {
                "type": "board",
                "id": "alpha",
                "external_id": task_id.rsplit("-", 1)[-1],
                "url": "https://fixture.local/task",
            },
            "received_at": "2026-08-28T09:44:00+09:00",
            "title": title,
            "body": "첨부물 확인 바랍니다.",
            "author": "요청부서A",
            "attachments": attachments,
            "mask_table_ref": f"local://masks/{task_id}.json",
        }
    )


class CollectMetricsTest(unittest.TestCase):
    def test_empty_root_reports_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collect(tmp)["tasks"], 0)
            self.assertEqual(collect(tmp)["human_decided"], 0)

    def test_counts_only_tasks_the_rules_actually_cover(self):
        # The rule config is the thing under measurement: a covered task must
        # reach rule_assessed without Hermes, an uncovered one must not be
        # counted as covered just because it was collected.
        with tempfile.TemporaryDirectory() as tmp:
            put_task(make_task("alpha-1", "무더위쉼터 현황 현행화", "hwpx"), root=tmp)
            put_task(make_task("alpha-2", "이웃작가X상주작가 배너 링크 수정"), root=tmp)

            metrics = collect(tmp)
            self.assertEqual(metrics["tasks"], 2)
            self.assertEqual(metrics["rule_classified"], 1)
            self.assertEqual(metrics["rule_assessed"], 1)

    def test_only_tasks_with_a_recorded_decision_count_as_human_decided(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put_task(make_task("alpha-1", "무더위쉼터 현황 현행화", "hwpx"), root=root)
            put_task(make_task("alpha-2", "이웃작가X상주작가 배너 링크 수정"), root=root)
            log = EventLog(root / "state" / "events.jsonl")
            log.append({"event_id": "e1", "type": "decision", "task_id": "alpha-1"})
            log.append({"event_id": "e2", "stage": "hermes", "task_id": "alpha-2"})

            metrics = collect(root)
            self.assertEqual(metrics["human_decided"], 1)
            self.assertEqual(metrics["hermes_required"], 1)
            self.assertEqual(metrics["executions_succeeded"], 0)

    def test_needs_decision_counts_only_work_parked_in_review(self):
        # The queue the operator faces: auto-completed work and already-decided
        # work are both off it, and only review_required is on it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for task_id in ("t-1", "t-2", "t-3"):
                put_task(make_task(task_id, "무더위쉼터 현황 현행화", "hwpx"), root=root)
            log = EventLog(root / "state" / "events.jsonl")
            log.append({"event_id": "a", "state": "review_required", "task_id": "t-1"})
            log.append({"event_id": "b", "state": "completed", "task_id": "t-2"})
            log.append({"event_id": "c", "state": "review_required", "task_id": "t-3"})
            log.append({"event_id": "d", "type": "decision", "task_id": "t-3"})

            self.assertEqual(collect(root)["needs_decision"], 1)

    def test_events_about_unknown_tasks_are_not_counted(self):
        # An events.jsonl carried over from another run must not inflate the
        # numbers past the tasks actually present.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put_task(make_task("alpha-1", "이웃작가X상주작가 배너 링크 수정"), root=root)
            log = EventLog(root / "state" / "events.jsonl")
            log.append({"event_id": "e1", "stage": "hermes", "task_id": "ghost-9"})
            log.append(
                {
                    "event_id": "e2",
                    "type": "execution",
                    "state": "succeeded",
                    "task_id": "ghost-9",
                }
            )

            metrics = collect(root)
            self.assertEqual(metrics["hermes_required"], 0)
            self.assertEqual(metrics["executions_succeeded"], 0)
            self.assertEqual(metrics["human_decided"], 0)


if __name__ == "__main__":
    unittest.main()
