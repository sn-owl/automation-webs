from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.hermes_client import HermesClient, HermesClientError


REPO_ROOT = Path(__file__).parents[1]
WORK_ITEM = {
    "task_id": "task-36",
    "source": {
        "type": "board",
        "id": "board-1",
        "external_id": "post-7",
        "url": "http://fixture.local/source/post-7",
    },
    "received_at": "2026-09-02T12:00:00+00:00",
    "title": "Repair a converter",
    "body": "The sanitized body is safe to send.",
    "author": "masked-author",
    "attachments": [
        {
            "name": "request.hwpx",
            "type": "hwpx",
            "raw_ref": "attachments/request.hwpx",
            "extracted_ref": "extracted/request",
        }
    ],
    "mask_table_ref": "masks/task-36.json",
}
MINIMIZED_WORK_ITEM = {
    "request_id": "a" * 64,
    "summary": "Repair a converter",
    "policy_context": {"roles": ["development"]},
}


class HermesClientTest(unittest.TestCase):
    def _client(self, *, timeout: float = 7.0) -> HermesClient:
        return HermesClient(
            command=("hermes",), repository_root=REPO_ROOT, timeout=timeout
        )

    def test_classification_and_assessment_send_distinct_operations(self):
        observed = []

        def run(command, **_kwargs):
            query_path = Path(command[command.index("--query-file") + 1])
            payload = json.loads(
                query_path.read_text(encoding="utf-8").split("Input JSON:\n", 1)[1]
            )
            observed.append(payload)
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

        with patch("automation.hermes_client.subprocess.run", side_effect=run):
            self._client().classify(MINIMIZED_WORK_ITEM)
            self._client().assess(MINIMIZED_WORK_ITEM, ("recipe-a",))

        self.assertEqual(observed[0]["operation"], "classification")
        self.assertEqual(observed[0]["allowed_recipe_ids"], [])
        self.assertEqual(observed[1]["operation"], "assessment")
        self.assertEqual(observed[1]["allowed_recipe_ids"], ["recipe-a"])

    def test_raw_work_item_with_unknown_fields_is_rejected_before_transport(self):
        with patch("automation.hermes_client.subprocess.run") as transport:
            with self.assertRaises(HermesClientError):
                self._client().classify(
                    dict(WORK_ITEM, raw_body="SECRET-raw", credentials="SECRET-token")
                )
        transport.assert_not_called()

    def test_projection_cannot_smuggle_extra_fields(self):
        with self.assertRaises(HermesClientError):
            self._client()._sanitize_work_item(
                dict(MINIMIZED_WORK_ITEM, attachments=[])
            )

    def test_same_content_has_same_projection_across_source_and_owner_names(self):
        from copy import deepcopy

        first = deepcopy(WORK_ITEM)
        second = deepcopy(first)
        second["source"]["id"] = "different-source"
        second["author"] = "another-person"
        left = self._client()._sanitize_work_item(first)
        right = self._client()._sanitize_work_item(second)
        self.assertEqual(left, right)

    def test_assessment_allow_list_is_strict(self):
        for value in ("recipe", ("",), ("same", "same"), (1,)):
            with self.subTest(value=value):
                with self.assertRaises(HermesClientError):
                    self._client().assess(MINIMIZED_WORK_ITEM, value)

    def test_transport_failures_are_safe(self):
        cases = (
            (
                subprocess.CompletedProcess(
                    ["hermes"], 2, stdout="", stderr="SECRET-cli-error"
                ),
                None,
                "Hermes CLI returned a failure",
            ),
            (None, subprocess.TimeoutExpired(["hermes"], 7), "Hermes CLI timed out"),
            (None, FileNotFoundError(), "Hermes CLI was not found"),
        )
        for completed, failure, expected in cases:
            with self.subTest(expected=expected):
                effect = failure if failure is not None else None
                with patch(
                    "automation.hermes_client.subprocess.run",
                    return_value=completed,
                    side_effect=effect,
                ):
                    with self.assertRaises(HermesClientError) as caught:
                        self._client().classify(WORK_ITEM)
                self.assertEqual(str(caught.exception), expected)
                self.assertNotIn("SECRET-cli-error", str(caught.exception))

    def test_empty_stdout_is_rejected(self):
        with patch(
            "automation.hermes_client.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["hermes"], 0, stdout="", stderr=""
            ),
        ):
            with self.assertRaisesRegex(HermesClientError, "no proposal"):
                self._client().classify(WORK_ITEM)


if __name__ == "__main__":
    unittest.main()
