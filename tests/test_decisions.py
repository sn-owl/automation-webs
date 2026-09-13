from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "decision.schema.json"

BASE = {
    "actor": "alice",
    "task_id": "alpha-13452",
    "task_version": 3,
    "action": "approve",
    "reason": "Matches the reviewed request",
    "timestamp": "2026-09-02T10:15:00+09:00",
}


class DecisionContractTest(unittest.TestCase):
    def model(self):
        try:
            from automation.decisions import Decision
        except ModuleNotFoundError as exc:
            self.fail(f"automation.decisions should exist: {exc}")
        return Decision

    def schema(self):
        try:
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            self.fail(f"decision schema should exist: {exc}")

    def test_approval_round_trips_required_metadata(self):
        Decision = self.model()
        decision = Decision.from_dict(BASE)

        self.assertEqual(decision.to_dict(), BASE)
        self.assertEqual(json.loads(json.dumps(decision.to_dict())), BASE)

    def test_modify_round_trips_details_and_reason(self):
        Decision = self.model()
        payload = dict(
            BASE,
            action="modify",
            reason="Correct the requested publication date",
            details={"publication_date": "2026-09-03", "notify": True},
        )

        decision = Decision.from_dict(payload)

        self.assertEqual(decision.to_dict(), payload)
        self.assertEqual(decision.details["publication_date"], "2026-09-03")

    def test_decision_is_frozen_and_details_are_immutable(self):
        Decision = self.model()
        decision = Decision.from_dict(
            dict(BASE, action="modify", details={"fields": {"title": "new title"}})
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.reason = "changed"
        with self.assertRaises(TypeError):
            decision.details["new"] = "value"
        with self.assertRaises(TypeError):
            decision.details["fields"]["title"] = "changed"

    def test_action_is_closed(self):
        Decision = self.model()
        for action in ("approve", "reject", "modify", "defer"):
            payload = dict(BASE, action=action)
            if action == "modify":
                payload["details"] = {"title": "new"}
            with self.subTest(action=action):
                self.assertEqual(Decision.from_dict(payload).action, action)

        with self.assertRaisesRegex(ValueError, "action"):
            Decision.from_dict(dict(BASE, action="cancel"))

    def test_required_metadata_is_rejected_when_missing_or_blank(self):
        Decision = self.model()
        for key in ("actor", "task_id", "task_version", "action", "reason", "timestamp"):
            with self.subTest(key=key):
                payload = dict(BASE)
                payload.pop(key)
                with self.assertRaisesRegex(ValueError, key):
                    Decision.from_dict(payload)

        for key in ("actor", "task_id", "reason", "timestamp"):
            with self.subTest(blank=key):
                with self.assertRaisesRegex(ValueError, key):
                    Decision.from_dict(dict(BASE, **{key: "  "}))

    def test_modify_requires_object_details(self):
        Decision = self.model()
        for details in (None, "not an object", {}, []):
            with self.subTest(details=details):
                payload = dict(BASE, action="modify", details=details)
                with self.assertRaisesRegex(ValueError, "details"):
                    Decision.from_dict(payload)

    def test_approval_for_stale_task_version_is_rejected_locally(self):
        Decision = self.model()

        with self.assertRaisesRegex(ValueError, "stale"):
            Decision.from_dict(BASE, current_task_version=4)

        current = Decision.from_dict(BASE, current_task_version=3)
        self.assertEqual(current.task_version, 3)

    def test_stale_non_approval_decisions_are_allowed(self):
        Decision = self.model()
        for action in ("reject", "modify", "defer"):
            payload = dict(BASE, action=action)
            if action == "modify":
                payload["details"] = {"title": "new"}
            with self.subTest(action=action):
                self.assertEqual(
                    Decision.from_dict(payload, current_task_version=4).action, action
                )

    def test_schema_is_closed_and_matches_model(self):
        Decision = self.model()
        schema = self.schema()

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"actor", "task_id", "task_version", "action", "reason", "timestamp"},
        )
        self.assertEqual(set(schema["properties"]["action"]["enum"]), set(Decision.ACTIONS))
        self.assertIn("details", schema["properties"])
        self.assertNotIn("details", schema["required"])

    def test_schema_validates_samples(self):
        try:
            import jsonschema
        except ModuleNotFoundError:
            self.skipTest("jsonschema not installed")
        schema = self.schema()
        jsonschema.validate(BASE, schema)
        jsonschema.validate(
            dict(BASE, action="modify", details={"title": "new"}), schema
        )
        jsonschema.validate(
            dict(
                BASE,
                action="reject",
                details={"reject_signal": "out_of_scope"},
            ),
            schema,
        )
        jsonschema.validate(
            dict(
                BASE,
                action="reject",
                details={
                    "reject_signal": "classification_correction",
                    "classification": self._sample_classification(),
                },
            ),
            schema,
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(dict(BASE, action="cancel"), schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(dict(BASE, action="modify"), schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                dict(
                    BASE,
                    action="reject",
                    details={"reject_signal": "classification_correction"},
                ),
                schema,
            )
    @staticmethod
    def _sample_classification():
        return {
            "responsibility": "design",
            "task_type": "image_popup",
            "size": "simple",
            "ownership": "내 업무",
            "primary_role": "design",
            "responsibility_scope": "팝업 게재",
            "collaboration": [],
            "risk_flags": [],
            "confidence": 0.85,
            "evidence": ["팝업 게재는 디자인팀 소관"],
            "next_action": "게재 범위를 확인하세요.",
            "pattern_match": {},
            "unmatched_aspects": [],
        }


class DecisionCorrectionTest(unittest.TestCase):
    """details["classification"], the one key Core reads back out of a decision."""

    CORRECTION = {
        "responsibility": "design",
        "task_type": "image_popup",
        "size": "simple",
        "ownership": "내 업무",
        "primary_role": "design",
        "responsibility_scope": "팝업 게재",
        "collaboration": [],
        "risk_flags": [],
        "confidence": 0.85,
        "evidence": ["팝업 게재는 디자인팀 소관"],
        "next_action": "게재 범위를 확인하세요.",
        "pattern_match": {},
        "unmatched_aspects": [],
    }

    def modify(self, details):
        from automation.decisions import Decision

        return Decision.from_dict(dict(BASE, action="modify", details=details))

    def test_a_correction_round_trips_as_a_classification(self):
        correction = self.modify({"classification": self.CORRECTION}).correction()
        self.assertEqual(correction.responsibility, "design")
        self.assertEqual(correction.task_type, "image_popup")

    def test_prose_details_carry_no_correction(self):
        self.assertIsNone(self.modify({"note": "좀더필요"}).correction())

    def test_a_malformed_correction_is_refused_when_it_is_recorded(self):
        # Rejected now, while the operator still has the task in front of them,
        # rather than silently skipped later by the pattern pipeline.
        for broken in (
            {"classification": {"responsibility": "marketing"}},
            {"classification": dict(self.CORRECTION, size=" ")},
            {"classification": dict(self.CORRECTION, confidence=2)},
            {"classification": "design"},
        ):
            with self.subTest(details=broken), self.assertRaises(ValueError):
                self.modify(broken)

    def test_reject_signals_distinguish_learning_from_scope(self):
        from automation.decisions import Decision

        correction = Decision.from_dict(
            dict(
                BASE,
                action="reject",
                details={
                    "reject_signal": "classification_correction",
                    "classification": self.CORRECTION,
                },
            )
        )
        self.assertEqual(correction.rejection_signal(), "classification_correction")
        self.assertEqual(correction.correction().responsibility, "design")

        out_of_scope = Decision.from_dict(
            dict(
                BASE,
                action="reject",
                details={"reject_signal": "out_of_scope"},
            )
        )
        self.assertEqual(out_of_scope.rejection_signal(), "out_of_scope")
        self.assertIsNone(out_of_scope.correction())

    def test_reject_signal_requires_matching_correction_shape(self):
        from automation.decisions import Decision

        cases = (
            {"reject_signal": "unknown"},
            {"reject_signal": "classification_correction"},
            {
                "reject_signal": "out_of_scope",
                "classification": self.CORRECTION,
            },
            {"classification": self.CORRECTION},
        )
        for details in cases:
            with self.subTest(details=details), self.assertRaises(ValueError):
                Decision.from_dict(dict(BASE, action="reject", details=details))

    def test_a_correction_on_a_non_modify_decision_is_still_validated(self):
        from automation.decisions import Decision

        with self.assertRaises(ValueError):
            Decision.from_dict(
                dict(BASE, action="defer", details={"classification": {"responsibility": "x"}})
            )


if __name__ == "__main__":
    unittest.main()
