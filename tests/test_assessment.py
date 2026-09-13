import dataclasses
import json
import unittest
from pathlib import Path


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "assessment.schema.json"

READY = {
    "automation_level": "ready",
    "risk": "local_artifact_only",
    "confidence": 0.96,
    "recipe": "heat-shelter-xls-v1",
    "evidence": ["검증된 Recipe 조건과 일치", "HWPX 첨부파일 존재"],
}

MANUAL = {
    "automation_level": "manual",
    "risk": "remote_write",
    "confidence": 0.7,
    "evidence": ["로그인·업로드 책임 필요"],
}


class AssessmentContractTest(unittest.TestCase):
    def model(self):
        try:
            from automation.assessment import Assessment
        except ModuleNotFoundError as exc:
            self.fail(f"automation.assessment should exist: {exc}")
        return Assessment

    def schema(self):
        try:
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            self.fail(f"assessment schema should exist: {exc}")

    def test_ready_round_trips_with_recipe(self):
        Assessment = self.model()
        result = Assessment.from_dict(READY)

        self.assertEqual(result.to_dict(), READY)
        self.assertEqual(
            json.loads(json.dumps(result.to_dict(), ensure_ascii=False)), READY
        )

    def test_non_ready_round_trips_without_recipe_key(self):
        Assessment = self.model()
        result = Assessment.from_dict(MANUAL)

        self.assertIsNone(result.recipe)
        self.assertNotIn("recipe", result.to_dict())
        self.assertEqual(result.to_dict(), MANUAL)

    def test_model_is_frozen(self):
        Assessment = self.model()
        result = Assessment.from_dict(READY)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.confidence = 0.1

    def test_level_outside_enum_is_rejected(self):
        Assessment = self.model()
        with self.assertRaisesRegex(ValueError, "automation_level"):
            Assessment.from_dict(dict(READY, automation_level="semi"))

    def test_risk_outside_enum_is_rejected(self):
        Assessment = self.model()
        with self.assertRaisesRegex(ValueError, "risk"):
            Assessment.from_dict(dict(READY, risk="scary"))

    def test_confidence_outside_unit_interval_is_rejected(self):
        Assessment = self.model()
        for bad in (-0.01, 1.01, "high", True):
            with self.subTest(confidence=bad):
                with self.assertRaisesRegex(ValueError, "confidence"):
                    Assessment.from_dict(dict(READY, confidence=bad))

    def test_evidence_must_be_non_empty(self):
        Assessment = self.model()
        with self.assertRaisesRegex(ValueError, "evidence"):
            Assessment.from_dict(dict(READY, evidence=[]))

    # Step 4 — Ready without a recipe is a contradiction.

    def test_ready_without_recipe_is_rejected(self):
        Assessment = self.model()
        payload = {k: v for k, v in READY.items() if k != "recipe"}
        with self.assertRaisesRegex(ValueError, "recipe"):
            Assessment.from_dict(payload)

    def test_ready_with_blank_recipe_is_rejected(self):
        Assessment = self.model()
        with self.assertRaisesRegex(ValueError, "recipe"):
            Assessment.from_dict(dict(READY, recipe="  "))

    def test_recipe_on_non_ready_level_is_kept(self):
        Assessment = self.model()
        result = Assessment.from_dict(dict(MANUAL, recipe="draft-ref"))
        self.assertEqual(result.recipe, "draft-ref")
        self.assertEqual(result.to_dict()["recipe"], "draft-ref")
    def test_unknown_fields_are_rejected_like_the_closed_schema(self):
        Assessment = self.model()
        with self.assertRaisesRegex(ValueError, "unknown"):
            Assessment.from_dict({**MANUAL, "next_action": "not part of assessment"})


    def test_schema_matches_model_contract(self):
        Assessment = self.model()
        schema = self.schema()

        self.assertEqual(
            set(schema["required"]),
            {"automation_level", "risk", "confidence", "evidence"},
        )
        self.assertIn("recipe", schema["properties"])
        self.assertNotIn("recipe", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]["automation_level"]["enum"]),
            set(Assessment.LEVELS),
        )
        self.assertEqual(set(schema["properties"]["risk"]["enum"]), set(Assessment.RISKS))
        self.assertNotIn("null", _schema_types(schema))

    def test_schema_validates_samples(self):
        try:
            import jsonschema
        except ModuleNotFoundError:
            self.skipTest("jsonschema not installed")
        schema = self.schema()
        jsonschema.validate(READY, schema)
        jsonschema.validate(MANUAL, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(dict(READY, automation_level="semi"), schema)


def _schema_types(node):
    found = []
    if isinstance(node, dict):
        value = node.get("type")
        if isinstance(value, list):
            found.extend(value)
        elif isinstance(value, str):
            found.append(value)
        for child in node.values():
            found.extend(_schema_types(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_schema_types(child))
    return found


if __name__ == "__main__":
    unittest.main()
