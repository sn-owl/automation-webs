import copy
import json
import tempfile
import unittest
from pathlib import Path

from automation.assessment import Assessment
from automation.classification import Classification
from automation.hermes_validator import (
    HermesValidationError,
    validate_hermes_assessment,
    validate_hermes_classification,
)
from automation.models import WorkItem
from automation.recognition import build_model_projection


VALID_RECIPE = {
    "recipe_id": "restarea-converter",
    "description": "HWPX를 XLS로 변환",
    "executor": "restarea-converter",
    "input_type": "hwpx",
    "output_type": "xls",
    "scope": "legacy",
}
REGISTRY = {"restarea-converter": VALID_RECIPE}
VALID_CLASSIFICATION = {
    "classification": {
        "responsibility": "시설 데이터 관리",
        "task_type": "현행화 요청",
        "size": "반복 업무",
        "ownership": "정보 부족",
        "primary_role": "시설 데이터 담당",
        "responsibility_scope": "입력 자료 검토와 현행화 준비",
        "collaboration": ["게시 담당"],
        "risk_flags": ["원격 게시 별도 승인"],
        "confidence": 0.91,
        "evidence": ["본문에 시설 자료 현행화 요청이 있음"],
        "next_action": "담당 소유권을 확인하세요.",
        "pattern_match": {},
        "unmatched_aspects": ["게시 담당"],
    }
}
VALID_ASSESSMENT = {
    "assessment": {
        "automation_level": "ready",
        "risk": "local_artifact_only",
        "confidence": 0.91,
        "evidence": ["허용된 로컬 변환 조건과 일치"],
        "recipe": "restarea-converter",
    }
}


