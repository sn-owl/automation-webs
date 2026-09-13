from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path

from automation.inbox import accept_source_observation, ingest_source_observation
from automation.identity import task_id
from automation.models import WorkItem
from automation.events import EventLog
from automation.task_store import get_task, list_task_revisions, put_task


class SourceObservationBoundaryTest(unittest.TestCase):
    def bundle(self, root: Path, payload: dict, marker: str | None = None) -> Path:
        name = payload["observation_id"] + "--source-observation"
        path = root / name
        path.mkdir()
        (path / "source_observation.json").write_text(json.dumps(payload), encoding="utf-8")
        ready = marker if marker is not None else payload["observation_id"] + "\n"
        (path / "_READY").write_text(ready, encoding="utf-8")
        return path

    def payload(self, kind: str) -> dict:
        payload = {
            "schema_version": 1,
            "observation_id": "obs-1",
            "kind": kind,
            "observed_at": "2026-09-12T12:00:00.000Z",
            "source": {"type": "board", "id": "yeonje", "adapter": "gnuboard"},
        }
        if kind == "source_completion_observed":
            payload["external_id"] = "42"
        elif kind == "source_error":
            payload.update(code="http_error", stage="fetch", retryable=True, next_action="retry", last_success_at="2026-09-12T11:00:00.000Z")
        else:
            payload["prior_error_code"] = "http_error"
        return payload

    def test_accepts_all_kinds(self):
        for kind in ("source_completion_observed", "source_error", "source_recovered"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                accepted = accept_source_observation(self.bundle(root, self.payload(kind)), root / "inbox")
                self.assertIsNotNone(accepted)

    def test_rejects_partial_malformed_mismatch_extras_secrets_and_urls(self):
        cases = []
        payload = self.payload("source_error")
        payload["extra"] = "x"
        cases.append(payload)
        payload = self.payload("source_error")
        payload["code"] = "not safe"
        cases.append(payload)
        payload = self.payload("source_error")
        payload["next_action"] = "https://example.invalid"
        cases.append(payload)
        payload = self.payload("source_error")
        payload["secret"] = "nope"
        cases.append(payload)
        for invalid in cases:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self.assertIsNone(accept_source_observation(self.bundle(root, invalid), root / "inbox"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            missing = self.bundle(root, self.payload("source_error"))
            (missing / "_READY").unlink()
            extra_payload = self.payload("source_recovered")
            extra_payload["observation_id"] = "obs-extra"
            extra = self.bundle(root, extra_payload)
            (extra / "extra").write_text("x", encoding="utf-8")
            self.assertIsNone(accept_source_observation(extra, root / "inbox"))

    def test_rejects_malformed_missing_name_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            malformed = root / "bad--source-observation"
            malformed.mkdir()
            (malformed / "source_observation.json").write_text("{", encoding="utf-8")
            (malformed / "_READY").write_text("bad\n", encoding="utf-8")
            self.assertIsNone(accept_source_observation(malformed, root / "inbox"))
            mismatch = self.payload("source_error")
            mismatch["observation_id"] = "other"
            bundle = self.bundle(root, mismatch)
            bundle.rename(root / "wrong--source-observation")
            self.assertIsNone(accept_source_observation(root / "wrong--source-observation", root / "inbox"))
            target = root / "target.json"
            target.write_text(json.dumps(self.payload("source_error")), encoding="utf-8")
            linked = root / "obs-link--source-observation"
            linked.mkdir()
            try:
                os.symlink(target, linked / "source_observation.json")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            (linked / "_READY").write_text("obs-link\n", encoding="utf-8")
            self.assertIsNone(accept_source_observation(linked, root / "inbox"))

    def test_duplicate_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inbox = root / "inbox"
            source = self.bundle(root, self.payload("source_recovered"))
            ready_bytes = (source / "_READY").read_bytes()
            self.assertIsNotNone(accept_source_observation(source, inbox))
            self.assertEqual((inbox / "source-observations" / "obs-1--source-observation" / "_READY").read_bytes(), ready_bytes)
            self.assertFalse(source.exists())
            duplicate = self.bundle(root, self.payload("source_recovered"))
            self.assertIsNotNone(accept_source_observation(duplicate, inbox))
            self.assertFalse(duplicate.exists())
            conflict_payload = self.payload("source_recovered")
            conflict_payload["prior_error_code"] = "parse_error"
            conflict = self.bundle(root, conflict_payload)
            self.assertIsNone(accept_source_observation(conflict, inbox))
            self.assertTrue(conflict.exists())


    def test_ingest_completion_binds_task_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            payload = self.payload("source_completion_observed")
            identifier = task_id("yeonje", "42", scope="alice", connector_id="board")
            task = WorkItem.from_dict({"task_id": identifier, "source": {"type": "board", "id": "yeonje", "external_id": "42", "url": "https://fixture.invalid/task"}, "received_at": "2026-09-12T00:00:00Z", "title": "업무", "body": "본문", "author": "작성자", "attachments": [], "mask_table_ref": "local://masks/x.json", "contract_version": 2, "scope": "alice", "connector_id": "board", "capture_id": "capture", "revision": 1, "completeness": "complete", "provenance": {}, "source_completion_observed": False})
            put_task(task, root=root)
            task_path = root / "state" / "tasks" / f"{identifier}.json"
            before = task_path.read_bytes()
            accepted = accept_source_observation(self.bundle(root, payload), root / "inbox")
            event = ingest_source_observation(accepted, root=root, scope="alice", connector_id="board")
            self.assertEqual(event["stage"], "source")
            self.assertEqual(event["task_version"], 1)
            self.assertEqual(event["content_digest"], task.computed_content_digest())
            self.assertEqual(task_path.read_bytes(), before)
            revisions = list_task_revisions(identifier, root=root)
            self.assertEqual(len(revisions), 1)
            self.assertEqual(revisions[0].revision, 1)
            self.assertEqual(revisions[0].task_id, identifier)
            self.assertFalse(any(item.get("actual_completion") for item in EventLog(root / "state" / "events.jsonl").read()))
            cross_payload = self.payload("source_completion_observed")
            cross_payload["observation_id"] = "obs-cross"
            cross_payload["external_id"] = "99"
            cross_id = task_id("yeonje", "99", scope="bob", connector_id="board")
            cross_task = WorkItem.from_dict({"task_id": cross_id, "source": {"type": "board", "id": "yeonje", "external_id": "99", "url": "https://fixture.invalid/task"}, "received_at": "2026-09-12T00:00:00Z", "title": "업무", "body": "본문", "author": "작성자", "attachments": [], "mask_table_ref": "local://masks/x.json", "contract_version": 2, "scope": "bob", "connector_id": "board", "capture_id": "cross", "revision": 1, "completeness": "complete", "provenance": {}, "source_completion_observed": False})
            put_task(cross_task, root=root)
            cross = accept_source_observation(self.bundle(root, cross_payload), root / "inbox")
            self.assertIsNone(ingest_source_observation(cross, root=root, scope="alice", connector_id="board"))

    def test_ingest_unknown_cross_scope_and_event_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            payload = self.payload("source_completion_observed")
            accepted = accept_source_observation(self.bundle(root, payload), root / "inbox")
            self.assertIsNotNone(accepted)
            self.assertIsNone(ingest_source_observation(accepted, root=root, scope="alice", connector_id="board"))
            error = self.payload("source_error")
            error["observation_id"] = "obs-error"
            error["next_action"] = "다시 시도하세요 (잠시 후)"
            accepted_error = accept_source_observation(self.bundle(root, error), root / "inbox")
            first = ingest_source_observation(accepted_error, root=root, scope="alice", connector_id="board")
            second = ingest_source_observation(accepted_error, root=root, scope="alice", connector_id="board")
            self.assertEqual(first, second)
            self.assertEqual(first["stage"], "source")
            self.assertEqual(first["details"]["error"]["next_action"], "다시 시도하세요 (잠시 후)")
            changed = dict(error)
            changed["code"] = "parse_error"
            accepted_error.write_text(json.dumps(changed), encoding="utf-8")
            self.assertIsNone(ingest_source_observation(accepted_error, root=root, scope="alice", connector_id="board"))

    def test_recovery_event_and_scope_separated_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            recovery = self.payload("source_recovered")
            recovery["observation_id"] = "obs-recovery"
            accepted = accept_source_observation(self.bundle(root, recovery), root / "inbox")
            first = ingest_source_observation(accepted, root=root, scope="alice", connector_id="board")
            second = ingest_source_observation(accepted, root=root, scope="bob", connector_id="board")
            self.assertEqual(first["details"]["recovery"]["prior_error_code"], "http_error")
            self.assertNotEqual(first["event_id"], second["event_id"])
            self.assertEqual(first["stage"], second["stage"])

if __name__ == "__main__":
    unittest.main()

