import unittest

from automation.classification import Classification
from automation.models import WorkItem


def work_item(**overrides):
    base = {
        "task_id": "yeonje-1",
        "source": {
            "type": "board",
            "id": "yeonje",
            "external_id": "1",
            "url": "https://fixture.local/bbs/board.php?wr_id=1",
        },
        "received_at": "2026-08-28T09:44:00+09:00",
        "title": "제목",
        "body": "본문",
        "author": "요청부서",
        "attachments": [],
        "mask_table_ref": "local://masks/yeonje-1.json",
    }
    base.update(overrides)
    return WorkItem.from_dict(base)


def classification(**overrides):
    base = {
        "responsibility": "development",
        "task_type": "data_refresh",
        "size": "recurring",
        "confidence": 0.9,
    }
    base.update(overrides)
    return Classification.from_rule(
        base,
        evidence=["rule:heat-shelter-refresh 조건과 일치"],
        rule_ids=["heat-shelter-refresh"],
    )


BUNDLED_CONFIG = {
    "evaluation_order": ["responsibility", "task_type", "automation_level", "risk"],
    "min_classification_confidence": 0.6,
    "rules": [
        {
            "id": "heat-shelter-ready",
            "when": {
                "responsibility_in": ["development"],
                "task_type_in": ["data_refresh"],
                "size_in": ["recurring"],
            },
            "then": {
                "automation_level": "ready",
                "risk": "local_artifact_only",
                "confidence": 0.9,
                "recipe": "restarea-hwpx-to-xls",
            },
        },
        {
            "id": "unclear-manual",
            "when": {"size_in": ["unclear"]},
            "then": {
                "automation_level": "manual",
                "risk": "read_only",
                "confidence": 0.7,
            },
        },
    ],
}


class DeterministicEvaluatorTest(unittest.TestCase):
    def evaluate(self):
        try:
            from automation.evaluator import evaluate_with_rules
        except ModuleNotFoundError as exc:
            self.fail(f"automation.evaluator should exist: {exc}")
        return evaluate_with_rules

    # Step 1 — Ready / Manual / abstain.

    def test_ready_case(self):
        evaluate_with_rules = self.evaluate()
        result = evaluate_with_rules(work_item(), classification(), config=BUNDLED_CONFIG)

        self.assertIsNotNone(result)
        self.assertEqual(result.automation_level, "ready")
        self.assertEqual(result.risk, "local_artifact_only")
        self.assertEqual(result.recipe, "restarea-hwpx-to-xls")
        self.assertTrue(any("heat-shelter-ready" in e for e in result.evidence))

    def test_manual_case(self):
        evaluate_with_rules = self.evaluate()
        result = evaluate_with_rules(
            work_item(),
            classification(task_type="info_request", size="unclear"),
            config=BUNDLED_CONFIG,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.automation_level, "manual")
        self.assertIsNone(result.recipe)

    def test_abstains_when_no_rule_matches(self):
        evaluate_with_rules = self.evaluate()
        result = evaluate_with_rules(
            work_item(),
            classification(responsibility="content", task_type="content_edit", size="simple"),
            config=BUNDLED_CONFIG,
        )
        self.assertIsNone(result)

    def test_abstains_below_classification_confidence_floor(self):
        evaluate_with_rules = self.evaluate()
        result = evaluate_with_rules(
            work_item(), classification(confidence=0.3), config=BUNDLED_CONFIG
        )
        self.assertIsNone(result)

    def test_conflicting_rules_abstain(self):
        evaluate_with_rules = self.evaluate()
        config = dict(
            BUNDLED_CONFIG,
            rules=[
                {
                    "id": "a",
                    "when": {"responsibility_in": ["development"]},
                    "then": {"automation_level": "ready", "risk": "local_artifact_only", "confidence": 0.8, "recipe": "r"},
                },
                {
                    "id": "b",
                    "when": {"task_type_in": ["data_refresh"]},
                    "then": {"automation_level": "manual", "risk": "read_only", "confidence": 0.8},
                },
            ],
        )
        self.assertIsNone(evaluate_with_rules(work_item(), classification(), config=config))

    # Step 4 — evaluation order is read from config, not hardcoded.

    def test_evaluation_order_comes_from_config(self):
        from automation.evaluator import evaluation_order

        self.assertEqual(
            evaluation_order(BUNDLED_CONFIG),
            ["responsibility", "task_type", "automation_level", "risk"],
        )
        custom = dict(BUNDLED_CONFIG, evaluation_order=["risk", "automation_level", "task_type", "responsibility"])
        self.assertEqual(evaluation_order(custom), custom["evaluation_order"])

    def test_evaluation_order_drives_evidence_sequence(self):
        evaluate_with_rules = self.evaluate()
        default = evaluate_with_rules(work_item(), classification(), config=BUNDLED_CONFIG)
        reordered_config = dict(
            BUNDLED_CONFIG,
            evaluation_order=["risk", "automation_level", "task_type", "responsibility"],
        )
        reordered = evaluate_with_rules(work_item(), classification(), config=reordered_config)

        self.assertEqual(set(default.evidence), set(reordered.evidence))
        self.assertNotEqual(default.evidence, reordered.evidence)

    def test_unknown_evaluation_order_stage_is_rejected(self):
        evaluate_with_rules = self.evaluate()
        bad = dict(BUNDLED_CONFIG, evaluation_order=["responsibility", "vibes"])
        with self.assertRaisesRegex(ValueError, "vibes"):
            evaluate_with_rules(work_item(), classification(), config=bad)

    def test_bundled_config_matches_spec_order(self):
        from automation.evaluator import load_evaluation

        config = load_evaluation()
        self.assertEqual(
            config["evaluation_order"],
            ["responsibility", "task_type", "automation_level", "risk"],
        )
        self.assertGreater(len(config["rules"]), 0)

    # The shipped config/evaluation.json, not a test copy, drives these.

    def test_shipped_config_marks_heat_shelter_refresh_ready(self):
        evaluate_with_rules = self.evaluate()
        result = evaluate_with_rules(work_item(), classification())
        self.assertIsNotNone(result)
        self.assertEqual(result.automation_level, "ready")
        self.assertEqual(result.risk, "local_artifact_only")
        self.assertTrue(result.recipe)

    def test_shipped_config_abstains_on_unmatched_classification(self):
        evaluate_with_rules = self.evaluate()
        result = evaluate_with_rules(
            work_item(),
            classification(responsibility="content", task_type="content_edit", size="simple"),
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
