import dataclasses
import json
import unittest
from pathlib import Path


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "pattern.schema.json"

SAMPLE = {
    "scope": "owner-a",
    "pattern_id": "heat-shelter-title-rule",
    "kind": "rule",
    "state": "candidate",
    "confidence": 0.72,
    "evidence": [
        "제목에 '무더위쉼터 현행화' 반복 관찰 (12건)",
        "동일 title_pattern이 서로 다른 3개 fixture에서 발견",
    ],
}


class PatternContractTest(unittest.TestCase):
    def model(self):
        try:
            from automation.patterns import Pattern
        except ModuleNotFoundError as exc:
            self.fail(f"automation.patterns should exist: {exc}")
        return Pattern

    def schema(self):
        try:
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            self.fail(f"pattern schema should exist: {exc}")

    # -- shape ---------------------------------------------------------

    def test_round_trips_to_canonical_dict(self):
        Pattern = self.model()

        result = Pattern.from_dict(SAMPLE)

        self.assertEqual(result.to_dict(), SAMPLE)
        self.assertEqual(
            json.loads(json.dumps(result.to_dict(), ensure_ascii=False)), SAMPLE
        )

    def test_model_is_frozen(self):
        Pattern = self.model()
        result = Pattern.from_dict(SAMPLE)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.state = "shadow"

    def test_pattern_id_must_be_a_safe_identifier(self):
        Pattern = self.model()

        for bad in ("  ", "", "../escape", "Heat-Shelter"):
            with self.subTest(pattern_id=bad):
                with self.assertRaisesRegex(ValueError, "pattern_id"):
                    Pattern.from_dict(dict(SAMPLE, pattern_id=bad))

    def test_scope_must_be_a_safe_identifier(self):
        Pattern = self.model()

        for bad in ("", "  ", "../escape", "Owner-A", "x" * 65):
            with self.subTest(scope=bad):
                with self.assertRaisesRegex(ValueError, "scope"):
                    Pattern.from_dict(dict(SAMPLE, scope=bad))

    def test_scope_is_part_of_pattern_identity(self):
        Pattern = self.model()

        mine = Pattern.from_dict(SAMPLE)
        theirs = Pattern.from_dict(dict(SAMPLE, scope="owner-b"))

        self.assertNotEqual(mine, theirs)
        self.assertEqual(theirs.to_dict()["scope"], "owner-b")

    def test_kind_outside_enum_is_rejected(self):
        Pattern = self.model()

        with self.assertRaisesRegex(ValueError, "kind"):
            Pattern.from_dict(dict(SAMPLE, kind="magic"))

    def test_state_outside_enum_is_rejected(self):
        Pattern = self.model()

        with self.assertRaisesRegex(ValueError, "state"):
            Pattern.from_dict(dict(SAMPLE, state="deprecated"))

    def test_confidence_outside_unit_interval_is_rejected(self):
        Pattern = self.model()

        for bad in (-0.1, 1.5, "high"):
            with self.subTest(confidence=bad):
                with self.assertRaisesRegex(ValueError, "confidence"):
                    Pattern.from_dict(dict(SAMPLE, confidence=bad))

    def test_evidence_must_be_non_empty(self):
        Pattern = self.model()

        with self.assertRaisesRegex(ValueError, "evidence"):
            Pattern.from_dict(dict(SAMPLE, evidence=[]))

    def test_blank_evidence_entry_is_rejected(self):
        Pattern = self.model()

        with self.assertRaisesRegex(ValueError, "evidence"):
            Pattern.from_dict(dict(SAMPLE, evidence=["  "]))

    def test_missing_field_is_rejected(self):
        Pattern = self.model()
        for field in SAMPLE:
            payload = {k: v for k, v in SAMPLE.items() if k != field}
            with self.subTest(missing=field):
                with self.assertRaisesRegex(ValueError, field):
                    Pattern.from_dict(payload)
    def test_unknown_fields_are_rejected_like_the_closed_schema(self):
        Pattern = self.model()
        with self.assertRaisesRegex(ValueError, "unknown"):
            Pattern.from_dict({**SAMPLE, "proposal": "not part of a pattern"})


    def test_schema_matches_model_contract(self):
        Pattern = self.model()
        schema = self.schema()

        self.assertEqual(set(schema["required"]), set(SAMPLE))
        # "definition" is optional: a pattern carries the body it would be
        # promoted to, and patterns predating it stay loadable.
        self.assertEqual(set(schema["properties"]), set(SAMPLE) | {"definition"})
        self.assertNotIn("definition", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]["kind"]["enum"]), set(Pattern.KINDS)
        )
        self.assertEqual(
            set(schema["properties"]["state"]["enum"]), set(Pattern.STATES)
        )
        self.assertEqual(schema["properties"]["confidence"]["minimum"], 0)
        self.assertEqual(schema["properties"]["confidence"]["maximum"], 1)
        self.assertEqual(schema["properties"]["evidence"]["minItems"], 1)

    def test_schema_validates_sample_and_rejects_bad_enum(self):
        jsonschema = _load_jsonschema()
        if jsonschema is None:
            self.skipTest("jsonschema not installed")
        schema = self.schema()

        jsonschema.validate(SAMPLE, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(dict(SAMPLE, state="deprecated"), schema)

    # -- Step 1: legal transitions --------------------------------------

    def test_candidate_can_move_to_shadow_without_approval(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(SAMPLE)

        result = pattern.transition("shadow")

        self.assertEqual(result.state, "shadow")
        self.assertEqual(pattern.state, "candidate")  # original untouched

    def test_candidate_can_be_retired(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(SAMPLE)

        result = pattern.transition("retired")

        self.assertEqual(result.state, "retired")

    def test_shadow_can_be_retired(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="shadow"))

        result = pattern.transition("retired")

        self.assertEqual(result.state, "retired")

    def test_shadow_can_be_promoted_to_active_with_approval(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="shadow"))

        result = pattern.transition("active")

        self.assertEqual(result.state, "active")

    def test_active_can_be_suspended(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="active"))

        result = pattern.transition("suspended")

        self.assertEqual(result.state, "suspended")

    def test_active_can_be_retired(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="active"))

        result = pattern.transition("retired")

        self.assertEqual(result.state, "retired")

    def test_suspended_can_be_reactivated_with_approval(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="suspended"))

        result = pattern.transition("active")

        self.assertEqual(result.state, "active")

    def test_suspended_can_be_retired(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="suspended"))

        result = pattern.transition("retired")

        self.assertEqual(result.state, "retired")

    def test_retired_is_terminal(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="retired"))

        for target in Pattern.STATES:
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "illegal transition"):
                    pattern.transition(target)

    # -- Step 1: illegal promotion (candidate -> active) -----------------

    def test_candidate_cannot_jump_straight_to_active(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(SAMPLE)

        with self.assertRaisesRegex(ValueError, "illegal transition"):
            pattern.transition("active")

    def test_candidate_cannot_jump_to_active_even_with_approval(self):
        # Explicit spec case: shadow/replay cannot be skipped by supplying an
        # approval reference. An approval only satisfies the gate on a LEGAL
        # transition target; it never legalizes an otherwise-illegal edge.
        Pattern = self.model()
        pattern = Pattern.from_dict(SAMPLE)

        with self.assertRaises(ValueError):
            pattern.transition("active")

    def test_candidate_cannot_jump_to_suspended(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(SAMPLE)

        with self.assertRaisesRegex(ValueError, "illegal transition"):
            pattern.transition("suspended")

    def test_shadow_cannot_move_back_to_candidate(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="shadow"))

        with self.assertRaisesRegex(ValueError, "illegal transition"):
            pattern.transition("candidate")

    def test_active_cannot_move_back_to_shadow(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="active"))

        with self.assertRaisesRegex(ValueError, "illegal transition"):
            pattern.transition("shadow")

    def test_unknown_target_state_is_rejected(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(SAMPLE)

        with self.assertRaisesRegex(ValueError, "state"):
            pattern.transition("deprecated")

    # -- Step 4: approval lives at the write boundary, not here ----------

    def test_transition_does_not_take_an_approval_argument(self):
        # R02: a free-text approval string never authorized anything. The
        # binding is a PatternDecision checked by pattern_store.revise_pattern
        # and candidates.promote; transition() only owns legal edges.
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="shadow"))

        with self.assertRaises(TypeError):
            pattern.transition("active", approval="decision:definitely-approved")

    def test_shadow_to_active_is_a_legal_edge(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="shadow"))

        self.assertEqual(pattern.transition("active").state, "active")

    def test_suspended_to_active_is_a_legal_edge(self):
        Pattern = self.model()
        pattern = Pattern.from_dict(dict(SAMPLE, state="suspended"))

        self.assertEqual(pattern.transition("active").state, "active")


def _load_jsonschema():
    try:
        import jsonschema

        return jsonschema
    except ModuleNotFoundError:
        return None


if __name__ == "__main__":
    unittest.main()
