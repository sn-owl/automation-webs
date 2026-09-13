from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from automation.events import EventLog
from automation.identity import task_id
from automation.models import WorkItem
from automation.task_store import list_task_revisions, put_task
from inbox_runner import process_inbox


class SourceObservationFlowTest(unittest.TestCase):
    def test_javascript_observations_run_through_real_inbox_and_preserve_task_authority(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        source_url = "https://example.invalid/board.php?bo_table=yeonje&wr_id=42"
        observations = [
            {
                "observationId": "obs-1-error-20260912t120000000z",
                "kind": "source_error",
                "observedAt": "2026-09-12T12:00:00.000Z",
                "sourceId": "yeonje",
                "sourceUrl": source_url,
                "code": "http_error",
                "stage": "fetch",
                "retryable": True,
                "lastSuccessAt": "2026-09-12T11:00:00.000Z",
                "nextAction": "retry",
            },
            {
                "observationId": "obs-2-recovery-20260912t120100000z",
                "kind": "source_recovered",
                "observedAt": "2026-09-12T12:01:00.000Z",
                "sourceId": "yeonje",
                "sourceUrl": source_url,
                "priorErrorCode": "http_error",
            },
            {
                "observationId": "obs-3-completion-20260912t120200000z",
                "kind": "source_completion_observed",
                "observedAt": "2026-09-12T12:02:00.000Z",
                "sourceId": "yeonje",
                "sourceUrl": source_url,
                "externalId": "42",
            },
        ]
        node_program = """
import { buildSourceObservation } from './extension/archiver.js';
const observations = JSON.parse(process.argv[1]);
const encoded = observations.map((observation) => {
  const payload = buildSourceObservation(observation);
  return Buffer.from(JSON.stringify(payload) + "\\n", "utf8").toString("base64");
});
process.stdout.write(JSON.stringify(encoded));
"""
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", node_program, json.dumps(observations)],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            self.fail(f"Node producer failed ({completed.returncode}): {completed.stderr}")
        try:
            serialized_payloads = [base64.b64decode(value) for value in json.loads(completed.stdout)]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.fail(f"Node producer returned invalid serialized payloads: {exc}")

        expected_keys = {
            "source_error": {"schema_version", "observation_id", "kind", "observed_at", "source", "code", "stage", "retryable", "last_success_at", "next_action"},
            "source_recovered": {"schema_version", "observation_id", "kind", "observed_at", "source", "prior_error_code"},
            "source_completion_observed": {"schema_version", "observation_id", "kind", "observed_at", "source", "external_id"},
        }
        payloads = []
        for serialized in serialized_payloads:
            payload = json.loads(serialized.decode("utf-8"))
            self.assertEqual(set(payload), expected_keys[payload["kind"]])
            encoded = serialized.decode("utf-8").casefold()
            for forbidden in ("sourceurl", "raw", "password", "secret", "token", "cookie", "authorization", "https://", "http://"):
                self.assertNotIn(forbidden, encoded)
            payloads.append((payload, serialized))
        self.assertEqual(
            [payload["kind"] for payload, _ in payloads],
            ["source_error", "source_recovered", "source_completion_observed"],
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            downloads = root / "Downloads"
            inbox = root / "Inbox"
            output_root = root / "Core"
            downloads.mkdir()
            scope = "owner-a"
            connector_id = "chrome-extension"
            identifier = task_id("yeonje", "42", scope=scope, connector_id=connector_id)
            task = WorkItem.from_dict(
                {
                    "task_id": identifier,
                    "source": {"type": "board", "id": "yeonje", "external_id": "42", "url": "local://fixture/task"},
                    "received_at": "2026-09-12T00:00:00Z",
                    "title": "업무",
                    "body": "본문",
                    "author": "작성자",
                    "attachments": [],
                    "mask_table_ref": "local://masks/task.json",
                    "contract_version": 2,
                    "scope": scope,
                    "connector_id": connector_id,
                    "capture_id": "capture-1",
                    "revision": 1,
                    "completeness": "complete",
                    "provenance": {"source": "fixture"},
                    "source_completion_observed": False,
                }
            )
            put_task(task, root=output_root)
            task_path = output_root / "state" / "tasks" / f"{identifier}.json"
            before_bytes = task_path.read_bytes()
            before_revisions = [item.to_dict() for item in list_task_revisions(identifier, root=output_root)]

            for payload, serialized in payloads:
                bundle = downloads / f"{payload['observation_id']}--source-observation"
                bundle.mkdir()
                (bundle / "source_observation.json").write_bytes(serialized)
                (bundle / "_READY").write_bytes((payload["observation_id"] + "\n").encode("utf-8"))

            results = process_inbox(
                downloads,
                inbox,
                output_root,
                scope=scope,
                connector_id=connector_id,
            )
            self.assertEqual([result["status"] for result in results], ["source_observed"] * 3)
            self.assertEqual([result["kind"] for result in results], [payload["kind"] for payload, _ in payloads])

            events = EventLog(output_root / "state" / "events.jsonl").read()
            self.assertEqual([event["state"] for event in events], [payload["kind"] for payload, _ in payloads])
            for event in events:
                self.assertEqual(event["scope"], scope)
                self.assertEqual(event["connector_id"], connector_id)
            completion = events[-1]
            self.assertEqual(completion["task_id"], identifier)
            self.assertEqual(completion["task_version"], task.revision)
            self.assertEqual(completion["content_digest"], task.computed_content_digest())
            self.assertFalse(any(event.get("actual_completion") for event in events))
            self.assertEqual(task_path.read_bytes(), before_bytes)
            self.assertEqual([item.to_dict() for item in list_task_revisions(identifier, root=output_root)], before_revisions)
            self.assertFalse(any(downloads.iterdir()))
            for payload, _ in payloads:
                accepted = inbox / "source-observations" / f"{payload['observation_id']}--source-observation"
                self.assertTrue((accepted / "source_observation.json").is_file())
                self.assertTrue((accepted / "_READY").is_file())


if __name__ == "__main__":
    unittest.main()
