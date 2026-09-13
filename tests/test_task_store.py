import tempfile
from dataclasses import replace
import unittest
from pathlib import Path

from automation.models import WorkItem
from automation.task_store import (
    CorruptTaskError,
    TaskConflictError,
    TaskNotFoundError,
    get_task,
    list_tasks,
    put_task,
)
from automation.task_store import get_task_revision, list_task_revisions


def make_task(task_id: str, title: str = "업무") -> WorkItem:
    return WorkItem.from_dict(
        {
            "task_id": task_id,
            "source": {
                "type": "board",
                "id": "yeonje",
                "external_id": task_id.rsplit("-", 1)[-1],
                "url": "https://fixture.local/task",
            },
            "received_at": "2026-08-28T09:44:00+09:00",
            "title": title,
            "body": "본문",
            "author": "요청부서A",
            "attachments": [],
            "mask_table_ref": "local://masks/task.json",
        }
    )


class TaskStoreTest(unittest.TestCase):
    def test_changed_v2_preserves_initial_revision_and_scope(self):
        first = replace(make_task("history-1"), contract_version=2, scope="alice", connector_id="board", capture_id="first")
        second = replace(first, title="수정 업무", capture_id="second", revision=2)
        with tempfile.TemporaryDirectory() as directory:
            put_task(first, root=directory)
            put_task(second, root=directory)
            self.assertEqual(get_task_revision(first.task_id, first.computed_revision_digest(), root=directory).title, first.title)
            self.assertEqual({item.revision for item in list_task_revisions(first.task_id, root=directory)}, {1, 2})
            self.assertEqual(get_task(first.task_id, root=directory).title, second.title)
            with self.assertRaises(TaskConflictError):
                put_task(replace(second, scope="bob", title="다른 소유자"), root=directory)
            self.assertEqual(get_task(first.task_id, root=directory).scope, "alice")

    def test_v2_recapture_does_not_create_semantic_revision(self):
        first = replace(make_task("capture-1"), contract_version=2, scope="alice", connector_id="board", capture_id="first")
        with tempfile.TemporaryDirectory() as directory:
            put_task(first, root=directory)
            put_task(replace(first, capture_id="second", received_at="2026-09-09T00:00:00Z"), root=directory)
            self.assertEqual(len(list_task_revisions(first.task_id, root=directory)), 1)
            self.assertEqual(get_task(first.task_id, root=directory).revision, 1)
    def test_completion_observation_updates_metadata_without_revision(self):
        first = replace(
            make_task("completion-1"),
            contract_version=2,
            scope="alice",
            connector_id="board",
            capture_id="first",
        )
        observation = replace(
            first,
            capture_id="completion-capture",
            source_completion_observed=True,
        )

        with tempfile.TemporaryDirectory() as directory:
            put_task(first, root=directory)
            put_task(observation, root=directory)
            stored = get_task(first.task_id, root=directory)

            self.assertTrue(stored.source_completion_observed)
            self.assertEqual(stored.revision, 1)
            self.assertEqual(len(list_task_revisions(first.task_id, root=directory)), 1)
            self.assertEqual(stored.capture_id, "completion-capture")


    def test_same_content_policy_metadata_updates_without_revision(self):
        first = replace(
            make_task("policy-1"),
            contract_version=2,
            scope="alice",
            connector_id="board",
            capture_id="first",
            provenance={"policy_version": "1"},
            completeness="complete",
        )
        updated = replace(
            first,
            capture_id="second",
            received_at="2026-09-09T00:00:00Z",
            provenance={"policy_version": "2"},
            completeness="incomplete",
        )
        with tempfile.TemporaryDirectory() as directory:
            put_task(first, root=directory)
            put_task(updated, root=directory)
            stored = get_task(first.task_id, root=directory)
            self.assertEqual(stored.revision, first.revision)
            self.assertEqual(stored.provenance["policy_version"], "2")
            self.assertEqual(stored.capture_id, "second")
            self.assertEqual(stored.completeness, "complete")

    def test_put_task_persists_and_get_task_reads_work_item(self):
        task = make_task("yeonje-13452")

        with tempfile.TemporaryDirectory() as directory:
            put_task(task, root=directory)

            path = Path(directory) / "state" / "tasks" / "yeonje-13452.json"
            self.assertTrue(path.is_file())
            self.assertEqual(get_task("yeonje-13452", root=directory), task)

    def test_get_missing_task_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TaskNotFoundError):
                get_task("yeonje-13452", root=directory)

    def test_same_task_can_be_put_again_without_changing_bytes(self):
        task = make_task("yeonje-13452")

        with tempfile.TemporaryDirectory() as directory:
            put_task(task, root=directory)
            path = Path(directory) / "state" / "tasks" / "yeonje-13452.json"
            first_bytes = path.read_bytes()

            put_task(task, root=directory)

            self.assertEqual(path.read_bytes(), first_bytes)

    def test_different_content_for_existing_task_is_rejected(self):
        task = make_task("yeonje-13452", title="첫 업무")
        changed = make_task("yeonje-13452", title="변경된 업무")

        with tempfile.TemporaryDirectory() as directory:
            put_task(task, root=directory)

            with self.assertRaises(TaskConflictError):
                put_task(changed, root=directory)

            self.assertEqual(get_task("yeonje-13452", root=directory), task)

    def test_list_tasks_returns_tasks_in_task_id_order(self):
        tasks = [make_task("yeonje-2"), make_task("yeonje-1")]

        with tempfile.TemporaryDirectory() as directory:
            for task in tasks:
                put_task(task, root=directory)

            self.assertEqual(list_tasks(root=directory), [tasks[1], tasks[0]])

    def test_corrupt_json_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory) / "state" / "tasks"
            task_dir.mkdir(parents=True)
            path = task_dir / "yeonje-13452.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(CorruptTaskError):
                get_task("yeonje-13452", root=directory)
            with self.assertRaises(CorruptTaskError):
                list_tasks(root=directory)

    def test_invalid_task_id_cannot_escape_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                put_task(make_task("../outside"), root=directory)
            with self.assertRaises(ValueError):
                get_task("../outside", root=directory)


if __name__ == "__main__":
    unittest.main()