class HermesOutputValidatorTest(unittest.TestCase):
    def test_classification_and_assessment_are_separate_typed_operations(self):
        classification = validate_hermes_classification(
            json.dumps(VALID_CLASSIFICATION)
        )
        assessment = validate_hermes_assessment(
            VALID_ASSESSMENT,
            ("restarea-converter",),
            recipes=REGISTRY,
        )
        self.assertIsInstance(classification, Classification)
        self.assertIsInstance(assessment, Assessment)
        self.assertEqual(classification.ownership, "정보 부족")
        self.assertEqual(assessment.recipe, "restarea-converter")

    def test_combined_or_wrong_operation_envelopes_are_rejected(self):
        combined = {**VALID_CLASSIFICATION, **VALID_ASSESSMENT}
        with self.assertRaisesRegex(HermesValidationError, "envelope"):
            validate_hermes_classification(combined)
        with self.assertRaisesRegex(HermesValidationError, "envelope"):
            validate_hermes_assessment(combined, (), recipes={})
        with self.assertRaisesRegex(HermesValidationError, "envelope"):
            validate_hermes_classification(VALID_ASSESSMENT)

    def test_open_domain_labels_are_accepted(self):
        payload = copy.deepcopy(VALID_CLASSIFICATION)
        payload["classification"]["responsibility"] = "새로운 비공개 조직 용어"
        payload["classification"]["task_type"] = "새로운 요청 유형"
        result = validate_hermes_classification(payload)
        self.assertEqual(result.task_type, "새로운 요청 유형")

    def test_a05_cases_remain_distinct_open_domain_classifications(self):
        cases = (
            ("development incident", "bug triage", "focused"),
            ("contract renewal", "agreement update", "recurring"),
            ("content", "location unspecified", "small"),
            ("coordination", "collaboration request", "medium"),
        )
        for responsibility, task_type, size in cases:
            with self.subTest(task_type=task_type):
                payload = copy.deepcopy(VALID_CLASSIFICATION)
                payload["classification"].update(
                    {
                        "responsibility": responsibility,
                        "task_type": task_type,
                        "size": size,
                    }
                )
                result = validate_hermes_classification(payload)
                self.assertEqual(
                    (result.responsibility, result.task_type, result.size),
                    (responsibility, task_type, size),
                )

    def test_source_and_user_identity_do_not_change_model_semantics(self):
        base = {
            "source": {
                "type": "board",
                "id": "source-a",
                "external_id": "42",
                "url": "local://source",
            },
            "received_at": "2026-09-09T00:00:00Z",
            "title": "협업 요청",
            "body": "계약 갱신 위치를 확인해 주세요.",
            "attachments": [],
            "mask_table_ref": "local://mask.json",
        }
        first = WorkItem.from_dict({"task_id": "task-a", "author": "user-a", **base})
        second = WorkItem.from_dict(
            {
                **base,
                "task_id": "task-b",
                "author": "user-b",
                "source": {
                    **base["source"],
                    "id": "source-b",
                    "external_id": "99",
                },
            }
        )
        policy = {"roles": ["coordinator"], "constraints": ["read-only"]}
        first_projection = build_model_projection(first, policy)
        second_projection = build_model_projection(second, policy)
        self.assertEqual(
            {key: value for key, value in first_projection.items() if key != "request_id"},
            {key: value for key, value in second_projection.items() if key != "request_id"},
        )

    def test_full_classification_fields_are_required(self):
        payload = copy.deepcopy(VALID_CLASSIFICATION)
        del payload["classification"]["ownership"]
        with self.assertRaisesRegex(HermesValidationError, "classification"):
            validate_hermes_classification(payload)

    def test_malformed_json_is_rejected_without_raw_text(self):
        secret = "unique-hermes-secret-7d7d"
        with self.assertRaisesRegex(HermesValidationError, "valid JSON") as caught:
            validate_hermes_classification("{not-json:" + secret)
        self.assertNotIn(secret, str(caught.exception))

    def test_assessment_recipe_requires_allow_list_and_registry(self):
        with self.assertRaisesRegex(HermesValidationError, "allowed"):
            validate_hermes_assessment(
                VALID_ASSESSMENT, ("another-recipe",), recipes=REGISTRY
            )
        with self.assertRaisesRegex(HermesValidationError, "registered"):
            validate_hermes_assessment(
                VALID_ASSESSMENT, ("restarea-converter",), recipes={}
            )

    def test_non_ready_assessment_without_recipe_accepts_empty_registry(self):
        payload = {
            "assessment": {
                "automation_level": "manual",
                "risk": "read_only",
                "confidence": 0.4,
                "evidence": ["지원 능력을 확인할 수 없음"],
            }
        }
        result = validate_hermes_assessment(payload, (), recipes={})
        self.assertIsNone(result.recipe)

    def test_invalid_values_and_extra_fields_are_rejected(self):
        for field, value in (
            ("automation_level", "semi"),
            ("risk", "unknown"),
            ("confidence", True),
            ("evidence", []),
        ):
            with self.subTest(field=field):
                payload = copy.deepcopy(VALID_ASSESSMENT)
                payload["assessment"][field] = value
                with self.assertRaises(HermesValidationError):
                    validate_hermes_assessment(
                        payload, ("restarea-converter",), recipes=REGISTRY
                    )
        payload = copy.deepcopy(VALID_ASSESSMENT)
        payload["assessment"]["extra"] = True
        with self.assertRaisesRegex(HermesValidationError, "fields"):
            validate_hermes_assessment(
                payload, ("restarea-converter",), recipes=REGISTRY
            )

    def test_forbidden_keys_are_rejected_recursively(self):
        payload = copy.deepcopy(VALID_CLASSIFICATION)
        payload["classification"]["pattern_match"] = {
            "metadata": {"command": "touch should-not-run"}
        }
        with self.assertRaisesRegex(HermesValidationError, "forbidden"):
            validate_hermes_classification(payload)

    def test_command_like_evidence_is_inert_data(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "sentinel"
            payload = copy.deepcopy(VALID_CLASSIFICATION)
            command_text = f"python -c 'open({str(sentinel)!r}, 'w').write('x')'"
            payload["classification"]["evidence"] = [command_text]
            result = validate_hermes_classification(payload)
            self.assertEqual(result.evidence, (command_text,))
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
