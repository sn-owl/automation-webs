import dataclasses
import json
import unittest
from pathlib import Path


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "classification.schema.json"
SAMPLE = {
    "responsibility": "public-safety coordination",
    "task_type": "seasonal facility data reconciliation",
    "size": "recurring",
    "ownership": "정보 부족",
    "primary_role": "facility data steward",
    "responsibility_scope": "입력 자료 검토와 현행화 준비",
    "collaboration": ["게시 담당"],
    "risk_flags": ["원격 반영은 별도 승인 필요"],
    "confidence": 0.94,
    "evidence": ["본문에 시설 자료 현행화 요청이 있음"],
    "next_action": "담당 소유권을 확인하세요.",
    "pattern_match": {"rule_ids": ["facility-refresh"]},
    "unmatched_aspects": ["실제 게시 담당"],
}


class ClassificationContractTest(unittest.TestCase):
    def model(self):
        from automation.classification import Classification

        return Classification

    def schema(self):
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_round_trips_full_user_intent_contract(self):
        result = self.model().from_dict(SAMPLE)
        self.assertEqual(result.to_dict(), SAMPLE)
        self.assertEqual(
            json.loads(json.dumps(result.to_dict(), ensure_ascii=False)), SAMPLE
        )

    def test_model_is_frozen(self):
        result = self.model().from_dict(SAMPLE)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.size = "simple"

    def test_domain_labels_are_extensible_but_non_empty(self):
        Classification = self.model()
        result = Classification.from_dict(
            dict(
                SAMPLE,
                responsibility="new municipal domain",
                task_type="newly declared action",
                size="organization-defined scope",
            )
        )
        self.assertEqual(result.responsibility, "new municipal domain")
        for field in ("responsibility", "task_type", "size"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    Classification.from_dict(dict(SAMPLE, **{field: " "}))

    def test_ownership_and_evidence_are_validated(self):
        Classification = self.model()
        with self.assertRaisesRegex(ValueError, "ownership"):
            Classification.from_dict(dict(SAMPLE, ownership="모름"))
        with self.assertRaisesRegex(ValueError, "evidence"):
            Classification.from_dict(dict(SAMPLE, evidence=[]))

    def test_exact_full_field_set_is_required(self):
        Classification = self.model()
        for field in SAMPLE:
            with self.subTest(missing=field):
                payload = {key: value for key, value in SAMPLE.items() if key != field}
                with self.assertRaisesRegex(ValueError, "required fields"):
                    Classification.from_dict(payload)
        with self.assertRaisesRegex(ValueError, "required fields"):
            Classification.from_dict(dict(SAMPLE, extra=True))

    def test_confidence_is_bounded(self):
        Classification = self.model()
        for value in (-0.1, 1.5, "high", True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "confidence"):
                    Classification.from_dict(dict(SAMPLE, confidence=value))

    def test_schema_matches_and_validates_model_contract(self):
        Classification = self.model()
        schema = self.schema()
        self.assertEqual(set(schema["required"]), Classification.FIELDS)
        self.assertEqual(set(schema["properties"]), Classification.FIELDS)
        self.assertEqual(
            set(schema["properties"]["ownership"]["enum"]),
            set(Classification.OWNERSHIPS),
        )
        self.assertFalse(schema["additionalProperties"])
        jsonschema = _load_jsonschema()
        if jsonschema is None:
            self.skipTest("jsonschema not installed")
        jsonschema.validate(SAMPLE, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(dict(SAMPLE, ownership="모름"), schema)


def _load_jsonschema():
    try:
        import jsonschema

        return jsonschema
    except ModuleNotFoundError:
        return None


if __name__ == "__main__":
    unittest.main()
