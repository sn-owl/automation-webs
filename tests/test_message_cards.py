from __future__ import annotations

import dataclasses
import unittest
from unittest.mock import patch

from automation.message_cards import TaskMessageCard, render_task_message


BASE = {
    "task_id": "yeonje-13452",
    "task_version": 3,
    "proposal": "Prepare the reviewed artifact",
    "assessment": "Ready for a human decision",
    "evidence": ("classification:development", "assessment:local_artifact_only"),
    "risk": "local_artifact_only",
    "impact": "Creates a local artifact only",
}


class TaskMessageCardTest(unittest.TestCase):
    def test_card_is_frozen_and_contains_required_decision_context(self):
        card = TaskMessageCard(**BASE)

        self.assertTrue(dataclasses.is_dataclass(card))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            card.task_id = "other-task"
        self.assertEqual(card.task_id, "yeonje-13452")
        self.assertEqual(card.task_version, 3)
        self.assertEqual(card.evidence, BASE["evidence"])
        self.assertEqual(card.available_actions, ("approve", "reject", "modify", "defer"))

    def test_required_fields_reject_blank_or_invalid_values(self):
        for field, value in (
            ("task_id", "  "),
            ("proposal", ""),
            ("assessment", "  "),
            ("risk", ""),
            ("impact", "  "),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, field: value})

        for version in (0, -1, True, "3", None):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, "task_version": version})

        for evidence in ((), [], ("",), ("  ",), "evidence"):
            with self.subTest(evidence=evidence):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, "evidence": evidence})

    def test_missing_proposal_and_assessment_is_missing_decision_context(self):
        with self.assertRaises(ValueError):
            TaskMessageCard(**{**BASE, "proposal": "", "assessment": ""})

    def test_task_id_is_safe_identifier_for_rendered_command(self):
        for task_id in ("task/42", "task\\42", "task 42", "task\n42", ".", ".."):
            with self.subTest(task_id=task_id):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, "task_id": task_id})

    def test_renderer_emits_exactly_four_task_commands(self):
        rendered = render_task_message(TaskMessageCard(**BASE))

        self.assertEqual(
            [line for line in rendered.splitlines() if line.startswith("/task ")],
            [
                "/task approve yeonje-13452",
                "/task reject yeonje-13452",
                "/task modify yeonje-13452",
                "/task defer yeonje-13452",
            ],
        )
        self.assertNotIn("/approve", rendered)

    def test_renderer_is_deterministic_and_renders_c7_fields_only(self):
        card = TaskMessageCard(**BASE)

        first = render_task_message(card)
        second = render_task_message(card)
        self.assertEqual(first, second)
        self.assertIn("task_name: Prepare the reviewed artifact", first)
        self.assertIn("reason: Ready for a human decision", first)
        self.assertIn("next_action: Creates a local artifact only", first)
        self.assertIn("/task approve yeonje-13452", first)
        self.assertNotIn("version:", first)
        self.assertNotIn("evidence:", first)
        self.assertNotIn("risk:", first)
        self.assertNotIn("proposal: present", first)

    def test_unsafe_free_form_context_is_rejected_before_rendering(self):
        for field, value in (
            ("proposal", "cat README.md"),
            ("assessment", "grep TODO README.md"),
            ("risk", "ruby script.rb"),
            ("impact", "docker run app"),
            ("evidence", ("cat README.md",)),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, field: value})

    def test_credential_bearing_context_is_rejected_before_rendering(self):
        secret = "Password hunter2 is present"
        for field in ("proposal", "assessment", "risk", "impact"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as context:
                    TaskMessageCard(**{**BASE, field: secret})
                self.assertNotIn(secret, str(context.exception))

        with self.assertRaises(ValueError) as context:
            TaskMessageCard(**{**BASE, "evidence": (secret,)})
        self.assertNotIn(secret, str(context.exception))


    def test_shell_commands_and_bearer_credentials_are_rejected(self):
        for field in ("proposal", "assessment", "risk", "impact"):
            with self.subTest(kind="shell command", field=field):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, field: "rm -rf workspace"})
            with self.subTest(kind="bearer credential", field=field):
                with self.assertRaises(ValueError):
                    TaskMessageCard(
                        **{**BASE, field: "Authorization bearer hunter2 is present"}
                    )

        with self.assertRaises(ValueError):
            TaskMessageCard(**{**BASE, "evidence": ("rm -rf workspace",)})
        with self.assertRaises(ValueError):
            TaskMessageCard(
                **{**BASE, "evidence": ("Authorization bearer hunter2 is present",)}
            )
    def test_rendering_is_presentation_only(self):
        card = TaskMessageCard(**BASE)
        with patch("automation.message_cards.taskctl", create=True) as taskctl:
            rendered = render_task_message(card)

        taskctl.assert_not_called()
        self.assertEqual(card, TaskMessageCard(**BASE))
        self.assertTrue(rendered)

    def test_embedded_shell_commands_and_auth_headers_are_rejected(self):
        unsafe_values = (
            "echo hello",
            "ls -la",
            "docker run app",
            "Run rm -rf workspace",
            "Authorization: hunter2",
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    TaskMessageCard(**{**BASE, "proposal": value, "assessment": "Review"})


if __name__ == "__main__":
    unittest.main()
